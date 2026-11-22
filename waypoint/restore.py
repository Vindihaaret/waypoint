"""Leser en sikkerhetskopi og legger oppsettet tilbake på denne maskinen."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zlib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import crypto, system
from .backup import DCONF_GROUPS, FILE_CATEGORIES, PRIVATE_MEMBER, path_label
from .i18n import _
from .model import Group, Item, Section, find_group
from .system import HOME, USER_EXT_DIR

PRIVILEGED = Path(__file__).with_name("privileged.py")   # kilden; install.sh legger den inn som HELPER (root-eid)


def helper() -> Path:
    from .privileged import HELPER_PATH
    return Path(os.environ.get("WAYPOINT_HELPER") or HELPER_PATH)


def helper_version(path: Path) -> int | None:
    """HELPER_VERSION i en hjelperfil (leses som tekst, kjøres ikke), eller None."""
    import re
    try:
        m = re.search(r"^HELPER_VERSION\s*=\s*(\d+)", path.read_text(encoding="utf-8"), re.M)
    except (OSError, UnicodeDecodeError):
        return None
    return int(m.group(1)) if m else None


def helper_ready() -> bool:
    """True når den root-eide hjelperen er på plass og minst like ny som kopien i appen."""
    from .privileged import HELPER_VERSION
    path = helper()
    have = helper_version(path)
    try:
        owned_by_root = path.stat().st_uid == 0
    except OSError:
        owned_by_root = False
    return owned_by_root and have is not None and have >= HELPER_VERSION


def helper_command(plan: str) -> list[str]:
    """pkexec-kommandoen for en plan. Er hjelperen på plass, brukes den. Ellers kjøres kopien i appen én gang med
    --install-and-run: den legger seg inn som root-eid fil og kjører planen, så brukeren bare får ett passordvindu.
    Neste gang brukes den installerte hjelperen, og ingen fil i hjemmemappen kjøres som root."""
    if helper_ready():
        return ["pkexec", str(helper()), plan]
    return ["pkexec", "/usr/bin/python3", "-I", str(PRIVILEGED), "--install-and-run", plan]


class BadBackup(Exception):
    pass


class NeedPassword(Exception):
    pass


class BackupFile:
    """En åpnet sikkerhetskopi. De små delene (innstillinger, bakgrunn, utvidelser og det krypterte arkivet
    med private data) pakkes ut med en gang til en midlertidig mappe; filene fra hjemmemappen leses først
    når de skal gjenopprettes. Er hele filen kryptert, trengs passordet for å åpne den."""

    def __init__(self, path: Path, password: str | None = None):
        self.path = Path(path)
        self.encrypted_all = crypto.is_encrypted(self.path)
        if self.encrypted_all and not password:
            raise NeedPassword()
        self.password = password
        self.private_unlocked = self.encrypted_all
        self.staging = Path(tempfile.mkdtemp(prefix="waypoint-"))
        os.chmod(self.staging, 0o700)
        self.manifest: dict | None = None
        try:
            with self.stream() as tar:
                for m in tar:
                    if m.name.startswith("home/"):
                        break  # resten er filer fra hjemmemappen
                    if not _safe_name(m.name):
                        continue
                    if m.name == "manifest.json":
                        self.manifest = json.load(tar.extractfile(m))
                    elif m.name in ("dconf.ini", PRIVATE_MEMBER) or m.name.startswith(("wallpaper/", "extensions/")):
                        tar.extract(m, self.staging, filter="tar")
        except crypto.BadPassword:
            self.cleanup()
            raise
        except (tarfile.TarError, OSError, EOFError, ValueError, crypto.CryptoError) as e:
            self.cleanup()
            raise BadBackup(_("Filen kunne ikke leses: {}").format(e)) from e
        if not self.manifest or self.manifest.get("app") not in ("waypoint", "flytt"):
            self.cleanup()
            raise BadBackup(_("Filen er ikke en sikkerhetskopi fra Waypoint."))
        if self.has_private and password and not self.encrypted_all:
            self.unlock(password)

    @contextmanager
    def stream(self, src: Path | None = None, password: str | None = None, verify: bool = False):
        """Gir et tarfile som leses sekvensielt, dekryptert ved behov. Med verify leses resten av en kryptert
        strøm til slutten, og gpg sin kontroll av at innholdet er uendret (MDC) sjekkes: den meldes først helt til
        slutt. Uten verify (f.eks. når bare begynnelsen av filen leses) stoppes gpg i stedet."""
        src = src or self.path
        encrypted = crypto.is_encrypted(src)
        if not encrypted:
            with tarfile.open(src, "r|gz") as tar:
                yield tar
            return
        g = crypto.decrypt_from(src, password or self.password)
        try:
            try:
                tar = tarfile.open(fileobj=g.proc.stdout, mode="r|gz")
            except tarfile.ReadError:
                code, err = g.finish()
                crypto.raise_for(code, err)
                raise
            yield tar
            if verify:
                while g.proc.stdout.read(1 << 16):
                    pass
                code, err = g.finish()
                crypto.raise_for(code, err)
        finally:
            g.kill()
            g.cleanup()

    @property
    def has_private(self) -> bool:
        return bool(self.manifest and self.manifest.get("private"))

    def unlock(self, password: str) -> bool:
        """Sjekker passordet mot det krypterte arkivet med private data."""
        if self.encrypted_all:
            return True
        blob = self.staging / PRIVATE_MEMBER
        if blob.exists() and crypto.check_password(blob, password):
            self.password = password
            self.private_unlocked = True
            return True
        return False

    def cleanup(self):
        shutil.rmtree(self.staging, ignore_errors=True)

    # ------------------------------------------------------------ beskrivelse

    def summary(self) -> str:
        m = self.manifest
        try:
            when = datetime.fromisoformat(m["created"]).strftime(_("%d.%m.%Y kl. %H:%M"))
        except (KeyError, ValueError):
            when = "?"
        text = f"{m.get('hostname', '?')} · {m.get('distro', {}).get('name', 'Linux')} · {when}"
        if self.encrypted_all:
            text += " · " + _("kryptert")
        return text

    def sections(self) -> list[Section]:
        m = self.manifest
        result = []

        desk = Section(_("Skrivebordet"), _("Slik GNOME så ut og oppførte seg"))
        dconf = self.staging / "dconf.ini"
        if dconf.exists():
            text = dconf.read_text()
            g = Group("settings", _("GNOME-innstillinger"), icon="preferences-desktop-appearance-symbolic", flat=True)
            stored = m.get("settings", {}).get("categories", [])
            for cat in [c for c in DCONF_GROUPS if c in stored] + [c for c in stored if c not in DCONF_GROUPS]:
                _text, count = system.filter_dconf(text, {cat})
                title, desc = DCONF_GROUPS.get(cat, (cat, ""))
                if count:
                    g.items.append(Item(id=cat, label=_(title), detail=_("{} · {} verdier").format(_(desc), count)))
            if g.items:
                desk.groups.append(g)
        wp = m.get("wallpaper")
        if wp:
            files = {v["file"] for v in wp["keys"].values()}
            first = next(iter(wp["keys"].values()))
            g = Group("wallpaper", _("Bakgrunnsbilde"), icon="preferences-desktop-wallpaper-symbolic", flat=True)
            g.items.append(Item(id="wallpaper", label=_("Bakgrunn og låseskjerm") if len(files) == 1 else
                                _("Bakgrunnsbilder (lys, mørk og låseskjerm)"), detail=Path(first["original"]).name,
                                data={"keys": wp["keys"], "preview": str(self.staging / first["file"])}))
            desk.groups.append(g)
        result.append(desk)

        if m.get("extensions"):
            g = Group("extensions", _("GNOME-utvidelser"), icon="application-x-addon-symbolic",
                      subtitle=_("Hentes fra sikkerhetskopien, eller fra extensions.gnome.org hvis GNOME er nyere"))
            for e in m["extensions"]:
                here = system.extension_installed(e["uuid"])
                detail = e["uuid"] + " · " + (_("slått på") if e.get("enabled") else _("slått av"))
                if here:
                    detail += " · " + _("finnes allerede her")
                g.items.append(Item(id=e["uuid"], label=e["name"], detail=detail, selected=not here, data=e))
            result.append(Section(_("Utvidelser"), _("Utvidelsene du hadde, og om de var slått på"), [g]))

        apps = Section(_("Programmer"), _("Installeres på nytt fra nettet – krever internett og administratorpassord"))
        mgr = system.package_manager()
        other = system.PM_NAMES.get(mgr, _("et annet pakkesystem"))
        repos = m.get("repos")
        if repos and repos.get("items"):
            g = Group("repos", _("Pakkekilder"), icon="network-server-symbolic",
                      subtitle=_("Legges til før pakkene installeres"))
            current = system.repositories(mgr)
            urls = {_norm(r["url"]) for r in current}
            aliases = {r["alias"] for r in current}
            same = repos.get("manager") == mgr
            for r in repos["items"]:
                exists = _norm(r["url"]) in urls or r["alias"] in aliases
                # En kilde kan gi root-tilgang via pakkene, og kopien kan være laget et annet sted: aldri valgt på
                # forhånd, og brukeren ser hele adressen (for dnf hele filen slik den blir skrevet).
                ok = True
                if exists:
                    detail = _("Finnes allerede")
                elif r.get("content") is not None:
                    from .privileged import valid_dnf_repo
                    clean = valid_dnf_repo(r.get("content"))
                    ok = clean is not None
                    detail = clean.strip() if ok else _("Hoppes over: filen er ikke én signert pakkekilde")
                else:
                    detail = r["url"] + (" · " + _("nøkkel: {}").format(r["gpgkey"]) if r.get("gpgkey") else "")
                g.items.append(Item(id=r["alias"], label=r["name"], detail=detail,
                                    available=same and not exists and ok, selected=False, data=r))
            if not same:
                g.note = _("Kopien er laget med {}, denne maskinen bruker {}.").format(
                    system.PM_NAMES.get(repos.get("manager"), "?"), other)
            apps.groups.append(g)
        pk = m.get("packages")
        if pk and pk.get("items"):
            same = pk.get("manager") == mgr
            installed = system.installed_packages(mgr) if same else set()
            g = Group("packages", _("Systempakker"), icon="system-software-install-symbolic",
                      subtitle=_("Fra {}").format(system.PM_NAMES.get(pk.get("manager"), "?")))
            for p in pk["items"]:
                here = p["name"] in installed
                g.items.append(Item(id=p["name"], label=p["name"],
                                    detail=_("Installert") if here else p.get("summary", ""),
                                    available=same and not here, selected=same and not here, data=p))
            if not same:
                g.note = _("Pakkenavnene er fra en annen distribusjon og kan ikke installeres automatisk her. "
                           "Bruk listen som huskeliste.")
            apps.groups.append(g)
        fp = m.get("flatpaks")
        if fp and fp.get("items"):
            have = system.have("flatpak")
            installed = {a["id"] for a in system.flatpak_apps()}
            g = Group("flatpaks", _("Flatpak-apper"), icon="package-x-generic-symbolic",
                      subtitle=_("Fra Flathub og andre Flatpak-kilder"), data={"remotes": fp.get("remotes", [])})
            for a in fp["items"]:
                here = a["id"] in installed
                g.items.append(Item(id=a["id"], label=a["name"], detail=_("Installert") if here else a["id"],
                                    available=have and not here, selected=have and not here, data=a))
            if not have:
                g.note = _("Flatpak er ikke installert. Installer pakken «flatpak» først (den står kanskje i listen over).")
            apps.groups.append(g)
        result.append(apps)

        files = Section(_("Filer og oppsett"), _("Legges tilbake i hjemmemappen. Det som ligger der fra før, flyttes "
                                                 "til ~/.local/share/waypoint/replaced, så ingenting går tapt."))
        cats: dict[str, Group] = {}
        for f in m.get("files", []):
            cat = f.get("category", "apps")
            if cat not in cats:
                title, desc, icon = FILE_CATEGORIES.get(cat, (cat, "", "folder-symbolic"))
                cats[cat] = Group("files-" + cat, _(title), subtitle=_(desc), icon=icon)
            exists = (HOME / f["path"]).exists()
            detail = "~/" + f["path"]
            if exists:
                detail += " · " + _("erstatter det som er der nå")
            cats[cat].items.append(Item(id=f["path"], label=path_label(f["path"], f["label"]), size=f.get("size", 0),
                                        detail=detail, sensitive=f.get("sensitive", False), data=f))
        files.groups = [cats[c] for c in FILE_CATEGORIES if c in cats] + [g for c, g in cats.items()
                                                                           if c not in FILE_CATEGORIES]
        result.append(files)
        return [s for s in result if s.groups]


def _norm(url: str) -> str:
    return url.strip().rstrip("/").replace("http://", "https://")


def _safe_name(name: str) -> bool:
    return not name.startswith("/") and ".." not in Path(name).parts


# ---------------------------------------------------------------- gjennomføring

class Step:
    def __init__(self, id: str, title: str, func):
        self.id, self.title, self.func = id, title, func


class Restorer:
    """Bygger stegene ut fra valgene. Hvert steg kalles med log(tekst) og returnerer (ok, oppsummering)."""

    def __init__(self, backup: BackupFile, sections: list[Section]):
        self.b = backup
        self.s = sections
        self.undo_dir = HOME / ".local/share/waypoint/replaced" / datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.needs_logout = False
        self.proc: subprocess.Popen | None = None

    def _sel(self, gid: str) -> list[Item]:
        g = find_group(self.s, gid)
        return g.selected() if g else []

    def _files(self) -> list[Item]:
        return [i for sec in self.s for g in sec.groups if g.id.startswith("files-") for i in g.selected()]

    def needs_private_password(self) -> bool:
        return any(i.data.get("sensitive") for i in self._files()) and not self.b.private_unlocked

    def steps(self) -> list[Step]:
        steps = []
        if self._sel("repos") or self._sel("packages"):
            steps.append(Step("packages", _("Systempakker og pakkekilder"), self.do_packages))
        if self._sel("flatpaks"):
            steps.append(Step("flatpaks", _("Flatpak-apper"), self.do_flatpaks))
        if self._files():
            steps.append(Step("files", _("Filer og oppsett"), self.do_files))
        if self._sel("extensions"):
            steps.append(Step("extensions", _("GNOME-utvidelser"), self.do_extensions))
        if self._sel("settings"):
            steps.append(Step("settings", _("GNOME-innstillinger"), self.do_settings))
        if self._sel("wallpaper"):
            steps.append(Step("wallpaper", _("Bakgrunnsbilde"), self.do_wallpaper))
        return steps

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        if isinstance(self.b, SnapshotBackup):
            self.b.repo.cancel()

    def _stream(self, cmd: list[str], log) -> tuple[int, list[str]]:
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, env={**os.environ, "LC_ALL": "C.UTF-8"})
        lines = []
        from . import debuglog
        debuglog.command(cmd)
        for line in self.proc.stdout:
            line = line.rstrip()
            lines.append(line)
            log(line)
        code = self.proc.wait()
        debuglog.debug("  → exit %s", code)
        return code, lines

    # ------------------------------------------------------------ steg

    def do_packages(self, log):
        mgr = system.package_manager()
        plan = {"manager": mgr, "repos": [i.data for i in self._sel("repos")],
                "packages": [i.id for i in self._sel("packages")]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(plan, f)
        try:
            log(_("Ber om administratorpassord …"))
            if not helper_ready():
                log(_("Legger også inn hjelperen som installerer pakker (bare første gang)"))
            code, lines = self._stream(helper_command(f.name), log)
        finally:
            os.unlink(f.name)
        if code in (126, 127) and not any(line.startswith("@@") for line in lines):
            return False, _("Avbrutt – administratorpassordet ble ikke gitt")
        ok = sum(line.startswith("@@OK ") for line in lines)
        failed = [line[7:] for line in lines if line.startswith("@@FAIL ")]
        parts = []
        if plan["repos"]:
            parts.append(_("{} kilder lagt til").format(len(plan["repos"])))
        if plan["packages"]:
            parts.append(_("{} av {} pakker installert").format(ok, len(plan["packages"])))
        if failed:
            parts.append(_("feilet: {}").format(", ".join(failed[:6]) + (" …" if len(failed) > 6 else "")))
        return code == 0 and not failed, " · ".join(parts) or _("Ferdig")

    def do_flatpaks(self, log):
        g = find_group(self.s, "flatpaks")
        apps = g.selected()
        existing = {(r["name"], r["installation"]) for r in system.flatpak_remotes()}
        for r in g.data.get("remotes", []):
            if (r["name"], r["installation"]) in existing or r["name"] not in {a.data["origin"] for a in apps}:
                continue
            url = r["url"]
            if "flathub" in r["name"] or "dl.flathub.org" in url:
                url = "https://dl.flathub.org/repo/flathub.flatpakrepo"
            log(_("Legger til Flatpak-kilden {}").format(r["name"]))
            self._stream(["flatpak", "remote-add", "--if-not-exists", f"--{r['installation']}", r["name"], url], log)
        # Samlet per installasjon og kilde, så en systeminstallasjon bare ber om administratorpassordet én gang
        # (polkit husker det en stund). Ikke --noninteractive: da får Flatpak ikke spørre polkit om passordet, og
        # alt som skal installeres for hele systemet avvises («Deploy not allowed for user»). -y svarer ja på
        # Flatpak sine egne spørsmål.
        groups: dict[tuple[str, str], list] = {}
        for a in apps:
            groups.setdefault((a.data["installation"], a.data["origin"]), []).append(a)
        ok, failed = 0, []
        for (installation, origin), members in groups.items():
            base = ["flatpak", "install", "-y", f"--{installation}", origin]
            names = ", ".join(a.data["name"] for a in members)
            log(_("Installerer {} fra {}").format(names, origin))
            if installation == "system":
                log(_("Flatpak ber om administratorpassordet for å installere for alle brukere"))
            code, _lines = self._stream(base + [a.data["id"] for a in members], log)
            if code == 0:
                ok += len(members)
                continue
            if len(members) > 1:
                log(_("Ikke alle kunne installeres samtidig – prøver én om gangen"))
            for n, a in enumerate(members, 1):
                d = a.data
                if len(members) > 1:
                    log(_("[{}/{}] Installerer {} ({})").format(n, len(members), d["name"], d["id"]))
                    code, _lines = self._stream(base + [d["id"]], log)
                if code == 0:
                    ok += 1
                else:
                    failed.append(d["name"])
        text = _("{} av {} apper installert").format(ok, len(apps))
        if failed:
            text += " · " + _("feilet: {}").format(", ".join(failed))
        return not failed, text

    def _extract(self, tar, wanted: list[str], counts: dict, log):
        restored: set[str] = set()   # det som er lagt tilbake i denne kjøringen (mål for hardlenker)
        for m in tar:
            if not m.name.startswith("home/"):
                continue
            rel = m.name[5:]
            if not _safe_name(rel) or not any(rel == w or rel.startswith(w + "/") for w in wanted):
                continue
            if m.islnk():
                # Hardlenker peker på et annet medlem i arkivet; det navnet må skrives om på samme måte, og målet
                # må være lagt tilbake nå (ellers kunne lenken peke på en fil som lå der fra før).
                link = m.linkname[5:] if m.linkname.startswith("home/") else m.linkname
                if not _safe_name(link) or link not in restored:
                    log(_("Kunne ikke legge tilbake {}: {}").format("~/" + rel, m.linkname))
                    continue
                m.linkname = link
            dest = HOME / rel
            moved_to = None
            if dest.exists() or dest.is_symlink():
                if not (m.isdir() and dest.is_dir() and not dest.is_symlink()):
                    moved_to = self.undo_dir / rel
                    moved_to.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dest), str(moved_to))
                    counts["moved"] += 1
            m.name = rel
            try:
                # filter="tar" nekter å skrive utenfor hjemmemappen, også via symlenker i stien (realpath-sjekk),
                # men tillater absolutte symlenker, som er vanlige og ufarlige i oppsettsfiler (f.eks. til /opt).
                tar.extract(m, HOME, filter="tar")
                counts["files"] += 1
                restored.add(rel)
            except (OSError, tarfile.TarError, KeyError) as e:
                log(_("Kunne ikke legge tilbake {}: {}").format("~/" + rel, e))
                if moved_to is not None:  # legg originalen tilbake, eller si hvor den er
                    try:
                        if not (dest.exists() or dest.is_symlink()):
                            shutil.move(str(moved_to), str(dest))
                            counts["moved"] -= 1
                            continue
                    except OSError:
                        pass
                    log(_("Det som lå der fra før, er flyttet til {}").format(moved_to))
            if counts["files"] % 500 == 0:
                log(_("{} filer lagt tilbake …").format(counts["files"]))

    def do_files(self, log):
        result = self._do_files(log)
        old_home = (self.b.manifest or {}).get("home") or str(HOME)
        fix_home_paths(old_home, log)
        return result

    def _do_files(self, log):
        items = self._files()
        if isinstance(self.b, SnapshotBackup):
            return self.b.restore_files([i.id for i in items], self.undo_dir, log)
        counts = {"files": 0, "moved": 0}
        inline = [i.id for i in items if self.b.encrypted_all or not i.data.get("sensitive")]
        private = [i.id for i in items if not self.b.encrypted_all and i.data.get("sensitive")]
        try:
            if inline:
                with self.b.stream(verify=True) as tar:
                    self._extract(tar, inline, counts, log)
            if private:
                if not self.b.private_unlocked:
                    return False, _("Nøkler og passord ble hoppet over: passordet mangler")
                log(_("Dekrypterer nøkler og passord …"))
                with self.b.stream(self.b.staging / PRIVATE_MEMBER, self.b.password, verify=True) as tar:
                    self._extract(tar, private, counts, log)
        except (crypto.CryptoError, crypto.BadPassword, tarfile.TarError, EOFError, OSError, zlib.error) as e:
            # Passordet er allerede kontrollert da filen ble åpnet, så en feil her betyr at innholdet er skadet
            # eller endret (gpg sin kontroll feilet, eller arkivet slutter midt i).
            log(_("Feil: {}").format(e or type(e).__name__))
            text = _("Sikkerhetskopien kan være skadet eller endret – kontroller filene. Det som lå der fra før, "
                     "er i {}").format(str(self.undo_dir).replace(str(HOME), "~"))
            log(text)
            return False, text
        text = _("{} filer og mapper lagt tilbake").format(counts["files"])
        if counts["moved"]:
            where = str(self.undo_dir).replace(str(HOME), "~")
            text += " · " + _("{} gamle flyttet til {}").format(counts["moved"], where)
            log(_("Det som lå der fra før, er flyttet til {}").format(self.undo_dir))
        return True, text

    def do_extensions(self, log):
        major = system.shell_major()
        ok, failed = 0, []
        for item in self._sel("extensions"):
            e = item.data
            uuid = e["uuid"]
            staged = self.b.staging / "extensions" / uuid
            compatible = major in e.get("shell_versions", [])
            done = False
            if e.get("files") and staged.is_dir() and compatible:
                done = self._copy_extension(staged, uuid, log)
            if not done:
                done = self._download_extension(uuid, major, log)
            if not done and e.get("files") and staged.is_dir():
                log(_("{}: bruker kopien fra sikkerhetskopien, selv om den ikke er merket for GNOME {}")
                    .format(e["name"], major))
                done = self._copy_extension(staged, uuid, log)
            if done:
                ok += 1
            else:
                failed.append(e["name"])
                log(_("{}: fant ingen versjon som kan installeres").format(e["name"]))
        on = [i.data["uuid"] for i in self._sel("extensions") if i.data.get("enabled")]
        enabled = system.parse_strv(system.gsettings_get("org.gnome.shell", "enabled-extensions"))
        disabled = system.parse_strv(system.gsettings_get("org.gnome.shell", "disabled-extensions"))
        system.gsettings_set("org.gnome.shell", "enabled-extensions",
                             system.format_strv(enabled + [u for u in on if u not in enabled]))
        system.gsettings_set("org.gnome.shell", "disabled-extensions",
                             system.format_strv([u for u in disabled if u not in on]))
        system.gsettings_set("org.gnome.shell", "disable-user-extensions", "false")
        self.needs_logout = True
        text = _("{} av {} utvidelser installert – virker etter ny innlogging").format(ok, ok + len(failed))
        if failed:
            text += " · " + _("mangler: {}").format(", ".join(failed))
        return not failed, text

    def _copy_extension(self, staged: Path, uuid: str, log) -> bool:
        dest = USER_EXT_DIR / uuid
        try:
            if dest.exists():
                target = self.undo_dir / ".local/share/gnome-shell/extensions" / uuid
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest), str(target))
            shutil.copytree(staged, dest, symlinks=True)
            log(_("{}: lagt tilbake fra sikkerhetskopien").format(uuid))
            return True
        except OSError as e:
            log(f"{uuid}: {e}")
            return False

    def _download_extension(self, uuid: str, major: str, log) -> bool:
        if not major:
            return False
        try:
            q = urllib.parse.urlencode({"uuid": uuid, "shell_version": major})
            req = urllib.request.Request(f"{EGO}/extension-info/?{q}", headers={"User-Agent": "Waypoint"})
            with urllib.request.urlopen(req, timeout=20) as r:
                info = json.load(r)
            url = info.get("download_url")
            if not url:
                return False
            log(_("{}: henter versjon for GNOME {} fra extensions.gnome.org").format(uuid, major))
            req = urllib.request.Request(EGO + url, headers={"User-Agent": "Waypoint"})
            with urllib.request.urlopen(req, timeout=60) as r, \
                    tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
                shutil.copyfileobj(r, f)
            try:
                code, _out, err = system.run(["gnome-extensions", "install", "--force", f.name], 60)
            finally:
                os.unlink(f.name)
            if code != 0:
                log(f"{uuid}: {err.strip()}")
            return code == 0
        except (OSError, ValueError) as e:
            log(_("{}: ikke tilgjengelig på extensions.gnome.org ({})").format(uuid, e))
            return False

    def do_settings(self, log):
        cats = {i.id for i in self._sel("settings")}
        text, count = system.filter_dconf((self.b.staging / "dconf.ini").read_text(), cats)
        text = rewrite_home(text, (self.b.manifest or {}).get("home") or str(HOME), str(HOME))
        # Ta vare på de nåværende innstillingene, så de kan hentes tilbake med «dconf load / < fil».
        self.undo_dir.mkdir(parents=True, exist_ok=True)
        saved = self.undo_dir / "dconf-before-restore.ini"
        saved.write_text(system.dconf_dump())
        code, _out, err = system.run(["dconf", "load", "/"], 60, input=text)
        if code != 0:
            log(err)
            return False, _("dconf kunne ikke lese innstillingene")
        log(_("{} innstillinger lagt inn. De gamle er lagret i {}").format(count, saved))
        return True, _("{} innstillinger lagt inn").format(count)

    def do_wallpaper(self, log):
        keys = self._sel("wallpaper")[0].data["keys"]
        bg_dir = HOME / ".local/share/backgrounds"
        done = {}
        for k, v in keys.items():
            schema, key = k.split(" ", 1)
            orig = Path(v["original"])
            if v["file"] in done:
                path = done[v["file"]]
            elif orig.is_file():
                path = orig
            else:
                bg_dir.mkdir(parents=True, exist_ok=True)
                path = bg_dir / orig.name
                shutil.copy2(self.b.staging / v["file"], path)
                log(_("Kopierte {} til {}").format(orig.name, bg_dir))
            done[v["file"]] = path
            system.gsettings_set(schema, key, "'" + system.path_to_uri(path) + "'")
        return True, _("Bakgrunnsbildet er satt")


# ---------------------------------------------------------------- stier fra en annen hjemmemappe

def rewrite_home(text: str, old_home: str, new_home: str) -> str:
    """Bytter den gamle hjemmemappen (f.eks. /home/ola på maskinen kopien ble tatt på) med den nye, både som sti og
    som file://-adresse. Bare hele stier: /home/ola/… og /home/ola rett før et anførselstegn eller linjeslutt, så
    /home/olav ikke blir berørt."""
    old_home, new_home = old_home.rstrip("/"), new_home.rstrip("/")
    if not old_home or old_home == new_home:
        return text
    import re
    for old, new in ((system.path_to_uri(old_home), system.path_to_uri(new_home)), (old_home, new_home)):
        text = re.sub(re.escape(old) + r"(?=[/'\"\s]|$)", lambda _m, n=new: n, text, flags=re.M)
    return text


def fix_home_paths(old_home: str, log=lambda t: None):
    """Etter at filene er lagt tilbake: bokmerkene i Filer og standardmappene (Dokumenter, Nedlastinger …) peker
    på den nye hjemmemappen, og standardmapper som mangler, opprettes. Ellers viser Filer dem med varseltegn og
    «Unable to find …» (f.eks. når brukernavnet er et annet, eller de personlige mappene ikke ble valgt)."""
    for rel in (".config/gtk-3.0/bookmarks", ".config/user-dirs.dirs"):
        f = HOME / rel
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new = rewrite_home(text, old_home, str(HOME))
        if new != text:
            f.write_text(new, encoding="utf-8")
            log(_("Skrev om stier fra {} til {} i {}").format(old_home, HOME, "~/" + rel))
    try:
        text = (HOME / ".config/user-dirs.dirs").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    import re
    for value in re.findall(r'^XDG_[A-Z]+_DIR="([^"]*)"', text, re.M):
        path = Path(value.replace("$HOME", str(HOME), 1))
        if path == HOME or HOME not in path.parents or not _safe_name(str(path.relative_to(HOME))):
            continue
        if not path.exists() and not path.is_symlink():
            try:
                path.mkdir(parents=True)
                log(_("Opprettet mappen {}").format("~/" + str(path.relative_to(HOME))))
            except OSError:
                pass


# ---------------------------------------------------------------- restic-tidspunkter

DOUBLE_SUFFIXES = (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst")


def unique_name(dest: Path, stamp: str) -> Path:
    """Et ledig navn ved siden av dest med datoen i: «navn (dato).ext», så «navn (dato 2).ext» osv. Doble
    endelser som .tar.gz holdes samlet, og mapper får ikke delt navnet sitt ved et punktum."""
    name = dest.name
    suffix = next((name[-len(e):] for e in DOUBLE_SUFFIXES if name.lower().endswith(e) and len(name) > len(e)), None)
    if suffix is None:
        suffix = "" if dest.is_dir() and not dest.is_symlink() else dest.suffix
    stem = name[:len(name) - len(suffix)] if suffix else name
    n = 1
    while True:
        label = stamp if n == 1 else f"{stamp} {n}"
        candidate = dest.with_name(f"{stem} ({label}){suffix}")
        if not (candidate.exists() or candidate.is_symlink()):
            return candidate
        n += 1


def place(src_root: Path, dest_root: Path, undo_dir: Path | None, log=lambda t: None,
          keep_both_suffix: str | None = None) -> dict:
    """Flytter alt under src_root inn under dest_root. Det som ligger der fra før, flyttes til undo_dir
    (eller, med keep_both_suffix, får den nye kopien et eget navn). Returnerer antall filer og flyttede."""
    counts = {"files": 0, "moved": 0}
    for dirpath, dirnames, filenames in os.walk(src_root):
        rel_dir = Path(dirpath).relative_to(src_root)
        for name in dirnames + filenames:
            src = Path(dirpath) / name
            if src.is_dir() and not src.is_symlink():
                d = dest_root / rel_dir / name
                if d.exists() and not d.is_dir():
                    if undo_dir:
                        t = undo_dir / rel_dir / name
                        t.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(d), str(t))
                        counts["moved"] += 1
                d.mkdir(parents=True, exist_ok=True)
                continue
            dest = dest_root / rel_dir / name
            if dest.exists() or dest.is_symlink():
                if keep_both_suffix:
                    dest = unique_name(dest, keep_both_suffix)
                elif undo_dir:
                    t = undo_dir / rel_dir / name
                    t.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dest), str(t))
                    counts["moved"] += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(src, dest)
                counts["files"] += 1
            except OSError:
                try:
                    shutil.move(str(src), str(dest))
                    counts["files"] += 1
                except OSError as e:
                    log(_("Kunne ikke legge tilbake {}: {}").format(dest, e))
    return counts


def scratch_dir() -> Path:
    """Midlertidig mappe på samme disk som hjemmemappen, så filer kan flyttes på plass uten å kopieres."""
    from . import settings
    base = settings.state_dir()
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="restore-", dir=base))


class SnapshotBackup:
    """Et tidspunkt i restic-lageret, med samme grensesnitt som BackupFile."""

    encrypted_all = True
    private_unlocked = True
    has_private = False

    def __init__(self, repo, snapshot: dict):
        self.repo = repo
        self.snap = snapshot
        self.path = Path(f"{snapshot.get('hostname', '?')} {snapshot.get('time', '')[:16].replace('T', ' ')}")
        self.staging = Path(tempfile.mkdtemp(prefix="waypoint-"))
        os.chmod(self.staging, 0o700)
        from .repo import is_setup_path
        setup = next((p for p in snapshot.get("paths", []) if is_setup_path(p)), None)
        if not setup:
            self.cleanup()
            raise BadBackup(_("Tidspunktet mangler oppsettet fra Waypoint."))
        try:
            snap_dir = self.staging / "snap"
            repo.restore(snapshot["id"], str(snap_dir), [setup])
            src = snap_dir / setup.lstrip("/")
            self.manifest = json.loads((src / "manifest.json").read_text())
            if (src / "dconf.ini").exists():
                shutil.copy2(src / "dconf.ini", self.staging / "dconf.ini")
            # Utvidelser og bakgrunnsbilder er små; hent dem med en gang.
            extra = [e["dir"] for e in self.manifest.get("extensions", []) if e.get("dir")]
            extra += [v["original"] for v in self.manifest.get("wallpaper", {}).get("keys", {}).values()]
            if extra:
                repo.restore(snapshot["id"], str(snap_dir), extra)
            for e in self.manifest.get("extensions", []):
                d = snap_dir / e.get("dir", "").lstrip("/")
                if e.get("dir") and d.is_dir():
                    (self.staging / "extensions").mkdir(exist_ok=True)
                    shutil.move(str(d), str(self.staging / "extensions" / e["uuid"]))
            for v in self.manifest.get("wallpaper", {}).get("keys", {}).values():
                v["file"] = "snap" + v["original"]
        except Exception as e:  # noqa: BLE001 – også RepoError fra restic; staging-mappen må ryddes uansett
            self.cleanup()
            raise BadBackup(_("Filen kunne ikke leses: {}").format(e)) from e

    def cleanup(self):
        shutil.rmtree(self.staging, ignore_errors=True)

    def summary(self) -> str:
        try:
            when = datetime.fromisoformat(self.snap["time"][:19]).strftime(_("%d.%m.%Y kl. %H:%M"))
        except (KeyError, ValueError):
            when = "?"
        from .repo import describe
        return f"{self.snap.get('hostname', '?')} · {when} · {describe(self.repo.cfg)}"

    def sections(self) -> list[Section]:
        return BackupFile.sections(self)  # samme visning; manifestet har samme form

    def restore_files(self, rels: list[str], undo_dir: Path, log) -> tuple[bool, str]:
        old_home = self.manifest.get("home") or str(HOME)
        tmp = scratch_dir()
        try:
            last = [-1]

            def progress(frac):
                pct = int(frac * 10) * 10
                if pct > last[0]:
                    last[0] = pct
                    log(_("Henter filer … {} %").format(pct))
            self.repo.restore(self.snap["id"], str(tmp), [f"{old_home}/{r}" for r in rels], progress)
            src = tmp / old_home.lstrip("/")
            # restic melder suksess også når et mønster ikke traff noe; sjekk at hvert valgt element faktisk kom.
            missing = [r for r in rels if not ((src / r).exists() or (src / r).is_symlink())]
            counts = place(src, HOME, undo_dir, log) if src.exists() else {"files": 0, "moved": 0}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        text = _("{} filer og mapper lagt tilbake").format(counts["files"])
        if counts["moved"]:
            text += " · " + _("{} gamle flyttet til {}").format(counts["moved"], str(undo_dir).replace(str(HOME), "~"))
        if missing:
            text += " · " + _("fant ikke: {}").format(", ".join("~/" + m for m in missing[:6]))
            for m in missing:
                log(_("Fant ikke {} i tidspunktet").format("~/" + m))
        return not missing, text


def restore_single(repo, snapshot: dict, path: str, log=lambda t: None) -> Path:
    """Henter én fil eller mappe fra et tidspunkt tilbake til samme sted. Finnes den fra før, får den hentede
    kopien et navn med datoen, så ingenting blir overskrevet. Returnerer stien den havnet på."""
    tmp = scratch_dir()
    try:
        repo.restore(snapshot["id"], str(tmp), [path])
        src = tmp / path.lstrip("/")
        if not (src.exists() or src.is_symlink()):
            from .repo import RepoError
            raise RepoError(_("Fant ikke {} i tidspunktet").format(path))
        from .repo import snapshot_home
        old_home = snapshot_home(snapshot)
        rel = os.path.relpath(path, old_home) if path.startswith(old_home.rstrip("/") + "/") else None
        dest = HOME / rel if rel else Path(path)
        stamp = snapshot.get("time", "")[:10]
        if dest.exists() or dest.is_symlink():
            dest = unique_name(dest, stamp)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def is_backup_name(name: str) -> bool:
    return system.is_backup_name(name)


def find_backups(extra: list[Path] = ()) -> list[Path]:
    """Sikkerhetskopier i hjemmemappen, på minnepinner og i mappen for automatiske kopier, nyeste først."""
    places = [HOME, HOME / "Dokumenter", HOME / "Documents", HOME / "Nedlastinger", HOME / "Downloads", *extra]
    media = Path("/run/media") / os.environ.get("USER", "")
    try:
        places += [d for d in media.iterdir() if d.is_dir()]
    except OSError:
        pass
    found = set()
    for d in places:
        try:
            found.update(p for p in d.iterdir() if p.is_file() and is_backup_name(p.name))
        except OSError:
            continue
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)
