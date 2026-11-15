"""Dialogen for å velge og koble til lagringsstedet (restic-lageret).

To typer: en mappe på denne maskinen eller en USB-disk, eller en mappe på en NAS i nettverket (SMB eller SFTP).
For NAS logger brukeren inn med brukernavn og passord og blar seg frem til mappen – ingen stier å skrive.
"""

from __future__ import annotations

import threading
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from . import crypto, network, repo as repo_mod, settings
from .i18n import N_, _

KINDS = [("local", N_("Denne maskinen eller USB-disk")), ("network", N_("Nettverk (NAS)"))]


class StorageDialog(Adw.Dialog):
    """Velg disk/USB eller NAS og koble til.

    mode="job": lagringssted for en oppgave; finnes det ingen sikkerhetskopier der, opprettes et nytt lager.
    mode="restore": finn sikkerhetskopier for å gjenopprette; det opprettes aldri noe nytt lager, og et annet passord
    enn det i nøkkelringen brukes bare for denne gangen (legges i cfg["_password"], som aldri lagres)."""

    def __init__(self, win, on_done=lambda cfg: None, current: dict | None = None, mode: str = "job"):
        """on_done(cfg) kalles med det valgte lagringsstedet når tilkoblingen er godkjent. current: stedet som er
        valgt fra før (for å fylle ut feltene)."""
        super().__init__(title=_("Lagringssted"), content_width=580, content_height=720)
        self.win = win
        self.on_done = on_done
        self.mode = mode
        self._remounted: set[str] = set()
        cfg = dict(current or {})
        net = cfg.get("network") or {}
        self.folder = {"path": cfg.get("path"), "uri": cfg.get("uri")} if cfg.get("path") and not net else {}
        self.browse_root: str | None = None   # serverens rot (smb://nas/ eller sftp://bruker@nas/)
        self.browse_uri: str | None = None    # mappen vi står i
        self.net_login: dict = {}

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.toasts = Adw.ToastOverlay()
        page = Adw.PreferencesPage()
        self.toasts.set_child(page)
        tv.set_content(self.toasts)
        self.set_child(tv)
        self.banner = Adw.Banner(title=_("restic er ikke installert. Det trengs for sikkerhetskopiene."),
                                 button_label=_("Installer"))
        self.banner.connect("button-clicked", self._install)
        tv.add_top_bar(self.banner)
        self.banner.set_revealed(repo_mod.restic_path() is None)

        intro = Adw.PreferencesGroup(description=_("Bare nye og endrede filer lagres hver gang, og alt krypteres med "
                                                   "passordet ditt før det forlater maskinen."))
        page.add(intro)
        g = Adw.PreferencesGroup()
        self.kinds = [k for k, _l in KINDS]
        self.kind = Adw.ComboRow(title=_("Hvor"), model=Gtk.StringList.new([_(lbl) for _k, lbl in KINDS]))
        self.kind.set_selected(1 if net else 0)
        self.kind.connect("notify::selected", lambda *_a: self._show())
        g.add(self.kind)
        page.add(g)

        # --- Denne maskinen / USB
        self.local = Adw.PreferencesGroup(description=_("Velg en mappe på en ekstern disk, en minnepinne eller denne "
                                                        "maskinen. Waypoint lager mappen «Waypoint» der."))
        self.folder_row = Adw.ActionRow(title=_("Mappe"), use_markup=False)
        b = Gtk.Button(label=_("Velg …"), valign=Gtk.Align.CENTER)
        b.connect("clicked", self._pick_folder)
        self.folder_row.add_suffix(b)
        self.local.add(self.folder_row)
        page.add(self.local)

        # --- Nettverk: finn, logg inn
        self.found = Adw.PreferencesGroup(title=_("Funnet på nettverket"))
        self.found_rows: list[Gtk.Widget] = []
        page.add(self.found)
        self.found.set_visible(False)

        now = _("Nå: {}").format(network.display(cfg["uri"])) + "\n" if net and cfg.get("uri") else ""
        self.net = Adw.PreferencesGroup(title=_("Logg inn på NAS-en"),
                                        description=now + _("Passordet lagres i nøkkelringen, så Waypoint kan koble "
                                                            "til NAS-en selv når det er tid for en kopi."))
        self.protocols = [k for k, _l in network.PROTOCOLS]
        self.protocol = Adw.ComboRow(title=_("Tilkobling"),
                                     model=Gtk.StringList.new([_(lbl) for _k, lbl in network.PROTOCOLS]))
        self.protocol.set_selected(self.protocols.index(net["protocol"]) if net.get("protocol") in self.protocols else 0)
        self.protocol.connect("notify::selected", lambda *_a: self._show())
        self.server = Adw.EntryRow(title=_("Server (navn eller IP-adresse)"), text=net.get("server", ""))
        self.user = Adw.EntryRow(title=_("Brukernavn"), text=net.get("user", ""))
        self.password = Adw.PasswordEntryRow(title=_("Passord"))
        self.domain = Adw.EntryRow(title=_("Domene (valgfritt)"), text=net.get("domain", ""))
        for r in (self.protocol, self.server, self.user, self.password, self.domain):
            self.net.add(r)
        self.password.connect("entry-activated", lambda *_a: self._net_connect())
        login_box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=12)
        self.login_btn = Gtk.Button(label=_("Koble til og bla i mappene"))
        self.login_btn.add_css_class("pill")
        self.login_btn.connect("clicked", lambda *_a: self._net_connect())
        self.login_spin = Adw.Spinner(visible=False)
        login_box.append(self.login_btn)
        login_box.append(self.login_spin)
        self.net_after = Adw.PreferencesGroup()
        self.net_after.add(login_box)
        page.add(self.net)
        page.add(self.net_after)

        # --- Nettverk: tilkoblet (erstatter innloggingen) og mappevelger
        self.connected = Adw.PreferencesGroup()
        self.connected_row = Adw.ActionRow(use_markup=False)
        self.connected_row.add_prefix(Gtk.Image(icon_name="network-server-symbolic"))
        switch = Gtk.Button(label=_("Bytt"), valign=Gtk.Align.CENTER)
        switch.add_css_class("flat")
        switch.connect("clicked", lambda *_a: self._disconnect())
        self.connected_row.add_suffix(switch)
        self.connected.add(self.connected_row)
        page.add(self.connected)
        self.page = page
        self.browse = Adw.PreferencesGroup(title=_("Velg mappe på NAS-en"))
        self.browse_rows: list[Gtk.Widget] = []
        page.add(self.browse)
        self.browse.set_visible(False)

        # --- Passord for sikkerhetskopiene
        self.pw_group = Adw.PreferencesGroup(title=_("Passord for sikkerhetskopiene"))
        self.stored = settings.get_password()
        self.pw_stored = Adw.ActionRow(title=_("Bruker passordet som er lagret i nøkkelringen"), use_markup=False)
        # Alle oppgavenes lagre bruker det ene passordet i nøkkelringen. Et annet passord for en ny oppgave ville
        # gjort de andre lagrene uleselige, så det kan bare byttes for alle samtidig (Innstillinger → Kryptering).
        self.password_locked = self.mode == "job" and settings.password_in_use()
        if self.password_locked:
            self.pw_stored.set_subtitle(_("Alle oppgaver bruker samme passord. Det kan endres for alle under "
                                          "Innstillinger → Kryptering."))
            self.pw_stored.set_subtitle_lines(3)
        else:
            other = Gtk.Button(label=_("Bruk et annet …"), valign=Gtk.Align.CENTER)
            other.add_css_class("flat")
            other.connect("clicked", lambda *_a: self._new_password())
            self.pw_stored.add_suffix(other)
        self.pw1 = Adw.PasswordEntryRow(title=_("Passord"))
        self.pw2 = Adw.PasswordEntryRow(title=_("Gjenta passordet (bare for et nytt lager)"))
        self.pw_hint = Adw.ActionRow(title=_("Ta vare på passordet"), use_markup=False,
                                     subtitle=_("Uten passordet kan ingen åpne det som er kryptert – heller ikke du. "
                                                "Skriv det ned et trygt sted."))
        for w in (self.pw_stored, self.pw1, self.pw2, self.pw_hint):
            self.pw_group.add(w)
        page.add(self.pw_group)
        self.using_stored = bool(self.stored)

        btn_group = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.connect_btn = Gtk.Button(label=_("Bruk dette lagringsstedet"), halign=Gtk.Align.CENTER)
        self.connect_btn.add_css_class("pill")
        self.connect_btn.add_css_class("suggested-action")
        self.connect_btn.connect("clicked", self._connect)
        self.spinner = Adw.Spinner(visible=False, halign=Gtk.Align.CENTER)
        box.append(self.connect_btn)
        box.append(self.spinner)
        btn_group.add(box)
        page.add(btn_group)
        self._show()
        self._show_pw()
        threading.Thread(target=self._discover, daemon=True).start()

    # ------------------------------------------------------------ visning

    def _kind(self) -> str:
        return self.kinds[self.kind.get_selected()]

    def _protocol(self) -> str:
        return self.protocols[self.protocol.get_selected()]

    def _show(self):
        local = self._kind() == "local"
        self.local.set_visible(local)
        logged_in = self.browse_root is not None
        for w in (self.net, self.net_after):
            w.set_visible(not local and not logged_in)
        self.found.set_visible(not local and not logged_in and bool(self.found_rows))
        self.connected.set_visible(not local and logged_in)
        self.browse.set_visible(not local and self.browse_uri is not None)
        self.domain.set_visible(self._protocol() == "smb")
        self.folder_row.set_subtitle(self.folder.get("path") or _("Ikke valgt"))
        self._update_connect()

    def _update_connect(self):
        if self._kind() == "local":
            ok = bool(self.folder.get("path")) and (self.folder.get("uri") or "file://").startswith("file://")
        else:
            ok = self.browse_uri is not None and network.usable(self.browse_uri, self._protocol())
        self.connect_btn.set_sensitive(ok)

    def _show_pw(self):
        self.pw_stored.set_visible(self.using_stored)
        for w in (self.pw1, self.pw2, self.pw_hint):
            w.set_visible(not self.using_stored)
        if self.mode == "restore":  # gjenoppretting lager aldri et nytt lager, så passordet skrives bare én gang
            self.pw2.set_visible(False)
            self.pw1.set_title(_("Passordet for sikkerhetskopiene"))

    def _new_password(self):
        self.using_stored = False
        self._show_pw()

    def _toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=6))

    # ------------------------------------------------------------ lokal mappe

    def _pick_folder(self, *_a):
        dlg = Gtk.FileDialog(title=_("Velg hvor sikkerhetskopiene skal lagres"))
        media = Path("/run/media") / (GLib.get_user_name() or "")
        start = self.folder.get("path") if self.folder.get("path") and Path(self.folder["path"]).is_dir() else \
            (str(media) if media.is_dir() else None)
        if start:
            dlg.set_initial_folder(Gio.File.new_for_path(start))

        def chosen(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if not f.get_path() or not f.get_uri().startswith("file://"):
                self._toast(_("Velg «Nettverk (NAS)» for mapper på en NAS"))
                return
            self.folder = {"path": f.get_path(), "uri": f.get_uri()}
            self._show()
        dlg.select_folder(self.win, None, chosen)

    # ------------------------------------------------------------ nettverk

    def _discover(self):
        found = network.discover()
        GLib.idle_add(self._show_found, found)

    def _show_found(self, found):
        for r in self.found_rows:
            self.found.remove(r)
        self.found_rows = []
        for item in found:
            row = Adw.ActionRow(title=item["name"], subtitle=item["host"], activatable=True, use_markup=False)
            row.add_prefix(Gtk.Image(icon_name="network-server-symbolic"))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, h=item["host"]: (self.server.set_text(h), self.user.grab_focus()))
            self.found.add(row)
            self.found_rows.append(row)
        self._show()
        return False

    def _ask(self, message: str, choices: list[str], answer):
        """Spørsmål fra serveren, f.eks. om en ukjent SSH-nøkkel skal godtas."""
        d = Adw.AlertDialog(heading=_("Spørsmål fra serveren"), body=message)
        for i, c in enumerate(choices):
            d.add_response(str(i), c)
        d.add_response("cancel", _("Avbryt"))
        d.set_close_response("cancel")
        d.connect("response", lambda _d, r: answer(None if r == "cancel" else int(r)))
        d.present(self)

    def _net_connect(self):
        server = network.clean_server(self.server.get_text())
        user = self.user.get_text().strip()
        if not server:
            self._toast(_("Skriv navnet eller IP-adressen til NAS-en"))
            return
        if self._protocol() == "sftp" and not user:
            self._toast(_("SFTP trenger et brukernavn"))
            return
        root = network.server_uri(self._protocol(), server, user)
        self.net_login = {"protocol": self._protocol(), "server": server, "user": user,
                          "domain": self.domain.get_text().strip()}
        self.login_btn.set_sensitive(False)
        self.login_spin.set_visible(True)

        def done(err):
            self.login_btn.set_sensitive(True)
            self.login_spin.set_visible(False)
            if err:
                self._toast(err)
                return
            self.browse_root = root
            self.connected_row.set_title(_("Tilkoblet til {}").format(server))
            self.connected_row.set_subtitle(user or "")
            self._open(root)
            self.page.scroll_to_top()
        network.mount(root, user, self.password.get_text(), self.net_login["domain"], self._ask, done)

    def _disconnect(self):
        """Tilbake til innloggingen (for å bytte server eller bruker)."""
        self.browse_root = None
        self.browse_uri = None
        self._show()

    def _open(self, uri: str):
        """Viser mappene i uri. En delt mappe som ikke er koblet til ennå, kobles til med samme innlogging."""
        self.browse_uri = uri
        self.browse.set_description(network.display(uri))
        self._set_browse_rows([Adw.ActionRow(title=_("Henter mapper …"))])
        self._show()

        def work():
            try:
                return network.list_folders(uri), None
            except GLib.Error as e:
                return None, e

        def done(folders, err):
            if err is not None and network.is_not_mounted(err) and uri not in self._remounted:
                # En delt mappe som ikke er koblet til: koble til med samme innlogging, og prøv én gang til.
                self._remounted.add(uri)
                network.mount(uri, self.net_login.get("user", ""), self.password.get_text(),
                              self.net_login.get("domain", ""), self._ask,
                              lambda e: self._show_browse_error(e) if e else self._open(uri))
                return False
            if err is not None:
                self._set_browse_rows([Adw.ActionRow(title=err.message, use_markup=False)])
                return False
            self._fill_browse(uri, folders)
            return False
        threading.Thread(target=lambda: GLib.idle_add(done, *work()), daemon=True).start()

    def _show_browse_error(self, text: str):
        self._set_browse_rows([Adw.ActionRow(title=text, use_markup=False)])
        self._toast(text)

    def _set_browse_rows(self, rows):
        for r in self.browse_rows:
            self.browse.remove(r)
        self.browse_rows = list(rows)
        for r in rows:
            self.browse.add(r)

    def _fill_browse(self, uri: str, folders: list[dict]):
        rows = []
        parent = network.parent_uri(uri, self.browse_root or uri)
        if parent:
            up = Adw.ActionRow(title=_("Opp én mappe"), activatable=True)
            up.add_prefix(Gtk.Image(icon_name="go-up-symbolic"))
            up.connect("activated", lambda *_a: self._open(parent))
            rows.append(up)
        for f in folders:
            row = Adw.ActionRow(title=f["name"], activatable=True, use_markup=False)
            row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, u=f["uri"]: self._open(u))
            rows.append(row)
        if not folders:
            rows.append(Adw.ActionRow(title=_("Ingen undermapper")))
        protocol = self.net_login.get("protocol", "smb")
        if network.usable(uri, protocol):
            new = Adw.EntryRow(title=_("Ny mappe her"), show_apply_button=True)
            new.add_prefix(Gtk.Image(icon_name="folder-new-symbolic"))

            def create(row):
                name = row.get_text().strip()
                if not name:
                    return
                try:
                    self._open(network.make_folder(uri, name))
                except GLib.Error as e:
                    self._toast(e.message)
            new.connect("apply", create)
            rows.append(new)
            chosen = Adw.ActionRow(title=_("Valgt mappe"), subtitle=network.display(uri), use_markup=False)
            chosen.add_prefix(Gtk.Image(icon_name="object-select-symbolic"))
            rows.append(chosen)
        else:
            rows.append(Adw.ActionRow(title=_("Åpne en delt mappe"), use_markup=False,
                                      subtitle=_("Sikkerhetskopiene må ligge i en av de delte mappene på NAS-en")))
        self._set_browse_rows(rows)
        self._update_connect()

    # ------------------------------------------------------------ restic

    def _install(self, *_a):
        self.banner.set_button_label(_("Installerer …"))

        def work():
            ok = repo_mod.install_restic()
            GLib.idle_add(done, ok)

        def done(ok):
            self.banner.set_revealed(not ok)
            self.banner.set_button_label(_("Installer"))
            if not ok:
                self._toast(_("restic ble ikke installert"))
            return False
        threading.Thread(target=work, daemon=True).start()

    def _config(self) -> tuple[dict, str | None]:
        if self._kind() == "local":
            if not self.folder.get("path"):
                return {}, _("Velg en mappe")
            path, uri = repo_mod.storage_root(self.folder["path"], self.folder.get("uri"))
            return {"kind": "local", "path": path, "uri": uri}, None
        if not self.browse_uri or not network.usable(self.browse_uri, self.net_login.get("protocol", "smb")):
            return {}, _("Koble til NAS-en og velg en mappe")
        path = network.local_path(self.browse_uri)
        if not path:
            return {}, _("Mappen kan ikke åpnes som en vanlig mappe. Er pakken gvfs-fuse installert?")
        path, uri = repo_mod.storage_root(path, self.browse_uri)
        return {"kind": "local", "path": path, "uri": uri, "network": dict(self.net_login)}, None

    def _connect(self, *_a):
        cfg, problem = self._config()
        if problem:
            self._toast(problem)
            return
        if not repo_mod.restic_path():
            self._toast(_("Installer restic først"))
            return
        if self.using_stored:
            password = self.stored
        else:
            password = self.pw1.get_text()
            if len(password) < crypto.MIN_LENGTH:
                self._toast(_("Passordet må ha minst {} tegn").format(crypto.MIN_LENGTH))
                return
        self.connect_btn.set_sensitive(False)
        self.spinner.set_visible(True)
        net = cfg.get("network")
        if net and self.password.get_text():
            # Koble fra og til igjen med innloggingen som ble skrevet: da blir den sjekket og lagret i nøkkelringen
            # (var delingen allerede tilkoblet, f.eks. fra Filer, ville den ellers ikke blitt lagret, og de
            # automatiske kopiene kunne ikke koblet til etter en omstart).
            def relogged(err):
                if err:
                    self.connect_btn.set_sensitive(True)
                    self.spinner.set_visible(False)
                    self._toast(err)
                    return
                self._check_repo(cfg, password)
            network.remount(cfg["uri"], net.get("user", ""), self.password.get_text(), net.get("domain", ""),
                            self._ask, relogged)
            return
        self._check_repo(cfg, password)

    def _check_repo(self, cfg: dict, password: str):
        def work():
            r = repo_mod.Repo(cfg, password)
            try:
                if r.exists():
                    return "existing", None
                if self.mode == "restore":
                    return None, _("Fant ingen sikkerhetskopier fra Waypoint her. Velg mappen som inneholder "
                                   "mappen «Waypoint».")
                if not self.using_stored and self.pw2.get_text() != password:
                    return None, _("Det finnes ingen sikkerhetskopier her ennå. Skriv passordet to ganger for å "
                                   "opprette et nytt lager.")
                r.init()
                return "created", None
            except repo_mod.RepoError as e:
                return None, str(e)

        def done(result, err):
            self.connect_btn.set_sensitive(True)
            self.spinner.set_visible(False)
            if err:
                self._toast(err)
                return False
            if not self.stored:
                settings.store_password(password)  # ingen passord fra før: ingenting blir overskrevet
            elif not self.using_stored:
                if self.mode == "restore":
                    cfg["_password"] = password  # bare for denne gjenopprettingen; lagres aldri
                elif not self.password_locked:
                    settings.store_password(password)  # ingen oppgaver bruker det gamle passordet
            self.win.toast(_("Koblet til eksisterende sikkerhetskopier") if result == "existing"
                           else _("Nytt lager for sikkerhetskopier er opprettet"))
            self.close()
            self.on_done(cfg)
            return False
        threading.Thread(target=lambda: GLib.idle_add(done, *work()), daemon=True).start()
