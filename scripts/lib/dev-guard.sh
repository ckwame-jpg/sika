#!/usr/bin/env bash

dev_repo_root() {
  cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P
}

dev_port_pids() {
  local port="$1"
  lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u
}

dev_pid_cwd() {
  local pid="$1"
  lsof -p "$pid" -a -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

dev_collect_pids() {
  local port="$1"
  local pids=()
  local pid=""

  while IFS= read -r pid; do
    if [ -n "$pid" ]; then
      pids+=("$pid")
    fi
  done < <(dev_port_pids "$port")

  if [ "${#pids[@]}" -gt 0 ]; then
    printf '%s\n' "${pids[@]}"
  fi
}

dev_path_within_repo() {
  local candidate="${1%/}"
  local repo_root="${2%/}"
  case "$candidate" in
    "$repo_root"|"$repo_root"/*) return 0 ;;
    *) return 1 ;;
  esac
}

dev_port_status_or_die() {
  local port="$1"
  local label="$2"
  local repo_root="$3"
  local pids=()
  local pid=""
  local cwd=""

  while IFS= read -r pid; do
    if [ -n "$pid" ]; then
      pids+=("$pid")
    fi
  done < <(dev_collect_pids "$port")
  if [ "${#pids[@]}" -eq 0 ]; then
    printf 'free\n'
    return 0
  fi

  for pid in "${pids[@]}"; do
    cwd="$(dev_pid_cwd "$pid")"
    if ! dev_path_within_repo "$cwd" "$repo_root"; then
      printf '%s port %s is already owned by another checkout.\n' "$label" "$port" >&2
      for pid in "${pids[@]}"; do
        cwd="$(dev_pid_cwd "$pid")"
        printf '  PID %s cwd=%s\n' "$pid" "${cwd:-unknown}" >&2
      done
      printf 'Stop the conflicting process(es), for example:\n' >&2
      printf '  kill %s\n' "${pids[*]}" >&2
      exit 1
    fi
  done

  printf 'repo\n'
}

dev_fetch_health() {
  curl -fsS http://127.0.0.1:8000/health 2>/dev/null
}

dev_health_is_current() {
  local payload="$1"
  case "$payload" in
    *'"refresh_status"'*'"refresh_reason"'*'"last_successful_refresh_at"'*'"data_stale"'*'"refresh_error_message"'*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

dev_wait_for_current_health() {
  local attempts="${1:-45}"
  local response=""
  local attempt=1

  while [ "$attempt" -le "$attempts" ]; do
    response="$(dev_fetch_health || true)"
    if [ -n "$response" ]; then
      if dev_health_is_current "$response"; then
        return 0
      fi
      printf 'API on port 8000 responded with a stale /health payload.\n' >&2
      printf '%s\n' "$response" >&2
      return 1
    fi
    sleep 1
    attempt=$((attempt + 1))
  done

  printf 'Timed out waiting for http://127.0.0.1:8000/health\n' >&2
  return 1
}

dev_print_port_report() {
  local port="$1"
  local repo_root="$2"
  local pids=()
  local pid=""
  local cwd=""
  local status="foreign"

  while IFS= read -r pid; do
    if [ -n "$pid" ]; then
      pids+=("$pid")
    fi
  done < <(dev_collect_pids "$port")
  if [ "${#pids[@]}" -eq 0 ]; then
    printf 'Port %s: free\n' "$port"
    return 0
  fi

  status="repo"
  for pid in "${pids[@]}"; do
    cwd="$(dev_pid_cwd "$pid")"
    if ! dev_path_within_repo "$cwd" "$repo_root"; then
      status="foreign"
    fi
  done

  if [ "$status" = "repo" ]; then
    printf 'Port %s: owned by this repo\n' "$port"
  else
    printf 'Port %s: owned by another checkout\n' "$port"
  fi

  for pid in "${pids[@]}"; do
    cwd="$(dev_pid_cwd "$pid")"
    printf '  PID %s cwd=%s\n' "$pid" "${cwd:-unknown}"
  done
}
