"""Ikon i statusfeltet (StatusNotifierItem + DBusMenu over D-Bus), samme teknikk som i Vifte.

GNOME viser det med utvidelsen «AppIndicator and KStatusNotifierItem Support»; KDE og de fleste andre
skrivebord støtter det direkte. Kjøres som egen prosess («waypoint tray») og startes ved innlogging.
Viser status for automatisk sikkerhetskopi ved å følge med på ~/.local/state/waypoint/status.json.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402

from . import APP_ID, auto, i18n, settings  # noqa: E402
from .i18n import _  # noqa: E402

WATCHER = "org.kde.StatusNotifierWatcher"
BUS_NAME = "io.github.waypoint.Tray"
ICON = Path(__file__).parent / "icons/hicolor/symbolic/apps/io.github.waypoint.Waypoint-symbolic.svg"
AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / \
    "io.github.waypoint.Waypoint-tray.desktop"

SNI_XML = """
<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/><property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/><property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/><property name="AttentionIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/><property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewTitle"/><signal name="NewIcon"/><signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
</interface></node>
"""

MENU_XML = """
<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/><property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout"><arg type="i" name="parentId" direction="in"/><arg type="i" name="recursionDepth" direction="in"/>
    <arg type="as" name="propertyNames" direction="in"/><arg type="u" name="revision" direction="out"/>
    <arg type="(ia{sv}av)" name="layout" direction="out"/></method>
  <method name="GetGroupProperties"><arg type="ai" name="ids" direction="in"/><arg type="as" name="propertyNames" direction="in"/>
    <arg type="a(ia{sv})" name="properties" direction="out"/></method>
  <method name="GetProperty"><arg type="i" name="id" direction="in"/><arg type="s" name="name" direction="in"/>
    <arg type="v" name="value" direction="out"/></method>
  <method name="Event"><arg type="i" name="id" direction="in"/><arg type="s" name="eventId" direction="in"/>
    <arg type="v" name="data" direction="in"/><arg type="u" name="timestamp" direction="in"/></method>
  <method name="EventGroup"><arg type="a(isvu)" name="events" direction="in"/><arg type="ai" name="idErrors" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" name="id" direction="in"/><arg type="b" name="needUpdate" direction="out"/></method>
  <method name="AboutToShowGroup"><arg type="ai" name="ids" direction="in"/><arg type="ai" name="updatesNeeded" direction="out"/>
    <arg type="ai" name="idErrors" direction="out"/></method>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})" name="updatedProps"/><arg type="a(ias)" name="removedProps"/></signal>
  <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
</interface></node>
"""

_SNI = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
_MENU = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]

DOT = {"running": (0x3b, 0x82, 0xf6), "error": (0xe0, 0x1b, 0x24), "unavailable": (0xf6, 0xa3, 0x2b),
       "waiting_power": (0xf6, 0xa3, 0x2b), "waiting_network": (0xf6, 0xa3, 0x2b)}


def state_text(state: str | None, st: dict) -> str | None:
    """Kort tekst for tilstander som trenger forklaring (ellers None)."""
    if state == "running":
        return _("Tar sikkerhetskopi … {} %").format(int((st.get("progress") or 0) * 100))
    if state == "error":
        return _("Siste forsøk feilet: {}").format(st.get("last_error") or "?")
    if state == "unavailable":
        return st.get("hint") or _("Lagringsstedet er ikke tilgjengelig")
    if state == "waiting_power":
        return _("Venter på strøm (går på batteri)")
    if state == "waiting_network":
        return _("Venter på et nett som ikke er målt")
    return None


def render_icon(size: int, dot: tuple[int, int, int] | None) -> tuple[int, int, bytes]:
    """Det hvite symbolikonet som ARGB (nettverksrekkefølge), med en farget prikk for status."""
    svg = ICON.read_text().replace("#2e3436", "#ffffff")
    loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
    loader.set_size(size, size)
    loader.write(svg.encode())
    loader.close()
    pb = loader.get_pixbuf()
    if not pb.get_has_alpha():
        pb = pb.add_alpha(False, 0, 0, 0)
    w, h, stride = pb.get_width(), pb.get_height(), pb.get_rowstride()
    src = pb.get_pixels()
    out = bytearray(w * h * 4)
    for y in range(h):
        row = src[y * stride:y * stride + w * 4]
        o = y * w * 4
        out[o + 0:o + w * 4:4] = row[3::4]
        out[o + 1:o + w * 4:4] = row[0::4]
        out[o + 2:o + w * 4:4] = row[1::4]
        out[o + 3:o + w * 4:4] = row[2::4]
    if dot:
        r = size * 0.2
        cx, cy = size - r - 0.5, size - r - 0.5
        for y in range(h):
            for x in range(w):
                d = ((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5
                if d <= r + 1.5:
                    o = (y * w + x) * 4
                    out[o:o + 4] = bytes((255, 30, 30, 30)) if d > r else bytes((255, *dot))
    return w, h, bytes(out)


class Menu:
    def __init__(self, conn, path):
        self.conn, self.path = conn, path
        self.revision = 1
        self.items: dict[int, dict] = {}
        self.root: list[dict] = []
        conn.register_object(path, _MENU, self._call, self._get, None)

    def set(self, items):
        self.root, self.items = items, {}
        self._number(items)
        self.revision += 1
        self.conn.emit_signal(None, self.path, "com.canonical.dbusmenu", "LayoutUpdated",
                              GLib.Variant("(ui)", (self.revision, 0)))

    def _number(self, items):
        for it in items:
            it["_id"] = len(self.items) + 1
            self.items[it["_id"]] = it

    @staticmethod
    def _props(it):
        if it is None:
            return {"children-display": GLib.Variant("s", "submenu")}
        if it.get("separator"):
            return {"type": GLib.Variant("s", "separator")}
        return {"label": GLib.Variant("s", it["label"].replace("_", "__")),
                "enabled": GLib.Variant("b", it.get("enabled", True))}

    def _layout(self, ident, depth):
        kids = []
        if ident == 0 and depth != 0:
            kids = [GLib.Variant("(ia{sv}av)", (c["_id"], self._props(c), [])) for c in self.root]
        return (ident, self._props(self.items.get(ident)), kids)

    def _call(self, conn, sender, path, iface, method, params, inv):
        if method == "GetLayout":
            parent, depth, _names = params.unpack()
            inv.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout(parent, depth))))
        elif method == "GetGroupProperties":
            ids, _names = params.unpack()
            out = [(i, self._props(self.items.get(i))) for i in ids if i in self.items or i == 0]
            inv.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            i, name = params.unpack()
            inv.return_value(GLib.Variant("(v)", (self._props(self.items.get(i)).get(name, GLib.Variant("s", "")),)))
        elif method in ("Event", "EventGroup"):
            events = params.unpack()[0] if method == "EventGroup" else [params.unpack()]
            for i, event, *_rest in events:
                it = self.items.get(i)
                if event == "clicked" and it and it.get("action"):
                    GLib.idle_add(lambda a=it["action"]: a() and False)
            inv.return_value(None if method == "Event" else GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _get(self, *args):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(args[-1])


class Tray:
    PATH = "/io/github/waypoint/Tray"

    def __init__(self, loop: GLib.MainLoop):
        self.loop = loop
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.menu = Menu(self.conn, "/io/github/waypoint/Menu")
        self.state = None
        self.pixmap = []
        self.tooltip = ("Waypoint", "")
        self.conn.register_object(self.PATH, _SNI, self._call, self._get, None)
        self._started: dict[str, str] = {}
        self._first_refresh = True  # ikke varsle om en kopi som allerede kjørte da ikonet startet
        self._notifications: set[int] = set()
        self.conn.signal_subscribe("org.freedesktop.Notifications", "org.freedesktop.Notifications", "ActionInvoked",
                                   "/org/freedesktop/Notifications", None, Gio.DBusSignalFlags.NONE,
                                   self._action_invoked)
        Gio.bus_watch_name_on_connection(self.conn, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                         lambda *a: self._register(), None)
        settings.state_dir().mkdir(parents=True, exist_ok=True)
        self.mon = Gio.File.new_for_path(str(settings.state_dir())).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self.mon.connect("changed", lambda *a: self._queue_refresh())
        settings.config_dir().mkdir(parents=True, exist_ok=True)
        self.cmon = Gio.File.new_for_path(str(settings.config_dir())).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self.cmon.connect("changed", lambda *a: self._queue_refresh())
        GLib.timeout_add_seconds(60, lambda: self.refresh() or True)  # «for 5 min siden» og neste tidspunkt
        # Se etter nye versjoner kort etter oppstart og deretter hver sjette time (selve sjekken skjer høyst én
        # gang i døgnet, se updater.maybe_update). Kjøres i en tråd, så menyen aldri henger.
        GLib.timeout_add_seconds(120, lambda: self._update_check() and False)
        GLib.timeout_add_seconds(6 * 3600, lambda: self._update_check() or True)
        self.refresh()

    def _queue_refresh(self):
        """Statusfilen skrives ofte under en kopi; bygg menyen på nytt høyst et par ganger i sekundet."""
        if not getattr(self, "_refresh_pending", False):
            self._refresh_pending = True

            def run():
                self._refresh_pending = False
                self.refresh()
                return False
            GLib.timeout_add(500, run)

    def _update_check(self):
        import threading

        def work():
            try:
                from . import updater
                updater.maybe_update(log=lambda t: None)
            except Exception:  # noqa: BLE001 – en feil i oppdateringen skal ikke stoppe ikonet
                pass
        threading.Thread(target=work, daemon=True).start()

    def _register(self):
        self.conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                       GLib.Variant("(s)", (self.PATH,)), None, Gio.DBusCallFlags.NONE, 2000, None, None)

    def _emit(self, sig):
        self.conn.emit_signal(None, self.PATH, "org.kde.StatusNotifierItem", sig, None)

    def _get(self, conn, sender, path, iface, prop):
        v = {
            "Category": ("s", "ApplicationStatus"), "Id": ("s", "waypoint"), "Title": ("s", "Waypoint"),
            "Status": ("s", "Active"), "WindowId": ("i", 0), "IconName": ("s", ""),
            "IconThemePath": ("s", ""), "IconPixmap": ("a(iiay)", self.pixmap),
            "OverlayIconName": ("s", ""), "AttentionIconName": ("s", ""),
            "ToolTip": ("(sa(iiay)ss)", ("", [], self.tooltip[0], self.tooltip[1])),
            "ItemIsMenu": ("b", False), "Menu": ("o", self.menu.path),
        }.get(prop)
        return GLib.Variant(*v) if v else None

    def _call(self, conn, sender, path, iface, method, params, inv):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: self.open_app() and False)
        inv.return_value(None)

    # ------------------------------------------------------------ handlinger

    def open_app(self, *extra):
        subprocess.Popen([*auto.command(), *extra], start_new_session=True)

    def hide(self):
        settings.set("tray", False)
        AUTOSTART.unlink(missing_ok=True)
        self.conn.emit_signal(None, self.PATH, "org.kde.StatusNotifierItem", "NewStatus",
                              GLib.Variant("(s)", ("Passive",)))
        GLib.timeout_add(300, lambda: self.loop.quit() or False)

    def quit_all(self):
        """Lukker et åpent Waypoint-vindu og statusfeltikonet. Ikonet kommer tilbake ved neste innlogging hvis
        «Start ved innlogging» er på, og de automatiske kopiene fortsetter (de kjøres av systemd-timeren)."""
        try:
            actions = Gio.DBusActionGroup.get(self.conn, APP_ID, "/" + APP_ID.replace(".", "/"))
            actions.activate_action("quit", None)  # gjør ingenting hvis vinduet ikke er åpent
        except GLib.Error:
            pass
        self.conn.emit_signal(None, self.PATH, "org.kde.StatusNotifierItem", "NewStatus",
                              GLib.Variant("(s)", ("Passive",)))
        GLib.timeout_add(300, lambda: self.loop.quit() or False)

    # ------------------------------------------------------------ status

    def refresh(self):
        cfg = settings.load()
        if not cfg.get("tray", True):
            self.loop.quit()
            return
        if cfg.get("language") != getattr(self, "_language", cfg.get("language")):
            i18n.reset()
            self.state = None
        self._language = cfg.get("language")
        jobs = settings.jobs()
        status = settings.read_status()["jobs"]
        self._notify_started(cfg, jobs, status)

        # Én linje per oppgave, og samlet tilstand for prikken på ikonet (kjører > feil > venter).
        lines, worst = [], None
        rank = {"running": 4, "error": 3, "unavailable": 2, "waiting_power": 1, "waiting_network": 1}
        for j in jobs[:6]:
            st = status.get(j["id"], {})
            lines.append(f"{j.get('name') or '?'}: {job_line(st)}")
            state = st.get("state")
            relevant = state in ("running", "error") or (j.get("enabled") and state in rank)
            if relevant and rank.get(state, 0) > rank.get(worst, 0):
                worst = state
        if not jobs:
            lines.append(_("Ingen sikkerhetskopioppgaver ennå"))
        nexts = [n for n in (auto.next_time(j, status.get(j["id"], {})) for j in jobs) if n]
        if not any(j.get("enabled") for j in jobs):
            next_line = _("Automatisk sikkerhetskopi er av")
        elif nexts and min(nexts) <= datetime.now():
            next_line = _("Neste: snart")
        else:
            next_line = _("Neste: {}").format(_when(min(nexts).isoformat()) if nexts else "?")
        dot = DOT.get(worst)
        if dot != self.state:
            self.state = dot
            self.pixmap = [render_icon(22, dot), render_icon(44, dot)]
            self._emit("NewIcon")
        self.tooltip = ("Waypoint", "\n".join(lines + [next_line]))
        self._emit("NewToolTip")
        running = any(st.get("state") == "running" for st in status.values())
        items = [{"label": line, "enabled": False} for line in lines]
        items += [{"label": next_line, "enabled": False}, {"separator": True}]
        if running:
            items.append({"label": _("Vis fremdrift"), "action": lambda: self.open_app("progress")})
        items += [{"label": _("Ta sikkerhetskopi nå"), "action": auto.start_now, "enabled": bool(jobs)},
                  {"label": _("Hent enkeltfiler …"), "action": lambda: self.open_app("browse"), "enabled": bool(jobs)},
                  {"label": _("Åpne Waypoint"), "action": self.open_app},
                  {"label": _("Innstillinger …"), "action": lambda: self.open_app("preferences")},
                  {"separator": True},
                  {"label": _("Skjul ikonet"), "action": self.hide},
                  {"label": _("Avslutt Waypoint"), "action": self.quit_all}]
        self.menu.set(items)

    # ------------------------------------------------------------ varsel når en kopi starter

    def _notify_started(self, cfg: dict, jobs: list[dict], status: dict):
        """Varsel med «Vis fremdrift» når en kopi starter i bakgrunnen (ikke når den ble startet fra vinduet)."""
        names = {j["id"]: j.get("name") or "?" for j in jobs}
        for jid, st in status.items():
            if st.get("state") != "running" or not st.get("started"):
                continue
            if self._started.get(jid) == st["started"]:
                continue
            self._started[jid] = st["started"]
            if self._first_refresh or st.get("interactive") or not cfg.get("notify_start", True):
                continue
            self._send(_("Sikkerhetskopi startet"), names.get(jid, "?"))
        self._first_refresh = False

    def _send(self, title: str, body: str):
        try:
            r = self.conn.call_sync(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
                "Notify", GLib.Variant("(susssasa{sv}i)", (
                    "Waypoint", 0, APP_ID, title, body, ["default", _("Vis fremdrift"), "progress", _("Vis fremdrift")],
                    {"desktop-entry": GLib.Variant("s", APP_ID)}, -1)),
                None, Gio.DBusCallFlags.NONE, 3000, None)
            self._notifications.add(r.unpack()[0])
        except GLib.Error:
            pass

    def _action_invoked(self, _conn, _sender, _path, _iface, _signal, params):
        nid, action = params.unpack()
        if nid in self._notifications and action in ("default", "progress"):
            self.open_app("progress")


def job_line(st: dict) -> str:
    """Kort status for én oppgave i menyen."""
    state = st.get("state")
    if st.get("queued"):
        return _("i kø")
    text = state_text(state, st)
    if text:
        return text
    last = _when(st.get("last_success"))
    return _("siste {}").format(last) if last else _("ingen kopi ennå")


def _when(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso).replace(tzinfo=None)
    except ValueError:
        return None
    now = datetime.now()
    clock = t.strftime("%H:%M")
    if t.date() == now.date():
        return _("i dag {}").format(clock)
    if (t.date() - now.date()).days == -1:
        return _("i går {}").format(clock)
    if (t.date() - now.date()).days == 1:
        return _("i morgen {}").format(clock)
    return t.strftime(_("%d.%m.%Y kl. %H:%M"))


def sync_autostart():
    """Autostart-filen finnes bare når både «Vis ikon i statusfeltet» og «Start ved innlogging» er på."""
    write_autostart(bool(settings.get("tray")) and bool(settings.get("autostart")))


def write_autostart(enabled: bool):
    if enabled:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        cmd = " ".join(auto.command())
        AUTOSTART.write_text("[Desktop Entry]\nType=Application\nName=Waypoint\n"
                             f"Exec={cmd} tray\nIcon={APP_ID}\nNoDisplay=true\nTerminal=false\n"
                             "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=5\n")
    else:
        AUTOSTART.unlink(missing_ok=True)


def is_running() -> bool:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        r = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                          GLib.Variant("(s)", (BUS_NAME,)), None, Gio.DBusCallFlags.NONE, 1000, None)
        return r.unpack()[0]
    except GLib.Error:
        return False


def start():
    """Starter statusfeltikonet hvis det skal vises og ikke allerede kjører."""
    if settings.get("tray") and not is_running():
        subprocess.Popen([*auto.command(), "tray"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    if not settings.get("tray"):
        return 0
    loop = GLib.MainLoop()
    owned = {"ok": None}

    def acquired(*_a):
        owned["ok"] = True
        Tray(loop)

    def lost(*_a):
        if owned["ok"] is None:
            print(_("Statusfeltikonet kjører allerede."), file=sys.stderr)
        loop.quit()
    Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, acquired, lost)
    loop.run()
    return 0
