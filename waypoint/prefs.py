"""Innstillinger: sikkerhetskopi (lagringssted, automatikk, regler, kontroll), unntak, kryptering og generelt."""

from __future__ import annotations

import threading

from gi.repository import Adw, Gio, GLib, Gtk

from . import auto, crypto, i18n, repo as repo_mod, settings, tray
from .i18n import N_, _

THEMES = [("system", N_("Følg systemet")), ("light", N_("Lyst")), ("dark", N_("Mørkt"))]
WARN = [(0, N_("Aldri")), (3, N_("Etter 3 dager")), (7, N_("Etter en uke")), (14, N_("Etter to uker")),
        (30, N_("Etter en måned"))]
CHECKS = [(7, N_("Hver uke")), (30, N_("Hver måned")), (0, N_("Aldri"))]
SIZES = [("", N_("Ingen grense")), ("100M", "100 MB"), ("1G", "1 GB"), ("4G", "4 GB"), ("16G", "16 GB")]


def combo(title: str, options: list, current, on_change, icon: str | None = None) -> Adw.ComboRow:
    keys = [k for k, _l in options]
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new([_(lbl) for _k, lbl in options]))
    if icon:
        row.add_prefix(Gtk.Image(icon_name=icon))
    row.set_selected(keys.index(current) if current in keys else 0)
    row.connect("notify::selected", lambda r, _p: on_change(keys[r.get_selected()]))
    return row


class Preferences(Adw.PreferencesDialog):
    def __init__(self, win, page_name: str | None = None):
        super().__init__(title=_("Innstillinger"), search_enabled=False)
        self.win = win
        self.cfg = settings.load()
        self.add(self._backup_page())
        self.add(self._exclude_page())
        self.add(self._crypto_page())
        self.add(self._general_page())
        if page_name:
            self.set_visible_page_name(page_name)
        self._timer = GLib.timeout_add_seconds(2, self._tick)
        self.connect("closed", lambda *_a: GLib.source_remove(self._timer))

    # ------------------------------------------------------------ sikkerhetskopi

    def _backup_page(self):
        """Felles for alle oppgaver: regler, varsler, utynning og kontroll. Lagringssted, innhold og hvor ofte
        stilles inn per oppgave på forsiden."""
        p = Adw.PreferencesPage(title=_("Kopier"), icon_name="document-open-recent-symbolic", name="auto")
        info = Adw.PreferencesGroup(description=_("Lagringssted, innhold og hvor ofte stilles inn for hver "
                                                  "sikkerhetskopioppgave på forsiden. Valgene her gjelder alle."))
        p.add(info)

        rules = Adw.PreferencesGroup(title=_("Regler"))
        bat = Adw.SwitchRow(title=_("Ikke på batteri"), active=self.cfg["skip_on_battery"],
                            subtitle=_("Venter til maskinen er koblet til strøm"))
        bat.add_prefix(Gtk.Image(icon_name="battery-symbolic"))
        bat.connect("notify::active", lambda r, _p: settings.set("skip_on_battery", r.get_active()))
        rules.add(bat)
        met = Adw.SwitchRow(title=_("Ikke på målt nettverk"), active=self.cfg["skip_on_metered"],
                            subtitle=_("Gjelder lagring på en NAS, f.eks. når du deler nett fra telefonen"))
        met.add_prefix(Gtk.Image(icon_name="network-cellular-symbolic"))
        met.connect("notify::active", lambda r, _p: settings.set("skip_on_metered", r.get_active()))
        rules.add(met)
        rules.add(combo(_("Varsle når det ikke er tatt kopi"), WARN, self.cfg["warn_days"],
                        lambda v: settings.set("warn_days", v), "dialog-warning-symbolic"))
        p.add(rules)

        notes = Adw.PreferencesGroup(title=_("Varsler"))
        start = Adw.SwitchRow(title=_("Varsle når en sikkerhetskopi starter"), active=self.cfg["notify_start"],
                              subtitle=_("Med knappen «Vis fremdrift», så du kan velge å følge med. Krever ikonet i "
                                         "statusfeltet."))
        start.add_prefix(Gtk.Image(icon_name="preferences-system-notifications-symbolic"))
        start.connect("notify::active", lambda r, _p: settings.set("notify_start", r.get_active()))
        notes.add(start)
        p.add(notes)

        keep_group = Adw.PreferencesGroup()
        keep = Adw.ActionRow(title=_("Gamle tidspunkter"), use_markup=False,
                             subtitle=_("Alle fra siste døgn, ett per dag den siste måneden og ett per uke før det. "
                                        "Når disken blir full, slettes de eldste."))
        keep.set_subtitle_lines(3)
        keep.add_prefix(Gtk.Image(icon_name="user-trash-symbolic"))
        keep_group.add(keep)
        p.add(keep_group)

        c = Adw.PreferencesGroup(title=_("Kontroll"),
                                 description=_("Leser en del av sikkerhetskopiene med jevne mellomrom for å se at de er "
                                               "hele og kan gjenopprettes."))
        c.add(combo(_("Kontroller kopiene"), CHECKS, self.cfg["check_days"], lambda v: settings.set("check_days", v),
                    "security-high-symbolic"))
        self.check_row = Adw.ActionRow(title=_("Siste kontroll"), use_markup=False)
        self.check_row.set_subtitle_lines(4)
        self.check_btn = Gtk.Button(label=_("Kontroller nå"), valign=Gtk.Align.CENTER)
        self.check_btn.connect("clicked", self._check_now)
        self.check_row.add_suffix(self.check_btn)
        c.add(self.check_row)
        p.add(c)
        self._refresh()
        return p

    def _refresh(self):
        jobs = settings.jobs()
        status = settings.read_status()["jobs"]
        lines, checking = [], False
        for j in jobs:
            st = status.get(j["id"], {})
            checking = checking or bool(st.get("checking"))
            if st.get("checking"):
                res = _("Kontrollerer …")
            elif st.get("check_skipped"):
                res = st["check_skipped"]
            elif st.get("last_check"):
                ok = _("i orden") if st.get("check_ok") else (st.get("check_text") or _("fant feil"))
                res = f"{tray._when(st['last_check'])} · {ok}"
            else:
                res = _("Ikke kontrollert ennå")
            lines.append(f"{j.get('name') or '?'}: {res}")
        self.check_row.set_subtitle("\n".join(lines) or _("Ingen sikkerhetskopioppgaver ennå"))
        running = any(st.get("state") == "running" for st in status.values())
        self.check_btn.set_sensitive(bool(jobs) and not checking and not running)

    def _tick(self):
        self._refresh()
        return True

    def _check_now(self, *_a):
        pid = auto.start_check()
        for j in settings.jobs():
            settings.update_job_status(j["id"], checking=True, check_pid=pid, check_boot=settings.boot_id(),
                                       check_skipped=None)
        self._refresh()

    # ------------------------------------------------------------ unntak

    def _exclude_page(self):
        p = Adw.PreferencesPage(title=_("Unntak"), icon_name="action-unavailable-symbolic", name="exclude")
        self.pat_group = Adw.PreferencesGroup(
            title=_("Hopp over"),
            description=_("Filer og mapper som passer med et mønster, tas ikke med. Eksempler: node_modules, *.iso, "
                          "~/Videoer/Opptak. Mellomlager og midlertidige filer hoppes alltid over."))
        self.add_row = Adw.EntryRow(title=_("Legg til mønster"), show_apply_button=True)
        self.add_row.connect("apply", self._add_pattern)
        self.pat_rows: list[Gtk.Widget] = []
        p.add(self.pat_group)
        self._fill_patterns()
        big = Adw.PreferencesGroup()
        big.add(combo(_("Hopp over filer større enn"), SIZES, self.cfg["exclude_larger"],
                      lambda v: settings.set("exclude_larger", v), "drive-harddisk-symbolic"))
        p.add(big)
        return p

    def _fill_patterns(self):
        for r in self.pat_rows:
            self.pat_group.remove(r)
        self.pat_rows = []
        for pat in settings.get("excludes") or []:
            row = Adw.ActionRow(title=pat, use_markup=False)
            rm = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER, tooltip_text=_("Fjern"))
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, pat=pat: self._remove_pattern(pat))
            row.add_suffix(rm)
            self.pat_group.add(row)
            self.pat_rows.append(row)
        self.pat_group.add(self.add_row)
        self.pat_rows.append(self.add_row)

    def _add_pattern(self, row):
        pat = row.get_text().strip()
        pats = settings.get("excludes") or []
        if pat and pat not in pats:
            settings.set("excludes", pats + [pat])
        row.set_text("")
        self._fill_patterns()

    def _remove_pattern(self, pat):
        settings.set("excludes", [p for p in settings.get("excludes") or [] if p != pat])
        self._fill_patterns()

    # ------------------------------------------------------------ kryptering

    def _crypto_page(self):
        p = Adw.PreferencesPage(title=_("Kryptering"), icon_name="channel-secure-symbolic", name="crypto")
        g = Adw.PreferencesGroup(description=_("Sikkerhetskopiene på lagringsstedet er alltid kryptert med AES-256 "
                                               "(restic). Eksporterte filer krypteres med gpg og kan også åpnes uten "
                                               "Waypoint: gpg -d fil.tar.gz.gpg | tar xz"))
        self.pw_row = Adw.ActionRow(title=_("Passord"), use_markup=False)
        self.pw_row.add_prefix(Gtk.Image(icon_name="dialog-password-symbolic"))
        self.pw_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.pw_btn.connect("clicked", self._set_password)
        self.pw_row.add_suffix(self.pw_btn)
        g.add(self.pw_row)
        enc = Adw.SwitchRow(title=_("Krypter hele eksportfilen"), active=self.cfg["encrypt_all"],
                            subtitle=_("Nøkler, passord og nettleserprofiler krypteres alltid."))
        enc.connect("notify::active", lambda r, _p: settings.set("encrypt_all", r.get_active()))
        g.add(enc)
        p.add(g)
        self._refresh_pw()
        return p

    def _refresh_pw(self):
        has = bool(settings.get_password())
        self.pw_row.set_subtitle(_("Lagret i nøkkelringen") if has else _("Ikke angitt"))
        self.pw_btn.set_label(_("Endre …") if has else _("Angi …"))

    def _set_password(self, *_a):
        has_repo = bool(repo_mod.Repo.all_storages()) and bool(settings.get_password())
        body = _("Passordet lagres i nøkkelringen, så automatiske kopier kan krypteres. Uten passordet kan ingen "
                 "åpne kopiene – heller ikke du. Skriv det ned et trygt sted.")
        if has_repo:
            body += "\n\n" + _("Passordet endres også på lagringsstedet. Tidligere eksporterte filer beholder det "
                               "gamle passordet.")
        d = Adw.AlertDialog(heading=_("Passord for sikkerhetskopiene"), body=body)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        pw1 = Adw.PasswordEntryRow(title=_("Nytt passord"))
        pw2 = Adw.PasswordEntryRow(title=_("Gjenta passordet"))
        lb.append(pw1)
        lb.append(pw2)
        d.set_extra_child(lb)
        d.add_response("cancel", _("Avbryt"))
        d.add_response("ok", _("Lagre"))
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_response_enabled("ok", False)

        def changed(*_a):
            a, b = pw1.get_text(), pw2.get_text()
            d.set_response_enabled("ok", len(a) >= crypto.MIN_LENGTH and a == b)
        pw1.connect("changed", changed)
        pw2.connect("changed", changed)

        def resp(_d, rid):
            if rid != "ok":
                return
            new = pw1.get_text()

            def work():
                try:
                    if not has_repo:
                        return None if settings.store_password(new) else _("Nøkkelringen er ikke tilgjengelig")
                    # Alle oppgavenes lagre har samme passord. Alle må være tilgjengelige, ellers ville noen få det
                    # nye og andre beholde det gamle.
                    repos = repo_mod.Repo.all_storages()
                    missing = [repo_mod.describe(r.cfg) for r in repos if not r.available()]
                    if missing:
                        return _("Koble til alle lagringsstedene først: {}").format(", ".join(missing))
                    old = settings.get_password()
                    changed = []
                    try:
                        for r in repos:
                            r.change_password(new)
                            changed.append(r)
                        if settings.store_password(new):
                            return None
                        error = _("Nøkkelringen er ikke tilgjengelig. Passordet er ikke endret.")
                    except repo_mod.RepoError as e:
                        error = str(e)
                    # Rull tilbake hvert lager for seg, så én feil ikke stopper resten.
                    stuck = []
                    for r in changed:
                        try:
                            repo_mod.Repo(r.cfg, new).change_password(old)
                        except repo_mod.RepoError:
                            stuck.append(repo_mod.describe(r.cfg))
                    if stuck:
                        return _("Passordet ble ikke endret ({}). Disse lagringsstedene har likevel fått det NYE "
                                 "passordet og kunne ikke settes tilbake: {}. Skriv ned det nye passordet, og prøv å "
                                 "bytte på nytt når alle er tilgjengelige.").format(error, ", ".join(stuck))
                    return _("Passordet ble ikke endret: {}").format(error)
                except repo_mod.RepoError as e:
                    return str(e)

            def done(err):
                self.add_toast(Adw.Toast(title=err or _("Passordet er lagret")))
                self._refresh_pw()
                return False
            threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()
        d.connect("response", resp)
        d.present(self)

    # ------------------------------------------------------------ generelt

    def _general_page(self):
        p = Adw.PreferencesPage(title=_("Generelt"), icon_name="preferences-system-symbolic", name="general")
        g = Adw.PreferencesGroup()
        codes = [c for c, _n in i18n.LANGUAGES]
        lang = Adw.ComboRow(title=_("Språk"), model=Gtk.StringList.new([i18n.language_name(c) for c in codes]))
        lang.add_prefix(Gtk.Image(icon_name="preferences-desktop-locale-symbolic"))
        lang.set_selected(codes.index(self.cfg["language"]) if self.cfg["language"] in codes else 0)

        def lang_changed(r, _p):
            settings.set("language", codes[r.get_selected()])
            i18n.reset()
            app = self.win.get_application()
            self.close()
            GLib.idle_add(lambda: app.rebuild() or False)
        lang.connect("notify::selected", lang_changed)
        g.add(lang)

        def theme_changed(v):
            from .app import apply_theme
            settings.set("theme", v)
            apply_theme()
        g.add(combo(_("Utseende"), THEMES, self.cfg["theme"], theme_changed, "weather-clear-night-symbolic"))
        p.add(g)

        t = Adw.PreferencesGroup(title=_("Oppstart og statusfelt"),
                                 description=_("Automatiske sikkerhetskopier tas uansett, også når Waypoint ikke er "
                                               "startet."))
        tr = Adw.SwitchRow(title=_("Vis ikon i statusfeltet"), active=self.cfg["tray"],
                           subtitle=_("Viser status for automatisk sikkerhetskopi. På GNOME trengs utvidelsen "
                                      "«AppIndicator and KStatusNotifierItem Support»."))
        auto_start = Adw.SwitchRow(title=_("Start ved innlogging"), active=self.cfg["autostart"],
                                   subtitle=_("Starter Waypoint i bakgrunnen, med ikonet i statusfeltet"))
        auto_start.set_sensitive(self.cfg["tray"])

        def tray_changed(r, _p):
            on = r.get_active()
            settings.set("tray", on)
            auto_start.set_sensitive(on)
            tray.sync_autostart()
            if on:
                tray.start()
        tr.connect("notify::active", tray_changed)

        def autostart_changed(r, _p):
            settings.set("autostart", r.get_active())
            tray.sync_autostart()
        auto_start.connect("notify::active", autostart_changed)
        t.add(tr)
        t.add(auto_start)
        p.add(t)
        p.add(self._update_group())
        # Avinstallering ligger nederst her (ikke i hovedmenyen), så det må være et aktivt valg.
        from . import updater
        if updater.is_installed() and (updater.install_dir() / "uninstall.sh").is_file():
            u = Adw.PreferencesGroup()
            row = Adw.ActionRow(title=_("Avinstaller Waypoint"),
                                subtitle=_("Programmet, menyoppføringen, statusfeltikonet og de automatiske kopiene "
                                           "fjernes. Sikkerhetskopiene dine beholdes."))
            btn = Gtk.Button(label=_("Avinstaller"), valign=Gtk.Align.CENTER)
            btn.add_css_class("destructive-action")
            btn.connect("clicked", lambda *_a: self._uninstall())
            row.add_suffix(btn)
            u.add(row)
            p.add(u)
        return p

    # ------------------------------------------------------------ avinstallering

    def _uninstall(self):
        d = Adw.AlertDialog(heading=_("Avinstaller Waypoint"),
                            body=_("Programmet, menyoppføringen, statusfeltikonet og de automatiske kopiene fjernes. "
                                   "Sikkerhetskopiene dine og passordet i nøkkelringen beholdes, så du kan hente "
                                   "dem tilbake senere."))
        purge = Gtk.CheckButton(label=_("Slett også innstillingene og oppgavene"))
        d.set_extra_child(purge)
        d.add_response("cancel", _("Avbryt"))
        d.add_response("uninstall", _("Avinstaller"))
        d.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")
        d.connect("response", lambda _d, r: r == "uninstall" and self._run_uninstall(purge.get_active()))
        d.present(self)

    def _run_uninstall(self, purge: bool):
        from . import updater
        argv = ["bash", str(updater.install_dir() / "uninstall.sh")] + (["--purge"] if purge else [])
        try:
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as e:
            self.add_toast(Adw.Toast(title=_("Avinstallering feilet: {}").format(e.message)))
            return
        app = self.win.get_application()

        def done(p, res):
            try:
                _ok, out, _err = p.communicate_utf8_finish(res)
            except GLib.Error as e:
                out = e.message
            if p.get_successful():
                msg = Adw.AlertDialog(heading=_("Waypoint er avinstallert"),
                                      body=_("Sikkerhetskopiene dine ligger der de var. Installer Waypoint igjen "
                                             "for å hente dem tilbake."))
                msg.add_response("ok", _("OK"))
                msg.connect("response", lambda *_a: app.quit())
                msg.present(self.win)
            else:
                self.add_toast(Adw.Toast(title=_("Avinstallering feilet: {}").format((out or "").strip()[-200:])))
        proc.communicate_utf8_async(None, None, done)

    # ------------------------------------------------------------ oppdateringer

    def _update_group(self):
        from . import __version__, updater
        g = Adw.PreferencesGroup(title=_("Oppdateringer"))
        check = Adw.SwitchRow(title=_("Se etter nye versjoner"), active=self.cfg["update_check"],
                              subtitle=_("Én gang i døgnet, fra Waypoint sin side på GitHub"))
        install = Adw.SwitchRow(title=_("Installer oppdateringer automatisk"), active=self.cfg["update_auto"],
                                subtitle=_("Ellers får du et varsel og kan oppdatere her"))
        install.set_sensitive(self.cfg["update_check"])

        def check_changed(r, _p):
            settings.set("update_check", r.get_active())
            install.set_sensitive(r.get_active())
        check.connect("notify::active", check_changed)
        install.connect("notify::active", lambda r, _p: settings.set("update_auto", r.get_active()))
        g.add(check)
        g.add(install)

        self.ver_row = Adw.ActionRow(title=_("Versjon {}").format(__version__), use_markup=False)
        self.ver_row.add_prefix(Gtk.Image(icon_name="software-update-available-symbolic"))
        self.ver_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.ver_btn.connect("clicked", self._update_clicked)
        self.ver_row.add_suffix(self.ver_btn)
        g.add(self.ver_row)
        self._refresh_update()
        if not updater.is_installed():
            g.set_description(_("Oppdatering fra appen gjelder bare en installert Waypoint (./install.sh), ikke "
                                "en kopi av kildekoden."))
        return g

    def _refresh_update(self, busy: str | None = None):
        from . import updater
        st = settings.read_status()
        avail = st.get("update_available")
        if avail and not updater.is_newer(avail):
            avail = None  # allerede installert
        if busy:
            text = busy
        elif avail:
            text = _("Versjon {} er klar til å installeres").format(avail.lstrip("v"))
        elif st.get("update_error"):
            text = _("Kunne ikke se etter oppdateringer: {}").format(st["update_error"])
        elif st.get("update_checked"):
            text = _("Du har siste versjon · sjekket {}").format(tray._when(st["update_checked"]))
        else:
            text = _("Ikke sjekket ennå")
        self.ver_row.set_subtitle(text)
        self._avail = avail
        self.ver_btn.set_label(_("Oppdater nå") if avail else _("Se etter nå"))
        if avail:
            self.ver_btn.add_css_class("suggested-action")
        else:
            self.ver_btn.remove_css_class("suggested-action")
        self.ver_btn.set_sensitive(not busy and (not avail or updater.is_installed()))

    def _update_clicked(self, *_a):
        from . import updater
        tag = self._avail
        self._refresh_update(busy=_("Installerer …") if tag else _("Ser etter …"))

        def work():
            try:
                if tag:
                    updater.install(tag, log=lambda t: None)
                    settings.update_status(update_available=None, update_installed=tag)
                    updater.restart_tray()
                    return "installed", None
                return ("newer" if updater.check() else "latest"), None
            except Exception as e:  # noqa: BLE001
                settings.update_status(update_error=str(e))
                return None, str(e)

        def done(result, err):
            self._refresh_update()
            if result == "installed":
                d = Adw.AlertDialog(heading=_("Waypoint er oppdatert"),
                                    body=_("Start Waypoint på nytt for å ta i bruk den nye versjonen."))
                d.add_response("later", _("Senere"))
                d.add_response("restart", _("Start på nytt"))
                d.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)
                d.connect("response", lambda _d, r: r == "restart" and self._restart_app())
                d.present(self)
            elif result == "latest":
                self.add_toast(Adw.Toast(title=_("Du har siste versjon")))
            elif err:
                self.add_toast(Adw.Toast(title=err))
            return False
        threading.Thread(target=lambda: GLib.idle_add(done, *work()), daemon=True).start()

    def _restart_app(self):
        import subprocess
        # Waypoint kjører som én forekomst; den nye må vente til denne har avsluttet, ellers sender den bare
        # «aktiver» til den gamle.
        subprocess.Popen(["sh", "-c", 'sleep 1; exec "$@"', "sh", *auto.command()], start_new_session=True)
        self.win.get_application().quit()
