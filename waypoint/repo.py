"""Lagringsstedet for sikkerhetskopier: et restic-lager i mappen «Waypoint» på en disk, USB-disk eller NAS
(en NAS kobles til via GVfs, se network.py, og brukes som en vanlig mappe).

restic lagrer bare det som er nytt eller endret (deduplisering), krypterer alt med AES-256 og holder
hvert tidspunkt («snapshot») som en hel kopi. Waypoint kaller restic-programmet med --json og tolker
utdataene. Passordet ligger i nøkkelringen og sendes som miljøvariabel. Hvert tidspunkt har merkelappene
«waypoint», «home=<hjemmemappe>» og «job=<oppgave>».
"""

from __future__ import annotations

import json
import re
import os
import shutil
import socket
import subprocess
from pathlib import Path

from . import settings, system
from .i18n import _

TAG = "waypoint"
EXIT_INCOMPLETE = 3      # kopien ble laget, men noen filer kunne ikke leses
EXIT_NO_REPO = 10
EXIT_LOCKED = 11
EXIT_WRONG_PASSWORD = 12

GROUP_BY = "host,tags"
REPO_DIR = "Waypoint"  # lageret ligger i denne mappen på lagringsstedet


def glob_escape(path: str) -> str:
    """restic tolker --include/--exclude som mønstre; dette gjør en vanlig sti bokstavelig («Album [2020]»)."""
    return "".join("\\" + c if c in "\\*?[]" else c for c in path)


class RepoError(Exception):
    def __init__(self, message: str = "", details: str = ""):
        super().__init__(message)
        self.details = details or message

    @property
    def no_space(self) -> bool:
        return "no space left" in self.details.lower()


class WrongPassword(RepoError):
    pass


class NoRepo(RepoError):
    pass


def restic_path() -> str | None:
    return os.environ.get("WAYPOINT_RESTIC") or shutil.which("restic")


def host() -> str:
    return socket.gethostname().split(".")[0] or "linux"


def describe(cfg: dict | None) -> str:
    """Kort beskrivelse av lagringsstedet for visning: en mappe, eller «nas / deling / mappe» for en NAS."""
    if not cfg:
        return _("Ikke valgt")
    uri = cfg.get("uri") or ""
    if uri and not uri.startswith("file://"):
        from .network import display
        return display(uri)
    return cfg.get("path") or "?"


class Repo:
    """Et restic-lager i mappen «Waypoint» på en lokal disk, USB-disk eller en NAS koblet til via GVfs."""

    def __init__(self, cfg: dict, password: str | None):
        self.cfg = cfg or {}
        self.password = password
        self.proc: subprocess.Popen | None = None
        self.mount_error = ""

    @classmethod
    def for_job(cls, job: dict | None) -> "Repo | None":
        if not job or not job.get("storage"):
            return None
        return cls(job["storage"], settings.get_password())

    @classmethod
    def all_storages(cls) -> list["Repo"]:
        """Ett Repo per lagringssted som brukes av en oppgave (oppgaver som deler sted, gir ett)."""
        seen, out = set(), []
        for j in settings.jobs():
            st = j.get("storage") or {}
            key = st.get("uri") or st.get("path")
            if key and key not in seen:
                seen.add(key)
                out.append(cls(st, settings.get_password()))
        return out

    @property
    def remote(self) -> bool:
        """En NAS i nettverket (gjelder reglene for målt nett og håndtering av full disk)."""
        return self.network_folder

    # ------------------------------------------------------------ plassering og miljø

    def local_root(self, mount: bool = True) -> Path | None:
        """Mappen på disk for et lokalt lager (kobler til nettverksmapper med gio mount)."""
        path = Path(self.cfg["path"]) if self.cfg.get("path") else None
        if path and path.is_dir():
            return path
        uri = self.cfg.get("uri") or ""
        if mount and self.network_folder:
            # Kobler til delte mapper (smb://, sftp://, nfs:// …) med innloggingen som er lagret i nøkkelringen.
            # stdin er lukket, så gio gir opp i stedet for å vente på et passord ingen kan skrive.
            code, _out, err = system.run(["gio", "mount", uri], timeout=60)
            self.mount_error = err.strip() if code != 0 else ""
            if path and path.is_dir():
                return path
        return None

    @property
    def network_folder(self) -> bool:
        uri = self.cfg.get("uri") or ""
        return bool(uri) and not uri.startswith("file://")

    def location(self, mount: bool = True) -> str | None:
        root = self.local_root(mount)
        return str(root / REPO_DIR) if root else None

    def available(self) -> bool:
        return self.location() is not None

    def env(self) -> dict:
        loc = self.location()
        if not loc:
            raise RepoError(_("Lagringsstedet er ikke tilgjengelig"))
        if not self.password:
            raise RepoError(_("Passordet for sikkerhetskopiene mangler"))
        env = {**os.environ, "RESTIC_REPOSITORY": loc, "RESTIC_PASSWORD": self.password, "LC_ALL": "C.UTF-8",
               "RESTIC_PROGRESS_FPS": "2"}
        return env

    # ------------------------------------------------------------ kjøring

    def _cmd(self, args: list[str]) -> list[str]:
        exe = restic_path()
        if not exe:
            raise RepoError(_("restic er ikke installert"))
        return [exe, *args]

    def run(self, args: list[str], timeout: int | None = 600, input: str | None = None) -> tuple[int, str, str]:
        try:
            p = subprocess.run(self._cmd(args), capture_output=True, text=True, timeout=timeout, input=input,
                               env=self.env())
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            raise RepoError(_("restic svarte ikke i tide")) from None

    def stream(self, args: list[str], on_message, cancelled=lambda: False) -> tuple[int, str]:
        """Kjører restic med --json og kaller on_message(dict) for hver linje."""
        self.proc = subprocess.Popen(self._cmd(["--json", *args]), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=self.env())
        errors: list[str] = []
        import threading

        def read_err():
            for line in self.proc.stderr:
                errors.append(line.rstrip())
        t = threading.Thread(target=read_err, daemon=True)
        t.start()
        try:
            for line in self.proc.stdout:
                if cancelled():
                    self.proc.terminate()
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                on_message(msg)
        except BaseException:
            # Feil i Waypoint (f.eks. i fremdriftsvisningen) skal ikke etterlate restic kjørende med lås på lageret.
            self.proc.terminate()
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            raise
        code = self.proc.wait()
        t.join(2)
        return code, "\n".join(errors)

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    @staticmethod
    def _raise(code: int, err: str):
        if code == EXIT_WRONG_PASSWORD or "wrong password" in err.lower():
            raise WrongPassword(_("Feil passord for sikkerhetskopiene"))
        if code == EXIT_NO_REPO or "repository does not exist" in err.lower():
            raise NoRepo(_("Det finnes ingen sikkerhetskopier på lagringsstedet ennå"))
        lines = [ln for ln in err.strip().splitlines() if ln.strip()]
        raise RepoError(lines[-1] if lines else _("restic avsluttet med kode {}").format(code), err)

    # ------------------------------------------------------------ operasjoner

    def exists(self) -> bool:
        """True hvis lageret finnes og passordet stemmer; False hvis det ikke finnes. Ellers en feil."""
        code, _out, err = self.run(["cat", "config"], timeout=120)
        if code == 0:
            return True
        if code == EXIT_NO_REPO or "repository does not exist" in err.lower() or "unable to open config" in err.lower():
            return False
        self._raise(code, err)

    def init(self):
        code, _out, err = self.run(["init"], timeout=300)
        if code != 0:
            self._raise(code, err)

    def ensure(self) -> bool:
        """Oppretter lageret hvis det ikke finnes. Returnerer True hvis det ble opprettet nå."""
        if self.exists():
            return False
        self.init()
        return True

    def unlock(self):
        self.run(["unlock"], timeout=120)

    def snapshots(self, only_host: bool = False, job_id: str | None = None) -> list[dict]:
        args = ["snapshots", "--json", "--tag", f"{TAG},job={job_id}" if job_id else TAG]
        if only_host:
            args += ["--host", host()]
        code, out, err = self.run(args, timeout=300)
        if code != 0:
            self._raise(code, err)
        snaps = json.loads(out or "[]") or []
        return sorted(snaps, key=lambda s: s.get("time", ""), reverse=True)

    def backup(self, paths: list[str], exclude_file: str | None, extra: list[str], progress, cancelled,
               job_id: str | None = None) -> dict:
        # --group-by host,tags: forelder-tidspunktet finnes selv om listen over mapper har endret seg (ny mappe
        # i ~/.config o.l.), så bare endrede filer leses. Samme gruppering brukes ved utynning.
        args = ["backup", "--tag", TAG, "--tag", "home=" + str(Path.home()), "--host", host(), "--exclude-caches",
                "--one-file-system", "--group-by", GROUP_BY]
        if job_id:  # flere oppgaver kan dele lagringssted; merkelappen holder tidspunktene deres fra hverandre
            args += ["--tag", "job=" + job_id]
        if exclude_file:
            args += ["--exclude-file", exclude_file]
        args += extra + ["--", *paths]
        summary: dict = {}

        def msg(m):
            t = m.get("message_type")
            if t == "status":
                progress(float(m.get("percent_done") or 0), (m.get("current_files") or [""])[0])
            elif t == "summary":
                summary.update(m)
        code, err = self.stream(args, msg, cancelled)
        if code not in (0, EXIT_INCOMPLETE):
            self._raise(code, err)
        summary["incomplete"] = code == EXIT_INCOMPLETE
        return summary

    def forget_timeline(self, prune: bool, job_id: str | None = None):
        """Utynning som Time Machine: alt fra siste døgn, én per dag den siste måneden, én per uke ellers."""
        tag = f"{TAG},job={job_id}" if job_id else TAG  # komma = alle merkelappene må finnes
        args = ["forget", "--tag", tag, "--host", host(), "--group-by", GROUP_BY, "--keep-within", "1d",
                "--keep-within-daily", "1m", "--keep-within-weekly", "100y", "--keep-last", "1"]
        if prune:
            args.append("--prune")
        code, _out, err = self.run(args, timeout=None)
        if code != 0:
            self._raise(code, err)

    def add_tag(self, ids: list[str], tag: str):
        code, _out, err = self.run(["tag", "--add", tag, *ids], timeout=600)
        if code != 0:
            self._raise(code, err)

    def forget_job(self, job_id: str) -> int:
        """Sletter alle tidspunktene til en oppgave (når brukeren sletter oppgaven og ber om det)."""
        ids = [s["id"] for s in self.snapshots(job_id=job_id)]
        if ids:
            self.forget_ids(ids, prune=True)
        return len(ids)

    def forget_ids(self, ids: list[str], prune: bool = True):
        code, _out, err = self.run(["forget", *(["--prune"] if prune else []), *ids], timeout=None)
        if code != 0:
            self._raise(code, err)

    def check(self, subset: str = "2%") -> tuple[bool, str]:
        code, out, err = self.run(["check", "--read-data-subset", subset], timeout=None)
        text = (err or out).strip().splitlines()
        return code == 0, (text[-1] if text else "")

    def ls(self, snapshot: str, path: str) -> list[dict]:
        """Det som ligger direkte i mappen path i et tidspunkt."""
        code, out, err = self.run(["ls", "--json", snapshot, path], timeout=300)
        if code != 0:
            self._raise(code, err)
        nodes = []
        base = path.rstrip("/") or "/"
        for line in out.splitlines():
            try:
                n = json.loads(line)
            except ValueError:
                continue
            if n.get("struct_type", n.get("message_type")) != "node":
                continue
            if os.path.dirname(n.get("path", "").rstrip("/")) == base:
                nodes.append(n)
        return sorted(nodes, key=lambda n: (n.get("type") != "dir", n.get("name", "").lower()))

    def restore(self, snapshot: str, target: str, includes: list[str], progress=lambda f: None, cancelled=lambda: False):
        args = ["restore", snapshot, "--target", target]
        for inc in includes:
            args += ["--include", glob_escape(inc)]

        def msg(m):
            if m.get("message_type") == "status":
                progress(float(m.get("percent_done") or 0))
        code, err = self.stream(args, msg, cancelled)
        if code != 0:
            self._raise(code, err)

    def change_password(self, new_password: str):
        import tempfile
        settings.state_dir().mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=settings.state_dir()) as f:  # 0600, privat mappe
            f.write(new_password)
        try:
            os.chmod(f.name, 0o600)
            code, _out, err = self.run(["key", "passwd", "--new-password-file", f.name], timeout=300)
        finally:
            os.unlink(f.name)
        if code != 0:
            self._raise(code, err)

    def free_space(self) -> tuple[int, int] | None:
        """(ledig, totalt) for lokale lagre."""
        root = self.local_root(mount=False)
        if not root:
            return None
        st = os.statvfs(root)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


def storage_root(path: str, uri: str | None = None) -> tuple[str, str | None]:
    """Velger brukeren selve «Waypoint»-mappen (lageret), er det mappen over som er lagringsstedet."""
    p = Path(path)
    if p.name == REPO_DIR and (p / "config").is_file():
        parent_uri = uri
        if uri:
            from gi.repository import Gio
            parent = Gio.File.new_for_uri(uri).get_parent()
            parent_uri = parent.get_uri() if parent else uri
        return str(p.parent), parent_uri
    return path, uri


def snapshot_job(snap: dict) -> str | None:
    """Oppgaven et tidspunkt hører til (merkelappen job=…), eller None for tidspunkter fra før oppgavene fantes."""
    return next((t[4:] for t in snap.get("tags") or [] if t.startswith("job=")), None)


def is_setup_path(path: str) -> bool:
    """Mappen med Waypoint-oppsettet (manifest og GNOME-innstillinger) i et tidspunkt: …/waypoint/setup-<oppgave>
    (eller …/waypoint/setup fra før oppgavene fantes)."""
    return re.search(r"/waypoint/setup(-[0-9a-z]+)?/?$", path) is not None


def snapshot_home(snap: dict) -> str:
    """Hjemmemappen på maskinen tidspunktet ble tatt fra (lagret som merkelappen home=…)."""
    for t in snap.get("tags") or []:
        if t.startswith("home="):
            return t[5:]
    paths = [p for p in snap.get("paths", []) if p.startswith("/home/") and not is_setup_path(p)]
    return os.path.commonpath(paths) if paths else str(Path.home())


def install_restic(log=print) -> bool:
    """Installerer restic med systemets pakkesystem (spør om administratorpassord)."""
    from .restore import helper, helper_problem
    import tempfile
    mgr = system.package_manager()
    if not mgr or helper_problem():
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"manager": mgr, "repos": [], "packages": ["restic"]}, f)
    try:
        p = subprocess.run(["pkexec", str(helper()), f.name], capture_output=True, text=True)
        for line in p.stdout.splitlines():
            log(line)
    finally:
        os.unlink(f.name)
    return shutil.which("restic") is not None
