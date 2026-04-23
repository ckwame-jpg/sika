# SIKA Laptop Server

This is the zero-cloud setup: your laptop runs both the FastAPI API and the Next.js web app. Render and Vercel are not required while this is running.

## What Keeps Learning Running

The scheduler lives in the API process. For predictions, settlement, shadow capture, and readiness stats to keep moving, the API must stay running with:

```bash
SCHEDULER_ENABLED=true
```

If the laptop sleeps, loses internet, or the API stops, SIKA stops making timestamped predictions. It can settle predictions it already captured after it starts again, but it cannot honestly learn from predictions it never made.

## One-Time Setup

```bash
cd /path/to/sika           # your clone of this repo
python3.12 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
npm install
cp apps/api/.env.example apps/api/.env
npm run server:db:up
```

If `npm` is not found in a non-interactive shell on this laptop, use Homebrew's path explicitly:

```bash
/opt/homebrew/bin/npm install
```

Edit `apps/api/.env` for Kalshi credentials if you need authenticated demo actions:

```bash
KALSHI_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=$HOME/.config/kalshi/kalshi-demo.key
```

The laptop-server default database is local Postgres 18 from `docker-compose.yml`, matching Render production:

```bash
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/kalshi_sports_copilot
```

SQLite is still fine for isolated smoke/dev runs, but it is not the server-mode default. The laptop server starts the `postgres` Docker Compose service before it starts the API.

## Run Manually

```bash
sika
```

Open:

```text
http://127.0.0.1:3000
```

Useful commands:

```bash
sika status
sika health
sika refresh
sika logs
sika stop
```

The API runs at `http://127.0.0.1:8000`, and the web app proxies `/api/*` to it.

The lower-level `npm run server:*` scripts still exist for diagnostics and automation.

## Install The Easy Command And Apps

Install the `sika` command and Desktop apps:

```bash
npm run server:install-command
```

This creates:

- `~/.local/bin/sika`
- `~/Desktop/sika.app`
- `~/Desktop/sika stop.app`
- `~/Desktop/sika storage.app`

From Finder, double-click:

- `sika` — starts Postgres/API/web, opens Trade and Runs, then streams logs.
- `sika stop` — stops the local API/web supervisor and unloads the login auto-start agent for this user session if it is running.
- `sika storage` — shows disk usage and cleanup commands.

macOS may ask for permission the first time because these are local app launchers.

The equivalent terminal command is:

```bash
sika
```

## Start Automatically When You Log In

Install a macOS LaunchAgent:

```bash
npm run server:install-login
```

Remove it:

```bash
npm run server:uninstall-login
```

This starts the laptop server whenever your user logs in. It does not defeat macOS sleep. For continuous learning, keep the laptop awake:

```bash
caffeinate -dimsu
```

Or use macOS battery/power settings to prevent sleep while plugged in.

Running `sika stop` unloads the login auto-start agent for the current user session so the server stays stopped. To start the login agent again immediately, rerun `npm run server:install-login`. To remove login auto-start entirely, run `npm run server:uninstall-login`.

If the login auto-start agent is installed, the plain `sika` command starts through that agent so API/web stay managed after the launching Terminal exits.

## Preserve Existing Production Learning

If you start with an empty local Postgres database, the app will work, but prior Render prediction history and shadow-learning records will not be present.

To continue from production history, migrate the production database first. The important tables include predictions, parlay predictions, shadow inference tables, runs, refresh jobs, markets, snapshots, and model readiness/runtime tables. Without that migration, the local model starts a new local learning ledger.

```bash
export RENDER_DATABASE_URL='postgres://...'
npm run server:migrate-render-db
```

The migration command:

- starts local Postgres
- stops the laptop server before restore
- writes a custom-format Docker Postgres 18 `pg_dump` under `.local-server/backups/`
- drops and recreates the local target database
- restores into the empty local database with Docker Postgres 18 `pg_restore --no-owner --no-acl`
- runs the API startup schema patches
- restarts the laptop server

If production ML env values are set in the shell, the script also writes them into `apps/api/.env`:

```bash
export ML_SERVING_MODE=shadow
export ML_MANIFEST_PATH=/absolute/path/to/manifest.json
export ML_FAMILY_MODES_JSON='{"NBA":"shadow","MLB":"shadow"}'
```

## Operational Notes

- Keep the laptop plugged in and online during game windows.
- Do not run another API on port `8000` or another web app on port `3000`.
- Logs are written under `.local-server/logs/`.
- Check storage with `sika storage`.
- Run safe cleanup with `sika cleanup --yes`.
- Run aggressive cleanup with `sika cleanup --yes --aggressive` only when you are comfortable rebuilding the local web app and pruning disposable Docker build/image cache.
- Local Postgres is the durable laptop-server database. Use SQLite only for disposable smoke/dev runs.
- A current-slate refresh now tries targeted Kalshi event discovery before broad market sampling: it matches today’s NBA/MLB ESPN events to Kalshi event tickers, hydrates open markets for those event tickers, maps them, and then scores.
- If today’s games exist but no current open markets reach scoring, Trade should be `degraded` with diagnostics instead of `fresh` and empty.

## Storage Policy

The local Postgres Docker volume is the source of truth for laptop-server history. Do not prune Docker volumes unless you intentionally want to destroy the local database.

The safe cleanup command only removes disposable artifacts:

- zero-byte failed Render dumps
- older Render dumps while keeping the newest two
- oversized local logs
- generated screenshots/test output
- TypeScript build-info cache

Aggressive cleanup additionally removes disposable Docker build/image cache and, if the web server is stopped, the local Next.js build. It never removes Docker volumes.

The laptop server warns when free space is under `25 GiB` and prints a critical warning under `10 GiB`.

An external drive can help later for Render dumps and backup archives. Do not move the live Postgres Docker volume there unless the drive is a fast SSD, always plugged in, and intentionally configured for that role.
