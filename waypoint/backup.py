"""Finner det som kan tas med, og skriver sikkerhetskopien.

Arkivet er et tar.gz med manifest.json først. Med «krypter hele» er hele arkivet kryptert med gpg
(filnavn .tar.gz.gpg). Ellers ligger private data (nøkler, passord, nettleserprofiler) i et eget,
kryptert arkiv inne i sikkerhetskopien: private.tar.gz.gpg. Private data lagres aldri ukryptert.
"""

from __future__ import annotations

import io
import json
import os
import socket
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import __version__, crypto, system
from .i18n import N_, _
from .model import Group, Item, Section, find_group
from .system import HOME

FORMAT = 2
MB = 1024 * 1024
AUTO_SELECT_LIMIT = 100 * MB  # større programdata må velges bevisst
PRIVATE_MEMBER = "private.tar.gz.gpg"
SENSITIVE_CATEGORIES = {"private", "browsers"}

# (sti i hjemmemappen, navn, kategori)
KNOWN_PATHS = [
    (".local/share/fonts", N_("Skrifttyper"), "appearance"),
    (".fonts", N_("Skrifttyper (eldre plassering)"), "appearance"),
    (".icons", N_("Ikoner og musepekere"), "appearance"),
    (".local/share/icons", N_("Ikontemaer"), "appearance"),
    (".themes", N_("Temaer"), "appearance"),
    (".local/share/themes", N_("Temaer"), "appearance"),
    (".config/gtk-3.0", N_("GTK 3: tema og bokmerker i filbehandleren"), "appearance"),
    (".config/gtk-4.0", N_("GTK 4: tema og stil"), "appearance"),
    (".local/share/backgrounds", N_("Alle bakgrunnsbilder du har lagret"), "appearance"),
    (".config/monitors.xml", N_("Skjermoppsett (oppløsning og plassering)"), "appearance"),
    (".config/autostart", N_("Programmer som starter ved innlogging"), "appearance"),
    (".local/share/applications", N_("Egne programsnarveier"), "appearance"),
    (".config/mimeapps.list", N_("Standardprogrammer for filtyper"), "appearance"),
    (".config/user-dirs.dirs", N_("Plassering av Bilder, Dokumenter osv."), "appearance"),
    (".local/share/nautilus/scripts", N_("Skript i filbehandleren"), "appearance"),
    (".bashrc", N_("Bash-oppsett"), "shell"),
    (".bash_profile", N_("Bash-profil"), "shell"),
    (".bash_aliases", N_("Bash-alias"), "shell"),
    (".profile", N_("Skallprofil"), "shell"),
    (".zshrc", N_("Zsh-oppsett"), "shell"),
    (".zprofile", N_("Zsh-profil"), "shell"),
    (".inputrc", N_("Tastatur i terminalen"), "shell"),
    (".vimrc", N_("Vim-oppsett"), "shell"),
    (".nanorc", N_("Nano-oppsett"), "shell"),
    (".gitconfig", N_("Git-oppsett (navn, e-post, alias)"), "shell"),
    (".config/git", N_("Git-oppsett"), "shell"),
    (".config/fish", N_("Fish-oppsett"), "shell"),
    (".config/starship.toml", N_("Starship-ledetekst"), "shell"),
    (".local/bin", N_("Egne skript og programmer"), "shell"),
    (".mozilla", "Firefox", "browsers"),
    (".config/google-chrome", "Google Chrome", "browsers"),
    (".config/chromium", "Chromium", "browsers"),
    (".config/BraveSoftware", "Brave", "browsers"),
    (".config/vivaldi", "Vivaldi", "browsers"),
    (".config/microsoft-edge", "Microsoft Edge", "browsers"),
    (".config/opera", "Opera", "browsers"),
    (".ssh", N_("SSH-nøkler og -oppsett"), "private"),
    (".gnupg", N_("GPG-nøkler"), "private"),
    (".local/share/keyrings", N_("Lagrede passord (nøkkelring)"), "private"),
    (".config/gh", N_("GitHub CLI-innlogging"), "private"),
    (".config/goa-1.0", N_("Nettkontoer (Google, Microsoft osv.)"), "private"),
]
KNOWN_LABELS = {rel: label for rel, label, _cat in KNOWN_PATHS}

# Mapper i ~/.config som ikke skal tas med som «programinnstillinger».
CONFIG_SKIP = {"dconf", "pulse", "google-chrome-headless", "ibus", "enchant", "session", "gnome-session",
               "evolution", "procps", "QtProject.conf", "menus", "systemd", "waypoint"}

FILE_CATEGORIES = {
    "personal": (N_("Personlige mapper"), N_("Dokumenter, bilder, musikk, videoer og andre mapper i hjemmemappen"),
                 "user-home-symbolic"),
    "appearance": (N_("Utseende og skrivebord"), N_("Skrifttyper, temaer, ikoner, snarveier og oppstartsprogrammer"),
                   "applications-graphics-symbolic"),
    "shell": (N_("Terminal og skall"), N_("Oppsett for bash, git og egne skript"), "utilities-terminal-symbolic"),
    "apps": (N_("Programinnstillinger"), N_("Innstillingsmapper i ~/.config"), "preferences-other-symbolic"),
    "flatpak-data": (N_("Data for Flatpak-apper"), N_("Innstillinger og data i ~/.var/app (uten mellomlager)"),
                     "package-x-generic-symbolic"),
    "browsers": (N_("Nettlesere"), N_("Hele profilen med bokmerker, passord og historikk. Krypteres alltid. "
                                      "Nettleserens egen synkronisering er ofte enklere."), "web-browser-symbolic"),
    "private": (N_("Nøkler og passord"), N_("Krypteres alltid med passordet ditt"), "dialog-password-symbolic"),
}

DCONF_GROUPS = {
    "desktop": (N_("Skrivebord og system"), N_("Tastatursnarveier, tema, mus, tastatur, strøm, varsler og programmappene")),
    "extensions": (N_("Innstillinger for utvidelser"), N_("Det du har stilt inn i hver utvidelse")),
    "apps": (N_("Innstillinger for programmer"), N_("Terminal, filbehandler, tekstredigering og andre GNOME-programmer")),
}


def path_label(rel: str, fallback: str) -> str:
    return _(KNOWN_LABELS[rel]) if rel in KNOWN_LABELS else fallback


def _path_item(rel: str, label: str, category: str, encrypted_store: bool = False,
               measure: bool = True) -> Item | None:
    """encrypted_store: valg for restic-lageret (alt krypteres, ingen størrelsesgrense for standardvalget).
    measure=False hopper over størrelsesmålingen (automatiske kopier trenger den ikke)."""
    p = HOME / rel
    if not (p.exists() or p.is_symlink()):
        return None
    size = system.tree_size(p) if measure else 0
    if p.is_dir() and not p.is_symlink() and size == 0 and not any(p.iterdir()):
        return None
    if measure and category == "browsers" and size < 10 * 1024:
        return None  # bare en tom profilmappe
    sensitive = category in SENSITIVE_CATEGORIES
    selected = (not sensitive or (encrypted_store and category == "private")) and \
        (encrypted_store or size <= AUTO_SELECT_LIMIT)
    return Item(id=rel, label=label, detail="~/" + rel, size=size, selected=selected, sensitive=sensitive,
                data={"path": rel, "category": category})


def personal_folders() -> list[tuple[str, str]]:
    """Mappene i hjemmemappen som hører til brukeren selv: Dokumenter, Bilder osv. og andre synlige mapper."""
    from gi.repository import GLib
    special = []
    for d in (GLib.UserDirectory.DIRECTORY_DESKTOP, GLib.UserDirectory.DIRECTORY_DOCUMENTS,
              GLib.UserDirectory.DIRECTORY_DOWNLOAD, GLib.UserDirectory.DIRECTORY_PICTURES,
              GLib.UserDirectory.DIRECTORY_MUSIC, GLib.UserDirectory.DIRECTORY_VIDEOS,
              GLib.UserDirectory.DIRECTORY_TEMPLATES, GLib.UserDirectory.DIRECTORY_PUBLIC_SHARE):
        p = GLib.get_user_special_dir(d)
        if p and Path(p).is_dir() and Path(p) != HOME and Path(p).parent == HOME:
            special.append(Path(p).name)
    others = []
    try:
        for p in sorted(HOME.iterdir(), key=lambda p: p.name.lower()):
            if p.name.startswith(".") or not p.is_dir() or p.is_symlink() or p.name in special:
                continue
            others.append(p.name)
    except OSError:
        pass
    return [(n, n) for n in special + others]


def scan(progress=lambda text: None, personal: bool = True, measure: bool = True) -> list[Section]:
    """Leser alt som kan sikkerhetskopieres. Tar noen sekunder (mappestørrelser og pakkelister).
    personal: valg for restic-lageret – personlige mapper og nøkler er valgt på forhånd, og standardvalget avhenger
    ikke av størrelse. measure=False hopper over størrelsesmålingen (brukes av den automatiske kopien)."""
    sections = []

    # --- Skrivebordet
    progress(_("Leser GNOME-innstillinger …"))
    dump = system.dconf_dump()
    settings = Group("settings", _("GNOME-innstillinger"), icon="preferences-desktop-appearance-symbolic", flat=True)
    for cat, (title, desc) in DCONF_GROUPS.items():
        _text, count = system.filter_dconf(dump, {cat})
        if count:
            settings.items.append(Item(id=cat, label=_(title), detail=_("{} · {} verdier").format(_(desc), count)))
    wallpaper = Group("wallpaper", _("Bakgrunnsbilde"), icon="preferences-desktop-wallpaper-symbolic", flat=True)
    files = {}
    for schema, key in system.WALLPAPER_KEYS:
        p = system.uri_to_path(system.gsettings_get(schema, key))
        if p and p.is_file():
            files[f"{schema} {key}"] = str(p)
    if files:
        first = Path(next(iter(files.values())))
        size = sum(Path(f).stat().st_size for f in set(files.values()))
        label = _("Bakgrunn og låseskjerm") if len(set(files.values())) == 1 else \
            _("Bakgrunnsbilder (lys, mørk og låseskjerm)")
        wallpaper.items.append(Item(id="wallpaper", label=label, detail=first.name, size=size,
                                    data={"keys": files, "preview": str(first)}))
    sections.append(Section(_("Skrivebordet"), _("Slik GNOME ser ut og oppfører seg"),
                            [g for g in (settings, wallpaper) if g.items]))

    # --- Utvidelser
    progress(_("Leser utvidelser …"))
    ext = Group("extensions", _("GNOME-utvidelser"), icon="application-x-addon-symbolic",
                subtitle=_("Lagres med filer, og hentes på nytt fra extensions.gnome.org hvis GNOME er oppdatert"))
    for e in system.extensions():
        size = system.tree_size(Path(e["dir"])) if e["dir"] and measure else 0
        detail = e["uuid"] + " · " + (_("slått på") if e["enabled"] else _("slått av"))
        if e["system"]:
            detail += " · " + _("følger med systemet")
        ext.items.append(Item(id=e["uuid"], label=e["name"], detail=detail, size=size,
                              selected=e["enabled"] or not e["system"], data=e))
    sections.append(Section(_("Utvidelser"), _("Utvidelsene du har installert, og om de er slått på"),
                            [ext] if ext.items else []))

    # --- Programmer
    progress(_("Leser installerte programmer …"))
    mgr = system.package_manager()
    apps_section = Section(_("Programmer"), _("Navn på programmene dine, så de kan installeres på nytt"))
    if mgr:
        pkgs = Group("packages", _("Systempakker"), subtitle=_("Installert med {}").format(system.PM_NAMES[mgr]),
                     icon="system-software-install-symbolic")
        for p in system.user_packages(mgr):
            pkgs.items.append(Item(id=p["name"], label=p["name"], detail=p["summary"], data=p))
        repos = Group("repos", _("Pakkekilder"), subtitle=_("Ekstra kilder som Packman, Google Chrome og NVIDIA"),
                      icon="network-server-symbolic")
        for r in system.repositories(mgr):
            repos.items.append(Item(id=r["alias"], label=r["name"], detail=r["url"], data=r))
        apps_section.groups += [g for g in (pkgs, repos) if g.items]
    progress(_("Leser Flatpak-apper …"))
    fp = Group("flatpaks", _("Flatpak-apper"), subtitle=_("Fra Flathub og andre Flatpak-kilder"),
               icon="package-x-generic-symbolic", data={"remotes": system.flatpak_remotes()})
    for a in system.flatpak_apps():
        fp.items.append(Item(id=a["id"], label=a["name"], detail=f"{a['id']} · {a['origin']}", data=a))
    if fp.items:
        apps_section.groups.append(fp)
    sections.append(apps_section)

    # --- Personlige mapper
    mine = Group("files-personal", _("Personlige mapper"),
                 subtitle=_("Dokumenter, bilder, musikk, videoer og andre mapper i hjemmemappen"), icon="user-home-symbolic")
    for rel, label in personal_folders():
        progress(_("Måler {} …").format("~/" + rel))
        p = HOME / rel
        size = system.tree_size(p) if measure else 0
        mine.items.append(Item(id=rel, label=label, detail="~/" + rel, size=size, selected=personal,
                               data={"path": rel, "category": "personal"}))
    sections.append(Section(_("Dine filer"), _("Dokumenter, bilder og andre mapper du har laget selv"),
                            [mine] if mine.items else []))

    # --- Filer
    progress(_("Måler mapper i hjemmemappen …"))
    by_cat: dict[str, Group] = {}
    for cat, (title, desc, icon) in FILE_CATEGORIES.items():
        if cat == "personal":
            continue
        by_cat[cat] = Group("files-" + cat, _(title), subtitle=_(desc), icon=icon)
    claimed = set()
    for rel, label, cat in KNOWN_PATHS:
        claimed.add(rel)
        item = _path_item(rel, _(label), cat, encrypted_store=personal, measure=measure)
        if item:
            by_cat[cat].items.append(item)
    cfg = HOME / ".config"
    if cfg.is_dir():
        for p in sorted(cfg.iterdir(), key=lambda p: p.name.lower()):
            rel = f".config/{p.name}"
            if rel in claimed or p.name in CONFIG_SKIP or p.name.startswith(".") or p.name.endswith((".lock", "~")) \
                    or p.is_socket():
                continue
            progress(_("Måler {} …").format("~/" + rel))
            item = _path_item(rel, p.name, "apps", encrypted_store=personal, measure=measure)
            if item:
                by_cat["apps"].items.append(item)
    var_app = HOME / ".var/app"
    if var_app.is_dir():
        names = {a.id: a.label for a in fp.items}
        for p in sorted(var_app.iterdir()):
            if not p.is_dir():
                continue
            progress(_("Måler {} …").format("~/.var/app/" + p.name))
            item = _path_item(f".var/app/{p.name}", names.get(p.name, p.name), "flatpak-data",
                              encrypted_store=personal, measure=measure)
            if item:
                by_cat["flatpak-data"].items.append(item)
    sections.append(Section(_("Filer og oppsett"), _("Innstillingsfiler fra hjemmemappen din"),
                            [g for g in by_cat.values() if g.items]))
    progress("")
    for s in sections:
        for g in s.groups:
            for i in g.items:
                i.default = i.selected
    return sections


def default_filename(encrypted: bool = False, stamp: bool = False) -> str:
    host = socket.gethostname().split(".")[0] or "linux"
    when = datetime.now().strftime("%Y-%m-%d_%H%M%S" if stamp else "%Y-%m-%d")
    return f"waypoint-{host}-{when}.tar.gz" + (".gpg" if encrypted else "")


def default_folder() -> Path:
    """En minnepinne hvis det står en i, ellers hjemmemappen."""
    media = Path("/run/media") / os.environ.get("USER", "")
    try:
        drives = [d for d in media.iterdir() if d.is_dir() and os.access(d, os.W_OK)]
    except OSError:
        drives = []
    return drives[0] if drives else HOME


def estimate(sections: list[Section]) -> tuple[int, int]:
    """(antall valgte elementer, omtrentlig ukomprimert størrelse)."""
    n = size = 0
    for s in sections:
        for g in s.groups:
            for i in g.selected():
                n += 1
                size += i.size
    return n, size


def needs_password(sections: list[Section], encrypt_all: bool) -> bool:
    return encrypt_all or any(i.sensitive for s in sections for g in s.groups for i in g.selected())


class Cancelled(Exception):
    pass


def build_manifest(sections: list[Section], encrypt_all: bool) -> dict:
    mgr = system.package_manager()
    m = {
        "format": FORMAT, "app": "waypoint", "app_version": __version__,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "hostname": socket.gethostname(), "user": os.environ.get("USER", ""), "home": str(HOME),
        "distro": system.os_release(), "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "shell_version": system.shell_version(), "encrypted": "all" if encrypt_all else None,
    }
    g = find_group(sections, "settings")
    cats = {i.id for i in g.selected()} if g else set()
    if cats:
        m["settings"] = {"categories": sorted(cats)}
    g = find_group(sections, "wallpaper")
    if g and g.selected():
        keys, stored = {}, {}
        for k, path in g.selected()[0].data["keys"].items():
            if path not in stored:
                stored[path] = f"wallpaper/{len(stored)}-{Path(path).name}"
            keys[k] = {"original": path, "file": stored[path]}
        m["wallpaper"] = {"keys": keys}
    g = find_group(sections, "extensions")
    if g and g.selected():
        m["extensions"] = []
        for i in g.selected():
            e = i.data
            entry = {k: e[k] for k in ("uuid", "name", "description", "shell_versions", "enabled", "system")}
            entry["files"] = bool(e["dir"])
            m["extensions"].append(entry)
    g = find_group(sections, "packages")
    if g and g.selected():
        m["packages"] = {"manager": mgr, "items": [i.data for i in g.selected()]}
    g = find_group(sections, "repos")
    if g and g.selected():
        m["repos"] = {"manager": mgr, "items": [i.data for i in g.selected()]}
    g = find_group(sections, "flatpaks")
    if g and g.selected():
        used = {i.data["origin"] for i in g.selected()}
        m["flatpaks"] = {"remotes": [r for r in g.data.get("remotes", []) if r["name"] in used],
                         "items": [i.data for i in g.selected()]}
    m["files"] = []
    for s in sections:
        for g in s.groups:
            if g.id.startswith("files-"):
                for i in g.selected():
                    m["files"].append({"path": i.data["path"], "label": i.label, "category": i.data["category"],
                                       "size": i.size, "sensitive": i.sensitive,
                                       "encrypted": i.sensitive or encrypt_all})
    if any(f["sensitive"] for f in m["files"]) and not encrypt_all:
        m["private"] = {"member": PRIVATE_MEMBER}
    return m


class _ExactReader:
    """Leser nøyaktig size byte fra en fil. Krymper filen mens den leses (eller gir lesefeil), fylles resten med
    null-byte, så arkivet forblir gyldig – tarfile har allerede skrevet headeren med den opprinnelige størrelsen."""

    def __init__(self, f, size: int):
        self.f, self.left = f, size

    def read(self, n: int = -1) -> bytes:
        if n < 0 or n > self.left:
            n = self.left
        try:
            data = self.f.read(n)
        except OSError:
            data = b""
        data = data[:n] + b"\0" * (n - len(data))
        self.left -= n
        return data


def _add_entry(t: tarfile.TarFile, path: Path, arc: str):
    """Som TarFile.add(recursive=False), men robust mot filer som endres mens de leses."""
    info = t.gettarinfo(str(path), arcname=arc)
    if info is None:  # sokler o.l.
        return
    if info.isreg():
        with open(path, "rb") as f:
            t.addfile(info, _ExactReader(f, info.size))
    else:
        t.addfile(info)


def write_backup(sections: list[Section], dest: Path, password: str | None = None, encrypt_all: bool = False,
                 progress=lambda frac, text: None, cancelled=lambda: False) -> dict:
    """Skriver arkivet. progress(andel 0–1, tekst) kalles jevnlig. Returnerer manifestet."""
    if needs_password(sections, encrypt_all) and not password:
        raise ValueError(_("Et passord trengs for å kryptere sikkerhetskopien."))
    _n, total = estimate(sections)
    total = max(total, 1)
    done = 0
    manifest = build_manifest(sections, encrypt_all)
    g = find_group(sections, "settings")
    cats = {i.id for i in g.selected()} if g else set()
    dconf_text = ""
    if cats:
        dconf_text, count = system.filter_dconf(system.dconf_dump(), cats)
        manifest["settings"]["keys"] = count
    wallpaper = {v["original"]: v["file"] for v in manifest.get("wallpaper", {}).get("keys", {}).values()}
    ext_dirs = [(Path(i.data["dir"]), i.data["uuid"]) for i in (find_group(sections, "extensions").selected()
                if find_group(sections, "extensions") else []) if i.data["dir"]]
    public = [f for f in manifest["files"] if not (f["sensitive"] and not encrypt_all)]
    private = [f for f in manifest["files"] if f["sensitive"] and not encrypt_all]

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    gpg = None
    blob = None
    if encrypt_all:
        gpg = crypto.encrypt_to(tmp, password)
        tar = tarfile.open(fileobj=gpg.proc.stdin, mode="w|gz")
    else:
        tar = tarfile.open(tmp, "w:gz", compresslevel=6)

    def add_bytes(t, name: str, data: bytes):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mtime = int(time.time())
        info.mode = 0o644
        t.addfile(info, io.BytesIO(data))

    def add_tree(t, src: Path, arc_prefix: str):
        nonlocal done
        for p, size in system.walk_files(src):
            if cancelled():
                raise Cancelled()
            arc = arc_prefix + str(p.relative_to(src)) if p != src else arc_prefix.rstrip("/")
            try:
                _add_entry(t, p, arc)
            except OSError:
                continue  # fil som forsvant eller ikke kan leses (før noe er skrevet til arkivet)
            done += size
            progress(min(done / total, 1.0), str(p).replace(str(HOME), "~", 1))

    try:
        add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False).encode())
        if dconf_text:
            progress(0, _("GNOME-innstillinger"))
            add_bytes(tar, "dconf.ini", dconf_text.encode())
        for path, arc in wallpaper.items():
            tar.add(path, arcname=arc)
            done += Path(path).stat().st_size
        for d, uuid in ext_dirs:
            add_tree(tar, d, f"extensions/{uuid}/")
        if private:
            # Private data krypteres i et eget arkiv før de legges inn; klarteksten skrives aldri til disk.
            progress(done / total, _("Krypterer nøkler og passord"))
            fd, name = tempfile.mkstemp(prefix=".waypoint-private-", suffix=".gpg", dir=dest.parent)
            os.close(fd)
            blob = Path(name)
            with crypto.encrypt_to(blob, password) as inner:
                itar = tarfile.open(fileobj=inner.proc.stdin, mode="w|gz")
                for f in private:
                    add_tree(itar, HOME / f["path"], f"home/{f['path']}/")
                itar.close()
                crypto.raise_for(*inner.finish())
            tar.add(blob, arcname=PRIVATE_MEMBER)
            blob.unlink()
            blob = None
        for f in public:
            add_tree(tar, HOME / f["path"], f"home/{f['path']}/")
        progress(1.0, _("Fullfører"))
        tar.close()
        if gpg:
            crypto.raise_for(*gpg.finish())
            gpg.cleanup()
            gpg = None
        os.replace(tmp, dest)
        return manifest
    except BaseException:
        try:
            tar.close()
        except Exception:  # noqa: BLE001
            pass
        if gpg:
            gpg.kill()
            gpg.cleanup()
        if blob:
            blob.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- restic

def prepare_snapshot(sections: list[Section], setup_dir: Path) -> tuple[list[str], dict]:
    """Skriver oppsettet (manifest og GNOME-innstillinger) til setup_dir og returnerer stiene restic skal
    ta med: setup_dir, valgte mapper i hjemmemappen, utvidelser og bakgrunnsbilder."""
    manifest = build_manifest(sections, encrypt_all=True)
    manifest["encrypted"] = "restic"
    manifest["setup_dir"] = str(setup_dir)
    g = find_group(sections, "extensions")
    dirs = {i.data["uuid"]: i.data["dir"] for i in (g.selected() if g else []) if i.data.get("dir")}
    for e in manifest.get("extensions", []):
        e["dir"] = dirs.get(e["uuid"], "")
    for v in manifest.get("wallpaper", {}).get("keys", {}).values():
        v["file"] = v["original"]
    if setup_dir.exists():
        import shutil
        shutil.rmtree(setup_dir)
    setup_dir.mkdir(parents=True)
    cats = set(manifest.get("settings", {}).get("categories", []))
    if cats:
        text, count = system.filter_dconf(system.dconf_dump(), cats)
        manifest["settings"]["keys"] = count
        (setup_dir / "dconf.ini").write_text(text)
    (setup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    paths = [str(setup_dir)] + [str(HOME / f["path"]) for f in manifest["files"]]
    paths += [d for d in dirs.values()]
    paths += sorted({v["original"] for v in manifest.get("wallpaper", {}).get("keys", {}).values()})
    # Fjern stier som allerede ligger inne i en annen sti på listen.
    unique: list[str] = []
    for p in sorted(set(paths), key=len):
        if not any(p == u or p.startswith(u.rstrip("/") + "/") for u in unique):
            unique.append(p)
    return unique, manifest


# Mapper der programmer lager mellomlager og låsefiler. Mønstrene for disse navnene gjelder bare her, aldri i
# personlige mapper (en mappe som heter «Cache» eller «lock» i Dokumenter skal med).
APP_DATA_ROOTS = (".config", ".local/share", ".var/app", ".mozilla", ".thunderbird")


def exclude_patterns(user_patterns: list[str], extra_paths: list[str] = ()) -> list[str]:
    """Mønstre restic skal hoppe over: mellomlager og låsefiler i programdata, brukerens egne mønstre og
    lagringsstedet selv. Stier fra systemet escapes, så tegn som [ ] * ? i navn tolkes bokstavelig."""
    from .repo import glob_escape
    home = glob_escape(str(HOME))
    names = sorted(system.CACHE_NAMES | system.LOCK_NAMES)
    pats = [f"{home}/{root}/**/{glob_escape(n)}" for root in APP_DATA_ROOTS for n in names]
    pats += [home + p[1:] if p.startswith("~/") else p for p in user_patterns if p.strip()]
    pats += [glob_escape(p) for p in extra_paths]
    return pats
