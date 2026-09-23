"""PostgreSQL data layer (asyncpg).

Connection model
    A single pool is opened at startup and shared by every coroutine. The pool is
    the only thing that talks to Postgres; nothing else opens connections.

Availability
    The bot and Postgres are separate containers that start, stop and restart
    independently, so neither "Postgres is not up yet" nor "Postgres went away for
    ten seconds" is treated as fatal. Startup retries with exponential backoff, and
    individual queries retry a bounded number of times. Errors that no amount of
    retrying will fix - bad password, missing database, missing privileges - fail
    immediately with a message that says what to correct.

Conventions
    Queries use $1, $2 ... placeholders (asyncpg style).
    Discord snowflakes are BIGINT; they overflow a 32-bit INT.
"""
import asyncio
import json
import logging
import random
import time as _time
from datetime import datetime, timezone

import asyncpg

import config

logger = logging.getLogger("modbot.database")

_pool: asyncpg.Pool | None = None

# Set when the pool is deliberately closed, so in-flight queries racing with
# shutdown fail fast instead of retrying against a pool that is going away.
_closing = False

# Tracks the outcome of the most recent database interaction, for the health
# endpoint and the /debug report.
_last_success_at: float | None = None
_last_failure: str | None = None

# Advisory-lock key for schema setup. Arbitrary but must stay stable: it stops two
# containers started at the same moment from running CREATE TABLE concurrently.
_SCHEMA_LOCK_KEY = 0x4E46_5044  # "NFPD"

# Transient faults: the connection died or the server is not accepting work yet.
# Retrying these is worthwhile.
_RETRYABLE_ERRORS = (
    asyncpg.exceptions.PostgresConnectionError,
    asyncpg.exceptions.CannotConnectNowError,
    asyncpg.exceptions.TooManyConnectionsError,
    asyncpg.exceptions.AdminShutdownError,
    asyncpg.exceptions.CrashShutdownError,
    asyncpg.InterfaceError,
    OSError,  # includes ConnectionRefusedError / ConnectionResetError
    asyncio.TimeoutError,
)

# Misconfiguration: retrying forever would hide the real problem behind an endless
# "still waiting for the database" loop, so these abort startup immediately.
_FATAL_CONFIG_ERRORS = (
    asyncpg.exceptions.InvalidPasswordError,
    asyncpg.exceptions.InvalidCatalogNameError,          # database does not exist
    asyncpg.exceptions.InvalidAuthorizationSpecificationError,
    asyncpg.exceptions.InsufficientPrivilegeError,
)


class DatabaseUnavailable(RuntimeError):
    """Raised when a query cannot reach Postgres after exhausting its retries."""


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS cases (
        id            SERIAL PRIMARY KEY,
        guild_id      BIGINT NOT NULL,
        user_id       BIGINT NOT NULL,
        moderator_id  BIGINT NOT NULL,
        action_type   TEXT NOT NULL,
        reason        TEXT,
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cases_guild_user   ON cases (guild_id, user_id)",
    "CREATE INDEX IF NOT EXISTS idx_cases_guild_action ON cases (guild_id, action_type)",
    # Backs the per-guild case listing and the CSV export, both of which order by id.
    "CREATE INDEX IF NOT EXISTS idx_cases_guild_id     ON cases (guild_id, id)",
    # Backs the "most active moderators" leaderboard.
    "CREATE INDEX IF NOT EXISTS idx_cases_guild_mod    ON cases (guild_id, moderator_id)",
    """
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id                  BIGINT PRIMARY KEY,
        log_channel_id            BIGINT,
        server_log_channel_id     BIGINT,
        lockdown_role_id          BIGINT,
        raid_min_account_age_hours INTEGER,
        warn_mute_threshold        INTEGER,
        warn_mute_minutes          INTEGER,
        warn_kick_threshold        INTEGER,
        warn_ban_threshold         INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lockdown_roles (
        guild_id BIGINT NOT NULL,
        role_id  BIGINT NOT NULL,
        PRIMARY KEY (guild_id, role_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS temp_bans (
        guild_id  BIGINT NOT NULL,
        user_id   BIGINT NOT NULL,
        unban_at  TEXT NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_temp_bans_unban_at ON temp_bans (unban_at)",
    """
    CREATE TABLE IF NOT EXISTS channel_locks (
        guild_id       BIGINT NOT NULL,
        channel_id     BIGINT NOT NULL,
        previous_state TEXT NOT NULL,
        PRIMARY KEY (guild_id, channel_id)
    )
    """,
)

# Columns added after the first release. Applied with ADD COLUMN IF NOT EXISTS so an
# existing database gains them in place - CREATE TABLE IF NOT EXISTS alone would skip
# an already-created table and silently leave the new column missing.
MIGRATION_STATEMENTS = (
    "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS server_log_channel_id BIGINT",
    "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS warn_mute_threshold   INTEGER",
    "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS warn_mute_minutes     INTEGER",
    "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS warn_kick_threshold   INTEGER",
    "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS warn_ban_threshold    INTEGER",
)


# --- Connection lifecycle -----------------------------------------------------

async def _new_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=config.DATABASE_URL,
        min_size=config.DB_POOL_MIN_SIZE,
        max_size=config.DB_POOL_MAX_SIZE,
        command_timeout=config.DB_COMMAND_TIMEOUT,
        max_inactive_connection_lifetime=config.DB_MAX_INACTIVE_CONNECTION_LIFETIME,
        # Shows up in pg_stat_activity, so it is obvious which client owns a
        # connection when inspecting the shared Postgres instance.
        server_settings={"application_name": "nfpd-mod-bot"},
    )


async def _prepare_schema(pool: asyncpg.Pool) -> None:
    """Create missing tables, indexes and columns. Never drops or rewrites data."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Serialises schema setup across containers starting simultaneously.
            # Released automatically when the transaction ends.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _SCHEMA_LOCK_KEY)
            for statement in SCHEMA_STATEMENTS:
                await conn.execute(statement)
            for statement in MIGRATION_STATEMENTS:
                await conn.execute(statement)


async def connect_database() -> None:
    """Open the pool and ensure the schema exists, waiting for Postgres if needed.

    Retries with exponential backoff. With the default DB_CONNECT_MAX_ATTEMPTS of 0
    it waits indefinitely, which is deliberate: a bot that exits because Postgres is
    thirty seconds behind it in the boot order just crash-loops, whereas one that
    waits comes up on its own the moment the database is ready.
    """
    global _pool, _closing

    if _pool is not None:
        return

    _closing = False
    delay = config.DB_CONNECT_BACKOFF_START
    attempt = 0
    started = _time.monotonic()

    while True:
        attempt += 1
        pool = None
        try:
            pool = await _new_pool()
            await _prepare_schema(pool)
        except _FATAL_CONFIG_ERRORS as error:
            await _discard(pool)
            logger.critical(
                "Database rejected the connection: %s. This is a configuration problem "
                "and will not resolve by retrying - check the credentials and database "
                "name in DATABASE_URL (%s).",
                error, config.redacted_database_url(),
            )
            raise
        except Exception as error:
            await _discard(pool)
            _record_failure(error)

            if config.DB_CONNECT_MAX_ATTEMPTS and attempt >= config.DB_CONNECT_MAX_ATTEMPTS:
                logger.critical(
                    "Could not reach the database at %s after %d attempt(s): %s",
                    config.redacted_database_url(), attempt, error,
                )
                raise

            # Jitter (±25%) stops the bot and any sibling service from retrying in lockstep.
            base = min(delay, config.DB_CONNECT_BACKOFF_MAX)
            wait = base * (0.75 + random.random() * 0.5)
            logger.warning(
                "Database not reachable at %s (attempt %d, retrying in %.1fs): %s",
                config.redacted_database_url(), attempt, wait, error,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, config.DB_CONNECT_BACKOFF_MAX)
            continue

        _pool = pool
        _record_success()
        logger.info(
            "Database ready at %s (pool %d-%d, %d attempt(s), %.1fs)",
            config.redacted_database_url(),
            config.DB_POOL_MIN_SIZE, config.DB_POOL_MAX_SIZE,
            attempt, _time.monotonic() - started,
        )
        return


async def _discard(pool: asyncpg.Pool | None) -> None:
    """Drop a pool that failed during setup, so the retry doesn't leak connections."""
    if pool is None:
        return
    try:
        await asyncio.wait_for(pool.close(), timeout=5.0)
    except Exception:
        # Cleanup must never mask the connection error that brought us here.
        pool.terminate()


async def close_database() -> None:
    """Close the pool, giving in-flight queries a bounded moment to finish.

    Bounded because Docker SIGKILLs the container when the stop grace period runs
    out; a pool.close() blocked on a stuck query would otherwise consume all of it
    and the process would be killed mid-cleanup anyway.
    """
    global _pool, _closing

    pool, _pool = _pool, None
    if pool is None:
        return

    _closing = True
    try:
        await asyncio.wait_for(pool.close(), timeout=config.DB_CLOSE_TIMEOUT)
        logger.info("Database pool closed cleanly")
    except asyncio.TimeoutError:
        pool.terminate()
        logger.warning(
            "Database pool did not close within %.0fs - connections terminated",
            config.DB_CLOSE_TIMEOUT,
        )
    except Exception as error:
        pool.terminate()
        logger.warning("Error while closing the database pool, terminated instead: %s", error)


def is_connected() -> bool:
    return _pool is not None and not _pool.is_closing()


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise DatabaseUnavailable(
            "Database is not connected. connect_database() must run during startup."
        )
    return _pool


def _record_success() -> None:
    global _last_success_at, _last_failure
    _last_success_at = _time.time()
    _last_failure = None


def _record_failure(error: BaseException) -> None:
    global _last_failure
    _last_failure = type(error).__name__


def pool_stats() -> dict:
    """Snapshot of pool state for the health endpoint and /debug."""
    stats = {
        "connected": is_connected(),
        "last_success_at": _last_success_at,
        "last_failure": _last_failure,
    }
    if _pool is not None:
        stats.update(
            size=_pool.get_size(),
            idle=_pool.get_idle_size(),
            min_size=_pool.get_min_size(),
            max_size=_pool.get_max_size(),
        )
    return stats


# --- Query execution ----------------------------------------------------------

async def _run(operation, *, idempotent: bool):
    """Acquire a connection and run `operation`, retrying transient failures.

    `idempotent` says whether re-running the statement is safe. A connection that
    drops mid-query is ambiguous: PostgreSQL may or may not have committed the
    statement before the client lost contact. Non-idempotent writes (an INSERT that
    allocates a case number, a DELETE ... RETURNING that consumes saved state) are
    therefore only retried when the failure happened while acquiring the connection —
    that is, before any statement could have run. If the failure is ambiguous (after
    acquire, during execution), the operation is NOT retried and DatabaseUnavailable
    is raised so the caller can surface a safe error to the user.

    Callers that are naturally safe to repeat — UPSERTs, DELETEs by primary key,
    idempotent SELECTs — should pass idempotent=True so they benefit from retry.
    """
    attempt = 0
    while True:
        if _closing:
            raise DatabaseUnavailable("Database pool is shutting down")

        pool = _get_pool()
        started_executing = False
        try:
            async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
                started_executing = True
                result = await operation(conn)
            _record_success()
            return result
        except _RETRYABLE_ERRORS as error:
            _record_failure(error)
            retryable = idempotent or not started_executing
            if not retryable or attempt >= config.DB_QUERY_MAX_RETRIES or _closing:
                logger.warning(
                    "Database query failed (attempt %d, retryable=%s): %s",
                    attempt + 1, retryable, error,
                )
                raise DatabaseUnavailable(str(error)) from error

            attempt += 1
            wait = min(0.5 * (2 ** (attempt - 1)), 5.0)
            logger.warning(
                "Database query failed (attempt %d/%d, retrying in %.1fs): %s",
                attempt, config.DB_QUERY_MAX_RETRIES + 1, wait, error,
            )
            await asyncio.sleep(wait)
        except asyncpg.PostgresError as error:
            # A real SQL error (bad query, constraint violation). Not retryable, and
            # not a connectivity problem, so it must not mark the database unhealthy.
            _record_success()
            logger.exception("Database rejected a statement: %s", error)
            raise


async def _fetch_all(query: str, *args) -> list[asyncpg.Record]:
    return await _run(lambda conn: conn.fetch(query, *args), idempotent=True)


async def _fetch_one(query: str, *args) -> asyncpg.Record | None:
    return await _run(lambda conn: conn.fetchrow(query, *args), idempotent=True)


async def _fetch_val(query: str, *args):
    return await _run(lambda conn: conn.fetchval(query, *args), idempotent=True)


async def _execute(query: str, *args, idempotent: bool = False) -> int:
    """Execute a statement and return the number of affected rows."""
    status = await _run(lambda conn: conn.execute(query, *args), idempotent=idempotent)
    # asyncpg returns a status string like "UPDATE 3" or "DELETE 1"
    try:
        return int(status.split()[-1])
    except (AttributeError, IndexError, ValueError):
        return 0


# --- Cases -------------------------------------------------------------------

async def add_case(guild_id: int, user_id: int, moderator_id: int, action_type: str, reason: str) -> int:
    # Not idempotent: a retry would allocate a second case number for one action.
    return await _run(
        lambda conn: conn.fetchval(
            """
            INSERT INTO cases (guild_id, user_id, moderator_id, action_type, reason, created_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            guild_id, user_id, moderator_id, action_type, reason,
            datetime.now(timezone.utc).isoformat(),
        ),
        idempotent=False,
    )


async def get_cases_for_user(guild_id: int, user_id: int) -> list[asyncpg.Record]:
    return await _fetch_all(
        """
        SELECT id, moderator_id, action_type, reason, created_at
        FROM cases
        WHERE guild_id = $1 AND user_id = $2
        ORDER BY id DESC
        """,
        guild_id, user_id,
    )


async def get_all_cases(guild_id: int) -> list[asyncpg.Record]:
    return await _fetch_all(
        """
        SELECT id, user_id, moderator_id, action_type, reason, created_at
        FROM cases
        WHERE guild_id = $1
        ORDER BY id ASC
        """,
        guild_id,
    )


async def get_case_by_id(guild_id: int, case_id: int) -> asyncpg.Record | None:
    return await _fetch_one(
        "SELECT * FROM cases WHERE guild_id = $1 AND id = $2",
        guild_id, case_id,
    )


async def update_case_reason(guild_id: int, case_id: int, new_reason: str) -> bool:
    changed = await _execute(
        "UPDATE cases SET reason = $1 WHERE guild_id = $2 AND id = $3",
        new_reason, guild_id, case_id,
        idempotent=True,
    )
    return changed > 0


async def delete_case(guild_id: int, case_id: int) -> bool:
    changed = await _execute(
        "DELETE FROM cases WHERE guild_id = $1 AND id = $2",
        guild_id, case_id,
        idempotent=True,
    )
    return changed > 0


async def get_warn_count(guild_id: int, user_id: int) -> int:
    val = await _fetch_val(
        "SELECT COUNT(*) FROM cases WHERE guild_id = $1 AND user_id = $2 AND action_type = 'warn'",
        guild_id, user_id,
    )
    return val or 0


async def get_action_counts(guild_id: int) -> list[asyncpg.Record]:
    return await _fetch_all(
        """
        SELECT action_type, COUNT(*) AS total
        FROM cases
        WHERE guild_id = $1
        GROUP BY action_type
        ORDER BY total DESC
        """,
        guild_id,
    )


async def get_top_moderators(guild_id: int, limit: int = 5) -> list[asyncpg.Record]:
    return await _fetch_all(
        """
        SELECT moderator_id, COUNT(*) AS total
        FROM cases
        WHERE guild_id = $1
        GROUP BY moderator_id
        ORDER BY total DESC
        LIMIT $2
        """,
        guild_id, limit,
    )


async def get_most_warned_users(guild_id: int, limit: int = 5) -> list[asyncpg.Record]:
    return await _fetch_all(
        """
        SELECT user_id, COUNT(*) AS total
        FROM cases
        WHERE guild_id = $1 AND action_type = 'warn'
        GROUP BY user_id
        ORDER BY total DESC
        LIMIT $2
        """,
        guild_id, limit,
    )


# --- Guild settings -----------------------------------------------------------

DEFAULT_SETTINGS: dict = {
    "log_channel_id": None,
    "server_log_channel_id": None,
    "lockdown_role_id": None,
    "raid_min_account_age_hours": None,
    "warn_mute_threshold": None,
    "warn_mute_minutes": None,
    "warn_kick_threshold": None,
    "warn_ban_threshold": None,
}

# Columns _upsert_settings is allowed to write. The column name is interpolated into
# the SQL (it cannot be a bind parameter), so it is checked against this set rather
# than trusted from the caller.
_SETTINGS_COLUMNS = frozenset(DEFAULT_SETTINGS)


async def get_guild_settings(guild_id: int) -> dict:
    row = await _fetch_one("SELECT * FROM guild_settings WHERE guild_id = $1", guild_id)
    if row is None:
        return {"guild_id": guild_id, **DEFAULT_SETTINGS}
    return dict(row)


async def _upsert_settings(guild_id: int, column: str, value) -> None:
    if column not in _SETTINGS_COLUMNS:
        raise ValueError(f"Refusing to write unknown settings column {column!r}")
    await _execute(
        f"""
        INSERT INTO guild_settings (guild_id, {column})
        VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET {column} = EXCLUDED.{column}
        """,
        guild_id, value,
        idempotent=True,
    )


async def set_log_channel(guild_id: int, channel_id: int) -> None:
    await _upsert_settings(guild_id, "log_channel_id", channel_id)


async def set_server_log_channel(guild_id: int, channel_id: int | None) -> None:
    await _upsert_settings(guild_id, "server_log_channel_id", channel_id)


async def set_lockdown_role(guild_id: int, role_id: int | None) -> None:
    await _upsert_settings(guild_id, "lockdown_role_id", role_id)


async def set_raid_protection(guild_id: int, min_account_age_hours: int | None) -> None:
    await _upsert_settings(guild_id, "raid_min_account_age_hours", min_account_age_hours)


async def set_warn_thresholds(
    guild_id: int,
    mute_threshold: int | None,
    mute_minutes: int | None,
    kick_threshold: int | None,
    ban_threshold: int | None,
) -> None:
    await _execute(
        """
        INSERT INTO guild_settings
            (guild_id, warn_mute_threshold, warn_mute_minutes, warn_kick_threshold, warn_ban_threshold)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (guild_id) DO UPDATE SET
            warn_mute_threshold = EXCLUDED.warn_mute_threshold,
            warn_mute_minutes   = EXCLUDED.warn_mute_minutes,
            warn_kick_threshold = EXCLUDED.warn_kick_threshold,
            warn_ban_threshold  = EXCLUDED.warn_ban_threshold
        """,
        guild_id, mute_threshold, mute_minutes, kick_threshold, ban_threshold,
        idempotent=True,
    )


# --- Lockdown roles ----------------------------------------------------------

async def get_lockdown_role_ids(guild_id: int) -> list[int]:
    rows = await _fetch_all(
        "SELECT role_id FROM lockdown_roles WHERE guild_id = $1 ORDER BY role_id",
        guild_id,
    )
    return [row["role_id"] for row in rows]


async def add_lockdown_role(guild_id: int, role_id: int) -> None:
    await _execute(
        "INSERT INTO lockdown_roles (guild_id, role_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        guild_id, role_id,
        idempotent=True,
    )


async def remove_lockdown_role(guild_id: int, role_id: int) -> bool:
    changed = await _execute(
        "DELETE FROM lockdown_roles WHERE guild_id = $1 AND role_id = $2",
        guild_id, role_id,
        idempotent=True,
    )
    return changed > 0


async def clear_lockdown_roles(guild_id: int) -> None:
    await _execute("DELETE FROM lockdown_roles WHERE guild_id = $1", guild_id, idempotent=True)


# --- Temporary bans ----------------------------------------------------------

async def add_temp_ban(guild_id: int, user_id: int, unban_at: datetime) -> None:
    await _execute(
        """
        INSERT INTO temp_bans (guild_id, user_id, unban_at)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, user_id) DO UPDATE SET unban_at = EXCLUDED.unban_at
        """,
        guild_id, user_id, unban_at.isoformat(),
        idempotent=True,
    )


async def remove_temp_ban(guild_id: int, user_id: int) -> None:
    await _execute(
        "DELETE FROM temp_bans WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
        idempotent=True,
    )


async def get_expired_temp_bans(now: datetime) -> list[asyncpg.Record]:
    return await _fetch_all(
        "SELECT guild_id, user_id FROM temp_bans WHERE unban_at <= $1",
        now.isoformat(),
    )


# --- Channel lock state -------------------------------------------------------

async def save_channel_lock(guild_id: int, channel_id: int, role_states: dict[int, str]) -> None:
    await _execute(
        """
        INSERT INTO channel_locks (guild_id, channel_id, previous_state)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, channel_id) DO UPDATE SET previous_state = EXCLUDED.previous_state
        """,
        guild_id, channel_id, json.dumps({str(k): v for k, v in role_states.items()}),
        idempotent=True,
    )


async def pop_channel_lock(guild_id: int, channel_id: int) -> dict[int, str] | None:
    """Return {role_id: tristate_string} and delete the row, or None if none recorded."""
    # Not idempotent: a retry after a successful delete would report "no saved state"
    # and the channel's original permissions would be lost.
    row = await _run(
        lambda conn: conn.fetchrow(
            "DELETE FROM channel_locks WHERE guild_id = $1 AND channel_id = $2 RETURNING previous_state",
            guild_id, channel_id,
        ),
        idempotent=False,
    )
    if row is None:
        return None
    try:
        return {int(k): v for k, v in json.loads(row["previous_state"]).items()}
    except (json.JSONDecodeError, ValueError):
        return None


# --- Health checks -----------------------------------------------------------

async def check_connection() -> tuple[bool, str]:
    """Round-trip a trivial query. Returns (ok, latency or error text).

    Error text is the exception class name only — the full message may contain
    connection details that should not reach an HTTP endpoint or a Discord embed.
    """
    if not is_connected():
        return False, _last_failure or "pool is not open"
    try:
        started = _time.perf_counter()
        await _fetch_val("SELECT 1")
        elapsed_ms = (_time.perf_counter() - started) * 1000
        return True, f"{elapsed_ms:.1f}ms"
    except Exception as error:
        return False, f"{type(error).__name__}: query failed"


async def get_total_case_count() -> int:
    return (await _fetch_val("SELECT COUNT(*) FROM cases")) or 0


async def get_active_temp_ban_count() -> int:
    return (await _fetch_val("SELECT COUNT(*) FROM temp_bans")) or 0
