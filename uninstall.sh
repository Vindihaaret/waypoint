#!/bin/bash
# Removes Waypoint. Your backups, settings and ~/.local/share/waypoint/replaced are kept.
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
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
echo "Waypoint has been removed. Your backups and settings are kept."
