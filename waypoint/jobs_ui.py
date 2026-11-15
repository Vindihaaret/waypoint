"""Sikkerhetskopioppgaver i vinduet: listen på forsiden, siden for å lage og redigere en oppgave, statuslinjen
nederst i vinduet mens en kopi kjører, og siden med detaljert fremdrift."""

from __future__ import annotations

import time
from datetime import datetime

from gi.repository import Adw, GLib, Gtk, Pango

from . import auto, repo as repo_mod, settings, tray
from .i18n import N_, _

PRESETS = [("all", N_("Alt")), ("files", N_("Bare personlige filer")),
           ("setup", N_("Bare oppsett og innstillinger"))]
INTERVALS = [("hourly", N_("Hver time")), ("daily", N_("Hver dag")), ("weekly", N_("Hver uke"))]
PRESET_ICONS = {"all": "drive-harddisk-symbolic", "files": "user-home-symbolic",
                "setup": "preferences-desktop-appearance-symbolic"}


SHORT = {"all": N_("Alt"), "files": N_("Personlige filer"), "setup": N_("Oppsett og innstillinger")}


def preset_label(job: dict) -> str:
    p = SHORT.get(job.get("preset") or "all", SHORT["all"])
    return _(p) + (" " + _("(tilpasset)") if job.get("choices") else "")


def schedule_label(job: dict) -> str:
    return _(dict(INTERVALS).get(job.get("interval"), INTERVALS[1][1])) if job.get("enabled") else _("Bare manuelt")


def status_line(job: dict, st: dict) -> str:
    state = st.get("state")
    if state == "running":
        return _("Tar sikkerhetskopi … {} %").format(int((st.get("progress") or 0) * 100))
    if st.get("queued"):
        return _("I kø – starter når den andre kopien er ferdig")
    text = tray.state_text(state, st)
    if text:
        return text
    last = tray._when(st.get("last_success"))
    return _("Siste sikkerhetskopi: {}").format(last) if last else _("Ingen sikkerhetskopi ennå")


def new_job() -> dict:
    return {"id": settings.new_job_id(), "name": "", "enabled": True, "interval": "daily", "preset": "all",
            "choices": {}, "storage": None}


# ======================================================================= listen på forsiden

def chip_state(job: dict, st: dict) -> tuple[str, str]:
    """(prikkens farge, kort tekst) for statusbrikken i oppgavelisten."""
    state = st.get("state")
    if st.get("queued"):
        return "live", _("I kø – starter når den andre kopien er ferdig")
    if state in ("error",):
        return "error", tray.state_text(state, st) or ""
    if state in ("unavailable", "waiting_power", "waiting_network"):
        return "warn", tray.state_text(state, st) or ""
    last = tray._when(st.get("last_success"))
    when = (last[:1].upper() + last[1:]) if last else _("Ingen kopi ennå")
    return ("ok" if last else "none"), f"{when} · {schedule_label(job)}"


class JobRow(Adw.PreferencesRow):
    """Én oppgave: navn, innhold → lagringssted, og en statusbrikke (eller fremdrift mens den kjører)."""

    def __init__(self, win, job: dict):
        super().__init__(activatable=True)
        self.win, self.job_id = win, job["id"]
        box = Gtk.Box(spacing=12, margin_top=9, margin_bottom=9, margin_start=14, margin_end=10)
        self.icon = Gtk.Image(valign=Gtk.Align.CENTER)
        box.append(self.icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.sub.add_css_class("dim-label")
        self.sub.add_css_class("caption")
        self.chip = Gtk.Box(spacing=5, halign=Gtk.Align.START, margin_top=3)
        self.chip.add_css_class("wp-chip")
        self.dot = Gtk.Box(valign=Gtk.Align.CENTER)
        self.dot.add_css_class("wp-dot")
        self.chip_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.END)
        self.chip.append(self.dot)
        self.chip.append(self.chip_label)
        self.bar = Gtk.ProgressBar(margin_top=6, visible=False)
        self.bar.add_css_class("job-progress")
        for w in (self.title, self.sub, self.chip, self.bar):
            text.append(w)
        box.append(text)
        self.play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text=_("Ta sikkerhetskopi nå"))
        self.play.add_css_class("flat")
        self.play.connect("clicked", lambda *_a: self.win.run_job(self.job_id))
        box.append(self.play)
        box.append(Gtk.Image(icon_name="go-next-symbolic", valign=Gtk.Align.CENTER))
        self.set_child(box)

    def update(self, job: dict, st: dict):
        name = job.get("name") or _("Uten navn")
        self.set_title(name)  # for skjermlesere
        self.title.set_label(name)
        self.icon.set_from_icon_name(PRESET_ICONS.get(job.get("preset") or "all", PRESET_ICONS["all"]))
        where = repo_mod.describe(job.get("storage")) if job.get("storage") else _("Lagringssted er ikke valgt")
        self.sub.set_label(f"{preset_label(job)} → {where}")
        running = st.get("state") == "running"
        self.bar.set_visible(running)
        self.chip.set_visible(not running)
        self.play.set_visible(not running)
        if running:
            self.bar.set_fraction(st.get("progress") or 0)
        else:
            color, text = chip_state(job, st)
            for c in ("ok", "live", "warn", "error"):
                self.dot.remove_css_class(c)
            if color != "none":
                self.dot.add_css_class(color)
            self.chip_label.set_label(text)
        self.play.set_sensitive(not st.get("queued") and bool(job.get("storage")))


class JobsGroup(Adw.PreferencesGroup):
    """«Sikkerhetskopioppgaver» på forsiden: én rad per oppgave og «Legg til oppgave»."""

    def __init__(self, win):
        super().__init__(title=_("Sikkerhetskopioppgaver"),
                         description=_("Hver oppgave kopierer det du velger til et lagringssted."))
        self.win = win
        self.rows: dict[str, JobRow] = {}
        self.add_row = Adw.ActionRow(title=_("Legg til oppgave"), activatable=True)
        self.add_row.add_prefix(Gtk.Image(icon_name="list-add-symbolic"))
        self.add_row.connect("activated", lambda *_a: self.win.edit_job(None))
        self.add(self.add_row)
        self.refresh()

    def _connect_activation(self):
        """Egne rader (PreferencesRow) får ikke «activated»; lytt på listen de ligger i i stedet."""
        lb = self.add_row.get_parent()
        if isinstance(lb, Gtk.ListBox) and not getattr(self, "_lb_connected", False):
            self._lb_connected = True
            lb.connect("row-activated", lambda _lb, row: isinstance(row, JobRow) and self.win.edit_job(row.job_id))

    def refresh(self):
        jobs = settings.jobs()
        ids = [j["id"] for j in jobs]
        if ids != list(self.rows):
            for r in self.rows.values():
                self.remove(r)
            self.remove(self.add_row)
            self.rows = {}
            for j in jobs:
                row = JobRow(self.win, j)
                self.rows[j["id"]] = row
                self.add(row)
            self.add(self.add_row)
            self._connect_activation()
            self.add_row.set_title(_("Legg til oppgave") if jobs else _("Lag din første sikkerhetskopioppgave"))
        status = settings.read_status()["jobs"]
        for j in jobs:
            self.rows[j["id"]].update(j, status.get(j["id"], {}))


# ======================================================================= statuslinjen nederst

class StatusBar(Gtk.Revealer):
    """Vises nederst i vinduet mens en sikkerhetskopi kjører. «Vis» åpner detaljene, «×» skjuler linjen til neste
    kopi starter – kopien fortsetter uansett."""

    def __init__(self, win):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.win = win
        self.dismissed: set[str] = set()   # «jobb-id:started» som brukeren har skjult
        self.job_id: str | None = None
        box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=14, margin_end=8)
        box.add_css_class("status-bar")
        box.append(Adw.Spinner(valign=Gtk.Align.CENTER))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("heading")
        self.bar = Gtk.ProgressBar()
        self.detail = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.detail.add_css_class("dim-label")
        self.detail.add_css_class("caption")
        text.append(self.title)
        text.append(self.bar)
        text.append(self.detail)
        box.append(text)
        show = Gtk.Button(label=_("Vis"), valign=Gtk.Align.CENTER)
        show.connect("clicked", lambda *_a: self.job_id and self.win.show_progress(self.job_id))
        box.append(show)
        hide = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER, tooltip_text=_("Skjul"))
        hide.add_css_class("flat")
        hide.connect("clicked", lambda *_a: self._dismiss())
        box.append(hide)
        self.set_child(box)
        self._key = None

    def _dismiss(self):
        if self._key:
            self.dismissed.add(self._key)
        self.set_reveal_child(False)

    def refresh(self):
        status = settings.read_status()["jobs"]
        running = [(jid, st) for jid, st in status.items() if st.get("state") == "running"]
        if not running:
            self.job_id = self._key = None
            self.set_reveal_child(False)
            return
        jid, st = running[0]
        self.job_id = jid
        self._key = f"{jid}:{st.get('started')}"
        job = settings.job(jid) or {}
        frac = st.get("progress") or 0
        self.title.set_label(_("Tar sikkerhetskopi: {} – {} %").format(job.get("name") or "?", int(frac * 100)))
        self.bar.set_fraction(frac)
        cur = st.get("current") or ""
        self.detail.set_label(cur.replace(GLib.get_home_dir(), "~", 1) if cur else _("Forbereder …"))
        self.set_reveal_child(self._key not in self.dismissed)


# ======================================================================= detaljert fremdrift

def progress_page(win, job_id: str, started_at: float | None = None) -> Adw.NavigationPage:
    """Følger én oppgave. started_at: når vinduet ba om kopien (for å se om den faktisk startet)."""
    from .app import fmt_size, label, page, pill, tilde, vbox
    job = settings.job(job_id) or {"name": "?"}
    status = Adw.StatusPage(icon_name="document-save-symbolic", title=job.get("name") or "?",
                            description=_("Du kan lukke vinduet – kopien fortsetter i bakgrunnen."))
    b = vbox(12)
    bar = Gtk.ProgressBar(show_text=True)
    current = label(_("Starter …"), ("dim-label", "caption"), xalign=0.5, ellipsize=Pango.EllipsizeMode.MIDDLE)
    ok = pill(_("Tilbake til start"), suggested=False)
    ok.set_halign(Gtk.Align.CENTER)
    ok.set_margin_top(12)
    ok.connect("clicked", lambda *_a: win.go_home())
    b.append(bar)
    b.append(current)
    b.append(ok)
    status.set_child(Adw.Clamp(maximum_size=460, child=b))
    pg = page(_("Sikkerhetskopi"), status)

    def attempted() -> bool:
        if started_at is None:
            return True
        try:
            st = settings.job_status(job_id)
            return datetime.fromisoformat(st.get("last_attempt") or "").timestamp() >= started_at - 1
        except ValueError:
            return False

    # Én poller om gangen: stoppes når siden skjules (lukkes eller dekkes), startes igjen når den vises.
    state = {"alive": True, "polling": False, "done": False}

    def start_polling():
        if not state["polling"] and not state["done"]:
            state["polling"] = True
            GLib.timeout_add(700, poll)

    def shown(*_a):
        state["alive"] = True
        start_polling()
    pg.connect("hidden", lambda *_a: state.__setitem__("alive", False))
    pg.connect("shown", shown)

    def poll():
        if not state["alive"]:
            state["polling"] = False
            return False
        st = settings.job_status(job_id)
        if st.get("state") == "running" or st.get("queued"):
            bar.set_fraction(st.get("progress") or 0)
            current.set_label(tilde(st.get("current") or "") or (_("I kø – starter når den andre kopien er ferdig")
                                                                  if st.get("queued") else _("Forbereder …")))
            return True
        if not attempted():
            if time.time() - started_at < 30:
                return True
            st = {**st, "state": "error", "last_error": _("Sikkerhetskopien startet ikke. Prøv igjen.")}
        bar.set_visible(False)
        if st.get("state") == "ok":
            status.set_icon_name("object-select-symbolic")
            status.add_css_class("success-status")
            status.set_title(_("Sikkerhetskopien er lagret"))
            current.set_label(_("{} nye eller endrede data · {} filer kontrollert").format(
                fmt_size(st.get("last_added") or 0) or "0 B", st.get("last_files") or 0))
        else:
            status.set_icon_name("dialog-warning-symbolic")
            status.set_title(_("Sikkerhetskopien ble ikke tatt"))
            current.set_label(st.get("last_error") or tray.state_text(st.get("state"), st) or "")
        ok.add_css_class("suggested-action")
        state["done"] = True
        state["polling"] = False
        return False
    pg.set_tag(f"progress-{job_id}")
    start_polling()
    return pg


# ======================================================================= lage og redigere en oppgave

def job_page(win, job: dict | None) -> Adw.NavigationPage:
    from .app import bottom_bar, label, page, pill, scrolled, vbox
    is_new = job is None
    job = dict(job or new_job())
    box = vbox(24, 24)

    g = Adw.PreferencesGroup()
    name = Adw.EntryRow(title=_("Navn på oppgaven"), text=job.get("name", ""))
    g.add(name)
    box.append(g)

    what = Adw.PreferencesGroup(title=_("Hva skal kopieres"))
    keys = [k for k, _l in PRESETS]
    preset = Adw.ComboRow(title=_("Innhold"), model=Gtk.StringList.new([_(lbl) for _k, lbl in PRESETS]))
    preset.add_prefix(Gtk.Image(icon_name="view-list-symbolic"))
    preset.set_selected(keys.index(job.get("preset")) if job.get("preset") in keys else 0)
    custom = Adw.ActionRow(title=_("Tilpass innholdet …"), activatable=True)
    custom.add_prefix(Gtk.Image(icon_name="emblem-system-symbolic"))
    custom.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
    what.add(preset)
    what.add(custom)
    box.append(what)

    where = Adw.PreferencesGroup(title=_("Hvor"))
    storage_row = Adw.ActionRow(title=_("Lagringssted"), use_markup=False)
    storage_row.add_prefix(Gtk.Image(icon_name="drive-harddisk-symbolic"))
    storage_btn = Gtk.Button(valign=Gtk.Align.CENTER)
    storage_row.add_suffix(storage_btn)
    where.add(storage_row)
    box.append(where)

    when = Adw.PreferencesGroup(title=_("Når"))
    automatic = Adw.SwitchRow(title=_("Automatisk"), active=bool(job.get("enabled")),
                              subtitle=_("Tas i bakgrunnen, også når Waypoint ikke er åpent"))
    ikeys = [k for k, _l in INTERVALS]
    often = Adw.ComboRow(title=_("Hvor ofte"), model=Gtk.StringList.new([_(lbl) for _k, lbl in INTERVALS]))
    often.set_selected(ikeys.index(job.get("interval")) if job.get("interval") in ikeys else 1)
    when.add(automatic)
    when.add(often)
    box.append(when)

    if not is_new:
        danger = Adw.PreferencesGroup()
        delete = Adw.ButtonRow(title=_("Slett oppgaven"))
        delete.add_css_class("destructive-action")
        danger.add(delete)
        box.append(danger)

    summary = label("", ("dim-label",), wrap=True)
    save = pill(_("Lagre oppgaven") if not is_new else _("Legg til oppgaven"))

    def refresh():
        storage_row.set_subtitle(repo_mod.describe(job.get("storage")) if job.get("storage") else
                                 _("Ikke valgt"))
        storage_btn.set_label(_("Endre …") if job.get("storage") else _("Velg …"))
        if job.get("storage"):
            storage_btn.remove_css_class("suggested-action")
        else:
            storage_btn.add_css_class("suggested-action")
        custom.set_subtitle(_("Egne valg") if job.get("choices") else _("Standardvalgene for «{}»").format(
            _(dict(PRESETS)[job.get("preset") or "all"])))
        often.set_sensitive(automatic.get_active())
        problem = None
        if not name.get_text().strip():
            problem = _("Gi oppgaven et navn")
        elif not job.get("storage"):
            problem = _("Velg et lagringssted")
        summary.set_label(problem or "")
        save.set_sensitive(problem is None)

    def preset_changed(*_a):
        job["preset"] = keys[preset.get_selected()]
        job["choices"] = {}  # nytt grunnvalg: egne justeringer gjaldt det gamle
        if not name.get_text().strip() or name.get_text() in [_(lbl) for _k, lbl in PRESETS]:
            name.set_text(_(dict(PRESETS)[job["preset"]]) if job["preset"] != "all" else "")
        refresh()
    preset.connect("notify::selected", preset_changed)
    name.connect("changed", lambda *_a: refresh())
    automatic.connect("notify::active", lambda *_a: refresh())

    def pick_storage(*_a):
        def chosen(cfg):
            job["storage"] = cfg
            refresh()
        win.show_storage(chosen, job.get("storage"))
    storage_btn.connect("clicked", pick_storage)

    def tune(*_a):
        def done(choices):
            job["choices"] = choices
            refresh()
        win.choose_job_content(job, done)
    custom.connect("activated", tune)

    def do_save(*_a):
        job["name"] = name.get_text().strip()
        job["enabled"] = automatic.get_active()
        job["interval"] = ikeys[often.get_selected()]
        old = settings.job(job["id"])
        settings.save_job(job)
        if job["enabled"] and (not old or not old.get("enabled")):
            settings.update_job_status(job["id"], enabled_since=auto.now_iso())
        if not auto.sync_timer():
            win.toast(_("Kunne ikke starte systemd-timeren"))
        if job["enabled"] and settings.get("tray"):
            tray.sync_autostart()
            tray.start()
        win.go_home()
        win.toast(_("Oppgaven «{}» er lagret").format(job["name"]))
    save.connect("clicked", do_save)

    if not is_new:
        def confirm_delete(*_a):
            st = settings.job_status(job["id"])
            if st.get("state") == "running" or st.get("queued"):
                d = Adw.AlertDialog(heading=_("Oppgaven kjører nå"),
                                    body=_("Vent til sikkerhetskopien er ferdig før du sletter oppgaven."))
                d.add_response("ok", _("OK"))
                d.present(win)
                return
            d = Adw.AlertDialog(heading=_("Slette «{}»?").format(job.get("name")),
                                body=_("Du kan la sikkerhetskopiene som allerede er tatt, bli liggende på "
                                       "lagringsstedet – da kan de fortsatt gjenopprettes – eller slette dem også og "
                                       "frigjøre plassen."))
            d.add_response("cancel", _("Avbryt"))
            d.add_response("keep", _("Slett bare oppgaven"))
            d.add_response("all", _("Slett oppgaven og kopiene"))
            d.set_response_appearance("all", Adw.ResponseAppearance.DESTRUCTIVE)
            d.set_close_response("cancel")

            def resp(_d, r):
                if r not in ("keep", "all"):
                    return
                repo = repo_mod.Repo.for_job(job) if r == "all" else None
                settings.delete_job(job["id"])
                auto.sync_timer()
                win.go_home()
                win.toast(_("Oppgaven er slettet"))
                if repo:
                    win.forget_job_snapshots(repo, job["id"], job.get("name") or "")
            d.connect("response", resp)
            d.present(win)
        delete.connect("activated", confirm_delete)

    refresh()
    return page(_("Ny oppgave") if is_new else (job.get("name") or _("Oppgave")), scrolled(box),
                bottom_bar(summary, save))
