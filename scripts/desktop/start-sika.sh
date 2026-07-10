#!/bin/bash
# Start the Sika dev stack (FastAPI API + Next.js web) and open the dashboard.
# Safe to run repeatedly: healthy stack -> just opens the browser; wedged
# stack (listening but erroring, e.g. after a macOS permission denial) ->
# stopped and restarted.
set -u

REPO="/Users/jollof/Documents/lock in/sika"
LOG="$HOME/Library/Logs/sika-dev.log"
mkdir -p "$HOME/Library/Logs"

# Put the nvm-installed Node on PATH (no shell profile sourcing needed).
NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | tail -1)"
[ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"

web_status() {
  curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://localhost:3000/trade 2>/dev/null
}

status="$(web_status)"
if [ "$status" != "200" ]; then
  # Anything listening is unhealthy (5xx) or half-up — clear it first so
  # we never stack a second npm run dev on occupied ports.
  if [ "$status" != "000" ]; then
    "$REPO/scripts/desktop/stop-sika.sh"
  fi
  cd "$REPO" || exit 1
  nohup npm run dev > "$LOG" 2>&1 < /dev/null &
  # Wait up to ~120s for the web server to answer healthy.
  for _ in $(seq 1 60); do
    [ "$(web_status)" = "200" ] && break
    sleep 2
  done
fi

open "http://localhost:3000"
