"""HTTP health endpoints for container orchestration and monitoring.

Two endpoints, with the distinction that matters to Docker:

    /health   liveness  - the process is up and its event loop is responsive.
                          Answering at all is the signal. Used by HEALTHCHECK.
    /ready    readiness - the bot is logged into Discord *and* the database is
                          reachable. Returns 503 when it is not, so a degraded
                          container is visible in `docker ps` and to whatever is
                          watching on the monitoring network.

Deliberately not published to the host in the compose file - it is reachable only
from inside the Docker network, so it needs no authentication.

aiohttp is used directly rather than pulled in for this alone: discord.py already
depends on it, and it is declared explicitly in requirements.txt.
"""
import logging
import time

from aiohttp import web

import config
import database
import diagnostics

logger = logging.getLogger("modbot.health")

# Readiness tolerates a brief Discord reconnect: discord.py resumes sessions on its
# own within seconds, and flapping the container's health state during a normal
# resume would be noise rather than signal.
DISCORD_GRACE_SECONDS = 90.0


class HealthServer:
    def __init__(self, bot) -> None:
        self._bot = bot
        self._runner: web.AppRunner | None = None
        self._disconnected_since: float | None = None

    # -- state tracking -------------------------------------------------------

    def note_disconnected(self) -> None:
        if self._disconnected_since is None:
            self._disconnected_since = time.monotonic()

    def note_connected(self) -> None:
        self._disconnected_since = None

    def _discord_state(self) -> tuple[bool, str]:
        if self._bot.is_closed():
            return False, "client closed"
        if not self._bot.is_ready():
            return False, "connecting"
        if self._disconnected_since is not None:
            outage = time.monotonic() - self._disconnected_since
            if outage > DISCORD_GRACE_SECONDS:
                return False, f"disconnected for {outage:.0f}s"
            return True, f"reconnecting ({outage:.0f}s)"
        return True, "connected"

    # -- endpoints ------------------------------------------------------------

    async def _liveness(self, request: web.Request) -> web.Response:
        # Reaching this handler proves the loop is scheduling work, which is the
        # whole question liveness asks. It stays 200 while dependencies are down,
        # because restarting the container would not fix a dependency outage.
        return web.json_response({
            "status": "alive",
            "uptime_seconds": round(diagnostics.uptime_seconds(), 1),
            "version": config.APP_VERSION,
            "commit": config.GIT_COMMIT,
        })

    async def _readiness(self, request: web.Request) -> web.Response:
        database_ok, database_detail = await database.check_connection()
        discord_ok, discord_detail = self._discord_state()
        ready = database_ok and discord_ok

        payload = {
            "status": "ready" if ready else "degraded",
            "uptime_seconds": round(diagnostics.uptime_seconds(), 1),
            "version": config.APP_VERSION,
            "commit": config.GIT_COMMIT,
            "discord": {
                "ok": discord_ok,
                "detail": discord_detail,
                "guilds": len(self._bot.guilds),
                "latency_ms": _latency_ms(self._bot),
            },
            "database": {
                "ok": database_ok,
                "detail": database_detail,
                **{k: v for k, v in database.pool_stats().items() if k != "connected"},
            },
        }
        return web.json_response(payload, status=200 if ready else 503)

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if not config.HEALTH_SERVER_ENABLED:
            logger.info("Health server disabled (HEALTH_SERVER_ENABLED=false)")
            return

        app = web.Application()
        app.router.add_get("/health", self._liveness)
        app.router.add_get("/ready", self._readiness)

        # access_log=None: every health probe would otherwise log a line, which at a
        # 30-second interval buries everything else in the container logs.
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, config.HEALTH_HOST, config.HEALTH_PORT)
        try:
            await site.start()
        except OSError as error:
            # A busy port must not take the bot down with it.
            await runner.cleanup()
            logger.error(
                "Health server could not bind %s:%d (%s) - continuing without it",
                config.HEALTH_HOST, config.HEALTH_PORT, error,
            )
            return

        self._runner = runner
        logger.info("Health server listening on %s:%d", config.HEALTH_HOST, config.HEALTH_PORT)

    async def stop(self) -> None:
        if self._runner is None:
            return
        runner, self._runner = self._runner, None
        try:
            await runner.cleanup()
            logger.info("Health server stopped")
        except Exception as error:
            logger.warning("Error stopping the health server: %s", error)


def _latency_ms(bot) -> float | None:
    """discord.py reports NaN before the first heartbeat, which is not valid JSON."""
    latency = bot.latency
    if latency != latency or latency in (float("inf"), float("-inf")):
        return None
    return round(latency * 1000, 1)
