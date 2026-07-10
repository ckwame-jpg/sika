#!/bin/bash
# Stop the Sika dev servers by freeing ports 3000 (web) and 8000 (API).
for p in 3000 8000; do
  pids="$(lsof -nP -tiTCP:"$p" -sTCP:LISTEN 2>/dev/null)"
  [ -n "$pids" ] && kill $pids 2>/dev/null
done
sleep 2
# Force-kill anything that ignored the polite signal.
for p in 3000 8000; do
  pids="$(lsof -nP -tiTCP:"$p" -sTCP:LISTEN 2>/dev/null)"
  [ -n "$pids" ] && kill -9 $pids 2>/dev/null
done
exit 0
