"""`topopaper-ctl watch`: tells the engine when windows cover the wallpaper.

The engine polls the covered flag ($XDG_RUNTIME_DIR/topopaper-covered) and
freezes its animation at a few fps while it reads "1". "Covered" means every
visible workspace (one per output) shows at least one tiled or fullscreen
window; a floating window alone leaves the wallpaper visible around it.

One backend per compositor, picked from the environment:
    sway / SwayFX   SWAYSOCK                      watch/sway.py
    Hyprland        HYPRLAND_INSTANCE_SIGNATURE   watch/hyprland.py
    niri            NIRI_SOCKET                   watch/niri.py
    KDE Plasma      XDG_CURRENT_DESKTOP / KDE_FULL_SESSION   watch/kde.py
    anything else   write "0" once and idle (the wallpaper always animates)

Backends block on the compositor's event stream, recompute the state from a
fresh query on each event, and reconnect after a pause when the IPC drops.
"""
import fcntl
import importlib
import os
import signal
import sys
import time

from .. import paths, util

BACKENDS = ("sway", "hyprland", "niri", "kde", "fallback")
RETRY_S = 3.0


def detect(env=None):
    env = os.environ if env is None else env
    if env.get("SWAYSOCK"):
        return "sway"
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if env.get("NIRI_SOCKET"):
        return "niri"
    desk = env.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    if "kde" in desk or env.get("KDE_FULL_SESSION"):
        return "kde"
    return "fallback"


class Flag:
    """The covered flag; rewrites the file only when the value changes."""

    def __init__(self, path=None):
        self.path = str(path or paths.covered_file())
        self.value = None

    def set(self, covered):
        covered = bool(covered)
        if covered != self.value:
            util.write_atomic(self.path, "1" if covered else "0")
            self.value = covered
            log(f"covered={int(covered)}")


def log(msg):
    print(f"topopaper-watch: {msg}", file=sys.stderr, flush=True)


def retry_forever(run_once, what):
    """Call run_once() again whenever it returns or raises, never faster than
    every RETRY_S: the compositor may be reloading or briefly gone."""
    while True:
        try:
            run_once()
            log(f"{what} event stream ended; reconnecting")
        except Exception as e:          # keep watching whatever happens
            log(f"{what}: {e!r}; retrying")
        time.sleep(RETRY_S)


def run_fallback(flag):
    flag.set(False)
    log("no covered-window support for this desktop; the wallpaper always animates")
    while True:
        signal.pause()


def _lock():
    f = open(paths.runtime_dir() / "topopaper-watch.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main(argv=None):
    argv = list(argv or [])
    if "-h" in argv or "--help" in argv:
        print("usage: topopaper-ctl watch [--backend NAME] [--once]\n\n"
              f"backends: {', '.join(BACKENDS)} (default: detected, now "
              f"'{detect()}')\n--once prints the current state and exits")
        return 0
    name = detect()
    if "--backend" in argv:
        i = argv.index("--backend")
        name = argv[i + 1] if i + 1 < len(argv) else ""
        if name not in BACKENDS:
            print(f"unknown backend '{name}'", file=sys.stderr)
            return 2
    flag = Flag()
    if name == "fallback":
        if "--once" in argv:
            print("0")
            return 0
        lock = _lock()
        if lock is None:
            return 0
        run_fallback(flag)
    mod = importlib.import_module(f"{__name__}.{name}")
    if "--once" in argv:
        c = mod.covered_now()
        print("1" if c else "0")
        return 0
    lock = _lock()
    if lock is None:
        log("another watcher is already running")
        return 0
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log(f"backend: {name}")
    mod.run(flag)
    return 0
