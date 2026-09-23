"""Environment configuration.

Everything is read from the process environment. In production Docker injects it
from the .env file next to docker-compose.yml; for local runs python-dotenv loads
the same file. Nothing here is tied to a particular host or hosting provider - the
only values with no sensible default are the Discord token and the database
connection details.

Validation collects *every* problem and reports them together, so a misconfigured
container tells you all of what is wrong in one startup instead of one item per
restart cycle.
"""
import os
import sys
from urllib.parse import quote, urlsplit

from dotenv import load_dotenv

load_dotenv()

# Problems found while reading the environment. Reported together at import time.
_errors: list[str] = []


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _parse_id_list(*variable_names: str) -> set[int]:
    """Read a comma-separated list of Discord IDs from the first variable that has a value."""
    for name in variable_names:
        raw_value = _env(name)
        if not raw_value:
            continue
        try:
            return {int(piece.strip()) for piece in raw_value.split(",") if piece.strip()}
        except ValueError:
            _errors.append(f"{name} must be a comma-separated list of numeric Discord IDs, got: {raw_value!r}")
            return set()
    return set()


def _parse_bool(name: str, default: bool = False) -> bool:
    raw_value = _env(name).lower()
    if not raw_value:
        return default
    if raw_value in {"1", "true", "yes", "y", "on"}:
        return True
    if raw_value in {"0", "false", "no", "n", "off"}:
        return False
    _errors.append(f"{name} must be a boolean (true/false), got: {raw_value!r}")
    return default


def _parse_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw_value = _env(name)
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        _errors.append(f"{name} must be a whole number, got: {raw_value!r}")
        return default
    if minimum is not None and value < minimum:
        _errors.append(f"{name} must be at least {minimum}, got {value}")
        return default
    if maximum is not None and value > maximum:
        _errors.append(f"{name} must be at most {maximum}, got {value}")
        return default
    return value


def _parse_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw_value = _env(name)
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        _errors.append(f"{name} must be a number, got: {raw_value!r}")
        return default
    if value < minimum:
        _errors.append(f"{name} must be at least {minimum}, got {value}")
        return default
    return value


def _parse_choice(name: str, default: str, allowed: set[str]) -> str:
    raw_value = _env(name).lower()
    if not raw_value:
        return default
    if raw_value not in allowed:
        _errors.append(f"{name} must be one of {sorted(allowed)}, got: {raw_value!r}")
        return default
    return raw_value


# --- Discord ------------------------------------------------------------------

BOT_TOKEN = _env("BOT_TOKEN")
if not BOT_TOKEN:
    _errors.append("BOT_TOKEN is required. Set it in your .env file.")

COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")
if not COMMAND_PREFIX:
    _errors.append("COMMAND_PREFIX must not be empty - prefix commands would be unusable.")

BRAND_NAME = os.environ.get("BRAND_NAME", "Moderation")

OWNER_IDS = _parse_id_list("OWNER_IDS")

# --- Tiered role system -------------------------------------------------------
# Six tiers, each inheriting every tier below it:
#   DEVELOPMENT > OWNERSHIP > MANAGEMENT > CR > BAN_PERM > MOD
#
# A user with a role in a higher tier can use every command available to lower
# tiers. OWNER_IDS (user IDs, not role IDs) bypass the tier system entirely.
MOD_ROLE_IDS = _parse_id_list("MOD_ROLE_IDS")
BAN_PERM_ROLE_IDS = _parse_id_list("BAN_PERM_ROLE_IDS")
CR_ROLE_IDS = _parse_id_list("CR_ROLE_IDS", "GLOBAL_ACTION_ROLE_IDS", "GLOBAL_ACTION_ROLE_ID")
MANAGEMENT_ROLE_IDS = _parse_id_list("MANAGEMENT_ROLE_IDS")
OWNERSHIP_ROLE_IDS = _parse_id_list("OWNERSHIP_ROLE_IDS")
DEVELOPMENT_ROLE_IDS = _parse_id_list("DEVELOPMENT_ROLE_IDS")

# Backward-compat alias so existing code referencing GLOBAL_ACTION_ROLE_IDS
# (e.g. diagnostics) keeps working without changes.
GLOBAL_ACTION_ROLE_IDS = CR_ROLE_IDS

# Servers that global actions may be run from and applied to. Leaving this empty means
# global actions reach EVERY server the bot is in, including ones added without your knowledge.
APPROVED_GUILD_IDS = _parse_id_list("APPROVED_GUILD_IDS")

# When true, the bot immediately leaves any server not in APPROVED_GUILD_IDS.
LEAVE_UNAPPROVED_GUILDS = _parse_bool("LEAVE_UNAPPROVED_GUILDS")

# Guilds that are exempt from GLOBAL moderation actions (globalban, globalunban,
# globalkick, globalmute, globalunmute). Intended for an Appeals server: a globally
# banned user must still be able to remain in or join it. Normal per-guild moderation
# commands still work inside exempt guilds.
GLOBAL_ACTION_EXEMPT_GUILD_IDS = _parse_id_list("GLOBAL_ACTION_EXEMPT_GUILD_IDS")

# Users who can never be the TARGET of a moderation action. Owners are always protected
# on top of this list, so a compromised moderator account can't remove them.
PROTECTED_USER_IDS = _parse_id_list("PROTECTED_USER_IDS")

# Users who may not USE the bot at all. Every command refuses for them.
BLOCKED_USER_IDS = _parse_id_list("BLOCKED_USER_IDS")


# --- Database -----------------------------------------------------------------

def _database_url() -> str:
    """DATABASE_URL if given, otherwise assembled from discrete POSTGRES_* variables.

    The discrete form exists so a compose file can reuse the same POSTGRES_USER /
    POSTGRES_PASSWORD / POSTGRES_DB values it already passes to the Postgres
    container, instead of repeating the credentials inside a second URL.
    """
    explicit = _env("DATABASE_URL")
    if explicit:
        return explicit

    user = _env("POSTGRES_USER")
    password = _env("POSTGRES_PASSWORD")
    host = _env("POSTGRES_HOST")
    database = _env("POSTGRES_DB")
    port = _env("POSTGRES_PORT", "5432")
    if user and password and host and database:
        # Credentials are percent-encoded: an unescaped '@' or '/' in a password
        # silently produces a URL that points somewhere else entirely.
        return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{database}"
    return ""


DATABASE_URL = _database_url()
if not DATABASE_URL:
    _errors.append(
        "Database connection details are required. Set DATABASE_URL "
        "(postgresql://user:password@host:5432/dbname), or set POSTGRES_USER, "
        "POSTGRES_PASSWORD, POSTGRES_HOST and POSTGRES_DB and let it be assembled."
    )
elif urlsplit(DATABASE_URL).scheme not in {"postgres", "postgresql"}:
    _errors.append(
        f"DATABASE_URL must be a PostgreSQL URL starting with postgresql://, "
        f"got scheme {urlsplit(DATABASE_URL).scheme!r}."
    )

# Pool sizing. The bot is not query-heavy; a small pool is plenty and keeps the
# footprint low on a shared self-hosted Postgres.
DB_POOL_MIN_SIZE = _parse_int("DB_POOL_MIN_SIZE", 1, minimum=0, maximum=100)
DB_POOL_MAX_SIZE = _parse_int("DB_POOL_MAX_SIZE", 10, minimum=1, maximum=100)
if DB_POOL_MAX_SIZE < DB_POOL_MIN_SIZE:
    _errors.append(
        f"DB_POOL_MAX_SIZE ({DB_POOL_MAX_SIZE}) must be >= DB_POOL_MIN_SIZE ({DB_POOL_MIN_SIZE})."
    )

# Caps on how long a single query, and a single wait for a free connection, may take.
# Without these a stalled database wedges the command that touched it forever.
DB_COMMAND_TIMEOUT = _parse_float("DB_COMMAND_TIMEOUT", 30.0, minimum=1.0)
DB_ACQUIRE_TIMEOUT = _parse_float("DB_ACQUIRE_TIMEOUT", 10.0, minimum=1.0)

# Recycle idle connections so a Postgres restart doesn't leave the pool holding
# handles to a server that no longer exists.
DB_MAX_INACTIVE_CONNECTION_LIFETIME = _parse_float("DB_MAX_INACTIVE_CONNECTION_LIFETIME", 300.0, minimum=0.0)

# Startup retry. 0 attempts means "keep trying forever", which is what you want in
# Docker: the bot waits for Postgres to come up instead of exiting and crash-looping.
DB_CONNECT_MAX_ATTEMPTS = _parse_int("DB_CONNECT_MAX_ATTEMPTS", 0, minimum=0)
DB_CONNECT_BACKOFF_START = _parse_float("DB_CONNECT_BACKOFF_START", 1.0, minimum=0.1)
DB_CONNECT_BACKOFF_MAX = _parse_float("DB_CONNECT_BACKOFF_MAX", 30.0, minimum=1.0)

# Retries for individual queries once the bot is running, covering brief blips
# such as Postgres being restarted underneath a live bot.
DB_QUERY_MAX_RETRIES = _parse_int("DB_QUERY_MAX_RETRIES", 2, minimum=0, maximum=10)

# How long pool shutdown may take before connections are dropped outright. Must stay
# below the container's stop grace period or Docker will SIGKILL mid-cleanup.
DB_CLOSE_TIMEOUT = _parse_float("DB_CLOSE_TIMEOUT", 10.0, minimum=1.0)


# --- Logging ------------------------------------------------------------------

LOG_LEVEL = _parse_choice(
    "LOG_LEVEL", "info", {"critical", "error", "warning", "info", "debug"}
).upper()
LOG_FORMAT = _parse_choice("LOG_FORMAT", "text", {"text", "json"})
# discord.py's gateway/http loggers are extremely chatty at DEBUG. Opt in separately
# so LOG_LEVEL=debug on our own code stays readable.
LOG_LIBRARY_DEBUG = _parse_bool("LOG_LIBRARY_DEBUG")


# --- Health endpoint ----------------------------------------------------------

# Small HTTP server for container health checks and external monitoring. It is not
# published to the host by the compose file - only reachable on the Docker network.
HEALTH_SERVER_ENABLED = _parse_bool("HEALTH_SERVER_ENABLED", True)
HEALTH_HOST = _env("HEALTH_HOST", "0.0.0.0")
HEALTH_PORT = _parse_int("HEALTH_PORT", 8080, minimum=1, maximum=65535)


# --- Build metadata -----------------------------------------------------------
# Set as build args in the Dockerfile so a running container can report exactly
# which commit it was built from.

APP_VERSION = _env("APP_VERSION", "dev")
GIT_COMMIT = _env("GIT_COMMIT", "unknown")


def redacted_database_url(url: str | None = None) -> str:
    """The DSN with its credentials stripped, safe to log or show in Discord.

    A Postgres URL embeds credentials as scheme://user:password@host/db, so it must
    never be logged verbatim or truncated to a fixed prefix.

    Passing None means "the configured URL"; an explicit empty string is reported as
    unconfigured rather than silently falling back to it.
    """
    url = DATABASE_URL if url is None else url
    if not url:
        return "not configured"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "set (unparseable)"
    if not parsed.hostname:
        return "set (redacted)"
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path.lstrip("/") or "unknown"
    return f"{parsed.scheme}://***@{parsed.hostname}{port}/{database}"


if _errors:
    # Printed rather than logged: this runs before logging is configured, and these
    # are the messages someone reads in `docker logs` when the container won't start.
    print("Configuration error - the bot cannot start:", file=sys.stderr)
    for problem in _errors:
        print(f"  - {problem}", file=sys.stderr)
    print(
        "\nFix the values above in your .env file, then recreate the container:\n"
        "  docker compose up -d --force-recreate",
        file=sys.stderr,
    )
    raise SystemExit(2)
