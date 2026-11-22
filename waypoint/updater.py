"""Se etter, hent og installer nye versjoner av Waypoint fra GitHub.

Sjekken bruker GitHubs offentlige API uten innlogging, og ingen data om brukeren sendes. Waypoint er installert
i hjemmemappen (~/.local/share/waypoint/app), så install.sh fra den nedlastede versjonen kjøres uten
administratorpassord. Oppdatering fra appen gjelder bare en installert Waypoint, ikke en kopi av kildekoden.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import __version__, settings

REPO = "Vindihaaret/waypoint"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
TARBALL = f"https://github.com/{REPO}/archive/refs/tags/{{tag}}.tar.gz"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
CHECK_INTERVAL = 24 * 3600


class UpdateError(Exception):
    pass


def parse_version(v: str | None):
    """«v1.2.3» / «1.2» → (1, 2, 3). Ugyldig → None."""
    m = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", (v or "").strip())
    return tuple(int(x or 0) for x in m.groups()) if m else None


def is_newer(tag: str, current: str = __version__) -> bool:
    new, cur = parse_version(tag), parse_version(current)
    return bool(new and cur and new > cur)


def install_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "waypoint" / "app"


def is_installed() -> bool:
    """True når denne koden kjører fra installasjonen i hjemmemappen (ikke fra en kildekodemappe)."""
    here = Path(__file__).resolve().parent
    return str(here).startswith(str(install_dir().resolve()) + os.sep)


def fetch_latest(timeout: int = 15) -> dict | None:
    """Siste utgivelse som {"tag", "version", "name", "notes", "url"}, eller None hvis det ikke finnes noen ennå."""
    req = urllib.request.Request(API_LATEST, headers={"Accept": "application/vnd.github+json",
                                                      "User-Agent": f"waypoint/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # ingen utgivelser publisert ennå
            return None
        raise UpdateError(str(e)) from e
    except (OSError, ValueError) as e:
        raise UpdateError(str(e)) from e
    tag = data.get("tag_name") or ""
    if not parse_version(tag) or data.get("draft") or data.get("prerelease"):
        return None
    return {"tag": tag, "version": ".".join(map(str, parse_version(tag))), "name": data.get("name") or tag,
            "notes": (data.get("body") or "").strip(), "url": data.get("html_url") or RELEASES_PAGE}


def download(tag: str, timeout: int = 120) -> tuple[Path, Path]:
    """Laster ned og pakker ut kildekoden for en utgivelse. Returnerer (midlertidig mappe, mappe med install.sh)."""
    if not parse_version(tag):
        raise UpdateError(f"invalid tag: {tag!r}")
    base = Path(tempfile.mkdtemp(prefix="waypoint-update-"))
    archive = base / "source.tar.gz"
    req = urllib.request.Request(TARBALL.format(tag=tag), headers={"User-Agent": f"waypoint/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(archive, "wb") as f:
            shutil.copyfileobj(resp, f)
        return base, extract(archive, base / "src", tag)
    except BaseException:
        shutil.rmtree(base, ignore_errors=True)
        raise


def extract(archive: Path, target: Path, tag: str) -> Path:
    """Trygg utpakking (bare vanlige filer og mapper, ingen stier utenfor målmappen) og kontroll av at innholdet
    faktisk er Waypoint i riktig versjon."""
    target.mkdir(parents=True, exist_ok=True)
    real = os.path.realpath(target)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            path = os.path.realpath(os.path.join(target, m.name))
            if not path.startswith(real + os.sep):
                raise UpdateError(f"unsafe path in archive: {m.name}")
            if not (m.isfile() or m.isdir()):
                raise UpdateError(f"unexpected entry in archive: {m.name}")
        tar.extractall(target, members=members, filter="data")
    roots = [d for d in target.iterdir() if d.is_dir()]
    if len(roots) != 1:
        raise UpdateError("unexpected archive layout")
    src = roots[0]
    init = src / "waypoint" / "__init__.py"
    if not ((src / "install.sh").is_file() and init.is_file()):
        raise UpdateError("the download does not look like Waypoint")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"))
    if not m or parse_version(m.group(1)) != parse_version(tag):
        raise UpdateError(f"version mismatch in download ({m.group(1) if m else '?'} vs {tag})")
    return src


class Busy(UpdateError):
    """En sikkerhetskopi eller kontroll pågår; oppdateringen må vente."""


def busy() -> bool:
    """True når en sikkerhetskopi kjører (i en hvilken som helst prosess) eller noen holder låsen for kopiene.
    Da skal ikke koden i ~/.local/share/waypoint/app byttes ut: kopien henter moduler underveis og kunne fått en
    blanding av gammel og ny kode."""
    if settings.running_jobs():
        return True
    from . import auto
    lock = auto._Lock()
    try:
        return not lock.acquire()
    finally:
        lock.close()  # slipper låsen igjen med en gang


def install(tag: str, log=print) -> None:
    """Henter utgivelsen og kjører install.sh fra den (uten root, siden Waypoint ligger i hjemmemappen).
    Holder låsen for sikkerhetskopiene mens den kjører, så ingen kopi kan starte midt i oppdateringen."""
    if not is_installed():
        raise UpdateError("not an installed Waypoint")
    from . import auto
    from .i18n import _
    lock = auto._Lock()
    if settings.running_jobs() or not lock.acquire():
        lock.close()
        raise Busy(_("En sikkerhetskopi pågår – prøv igjen når den er ferdig"))
    try:
        _install(tag, log)
    finally:
        lock.close()


def _install(tag: str, log) -> None:
    base, src = download(tag)
    try:
        # Uten terminal og med WAYPOINT_SKIP_DEPS: install.sh skal ikke prøve å installere pakker med sudo her.
        p = subprocess.run(["bash", str(src / "install.sh")], cwd=src, capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL, env={**os.environ, "WAYPOINT_SKIP_DEPS": "1"})
        for line in (p.stdout + p.stderr).splitlines():
            log(line)
        if p.returncode != 0:
            raise UpdateError((p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout).strip()
                              else f"install.sh exited with code {p.returncode}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def restart_tray():
    """Starter statusfeltikonet på nytt, så det bruker den nye versjonen."""
    me = os.getpid()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == me:
            continue
        try:
            args = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(args) >= 4 and args[0].endswith(b"python3") and args[1:4] == [b"-m", b"waypoint", b"tray"]:
            try:
                os.kill(int(proc.name), signal.SIGTERM)
            except OSError:
                pass
    time.sleep(1)
    from . import tray
    tray.start()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ago(iso: str | None) -> float:
    try:
        return time.time() - datetime.fromisoformat(iso or "").timestamp()
    except ValueError:
        return 1e12


def check(log=print) -> dict | None:
    """Ser etter en ny versjon nå. Lagrer resultatet i status og returnerer utgivelsen hvis den er nyere."""
    info = fetch_latest()
    newer = info if info and is_newer(info["tag"]) else None
    settings.update_status(update_checked=_now(), update_available=newer["tag"] if newer else None,
                           update_error=None)
    return newer


def maybe_update(log=print, force: bool = False) -> str | None:
    """Kjøres jevnlig (timeren, statusfeltet, vinduet). Ser etter nye versjoner høyst én gang i døgnet, og
    installerer dem hvis brukeren har valgt det. Returnerer taggen som ble installert, ellers None."""
    cfg = settings.load()
    if not force and not cfg.get("update_check", True):
        return None
    st = settings.read_status()
    if not force and _ago(st.get("update_checked")) < CHECK_INTERVAL:
        return None
    try:
        newer = check(log)
    except UpdateError as e:
        settings.update_status(update_checked=_now(), update_error=str(e))
        return None
    if not newer:
        return None
    from . import auto
    from .i18n import _
    if cfg.get("update_auto") and is_installed() and not busy():
        try:
            install(newer["tag"], log)
        except Busy:
            pass  # en kopi startet akkurat nå: si fra om den nye versjonen i stedet (under)
        except (UpdateError, OSError, subprocess.SubprocessError) as e:
            settings.update_status(update_error=str(e))
            auto.notify_once("update-failed:" + newer["tag"], _("Oppdateringen av Waypoint feilet"), str(e))
            return None
        else:
            settings.update_status(update_available=None, update_installed=newer["tag"])
            auto.notify(_("Waypoint er oppdatert"), _("Ny versjon: {}").format(newer["version"]))
            restart_tray()
            return newer["tag"]
    auto.notify_once("update:" + newer["tag"], _("Ny versjon av Waypoint"),
                     _("Versjon {} er klar. Oppdater fra Innstillinger → Generelt.").format(newer["version"]),
                     every=1e12)
    return None
