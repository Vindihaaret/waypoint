"""Overgang fra det gamle navnet Flytt: flytter innstillinger, status, erstattede filer, passordet i
nøkkelringen, timeren og autostart til Waypoint. Kjøres én gang; et merke i innstillingsmappen hindrer
at det gjøres på nytt."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import settings, system

OLD_SCHEMA = "io.github.flytt.Flytt"


def _home(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def _move(old: Path, new: Path):
    if old.exists() and not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))


def run():
    marker = settings.config_dir() / ".migrated-from-flytt"
    if marker.exists():
        return
    config, state, data = _home("XDG_CONFIG_HOME", ".config"), _home("XDG_STATE_HOME", ".local/state"), \
        _home("XDG_DATA_HOME", ".local/share")
    if not any(p.exists() for p in (config / "flytt", state / "flytt", data / "flytt",
                                    config / "systemd/user/flytt-auto.timer",
                                    config / "autostart/io.github.flytt.Flytt-tray.desktop")):
        settings.config_dir().mkdir(parents=True, exist_ok=True)
        marker.touch()
        return
    _move(config / "flytt", settings.config_dir())
    _move(state / "flytt", settings.state_dir())
    _move(data / "flytt/erstattet", data / "waypoint/replaced")
    for d in (data / "flytt",):
        try:
            d.rmdir()
        except OSError:
            pass

    # Passordet i nøkkelringen
    Secret, _schema = settings._secret()
    if Secret and not settings.get_password():
        try:
            old = Secret.Schema.new(OLD_SCHEMA, Secret.SchemaFlags.NONE, {"purpose": Secret.SchemaAttributeType.STRING})
            pw = Secret.password_lookup_sync(old, {"purpose": "backup"}, None)
            if pw and settings.store_password(pw):
                Secret.password_clear_sync(old, {"purpose": "backup"}, None)
        except Exception:  # noqa: BLE001 – låst nøkkelring; brukeren kan angi passordet på nytt
            pass

    # Timer og autostart med det gamle navnet
    old_timer = config / "systemd/user/flytt-auto.timer"
    if old_timer.exists():
        system.run(["systemctl", "--user", "disable", "--now", "flytt-auto.timer"])
        old_timer.unlink(missing_ok=True)
        (config / "systemd/user/flytt-auto.service").unlink(missing_ok=True)
        system.run(["systemctl", "--user", "daemon-reload"])
    (config / "autostart/io.github.flytt.Flytt-tray.desktop").unlink(missing_ok=True)
    from . import auto, tray
    settings.migrate_jobs()
    auto.sync_timer()
    if settings.get("tray"):
        tray.sync_autostart()
    settings.config_dir().mkdir(parents=True, exist_ok=True)
    marker.touch()
