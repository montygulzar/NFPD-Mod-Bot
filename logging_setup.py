"""Logging configuration for container deployment.

Everything goes to stdout, unbuffered, because that is what `docker logs` and any
log shipper on the monitoring network read. Nothing is written to disk: log
rotation is the Docker daemon's job (see the logging options in the compose file),
and a container writing its own log files just fills the host volume silently.
"""
import json
import logging
import sys
import time

import config
import diagnostics

# Libraries that are useful at INFO but overwhelming at DEBUG. They are held at INFO
# unless LOG_LIBRARY_DEBUG is set, so LOG_LEVEL=debug stays readable for our own code.
_NOISY_LOGGERS = (
    "discord.gateway",
    "discord.http",
    "discord.state",
    "discord.client",
    "websockets",
    "asyncio",
    "aiohttp.access",
    "asyncpg",
)

# Attributes present on every LogRecord. Anything else was attached by the caller
# via `extra=` and is worth emitting as structured context.
_STANDARD_RECORD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName"}


class _TextFormatter(logging.Formatter):
    """Human-readable single-line output, for reading in `docker logs`."""

    default_msec_format = "%s.%03d"
    converter = time.gmtime  # UTC everywhere; the host's local timezone is irrelevant.

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)sZ %(levelname)-8s %(name)-28s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        context = _extra_fields(record)
        if context:
            line += "  " + " ".join(f"{key}={value!r}" for key, value in sorted(context.items()))
        return line


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, for ingestion by a log aggregator."""

    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        payload.update(_extra_fields(record))
        # default=str so an unexpected object in `extra=` degrades instead of
        # raising inside the logging call and losing the record entirely.
        return json.dumps(payload, default=str)


def _extra_fields(record: logging.LogRecord) -> dict:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_")
    }


def configure_logging() -> None:
    """Install the root handler. Safe to call more than once."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if config.LOG_FORMAT == "json" else _TextFormatter())
    root.addHandler(handler)
    root.setLevel(config.LOG_LEVEL)

    if not config.LOG_LIBRARY_DEBUG:
        floor = max(logging.getLevelName(config.LOG_LEVEL), logging.INFO)
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(floor)

    # Route warnings.warn() through logging so deprecations show up in the same stream.
    logging.captureWarnings(True)

    # The in-memory ring buffer that /debug reports as "recent warnings/errors".
    # It has to be attached after the handler reset above, or it would be removed
    # again along with the default handlers.
    diagnostics.attach_recent_log_handler()
