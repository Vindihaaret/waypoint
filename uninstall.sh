#!/bin/bash
# Fjerner Waypoint. Sikkerhetskopiene dine, innstillingene og ~/.local/share/waypoint/replaced blir liggende.
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
systemctl --user disable --now waypoint-auto.timer 2>/dev/null || true
rm -f "$CONF/systemd/user/waypoint-auto.timer" "$CONF/systemd/user/waypoint-auto.service" \
  "$CONF/autostart/io.github.waypoint.Waypoint-tray.desktop"
systemctl --user daemon-reload 2>/dev/null || true
# Stopp statusfeltikonet. Ikke «pkill -f»: et bredt mønster kan treffe andre prosesser (eller skallet selv).
for pid in $(ps -eo pid=,args= | awk '$2 ~ /python3$/ && $3 == "-m" && $4 == "waypoint" && $5 == "tray" {print $1}'); do
  kill "$pid" 2>/dev/null || true
done
rm -rf "$PREFIX/waypoint/app"
rmdir "$PREFIX/waypoint" 2>/dev/null || true
rm -f "$HOME/.local/bin/waypoint" "$PREFIX/applications/io.github.waypoint.Waypoint.desktop" \
  "$PREFIX/icons/hicolor/scalable/apps/io.github.waypoint.Waypoint.svg" \
  "$PREFIX/icons/hicolor/symbolic/apps/io.github.waypoint.Waypoint-symbolic.svg"
echo "Waypoint er fjernet."
