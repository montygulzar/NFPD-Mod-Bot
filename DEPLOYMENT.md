# Deployment

Self-hosted on Ubuntu with Docker. PostgreSQL runs as a separate, pre-existing
container (`nfpd-postgres`); nothing here creates, migrates away from, or deletes
it. `docker-compose.yml` deliberately defines only the bot service, so no compose
command can touch the database container or its volume.

## Architecture

```
docker network: nodey-monitoring
├── nfpd-mod-bot    this repo, built from Dockerfile
└── nfpd-postgres   PostgreSQL 16, managed separately
```

The bot reaches the database at the hostname `nfpd-postgres` — containers on the
same user-defined Docker network resolve each other by container name. Do not use
`localhost` in `DATABASE_URL`: inside the bot container that points at the bot.

## First-time setup

1. **Clone and configure**

   ```bash
   git clone https://github.com/ombdeveloping/NFPD-Mod-Bot.git
   cd NFPD-Mod-Bot
   cp .env.example .env
   nano .env          # set BOT_TOKEN, DATABASE_URL, OWNER_IDS, ...
   chmod 600 .env     # the file holds the bot token and database password
   ```

2. **Confirm the network exists**

   ```bash
   docker network ls | grep nodey-monitoring \
     || docker network create nodey-monitoring
   ```

3. **Confirm the database container is on that network**

   ```bash
   docker network inspect nodey-monitoring --format '{{range .Containers}}{{.Name}} {{end}}'
   # if nfpd-postgres is missing:
   docker network connect nodey-monitoring nfpd-postgres
   ```

4. **Build and start**

   ```bash
   docker compose build --build-arg GIT_COMMIT="$(git rev-parse --short HEAD)"
   docker compose up -d
   docker compose logs -f
   ```

   The schema is created on first start. On an existing database, missing tables,
   indexes and columns are added in place — existing rows and case IDs are never
   modified or reset.

## Routine deployment

```bash
cd ~/NFPD-Mod-Bot
git pull
docker compose build --build-arg GIT_COMMIT="$(git rev-parse --short HEAD)"
docker compose up -d
docker compose logs -f --tail=50
```

`docker compose up -d` recreates the container only when the image or config
changed. The database container is untouched.

To confirm which build is live, run `/debug` in Discord — it reports the version
and commit stamped into the image at build time.

## Verifying a deployment

```bash
docker compose ps                       # State should be "Up ... (healthy)"
curl -s http://localhost:8080/ready     # only from inside the network; see below
docker compose logs --tail=30
```

The health port is not published to the host, so probe it from the network:

```bash
docker run --rm --network nodey-monitoring curlimages/curl -s http://nfpd-mod-bot:8080/ready
```

In Discord: `/health` for a quick summary, `/debug` for the full report.

Expected healthy startup log:

```
Starting NFPD moderation bot (version=1.0.0 commit=abc1234)
Health server listening on 0.0.0.0:8080
Database ready at postgresql://***@nfpd-postgres:5432/nfpd (pool 1-10, 1 attempt(s), 0.1s)
Loaded 13/13 extensions
Synced 37 slash command(s)
Connected as NFPD Moderation (…) across N guild(s)
```

## Endpoints

| Path      | Meaning                                                     |
|-----------|-------------------------------------------------------------|
| `/health` | Liveness. 200 whenever the process is responsive.            |
| `/ready`  | Readiness. 200 only when Discord **and** the database are up; 503 otherwise. |

Docker's `HEALTHCHECK` uses `/ready`, so a container with a failing database shows
as `unhealthy` in `docker ps` without being restarted — restarting would not fix a
dependency outage, and the bot reconnects on its own.

## Global action exemptions

`GLOBAL_ACTION_EXEMPT_GUILD_IDS` accepts a comma-separated list of Discord guild
(server) IDs. Exempt guilds are **skipped** by every global moderation command:
`globalban`, `globalunban`, `globalkick`, `globalmute`, `globalunmute`.

Use this for an **Appeals server**: a globally banned user must still be able to
remain in or join the Appeals server so they can appeal their punishment.

- Normal per-guild moderation commands (`/kick`, `/ban`, `/mute`, etc.) still work
  inside exempt guilds — the exemption applies only to the global/cross-server
  propagation.
- Because exempt guilds never receive a global action, reversing that global action
  (e.g. `globalunban`) does not touch them either, so any unrelated local moderation
  state in the exempt guild is preserved.
- An empty value (or omitting the variable) means no guilds are exempt, preserving
  the existing behaviour.
- Multiple guild IDs are separated by commas: `GLOBAL_ACTION_EXEMPT_GUILD_IDS=123,456`.
- Invalid IDs produce a configuration error at startup.

## Operational behaviour

**Postgres starts after the bot.** The bot retries with exponential backoff
(1s → 30s) and connects as soon as the database accepts connections. It does not
exit, so there is no crash loop and no restart backoff from Docker.

**Postgres restarts while the bot is running.** Individual queries retry
(`DB_QUERY_MAX_RETRIES`, default 2). Writes that cannot be safely repeated are
retried only when the failure happened before the statement could have run, so a
restart never duplicates a case or loses saved channel-lock state.

**Wrong password or missing database.** These fail immediately with a `CRITICAL`
log naming the problem, rather than retrying forever and hiding it.

**Host reboots.** `restart: unless-stopped` starts the container with the Docker
daemon. Ordering against `nfpd-postgres` does not matter, because of the retry
behaviour above.

**`docker stop` / reboot shutdown.** `tini` forwards SIGTERM, which triggers an
orderly shutdown: stop accepting work, close the gateway, drain and close the
connection pool. `stop_grace_period` (30s) exceeds the internal timeouts, so
cleanup completes before Docker escalates to SIGKILL.

## Logs

Docker keeps 5 × 10 MB per container (set in `docker-compose.yml`), so logs cannot
fill the host disk.

```bash
docker compose logs -f                 # follow
docker compose logs --since 1h         # recent
```

Set `LOG_LEVEL=debug` in `.env` for more detail, and `LOG_FORMAT=json` to emit one
JSON object per line for a log aggregator. discord.py/asyncpg internals stay at
INFO unless `LOG_LIBRARY_DEBUG=true`, so `debug` remains readable.

## Backups

The bot never deletes the database, but nothing here backs it up either. Dump the
volume on a schedule:

```bash
docker exec nfpd-postgres pg_dump -U nfpd_bot -d nfpd \
  | gzip > "nfpd-$(date +%F).sql.gz"
```

Restore into a **new** database first and verify before touching the live one.

## Rollback

```bash
git log --oneline -5
git checkout <previous-commit>
docker compose build && docker compose up -d
```

Safe as far as the schema is concerned: changes are additive only, so an older
build runs against a newer database. It simply ignores columns it does not know.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Configuration error - the bot cannot start` (exit 2) | Every problem found is listed. Fix `.env`, then `docker compose up -d --force-recreate`. |
| `Database not reachable ... retrying` repeating | Postgres is down, or not on `nodey-monitoring`. Check `docker ps` and `docker network inspect nodey-monitoring`. |
| `password authentication failed` | Wrong credentials in `DATABASE_URL`. Note that a password with `@ / : #` must be percent-encoded — or use the `POSTGRES_*` variables, which encode it for you. |
| `database "nfpd" does not exist` | Create it: `docker exec -it nfpd-postgres createdb -U nfpd_bot nfpd`. |
| `Discord rejected BOT_TOKEN` | Regenerate the token in the Discord developer portal and update `.env`. |
| `Privileged intents are not enabled` | Enable Server Members and Message Content in the developer portal. |
| Slash commands missing | Sync is rate-limited; it retries on the next restart. Prefix commands keep working. |
| Container shows `unhealthy` | `/ready` is returning 503. Check `docker compose logs` — usually the database. |
