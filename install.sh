#!/bin/bash
# Installs Waypoint for you (no sudo): ~/.local/share/waypoint/app, the "waypoint" command and an app icon.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN="$HOME/.local/bin"

# ---------------------------------------------------------------- dependencies
# Anything missing is installed with the system's package manager (asks for your password through sudo).
# Only when run from a terminal: the in-app update runs this script without one and skips this step.
gui_ok() {
  python3 - <<'PY' 2>/dev/null
import sys
assert sys.version_info >= (3, 10)
assert hasattr(__import__("tarfile"), "data_filter")  # tarfile filter=: Python 3.10.12+ or 3.11.4+
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Secret", "1")
from gi.repository import Gtk, Adw, Secret
assert (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 12)
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 6)
PY
}
have_gvfs() {  # a GVfs backend program, e.g. gvfsd-smb (its folder differs between distributions)
  local d
  for d in /usr/libexec/gvfs /usr/libexec /usr/lib/gvfs /usr/lib; do [ -x "$d/$1" ] && return 0; done
  return 1
}
missing() {  # what is missing, by name: gui restic gpg dconf gio smb sftp fuse
  gui_ok || echo gui
  command -v restic >/dev/null || echo restic
  command -v gpg >/dev/null || echo gpg
  command -v dconf >/dev/null || echo dconf
  command -v gio >/dev/null || echo gio
  have_gvfs gvfsd-smb || echo smb
  have_gvfs gvfsd-sftp || echo sftp
  have_gvfs gvfsd-fuse || echo fuse
}
packages() {  # package names for one missing item ($1) with this package manager ($2)
  case "$2:$1" in
    zypper:gui) echo python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 typelib-1_0-Secret-1 ;;
    zypper:gpg) echo gpg2 ;;
    zypper:gio) echo glib2-tools ;;
    zypper:smb) echo gvfs-backend-samba ;;
    zypper:sftp) echo gvfs-backends ;;
    zypper:fuse) echo gvfs-fuse ;;
    dnf:gui) echo python3-gobject gtk4 libadwaita libsecret ;;
    dnf:gpg) echo gnupg2 ;;
    dnf:gio) echo glib2 ;;
    dnf:smb) echo gvfs-smb ;;
    dnf:sftp) echo gvfs ;;
    dnf:fuse) echo gvfs-fuse ;;
    apt-get:gui) echo python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-secret-1 ;;
    apt-get:gpg) echo gnupg ;;
    apt-get:dconf) echo dconf-cli ;;
    apt-get:gio) echo libglib2.0-bin ;;
    apt-get:smb | apt-get:sftp) echo gvfs-backends ;;
    apt-get:fuse) echo gvfs-fuse ;;
    pacman:gui) echo python-gobject gtk4 libadwaita libsecret ;;
    pacman:gpg) echo gnupg ;;
    pacman:gio) echo glib2 ;;
    pacman:smb) echo gvfs-smb ;;
    pacman:sftp | pacman:fuse) echo gvfs ;;
    *) echo "$1" ;;  # restic and dconf have the same name everywhere else
  esac
}

MISSING=$(missing | xargs)
PKGS=""
if [ -n "$MISSING" ]; then
  PM=""
  for pm in zypper dnf apt-get pacman; do
    if command -v "$pm" >/dev/null; then PM=$pm; break; fi
  done
  if [ -n "$PM" ]; then
    PKGS=$(for m in $MISSING; do packages "$m" "$PM"; done | tr ' ' '\n' | sort -u | xargs)
  fi
  if [ -n "$PM" ] && [ -t 0 ] && [ -z "${WAYPOINT_SKIP_DEPS:-}" ]; then
    echo "==> Installing what Waypoint needs: $PKGS"
    case "$PM" in
      zypper) sudo zypper --non-interactive install $PKGS || true ;;
      dnf) sudo dnf install -y $PKGS || true ;;
      apt-get) sudo apt-get install -y $PKGS || true ;;
      pacman) sudo pacman -S --needed --noconfirm $PKGS || true ;;
    esac
    MISSING=$(missing | xargs)
  fi
fi
if [[ " $MISSING " == *" gui "* ]]; then
  echo "ERROR: Waypoint needs Python 3.10.12+ (or 3.11.4+), PyGObject, GTK 4.12+, libadwaita 1.6+ and libsecret."
  echo "       Install them with your package manager and run this script again."
  if [ -n "$PKGS" ]; then echo "       Packages: $PKGS"; fi
  exit 1
fi
if [ -n "$MISSING" ]; then
  echo "Note: still missing: $MISSING"
  case " $MISSING " in *" restic "*) echo "      Backups need restic." ;; esac
  case " $MISSING " in *" gpg "*) echo "      Encrypted export files need gpg." ;; esac
  case " $MISSING " in *" dconf "*) echo "      GNOME settings are only backed up with dconf." ;; esac
  case " $MISSING " in *" gio "* | *" smb "* | *" sftp "* | *" fuse "*)
    echo "      Storage on a NAS needs GVfs (gio, SMB/SFTP and FUSE)." ;; esac
  if [ -n "$PKGS" ]; then echo "      Packages to install: $PKGS"; fi
fi

# ---------------------------------------------------------------- root helper
# Packages are installed during a restore by a small helper that runs as root through pkexec. It must be owned
# by root outside your home folder, so no program running as you can replace it while waiting for the password.
HELPER=/usr/local/libexec/waypoint/waypoint-privileged
POLICY=/usr/share/polkit-1/actions/io.github.waypoint.Waypoint.policy
if ! cmp -s waypoint/privileged.py "$HELPER" || ! cmp -s data/io.github.waypoint.Waypoint.policy "$POLICY" \
    || [ "$(stat -c %u "$HELPER" 2>/dev/null)" != 0 ]; then
  if [ -t 0 ] && [ -z "${WAYPOINT_SKIP_DEPS:-}" ]; then
    echo "==> Installing the helper that installs packages during a restore"
    if ! { sudo install -Dm755 -o root -g root waypoint/privileged.py "$HELPER" \
           && sudo install -Dm644 -o root -g root data/io.github.waypoint.Waypoint.policy "$POLICY"; }; then
      echo "Note: the helper was not installed, so packages cannot be installed during a restore."
    fi
  else
    echo "Note: run ./install.sh from a terminal to install the helper that installs packages during a restore."
  fi
fi

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
install -m755 uninstall.sh "$CODE/uninstall.sh"   # used by "waypoint uninstall" and Settings → General
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
echo "Uninstall with: waypoint uninstall   (or Settings → General → Uninstall Waypoint)"
