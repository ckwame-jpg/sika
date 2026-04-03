#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
api_root="$repo_root/apps/api"
uvicorn_bin="$repo_root/.venv/bin/uvicorn"

if [ ! -x "$uvicorn_bin" ]; then
  printf 'Missing uvicorn at %s. Create the repo virtualenv first.\n' "$uvicorn_bin" >&2
  exit 1
fi

cd "$api_root"
export DATABASE_URL="${DATABASE_URL:-sqlite:///./kalshi_sports_copilot.db}"

exec "$uvicorn_bin" app.main:app --host 127.0.0.1 --port 8000 --reload
