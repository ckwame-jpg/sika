#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/.." && pwd -P)"
bin_dir="$HOME/.local/bin"
zshrc="$HOME/.zshrc"
sika_target="$repo_root/scripts/sika"
sika_link="$bin_dir/sika"
desktop_dir="$HOME/Desktop"

escape_applescript_string() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

write_terminal_app() {
  local app_name="$1"
  local terminal_title="$2"
  local terminal_command="$3"
  local app_path="$desktop_dir/$app_name.app"
  local macos_dir="$app_path/Contents/MacOS"
  local resources_dir="$app_path/Contents/Resources"
  local bundle_id_name escaped_command escaped_title

  bundle_id_name="$(printf '%s' "$app_name" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9.-')"
  escaped_command="$(escape_applescript_string "$terminal_command")"
  escaped_title="$(escape_applescript_string "$terminal_title")"

  rm -rf "$app_path"
  mkdir -p "$macos_dir" "$resources_dir"

  cat > "$app_path/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>launcher</string>
  <key>CFBundleIdentifier</key>
  <string>local.sika.$bundle_id_name</string>
  <key>CFBundleName</key>
  <string>$app_name</string>
  <key>CFBundleDisplayName</key>
  <string>$app_name</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleVersion</key>
  <string>1.0</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>LSMinimumSystemVersion</key>
  <string>10.13</string>
</dict>
</plist>
PLIST

  cat > "$macos_dir/launcher" <<LAUNCHER
#!/usr/bin/env bash
/usr/bin/osascript <<'APPLESCRIPT'
tell application "Terminal"
  activate
  do script "printf '\\\\e]0;$escaped_title\\\\a'; $escaped_command"
end tell
APPLESCRIPT
LAUNCHER
  chmod +x "$macos_dir/launcher"
}

install_cli() {
  mkdir -p "$bin_dir"
  ln -sfn "$sika_target" "$sika_link"
  chmod +x "$sika_target"

  touch "$zshrc"
  if ! grep -qs 'HOME/.local/bin' "$zshrc"; then
    {
      printf '\n# SIKA local command\n'
      printf 'export PATH="$HOME/.local/bin:$PATH"\n'
    } >> "$zshrc"
  fi
}

remove_old_desktop_launchers() {
  rm -f \
    "$desktop_dir/SIKA Server.command" \
    "$desktop_dir/SIKA Server Stop.command" \
    "$desktop_dir/SIKA Server Storage.command"
}

install_apps() {
  mkdir -p "$desktop_dir"
  local sika_bin="$sika_link"
  local quoted_repo quoted_sika
  quoted_repo="$(printf '%q' "$repo_root")"
  quoted_sika="$(printf '%q' "$sika_bin")"

  write_terminal_app "sika" "sika" \
    "cd $quoted_repo; $quoted_sika open; printf '\\nStreaming logs. Closing this Terminal window does not stop SIKA. Use sika stop to stop it.\\n\\n'; $quoted_sika logs all"
  write_terminal_app "sika stop" "sika stop" \
    "cd $quoted_repo; $quoted_sika stop; printf '\\nPress Return to close this window.'; read _"
  write_terminal_app "sika storage" "sika storage" \
    "cd $quoted_repo; $quoted_sika storage; printf '\\nPress Return to close this window.'; read _"
}

install_cli
remove_old_desktop_launchers
install_apps

printf 'Installed terminal command: %s\n' "$sika_link"
printf 'Desktop apps:\n'
printf '  %s\n' "$desktop_dir/sika.app"
printf '  %s\n' "$desktop_dir/sika stop.app"
printf '  %s\n' "$desktop_dir/sika storage.app"
printf '\nIf this shell cannot find sika yet, run:\n'
printf '  export PATH="$HOME/.local/bin:$PATH"\n'
