#!/bin/bash
# Removes Waypoint. Your backups, the password in the keyring and ~/.local/share/waypoint/replaced are kept.
# With --purge, your settings and status (~/.config/waypoint, ~/.local/state/waypoint) are deleted as well.
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}"
PURGE=""
[ "${1:-}" = "--purge" ] && PURGE=1
systemctl --user disable --now waypoint-auto.timer 2>/dev/null || true
rm -f "$CONF/systemd/user/waypoint-auto.timer" "$CONF/systemd/user/waypoint-auto.service" \
  "$CONF/autostart/io.github.waypoint.Waypoint-tray.desktop"
systemctl --user daemon-reload 2>/dev/null || true
# Stop the tray icon. Not "pkill -f": a broad pattern can hit other processes (or this shell).
for pid in $(ps -eo pid=,args= | awk '$2 ~ /python3$/ && $3 == "-m" && $4 == "waypoint" && $5 == "tray" {print $1}'); do
  kill "$pid" 2>/dev/null || true
done
rm -rf "$PREFIX/waypoint/app"
rmdir "$PREFIX/waypoint" 2>/dev/null || true
rm -f "$HOME/.local/bin/waypoint" "$PREFIX/applications/io.github.waypoint.Waypoint.desktop" \
  "$PREFIX/icons/hicolor/scalable/apps/io.github.waypoint.Waypoint.svg" \
  "$PREFIX/icons/hicolor/symbolic/apps/io.github.waypoint.Waypoint-symbolic.svg"
if [ -n "$PURGE" ]; then
  rm -rf "$CONF/waypoint" "$STATE/waypoint"
  echo "Waypoint and its settings have been removed. Your backups and the password in the keyring are kept."
else
  echo "Waypoint has been removed. Your backups and settings are kept."
fi
