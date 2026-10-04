"""topopaper-ctl: one entry point for everything outside the engine."""
import importlib
import json
import os
import shutil
import subprocess
import sys

from . import __version__, config, paths, util

# command -> (module, function, one-line help). Modules load lazily so a
# plain `fly` never imports numpy, and so optional parts can fail alone.
COMMANDS = {
    "fly":         ("topopaper.cli", "cmd_fly", "fly to a pack (no name: next pack)"),
    "search":      ("topopaper.search", "main", "pick or type a place (launcher menu)"),
    "status":      ("topopaper.cli", "cmd_status", "show paths, packs and what is running"),
    "config":      ("topopaper.cli", "cmd_config", "get/set settings: config [KEY [VALUE]]"),
    "settings":    ("topopaper.cli", "cmd_settings", "open the settings window"),
    "session":     ("topopaper.session", "main", "run the wallpaper + helpers (autostart this)"),
    "restart":     ("topopaper.session", "restart", "restart the running wallpaper"),
    "stop":        ("topopaper.session", "stop", "stop the running wallpaper session"),
    "weather":     ("topopaper.weather", "main", "fetch weather for the HUD once"),
    "watch":       ("topopaper.watch", "main", "run the covered-window watcher alone"),
    "autostart":   ("topopaper.autostart", "main", "autostart enable|disable|status"),
    "keybind":     ("topopaper.autostart", "keybind_main", "search keybinding add|remove|status"),
    "doctor":      ("topopaper.doctor", "main", "check the install and the desktop"),
    "starter":     ("topopaper.starter", "main", "download (or build) the starter globe"),
    "build":       ("topopaper.build.area", "run", "build one area pack (advanced)"),
    "route":       ("topopaper.build.route", "main", "build the rungs linking a pack to the globe"),
    "site":        ("topopaper.build.site", "main", "build ski-resort packs under a pack"),
    "hud":         ("topopaper.build.hud", "main", "re-bake hud.bin (developers)"),
    "lights":      ("topopaper.build.lights", "main", "rebuild city lights from OpenStreetMap"),
    "gc":          ("topopaper.gc", "main", "list/delete old auto-built packs"),
    "build-place": ("topopaper.cli", "cmd_build_place", None),   # internal worker
}


def cmd_fly(argv):
    if argv:
        name = argv[0]
        if not util.pack_exists(name):
            util.notify(f"no map named {name}", 2500)
            return 1
    else:
        names = [n for n, m in util.list_packs() if not m.get("scaffold")]
        if not names:
            util.notify("no maps installed yet", 2500)
            return 1
        cur = util.current_area()
        name = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
    util.fly(name)
    return 0


def engine_running():
    try:
        r = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", "topopaper"],
                           capture_output=True, check=False)
        return r.returncode == 0
    except OSError:
        return False


def cmd_status(argv):
    packs = util.list_packs()
    size = sum(util.pack_size(n) for n, _ in packs)
    info = {
        "version": __version__,
        "engine_running": engine_running(),
        "current_area": util.current_area(),
        "packs": len(packs),
        "packs_mb": round(size / 1e6, 1),
        "config": str(paths.config_file()),
        "data": str(paths.data_dir()),
        "state": str(paths.state_dir()),
        "share": str(paths.share_dir()),
        "cache": str(paths.cache_dir()),
    }
    if "--json" in argv:
        print(json.dumps(info, indent=1))
    else:
        for k, v in info.items():
            print(f"{k:15} {v}")
    return 0


def cmd_config(argv):
    cfg = config.load()
    if not argv:
        for k in config.SCHEMA:
            print(f"{k.name} = {cfg.values[k.name]}")
        return 0
    if argv[0] == "path":
        print(cfg.path)
        return 0
    key = argv[0]
    if key not in config.BY_NAME:
        print(f"unknown setting: {key}", file=sys.stderr)
        return 2
    if len(argv) == 1:
        print(cfg.values[key])
        return 0
    k = config.BY_NAME[key]
    val = " ".join(argv[1:])
    if k.kind == "choice" and val not in k.choices:
        print(f"{key} must be one of: {', '.join(k.choices)}", file=sys.stderr)
        return 2
    cfg.set(key, val)
    cfg.save()
    return 0


def open_settings(page=None):
    exe = shutil.which("topopaper-settings")
    cmd = [exe] if exe else [sys.executable, "-m", "topopaper.settings"]
    if page:
        cmd += ["--page", page]
    _, env = util.ctl_cmd()
    subprocess.Popen(cmd, env=env, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def cmd_settings(argv):
    return open_settings(argv[0] if argv else None)


def cmd_build_place(argv):
    from . import jobs
    return jobs.run(json.loads(argv[0]))


def usage():
    print(f"topopaper-ctl {__version__}\n\nusage: topopaper-ctl COMMAND [ARGS]\n")
    for name, (_, _, doc) in COMMANDS.items():
        if doc:
            print(f"  {name:12} {doc}")
    print("\nRun `topopaper-ctl COMMAND --help` for a command's options.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        usage()
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        print(f"topopaper-ctl: unknown command '{name}'\n", file=sys.stderr)
        usage()
        return 2
    mod, fn, _ = COMMANDS[name]
    rc = getattr(importlib.import_module(mod), fn)(rest)
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(main())
