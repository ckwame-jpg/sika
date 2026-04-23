#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file="$repo_root/apps/api/.env"
env_example="$repo_root/apps/api/.env.example"
owner_token_file="$repo_root/apps/api/.owner-token"
kalshi_key_dir="$HOME/.config/kalshi"
kalshi_key_file="$kalshi_key_dir/kalshi-live.key"

if [ ! -f "$env_file" ]; then
  cp "$env_example" "$env_file"
fi

backup_file="$env_file.backup.$(date +%Y%m%d%H%M%S)"
cp "$env_file" "$backup_file"

upsert_env() {
  local key="$1"
  local value="$2"
  local tmp_file
  tmp_file="$(mktemp)"
  grep -v "^${key}=" "$env_file" > "$tmp_file" || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
  mv "$tmp_file" "$env_file"
}

generate_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
    return
  fi
  LC_ALL=C tr -dc 'A-Fa-f0-9' </dev/urandom | head -c 64
  printf '\n'
}

printf 'Sika live secret setup\n'
printf 'This writes secrets only to local files ignored by git.\n\n'

read -r -p "Kalshi production API key ID: " kalshi_live_key_id
if [ -z "$kalshi_live_key_id" ]; then
  printf 'Kalshi key ID is required.\n' >&2
  exit 1
fi

mkdir -p "$kalshi_key_dir"
chmod 700 "$kalshi_key_dir"

printf '\nPaste the full Kalshi PRIVATE KEY below.\n'
printf 'When done, type END_SIKA_KEY on its own line and press Enter.\n\n'
: > "$kalshi_key_file"
while IFS= read -r line; do
  if [ "$line" = "END_SIKA_KEY" ]; then
    break
  fi
  printf '%s\n' "$line" >> "$kalshi_key_file"
done
chmod 600 "$kalshi_key_file"

if ! grep -q "BEGIN .*PRIVATE KEY" "$kalshi_key_file"; then
  printf 'The Kalshi private key file does not look like a PEM private key. Check %s\n' "$kalshi_key_file" >&2
  exit 1
fi

printf '\n'
read -r -s -p "OpenAI API key for the site analyst: " openai_api_key
printf '\n'
if [ -z "$openai_api_key" ]; then
  printf 'OpenAI API key is required for analyst chat.\n' >&2
  exit 1
fi

owner_token="$(generate_token)"
printf '%s\n' "$owner_token" > "$owner_token_file"
chmod 600 "$owner_token_file"

upsert_env "KALSHI_LIVE_BASE_URL" "https://api.elections.kalshi.com/trade-api/v2"
upsert_env "KALSHI_LIVE_KEY_ID" "$kalshi_live_key_id"
upsert_env "KALSHI_LIVE_PRIVATE_KEY_PATH" "$kalshi_key_file"
upsert_env "SIKA_OWNER_ADMIN_TOKEN" "$owner_token"
upsert_env "AUTO_TRADING_ENABLED" "true"
upsert_env "AUTO_TRADING_DAILY_BUDGET_CENTS" "1000"
upsert_env "AUTO_TRADING_LOCAL_TIME" "10:15"
upsert_env "AUTO_TRADING_MAX_ORDERS_PER_DAY" "5"
upsert_env "AUTO_TRADING_MARKET_SCOPE" "nba_mlb_current_slate"
upsert_env "AUTO_TRADING_ALLOW_PARLAYS" "false"
upsert_env "OPENAI_API_KEY" "$openai_api_key"
upsert_env "OPENAI_CHAT_MODEL" "gpt-5.4-mini"

printf '\nDone.\n'
printf 'Kalshi private key: %s\n' "$kalshi_key_file"
printf 'API env file: %s\n' "$env_file"
printf 'Owner token saved at: %s\n' "$owner_token_file"
printf 'Backup env file: %s\n' "$backup_file"
printf '\nUse the owner token from apps/api/.owner-token when the site asks for X-Sika-Admin-Token.\n'
