#!/bin/bash
# Takes the README screenshots (light and dark) with test settings in a temporary folder, so your own
# settings, status and keyring are not touched. Usage: tools/screenshots.sh [output folder]
# (default: docs/screenshots). Runs as a separate instance, so an open Waypoint window is not affected.
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=$(realpath -m "${1:-docs/screenshots}")
mkdir -p "$OUT"
TMP=$(mktemp -d)
trap 'kill "$SLEEPER" 2>/dev/null || true; rm -rf "$TMP"' EXIT

# A running backup needs a live process id, or Waypoint shows it as interrupted.
sleep 900 &
SLEEPER=$!
BOOT=$(cat /proc/sys/kernel/random/boot_id)
NOW=$(date -Iseconds)
TODAY_20=$(date -Iseconds -d 'today 20:00')

for theme in light dark; do
  T="$TMP/$theme"
  mkdir -p "$T/config/waypoint" "$T/state/waypoint"
  touch "$T/config/waypoint/.migrated-from-flytt"
  cat > "$T/config/waypoint/settings.json" <<EOF
{"language": "en", "theme": "$theme", "tray": false, "autostart": false, "update_check": false, "glow": true,
 "jobs": [
  {"id": "files", "name": "Files", "enabled": true, "interval": "weekly", "at": "21:30", "weekday": 6,
   "preset": "files", "choices": {},
   "storage": {"kind": "local", "path": "/run/media/user/External"}},
  {"id": "settings", "name": "Settings", "enabled": false, "interval": "daily", "preset": "setup", "choices": {},
   "storage": {"kind": "network",
               "path": "/run/user/$(id -u)/gvfs/smb-share:server=nas.local,share=backup",
               "uri": "smb://nas.local/backup/",
               "network": {"protocol": "smb", "server": "nas.local", "user": "alex", "domain": ""}}}
 ]}
EOF
  cat > "$T/state/waypoint/status.json" <<EOF
{"jobs": {
  "files": {"state": "ok", "last_success": "$TODAY_20"},
  "settings": {"state": "running", "pid": $SLEEPER, "boot": "$BOOT", "started": "$NOW", "progress": 0.42,
               "current": "$HOME/Pictures/Holiday 2025/IMG_2041.jpg", "interactive": true}}}
EOF
  # name  page  size
  while read -r name page size; do
    echo "Taking $name-$theme.png"
    XDG_CONFIG_HOME="$T/config" XDG_STATE_HOME="$T/state" WAYPOINT_PASSWORD=screenshot \
      WAYPOINT_SCREENSHOT="$OUT/$name-$theme.png" WAYPOINT_WINDOW_SIZE="$size" WAYPOINT_PAGE="$page" \
      WAYPOINT_SCREENSHOT_DELAY=4 timeout 40 python3 -m waypoint 2>>"$TMP/log.txt" \
      || { echo "Failed: $name-$theme.png (log below)"; cat "$TMP/log.txt"; exit 1; }
  done <<EOF
start       -                 640x760
task        job:files         640x880
storage     storage:settings  640x760
settings    preferences       640x760
EOF
done
echo "Screenshots saved in $OUT"
