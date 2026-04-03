# SIKA

Backend-first monorepo for a six-sport Kalshi copilot with paper tracking and demo trading support.

## Scope

- Sports: NBA, NFL, MLB, Soccer, Tennis, UFC
- Backend now: FastAPI API, scheduling hooks, Kalshi market ingestion, cross-sport event normalization, watchlist generation, paper positions, demo orders

## Default Data Sources

- `NBA`, `NFL`, `MLB`: ESPN public scoreboard endpoints
- `SOCCER`, `TENNIS`, `UFC`: TheSportsDB free `v1` API using key `123`

This keeps the backend usable without a paid provider. Soccer, Tennis, and UFC are intentionally lighter event-coverage paths in this free/public mode.

## Repo Layout

```text
kalshi-sports-copilot/
├── apps/
│   ├── api/        # FastAPI service and tests
│   └── web/        # Next.js dashboard deployed to Vercel
├── packages/
│   └── contracts/  # OpenAPI-derived TS contracts for future client apps
├── docker-compose.yml
└── package.json
```

## Quick Start

```bash
cd /Users/chris/Workspace/locked-in/github/kalshi-sports-copilot
python3.12 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
npm install
cp apps/api/.env.example apps/api/.env
npm run dev
```

`npm run dev` is the canonical local entrypoint. It validates that ports `8000` and `3000` are either free or already owned by this checkout, waits for the current `/health` payload on the API, and then starts the Next.js app against the matching backend.

For a local smoke run, the API uses the SQLite file at `apps/api/kalshi_sports_copilot.db` by default. `docker compose up -d` is only needed if you specifically want to run the optional local Postgres instance.

## Local Dev Commands

- `npm run dev`: guarded startup for the matching API + web pair
- `npm run api:dev`: standalone FastAPI dev server from `apps/api`
- `npm run web:dev`: standalone Next.js dev server from `apps/web`
- `npm run dev:doctor`: report repo root, port owners, and whether `/health` matches the current schema

## Environment

Copy `apps/api/.env.example` to `apps/api/.env` and set:

- `SPORTS_API_KEY` if you want something other than the free TheSportsDB key `123`
- `KALSHI_KEY_ID`
- `KALSHI_PRIVATE_KEY_PATH`

`DATABASE_URL` now defaults to a local SQLite file for smoke runs:

```bash
DATABASE_URL=sqlite:///./kalshi_sports_copilot.db
```

If you want to use the optional Postgres instance from `docker-compose.yml`, change it to:

```bash
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/kalshi_sports_copilot
```

For the web app, copy `apps/web/.env.example` if you want to override the local proxy target:

```bash
SIKA_API_BASE_URL=http://127.0.0.1:8000
```

## API

- `GET /health`
- `GET /sports`
- `GET /events`
- `GET /watchlist`
- `GET /positions`
- `GET /markets/{ticker}`
- `GET /markets/{ticker}/history`
- `GET /runs`
- `GET /runs/{id}`
- `POST /paper-positions`
- `POST /paper-positions/{id}/exit`
- `POST /demo-orders`
- `POST /demo-orders/{id}/cancel`
- `POST /jobs/refresh`
- `POST /stats/query`
  - `NBA`, `NFL`, `MLB`: supports `last N games`, `this season`, plus optional `home/away` and `vs opponent` filters
  - `SOCCER` beta: supports `last N matches` up to `5`, `this season`, and optional `home/away` or `vs opponent` filters for recent-match queries only
  - `TENNIS` beta: supports `last N matches` and `this season`, plus optional `vs opponent` filters; home/away splits are intentionally unsupported
  - `UFC` beta: supports `last N fights` and `this season`, plus optional `vs opponent` filters; home/away splits are intentionally unsupported
  - Responses include both raw `metrics` maps and ready-to-render `stat_line` strings on the summary and each game log

## Vercel

Deploy only `apps/web` to Vercel in this repo.

- Set the Vercel project root directory to `apps/web`
- Keep `SIKA_API_BASE_URL` set in Vercel for both Preview and Production to the public HTTPS FastAPI base URL
- The committed `apps/web/vercel.json` keeps the install/build commands aligned with the existing Next.js app
- Leave browser requests on `/api/:path*`; `apps/web/next.config.ts` rewrites them to `SIKA_API_BASE_URL`

Do not deploy the current FastAPI service to Vercel in this pass. The backend still depends on persistent scheduler jobs and a persistent database, so it should live on a long-running host instead.

The external API host used by Vercel must:

- serve over HTTPS
- allow the Vercel domain in CORS
- keep the scheduler enabled
- use persistent storage for the database and any local runtime assumptions

Use Vercel preview deployments for branch work. Promote to production only after the external API URL is stable and healthy.

## Troubleshooting

- `npm run dev` says port `8000` is owned by another checkout:
  - run `npm run dev:doctor`
  - stop the reported PID before retrying
- The frontend loads but data panels fail:
  - check `http://127.0.0.1:8000/health`
  - if the payload is missing `refresh_status`, you are talking to a stale backend copy
- The frontend is pointed at the wrong backend:
  - confirm `SIKA_API_BASE_URL`
  - make sure the Vercel env var targets the persistent FastAPI host, not localhost

## Notes

- Demo trading uses Kalshi-authenticated requests and requires manual approval per order.
- Live trading is intentionally out of scope.
- A replacement frontend can be built later against the API and the contracts package.
- In free/public mode, Soccer/Tennis/UFC use shorter history windows to stay under TheSportsDB free-tier rate limits.
- Soccer stats query now uses ESPN's public player overview page for season totals plus the latest available match logs, so responses include a coverage note when logs are capped at five matches.
- Tennis stats query now uses ESPN's public core tennis refs for singles match logs and season totals.
- UFC stats query now uses ESPN's public fighter history page, and treats `this season` as the calendar year.
