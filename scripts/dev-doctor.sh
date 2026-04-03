#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=./lib/dev-guard.sh
source "$script_dir/lib/dev-guard.sh"

repo_root="$(dev_repo_root)"

printf 'Repo root: %s\n' "$repo_root"
dev_print_port_report 8000 "$repo_root"
dev_print_port_report 3000 "$repo_root"

response="$(dev_fetch_health || true)"
if [ -z "$response" ]; then
  printf 'Health: unreachable\n'
  exit 0
fi

if dev_health_is_current "$response"; then
  printf 'Health schema: current\n'
else
  printf 'Health schema: stale\n'
fi
printf 'Health payload: %s\n' "$response"
