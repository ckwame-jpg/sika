#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=./lib/dev-guard.sh
source "$script_dir/lib/dev-guard.sh"

repo_root="$(dev_repo_root)"
web_root="$repo_root/apps/web"
created_pids=()

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [ "${#created_pids[@]}" -gt 0 ]; then
    printf '\nStopping local dev processes...\n'
    kill "${created_pids[@]}" 2>/dev/null || true
    wait "${created_pids[@]}" 2>/dev/null || true
  fi
  exit "$exit_code"
}

trap cleanup EXIT INT TERM

api_status="$(dev_port_status_or_die 8000 "API" "$repo_root")"
web_status="$(dev_port_status_or_die 3000 "Web" "$repo_root")"

if [ "$api_status" = "free" ]; then
  printf 'Starting API from %s\n' "$repo_root"
  bash "$script_dir/api-dev.sh" &
  created_pids+=("$!")
else
  printf 'Reusing API already running from this repo on port 8000.\n'
fi

printf 'Waiting for current API health payload...\n'
dev_wait_for_current_health

if [ "$web_status" = "free" ]; then
  printf 'Starting web from %s\n' "$repo_root"
  (
    cd "$web_root"
    export SIKA_API_BASE_URL="${SIKA_API_BASE_URL:-http://127.0.0.1:8000}"
    npm run dev
  ) &
  created_pids+=("$!")
else
  printf 'Reusing web already running from this repo on port 3000.\n'
fi

if [ "${#created_pids[@]}" -eq 0 ]; then
  printf 'Both API and web are already running from this repo.\n'
  exit 0
fi

wait "${created_pids[@]}"
