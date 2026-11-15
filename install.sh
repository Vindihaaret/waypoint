#!/bin/bash
# Installs Waypoint for you (no sudo): ~/.local/share/waypoint/app, the "waypoint" command and an app icon.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN="$HOME/.local/bin"

python3 - <<'PY' || { echo "Waypoint needs Python 3.10+, PyGObject, GTK 4.12+ and libadwaita 1.6+."; \
  echo "openSUSE: sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1"; \
  echo "Fedora:   sudo dnf install python3-gobject gtk4 libadwaita"; \
  echo "Ubuntu:   sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"; exit 1; }
import sys
assert sys.version_info >= (3, 10)
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
assert (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 12)
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 6)
PY

# Remove the old installation from when the app was called Flytt (its settings are moved on first start).
rm -rf "$PREFIX/flytt/app"
rmdir "$PREFIX/flytt" 2>/dev/null || true
rm -f "$BIN/flytt" "$PREFIX/applications/io.github.flytt.Flytt.desktop" \
  "$PREFIX/icons/hicolor/scalable/apps/io.github.flytt.Flytt.svg" \
  "$PREFIX/icons/hicolor/symbolic/apps/io.github.flytt.Flytt-symbolic.svg"

# The code lives in ~/.local/share/waypoint/app; "replaced" next to it must never be touched.
CODE="$PREFIX/waypoint/app"
rm -rf "$CODE"
mkdir -p "$CODE" "$BIN"
cp -r waypoint "$CODE/"
find "$CODE" -name __pycache__ -prune -exec rm -rf {} +
cat > "$BIN/waypoint" <<EOS
#!/bin/sh
# PYTHONSAFEPATH: do not put the current folder first in sys.path (or a source folder you stand in is loaded).
PYTHONSAFEPATH=1 PYTHONPATH="$CODE" exec /usr/bin/python3 -m waypoint "\$@"
EOS
chmod 755 "$BIN/waypoint"
install -Dm644 data/io.github.waypoint.Waypoint.desktop "$PREFIX/applications/io.github.waypoint.Waypoint.desktop"
sed -i "s|^Exec=waypoint|Exec=$BIN/waypoint|" "$PREFIX/applications/io.github.waypoint.Waypoint.desktop"
install -Dm644 waypoint/icons/hicolor/scalable/apps/io.github.waypoint.Waypoint.svg \
  "$PREFIX/icons/hicolor/scalable/apps/io.github.waypoint.Waypoint.svg"
install -Dm644 waypoint/icons/hicolor/symbolic/apps/io.github.waypoint.Waypoint-symbolic.svg \
  "$PREFIX/icons/hicolor/symbolic/apps/io.github.waypoint.Waypoint-symbolic.svg"
gtk-update-icon-cache -q -t "$PREFIX/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$PREFIX/applications" 2>/dev/null || true
# Point an existing timer and autostart entry at the new installation.
if [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/waypoint-auto.timer" ]; then
  sed -i "s|^ExecStart=.* auto$|ExecStart=$BIN/waypoint auto|" "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/waypoint-auto.service"
  systemctl --user daemon-reload || true
fi
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/io.github.waypoint.Waypoint-tray.desktop"
[ -f "$AUTOSTART" ] && sed -i "s|^Exec=.* tray$|Exec=$BIN/waypoint tray|" "$AUTOSTART"
echo "Waypoint is installed. Start it from the app overview or with \"waypoint\"."
echo "Backups need restic: sudo zypper install restic (or dnf/apt)."
