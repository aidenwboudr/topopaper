"""sway / SwayFX backend: `swaymsg -t subscribe` + get_tree / get_workspaces.

Also the optional power profile (config `power_profile = sway`): switch one
output's mode, and SwayFX's blur, with the power source. A mode set
reconfigures every layer surface and reads as a whole-desktop flicker, so a
confirmed change waits until windows (or swaylock) hide the wallpaper, with a
10-minute cap so the battery saving still lands eventually.
"""
import json
import re
import select
import subprocess
import time

from .. import config
from . import log, retry_forever

POLL_S = 12.0          # battery poll between events
BATT_SETTLE = 20.0     # charger state bounces at lid-open / outlet switching
PEND_MAX = 600.0


def sm(*args):
    try:
        return subprocess.check_output(["swaymsg", *args], timeout=5,
                                       stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return b""


def covered_in(tree, workspaces):
    """True when every visible workspace holds a tiled window, or a
    fullscreen one (floating windows alone don't count)."""
    vis = {w["name"] for w in workspaces if w.get("visible")}
    if not vis:
        return False
    seen = {}
    stack = [tree]
    while stack:
        n = stack.pop()
        if n.get("type") == "workspace":
            if n.get("name") in vis:
                seen[n["name"]] = bool(n.get("nodes")) or any(
                    f.get("fullscreen_mode") for f in _leaves(n.get("floating_nodes", [])))
            continue
        stack.extend(n.get("nodes", []))
    return len(seen) == len(vis) and all(seen.values())


def _leaves(nodes):
    stack = list(nodes)
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.get("nodes", []) + n.get("floating_nodes", []))


def covered_now():
    try:
        return covered_in(json.loads(sm("-t", "get_tree")),
                          json.loads(sm("-t", "get_workspaces")))
    except ValueError:
        return False


# ---- power profile -------------------------------------------------------------
def on_battery():
    # battery status flaps between Discharging / "Not charging" under USB-C PD
    # renegotiation and charge thresholds even while plugged in; the adapter's
    # online flag is the truth
    import glob
    for p in sorted(glob.glob("/sys/class/power_supply/*/type")):
        try:
            if open(p).read().strip() == "Mains":
                return open(p[:-4] + "online").read().strip() == "0"
        except OSError:
            continue
    for p in sorted(glob.glob("/sys/class/power_supply/BAT*/status")):
        try:
            return open(p).read().startswith("D")
        except OSError:
            continue
    return False


def parse_mode(s):
    """'2560x1600@120Hz' -> (2560, 1600, 120000 mHz); None if unparsable."""
    m = re.fullmatch(r"\s*(\d+)x(\d+)(?:@([\d.]+)\s*(?:Hz)?)?\s*", s or "")
    if not m:
        return None
    return int(m[1]), int(m[2]), int(float(m[3] or 0) * 1000)


def mode_matches(cur, want):
    """cur: sway's current_mode dict; want: parse_mode() tuple."""
    if not cur or not want:
        return False
    w, h, mhz = want
    return (cur.get("width") == w and cur.get("height") == h
            and (not mhz or abs(cur.get("refresh", 0) - mhz) < 1000))


class Power:
    def __init__(self):
        self.last = None        # adopted power state (True = battery)
        self.cand = None        # (value, first_seen) while debouncing
        self.pend = None        # (value, since): confirmed, waiting for cover

    @staticmethod
    def settings():
        cfg = config.load()
        if cfg.get("power_profile") != "sway" or not cfg.get("power_output"):
            return None
        ac, bat = parse_mode(cfg.get("power_mode_ac")), parse_mode(cfg.get("power_mode_battery"))
        return cfg.get("power_output"), cfg.get("power_mode_ac"), cfg.get("power_mode_battery"), ac, bat

    @staticmethod
    def current_mode(out):
        """sway's current_mode for `out`; None when it isn't connected."""
        try:
            for o in json.loads(sm("-t", "get_outputs")):
                if o.get("name") == out:
                    return o.get("current_mode") or {}
        except ValueError:
            return None
        return None

    def tick(self, covered):
        st = self.settings()
        if not st:
            self.last = self.cand = self.pend = None
            return
        out, mode_ac, mode_bat, ac, bat = st
        b = on_battery()
        now = time.time()
        if self.last is None:
            self.last = b       # adopt; reconcile if a previous run died mid-flip
            cur = self.current_mode(out)
            if cur is not None and not mode_matches(cur, bat if b else ac):
                self.pend = (b, now)
        elif b != self.last:
            if self.cand is None or self.cand[0] != b:
                self.cand = (b, now)
                return
            if now - self.cand[1] < BATT_SETTLE:
                return
            self.cand = None
            self.last = b
            self.pend = (b, now)
        else:
            self.cand = None
            if self.pend is not None and self.pend[0] != b:
                self.pend = None    # power reverted before we applied: no flip
        if self.pend is None:
            return
        if not (covered or locked()) and now - self.pend[1] < PEND_MAX:
            return                  # wallpaper visible: hold the flip
        b = self.pend[0]
        self.pend = None
        cur = self.current_mode(out)
        if cur is None:
            return                  # no such output here: leave modes alone
        want_s, want = (mode_bat, bat) if b else (mode_ac, ac)
        if want and not mode_matches(cur, want):
            log(f"power: {'battery' if b else 'AC'} -> output {out} mode {want_s}")
            sm("output", out, "mode", want_s)
        sm("blur", "disable" if b else "enable")     # SwayFX only; sway ignores it


def locked():
    return subprocess.run(["pgrep", "-x", "swaylock"], capture_output=True,
                          check=False).returncode == 0


def run(flag):
    power = Power()

    def apply():
        c = covered_now()
        flag.set(c)
        power.tick(c)

    def once():
        proc = subprocess.Popen(["swaymsg", "-t", "subscribe", "-m", '["window","workspace","output"]'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            apply()
            while True:
                r, _, _ = select.select([proc.stdout], [], [], POLL_S)
                if r:
                    if not proc.stdout.readline():
                        return              # sway IPC gone (reload, exit)
                apply()
        finally:
            proc.kill()
            proc.wait()

    retry_forever(once, "sway")
