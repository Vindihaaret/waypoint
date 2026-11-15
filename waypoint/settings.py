"""Brukerinnstillinger (~/.config/waypoint/settings.json), statusfil og passord i nøkkelringen."""

from __future__ import annotations

import json
import os
from pathlib import Path


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "waypoint"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "waypoint"


DEFAULTS = {
    "language": "auto",        # auto, nb, en, de, es, fr
    "theme": "system",         # system, light, dark
    "glow": True,              # svak blå glød i bakgrunnen (kan slås av for ekstra tydelighet)
    "tray": True,              # ikon i statusfeltet
    "autostart": True,         # start statusfeltikonet ved innlogging
    "update_check": True,      # se etter nye versjoner (én gang i døgnet)
    "update_auto": False,      # installer nye versjoner uten å spørre
    "encrypt_all": True,       # krypter hele eksportfilen
    # Sikkerhetskopioppgaver: [{"id", "name", "enabled", "interval": hourly|daily|weekly,
    #   "preset": all|files|setup, "choices": {gruppe: {element: valgt}},
    #   "storage": {"kind": "local", "path", "uri", "network": {protocol, server, user, domain}}}]
    "jobs": [],
    "notify_start": True,      # varsel med «Vis fremdrift» når en kopi starter i bakgrunnen
    "skip_on_battery": True,
    "skip_on_metered": True,   # gjelder bare NAS
    "warn_days": 7,            # varsle når det er så lenge siden siste vellykkede kopi
    "excludes": ["node_modules", "*.iso", "*.vdi", "*.vmdk", "*.qcow2", ".Trash-*"],
    "exclude_larger": "",      # f.eks. "4G"; tom = ingen grense
    "check_days": 30,          # kontroller kopiene så ofte; 0 = aldri
}
# Fra før oppgavene (0.5.0 og eldre): ett lagringssted med automatikk og valg. Gjøres om til én oppgave av
# migrate_jobs() og blir deretter ikke brukt.
LEGACY = ("storage", "auto_enabled", "auto_interval", "auto_dest", "choices")


def _path() -> Path:
    return config_dir() / "settings.json"


# ---------------------------------------------------------------- oppgaver

def jobs() -> list[dict]:
    return [j for j in (load().get("jobs") or []) if j.get("id")]


def job(job_id: str | None) -> dict | None:
    return next((j for j in jobs() if j["id"] == job_id), None)


def password_in_use() -> bool:
    """True når passordet i nøkkelringen er nøkkelen til minst ett lager (en oppgave med lagringssted)."""
    return bool(get_password()) and any(j.get("storage") for j in jobs())


def new_job_id() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


def save_job(job_dict: dict):
    """Legger til eller erstatter en oppgave (samme id)."""
    data = load()
    lst = [j for j in (data.get("jobs") or []) if j.get("id") != job_dict["id"]]
    old = [j.get("id") for j in (data.get("jobs") or [])]
    if job_dict["id"] in old:  # behold plassen i listen
        lst.insert(old.index(job_dict["id"]), job_dict)
    else:
        lst.append(job_dict)
    data["jobs"] = lst
    write_json(_path(), data)


def delete_job(job_id: str):
    data = load()
    data["jobs"] = [j for j in (data.get("jobs") or []) if j.get("id") != job_id]
    write_json(_path(), data)
    with _status_lock():
        s = _read_status_file()
        (s.get("jobs") or {}).pop(job_id, None)
        write_json(status_path(), s)
    import shutil
    shutil.rmtree(state_dir() / f"setup-{job_id}", ignore_errors=True)
    (state_dir() / f"excludes-{job_id}.txt").unlink(missing_ok=True)


def migrate_jobs():
    """Gjør det gamle oppsettet med ett lagringssted om til en oppgave (én gang)."""
    data = load()
    if data.get("jobs") or not data.get("storage"):
        return
    from .i18n import _
    data["jobs"] = [{"id": new_job_id(), "name": _("Sikkerhetskopi"), "enabled": bool(data.get("auto_enabled")),
                     "interval": data.get("auto_interval") or "daily", "preset": "all",
                     "choices": data.get("choices") or {}, "storage": data["storage"]}]
    data["storage"] = None
    data["auto_enabled"] = False
    write_json(_path(), data)
    with _status_lock():
        old = _read_status_file()
        keep = {k: v for k, v in old.items() if k.startswith(("update_", "notified"))}
        job_keys = {k: v for k, v in old.items() if k not in keep and k != "jobs"}
        write_json(status_path(), {**keep, "jobs": {data["jobs"][0]["id"]: job_keys}})


def load() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **data}


def get(key):
    return load().get(key, DEFAULTS.get(key))


def set(key, value):  # noqa: A001 – kort og tydelig her
    data = load()
    data[key] = value
    write_json(_path(), data)


def write_json(path: Path, data):
    """Atomisk skriving med et unikt midlertidig navn, så samtidige skrivere ikke tråkker på hverandre."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- status for automatisk kopi

def status_path() -> Path:
    return state_dir() / "status.json"


def _read_status_file() -> dict:
    try:
        return json.loads(status_path().read_text())
    except (OSError, ValueError):
        return {}


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def boot_id() -> str:
    """Endres ved hver omstart; brukes sammen med prosess-id, som kan gjenbrukes etter en omstart."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _gone(pid, boot) -> bool:
    return not _alive(pid) or bool(boot and boot != boot_id())


def runner_active() -> bool:
    """True hvis en prosess holder låsen for sikkerhetskopiene (en kopi kjører eller skal kjøre)."""
    import fcntl
    path = state_dir() / "auto.lock"
    if not path.exists():
        return False
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def _fix_stale(s: dict, runner: bool | None = None) -> dict:
    """En kopi eller kontroll som står som «pågår», men der prosessen er borte (krasj, utlogging, omstart), vises
    som avbrutt i stedet for å stå på «pågår» for alltid. En oppgave i kø uten noen som kjører, tas ut av køen."""
    if s.get("state") == "running" and s.get("pid") and _gone(s["pid"], s.get("boot")):
        from .i18n import _
        s.update(state="error", progress=None, last_error=_("Sikkerhetskopien ble avbrutt"))
    if s.get("checking") and s.get("check_pid") and _gone(s["check_pid"], s.get("check_boot")):
        s["checking"] = False
    if s.get("queued") and runner is False:
        s["queued"] = False
    return s


def read_status() -> dict:
    """Felles status (oppdateringer, varsler) og status per oppgave under «jobs»."""
    s = _read_status_file()
    jobs_status = s.get("jobs") or {}
    runner = runner_active() if any(js.get("queued") for js in jobs_status.values()) else None
    s["jobs"] = {jid: _fix_stale(dict(js), runner) for jid, js in jobs_status.items()}
    return s


def queued_job_ids() -> set[str]:
    """Oppgaver merket «i kø» slik det står i filen, uten oppryddingen i read_status (brukes av den som kjører
    kopiene, også i øyeblikket rett etter at låsen er sluppet)."""
    return {jid for jid, js in (_read_status_file().get("jobs") or {}).items() if js.get("queued")}


def job_status(job_id: str) -> dict:
    return read_status()["jobs"].get(job_id, {})


def running_jobs() -> list[str]:
    return [jid for jid, js in read_status()["jobs"].items() if js.get("state") == "running"]


def _status_lock():
    import contextlib
    import fcntl

    @contextlib.contextmanager
    def lock():
        state_dir().mkdir(parents=True, exist_ok=True)
        with open(state_dir() / "status.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
    return lock()


def update_status(**changes):
    """Felles status. Les-endre-skriv under en fillås: kopiene, vinduet og statusfeltet skriver samtidig."""
    with _status_lock():
        s = _read_status_file()
        s.update(changes)
        write_json(status_path(), s)


def update_job_status(job_id: str, **changes):
    """Status for én oppgave. En oppgave som er slettet (f.eks. mens kopien kjørte), får ingen ny status."""
    if job(job_id) is None:
        return
    with _status_lock():
        s = _read_status_file()
        jobs_status = s.setdefault("jobs", {})
        jobs_status.setdefault(job_id, {}).update(changes)
        write_json(status_path(), s)


# ---------------------------------------------------------------- nøkkelring

_SCHEMA = None


def _secret():
    global _SCHEMA
    try:
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
    except (ImportError, ValueError):
        return None, None
    if _SCHEMA is None:
        _SCHEMA = Secret.Schema.new("io.github.waypoint.Waypoint", Secret.SchemaFlags.NONE,
                                    {"purpose": Secret.SchemaAttributeType.STRING})
    return Secret, _SCHEMA


def get_secret(purpose: str) -> str | None:
    Secret, schema = _secret()
    if not Secret:
        return None
    try:
        return Secret.password_lookup_sync(schema, {"purpose": purpose}, None)
    except Exception:  # noqa: BLE001 – låst eller manglende nøkkelring
        return None


def store_secret(purpose: str, value: str, label: str = "Waypoint") -> bool:
    Secret, schema = _secret()
    if not Secret:
        return False
    try:
        return Secret.password_store_sync(schema, {"purpose": purpose}, Secret.COLLECTION_DEFAULT, label, value, None)
    except Exception:  # noqa: BLE001
        return False


def clear_secret(purpose: str):
    Secret, schema = _secret()
    if Secret:
        try:
            Secret.password_clear_sync(schema, {"purpose": purpose}, None)
        except Exception:  # noqa: BLE001
            pass


def get_password() -> str | None:
    """Passordet for sikkerhetskopiene (restic-lageret og krypterte eksportfiler)."""
    return os.environ.get("WAYPOINT_PASSWORD") or get_secret("backup")


def store_password(password: str) -> bool:
    return store_secret("backup", password, "Waypoint – passord for sikkerhetskopier")


def clear_password():
    clear_secret("backup")
