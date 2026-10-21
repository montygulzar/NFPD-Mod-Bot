"""Private bot-to-bot API, mounted on the health server.

Used by the NFPD Appeals Bot: when an appeal is accepted and staff press
Execute Unban, the Appeals Bot (which isn't a moderator in the main server)
asks this bot to lift the ban, so the unban happens here and is recorded as a
normal mod-log case.

    POST /internal/unban
    Authorization: Bearer <INTERNAL_API_TOKEN>
    {"guild_id": int, "user_id": int, "moderator_id": int,
     "reason": str, "appeal_id": int}

Responses (always JSON with a "status" field):
    200 unbanned     - ban lifted; "case_id" is the mod-log case
    200 not_banned   - user wasn't banned (already lifted) - safe to treat as done
    401 unauthorized - missing or wrong token
    400 bad_request  - malformed body
    403 forbidden    - guild not approved, or no Ban Members permission
    404 error        - this bot isn't in that server
    502 error        - Discord API error
    503 disabled     - INTERNAL_API_TOKEN not set, endpoint off

Unlike /health and /ready, this endpoint CHANGES state, so it requires a
shared secret even though the port is only reachable on the Docker network
(other containers share that network). With no token configured it is off.
"""
import hmac
import logging

import discord
from aiohttp import web

import config
from database import add_case, remove_temp_ban
from embeds import audit_reason, build_case_embed
from modlog import post_to_log_channel

logger = logging.getLogger("modbot.internal_api")

REASON_MAX = 400


def _token_ok(request: web.Request) -> bool:
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    # Constant-time compare so the token can't be guessed byte by byte.
    return hmac.compare_digest(supplied.encode(), config.INTERNAL_API_TOKEN.encode())


def _int_field(body: dict, name: str) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(name)
    return value


class UnbanHandler:
    def __init__(self, bot) -> None:
        self._bot = bot

    async def __call__(self, request: web.Request) -> web.Response:
        if not config.INTERNAL_API_TOKEN:
            return web.json_response({"status": "disabled"}, status=503)
        if not _token_ok(request):
            logger.warning("Rejected /internal/unban call with a missing or invalid token")
            return web.json_response({"status": "unauthorized"}, status=401)

        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body")
            guild_id = _int_field(body, "guild_id")
            user_id = _int_field(body, "user_id")
            moderator_id = _int_field(body, "moderator_id")
            appeal_id = body.get("appeal_id")
            reason = str(body.get("reason") or f"Ban appeal #{appeal_id} accepted")[:REASON_MAX]
        except (ValueError, TypeError) as error:
            return web.json_response({"status": "bad_request", "detail": f"invalid field: {error}"}, status=400)

        if config.APPROVED_GUILD_IDS and guild_id not in config.APPROVED_GUILD_IDS:
            return web.json_response(
                {"status": "forbidden", "detail": "That server is not in this bot's APPROVED_GUILD_IDS."},
                status=403,
            )
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return web.json_response(
                {"status": "error", "detail": "The Mod Bot is not in that server."}, status=404,
            )

        moderator = await self._resolve_user(moderator_id) or self._bot.user
        try:
            await guild.unban(
                discord.Object(id=user_id),
                reason=audit_reason(moderator, "Appeal unban", reason),
            )
        except discord.NotFound:
            logger.info("Appeal unban: user %s was not banned in %s", user_id, guild_id)
            return web.json_response({"status": "not_banned", "detail": "User was not banned."})
        except discord.Forbidden:
            return web.json_response(
                {"status": "forbidden", "detail": "The Mod Bot lacks Ban Members in that server."},
                status=403,
            )
        except discord.HTTPException as error:
            logger.warning("Appeal unban of %s in %s failed: %s", user_id, guild_id, error)
            return web.json_response(
                {"status": "error", "detail": f"Discord API error ({error.status})."}, status=502,
            )

        # The ban is lifted. Everything below is bookkeeping and must not turn
        # a successful unban into a reported failure (the caller would retry).
        case_id = None
        try:
            await remove_temp_ban(guild.id, user_id)  # a pending tempban must not re-fire
        except Exception:
            logger.exception("Appeal unban: could not clear temp-ban row for %s", user_id)
        try:
            target = await self._resolve_user(user_id) or discord.Object(id=user_id)
            case_id = await add_case(guild.id, user_id, moderator.id, "unban", reason)
            if isinstance(target, discord.abc.User):
                await post_to_log_channel(guild, build_case_embed("unban", target, moderator, reason, case_id))
        except Exception:
            logger.exception("Appeal unban of %s succeeded but the mod-log case could not be recorded", user_id)

        logger.info(
            "Appeal unban: user %s unbanned in %s (appeal %s, moderator %s, case %s)",
            user_id, guild_id, appeal_id, moderator_id, case_id,
        )
        return web.json_response({"status": "unbanned", "case_id": case_id})

    async def _resolve_user(self, user_id: int):
        user = self._bot.get_user(user_id)
        if user is not None:
            return user
        try:
            return await self._bot.fetch_user(user_id)
        except discord.HTTPException:
            return None
