#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/.." && pwd -P)"
label="com.sika.laptop-server"
plist="$HOME/Library/LaunchAgents/$label.plist"
launchd_domain="gui/$(id -u)"
log_dir="$repo_root/.local-server/logs"

uninstall() {
  launchctl bootout "$launchd_domain" "$plist" 2>/dev/null || true
  rm -f "$plist"
  printf 'Removed %s\n' "$plist"
}

if [ "${1:-}" = "uninstall" ]; then
  uninstall
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$log_dir"

cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$repo_root/scripts/laptop-server.sh</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$repo_root</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>$log_dir/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$log_dir/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>SIKA_API_HOST</key>
    <string>127.0.0.1</string>
    <key>SIKA_API_PORT</key>
    <string>8000</string>
    <key>SIKA_WEB_HOST</key>
    <string>127.0.0.1</string>
    <key>SIKA_WEB_PORT</key>
    <string>3000</string>
    <key>SIKA_API_BASE_URL</key>
    <string>http://127.0.0.1:8000</string>
    <key>DATABASE_URL</key>
    <string>postgresql+psycopg://postgres:postgres@localhost:5432/kalshi_sports_copilot</string>
    <key>SCHEDULER_ENABLED</key>
    <string>true</string>
  </dict>
</dict>
</plist>
PLIST

launchctl bootout "$launchd_domain" "$plist" 2>/dev/null || true
launchctl bootstrap "$launchd_domain" "$plist"
launchctl enable "$launchd_domain/$label"
launchctl kickstart -k "$launchd_domain/$label"

printf 'Installed and started %s\n' "$label"
printf 'Web: http://127.0.0.1:3000\n'
printf 'API: http://127.0.0.1:8000\n'
printf 'Status: sika status\n'
printf 'Logs: sika logs\n'
