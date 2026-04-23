#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/.." && pwd -P)"
api_root="$repo_root/apps/api"
state_dir="${SIKA_LOCAL_STATE_DIR:-$repo_root/.local-server}"
backup_dir="$state_dir/backups"
local_pg_url="${LOCAL_POSTGRES_URL:-postgresql://postgres:postgres@localhost:5432/kalshi_sports_copilot}"
local_sqlalchemy_url="${LOCAL_DATABASE_URL:-postgresql+psycopg://postgres:postgres@localhost:5432/kalshi_sports_copilot}"
restart_after_restore=true

usage() {
  cat <<EOF
Usage: RENDER_DATABASE_URL=postgres://... scripts/migrate-render-db-to-local-postgres.sh [--no-restart]

Restores the production Render Postgres database into local Docker Postgres.
The local target database is dropped and recreated before restore.

Environment:
  RENDER_DATABASE_URL       Required source database URL.
  LOCAL_POSTGRES_URL        Target pg_restore URL. Default: $local_pg_url
  LOCAL_DATABASE_URL        Target SQLAlchemy URL for API schema patches.
  ML_SERVING_MODE           Optional; copied into apps/api/.env when set.
  ML_MANIFEST_PATH          Optional; copied into apps/api/.env when set.
  ML_FAMILY_MODES_JSON      Optional; copied into apps/api/.env when set.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --no-restart) restart_after_restore=false ;;
    -h|--help) usage; exit 0 ;;
    *)
      printf 'Unknown argument: %s\n' "$arg" >&2
      usage >&2
      exit 1
      ;;
  esac
done

require_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$name" >&2
    exit 1
  fi
}

require_command docker

if [ -z "${RENDER_DATABASE_URL:-}" ]; then
  printf 'RENDER_DATABASE_URL is required.\n' >&2
  usage >&2
  exit 1
fi

mkdir -p "$backup_dir"
dump_path="$backup_dir/render-$(date -u +%Y%m%d-%H%M%S).dump"

set_env_value() {
  local key="$1"
  local value="$2"
  local env_file="$api_root/.env"
  touch "$env_file"
  if grep -q "^$key=" "$env_file"; then
    sed -i.bak "s|^$key=.*|$key=$value|" "$env_file"
    rm -f "$env_file.bak"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$env_file"
  fi
}

printf 'Starting local Postgres...\n'
"$repo_root/scripts/laptop-server.sh" db:up

printf 'Stopping laptop server before restore...\n'
"$repo_root/scripts/laptop-server.sh" stop || true

printf 'Writing Render dump to %s\n' "$dump_path"
(
  cd "$repo_root"
  docker compose exec -T postgres pg_dump --format=custom --no-owner --no-acl "$RENDER_DATABASE_URL" > "$dump_path"
)

printf 'Recreating local target database...\n'
(
  cd "$repo_root"
  docker compose exec -T postgres dropdb --force -h localhost -U postgres kalshi_sports_copilot 2>/dev/null || true
  docker compose exec -T postgres createdb -h localhost -U postgres kalshi_sports_copilot
)

printf 'Restoring into local Postgres...\n'
(
  cd "$repo_root"
  docker compose exec -T postgres pg_restore --no-owner --no-acl --dbname "$local_pg_url" < "$dump_path"
)

printf 'Writing local database URL into apps/api/.env...\n'
set_env_value "DATABASE_URL" "$local_sqlalchemy_url"

for key in ML_SERVING_MODE ML_MANIFEST_PATH ML_FAMILY_MODES_JSON; do
  value="${!key:-}"
  if [ -n "$value" ]; then
    printf 'Writing %s into apps/api/.env from current environment.\n' "$key"
    set_env_value "$key" "$value"
  fi
done

printf 'Running API startup schema patches against local Postgres...\n'
(
  cd "$api_root"
  DATABASE_URL="$local_sqlalchemy_url" PYTHONPATH="$api_root" "$repo_root/.venv/bin/python" -c "from app.database import init_db; init_db()"
)

if [ "$restart_after_restore" = true ]; then
  printf 'Restarting laptop server...\n'
  "$repo_root/scripts/laptop-server.sh" start
else
  printf 'Restore complete. Laptop server left stopped because --no-restart was passed.\n'
fi

printf 'Restore complete. Dump retained at %s\n' "$dump_path"
