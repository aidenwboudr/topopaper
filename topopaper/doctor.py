"""`topopaper-ctl doctor`: is everything topopaper needs actually here?

Each check returns {id, title, status: ok|warn|fail, detail, fix}. The
settings app shows the same list (run_checks()); the CLI prints it, or JSON
with --json. --offline skips the network probes.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.parse

from . import paths, util

HOSTS = [
    ("tiles", "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/0/0/0.png",
     "elevation tiles (building maps)"),
    ("overpass", "https://overpass-api.de/api/interpreter", "OpenStreetMap features (labels, lifts)"),
    ("nominatim", "https://nominatim.openstreetmap.org/status", "place search"),
    ("open-meteo", "https://api.open-meteo.com/v1/forecast", "weather"),
]
SUPPORTED = {"sway", "hyprland", "niri", "kde", "river", "wayfire"}
MARK = {"ok": "✓", "warn": "!", "fail": "✗"}


def check(id, title, status, detail="", fix=""):
    return {"id": id, "title": title, "status": status, "detail": detail, "fix": fix}


def desktop_name(env=None):
    env = os.environ if env is None else env
    from . import autostart
    c = autostart.compositor(env)
    if c != "other":
        return c
    desk = env.get("XDG_CURRENT_DESKTOP", "").lower()
    for name in ("gnome", "river", "wayfire", "labwc", "cosmic", "xfce", "lxqt"):
        if name in desk or name in env.get("XDG_SESSION_DESKTOP", "").lower():
            return name
    return desk or "unknown"


# ---- individual checks -------------------------------------------------------------
def c_wayland():
    if os.environ.get("WAYLAND_DISPLAY"):
        return check("wayland", "Wayland session", "ok", os.environ["WAYLAND_DISPLAY"])
    if os.environ.get("DISPLAY"):
        return check("wayland", "Wayland session", "fail", "this is an X11 session",
                     "log in to a Wayland session (sway, Hyprland, niri, KDE Plasma Wayland)")
    return check("wayland", "Wayland session", "warn", "WAYLAND_DISPLAY is not set",
                 "run this from inside your desktop session")


def c_compositor():
    d = desktop_name()
    if d == "gnome":
        return check("compositor", "Desktop", "fail",
                     "GNOME doesn't support the layer-shell protocol topopaper needs",
                     "use sway, Hyprland, niri, river, Wayfire or KDE Plasma")
    if d in SUPPORTED:
        extra = "" if d in ("sway", "hyprland", "niri", "kde") else \
            " (no covered-window detection: the wallpaper always animates)"
        return check("compositor", "Desktop", "ok", d + extra)
    return check("compositor", "Desktop", "warn", f"{d}: not tested",
                 "it works if the compositor supports wlr-layer-shell")


def c_layer_shell():
    if not shutil.which("wayland-info") or not os.environ.get("WAYLAND_DISPLAY"):
        return check("layer_shell", "Layer-shell protocol", "warn", "not checked",
                     "install wayland-utils (wayland-info) to check")
    try:
        out = subprocess.run(["wayland-info"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as e:
        return check("layer_shell", "Layer-shell protocol", "warn", f"wayland-info failed: {e}")
    if "zwlr_layer_shell_v1" in out:
        return check("layer_shell", "Layer-shell protocol", "ok", "zwlr_layer_shell_v1 offered")
    return check("layer_shell", "Layer-shell protocol", "fail",
                 "the compositor doesn't offer zwlr_layer_shell_v1",
                 "topopaper can't draw a wallpaper on this compositor")


def c_engine():
    from . import session
    e = session.find_engine()
    if e:
        return check("engine", "Wallpaper engine", "ok", e)
    return check("engine", "Wallpaper engine", "fail", "the `topopaper` binary was not found",
                 "run `make` (source checkout) or `make install`")


def _python():
    """The interpreter the launchers use: the venv if install.sh made one."""
    v = paths.venv_python()
    return str(v) if v.exists() else sys.executable


def _try_import(code):
    try:
        r = subprocess.run([_python(), "-c", code], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr.strip().splitlines() or [""])[-1]


def c_build_deps():
    ok, err = _try_import("import numpy, PIL")
    if ok:
        return check("build_deps", "Map builder (numpy, Pillow)", "ok", _python())
    return check("build_deps", "Map builder (numpy, Pillow)", "fail", err,
                 "install python numpy and pillow (e.g. pacman -S python-numpy python-pillow, "
                 "apt install python3-numpy python3-pil)")


def c_gui_deps():
    ok, err = _try_import("import gi; gi.require_version('Gtk', '4.0'); "
                          "gi.require_version('Adw', '1'); from gi.repository import Gtk, Adw")
    if ok:
        return check("gui_deps", "Settings window (GTK 4, libadwaita)", "ok")
    return check("gui_deps", "Settings window (GTK 4, libadwaita)", "warn", err,
                 "install PyGObject with GTK 4 and libadwaita (e.g. pacman -S python-gobject "
                 "libadwaita, apt install python3-gi gir1.2-adw-1); everything else works without")


def c_font():
    f = paths.font_file()
    if f.is_file():
        return check("font", "HUD font", "ok", str(f))
    return check("font", "HUD font", "fail", f"missing {f}", "reinstall topopaper")


def c_earth():
    if util.pack_exists("earth"):
        return check("earth", "Globe (earth pack)", "ok", str(paths.areas_dir() / "earth"))
    return check("earth", "Globe (earth pack)", "fail", "not installed",
                 "run `topopaper-ctl starter`")


def c_packs_writable():
    d = paths.areas_dir()
    probe = d if d.exists() else paths.data_dir()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        with tempfile.TemporaryFile(dir=probe):
            pass
        return check("packs_dir", "Maps folder writable", "ok", str(d))
    except OSError as e:
        return check("packs_dir", "Maps folder writable", "fail", f"{d}: {e.strerror}",
                     "fix the folder's permissions, or set TOPOPAPER_DATA")


def c_config():
    import configparser
    p = paths.config_file()
    if not p.exists():
        return check("config", "Settings file", "ok", f"{p} (defaults; not created yet)")
    try:
        configparser.ConfigParser(interpolation=None).read(p, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError) as e:
        return check("config", "Settings file", "fail", f"{p}: {e}",
                     "fix the file, or delete it to start from the defaults")
    return check("config", "Settings file", "ok", str(p))


def c_session():
    from . import session
    if session.is_running():
        return check("session", "Wallpaper running", "ok", f"session pid {session.read_pid()}")
    return check("session", "Wallpaper running", "warn", "no session",
                 "run `topopaper-session &` or `topopaper-ctl restart`")


def c_autostart():
    from . import autostart
    st = autostart.status()
    if st["enabled"]:
        return check("autostart", "Starts with the desktop", "ok", f"{st['method']}: {st['detail']}")
    return check("autostart", "Starts with the desktop", "warn", "not set up",
                 "run `topopaper-ctl autostart enable`")


def c_host(id, url, what):
    host = urllib.parse.urlsplit(url).hostname
    title = f"Network: {host}"
    try:
        socket.create_connection((host, 443), timeout=4).close()
        return check(f"net_{id}", title, "ok", what)
    except OSError as e:
        return check(f"net_{id}", title, "warn", f"unreachable ({e.strerror or e}); needed for {what}",
                     "check your connection or firewall")


def run_checks(offline=False):
    out = []
    for fn in (c_wayland, c_compositor, c_layer_shell, c_engine, c_build_deps, c_gui_deps,
               c_font, c_earth, c_packs_writable, c_config, c_session, c_autostart):
        try:
            out.append(fn())
        except Exception as e:          # a broken check must not hide the others
            out.append(check(fn.__name__[2:], fn.__name__[2:], "warn", f"check failed: {e!r}"))
    if not offline:
        out += [c_host(*h) for h in HOSTS]
    return out


def main(argv=None):
    argv = list(argv or [])
    if "-h" in argv or "--help" in argv:
        print("usage: topopaper-ctl doctor [--json] [--offline]")
        return 0
    res = run_checks(offline="--offline" in argv)
    if "--json" in argv:
        print(json.dumps(res, indent=1))
    else:
        for r in res:
            line = f"{MARK[r['status']]} {r['title']}"
            if r["detail"]:
                line += f": {r['detail']}"
            print(line)
            if r["fix"] and r["status"] != "ok":
                print(f"    -> {r['fix']}")
    return 1 if any(r["status"] == "fail" for r in res) else 0
