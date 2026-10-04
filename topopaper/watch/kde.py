"""KDE Plasma (KWin 6) backend, best effort.

KWin has no IPC that lists windows, so a small KWin script
(data/kwin/topopaper-covered.js) runs inside KWin and calls back over D-Bus:
this backend owns `io.github.aidenwboudr.topopaper.Watch` on the session bus,
exports one method, SetCovered(b), loads the script through
org.kde.KWin /Scripting, and reloads it whenever KWin (re)appears.

Needs PyGObject (Gio only, no GUI toolkit). Without it, or if KWin refuses
the script, the watcher falls back to "never covered": the wallpaper keeps
animating, which costs a little power but nothing else.
"""
import os
import signal

from .. import paths
from . import log, run_fallback

NAME = "io.github.aidenwboudr.topopaper.Watch"
OBJ = "/io/github/aidenwboudr/topopaper/Watch"
PLUGIN = "topopaper-covered"
XML = f"""<node><interface name="{NAME}">
  <method name="SetCovered"><arg type="b" name="covered" direction="in"/></method>
</interface></node>"""


def script_path():
    return paths.share_dir() / "kwin" / "topopaper-covered.js"


def covered_now():
    """No query API on KWin: the script pushes changes. Report unknown as 0."""
    return False


def _gio():
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        return Gio, GLib
    except (ImportError, ValueError):
        return None


def load_script(Gio, GLib, bus):
    """Unload any previous copy, load ours, start it. True on success."""
    def call(path, iface, method, args, ret):
        return bus.call_sync("org.kde.KWin", path, iface, method, args,
                             GLib.VariantType.new(ret) if ret else None,
                             Gio.DBusCallFlags.NONE, 5000, None)
    try:
        call("/Scripting", "org.kde.kwin.Scripting", "unloadScript",
             GLib.Variant("(s)", (PLUGIN,)), "(b)")
        sid = call("/Scripting", "org.kde.kwin.Scripting", "loadScript",
                   GLib.Variant("(ss)", (str(script_path()), PLUGIN)), "(i)").unpack()[0]
        if sid < 0:
            log("kde: KWin refused the script")
            return False
        try:
            call(f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run", None, None)
        except GLib.Error:
            call("/Scripting", "org.kde.kwin.Scripting", "start", None, None)
        log(f"kde: KWin script loaded (id {sid})")
        return True
    except GLib.Error as e:
        log(f"kde: could not load the KWin script: {e.message}")
        return False


def unload_script():
    g = _gio()
    if not g:
        return
    Gio, GLib = g
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                      GLib.Variant("(s)", (PLUGIN,)), None, Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error:
        pass


def run(flag):
    g = _gio()
    if not g or not script_path().is_file() or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        log("kde: needs PyGObject, a session bus and the bundled KWin script")
        run_fallback(flag)
    Gio, GLib = g
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    node = Gio.DBusNodeInfo.new_for_xml(XML)

    def on_call(conn, sender, path, iface, method, params, inv):
        if method == "SetCovered":
            flag.set(params.unpack()[0])
        inv.return_value(None)

    bus.register_object(OBJ, node.interfaces[0], on_call, None, None)
    Gio.bus_own_name_on_connection(bus, NAME, Gio.BusNameOwnerFlags.REPLACE, None, None)
    flag.set(False)

    def appeared(*_):
        if not load_script(Gio, GLib, bus):
            flag.set(False)

    def vanished(*_):
        flag.set(False)                 # KWin restarting: show the wallpaper

    Gio.bus_watch_name_on_connection(bus, "org.kde.KWin", Gio.BusNameWatcherFlags.NONE,
                                     appeared, vanished)
    loop = GLib.MainLoop()
    for sig in (signal.SIGTERM, signal.SIGINT):     # unload the script on the way out
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, loop.quit)
    try:
        loop.run()
    finally:
        unload_script()
