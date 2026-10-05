"""`topopaper-ctl autostart` / `keybind`: start with the desktop, and a key
for the place search.

Every compositor wants this somewhere else:
    KDE Plasma   ~/.config/autostart/topopaper.desktop (XDG autostart)
    sway         ~/.config/sway/config.d/topopaper.conf when the user's config
                 includes that directory, else a marked block in the config
    Hyprland     marked block (exec-once) in ~/.config/hypr/hyprland.conf
    niri         marked block (spawn-at-startup) in ~/.config/niri/config.kdl
    others       a systemd user unit bound to graphical-session.target

A "marked block" sits between `# >>> topopaper >>>` and `# <<< topopaper <<<`
(`//` in niri's KDL); a timestamped backup of the file is made before any
change. Every operation is idempotent and `disable`/`remove` take back
exactly what `enable`/`add` wrote.
"""
import fnmatch
import glob
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths

DEFAULT_KEYS = "Super+Shift+B"
DESKTOP_NAME = "topopaper.desktop"
UNIT_NAME = "topopaper.service"


# ---- where things are -----------------------------------------------------------
def config_home():
    return paths._xdg("XDG_CONFIG_HOME", ".config")


def compositor(env=None):
    """sway | hyprland | niri | kde | other. TOPOPAPER_COMPOSITOR overrides."""
    env = os.environ if env is None else env
    forced = env.get("TOPOPAPER_COMPOSITOR")
    if forced:
        return forced
    desk = env.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    if env.get("SWAYSOCK") or "sway" in desk:
        return "sway"
    if env.get("HYPRLAND_INSTANCE_SIGNATURE") or "hyprland" in desk:
        return "hyprland"
    if env.get("NIRI_SOCKET") or "niri" in desk:
        return "niri"
    if "kde" in desk or env.get("KDE_FULL_SESSION"):
        return "kde"
    return "other"


def launcher(name):
    """Absolute path of an installed launcher (generated lines must not
    depend on the compositor's PATH), else beside this one, else the name."""
    p = shutil.which(name)
    if p:
        return p
    b = os.environ.get("TOPOPAPER_BIN")
    if b and os.access(os.path.join(b, name), os.X_OK):
        return os.path.join(b, name)
    return name


def sway_config():
    return config_home() / "sway" / "config"


def sway_dropin(name="topopaper.conf"):
    return config_home() / "sway" / "config.d" / name


def hypr_config():
    return config_home() / "hypr" / "hyprland.conf"


def niri_config():
    return config_home() / "niri" / "config.kdl"


def desktop_file():
    return config_home() / "autostart" / DESKTOP_NAME


def unit_file():
    return config_home() / "systemd" / "user" / UNIT_NAME


# ---- marked blocks ------------------------------------------------------------------
def markers(tag, comment):
    return f"{comment} >>> {tag} >>>", f"{comment} <<< {tag} <<<"


def strip_block(text, tag, comment="#"):
    """text without the marked block (and the blank line we put before it)."""
    begin, end = markers(tag, comment)
    out, skip = [], False
    for ln in text.splitlines(keepends=True):
        s = ln.strip()
        if s == begin:
            skip = True
            if out and not out[-1].strip():
                out.pop()
            continue
        if skip:
            if s == end:
                skip = False
            continue
        out.append(ln)
    return "".join(out)


def has_block(text, tag, comment="#"):
    return markers(tag, comment)[0] in [ln.strip() for ln in text.splitlines()]


def make_block(lines, tag, comment="#", indent=""):
    begin, end = markers(tag, comment)
    note = f"{comment} added by topopaper; `topopaper-ctl autostart disable` / `keybind remove` removes it"
    return "".join(f"{indent}{ln}\n" for ln in [begin, note, *lines, end])


def with_block(text, lines, tag, comment="#"):
    """text with exactly one copy of the block, at the end."""
    base = strip_block(text, tag, comment)
    if base and not base.endswith("\n"):
        base += "\n"
    return base + ("\n" if base else "") + make_block(lines, tag, comment)


def backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = path.with_name(f"{path.name}.topopaper-bak-{stamp}")
    n = 1
    while dst.exists():                 # two edits in one second: keep both
        dst = path.with_name(f"{path.name}.topopaper-bak-{stamp}-{n}")
        n += 1
    shutil.copy2(path, dst)
    return dst


def rewrite(path, new):
    """Write `new` if it differs, after a backup. True when changed."""
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == new:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if old is not None:
        backup(path)
    tmp = path.with_name(path.name + ".topopaper-tmp")
    tmp.write_text(new, encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return True


def read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---- sway --------------------------------------------------------------------------
def _shell_default(m):
    # ${NAME:-fallback}: the form distro default configs use for config.d
    name, fallback = m[1], m[2]
    if name == "XDG_CONFIG_HOME":
        return str(config_home())
    return os.environ.get(name) or fallback


def _expand(pattern, base):
    home = str(Path.home())
    p = pattern.strip().strip('"').strip("'")
    p = p.replace("$HOME", home).replace("${HOME}", home)
    p = re.sub(r"\$\{(\w+):-([^}]*)\}", _shell_default, p)
    p = p.replace("$XDG_CONFIG_HOME", str(config_home())).replace("${XDG_CONFIG_HOME}", str(config_home()))
    p = p.replace("$HOME", home).replace("${HOME}", home)
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.join(base, p)


def sway_includes(path, text=None):
    """Expanded glob patterns of the config's `include` lines."""
    text = read(path) if text is None else text
    out = []
    for ln in text.splitlines():
        m = re.match(r"\s*include\s+(.+?)\s*$", ln)
        if m:
            out.append(_expand(m[1], str(path.parent)))
    return out


def sway_dropin_ok(name="topopaper.conf"):
    """Does the user's own sway config include ~/.config/sway/config.d/<name>?
    (The system config's `include /etc/sway/config.d/*` doesn't count.)"""
    target = str(sway_dropin(name))
    return any(fnmatch.fnmatch(target, pat) for pat in sway_includes(sway_config()))


def ensure_sway_config():
    """sway falls back to /etc/sway/config when the user has none: start the
    user's copy from it so adding a line doesn't throw away the defaults."""
    cfg = sway_config()
    if cfg.exists():
        return None
    for src in ("/etc/sway/config", "/usr/local/etc/sway/config"):
        if os.path.isfile(src):
            cfg.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, cfg)
            return src
    return None


def _sway_add(lines, tag, dropin):
    copied = ensure_sway_config()
    note = f"; started from a copy of {copied}" if copied else ""
    if sway_dropin_ok(dropin):
        f = sway_dropin(dropin)
        changed = rewrite(f, make_block(lines, tag))
        if has_block(read(sway_config()), tag):
            # a block left from before config.d was included would run it twice
            changed |= rewrite(sway_config(), strip_block(read(sway_config()), tag))
        return changed, f"{f}{note}"
    return rewrite(sway_config(), with_block(read(sway_config()), lines, tag)), \
        f"{sway_config()}{note}"


def _sway_remove(tag, dropin):
    changed = False
    f = sway_dropin(dropin)
    if f.exists() and has_block(read(f), tag):
        f.unlink()
        changed = True
    if has_block(read(sway_config()), tag):
        changed |= rewrite(sway_config(), strip_block(read(sway_config()), tag))
    return changed


def _sway_has(tag, dropin):
    f = sway_dropin(dropin)
    if f.exists() and has_block(read(f), tag) and sway_dropin_ok(dropin):
        return str(f)
    if has_block(read(sway_config()), tag):
        return str(sway_config())
    return ""


def reload_compositor(comp):
    """Ask a running sway/Hyprland to re-read its config (niri watches its
    own file; KDE and systemd need nothing)."""
    cmd = None
    if comp == "sway" and os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
        cmd = ["swaymsg", "reload"]
    elif comp == "hyprland" and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
        cmd = ["hyprctl", "reload"]
    if cmd:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=False)


# ---- niri (KDL: `//` comments, binds live inside one `binds { }`) ----------------------
def niri_with_binds(text, lines, tag):
    base = strip_block(text, tag, "//")
    m = re.search(r"^binds\s*\{[^\n]*\n", base, re.M)
    block = make_block(lines, tag, "//", indent="    ")
    if m:
        return base[:m.end()] + block + base[m.end():]
    if base and not base.endswith("\n"):
        base += "\n"
    return base + "\n" + make_block(["binds {", *("    " + ln for ln in lines), "}"], tag, "//")


# ---- autostart --------------------------------------------------------------------------
def _desktop_entry(exe):
    return ("[Desktop Entry]\nType=Application\nName=topopaper\n"
            "Comment=Live topographic wallpaper\n"
            f"Exec={exe}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n"
            "X-KDE-autostart-phase=2\n")


def _unit(exe):
    return ("[Unit]\nDescription=topopaper live wallpaper\n"
            "PartOf=graphical-session.target\nAfter=graphical-session.target\n"
            "Requisite=graphical-session.target\n\n"
            f"[Service]\nExecStart={exe}\nRestart=on-failure\nRestartSec=5\n\n"
            "[Install]\nWantedBy=graphical-session.target\n")


def _systemctl(*args):
    if not shutil.which("systemctl"):
        return False, "systemctl not found"
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                       timeout=20, check=False)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def _systemd_enabled():
    if not unit_file().exists():
        return False
    ok, _ = _systemctl("is-enabled", "--quiet", UNIT_NAME)
    # without a user manager (containers, odd setups) the file is the best signal
    return ok or not shutil.which("systemctl") or \
        (unit_file().parent / "graphical-session.target.wants" / UNIT_NAME).exists()


_START_LINE = re.compile(r"^\s*(exec(_always)?|exec-once|spawn-at-startup|Exec=)\b.*topopaper-session")


def _config_files(comp):
    """Config files a compositor reads at startup (main file + includes)."""
    if comp == "sway":
        # SwayFX setups often start from ~/.config/swayfx/config, which then
        # includes the sway config; follow includes a few levels deep
        files, todo = [], [sway_config(), config_home() / "swayfx" / "config"]
        while todo and len(files) < 200:
            f = todo.pop(0)
            if f in files or not f.is_file():
                continue
            files.append(f)
            for pat in sway_includes(f):
                if pat.startswith(str(Path.home())):
                    todo += [Path(g) for g in sorted(glob.glob(pat))]
        return files
    if comp == "hyprland":
        files = [hypr_config()]
        for ln in read(hypr_config()).splitlines():
            m = re.match(r"\s*source\s*=\s*(.+?)\s*$", ln)
            if m:
                files += [Path(f) for f in sorted(glob.glob(_expand(m[1], str(hypr_config().parent))))]
        return files
    if comp == "niri":
        return [niri_config()]
    if comp == "kde":
        return sorted(desktop_file().parent.glob("*.desktop"))
    return []


def own_autostart(comp=None):
    """A start line the user wrote themselves (README's manual setup, or a
    dotfiles repo): the path holding it, or ''."""
    for f in _config_files(comp or compositor()):
        if any(_START_LINE.match(ln) for ln in read(f).splitlines()):
            return str(f)
    return ""


def status():
    """{'enabled': bool, 'method': str, 'detail': str} for this desktop."""
    st = _managed_status()
    if not st["enabled"]:
        own = own_autostart()
        if own:
            return {"enabled": True, "method": "own-config",
                    "detail": f"started by your own line in {own}", "managed": False}
    return st


def _managed_status():
    comp = compositor()
    tag = "topopaper"
    if comp == "kde":
        on = desktop_file().exists()
        return {"enabled": on, "method": "xdg-autostart",
                "detail": str(desktop_file()) if on else "not set up"}
    if comp == "sway":
        where = _sway_has(tag, "topopaper.conf")
        return {"enabled": bool(where), "method": "sway-config", "detail": where or "not set up"}
    if comp == "hyprland":
        on = has_block(read(hypr_config()), tag)
        return {"enabled": on, "method": "hyprland-config",
                "detail": str(hypr_config()) if on else "not set up"}
    if comp == "niri":
        on = has_block(read(niri_config()), tag, "//")
        return {"enabled": on, "method": "niri-config",
                "detail": str(niri_config()) if on else "not set up"}
    on = _systemd_enabled()
    return {"enabled": on, "method": "systemd",
            "detail": str(unit_file()) if on else "not set up"}


def enable():
    """Start topopaper-session with the desktop. Returns (ok, message)."""
    comp = compositor()
    exe = launcher("topopaper-session")
    tag = "topopaper"
    try:
        if comp == "kde":
            rewrite(desktop_file(), _desktop_entry(exe))
            return True, f"added {desktop_file()}"
        if comp == "sway":
            _, where = _sway_add([f"exec {exe}"], tag, "topopaper.conf")
            reload_compositor(comp)
            return True, f"sway will start topopaper at login ({where})"
        if comp == "hyprland":
            rewrite(hypr_config(), with_block(read(hypr_config()), [f"exec-once = {exe}"], tag))
            reload_compositor(comp)
            return True, f"Hyprland will start topopaper at login ({hypr_config()})"
        if comp == "niri":
            rewrite(niri_config(), with_block(read(niri_config()),
                                              [f'spawn-at-startup "{exe}"'], tag, "//"))
            return True, f"niri will start topopaper at login ({niri_config()})"
        rewrite(unit_file(), _unit(exe))
        _systemctl("daemon-reload")
        ok, err = _systemctl("enable", UNIT_NAME)
        if not ok:
            return False, (f"wrote {unit_file()} but `systemctl --user enable` failed: {err}. "
                           "Add `topopaper-session` to your compositor's autostart instead.")
        return True, (f"installed the {UNIT_NAME} user unit. It starts with "
                      "graphical-session.target, which your compositor must start "
                      "(e.g. via uwsm, or `systemctl --user start graphical-session.target` "
                      "from its startup commands).")
    except OSError as e:
        return False, f"could not set up autostart: {e}"


def disable():
    """Undo enable() for every method (a desktop switch leaves no strays)."""
    tag = "topopaper"
    done = []
    try:
        if desktop_file().exists():
            desktop_file().unlink()
            done.append(str(desktop_file()))
        if _sway_remove(tag, "topopaper.conf"):
            done.append("sway config")
            reload_compositor("sway")
        if has_block(read(hypr_config()), tag):
            rewrite(hypr_config(), strip_block(read(hypr_config()), tag))
            done.append(str(hypr_config()))
            reload_compositor("hyprland")
        if has_block(read(niri_config()), tag, "//"):
            rewrite(niri_config(), strip_block(read(niri_config()), tag, "//"))
            done.append(str(niri_config()))
        if unit_file().exists():
            _systemctl("disable", UNIT_NAME)
            unit_file().unlink()
            _systemctl("daemon-reload")
            done.append(str(unit_file()))
    except OSError as e:
        return False, f"could not remove autostart: {e}"
    own = own_autostart()
    if own:
        return False, (f"topopaper is also started by your own line in {own} — "
                       "remove that line to stop it starting at login")
    return True, ("removed autostart from " + ", ".join(done)) if done else "autostart was not set up"


# ---- keybinding --------------------------------------------------------------------------
MODS = {
    "super": ("Mod4", "SUPER", "Mod"), "mod4": ("Mod4", "SUPER", "Mod"),
    "win": ("Mod4", "SUPER", "Mod"), "meta": ("Mod4", "SUPER", "Mod"),
    "mod": ("Mod4", "SUPER", "Mod"), "logo": ("Mod4", "SUPER", "Mod"),
    "shift": ("Shift", "SHIFT", "Shift"),
    "ctrl": ("Ctrl", "CTRL", "Ctrl"), "control": ("Ctrl", "CTRL", "Ctrl"),
    "alt": ("Mod1", "ALT", "Alt"), "mod1": ("Mod1", "ALT", "Alt"),
}


def parse_keys(keys):
    """'Super+Shift+B' -> (['super', 'shift'], 'B'). ValueError if odd."""
    parts = [p.strip() for p in re.split(r"[+ ]+", keys or "") if p.strip()]
    if not parts:
        raise ValueError("no key given")
    *mods, key = parts
    bad = [m for m in mods if m.lower() not in MODS]
    if bad:
        raise ValueError(f"unknown modifier {bad[0]!r} (use Super, Shift, Ctrl, Alt)")
    return [m.lower() for m in mods], key


def _keyname(key, upper):
    return (key.upper() if upper else key.lower()) if len(key) == 1 else key


def sway_bind(keys, cmd):
    mods, key = parse_keys(keys)
    return f"bindsym {'+'.join([MODS[m][0] for m in mods] + [_keyname(key, False)])} exec {cmd}"


def hypr_bind(keys, cmd):
    mods, key = parse_keys(keys)
    return f"bind = {' '.join(MODS[m][1] for m in mods)}, {_keyname(key, True)}, exec, {cmd}"


def niri_bind(keys, cmd_argv):
    mods, key = parse_keys(keys)
    args = " ".join(f'"{a}"' for a in cmd_argv)
    return f"{'+'.join([MODS[m][2] for m in mods] + [_keyname(key, True)])} {{ spawn {args}; }}"


_MOD_NORM = {"mod4": "super", "super": "super", "logo": "super", "win": "super",
             "meta": "super", "mod": "super", "mod1": "alt", "alt": "alt",
             "ctrl": "ctrl", "control": "ctrl", "shift": "shift"}


def _norm_combo(tokens):
    """['Mod4', 'Shift', 'b'] -> 'b+shift+super' (order-free, case-free)."""
    *mods, key = [t.strip().lower() for t in tokens if t.strip()]
    return "+".join([key] + sorted(_MOD_NORM.get(m, m) for m in mods))


def existing_binds(comp=None):
    """[(combo, file, line)] for every key binding in the user's config —
    not topopaper's own marked block — with variables resolved
    (`set $mod Mod4`, `$mainMod = SUPER`)."""
    comp = comp or compositor()
    out = []
    if comp not in ("sway", "hyprland"):
        return out
    files = _config_files(comp)
    var = {}
    for f in files:                          # variables first, from every file
        for ln in read(f).splitlines():
            m = (re.match(r"\s*set\s+(\$\w+)\s+(\S+)", ln) if comp == "sway"
                 else re.match(r"\s*(\$\w+)\s*=\s*(.+?)\s*$", ln))
            if m:
                var[m[1]] = m[2]

    def subst(t):
        for k in sorted(var, key=len, reverse=True):
            t = t.replace(k, var[k])
        return t
    for f in files:
        for ln in strip_block(read(f), "topopaper keybind").splitlines():
            st = ln.strip()
            if comp == "sway":
                m = re.match(r"bindsym\s+((?:--\S+\s+)*)(\S+)", st)
                if m:
                    out.append((_norm_combo(subst(m[2]).split("+")), str(f), st))
            else:
                m = re.match(r"bind\w*\s*=\s*([^,]*),\s*([^,]+),", st)
                if m:
                    mods = subst(m[1]).replace("_", " ").split()
                    out.append((_norm_combo(mods + [m[2]]), str(f), st))
    return out


def _own_search_bind(comp):
    """A binding the user wrote that already runs the search: (file, line) or None."""
    for _, f, ln in existing_binds(comp):
        if "topopaper-ctl search" in ln:
            return f, ln
    return None


KDE_HOWTO = ("Plasma keeps custom shortcuts in its own settings: open System Settings > "
             "Keyboard > Shortcuts > Add New > Command or Script, enter "
             "`{cmd} search`, and give it {keys}.")


def keybind_status():
    comp = compositor()
    tag = "topopaper keybind"
    if comp == "sway":
        where = _sway_has(tag, "topopaper-keys.conf")
    elif comp == "hyprland":
        where = str(hypr_config()) if has_block(read(hypr_config()), tag) else ""
    elif comp == "niri":
        where = str(niri_config()) if has_block(read(niri_config()), tag, "//") else ""
    else:
        return {"enabled": False, "supported": False, "detail":
                KDE_HOWTO.format(cmd=launcher("topopaper-ctl"), keys=DEFAULT_KEYS)
                if comp == "kde" else "set a shortcut for `topopaper-ctl search` in your desktop's settings"}
    keys = ""
    for ln in read(Path(where)).splitlines() if where else []:
        if "search" in ln and not ln.strip().startswith(("#", "//")):
            keys = ln.strip()
    if not where:
        own = _own_search_bind(comp)
        if own:
            return {"enabled": True, "supported": True, "managed": False,
                    "detail": f"set by your own line in {own[0]}", "line": own[1]}
    return {"enabled": bool(where), "supported": True, "detail": where or "not set up",
            "line": keys}


def keybind_add(keys=DEFAULT_KEYS):
    comp = compositor()
    ctl = launcher("topopaper-ctl")
    tag = "topopaper keybind"
    try:
        mods, key = parse_keys(keys)
        if comp in ("sway", "hyprland"):
            want = _norm_combo(mods + [key])
            for combo, f, ln in existing_binds(comp):
                if combo != want:
                    continue
                if "topopaper-ctl search" in ln:
                    return True, f"{keys} already opens the place search (your own line in {f})"
                # a second binding for the same keys is a config error in
                # sway (the red bar) and fires both commands in Hyprland
                return False, (f"{keys} is already bound in {f}:\n  {ln}\n"
                               "Remove that binding or pick other keys.")
        if comp == "sway":
            _, where = _sway_add([sway_bind(keys, f"{ctl} search")], tag, "topopaper-keys.conf")
            reload_compositor(comp)
            return True, f"{keys} opens the place search ({where})"
        if comp == "hyprland":
            rewrite(hypr_config(), with_block(read(hypr_config()),
                                              [hypr_bind(keys, f"{ctl} search")], tag))
            reload_compositor(comp)
            return True, f"{keys} opens the place search ({hypr_config()})"
        if comp == "niri":
            line = niri_bind(keys, [ctl, "search"])
            combo = line.split(" ", 1)[0]
            other = strip_block(read(niri_config()), tag, "//")
            if re.search(rf"^\s*{re.escape(combo)}\s", other, re.M | re.I):
                # niri rejects the whole config over a duplicate binding
                return False, f"{combo} is already bound in {niri_config()}; pick other keys"
            rewrite(niri_config(), niri_with_binds(read(niri_config()), [line], tag))
            return True, f"{keys} opens the place search ({niri_config()})"
        if comp == "kde":
            return False, KDE_HOWTO.format(cmd=ctl, keys=keys)
        return False, f"add a shortcut running `{ctl} search` in your desktop's keyboard settings"
    except ValueError as e:
        return False, str(e)
    except OSError as e:
        return False, f"could not add the keybinding: {e}"


def keybind_remove():
    tag = "topopaper keybind"
    done = []
    try:
        if _sway_remove(tag, "topopaper-keys.conf"):
            done.append("sway config")
            reload_compositor("sway")
        if has_block(read(hypr_config()), tag):
            rewrite(hypr_config(), strip_block(read(hypr_config()), tag))
            done.append(str(hypr_config()))
            reload_compositor("hyprland")
        if has_block(read(niri_config()), tag, "//"):
            rewrite(niri_config(), strip_block(read(niri_config()), tag, "//"))
            done.append(str(niri_config()))
    except OSError as e:
        return False, f"could not remove the keybinding: {e}"
    own = _own_search_bind(compositor())
    if own:
        return False, (f"the shortcut is set by your own line in {own[0]} — "
                       "remove that line to turn it off")
    return True, ("removed the keybinding from " + ", ".join(done)) if done else "no keybinding was set"


# ---- CLI --------------------------------------------------------------------------------
def _print_status(st):
    print(f"{'enabled' if st['enabled'] else 'disabled'} ({st.get('method', compositor())}): "
          f"{st['detail']}")


def main(argv=None):
    argv = list(argv or ["status"])
    cmd = argv[0]
    if cmd == "status":
        _print_status(status())
        return 0
    if cmd in ("enable", "disable"):
        ok, msg = enable() if cmd == "enable" else disable()
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    print("usage: topopaper-ctl autostart enable|disable|status\n\n"
          f"desktop: {compositor()} (set TOPOPAPER_COMPOSITOR to override)")
    return 0 if cmd in ("-h", "--help") else 2


def keybind_main(argv=None):
    argv = list(argv or ["status"])
    cmd = argv[0]
    if cmd == "status":
        st = keybind_status()
        print(f"{'set' if st['enabled'] else 'not set'}: {st['detail']}"
              + (f"\n  {st['line']}" if st.get("line") else ""))
        return 0
    if cmd in ("add", "remove"):
        ok, msg = keybind_add(" ".join(argv[1:]) or DEFAULT_KEYS) if cmd == "add" else keybind_remove()
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    print(f"usage: topopaper-ctl keybind add [KEYS]|remove|status\n\nKEYS default: {DEFAULT_KEYS}")
    return 0 if cmd in ("-h", "--help") else 2

