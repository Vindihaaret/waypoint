"""Sikkerhetskopioppgavene, automatisk som Time Machine.

En systemd-timer for brukeren kjører «waypoint auto» hvert 15. minutt. Hver automatiske oppgave tas når det har
gått lenge nok siden dens forrige vellykkede kopi, lagringsstedet er tilgjengelig og reglene tillater det (ikke på
batteri, ikke på målt nettverk for en NAS). Oppgavene kjøres etter hverandre under én lås; «Ta kopi nå» mens noe
kjører, setter oppgaven i kø. Bare nye og endrede filer lagres. Gamle tidspunkter tynnes ut per oppgave: alt fra
siste døgn, ett per dag den siste måneden og ett per uke før det. Blir disken full, slettes de eldste. Kopiene
kontrolleres med jevne mellomrom (under samme lås).
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import backup, repo as repo_mod, settings, system
from .i18n import _
from .model import apply_job

INTERVALS = {"hourly": 3600, "daily": 86400, "weekly": 7 * 86400}
UNIT = "waypoint-auto"


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user"


def command() -> list[str]:
    """Kommandoen som starter Waypoint, både installert og fra kildekoden."""
    exe = shutil.which("waypoint")
    if exe:
        return [exe]
    root = str(Path(__file__).resolve().parent.parent)
    return ["/usr/bin/env", f"PYTHONPATH={root}", sys.executable, "-m", "waypoint"]


def install_timer(enabled: bool) -> bool:
    """Skriver og starter (eller stopper) systemd-timeren."""
    d = unit_dir()
    if enabled:
        d.mkdir(parents=True, exist_ok=True)
        cmd = " ".join(f'"{c}"' if " " in c else c for c in command())
        (d / f"{UNIT}.service").write_text(
            "[Unit]\nDescription=Waypoint: automatic backup\n\n"
            f"[Service]\nType=oneshot\nExecStart={cmd} auto\nNice=10\nIOSchedulingClass=idle\n")
        (d / f"{UNIT}.timer").write_text(
            "[Unit]\nDescription=Waypoint: check whether a backup is due\n\n"
            "[Timer]\nOnStartupSec=5min\nOnUnitActiveSec=15min\nRandomizedDelaySec=60\n\n"
            "[Install]\nWantedBy=timers.target\n")
        system.run(["systemctl", "--user", "daemon-reload"])
        return system.run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"])[0] == 0
    system.run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"])
    for suffix in ("service", "timer"):
        (d / f"{UNIT}.{suffix}").unlink(missing_ok=True)
    system.run(["systemctl", "--user", "daemon-reload"])
    return True


def old_host_folder(dest: Path) -> Path:
    """Der automatiske enkeltfil-kopier lå da programmet het Flytt."""
    return dest / "Flytt" / repo_mod.host()


# ---------------------------------------------------------------- regler

def on_battery() -> bool:
    """True hvis maskinen har batteri og ikke er koblet til strøm."""
    base = Path("/sys/class/power_supply")
    mains, battery = [], False
    try:
        for d in base.iterdir():
            kind = (d / "type").read_text().strip()
            if kind == "Mains":
                mains.append((d / "online").read_text().strip() == "1")
            elif kind == "Battery":
                battery = True
    except OSError:
        return False
    return battery and bool(mains) and not any(mains)


def metered() -> bool:
    """True hvis NetworkManager mener forbindelsen er målt (mobilnett, delt fra telefon …)."""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        r = bus.call_sync("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager",
                          "org.freedesktop.DBus.Properties", "Get",
                          GLib.Variant("(ss)", ("org.freedesktop.NetworkManager", "Metered")),
                          None, Gio.DBusCallFlags.NONE, 2000, None)
        return r.unpack()[0] in (1, 3)
    except Exception:  # noqa: BLE001 – uten NetworkManager antar vi at nettet ikke er målt
        return False


# ---------------------------------------------------------------- tid

def _ago(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return time.time() - datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def interval(job: dict) -> int:
    return INTERVALS.get(job.get("interval"), 86400)


def due(job: dict, status: dict) -> bool:
    age = _ago(status.get("last_success"))
    return age is None or age >= interval(job) - 300


def next_time(job: dict, status: dict) -> datetime | None:
    if not job.get("enabled"):
        return None
    last = status.get("last_success")
    if not last:
        return datetime.now()
    try:
        return datetime.fromisoformat(last).replace(tzinfo=None) + timedelta(seconds=interval(job))
    except ValueError:
        return None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sync_timer() -> bool:
    """Timeren skal kjøre så lenge minst én oppgave er automatisk."""
    return install_timer(any(j.get("enabled") for j in settings.jobs()))


def notify(title: str, body: str):
    """Skrivebordsvarsel uten GTK (fungerer fra systemd-tjenesten)."""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                      "org.freedesktop.Notifications", "Notify",
                      GLib.Variant("(susssasa{sv}i)", ("Waypoint", 0, "io.github.waypoint.Waypoint", title, body, [],
                                                        {}, -1)),
                      None, Gio.DBusCallFlags.NONE, 5000, None)
    except Exception:  # noqa: BLE001 – varsler er ikke viktige nok til å stoppe noe
        pass


def notify_once(key: str, title: str, body: str, every: float = 86400):
    """Som notify, men samme varsel (nøkkel) høyst én gang per «every» sekunder – timeren kjører hvert 15. minutt."""
    sent = settings.read_status().get("notified") or {}
    if (_ago(sent.get(key)) or 1e12) < every:
        return
    sent = {k: v for k, v in sent.items() if (_ago(v) or 1e12) < every}  # rydd bort gamle
    sent[key] = now_iso()
    settings.update_status(notified=sent)
    notify(title, body)


def warn_if_overdue(cfg: dict, job: dict, status: dict):
    """Varsler (høyst én gang i døgnet per oppgave) når det er for lenge siden siste vellykkede kopi."""
    days = int(cfg.get("warn_days") or 0)
    if not days or not job.get("enabled"):
        return
    age = _ago(status.get("last_success")) or _ago(status.get("enabled_since"))
    if age is None or age < days * 86400:
        return
    notify_once("overdue:" + job["id"], _("Ingen sikkerhetskopi på {} dager").format(int(age // 86400)),
                _("«{}»: sjekk at lagringsstedet er tilgjengelig. Waypoint prøver igjen automatisk.")
                .format(job.get("name", "")))


# ---------------------------------------------------------------- kjøring

class _Lock:
    def __init__(self):
        settings.state_dir().mkdir(parents=True, exist_ok=True)
        self.f = open(settings.state_dir() / "auto.lock", "w")

    def acquire(self, wait: bool = False) -> bool:
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            return True
        except OSError:
            return False

    def close(self):
        self.f.close()


def run(force: bool = False, log=print, job_id: str | None = None, interactive: bool = False) -> int:
    """Kjøres av timeren (alle automatiske oppgaver som er på tide) og av «Ta sikkerhetskopi nå» (force; én
    oppgave med job_id, ellers alle). Returnerer 0 når alt er i orden."""
    try:
        return _run(force, log, job_id, interactive)
    finally:
        if not force:  # timeren: se også etter nye versjoner av Waypoint (høyst én gang i døgnet)
            try:
                from . import updater
                updater.maybe_update(log)
            except Exception as e:  # noqa: BLE001 – oppdateringer skal aldri stoppe sikkerhetskopiene
                log(_("Feil: {}").format(e))


def _queued_jobs() -> list[dict]:
    queued = settings.queued_job_ids()
    return [j for j in settings.jobs() if j["id"] in queued]


def _run(force: bool, log, job_id: str | None, interactive: bool = False, only_queued: bool = False) -> int:
    settings.migrate_jobs()
    cfg = settings.load()
    if only_queued:
        jobs = _queued_jobs()
    else:
        jobs = [settings.job(job_id)] if job_id else settings.jobs()
        jobs = [j for j in jobs if j and (force or j.get("enabled"))]
    if not jobs:
        return 0
    # Oppgavene kjøres etter hverandre: én lås for alle, så to kopier aldri konkurrerer om disk og nett.
    lock = _Lock()
    if not lock.acquire():
        log(_("En sikkerhetskopi pågår allerede."))
        for j in jobs:  # «Ta kopi nå» mens en annen kopi kjører: merk at denne står i kø
            if force and settings.job_status(j["id"]).get("state") != "running":
                # Hver køet oppgave husker selv om den ble startet fra vinduet (da ingen «Vis fremdrift»-varsel).
                settings.update_job_status(j["id"], queued=True, queued_interactive=interactive)
        return 0
    result = 0
    try:
        if not force and not only_queued:
            # «For lenge siden siste kopi» uansett hvorfor (ikke på tide, venter på strøm/nett, utilgjengelig).
            # notify_once begrenser det til ett varsel i døgnet per oppgave.
            for job in jobs:
                warn_if_overdue(cfg, job, settings.job_status(job["id"]))
        while jobs:
            for job in jobs:
                st = settings.job_status(job["id"])
                queued = job["id"] in settings.queued_job_ids()
                if not force and not queued and not due(job, st):
                    maybe_check(cfg, job, st, log)
                    continue
                inter = bool(st.get("queued_interactive")) if queued else interactive
                result |= _run_job(cfg, job, log, force or queued, inter)
            # Ble noe lagt i kø mens vi kjørte, tas det med nå.
            jobs = _queued_jobs()
    finally:
        lock.close()
    # En oppgave kan ha blitt lagt i kø i øyeblikket mellom siste sjekk og at låsen ble sluppet; ta (bare) den nå.
    if _queued_jobs():
        result |= _run(False, log, None, only_queued=True)
    return result


def _run_job(cfg: dict, job: dict, log, force: bool, interactive: bool = False) -> int:
    jid = job["id"]
    settings.update_job_status(jid, queued=False, queued_interactive=None, last_attempt=now_iso())
    repo = repo_mod.Repo.for_job(job)
    if not repo:
        settings.update_job_status(jid, state="error", last_error=_("Lagringssted er ikke valgt"), progress=None)
        return 1
    if not force:
        if cfg.get("skip_on_battery") and on_battery():
            settings.update_job_status(jid, state="waiting_power", progress=None)
            return 0
        if cfg.get("skip_on_metered") and repo.remote and metered():
            settings.update_job_status(jid, state="waiting_network", progress=None)
            return 0
    return _backup(cfg, job, repo, log, interactive)


def _backup(cfg: dict, job: dict, repo: repo_mod.Repo, log, interactive: bool = False) -> int:
    jid = job["id"]

    def status(**kw):
        settings.update_job_status(jid, **kw)
    if not repo_mod.restic_path():
        status(state="error", last_error=_("restic er ikke installert"), progress=None)
        return 1
    if not repo.password:
        status(state="error", progress=None, last_error=_("Passordet for sikkerhetskopiene mangler i nøkkelringen."))
        notify_once("no-password", _("Automatisk sikkerhetskopi stoppet"),
                    _("Angi passordet for sikkerhetskopiene i Waypoint."))
        return 1
    if not repo.available():
        hint = _("Kunne ikke koble til nettverksmappen. Åpne den i Filer én gang og velg «Husk passordet for "
                 "alltid».") if repo.network_folder else None
        status(state="unavailable", progress=None, hint=hint)
        log(_("Lagringsstedet er ikke tilgjengelig. Prøver igjen senere."))
        warn_if_overdue(cfg, job, settings.job_status(jid))
        return 0
    try:
        # Lageret opprettes bare fra dialogen for lagringssted. Finnes det ikke her, er det nesten alltid fordi
        # disken ikke er koblet til og bare en tom monteringsmappe står igjen – da skal ingenting skrives dit.
        if not repo.exists():
            status(state="unavailable", progress=None,
                   hint=_("Fant ikke sikkerhetskopiene på lagringsstedet. Er riktig disk koblet til?"))
            log(_("Lagringsstedet er ikke tilgjengelig. Prøver igjen senere."))
            return 0
    except repo_mod.RepoError as e:
        if repo.remote:  # nettverk eller NAS nede: prøv igjen senere
            status(state="unavailable", progress=None, hint=str(e))
            return 0
        status(state="error", last_error=str(e), progress=None)
        notify_once("error:" + str(e)[:200], _("Automatisk sikkerhetskopi feilet"), str(e))
        return 1
    # interactive: startet fra vinduet, som viser fremdriften selv – da sender statusfeltet ikke varsel.
    status(state="running", progress=0.0, last_error=None, hint=None, current=None, pid=os.getpid(),
           boot=settings.boot_id(), started=now_iso(), interactive=interactive)
    try:
        repo.unlock()  # fjerner gamle låser etter en avbrutt kjøring
        adopt_untagged(job, repo, log)
        status(progress=0.01)
        sections = backup.scan(personal=True, measure=False)
        apply_job(sections, job)
        setup_dir = settings.state_dir() / f"setup-{jid}"
        paths, _manifest = backup.prepare_snapshot(sections, setup_dir)
        # Aldri ta kopi av lagrene selv (et lager på en disk som også er en kilde, f.eks. ~/Backup).
        extra_paths = []
        for r in repo_mod.Repo.all_storages():
            root = r.local_root(mount=False)
            if root:
                extra_paths.append(str(root / repo_mod.REPO_DIR))
        excl = settings.state_dir() / f"excludes-{jid}.txt"
        excl.write_text("\n".join(backup.exclude_patterns(cfg.get("excludes") or [], extra_paths)) + "\n")
        extra = ["--exclude-larger-than", cfg["exclude_larger"]] if cfg.get("exclude_larger") else []
        last = [0.0]

        def progress(frac, current):
            if frac - last[0] >= 0.01 or frac >= 1:
                last[0] = frac
                try:  # fremdriften er bare til visning; den skal aldri stoppe kopien
                    status(progress=round(max(frac, 0.01), 3), current=current[-160:] if current else None)
                except OSError:
                    pass
        summary = _with_space_retry(repo, lambda: repo.backup(paths, str(excl), extra, progress, lambda: False,
                                                              job_id=jid), log, jid)
        prune = (_ago(settings.job_status(jid).get("last_prune")) or 1e9) > 6 * 86400
        status(progress=1.0, current=_("Rydder gamle tidspunkter …"))
        repo.forget_timeline(prune=prune, job_id=jid)
        changes = {"state": "ok", "last_success": now_iso(), "progress": None, "current": None,
                   "last_snapshot": summary.get("snapshot_id"), "last_added": summary.get("data_added"),
                   "last_files": summary.get("total_files_processed"),
                   "last_incomplete": bool(summary.get("incomplete"))}
        if prune:
            changes["last_prune"] = now_iso()
        status(**changes)
        log(_("Sikkerhetskopi lagret ({} nye data)").format(_fmt(summary.get("data_added") or 0)))
        maybe_check(cfg, job, settings.job_status(jid), log)
        return 0
    except Exception as e:  # noqa: BLE001
        status(state="error", last_error=str(e), progress=None, current=None)
        notify_once("error:" + str(e)[:200], _("Automatisk sikkerhetskopi feilet"), str(e))
        log(_("Feil: {}").format(e))
        return 1


def adopt_untagged(job: dict, repo: repo_mod.Repo, log):
    """Tidspunkter fra før oppgavene fantes har ingen job=-merkelapp, og ville da aldri blitt tynnet ut. Er denne
    oppgaven alene om lagringsstedet, får de dens merkelapp – da tynnes de som resten, og neste kopi finner en
    forelder (så alle filene ikke leses på nytt)."""
    key = (job.get("storage") or {}).get("uri") or (job.get("storage") or {}).get("path")
    sharing = [j for j in settings.jobs() if j["id"] != job["id"]
               and ((j.get("storage") or {}).get("uri") or (j.get("storage") or {}).get("path")) == key]
    if sharing:
        return
    untagged = [s["id"] for s in repo.snapshots(only_host=True) if repo_mod.snapshot_job(s) is None]
    if untagged:
        repo.add_tag(untagged, "job=" + job["id"])
        log(_("{} eldre tidspunkter er knyttet til oppgaven").format(len(untagged)))


SPACE_RETRIES = 5


def space_candidates(snaps: list[dict], job_id: str | None, known_jobs: set[str]) -> list[dict]:
    """Tidspunkter som kan slettes når disken er full, eldste først: denne oppgavens (unntatt det nyeste), og
    foreldreløse – uten oppgave (fra før oppgavene fantes) eller fra en oppgave som er slettet."""
    def job_of(s):
        return repo_mod.snapshot_job(s)
    own = sorted((s for s in snaps if job_of(s) == job_id), key=lambda s: s.get("time", ""))[:-1]
    orphans = [s for s in snaps if job_of(s) is None or job_of(s) not in known_jobs]
    return sorted(own + orphans, key=lambda s: s.get("time", ""))


def _with_space_retry(repo: repo_mod.Repo, do, log, job_id: str | None = None):
    """Kjører do(). Er en lokal disk full, slettes det eldste tidspunktet og kopien prøves igjen – ett om gangen,
    høyst SPACE_RETRIES ganger, og det nyeste tidspunktet beholdes alltid."""
    for attempt in range(SPACE_RETRIES + 1):
        try:
            return do()
        except repo_mod.RepoError as e:
            if not e.no_space or repo.remote or attempt == SPACE_RETRIES:
                raise
            candidates = space_candidates(repo.snapshots(only_host=True), job_id, {j["id"] for j in settings.jobs()})
            if not candidates:
                raise
            oldest = candidates[0]
            log(_("Lite plass – sletter det eldste tidspunktet {}").format(oldest.get("time", "")[:16]))
            repo.unlock()
            repo.forget_ids([oldest["id"]], prune=True)


def maybe_check(cfg: dict, job: dict, status: dict, log=print, force: bool = False) -> bool | None:
    """Kontrollerer kopiene til en oppgave hvis det er på tide. Returnerer resultatet, eller None hvis ikke kjørt."""
    days = int(cfg.get("check_days") or 0)
    if not force and (not days or (_ago(status.get("last_check")) or 1e12) < days * 86400):
        return None
    jid = job["id"]
    repo = repo_mod.Repo.for_job(job)
    reason = None
    if not repo:
        reason = _("Lagringssted er ikke valgt")
    elif not repo.password:
        reason = _("Passordet for sikkerhetskopiene mangler")
    elif not repo_mod.restic_path():
        reason = _("restic er ikke installert")
    elif not repo.available():
        reason = _("Lagringsstedet er ikke tilgjengelig")
    if reason:
        if force:  # «Kontroller nå» ble trykket: si hvorfor det ikke ble noe av, og lås opp knappen
            settings.update_job_status(jid, checking=False, check_pid=None, check_skipped=reason)
            log(reason)
        return None
    settings.update_job_status(jid, checking=True, check_pid=os.getpid(), check_boot=settings.boot_id(),
                               check_skipped=None)
    try:
        ok, text = repo.check("2%")
    except Exception as e:  # noqa: BLE001
        ok, text = False, str(e)
    settings.update_job_status(jid, checking=False, check_pid=None, last_check=now_iso(), check_ok=ok,
                               check_text=text)
    if not ok:
        notify_once("check:" + text[:200], _("Kontrollen av sikkerhetskopiene fant feil"),
                    "«{}»: {}".format(job.get("name", ""), text))
    log(_("Kontroll: {}").format(_("i orden") if ok else text))
    return ok


def check_all(log=print) -> bool:
    """«Kontroller nå» for alle oppgaver (kommandoen «waypoint check»). Tar samme lås som kopiene og venter på en
    kopi som kjører, så restic check og restic backup aldri kjører samtidig mot samme lager."""
    lock = _Lock()
    lock.acquire(wait=True)
    try:
        cfg, ok = settings.load(), True
        for job in settings.jobs():
            res = maybe_check(cfg, job, settings.job_status(job["id"]), log, force=True)
            ok = ok and res is not False
        return ok
    finally:
        lock.close()


def _fmt(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return ""


def start_now(job_id: str | None = None, interactive: bool = False):
    """Starter en sikkerhetskopi i bakgrunnen: én oppgave, eller alle (fra statusfeltet, vinduet eller innstillingene).
    Kjører det allerede en kopi, settes oppgaven i kø og tas når den første er ferdig."""
    args = [*command(), "auto", "--now"] + (["--job", job_id] if job_id else []) + \
        (["--interactive"] if interactive else [])
    subprocess.Popen(args, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_check() -> int:
    """Starter en kontroll av alle oppgaver i bakgrunnen og returnerer prosess-id-en."""
    p = subprocess.Popen([*command(), "check"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.pid
