"""Vinduet: forside, valg av innhold, fremdrift og resultat for både sikkerhetskopi og gjenoppretting."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

from . import APP_ID, __version__, auto, backup, crypto, restore, settings, tray  # noqa: E402
from . import repo as repo_mod  # noqa: E402
from .i18n import _  # noqa: E402
from .model import Group, Item, Section, apply_job, collect_choices  # noqa: E402

HERE = Path(__file__).parent
HOME = str(Path.home())


def tilde(path) -> str:
    s = str(path)
    return "~" + s[len(HOME):] if s.startswith(HOME) else s


def fmt_size(n: int) -> str:
    return GLib.format_size(n) if n else ""


def in_thread(work, done, *args):
    """Kjører work(*args) i en egen tråd og done(resultat, feil) i GUI-tråden."""
    def runner():
        try:
            result, error = work(*args), None
        except Exception as e:  # noqa: BLE001 – vises til brukeren
            result, error = None, e
        GLib.idle_add(done, result, error)
    threading.Thread(target=runner, daemon=True).start()


def label(text="", css=(), xalign=0.0, wrap=False, **kw) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=xalign, wrap=wrap, **kw)
    if wrap:
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for c in css:
        lbl.add_css_class(c)
    return lbl


def page(title: str, content: Gtk.Widget, bottom: Gtk.Widget | None = None, tag: str | None = None,
         header_end: Gtk.Widget | None = None) -> Adw.NavigationPage:
    tv = Adw.ToolbarView()
    hb = Adw.HeaderBar()
    if header_end:
        hb.pack_end(header_end)
    tv.add_top_bar(hb)
    tv.set_content(content)
    if bottom:
        tv.add_bottom_bar(bottom)
    p = Adw.NavigationPage(title=title, child=tv)
    if tag:
        p.set_tag(tag)
    return p


def scrolled(child: Gtk.Widget, width: int = 720) -> Gtk.ScrolledWindow:
    clamp = Adw.Clamp(maximum_size=width, tightening_threshold=width - 120, child=child)
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    sw.set_child(clamp)
    return sw


def vbox(spacing=12, margin=0) -> Gtk.Box:
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    for side in ("top", "bottom", "start", "end"):
        getattr(b, f"set_margin_{side}")(margin)
    return b


def pill(text: str, suggested=True) -> Gtk.Button:
    b = Gtk.Button(label=text)
    b.add_css_class("pill")
    if suggested:
        b.add_css_class("suggested-action")
    return b


def bottom_bar(summary: Gtk.Widget, button: Gtk.Button) -> Gtk.Widget:
    bar = Gtk.Box(spacing=12)
    bar.add_css_class("bottom-bar")
    summary.set_hexpand(True)
    bar.append(summary)
    bar.append(button)
    return bar


class _FixedPicture(Gtk.Picture):
    """Et bilde med fast størrelse uansett oppløsningen på bildet (skarpt på HiDPI-skjermer)."""

    def __init__(self, paintable, w, h):
        super().__init__(paintable=paintable, content_fit=Gtk.ContentFit.COVER)
        self._w, self._h = w, h

    def do_measure(self, orientation, for_size):
        n = self._w if orientation == Gtk.Orientation.HORIZONTAL else self._h
        return n, n, -1, -1


def thumbnail(path: str, w=72, h=46) -> Gtk.Widget:
    """Et lite, beskåret forhåndsbilde i nøyaktig w×h punkter."""
    try:
        src = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, w * 4, h * 4, True)
        scale = max(2 * w / src.get_width(), 2 * h / src.get_height())
        sw, sh = max(2 * w, round(src.get_width() * scale)), max(2 * h, round(src.get_height() * scale))
        pb = src.scale_simple(sw, sh, GdkPixbuf.InterpType.BILINEAR)
        pb = pb.new_subpixbuf((sw - 2 * w) // 2, (sh - 2 * h) // 2, 2 * w, 2 * h)
        frame = Gtk.Frame(child=_FixedPicture(Gdk.Texture.new_for_pixbuf(pb), w, h), valign=Gtk.Align.CENTER)
        frame.add_css_class("thumb")
        return frame
    except GLib.Error:
        return Gtk.Image(icon_name="image-x-generic-symbolic", pixel_size=24)


# ======================================================================= utvalg

class Chooser:
    """Lager radene for seksjonene og holder valgene i modellen oppdatert."""

    def __init__(self, sections: list[Section], on_change):
        self.sections = sections
        self.on_change = on_change

    def build(self) -> list[Gtk.Widget]:
        out = []
        for s in self.sections:
            grp = Adw.PreferencesGroup(title=GLib.markup_escape_text(s.title),
                                       description=GLib.markup_escape_text(s.description))
            for g in s.groups:
                if g.flat:
                    for item in g.items:
                        grp.add(self._switch_row(g, item))
                else:
                    grp.add(self._expander(g))
            out.append(grp)
        return out

    def _switch_row(self, g: Group, item: Item) -> Gtk.Widget:
        row = Adw.SwitchRow(title=item.label, subtitle=item.detail, use_markup=False)
        row.set_active(item.selected and item.available)
        row.set_sensitive(item.available)
        if g.id == "wallpaper" and item.data.get("preview"):
            row.add_prefix(thumbnail(item.data["preview"]))
        else:
            row.add_prefix(Gtk.Image(icon_name=g.icon))
        if item.size:
            row.add_suffix(label(fmt_size(item.size), ("dim-label", "numeric")))

        def toggled(r, _p):
            item.selected = r.get_active()
            self.on_change()
        row.connect("notify::active", toggled)
        return row

    def _expander(self, g: Group) -> Gtk.Widget:
        exp = Adw.ExpanderRow(title=g.title, use_markup=False)
        exp.add_prefix(Gtk.Image(icon_name=g.icon))
        usable = [i for i in g.items if i.available]
        all_check = Gtk.CheckButton(valign=Gtk.Align.CENTER, tooltip_text=_("Velg alle eller ingen"))
        all_check.add_css_class("selection-mode")
        all_check.set_sensitive(bool(usable))
        exp.add_suffix(all_check)
        checks: list[tuple[Item, Gtk.CheckButton]] = []
        updating = [False]

        def refresh():
            sel = [i for i in usable if i.selected]
            size = sum(i.size for i in sel)
            parts = []
            if g.note:
                parts.append(g.note)
            elif not usable:
                parts.append(_("Alt finnes allerede her"))
            else:
                parts.append(_("{} av {} valgt").format(len(sel), len(usable)) + (f" · {fmt_size(size)}" if size else ""))
                if g.subtitle:
                    parts.append(g.subtitle)
            exp.set_subtitle(" · ".join(parts))
            updating[0] = True
            all_check.set_active(bool(sel) and len(sel) == len(usable))
            all_check.set_inconsistent(0 < len(sel) < len(usable))
            updating[0] = False

        def item_toggled(btn, item):
            item.selected = btn.get_active()
            if not updating[0]:
                refresh()
                self.on_change()

        def all_toggled(btn):
            if updating[0]:
                return
            on = btn.get_active() or btn.get_inconsistent()
            updating[0] = True
            for item, chk in checks:
                if item.available:
                    chk.set_active(on)
                    item.selected = on
            updating[0] = False
            refresh()
            self.on_change()

        all_check.connect("toggled", all_toggled)
        for item in g.items:
            row = Adw.ActionRow(title=item.label, subtitle=item.detail, use_markup=False)
            row.set_subtitle_lines(1)
            chk = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=item.selected and item.available)
            chk.add_css_class("selection-mode")
            row.add_prefix(chk)
            row.set_activatable_widget(chk)
            if item.sensitive:
                lock = Gtk.Image(icon_name="channel-secure-symbolic", tooltip_text=_("Krypteres alltid med passordet ditt"))
                lock.add_css_class("dim-label")
                row.add_suffix(lock)
            if item.size:
                row.add_suffix(label(fmt_size(item.size), ("dim-label", "numeric", "caption")))
            if not item.available:
                row.set_sensitive(False)
                item.selected = False
            chk.connect("toggled", item_toggled, item)
            checks.append((item, chk))
            exp.add_row(row)
        refresh()
        return exp


class PasswordRows:
    """Passordfeltene for kryptering: bruker passordet i nøkkelringen, eller lar deg skrive et nytt."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.stored = settings.get_password()
        # Er et lagringssted satt opp, er passordet i nøkkelringen nøkkelen til restic-lageret. Et annet
        # passord for en eksportfil må da aldri lagres over det (da kunne lageret ikke åpnes lenger).
        self.has_storage = bool(settings.jobs())
        self.group = Adw.PreferencesGroup()
        self.stored_row = Adw.ActionRow(title=_("Passord"), subtitle=_("Bruker passordet som er lagret i nøkkelringen"))
        self.stored_row.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic"))
        other = Gtk.Button(label=_("Bruk et annet …"), valign=Gtk.Align.CENTER)
        other.add_css_class("flat")
        other.connect("clicked", lambda *_a: self._use_new())
        self.stored_row.add_suffix(other)
        self.pw1 = Adw.PasswordEntryRow(title=_("Passord"))
        self.pw2 = Adw.PasswordEntryRow(title=_("Gjenta passordet"))
        self.remember = Adw.SwitchRow(title=_("Husk passordet i nøkkelringen"),
                                      subtitle=_("Trengs for automatisk sikkerhetskopi"), active=True)
        self.hint = Adw.ActionRow(title=_("Ta vare på passordet"), use_markup=False,
                                  subtitle=_("Uten passordet kan ingen åpne det som er kryptert – heller ikke du. "
                                             "Skriv det ned et trygt sted."))
        self.hint.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        for w in (self.stored_row, self.pw1, self.pw2, self.remember, self.hint):
            self.group.add(w)
        for e in (self.pw1, self.pw2):
            e.connect("changed", lambda *_a: self._changed())
        self.using_stored = bool(self.stored)
        self._show()

    def _use_new(self):
        self.using_stored = False
        self._show()
        self.pw1.grab_focus()
        self.on_change()

    def _show(self):
        self.stored_row.set_visible(self.using_stored)
        for w in (self.pw1, self.pw2, self.hint):
            w.set_visible(not self.using_stored)
        self.remember.set_visible(not self.using_stored and not self.has_storage)

    def _changed(self):
        a, b = self.pw1.get_text(), self.pw2.get_text()
        self.pw2.remove_css_class("error")
        if b and a != b:
            self.pw2.add_css_class("error")
        self.on_change()

    def problem(self) -> str | None:
        if self.using_stored:
            return None
        a, b = self.pw1.get_text(), self.pw2.get_text()
        if len(a) < crypto.MIN_LENGTH:
            return _("Passordet må ha minst {} tegn").format(crypto.MIN_LENGTH)
        if a != b:
            return _("Passordene er ikke like")
        return None

    def password(self) -> str | None:
        if self.using_stored:
            return self.stored
        return None if self.problem() else self.pw1.get_text()

    def commit(self):
        """Lagrer et nytt passord i nøkkelringen hvis brukeren vil det."""
        if self.has_storage or self.using_stored:
            return
        if self.remember.get_active() and self.password():
            settings.store_password(self.password())


# ======================================================================= vindu

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Waypoint", default_width=640, default_height=760)
        self.set_size_request(380, 500)
        # Vifte-stilen (glød og svevende kort, se style.css) har egne verdier for lyst tema.
        self.add_css_class("waypoint-main")
        sm = Adw.StyleManager.get_default()
        self._sync_light = lambda *_a: (self.remove_css_class("waypoint-light") if sm.get_dark()
                                        else self.add_css_class("waypoint-light"))
        sm.connect("notify::dark", self._sync_light)
        self._sync_light()
        self.set_glow(bool(settings.get("glow")))
        self.busy = False
        self.cancel_flag = False
        self.restorer: restore.Restorer | None = None
        self.backup_file: restore.BackupFile | None = None
        self.toasts = Adw.ToastOverlay(vexpand=True)
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        from .jobs_ui import StatusBar
        self.status_bar = StatusBar(self)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self.toasts)
        root.append(self.status_bar)
        self.set_content(root)
        self.start = self._start_page()
        self.nav.add(self.start)
        self.connect("close-request", self._on_close)
        self._status_mon = None
        self._watch_status()

    def set_glow(self, on: bool):
        """Den svake blå gløden i bakgrunnen; kan slås av i menyen for ekstra tydelighet."""
        if on:
            self.add_css_class("waypoint-glow")
        else:
            self.remove_css_class("waypoint-glow")

    def toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=5))

    # ------------------------------------------------------------------ forside

    def _start_page(self) -> Adw.NavigationPage:
        box = vbox(18, 0)
        for side, m in (("top", 14), ("bottom", 18), ("start", 22), ("end", 22)):
            getattr(box, f"set_margin_{side}")(m)
        hero = vbox(2)
        hero.append(Gtk.Image(icon_name=APP_ID, pixel_size=56, margin_bottom=4))
        hero.append(label("Waypoint", ("hero-title",), xalign=0.5))
        hero.append(label(_("Sikkerhetskopi av oppsettet og filene dine. Gå tilbake til et hvilket som helst "
                            "tidspunkt."),
                          ("dim-label", "hero-text"), xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER,
                          max_width_chars=60))
        box.append(hero)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True)
        grid.attach(self._card("backup", "document-save-symbolic", _("Ny oppgave"),
                               _("Velg hva som kopieres, og hvor."), lambda: self.edit_job(None)), 0, 0, 1, 1)
        grid.attach(self._card("restore", "document-revert-symbolic", _("Gjenopprett"),
                               _("Legg tilbake oppsett og filer."), self.choose_restore_source), 1, 0, 1, 1)
        grid.attach(self._card("browse", "system-search-symbolic", _("Hent enkeltfiler"),
                               _("Finn en fil slik den var før."), self.begin_browse), 0, 1, 1, 1)
        grid.attach(self._card("export", "document-send-symbolic", _("Eksporter oppsett"),
                               _("Ta oppsettet med til en annen maskin."), lambda: self.begin_backup("export")),
                    1, 1, 1, 1)
        box.append(grid)

        from .jobs_ui import JobsGroup
        self.jobs_group = JobsGroup(self)
        box.append(self.jobs_group)

        how_group = Adw.PreferencesGroup()
        how = Adw.ExpanderRow(title=_("Slik gjør du det"), subtitle=_("Tre steg fra første kopi til gjenoppretting"))
        how.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        how_group.add(how)
        for n, (title, text) in enumerate([
            (_("Lag en oppgave"), _("Velg hva som skal kopieres, og hvor. Alt krypteres med passordet ditt.")),
            (_("La Waypoint ta kopier"), _("Automatisk i bakgrunnen – bare det som er nytt eller endret.")),
            (_("Gå tilbake når du trenger det"), _("Hent én fil, eller gjenopprett alt på en ny maskin.")),
        ], 1):
            row = Adw.ActionRow(title=title, subtitle=text)
            row.add_prefix(label(str(n), ("step-badge",), xalign=0.5, valign=Gtk.Align.CENTER))
            how.add_row(row)
        box.append(how_group)

        menu = Gio.Menu()
        look = Gio.Menu()
        look.append(_("Bakgrunnsglød"), "app.glow")
        menu.append_section(None, look)
        main = Gio.Menu()
        main.append(_("Innstillinger"), "app.preferences")
        main.append(_("Om Waypoint"), "app.about")
        main.append(_("Avslutt"), "app.quit")
        menu.append_section(None, main)
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text=_("Meny"))
        return page("Waypoint", scrolled(box, 600), tag="start", header_end=mb)

    def _card(self, kind, icon, title, text, callback) -> Gtk.Widget:
        btn = Gtk.Button()
        btn.add_css_class("card")
        btn.add_css_class("action-card")
        inner = vbox(4)
        head = Gtk.Box(spacing=10)
        ic = Gtk.Image(icon_name=icon, pixel_size=16, valign=Gtk.Align.CENTER)
        ic.add_css_class("action-icon")
        ic.add_css_class(kind)
        head.append(ic)
        t = label(title, ("action-title",), valign=Gtk.Align.CENTER, hexpand=True, wrap=True)
        t.set_wrap_mode(Pango.WrapMode.WORD)  # bryt bare mellom ord, aldri midt i «Sikkerhetskopi»
        head.append(t)
        inner.append(head)
        inner.append(label(text, ("dim-label", "action-text"), wrap=True))
        btn.set_child(inner)
        btn.connect("clicked", lambda *_a: callback())
        return btn

    def _watch_status(self):
        self._monitors = []
        for d in (settings.state_dir(), settings.config_dir()):
            d.mkdir(parents=True, exist_ok=True)
            mon = Gio.File.new_for_path(str(d)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            mon.connect("changed", lambda *_a: self._queue_refresh())
            self._monitors.append(mon)
        self._refresh_pending = False
        self.refresh_auto()
        GLib.timeout_add_seconds(30, lambda: self.refresh_auto() or True)  # «i dag 12:00» o.l.

    def _queue_refresh(self):
        """Statusfilen skrives ofte under en kopi; oppdater høyst et par ganger i sekundet."""
        if not self._refresh_pending:
            self._refresh_pending = True
            GLib.timeout_add(400, lambda: (setattr(self, "_refresh_pending", False), self.refresh_auto()) and False)

    def refresh_auto(self):
        """Oppdaterer oppgavelisten og statuslinjen fra status.json og innstillingene."""
        self.jobs_group.refresh()
        self.status_bar.refresh()

    def fill_found(self, group: Adw.PreferencesGroup):
        """Sikkerhetskopier i enkeltfiler: i hjemmemappen, på minnepinner og i gamle mapper."""
        extra = []
        for r in repo_mod.Repo.all_storages():
            root = r.local_root(mount=False)
            if root:
                extra.append(auto.old_host_folder(root))
        found = restore.find_backups(extra)[:8]
        for p in found:
            st = p.stat()
            when = GLib.DateTime.new_from_unix_local(int(st.st_mtime)).format("%d.%m.%Y %H:%M")
            row = Adw.ActionRow(title=p.name, subtitle=f"{tilde(p.parent)} · {fmt_size(st.st_size)} · {when}",
                                activatable=True, use_markup=False)
            icon = "channel-secure-symbolic" if p.name.endswith(".gpg") else \
                "drive-removable-media-symbolic" if str(p).startswith("/run/media") else "package-x-generic-symbolic"
            row.add_prefix(Gtk.Image(icon_name=icon))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, p=p: self.open_backup(p))
            group.add(row)
        return bool(found)

    def _loading(self, title: str, text: str) -> tuple[Adw.NavigationPage, Gtk.Label]:
        status = Adw.StatusPage(title=title)
        spinner = Adw.Spinner(width_request=48, height_request=48, halign=Gtk.Align.CENTER)
        detail = label(text, ("dim-label",), xalign=0.5, ellipsize=Pango.EllipsizeMode.MIDDLE)
        b = vbox(18)
        b.append(spinner)
        b.append(detail)
        status.set_child(b)
        return page(title, status), detail

    def _error(self, heading: str, body: str):
        d = Adw.AlertDialog(heading=heading, body=body)
        d.add_response("ok", _("OK"))
        d.present(self)

    def ask_password(self, heading: str, body: str, check, on_ok, on_cancel=lambda: None, error: str = ""):
        """Ber om et passord og sjekker det med check(passord) i en egen tråd."""
        d = Adw.AlertDialog(heading=heading, body=(error + "\n\n" + body) if error else body)
        entry = Adw.PasswordEntryRow(title=_("Passord"))
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        lb.append(entry)
        d.set_extra_child(lb)
        d.add_response("cancel", _("Avbryt"))
        d.add_response("ok", _("Åpne"))
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        d.set_close_response("cancel")
        entry.connect("entry-activated", lambda *_a: d.response("ok"))

        def resp(_d, rid):
            if rid != "ok":
                on_cancel()
                return
            pw = entry.get_text()

            def done(ok, err):
                if ok and not err:
                    on_ok(pw)
                else:
                    self.ask_password(heading, body, check, on_ok, on_cancel, _("Feil passord. Prøv igjen."))
            in_thread(check, done, pw)
        d.connect("response", resp)
        d.present(self)
        GLib.idle_add(lambda: entry.grab_focus() and False)

    def go_home(self):
        if self.backup_file:
            self.backup_file.cleanup()
            self.backup_file = None
        self.refresh_auto()
        self.nav.pop_to_page(self.start)

    def show_preferences(self, page_name: str | None = None):
        from .prefs import Preferences
        Preferences(self, page_name).present(self)

    # ------------------------------------------------------------------ sikkerhetskopi

    def begin_backup(self, mode: str = "export", job: dict | None = None, on_done=None):
        """mode: «export» (oppsettet til én fil) eller «job» (velg innholdet i en oppgave; on_done(valg))."""
        pg, detail = self._loading(_("Ser gjennom oppsettet ditt"), _("Leser innstillinger …"))
        self.nav.push(pg)

        def progress(text):
            GLib.idle_add(detail.set_label, text)

        def done(sections, err):
            if err:
                self.nav.pop()
                self._error(_("Kunne ikke lese oppsettet"), str(err))
                return
            self.nav.pop()
            if mode == "job":
                apply_job(sections, job)
            self.nav.push(self._backup_choose_page(sections, mode, on_done))
        in_thread(lambda: backup.scan(progress, personal=mode != "export"), done)

    def choose_job_content(self, job: dict, on_done):
        self.begin_backup("job", job, on_done)

    def _backup_choose_page(self, sections: list[Section], mode: str, on_done=None) -> Adw.NavigationPage:
        export = mode == "export"
        encrypt = [bool(settings.get("encrypt_all"))]
        folder = [backup.default_folder()]
        summary = label("", ("dim-label",), wrap=True)
        btn = pill(_("Eksporter") if export else _("Bruk valgene"))
        box = vbox(24, 24)
        intro = _("Lagrer oppsettet i én fil som du kan flytte til en annen maskin. Personlige mapper er ikke "
                  "valgt, siden filen da kan bli svært stor.") if export else \
            _("Velg nøyaktig hva denne oppgaven skal ta med. Åpne en gruppe for å se og endre innholdet.")
        box.append(label(intro, ("dim-label",), wrap=True))
        pw = PasswordRows(lambda: update())

        def update():
            n, size = backup.estimate(sections)
            text = _("{} elementer valgt · omtrent {}").format(n, fmt_size(size) or "0 kB")
            problem = None
            if export:
                need = backup.needs_password(sections, encrypt[0])
                problem = pw.problem() if need else None
                pw.group.set_visible(need)
            if problem:
                text = problem
            summary.set_label(text)
            btn.set_sensitive(n > 0 and not (problem and export))

        for w in Chooser(sections, update).build():
            box.append(w)

        if mode == "job":
            def use(*_a):
                self.nav.pop()
                on_done(collect_choices(sections))
            btn.connect("clicked", use)
            update()
            return page(_("Innholdet i oppgaven"), scrolled(box), bottom_bar(summary, btn))

        enc_group = Adw.PreferencesGroup(title=_("Kryptering"),
                                         description=_("Nøkler, passord og nettleserprofiler krypteres alltid."))
        enc = Adw.SwitchRow(title=_("Krypter hele filen"), active=encrypt[0],
                            subtitle=_("Anbefalt. Uten passordet kan ingen se hva filen inneholder."))
        enc.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic"))

        def enc_toggled(r, _p):
            encrypt[0] = r.get_active()
            settings.set("encrypt_all", encrypt[0])
            show_dest()
            update()
        enc.connect("notify::active", enc_toggled)
        enc_group.add(enc)
        box.append(enc_group)
        box.append(pw.group)

        dest_group = Adw.PreferencesGroup(title=_("Lagres som"))
        dest_row = Adw.ActionRow(use_markup=False)
        dest_row.add_prefix(Gtk.Image(icon_name="folder-download-symbolic"))
        change = Gtk.Button(label=_("Endre …"), valign=Gtk.Align.CENTER)
        change.add_css_class("flat")
        dest_row.add_suffix(change)
        dest_group.add(dest_row)
        hint = Adw.ActionRow(title=_("Husk å flytte filen"), use_markup=False,
                             subtitle=_("Hjemmemappen kan bli slettet ved ny installasjon. Kopier filen til en "
                                        "minnepinne eller skylagring etterpå."))
        hint.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        dest_group.add(hint)
        box.append(dest_group)

        def name():
            return backup.default_filename(encrypted=encrypt[0])

        def show_dest():
            dest_row.set_title(name())
            dest_row.set_subtitle(tilde(folder[0]))
            hint.set_visible(not str(folder[0]).startswith("/run/media"))

        def pick(*_a):
            dlg = Gtk.FileDialog(title=_("Velg mappe for sikkerhetskopien"),
                                 initial_folder=Gio.File.new_for_path(str(folder[0])))

            def chosen(d, res):
                try:
                    f = d.select_folder_finish(res)
                except GLib.Error:
                    return
                if f.get_path():
                    folder[0] = Path(f.get_path())
                    show_dest()
            dlg.select_folder(self, None, chosen)
        change.connect("clicked", pick)
        show_dest()
        update()

        def go_export(*_a):
            pw.commit()
            password = pw.password() if backup.needs_password(sections, encrypt[0]) else None
            self.run_backup(sections, folder[0] / name(), password, encrypt[0])
        btn.connect("clicked", go_export)
        return page(_("Eksporter oppsett"), scrolled(box), bottom_bar(summary, btn))

    def show_storage(self, on_done=lambda cfg: None, current: dict | None = None, mode: str = "job"):
        """Velg lagringssted for en oppgave (mode «job»), eller finn sikkerhetskopier for å gjenopprette («restore»;
        da opprettes aldri noe nytt lager)."""
        from .storage_ui import StorageDialog
        StorageDialog(self, on_done, current, mode).present(self)

    # ------------------------------------------------------------------ oppgaver

    def edit_job(self, job_id: str | None):
        from .jobs_ui import job_page
        self.nav.push(job_page(self, settings.job(job_id) if job_id else None))

    def run_job(self, job_id: str | None = None, follow: bool = False):
        """Ta sikkerhetskopi nå (én oppgave eller alle). Fremdriften vises i statuslinjen nederst; follow åpner
        også siden med detaljer."""
        started = time.time()
        auto.start_now(job_id, interactive=True)
        self.toast(_("Sikkerhetskopien er startet"))
        if follow and job_id and not self.nav.find_page(f"progress-{job_id}"):
            from .jobs_ui import progress_page
            self.nav.push(progress_page(self, job_id, started))
        GLib.timeout_add(1500, lambda: self.refresh_auto() and False)

    def show_progress(self, job_id: str | None = None):
        """Siden med detaljert fremdrift for en kopi som kjører (fra statuslinjen eller statusfeltet)."""
        from .jobs_ui import progress_page
        job_id = job_id or next(iter(settings.running_jobs()), None)
        if not job_id:
            self.toast(_("Ingen sikkerhetskopi kjører nå"))
            return
        existing = self.nav.find_page(f"progress-{job_id}")
        if existing:  # allerede åpen: gå dit i stedet for å legge en ny side oppå
            self.nav.pop_to_page(existing)
            return
        self.nav.push(progress_page(self, job_id))

    def forget_job_snapshots(self, repo, job_id: str, name: str):
        """Sletter tidspunktene til en slettet oppgave i bakgrunnen (brukeren valgte «Slett oppgaven og kopiene»)."""
        def work():
            if not repo.available():
                raise repo_mod.RepoError(_("Lagringsstedet er ikke tilgjengelig"))
            return repo.forget_job(job_id)

        def done(count, err):
            if err:
                self._error(_("Kopiene til «{}» ble ikke slettet").format(name),
                            _("{}\n\nOppgaven er slettet, men kopiene ligger fortsatt på lagringsstedet. Når det "
                              "blir fullt, slettes de eldste av dem automatisk.").format(err))
            else:
                self.toast(_("{} tidspunkter er slettet fra lagringsstedet").format(count))
        in_thread(work, done)

    def run_backup(self, sections, dest: Path, password: str | None, encrypt_all: bool):
        status = Adw.StatusPage(icon_name="document-save-symbolic", title=_("Lager sikkerhetskopi"),
                                description=_("Du kan bruke maskinen som vanlig mens dette pågår."))
        b = vbox(12)
        bar = Gtk.ProgressBar(show_text=True)
        current = label("", ("dim-label", "caption"), xalign=0.5, ellipsize=Pango.EllipsizeMode.MIDDLE)
        cancel = pill(_("Avbryt"), suggested=False)
        cancel.set_halign(Gtk.Align.CENTER)
        cancel.set_margin_top(12)
        b.append(bar)
        b.append(current)
        b.append(cancel)
        status.set_child(Adw.Clamp(maximum_size=460, child=b))
        pg = page(_("Sikkerhetskopi"), status)
        pg.set_can_pop(False)
        self.nav.push(pg)
        self.busy, self.cancel_flag = True, False
        cancel.connect("clicked", lambda *_a: setattr(self, "cancel_flag", True))
        last = [0.0]

        def progress(frac, text):
            if frac - last[0] >= 0.005 or frac >= 1:
                last[0] = frac
                GLib.idle_add(lambda: (bar.set_fraction(frac), current.set_label(text)) and False)

        def done(manifest, err):
            self.busy = False
            if isinstance(err, backup.Cancelled):
                self.nav.pop()
                self.toast(_("Sikkerhetskopien ble avbrutt"))
                return
            if err:
                self.nav.pop()
                self._error(_("Sikkerhetskopien feilet"), str(err))
                return
            self.nav.replace([self.start, self._backup_done_page(dest, manifest)])
        in_thread(lambda: backup.write_backup(sections, dest, password, encrypt_all, progress,
                                              lambda: self.cancel_flag), done)

    def _backup_done_page(self, dest: Path, manifest: dict) -> Adw.NavigationPage:
        status = Adw.StatusPage(icon_name="object-select-symbolic", title=_("Sikkerhetskopien er klar"))
        status.add_css_class("success-status")
        status.set_description(GLib.markup_escape_text(
            f"{dest.name}\n" + _("{} i {}").format(fmt_size(dest.stat().st_size), tilde(dest.parent))))
        b = vbox(24)
        btns = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        show = pill(_("Vis i filbehandler"), suggested=False)
        show.connect("clicked", lambda *_a: Gtk.FileLauncher(file=Gio.File.new_for_path(str(dest)))
                     .open_containing_folder(self, None, None, None))
        ok = pill(_("Ferdig"))
        ok.connect("clicked", lambda *_a: self.go_home())
        btns.append(show)
        btns.append(ok)
        b.append(btns)
        counts = Adw.PreferencesGroup(title=_("Dette er med"))
        rows = []
        if manifest.get("encrypted") == "all":
            rows.append(("channel-secure-symbolic", _("Kryptert"), _("Hele sikkerhetskopien er kryptert")))
        elif manifest.get("private"):
            rows.append(("channel-secure-symbolic", _("Kryptert"), _("Nøkler og passord er kryptert")))
        if manifest.get("settings"):
            rows.append(("preferences-desktop-appearance-symbolic", _("GNOME-innstillinger"),
                         _("{} verdier").format(manifest["settings"].get("keys", 0))))
        if manifest.get("wallpaper"):
            rows.append(("preferences-desktop-wallpaper-symbolic", _("Bakgrunnsbilde"), ""))
        if manifest.get("extensions"):
            rows.append(("application-x-addon-symbolic", _("Utvidelser"),
                         _("{} stk.").format(len(manifest["extensions"]))))
        n_apps = len(manifest.get("packages", {}).get("items", [])) + len(manifest.get("flatpaks", {}).get("items", []))
        if n_apps:
            rows.append(("system-software-install-symbolic", _("Programmer"), _("{} stk.").format(n_apps)))
        if manifest.get("files"):
            rows.append(("folder-symbolic", _("Filer og oppsett"),
                         _("{} mapper og filer").format(len(manifest["files"]))))
        for icon, title, sub in rows:
            r = Adw.ActionRow(title=title, subtitle=sub)
            r.add_prefix(Gtk.Image(icon_name=icon))
            counts.add(r)
        b.append(counts)
        if not str(dest).startswith("/run/media"):
            nxt = Adw.PreferencesGroup(title=_("Neste steg"))
            r = Adw.ActionRow(title=_("Kopier filen til et trygt sted"),
                              subtitle=_("En minnepinne, en ekstern disk eller en NAS – før du installerer på nytt."))
            r.add_prefix(Gtk.Image(icon_name="drive-removable-media-symbolic"))
            nxt.add(r)
            if not any(j.get("enabled") for j in settings.jobs()):
                r2 = Adw.ActionRow(title=_("Lag en automatisk sikkerhetskopioppgave"), activatable=True,
                                   subtitle=_("Waypoint tar nye kopier selv, til en disk, USB-disk eller NAS."))
                r2.add_prefix(Gtk.Image(icon_name="document-open-recent-symbolic"))
                r2.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                r2.connect("activated", lambda *_a: (self.go_home(), self.edit_job(None)))
                nxt.add(r2)
            b.append(nxt)
        status.set_child(Adw.Clamp(maximum_size=560, child=b))
        return page(_("Ferdig"), status)

    # ------------------------------------------------------------------ tidspunkter

    def _snapshot_title(self, s: dict) -> str:
        return tray._when(s.get("time", "")[:19]) or s.get("time", "")[:16]

    def _storages(self) -> list[dict]:
        """Lagringsstedene til oppgavene, pluss steder koblet til bare for å gjenopprette (f.eks. på en ny maskin)."""
        out, seen = [], set()
        for cfg in [r.cfg for r in repo_mod.Repo.all_storages()] + getattr(self, "extra_storages", []):
            key = cfg.get("uri") or cfg.get("path")
            if key not in seen:
                seen.add(key)
                out.append(cfg)
        return out

    def _snapshots_page(self, title: str, intro: str, on_pick, include_files: bool) -> Adw.NavigationPage:
        """Tidspunkter på hvert lagringssted (og eventuelt eksporterte filer)."""
        box = vbox(24, 24)
        box.append(label(intro, ("dim-label",), wrap=True))
        stores = vbox(24)
        box.append(stores)

        def add_storage_group(cfg):
            jobs = [j.get("name") for j in settings.jobs()
                    if (j.get("storage") or {}).get("uri") == cfg.get("uri")
                    and (j.get("storage") or {}).get("path") == cfg.get("path")]
            g = Adw.PreferencesGroup(title=GLib.markup_escape_text(repo_mod.describe(cfg)),
                                     description=GLib.markup_escape_text(", ".join(n for n in jobs if n)))
            loading = Adw.ActionRow(title=_("Henter tidspunkter …"))
            loading.add_prefix(Adw.Spinner())
            g.add(loading)
            stores.append(g)
            self._fill_snapshots(g, on_pick, cfg, loading)

        for cfg in self._storages():
            add_storage_group(cfg)
        other = Adw.PreferencesGroup()
        row = Adw.ActionRow(title=_("Koble til et annet lagringssted …") if self._storages() else
                            _("Koble til lagringssted …"), activatable=True,
                            subtitle=_("Disken eller NAS-en der sikkerhetskopiene ligger"))
        row.add_prefix(Gtk.Image(icon_name="drive-harddisk-symbolic"))
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))

        def connected(cfg):
            self.extra_storages = getattr(self, "extra_storages", []) + [cfg]
            add_storage_group(cfg)
        row.connect("activated", lambda *_a: self.show_storage(connected, mode="restore"))
        other.add(row)
        box.append(other)
        if include_files:
            files = Adw.PreferencesGroup(title=_("Fra en fil"),
                                         description=_("En eksportert fil, eller en sikkerhetskopi fra Flytt"))
            open_row = Adw.ActionRow(title=_("Åpne fil …"), activatable=True)
            open_row.add_prefix(Gtk.Image(icon_name="document-open-symbolic"))
            open_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            open_row.connect("activated", lambda *_a: self.choose_backup())
            files.add(open_row)
            self.fill_found(files)
            box.append(files)
        return page(title, scrolled(box))

    def _fill_snapshots(self, group, on_pick, cfg: dict, loading=None):
        def work():
            r = repo_mod.Repo(cfg, cfg.get("_password") or settings.get_password())
            if not r.available():
                raise repo_mod.RepoError(_("Lagringsstedet er ikke tilgjengelig"))
            return r, r.snapshots()

        def done(result, err):
            if loading is not None:
                group.remove(loading)
            if err:
                row = Adw.ActionRow(title=str(err), use_markup=False,
                                    subtitle=_("Koble til disken eller nettverket og prøv igjen."))
                row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
                group.add(row)
                return
            r, snaps = result
            if not snaps:
                group.add(Adw.ActionRow(title=_("Ingen tidspunkter ennå")))
                return
            for s in snaps[:60]:
                summ = s.get("summary") or {}
                sub = []
                job_id = next((t[4:] for t in s.get("tags") or [] if t.startswith("job=")), None)
                job = settings.job(job_id)
                if job:
                    sub.append(job.get("name") or "")
                if s.get("hostname") != repo_mod.host():
                    sub.append(s.get("hostname", ""))
                if summ.get("total_files_processed"):
                    sub.append(_("{} filer").format(summ["total_files_processed"]))
                if summ.get("data_added") is not None:
                    sub.append(_("{} nytt").format(fmt_size(summ["data_added"]) or "0 B"))
                row = Adw.ActionRow(title=self._snapshot_title(s), subtitle=" · ".join(x for x in sub if x),
                                    activatable=True, use_markup=False)
                row.add_prefix(Gtk.Image(icon_name="io.github.waypoint.Waypoint-symbolic"))
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.connect("activated", lambda _r, s=s: on_pick(r, s))
                group.add(row)
            if len(snaps) > 60:
                group.add(Adw.ActionRow(title=_("… og {} eldre").format(len(snaps) - 60)))
        in_thread(work, done)

    def choose_restore_source(self):
        self.nav.push(self._snapshots_page(
            _("Gjenopprett"), _("Velg tidspunktet du vil gå tilbake til. På neste side velger du hva som skal "
                                "tilbake."), self.open_snapshot, include_files=True))

    def open_snapshot(self, r, snap: dict):
        pg, _detail = self._loading(_("Leser tidspunktet"), self._snapshot_title(snap))
        self.nav.push(pg)

        def work():
            b = restore.SnapshotBackup(r, snap)
            return b, b.sections()

        def done(result, err):
            if err:
                self.nav.pop()
                self._error(_("Kunne ikke åpne tidspunktet"), str(err))
                return
            if self.backup_file:
                self.backup_file.cleanup()
            self.backup_file, sections = result
            self.nav.pop()
            self.nav.push(self._restore_choose_page(self.backup_file, sections))
        in_thread(work, done)

    def begin_browse(self):
        self.nav.push(self._snapshots_page(
            _("Hent enkeltfiler"), _("Velg et tidspunkt, finn filen eller mappen og hent den tilbake. Finnes den "
                                     "fra før, får den hentede kopien datoen i navnet, så ingenting overskrives."),
            self._browse_snapshot, include_files=False))

    def _browse_snapshot(self, r, snap: dict):
        self.nav.push(self._dir_page(r, snap, repo_mod.snapshot_home(snap)))

    def _dir_page(self, r, snap: dict, path: str) -> Adw.NavigationPage:
        box = vbox(18, 24)
        crumb = label(f"{self._snapshot_title(snap)} · {tilde(path)}", ("dim-label",), wrap=True)
        box.append(crumb)
        group = Adw.PreferencesGroup()
        loading = Adw.ActionRow(title=_("Henter innhold …"))
        loading.add_prefix(Adw.Spinner())
        group.add(loading)
        box.append(group)
        pg = page(Path(path).name or "/", scrolled(box))

        def fetch(node):
            def work():
                return restore.restore_single(r, snap, node["path"])

            def done(dest, err):
                if err:
                    self._error(_("Kunne ikke hente {}").format(node["name"]), str(err))
                    return
                t = Adw.Toast(title=GLib.markup_escape_text(_("Lagt tilbake: {}").format(tilde(dest))),
                              button_label=_("Vis"), timeout=8)
                t.connect("button-clicked", lambda *_a: Gtk.FileLauncher(file=Gio.File.new_for_path(str(dest)))
                          .open_containing_folder(self, None, None, None))
                self.toasts.add_toast(t)
            self.toast(_("Henter {} …").format(node["name"]))
            in_thread(work, done)

        def done(nodes, err):
            group.remove(loading)
            if err:
                group.add(Adw.ActionRow(title=str(err), use_markup=False))
                return
            if not nodes:
                group.add(Adw.ActionRow(title=_("Mappen er tom")))
            for n in nodes[:500]:
                is_dir = n.get("type") == "dir"
                sub = []
                if not is_dir and n.get("size") is not None:
                    sub.append(fmt_size(n["size"]) or "0 B")
                if n.get("mtime"):
                    sub.append(n["mtime"][:16].replace("T", " "))
                row = Adw.ActionRow(title=n.get("name", "?"), subtitle=" · ".join(sub), use_markup=False,
                                    activatable=is_dir)
                row.add_prefix(Gtk.Image(icon_name="folder-symbolic" if is_dir else "text-x-generic-symbolic"))
                get = Gtk.Button(icon_name="document-revert-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text=_("Hent tilbake"))
                get.add_css_class("flat")
                get.connect("clicked", lambda _b, n=n: fetch(n))
                row.add_suffix(get)
                if is_dir:
                    row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                    row.connect("activated", lambda _r, n=n: self.nav.push(self._dir_page(r, snap, n["path"])))
                group.add(row)
            if len(nodes) > 500:
                group.add(Adw.ActionRow(title=_("… og {} til").format(len(nodes) - 500)))
        in_thread(lambda: r.ls(snap["id"], path), done)
        return pg

    # ------------------------------------------------------------------ gjenoppretting fra fil


    def choose_backup(self):
        dlg = Gtk.FileDialog(title=_("Velg sikkerhetskopi"))
        flt = Gtk.FileFilter(name=_("Sikkerhetskopier fra Waypoint"))
        flt.add_pattern("*.tar.gz")
        flt.add_pattern("*.gpg")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dlg.set_filters(store)
        media = Path("/run/media") / os.environ.get("USER", "")
        dlg.set_initial_folder(Gio.File.new_for_path(str(media if media.is_dir() else Path.home())))

        def chosen(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            self.open_backup(Path(f.get_path()))
        dlg.open(self, None, chosen)

    def open_backup(self, path: Path, password: str | None = None):
        if self.busy:
            return
        if password is None and crypto.is_encrypted(path):
            password = settings.get_password()  # prøver passordet i nøkkelringen først
        pg, _detail = self._loading(_("Leser sikkerhetskopien"), path.name)
        self.nav.push(pg)

        def work():
            b = restore.BackupFile(path, password)
            if b.has_private and not b.private_unlocked:
                stored = settings.get_password()
                if stored:
                    b.unlock(stored)
            return b, b.sections()

        def done(result, err):
            if isinstance(err, (restore.NeedPassword, crypto.BadPassword)):
                self.nav.pop()

                def check(pw):
                    return crypto.check_password(path, pw)
                self.ask_password(_("Sikkerhetskopien er kryptert"),
                                  _("Skriv passordet du valgte da sikkerhetskopien ble laget."),
                                  check, lambda pw: self.open_backup(path, pw))
                return
            if err:
                self.nav.pop()
                self._error(_("Kunne ikke åpne sikkerhetskopien"), str(err))
                return
            if self.backup_file:
                self.backup_file.cleanup()
            self.backup_file, sections = result
            self.nav.replace([self.start, self._restore_choose_page(self.backup_file, sections)])
        in_thread(work, done)

    def _restore_choose_page(self, b: restore.BackupFile, sections: list[Section]) -> Adw.NavigationPage:
        summary = label("", ("dim-label",))
        btn = pill(_("Gjenopprett"))

        def update():
            n = sum(len(g.selected()) for s in sections for g in s.groups)
            summary.set_label(_("{} elementer valgt").format(n) if n else _("Ingenting valgt"))
            btn.set_sensitive(n > 0)

        box = vbox(24, 24)
        src = Adw.PreferencesGroup()
        row = Adw.ActionRow(title=b.path.name, subtitle=b.summary(), use_markup=False)
        row.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic" if b.encrypted_all else "package-x-generic-symbolic"))
        src.add(row)
        box.append(src)
        box.append(label(_("Det som allerede finnes på denne maskinen, er ikke valgt. Programmer installeres fra "
                           "nettet, så maskinen må være koblet til internett."), ("dim-label",), wrap=True))
        for w in Chooser(sections, update).build():
            box.append(w)
        update()

        def confirm():
            d = Adw.AlertDialog(heading=_("Gjenopprette nå?"),
                                body=_("Innstillinger og filer på denne maskinen blir erstattet av dem i "
                                       "sikkerhetskopien. Det som erstattes, legges i ~/.local/share/waypoint/replaced "
                                       "så du kan hente det tilbake.\n\nFor å installere programmer trengs "
                                       "administratorpassordet."))
            d.add_response("cancel", _("Avbryt"))
            d.add_response("go", _("Gjenopprett"))
            d.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
            d.set_default_response("go")
            d.connect("response", lambda _d, r: r == "go" and self.run_restore(b, sections))
            d.present(self)

        def clicked(*_a):
            r = restore.Restorer(b, sections)
            if not r.needs_private_password():
                confirm()
                return

            def skip():
                for s in sections:
                    for g in s.groups:
                        for i in g.items:
                            if i.sensitive and not b.encrypted_all:
                                i.selected = False
                self.toast(_("Nøkler og passord blir ikke gjenopprettet"))
                confirm()
            self.ask_password(_("Nøkler og passord er kryptert"),
                              _("Skriv passordet for å gjenopprette dem, eller avbryt for å hoppe over dem."),
                              b.unlock, lambda _pw: confirm(), skip)
        btn.connect("clicked", clicked)
        return page(_("Velg hva som skal tilbake"), scrolled(box), bottom_bar(summary, btn))

    def run_restore(self, b: restore.BackupFile, sections: list[Section]):
        self.restorer = r = restore.Restorer(b, sections)
        steps = r.steps()
        box = vbox(24, 24)
        head = vbox(6)
        title = label(_("Gjenoppretter …"), ("title-1",), xalign=0.5)
        sub = label(_("Dette kan ta en stund, særlig når programmer lastes ned."), ("dim-label",), xalign=0.5,
                    wrap=True, justify=Gtk.Justification.CENTER)
        head.append(title)
        head.append(sub)
        box.append(head)
        grp = Adw.PreferencesGroup()
        rows = {}
        for st in steps:
            row = Adw.ActionRow(title=st.title, subtitle=_("Venter"), use_markup=False)
            row.set_subtitle_lines(3)
            icon_stack = Gtk.Stack()
            icon_stack.add_named(Gtk.Image(icon_name="content-loading-symbolic", css_classes=["dim-label"]), "wait")
            icon_stack.add_named(Adw.Spinner(), "run")
            icon_stack.add_named(Gtk.Image(icon_name="object-select-symbolic", css_classes=["success"]), "ok")
            icon_stack.add_named(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]), "fail")
            icon_stack.add_named(Gtk.Image(icon_name="window-close-symbolic", css_classes=["dim-label"]), "skip")
            row.add_prefix(icon_stack)
            grp.add(row)
            rows[st.id] = (row, icon_stack)
        box.append(grp)

        buf = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=buf, editable=False, cursor_visible=False, monospace=True,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8, left_margin=10,
                            right_margin=10)
        sw = Gtk.ScrolledWindow(min_content_height=220, child=view)
        sw.add_css_class("card")
        exp = Gtk.Expander(label=_("Vis detaljer"), child=sw)
        box.append(exp)

        btns = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        cancel = pill(_("Avbryt"), suggested=False)
        btns.append(cancel)
        box.append(btns)
        pg = page(_("Gjenoppretting"), scrolled(box, 640))
        pg.set_can_pop(False)
        self.nav.push(pg)
        self.busy, self.cancel_flag = True, False

        def on_cancel(*_a):
            self.cancel_flag = True
            r.cancel()
            cancel.set_sensitive(False)
            cancel.set_label(_("Avbryter …"))
        cancel.connect("clicked", on_cancel)

        def log(text):
            def add():
                buf.insert(buf.get_end_iter(), text + "\n")
                if exp.get_expanded():
                    view.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)
                return False
            if text and not text.startswith("@@"):
                GLib.idle_add(add)

        def set_state(sid, state, text):
            row, stack = rows[sid]
            stack.set_visible_child_name(state)
            row.set_subtitle(text)
            return False

        def work():
            results = []
            for st in steps:
                if self.cancel_flag:
                    GLib.idle_add(set_state, st.id, "skip", _("Hoppet over"))
                    results.append(None)
                    continue
                GLib.idle_add(set_state, st.id, "run", _("Arbeider …"))
                log(f"\n━━ {st.title} ━━")
                try:
                    ok, text = st.func(log)
                except Exception as e:  # noqa: BLE001
                    ok, text = False, _("Feil: {}").format(e)
                    log(text)
                GLib.idle_add(set_state, st.id, "ok" if ok else "fail", text)
                results.append(ok)
            return results

        def done(results, err):
            self.busy = False
            pg.set_can_pop(True)
            btns.remove(cancel)
            failed = [x for x in (results or []) if x is False]
            if err or self.cancel_flag:
                title.set_label(_("Gjenopprettingen ble avbrutt"))
                sub.set_label(_("Det som ble gjort før du avbrøt, er beholdt."))
            elif failed:
                title.set_label(_("Nesten ferdig"))
                sub.set_label(_("Noe gikk ikke som det skulle – se de gule merkene. «Vis detaljer» forteller mer."))
            else:
                title.set_label(_("Ferdig!"))
                sub.set_label(_("Oppsettet ditt er tilbake."))
            if r.needs_logout or any(st.id == "settings" for st in steps):
                sub.set_label(sub.get_label() + " " + _("Logg ut og inn igjen for at utvidelser og alle innstillinger "
                                                        "skal tre i kraft."))
                logout = pill(_("Logg ut nå"))
                logout.connect("clicked", lambda *_a: subprocess.Popen(["gnome-session-quit", "--logout"]))
                btns.append(logout)
            close = pill(_("Tilbake til start"), suggested=False)
            close.connect("clicked", lambda *_a: self.go_home())
            btns.prepend(close)
        in_thread(work, done)

    # ------------------------------------------------------------------ lukking

    def _on_close(self, *_a):
        if not self.busy:
            if self.backup_file:
                self.backup_file.cleanup()
            return False
        d = Adw.AlertDialog(heading=_("Arbeidet er ikke ferdig"),
                            body=_("Hvis du lukker nå, blir det som pågår avbrutt."))
        d.add_response("stay", _("Fortsett"))
        d.add_response("quit", _("Avbryt og lukk"))
        d.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, rid):
            if rid == "quit":
                self.cancel_flag = True
                if self.restorer:
                    self.restorer.cancel()
                self.busy = False
                self.close()
        d.connect("response", resp)
        d.present(self)
        return True


def apply_theme():
    choice = settings.get("theme")
    scheme = {"light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}.get(choice,
                                                                                         Adw.ColorScheme.DEFAULT)
    Adw.StyleManager.get_default().set_color_scheme(scheme)


class App(Adw.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if os.environ.get("WAYPOINT_SCREENSHOT"):  # testmodus: egen forekomst, rører ikke et åpent vindu
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.win: Window | None = None
        self.open_action: str | None = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name("Waypoint")
        apply_theme()
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        theme.add_search_path(str(HERE / "icons"))
        css = Gtk.CssProvider()
        css.load_from_path(str(HERE / "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, cb in (("about", self._about), ("preferences", lambda *_a: self._prefs()),
                         ("browse", lambda *_a: self._do("browse")), ("backup", lambda *_a: self._do("backup")),
                         ("progress", lambda *_a: self._do("progress")),
                         ("quit", lambda *_a: self.win and self.win.close())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        glow = Gio.SimpleAction.new_stateful("glow", None, GLib.Variant("b", bool(settings.get("glow"))))
        glow.connect("change-state", self._glow_changed)
        self.add_action(glow)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("window.close", ["<Control>w"])
        if settings.get("tray") and not os.environ.get("WAYPOINT_SCREENSHOT"):
            tray.sync_autostart()
            tray.start()

    def _window(self) -> Window:
        if not self.win:
            self.win = Window(self)
        return self.win

    def _prefs(self):
        self._do("preferences")

    def _do(self, action: str):
        win = self._window()
        win.present()
        if win.busy:
            return
        if action == "preferences":
            win.show_preferences()
        elif action == "browse":
            win.nav.pop_to_page(win.start)
            win.begin_browse()
        elif action == "backup":
            win.nav.pop_to_page(win.start)
            win.run_job(None)
        elif action == "progress":
            win.nav.pop_to_page(win.start)
            win.show_progress()

    def rebuild(self):
        """Bygger vinduet på nytt, f.eks. etter at språket er endret."""
        if self.win and not self.win.busy:
            old = self.win
            self.win = None
            win = self._window()
            win.set_default_size(old.get_width(), old.get_height())
            win.present()
            old.destroy()

    def do_activate(self):
        win = self._window()
        win.present()
        if self.open_action:
            action, self.open_action = self.open_action, None
            GLib.idle_add(lambda: self._do(action) and False)
        shot = os.environ.get("WAYPOINT_SCREENSHOT")
        if shot:
            if os.environ.get("WAYPOINT_WINDOW_SIZE"):
                w, h = (int(x) for x in os.environ["WAYPOINT_WINDOW_SIZE"].split("x"))
                win.set_default_size(w, h)
            GLib.timeout_add(int(float(os.environ.get("WAYPOINT_SCREENSHOT_DELAY", "1.5")) * 1000), self._screenshot, shot)
            target = os.environ.get("WAYPOINT_PAGE", "")
            if target == "backup":
                win.begin_backup()
            elif target.startswith("restore:"):
                win.open_backup(Path(target[8:]))
            elif target.startswith("preferences"):
                win.show_preferences(target.partition(":")[2] or None)
            elif target == "restore-source":
                win.choose_restore_source()
            elif target == "browse":
                win.begin_browse()
            elif target == "storage":
                win.show_storage()
            elif target.startswith("storage:"):
                job = settings.job(target[8:])
                win.show_storage(current=job and job.get("storage"))
            elif target == "job-new":
                win.edit_job(None)
            elif target == "about":
                self._about()
            elif target.startswith("job:"):
                win.edit_job(target[4:])

    def do_open(self, files, _n, _hint):
        win = self._window()
        win.present()
        if files and files[0].get_path():
            win.open_backup(Path(files[0].get_path()))

    def _screenshot(self, path):
        """Til testing: lagrer vinduet som PNG og avslutter."""
        win = self.win
        if os.environ.get("WAYPOINT_EXPAND"):
            def walk(w):
                if isinstance(w, Adw.ExpanderRow) and os.environ["WAYPOINT_EXPAND"] in w.get_title():
                    w.set_expanded(True)
                c = w.get_first_child()
                while c:
                    walk(c)
                    c = c.get_next_sibling()
            walk(win)
            os.environ.pop("WAYPOINT_EXPAND")
            GLib.timeout_add(500, self._screenshot, path)
            return False
        w, h = win.get_width(), win.get_height()
        paintable = Gtk.WidgetPaintable.new(win)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, w, h)
        node = snap.to_node()
        tex = win.get_native().get_renderer().render_texture(node, None)
        tex.save_to_png(path)
        self.quit()
        return False

    def _glow_changed(self, action, value):
        action.set_state(value)
        settings.set("glow", value.get_boolean())
        if self.win:
            self.win.set_glow(value.get_boolean())

    def _about(self, *_a):
        d = Adw.AboutDialog(
            application_name="Waypoint", application_icon=APP_ID, version=__version__,
            developer_name="From Norway with Love ❤️", developers=["Jan Ivar Sørensen"],
            copyright="© 2026 Jan Ivar Sørensen",
            website="https://github.com/Vindihaaret/waypoint",
            issue_url="https://github.com/Vindihaaret/waypoint/issues",
            license_type=Gtk.License.GPL_3_0,
            comments=_("Sikkerhetskopi for Linux med GNOME.\n\n"
                       "Waypoint tar vare på innstillingene, utvidelsene, programmene og filene dine – automatisk, "
                       "kryptert og bare med det som er endret. Gå tilbake til et tidligere tidspunkt, hent en enkelt "
                       "fil, eller flytt hele oppsettet til en ny installasjon uten å sette opp alt på nytt.\n\n"
                       "Kopiene kan ligge på en ekstern disk, en USB-disk eller en NAS."))
        d.add_acknowledgement_section(_("Bygger på"), [
            "restic https://restic.net",
            "GTK https://gtk.org",
            "libadwaita https://gnome.pages.gitlab.gnome.org/libadwaita/",
            "GnuPG https://gnupg.org",
        ])
        d.present(self.win)


def main(args=(), action: str | None = None):
    """action: «preferences», «browse», «backup» eller «progress» – åpnes i vinduet (også hvis det allerede kjører)."""
    import sys
    app = App()
    if action:
        app.register(None)
        if app.get_is_remote():
            app.activate_action(action, None)
            return 0
        app.open_action = action
    return app.run([sys.argv[0], *args])
