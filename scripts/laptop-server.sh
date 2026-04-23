#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/.." && pwd -P)"
api_root="$repo_root/apps/api"
web_root="$repo_root/apps/web"
state_dir="${SIKA_LOCAL_STATE_DIR:-$repo_root/.local-server}"
log_dir="$state_dir/logs"
pid_dir="$state_dir/pids"

api_host="${SIKA_API_HOST:-127.0.0.1}"
api_port="${SIKA_API_PORT:-8000}"
web_host="${SIKA_WEB_HOST:-127.0.0.1}"
web_port="${SIKA_WEB_PORT:-3000}"
api_base_url="${SIKA_API_BASE_URL:-http://$api_host:$api_port}"
web_url="http://$web_host:$web_port"
local_database_url="${DATABASE_URL:-postgresql+psycopg://postgres:postgres@localhost:5432/kalshi_sports_copilot}"
health_timeout_seconds="${SIKA_HEALTH_TIMEOUT_SECONDS:-10}"

api_pid_file="$pid_dir/api.pid"
web_pid_file="$pid_dir/web.pid"
supervisor_pid_file="$pid_dir/supervisor.pid"
api_log="$log_dir/api.log"
web_log="$log_dir/web.log"
supervisor_log="$log_dir/supervisor.log"
launch_agent_label="com.sika.laptop-server"
launch_agent_plist="$HOME/Library/LaunchAgents/$launch_agent_label.plist"
launchd_domain="gui/$(id -u)"

usage() {
  cat <<EOF
Usage: scripts/laptop-server.sh <command>

Commands:
  build       Build the Next.js web app for local server mode.
  db:up       Start the local Postgres service from docker-compose.yml.
  open        Start the server, open the local web app, and print status.
  run         Run API + web in the foreground; intended for launchd/supervision.
  start       Start API + web in the background.
  stop        Stop background laptop-server processes.
  restart     Stop, then start.
  status      Show process and health status.
  health      Print API health and product freshness.
  refresh     Queue a current-slate refresh.
  storage     Show disk, repo, local-server, and Docker storage usage.
  cleanup     Clean disposable local artifacts. Add --yes to make changes.
  logs [name] Tail logs. name: supervisor, api, web, all. Default: all.

Environment overrides:
  SIKA_API_HOST=$api_host
  SIKA_API_PORT=$api_port
  SIKA_WEB_HOST=$web_host
  SIKA_WEB_PORT=$web_port
  SIKA_API_BASE_URL=$api_base_url
  SIKA_HEALTH_TIMEOUT_SECONDS=$health_timeout_seconds
  DATABASE_URL=$local_database_url
  SCHEDULER_ENABLED=true
EOF
}

ensure_dirs() {
  mkdir -p "$log_dir" "$pid_dir"
}

is_running() {
  local pid="${1:-}"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
  local file="$1"
  if [ -f "$file" ]; then
    tr -dc '0-9' < "$file"
  fi
}

pid_file_running() {
  local file="$1"
  local pid
  pid="$(read_pid "$file")"
  is_running "$pid"
}

require_file() {
  local path="$1"
  local message="$2"
  if [ ! -e "$path" ]; then
    printf '%s\n' "$message" >&2
    exit 1
  fi
}

require_deps() {
  require_file "$repo_root/.venv/bin/uvicorn" "Missing $repo_root/.venv/bin/uvicorn. Run: python3.12 -m venv .venv && .venv/bin/pip install -r apps/api/requirements.txt"
  if ! command -v npm >/dev/null 2>&1; then
    printf 'Missing npm. Install Node.js/npm first.\n' >&2
    exit 1
  fi
  require_file "$repo_root/node_modules" "Missing node_modules. Run: npm install"
  if [ ! -f "$api_root/.env" ]; then
    printf 'Warning: %s is missing; API will run on defaults. Copy apps/api/.env.example if Kalshi credentials are needed.\n' "$api_root/.env" >&2
  fi
}

uses_default_local_postgres() {
  [[ "$local_database_url" == postgresql*localhost:5432/kalshi_sports_copilot* || "$local_database_url" == postgresql*127.0.0.1:5432/kalshi_sports_copilot* ]]
}

require_docker_daemon() {
  if ! command -v docker >/dev/null 2>&1; then
    printf 'Missing docker. Start Docker Desktop or set DATABASE_URL to another reachable database.\n' >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    printf 'Docker Desktop is not running or the Docker socket is unavailable.\n' >&2
    printf 'Start Docker Desktop, wait for it to finish starting, then rerun: sika db:up or sika restart\n' >&2
    exit 1
  fi
}

postgres_up() {
  require_docker_daemon
  (
    cd "$repo_root"
    docker compose up -d postgres
  )
  local tries=0
  until (
    cd "$repo_root"
    docker compose exec -T postgres pg_isready -U postgres -d kalshi_sports_copilot >/dev/null 2>&1
  ); do
    tries=$((tries + 1))
    if [ "$tries" -ge 60 ]; then
      printf 'Local Postgres did not become ready. Check Docker Desktop and docker compose logs postgres.\n' >&2
      exit 1
    fi
    sleep 1
  done
}

ensure_database() {
  if uses_default_local_postgres; then
    postgres_up
  fi
}

port_in_use() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

assert_ports_available_for_start() {
  if port_in_use "$api_port" && ! pid_file_running "$api_pid_file"; then
    printf 'Port %s is already in use by another process. Stop it or set SIKA_API_PORT.\n' "$api_port" >&2
    exit 1
  fi
  if port_in_use "$web_port" && ! pid_file_running "$web_pid_file"; then
    printf 'Port %s is already in use by another process. Stop it or set SIKA_WEB_PORT.\n' "$web_port" >&2
    exit 1
  fi
}

build_web() {
  require_deps
  (
    cd "$repo_root"
    export SIKA_API_BASE_URL="$api_base_url"
    npm run web:build
  )
}

ensure_web_build() {
  if [ ! -f "$web_root/.next/BUILD_ID" ]; then
    printf 'No local web build found; building once before start.\n'
    build_web
  fi
}

wait_for_api() {
  local tries=0
  until curl -fsS --max-time "$health_timeout_seconds" "$api_base_url/health" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge 60 ]; then
      printf 'API did not become healthy at %s. Check %s\n' "$api_base_url/health" "$api_log" >&2
      return 1
    fi
    sleep 1
  done
}

run_api() {
  cd "$api_root"
  export DATABASE_URL="$local_database_url"
  export ENVIRONMENT="${ENVIRONMENT:-local-server}"
  export SCHEDULER_ENABLED="${SCHEDULER_ENABLED:-true}"
  exec "$repo_root/.venv/bin/uvicorn" app.main:app --host "$api_host" --port "$api_port"
}

run_web() {
  cd "$web_root"
  export SIKA_API_BASE_URL="$api_base_url"
  exec npm run start -- --hostname "$web_host" --port "$web_port"
}

cleanup_children() {
  local api_pid web_pid
  api_pid="$(read_pid "$api_pid_file")"
  web_pid="$(read_pid "$web_pid_file")"
  if is_running "$web_pid"; then
    kill "$web_pid" 2>/dev/null || true
  fi
  if is_running "$api_pid"; then
    kill "$api_pid" 2>/dev/null || true
  fi
  wait "$web_pid" "$api_pid" 2>/dev/null || true
  rm -f "$api_pid_file" "$web_pid_file" "$supervisor_pid_file"
}

stop_launch_agent_if_loaded() {
  if [ ! -f "$launch_agent_plist" ] || ! command -v launchctl >/dev/null 2>&1; then
    return 0
  fi

  if launchctl print "$launchd_domain/$launch_agent_label" >/dev/null 2>&1; then
    launchctl bootout "$launchd_domain" "$launch_agent_plist" 2>/dev/null || true
    printf 'Stopped SIKA login auto-start for this user session.\n'
    printf 'To turn login auto-start back on now, run: npm run server:install-login\n'
    sleep 1
  fi
}

start_launch_agent_if_installed() {
  if [ ! -f "$launch_agent_plist" ] || ! command -v launchctl >/dev/null 2>&1; then
    return 1
  fi

  if ! launchctl print "$launchd_domain/$launch_agent_label" >/dev/null 2>&1; then
    launchctl bootstrap "$launchd_domain" "$launch_agent_plist" 2>/dev/null || true
  fi
  launchctl kickstart -k "$launchd_domain/$launch_agent_label" 2>/dev/null || true
  printf 'Started SIKA login auto-start agent.\n'
  return 0
}

run_foreground() {
  ensure_dirs
  require_deps
  ensure_web_build
  assert_ports_available_for_start
  ensure_database
  echo "$$" > "$supervisor_pid_file"
  trap cleanup_children EXIT INT TERM

  printf 'Starting local API at %s\n' "$api_base_url"
  run_api >> "$api_log" 2>&1 &
  echo "$!" > "$api_pid_file"

  wait_for_api

  printf 'Starting local web at %s\n' "$web_url"
  run_web >> "$web_log" 2>&1 &
  echo "$!" > "$web_pid_file"

  printf 'SIKA laptop server is running.\n'
  printf 'Web: %s\n' "$web_url"
  printf 'API: %s\n' "$api_base_url"

  while true; do
    local api_pid web_pid
    api_pid="$(read_pid "$api_pid_file")"
    web_pid="$(read_pid "$web_pid_file")"
    if ! is_running "$api_pid"; then
      printf 'API process exited. Check %s\n' "$api_log" >&2
      exit 1
    fi
    if ! is_running "$web_pid"; then
      printf 'Web process exited. Check %s\n' "$web_log" >&2
      exit 1
    fi
    sleep 5
  done
}

start_background() {
  ensure_dirs
  require_deps
  if pid_file_running "$supervisor_pid_file"; then
    printf 'Laptop server already running.\n'
    status
    return 0
  fi
  ensure_web_build
  assert_ports_available_for_start
  ensure_database

  if start_launch_agent_if_installed; then
    wait_for_api
    printf 'Started SIKA laptop server.\n'
    printf 'Web: %s\n' "$web_url"
    printf 'API: %s\n' "$api_base_url"
    printf 'Logs: %s\n' "$log_dir"
    return 0
  fi

  nohup "$0" run >> "$supervisor_log" 2>&1 &
  echo "$!" > "$supervisor_pid_file"
  wait_for_api
  printf 'Started SIKA laptop server.\n'
  printf 'Web: %s\n' "$web_url"
  printf 'API: %s\n' "$api_base_url"
  printf 'Logs: %s\n' "$log_dir"
}

stop_background() {
  local supervisor_pid
  stop_launch_agent_if_loaded
  supervisor_pid="$(read_pid "$supervisor_pid_file")"
  if is_running "$supervisor_pid"; then
    kill "$supervisor_pid" 2>/dev/null || true
  fi
  cleanup_children
  printf 'Stopped SIKA laptop server.\n'
}

status() {
  ensure_dirs
  local supervisor_pid api_pid web_pid
  supervisor_pid="$(read_pid "$supervisor_pid_file")"
  api_pid="$(read_pid "$api_pid_file")"
  web_pid="$(read_pid "$web_pid_file")"
  printf 'Supervisor: '
  if is_running "$supervisor_pid"; then printf 'running (pid %s)\n' "$supervisor_pid"; else printf 'stopped\n'; fi
  printf 'API:        '
  if is_running "$api_pid"; then printf 'running (pid %s, %s)\n' "$api_pid" "$api_base_url"; else printf 'stopped\n'; fi
  printf 'Web:        '
  if is_running "$web_pid"; then printf 'running (pid %s, %s)\n' "$web_pid" "$web_url"; else printf 'stopped\n'; fi
  printf '\nHealth:\n'
  curl -fsS --max-time "$health_timeout_seconds" "$api_base_url/health" 2>/dev/null || printf 'API health unavailable\n'
  printf '\n'
}

health() {
  printf 'API health:\n'
  curl -fsS "$api_base_url/health"
  printf '\n\nProduct freshness:\n'
  curl -fsS "$api_base_url/product/freshness"
  printf '\n'
}

refresh_now() {
  curl -fsS -X POST "$api_base_url/ops/jobs/refresh"
  printf '\n'
}

open_dashboard() {
  start_background
  if command -v open >/dev/null 2>&1; then
    open "$web_url/trade?sport=MLB"
    open "$web_url/runs"
  else
    printf 'Open %s in your browser.\n' "$web_url"
  fi
  printf '\n'
  status
}

du_if_exists() {
  local path="$1"
  if [ -e "$path" ]; then
    du -sh "$path" 2>/dev/null || true
  else
    printf '0B\t%s (missing)\n' "$path"
  fi
}

available_kib() {
  df -k "$HOME" 2>/dev/null | awk 'NR == 2 {print $4}'
}

storage_space_warning() {
  local available_kib_value="${1:-}"
  if [ -z "$available_kib_value" ]; then
    return 0
  fi

  local warn_kib=$((25 * 1024 * 1024))
  local critical_kib=$((10 * 1024 * 1024))
  local available_gib
  available_gib="$(awk -v kib="$available_kib_value" 'BEGIN {printf "%.1f", kib / 1024 / 1024}')"

  if [ "$available_kib_value" -lt "$critical_kib" ]; then
    printf '\nCRITICAL: only %s GiB free. Stop and free disk before long refresh windows.\n' "$available_gib"
  elif [ "$available_kib_value" -lt "$warn_kib" ]; then
    printf '\nWARNING: only %s GiB free. Keep at least 25 GiB free for laptop-server stability.\n' "$available_gib"
  else
    printf '\nFree-space check: %s GiB free.\n' "$available_gib"
  fi
}

storage_report() {
  ensure_dirs
  local home_available_kib
  home_available_kib="$(available_kib)"
  printf 'Disk capacity:\n'
  df -h "$HOME" "$repo_root" 2>/dev/null || true
  storage_space_warning "$home_available_kib"

  printf '\nSIKA local storage:\n'
  du_if_exists "$repo_root"
  du_if_exists "$state_dir"
  du_if_exists "$state_dir/backups"
  du_if_exists "$log_dir"
  du_if_exists "$repo_root/output"
  du_if_exists "$repo_root/test-results"
  du_if_exists "$repo_root/node_modules"
  du_if_exists "$web_root/.next"
  du_if_exists "$web_root/tsconfig.tsbuildinfo"

  printf '\nRender dump backups:\n'
  if [ -d "$state_dir/backups" ]; then
    find "$state_dir/backups" -maxdepth 1 -type f -print0 \
      | xargs -0 ls -lh 2>/dev/null || true
  else
    printf 'No backup directory found.\n'
  fi

  printf '\nDocker storage:\n'
  if command -v docker >/dev/null 2>&1; then
    docker system df 2>/dev/null || printf 'Docker is not reachable. Start Docker Desktop and rerun this command.\n'
  else
    printf 'Docker command not found.\n'
  fi

  printf '\nCleanup commands:\n'
  printf '  sika cleanup --yes\n'
  printf '  sika cleanup --yes --aggressive\n'
  printf '\nNotes:\n'
  printf '  Safe cleanup keeps the newest two Render dumps and never removes Docker volumes.\n'
  printf '  Aggressive cleanup may remove disposable Docker build cache and the local Next.js build.\n'
  printf '  External drives are best for backup archives unless you intentionally move the live database.\n'
}

cleanup_storage() {
  ensure_dirs
  local yes=false
  local aggressive=false
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --yes) yes=true ;;
      --aggressive) aggressive=true ;;
      *)
        printf 'Unknown cleanup option: %s\n' "$1" >&2
        exit 1
        ;;
    esac
    shift
  done

  if [ "$yes" != true ]; then
    storage_report
    printf '\nNo files were changed. Rerun with --yes to clean disposable artifacts.\n'
    return 0
  fi

  printf 'Cleaning disposable SIKA artifacts...\n'
  if [ -d "$state_dir/backups" ]; then
    find "$state_dir/backups" -maxdepth 1 -type f -size 0 -delete
    find "$state_dir/backups" -maxdepth 1 -type f -name 'render-*.dump' -print0 \
      | xargs -0 ls -t 2>/dev/null \
      | tail -n +3 \
      | while IFS= read -r old_backup; do
          rm -f "$old_backup"
        done
  fi

  if [ -d "$log_dir" ]; then
    find "$log_dir" -maxdepth 1 -type f -name '*.log' -size +10M \
      -exec sh -c ': > "$1"' _ {} \;
  fi

  rm -rf "$repo_root/output"/* "$repo_root/test-results"/*
  rm -f "$web_root/tsconfig.tsbuildinfo"

  if [ "$aggressive" = true ]; then
    if pid_file_running "$web_pid_file"; then
      printf 'Skipping %s because the web server is running.\n' "$web_root/.next"
    else
      rm -rf "$web_root/.next"
    fi
    if command -v docker >/dev/null 2>&1; then
      docker builder prune -f 2>/dev/null || true
      docker image prune -f 2>/dev/null || true
    fi
  fi

  printf '\nStorage after cleanup:\n'
  storage_report
}

tail_logs() {
  ensure_dirs
  local name="${1:-all}"
  case "$name" in
    supervisor) tail -n 120 -f "$supervisor_log" ;;
    api) tail -n 120 -f "$api_log" ;;
    web) tail -n 120 -f "$web_log" ;;
    all) tail -n 80 -f "$supervisor_log" "$api_log" "$web_log" ;;
    *)
      printf 'Unknown log name: %s\n' "$name" >&2
      exit 1
      ;;
  esac
}

command="${1:-}"
case "$command" in
  build) build_web ;;
  db:up) postgres_up ;;
  open) open_dashboard ;;
  run) run_foreground ;;
  start) start_background ;;
  stop) stop_background ;;
  restart) stop_background; start_background ;;
  status) status ;;
  health) health ;;
  refresh) refresh_now ;;
  storage) storage_report ;;
  cleanup) shift; cleanup_storage "$@" ;;
  logs) tail_logs "${2:-all}" ;;
  -h|--help|help|"") usage ;;
  *)
    printf 'Unknown command: %s\n' "$command" >&2
    usage >&2
    exit 1
    ;;
esac
