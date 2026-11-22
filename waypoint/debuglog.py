"""Feilsøkingslogg og logg for gjenoppretting.

Med «Feilsøkingslogg» slått på (Innstillinger → Generelt, settings["debug"]) skriver alle Waypoint-prosessene –
vinduet, statusfeltet og de automatiske kopiene – det de gjør til ~/.local/state/waypoint/logs/waypoint-<dato>.log:
kommandoene som kjøres (restic, flatpak, gio, pkexec …) med utdata, meldingene fra kopier og gjenoppretting, og
uventede feil med full sporing. Hver gjenoppretting skrives i tillegg alltid til en egen restore-<tid>.log.

Passord skrives aldri: kjente hemmeligheter (passordet til lagrene, passord som skrives inn) byttes ut før en linje
skrives, det samme gjør passord i adresser og «password=…». Mappen er bare lesbar for brukeren, filene slettes
etter KEEP_DAYS dager, og en dagsfil stopper ved MAX_BYTES.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

KEEP_DAYS = 14
MAX_BYTES = 20 * 10**6
MASK = "••••"

_logger = logging.getLogger("waypoint")
_secrets: set[str] = set()
_role = "waypoint"
_ready = False


def log_dir() -> Path:
    from . import settings
    return settings.state_dir() / "logs"


def _open_dir() -> Path:
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def add_secret(value: str | None):
    """Et passord eller lignende som aldri skal stå i en logg."""
    if value and len(value) >= 3:
        _secrets.add(value)


def redact(text: str) -> str:
    for s in sorted(_secrets, key=len, reverse=True):
        text = text.replace(s, MASK)
    text = re.sub(r"(://[^/\s:@]+:)[^@\s/]+@", r"\1" + MASK + "@", text)          # bruker:passord@vert
    text = re.sub(r"(?i)\b(password|passord|passphrase|passwd)(\s*[=:]\s*)\S+", r"\1\2" + MASK, text)
    return text


class _Handler(logging.Handler):
    """Skriver til dagsfilen når feilsøkingsmodus er på. Innstillingen sjekkes høyst hvert 5. sekund, så den kan slås
    av og på mens prosesser kjører (statusfeltet går i dagevis)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self._checked = -1e9
        self._on = False
        self._mutex = threading.Lock()

    def enabled(self) -> bool:
        now = time.monotonic()
        if now - self._checked > 5:
            self._checked = now
            try:
                from . import settings
                self._on = bool(settings.get("debug"))
            except Exception:  # noqa: BLE001 – loggen skal aldri stoppe Waypoint
                self._on = False
        return self._on

    def emit(self, record):
        if not self.enabled():
            return
        try:
            line = redact(self.format(record))
            with self._mutex:
                f = _open_dir() / f"waypoint-{datetime.now():%Y-%m-%d}.log"
                if f.exists() and f.stat().st_size > MAX_BYTES:
                    return
                fd = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass


def refresh():
    """Les innstillingen på nytt med en gang (når den nettopp er endret i denne prosessen)."""
    for h in _logger.handlers:
        if isinstance(h, _Handler):
            h._checked = -1e9


def enabled() -> bool:
    for h in _logger.handlers:
        if isinstance(h, _Handler):
            return h.enabled()
    return False


def setup(role: str):
    """Kalles én gang når en prosess starter (cli.main)."""
    global _role, _ready
    _role = role
    if _ready:
        return
    _ready = True
    h = _Handler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(process)d " + role + "] %(levelname)s %(message)s"))
    _logger.addHandler(h)
    _logger.setLevel(logging.DEBUG)
    _logger.propagate = False
    add_secret(os.environ.get("WAYPOINT_PASSWORD"))

    old_hook = sys.excepthook

    def hook(kind, value, tb):
        _logger.error("Unexpected error", exc_info=(kind, value, tb))
        old_hook(kind, value, tb)
    sys.excepthook = hook
    old_thread_hook = threading.excepthook

    def thread_hook(args):
        _logger.error("Unexpected error in thread %s", args.thread.name if args.thread else "?",
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        old_thread_hook(args)
    threading.excepthook = thread_hook
    if h.enabled():
        from . import __version__
        _logger.info("Waypoint %s starts (%s), Python %s, argv %s", __version__, role, sys.version.split()[0],
                     sys.argv[1:])
        cleanup()


def cleanup():
    """Sletter logger som er eldre enn KEEP_DAYS dager."""
    limit = time.time() - KEEP_DAYS * 86400
    try:
        for f in log_dir().glob("*.log"):
            if f.stat().st_mtime < limit:
                f.unlink()
    except OSError:
        pass


def debug(msg: str, *args):
    _logger.debug(msg, *args)


def error(msg: str, *args, exc: bool = False):
    _logger.error(msg, *args, exc_info=exc)


def _tail(text: str, lines: int = 40) -> str:
    parts = [p if len(p) <= 500 else p[:500] + " …" for p in (text or "").rstrip().splitlines()]
    if len(parts) > lines:
        parts = [f"… ({len(parts) - lines} lines left out)"] + parts[-lines:]
    return "\n    ".join(parts)


def command(cmd: list[str], code: int | None = None, out: str = "", err: str = ""):
    """Én kjørt kommando med utfallet (utdata kortes ned til slutten)."""
    if not enabled():
        return
    text = "$ " + " ".join(str(c) for c in cmd)
    if code is not None:
        text += f"  → exit {code}"
    if out and out.strip():
        text += "\n  stdout:\n    " + _tail(out)
    if err and err.strip():
        text += "\n  stderr:\n    " + _tail(err)
    _logger.debug(text)


def tee(log, prefix: str = ""):
    """Gir en log-funksjon som også skriver til feilsøkingsloggen."""
    def both(text):
        if text:
            _logger.info("%s%s", prefix, text)
        log(text)
    return both


class RestoreLog:
    """Hele loggen fra én gjenoppretting, alltid (uavhengig av feilsøkingsmodus)."""

    def __init__(self):
        self.path: Path | None = None
        self._mutex = threading.Lock()
        try:
            self.path = _open_dir() / f"restore-{datetime.now():%Y-%m-%d_%H%M%S}.log"
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            self._f = os.fdopen(fd, "a", encoding="utf-8")
            from . import __version__
            self.write(f"Waypoint {__version__} · restore started {datetime.now().isoformat(timespec='seconds')}")
        except OSError:
            self.path, self._f = None, None

    def write(self, text: str):
        if not self._f or not text:
            return
        with self._mutex:
            try:
                self._f.write(redact(text) + "\n")
                self._f.flush()
            except (OSError, ValueError):
                pass

    def close(self):
        if self._f:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None
