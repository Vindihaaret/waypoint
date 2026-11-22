"""Leser tilstanden til systemet: distribusjon, pakker, Flatpak, GNOME-innstillinger og utvidelser."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

HOME = Path.home()
USER_EXT_DIR = HOME / ".local/share/gnome-shell/extensions"
SYSTEM_EXT_DIRS = [Path("/usr/share/gnome-shell/extensions"), Path("/usr/local/share/gnome-shell/extensions")]


def run(cmd: list[str], timeout: int = 120, input: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input,
                           stdin=None if input is not None else subprocess.DEVNULL)
        _debug_command(cmd, p.returncode, p.stdout, p.stderr)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        _debug_command(cmd, 127, "", str(e))
        return 127, "", str(e)


def _debug_command(cmd, code, out, err):
    from . import debuglog
    debuglog.command(cmd, code, out, err)


def out(cmd: list[str], timeout: int = 120) -> str:
    code, stdout, _ = run(cmd, timeout)
    return stdout if code == 0 else ""


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# ---------------------------------------------------------------- distribusjon

def os_release() -> dict:
    info = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            for line in Path(path).read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k] = v.strip().strip('"')
            break
        except OSError:
            continue
    return {"id": info.get("ID", "linux"), "name": info.get("PRETTY_NAME") or info.get("NAME", "Linux"),
            "like": info.get("ID_LIKE", ""), "version": info.get("VERSION_ID", "")}


def package_manager() -> str | None:
    for mgr in ("zypper", "dnf", "apt-get", "pacman"):
        if have(mgr):
            return "apt" if mgr == "apt-get" else mgr
    return None


PM_NAMES = {"zypper": "zypper (openSUSE)", "dnf": "dnf (Fedora)", "apt": "apt (Debian/Ubuntu)",
            "pacman": "pacman (Arch)"}


def shell_version() -> str:
    m = re.search(r"(\d+(?:\.\w+)?)", out(["gnome-shell", "--version"], 10))
    return m.group(1) if m else ""


def shell_major() -> str:
    return shell_version().split(".")[0]


# ---------------------------------------------------------------- systempakker

# Pakker som alltid følger med en installasjon og bare er støy i en liste over «dine» programmer.
_BASE_NAMES = {"gpg-pubkey", "glibc", "shim", "dracut", "rpm", "filesystem", "systemd", "openSUSE-release",
               "fedora-release", "sdbootutil", "dosfstools", "e2fsprogs", "btrfsprogs", "os-prober", "base-files",
               "kernel-default", "kernel-firmware", "kernel-core", "kernel"}
_BASE_PREFIXES = ("kernel-default-", "kernel-firmware-", "grub2", "patterns-base-", "patterns-yast-",
                  "linux-image-", "linux-headers-", "linux-firmware")
# Biblioteker: lib…<versjonsnummer>, f.eks. libavcodec61, libgtk-4-1, libnvidia-egl-gbm1-32bit, libssl3t64.
# Programmer som libreoffice-writer, libvirt og librewolf ender ikke på et tall og blir med.
_LIBRARY = re.compile(r"lib[\w.+-]*?\d+(?:[._-]\d+)*(?:t64)?(?:-(?:32|64)bit)?(?:-[a-z]{2,3})?")


def _is_noise(name: str) -> bool:
    if name.endswith(("-devel", "-dev")):
        return False  # utviklingspakker installeres bevisst
    if name in _BASE_NAMES or name.startswith(_BASE_PREFIXES):
        return True
    return bool(_LIBRARY.fullmatch(name))


def user_packages(mgr: str | None) -> list[dict]:
    """Pakker brukeren selv har valgt å installere (ikke de som kom med som avhengigheter)."""
    names: list[str] = []
    summaries: dict[str, str] = {}
    if mgr in ("zypper", "dnf"):
        rows = out(["rpm", "-qa", "--qf", "%{NAME}\t%{SUMMARY}\n"])
        installed = {}
        for line in rows.splitlines():
            n, _, s = line.partition("\t")
            installed.setdefault(n, s)
        if mgr == "zypper":
            try:
                auto = {l.strip() for l in Path("/var/lib/zypp/AutoInstalled").read_text().splitlines()
                        if l.strip() and not l.startswith("#")}
            except OSError:
                auto = set()
            names = [n for n in installed if n not in auto]
        else:
            names = out(["dnf", "repoquery", "--userinstalled", "--qf", "%{name}\n"], 300).split()
        summaries = installed
    elif mgr == "apt":
        names = out(["apt-mark", "showmanual"]).split()
    elif mgr == "pacman":
        names = out(["pacman", "-Qqe"]).split()
    result = []
    for n in sorted(set(names), key=str.lower):
        if not _is_noise(n):
            result.append({"name": n, "summary": summaries.get(n, "")})
    return result


def available_packages(mgr: str | None, names: list[str]) -> set[str] | None:
    """Hvilke av names som finnes i pakkekildene på denne maskinen (installert eller ikke), eller None hvis
    pakkesystemet ikke kunne spørres (da vet vi ikke, og ingenting bør gråes ut av den grunn)."""
    names = [n for n in names if n and not n.startswith("-")]
    if not names or not mgr:
        return set()
    if mgr == "zypper":
        # 104: ingen treff. Uten root brukes de nedlastede kildene som de er.
        code, xml, _err = run(["zypper", "--non-interactive", "--no-refresh", "-x", "search", "--match-exact",
                               "-t", "package", "--", *names], 120)
        if code not in (0, 104):
            return None
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return None
        return {s.get("name", "") for s in root.iter("solvable")} & set(names)
    if mgr == "dnf":
        code, text, _err = run(["dnf", "repoquery", "--quiet", "--qf", "%{name}\n", "--", *names], 300)
        return set(text.split()) & set(names) if code == 0 else None
    if mgr == "apt":
        code, text, _err = run(["apt-cache", "pkgnames"], 120)
        return set(text.split()) & set(names) if code == 0 else None
    if mgr == "pacman":
        code, text, _err = run(["pacman", "-Slq"], 120)
        return set(text.split()) & set(names) if code == 0 else None
    return None


def installed_packages(mgr: str | None) -> set[str]:
    if mgr in ("zypper", "dnf"):
        return set(out(["rpm", "-qa", "--qf", "%{NAME}\n"]).split())
    if mgr == "apt":
        return set(out(["dpkg-query", "-W", "-f", "${Package}\n"]).split())
    if mgr == "pacman":
        return set(out(["pacman", "-Qq"]).split())
    return set()


def repositories(mgr: str | None) -> list[dict]:
    """Aktive pakkekilder. For zypper: alias, navn, URL og GPG-nøkkel. For dnf: hele .repo-filen."""
    repos = []
    if mgr == "zypper":
        xml = out(["zypper", "-x", "lr"], 60)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
        for r in root.iter("repo"):
            url = (r.findtext("url") or "").strip()
            if r.get("enabled") != "1" or not url:
                continue
            repos.append({"alias": r.get("alias", ""), "name": r.get("name") or r.get("alias", ""), "url": url,
                          "gpgkey": r.get("gpgkey", ""), "priority": r.get("priority", "99")})
    elif mgr == "dnf":
        for f in sorted(Path("/etc/yum.repos.d").glob("*.repo")):
            # Filer som eies av en pakke (fedora.repo osv.) kommer med systemet; ta bare med egne.
            if run(["rpm", "-qf", str(f)])[0] == 0:
                continue
            try:
                text = f.read_text()
            except OSError:
                continue
            m = re.search(r"^baseurl\s*=\s*(\S+)|^metalink\s*=\s*(\S+)", text, re.M)
            url = (m.group(1) or m.group(2)) if m else ""
            repos.append({"alias": f.stem, "name": f.stem, "url": url, "file": f.name, "content": text})
    return repos


# ---------------------------------------------------------------- Flatpak

def flatpak_apps() -> list[dict]:
    if not have("flatpak"):
        return []
    rows = out(["flatpak", "list", "--app", "--columns=application,name,origin,installation"])
    apps = []
    for line in rows.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[0]:
            apps.append({"id": parts[0], "name": parts[1] or parts[0], "origin": parts[2],
                         "installation": "user" if parts[3] == "user" else "system"})
    return apps


def flatpak_remotes() -> list[dict]:
    if not have("flatpak"):
        return []
    remotes = []
    for line in out(["flatpak", "remotes", "--columns=name,url,options"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0]:
            opts = parts[2] if len(parts) > 2 else ""
            remotes.append({"name": parts[0], "url": parts[1],
                            "installation": "user" if "user" in opts else "system"})
    return remotes


# ---------------------------------------------------------------- GNOME-innstillinger

def gsettings_get(schema: str, key: str) -> str:
    return out(["gsettings", "get", schema, key], 10).strip()


def gsettings_set(schema: str, key: str, value: str) -> bool:
    return run(["gsettings", "set", schema, key, value], 10)[0] == 0


def parse_strv(text: str) -> list[str]:
    """Tolker en GVariant-strengliste som ['a', 'b'] eller @as []."""
    found = re.findall(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", text)
    return [re.sub(r"\\(.)", r"\1", a or b) for a, b in found]


def format_strv(items: list[str]) -> str:
    return "[" + ", ".join("'" + i.replace("\\", "\\\\").replace("'", "\\'") + "'" for i in items) + "]"


def uri_to_path(uri: str) -> Path | None:
    uri = uri.strip().strip("'\"")
    if uri.startswith("file://"):
        return Path(urllib.parse.unquote(urllib.parse.urlparse(uri).path))
    if uri.startswith("/"):
        return Path(uri)
    return None


def path_to_uri(path: Path) -> str:
    return "file://" + urllib.parse.quote(str(path))


WALLPAPER_KEYS = [("org.gnome.desktop.background", "picture-uri"),
                  ("org.gnome.desktop.background", "picture-uri-dark"),
                  ("org.gnome.desktop.screensaver", "picture-uri")]


def dconf_dump() -> str:
    return out(["dconf", "dump", "/"], 60) if have("dconf") else ""


def split_dconf(text: str) -> list[tuple[str, list[str]]]:
    """Deler en dconf-dump i (sti, linjer)-par. Hver verdi står på én linje i dumpen."""
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        m = re.fullmatch(r"\[(.*)\]", line.strip())
        if m:
            current = []
            sections.append((m.group(1), current))
        elif current is not None and line.strip():
            current.append(line)
    return sections


DESKTOP_PREFIXES = ("org/gnome/desktop", "org/gnome/shell", "org/gnome/mutter", "org/gnome/settings-daemon",
                    "org/gtk/settings", "org/gtk/gtk4/settings", "org/gnome/system", "org/gnome/login-screen",
                    "org/freedesktop/ibus")


def dconf_category(path: str) -> str:
    if path.startswith("org/gnome/shell/extensions"):
        return "extensions"
    if path.startswith(DESKTOP_PREFIXES):
        return "desktop"
    return "apps"


# Nøkler som Waypoint håndterer selv (bakgrunnsbilde) eller som bare gir mening på maskinen de kom fra.
_SKIP_KEYS = {
    "org/gnome/desktop/background": {"picture-uri", "picture-uri-dark"},
    "org/gnome/desktop/screensaver": {"picture-uri"},
}


def filter_dconf(text: str, categories: set[str]) -> tuple[str, int]:
    """Returnerer dumpen med bare de valgte kategoriene, og antall nøkler som er med."""
    parts, count = [], 0
    for path, lines in split_dconf(text):
        if dconf_category(path) not in categories:
            continue
        skip = _SKIP_KEYS.get(path, set())
        keep = [l for l in lines if l.split("=", 1)[0].strip() not in skip]
        if keep:
            parts.append(f"[{path}]\n" + "\n".join(keep) + "\n")
            count += len(keep)
    return "\n".join(parts), count


# ---------------------------------------------------------------- utvidelser

def _metadata(d: Path) -> dict:
    try:
        return json.loads((d / "metadata.json").read_text())
    except (OSError, ValueError):
        return {}


def extensions() -> list[dict]:
    enabled = parse_strv(gsettings_get("org.gnome.shell", "enabled-extensions"))
    result, seen = [], set()
    if USER_EXT_DIR.is_dir():
        for d in sorted(USER_EXT_DIR.iterdir()):
            meta = _metadata(d)
            if not d.is_dir() or not meta:
                continue
            uuid = meta.get("uuid", d.name)
            seen.add(uuid)
            result.append({"uuid": uuid, "name": meta.get("name", uuid), "description": meta.get("description", ""),
                           "shell_versions": [str(v) for v in meta.get("shell-version", [])],
                           "enabled": uuid in enabled, "system": False, "dir": str(d)})
    # Utvidelser fra systemet som er slått på: vi tar med navnet, ikke filene.
    for uuid in enabled:
        if uuid in seen:
            continue
        for base in SYSTEM_EXT_DIRS:
            meta = _metadata(base / uuid)
            if meta:
                result.append({"uuid": uuid, "name": meta.get("name", uuid), "description": meta.get("description", ""),
                               "shell_versions": [str(v) for v in meta.get("shell-version", [])],
                               "enabled": True, "system": True, "dir": ""})
                break
    return result


def extension_installed(uuid: str) -> bool:
    return any((base / uuid / "metadata.json").exists() for base in [USER_EXT_DIR, *SYSTEM_EXT_DIRS])


# ---------------------------------------------------------------- størrelser og filer

# Mapper som bare er mellomlager og som programmene lager på nytt selv.
CACHE_NAMES = {"cache", "Cache", ".cache", "Cache_Data", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
               "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "CacheStorage", "ScriptCache", "Crash Reports",
               "crashes", "Crashpad", "component_crx_cache", "extensions_crx_cache", "startupCache", "thumbnails",
               "cache2", "GraphiteDawnCache", "logs", "Logs", "tmp", "Temp"}
LOCK_NAMES = {"SingletonLock", "SingletonSocket", "SingletonCookie", "lock", ".parentlock", "parent.lock"}


def skip_entry(name: str, is_dir: bool) -> bool:
    return (is_dir and name in CACHE_NAMES) or name in LOCK_NAMES


def is_backup_name(name: str) -> bool:
    """En eksportfil fra Waypoint (eller Flytt)."""
    return name.startswith(("waypoint-", "flytt-")) and (name.endswith(".tar.gz") or name.endswith(".tar.gz.gpg"))


def is_export_artifact(name: str) -> bool:
    """Waypoint sine egne eksportfiler: ferdige, halvskrevne (.part) og det midlertidige krypterte arkivet med
    private data. De skal aldri tas med i en ny eksport (filen ville vokst for hver gang)."""
    if name.startswith(".waypoint-private-") and name.endswith(".gpg"):
        return True
    return is_backup_name(name[:-5] if name.endswith(".part") else name)


def walk_files(root: Path, skip: frozenset | set = frozenset()):
    """Gir (sti, størrelse) for alle filer, mapper og lenker under root, uten mellomlager. Stier i skip og
    Waypoint sine egne eksportfiler hoppes over."""
    if root in skip:
        return
    if root.is_symlink() or root.is_file():
        try:
            yield root, root.lstat().st_size if root.is_file() else 0
        except OSError:
            pass
        return
    if not root.is_dir():
        return
    yield root, 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not skip_entry(d, True)]
        base = Path(dirpath)
        for d in dirnames:
            yield base / d, 0
        for f in filenames:
            if skip_entry(f, False) or is_export_artifact(f):
                continue
            p = base / f
            if p in skip:
                continue
            try:
                st = p.lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                yield p, st.st_size
            elif stat.S_ISLNK(st.st_mode):
                yield p, 0


def tree_size(root: Path) -> int:
    return sum(size for _, size in walk_files(root))
