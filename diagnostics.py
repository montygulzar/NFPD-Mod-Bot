import logging
from collections import Counter, deque
from datetime import datetime, timezone

import config

START_TIME = datetime.now(timezone.utc)


def uptime_seconds() -> float:
    return (datetime.now(timezone.utc) - START_TIME).total_seconds()


# --- Recent log capture -------------------------------------------------------
# A ring buffer of recent WARNING+ records from anywhere in the process, so a
# debug command can show "what went wrong recently" without needing log file access.

class _RecentLogHandler(logging.Handler):
    def __init__(self, capacity: int = 50):
        super().__init__(level=logging.WARNING)
        self.records: deque[str] = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:
            pass  # A broken handler must never be what crashes the process.


_recent_log_handler: _RecentLogHandler | None = None


def attach_recent_log_handler() -> None:
    """Install the ring-buffer handler on the root logger. Call once at startup."""
    global _recent_log_handler
    if _recent_log_handler is not None:
        return  # Already attached; avoid double-logging on a reconnect or re-import.
    _recent_log_handler = _RecentLogHandler()
    logging.getLogger().addHandler(_recent_log_handler)


def get_recent_logs(limit: int = 10) -> list[str]:
    if _recent_log_handler is None:
        return []
    return list(_recent_log_handler.records)[-limit:]


# --- Command usage tracking ---------------------------------------------------

_invocations: Counter[str] = Counter()
_errors: Counter[str] = Counter()


def record_invocation(command_name: str) -> None:
    _invocations[command_name] += 1


def record_error(command_name: str) -> None:
    _errors[command_name] += 1


def get_command_stats(top: int = 10) -> tuple[list[tuple[str, int]], int, int]:
    """Returns (top N commands by use, total invocations, total errors)."""
    return _invocations.most_common(top), sum(_invocations.values()), sum(_errors.values())


# --- Configuration validation --------------------------------------------------

def validate_config() -> list[str]:
    """Human-readable warnings about the current configuration. An empty list means all clear."""
    warnings: list[str] = []

    if not config.OWNER_IDS:
        warnings.append("OWNER_IDS is empty - owner-only commands are unreachable by anyone.")

    if not config.APPROVED_GUILD_IDS:
        warnings.append("APPROVED_GUILD_IDS is empty - global actions apply to every server this bot is in.")

    if config.LEAVE_UNAPPROVED_GUILDS and not config.APPROVED_GUILD_IDS:
        warnings.append("LEAVE_UNAPPROVED_GUILDS is on with no APPROVED_GUILD_IDS - the bot will leave everywhere.")

    if not config.COMMAND_PREFIX:
        warnings.append("COMMAND_PREFIX is empty - prefix commands will not work.")

    overlap = config.PROTECTED_USER_IDS & config.BLOCKED_USER_IDS
    if overlap:
        warnings.append(
            f"{len(overlap)} user(s) are on both PROTECTED_USER_IDS and BLOCKED_USER_IDS - "
            "they cannot use the bot but also cannot be moderated."
        )

    if not config.CR_ROLE_IDS and not config.OWNER_IDS:
        warnings.append("No CR role or owner is configured - global and case commands are unreachable.")

    if not config.MOD_ROLE_IDS and not config.CR_ROLE_IDS and not config.OWNERSHIP_ROLE_IDS and not config.DEVELOPMENT_ROLE_IDS and not config.OWNER_IDS:
        warnings.append("No tier roles or owners are configured - all commands are unreachable.")

    non_approved_exempt = config.GLOBAL_ACTION_EXEMPT_GUILD_IDS - config.APPROVED_GUILD_IDS
    if config.APPROVED_GUILD_IDS and non_approved_exempt:
        warnings.append(
            f"{len(non_approved_exempt)} guild(s) in GLOBAL_ACTION_EXEMPT_GUILD_IDS are not in "
            "APPROVED_GUILD_IDS - they would never receive global actions anyway."
        )

    return warnings
