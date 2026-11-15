"""Nettverkslagring (NAS): finn servere, logg inn, bla i mapper.

Bruker GNOMEs eget system for nettverksmapper (GVfs, det samme som Filer bruker). Passordet gis til GVfs og
lagres i nøkkelringen («husk for alltid»), så de automatiske kopiene senere kan koble til NAS-en selv med
`gio mount`, uten å spørre. Den valgte mappen nås via GVfs sin FUSE-sti (/run/user/<uid>/gvfs/…), og restic-lageret
legges i undermappen «Waypoint» der, akkurat som på en lokal disk.
"""

from __future__ import annotations

import urllib.parse

from gi.repository import Gio, GLib

from .i18n import N_, _

PROTOCOLS = [("smb", N_("SMB – Windows-deling (de fleste NAS)")), ("sftp", N_("SFTP – SSH"))]
_ATTRS = "standard::name,standard::display-name,standard::type,standard::target-uri,standard::is-hidden"


def clean_server(text: str) -> str:
    """«smb://nas.local/del», «nas.local/», «  nas » → «nas.local» / «nas»."""
    text = text.strip()
    if "://" in text:
        text = urllib.parse.urlparse(text).netloc or text.split("://", 1)[1]
    text = text.split("/")[0]
    return text.rsplit("@", 1)[-1]


def server_uri(protocol: str, server: str, user: str = "") -> str:
    server = clean_server(server)
    if protocol == "sftp":
        return f"sftp://{urllib.parse.quote(user, safe='')}@{server}/" if user else f"sftp://{server}/"
    return f"smb://{server}/"


def discover() -> list[dict]:
    """NAS-er og servere som annonserer seg på nettverket (blokkerer noen sekunder – kjør i en tråd)."""
    found: dict[str, dict] = {}
    try:
        en = Gio.File.new_for_uri("network:///").enumerate_children(_ATTRS, Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error:
        return []
    for info in en:
        target = info.get_attribute_string("standard::target-uri") or ""
        host = urllib.parse.urlparse(target).hostname
        if not host or host in found or "._" in host:  # «nas._webdavs._tcp.local» er en tjeneste, ikke en vert
            continue
        name = (info.get_display_name() or host).split(" (")[0]
        found[host] = {"name": name, "host": host}
    return sorted(found.values(), key=lambda d: d["name"].lower())


def is_not_mounted(err: GLib.Error) -> bool:
    return err.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_MOUNTED)


def mount(uri: str, user: str, password: str, domain: str, on_question, done):
    """Kobler til uri (må kalles fra GUI-tråden). on_question(melding, valg, svar) brukes når serveren spør om noe,
    f.eks. en ukjent SSH-nøkkel; svar(indeks eller None). done(feilmelding eller None)."""
    f = Gio.File.new_for_uri(uri)
    op = Gio.MountOperation()
    tries = [0]

    def ask_password(op, _message, _default_user, _default_domain, flags):
        tries[0] += 1
        if tries[0] > 1:  # samme passord ble avvist – ikke prøv i det uendelige
            op.reply(Gio.MountOperationResult.ABORTED)
            return
        op.set_anonymous(False)
        if user:
            op.set_username(user)
        op.set_password(password)
        if domain and flags & Gio.AskPasswordFlags.NEED_DOMAIN:
            op.set_domain(domain)
        op.set_password_save(Gio.PasswordSave.PERMANENTLY)  # så automatiske kopier kan koble til senere
        op.reply(Gio.MountOperationResult.HANDLED)

    def ask_question(op, message, choices):
        def answer(index):
            if index is None:
                op.reply(Gio.MountOperationResult.ABORTED)
            else:
                op.set_choice(index)
                op.reply(Gio.MountOperationResult.HANDLED)
        on_question(message, list(choices), answer)

    op.connect("ask-password", ask_password)
    op.connect("ask-question", ask_question)

    def finished(src, res):
        try:
            src.mount_enclosing_volume_finish(res)
            done(None)
        except GLib.Error as e:
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                done(None)
            elif tries[0] > 1 or e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.PERMISSION_DENIED):
                done(_("Feil brukernavn eller passord"))
            elif e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.FAILED_HANDLED):
                done(_("Tilkoblingen ble avbrutt"))
            else:
                done(e.message)
    f.mount_enclosing_volume(Gio.MountMountFlags.NONE, op, None, finished)


def remount(uri: str, user: str, password: str, domain: str, on_question, done):
    """Kobler fra uri hvis den er tilkoblet, og så til igjen med innloggingen. Da blir brukernavn og passord sjekket
    og lagret permanent i nøkkelringen, også når delingen allerede var åpen (f.eks. i Filer)."""
    f = Gio.File.new_for_uri(uri)
    try:
        existing = f.find_enclosing_mount(None)
    except GLib.Error:
        existing = None
    if existing is None:
        mount(uri, user, password, domain, on_question, done)
        return

    def unmounted(m, res):
        try:
            m.unmount_with_operation_finish(res)
        except GLib.Error:
            pass  # i bruk e.l.: prøv likevel; ALREADY_MOUNTED regnes da som tilkoblet
        mount(uri, user, password, domain, on_question, done)
    existing.unmount_with_operation(Gio.MountUnmountFlags.NONE, None, None, unmounted)


def list_folders(uri: str) -> list[dict]:
    """Undermapper (og delte mapper på en server) i uri. Kaster GLib.Error, f.eks. NOT_MOUNTED for en deling
    som ikke er koblet til ennå. Blokkerer – kjør i en tråd."""
    f = Gio.File.new_for_uri(uri)
    out = []
    for info in f.enumerate_children(_ATTRS, Gio.FileQueryInfoFlags.NONE, None):
        kind = info.get_file_type()
        if info.get_is_hidden() or kind not in (Gio.FileType.DIRECTORY, Gio.FileType.MOUNTABLE,
                                               Gio.FileType.SHORTCUT):
            continue
        target = info.get_attribute_string("standard::target-uri") or f.get_child(info.get_name()).get_uri()
        out.append({"name": info.get_display_name() or info.get_name(), "uri": target})
    return sorted(out, key=lambda d: d["name"].lower())


def make_folder(uri: str, name: str) -> str:
    child = Gio.File.new_for_uri(uri).get_child_for_display_name(name)
    child.make_directory(None)
    return child.get_uri()


def local_path(uri: str) -> str | None:
    """FUSE-stien til en tilkoblet nettverksmappe (None hvis GVfs-FUSE mangler)."""
    return Gio.File.new_for_uri(uri).get_path()


def parent_uri(uri: str, root: str) -> str | None:
    """Mappen over uri, men aldri over serverens rot."""
    if uri.rstrip("/") == root.rstrip("/"):
        return None
    parent = Gio.File.new_for_uri(uri).get_parent()
    if not parent:
        return None
    p = parent.get_uri()
    return p if len(p.rstrip("/")) >= len(root.rstrip("/")) else root


def display(uri: str) -> str:
    """smb://nas/backup/ivar → nas / backup / ivar"""
    u = urllib.parse.urlparse(uri)
    parts = [u.hostname or ""] + [urllib.parse.unquote(p) for p in u.path.split("/") if p]
    return " / ".join(parts)


def usable(uri: str, protocol: str) -> bool:
    """En SMB-adresse må peke inn i en deling (smb://server/deling/…), ikke på selve serveren."""
    path = [p for p in urllib.parse.urlparse(uri).path.split("/") if p]
    return bool(path) if protocol == "smb" else True
