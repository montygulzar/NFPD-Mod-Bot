# syntax=docker/dockerfile:1

# --- build stage -------------------------------------------------------------
# Dependencies are built into a virtualenv here so the compiler toolchain never
# reaches the runtime image. asyncpg and psutil ship x86_64 wheels, but keeping
# build-essential available means a version without one still installs.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt


# --- runtime stage -----------------------------------------------------------
FROM python:3.12-slim AS runtime

# Recorded at build time so a running container can report exactly what it is.
ARG APP_VERSION=dev
ARG GIT_COMMIT=unknown

ENV APP_VERSION=${APP_VERSION} \
    GIT_COMMIT=${GIT_COMMIT} \
    # Unbuffered so log lines reach `docker logs` immediately instead of sitting
    # in a pipe buffer until the process exits.
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Surfaces the faulthandler traceback if the interpreter dies hard.
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH"

# tini reaps zombies and forwards SIGTERM to the bot as PID 1, which is what makes
# the graceful-shutdown path in main.py actually run on `docker stop`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Unprivileged, with a fixed uid so any bind-mounted path has predictable ownership.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 8080

# Uses /ready, so the container is reported unhealthy while Postgres or the gateway
# is unreachable. start-period covers the initial wait for the database.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "healthcheck.py"]

# Resolved via PATH rather than an absolute path, so the image does not depend on
# where the distro package happens to install it.
ENTRYPOINT ["tini", "--"]
CMD ["python", "-u", "main.py"]
