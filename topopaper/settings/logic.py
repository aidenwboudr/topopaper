"""Plain-Python logic behind the settings window (no Gtk, so tests can import it)."""
import json
import math
import os
import shutil
import time
from dataclasses import dataclass, field

from .. import paths, util
from ..geocode import TIER_NAMES

# tiers above the geocoder's range (it never builds K0/K1 packs)
SCALE_NAMES = {0: "globe", 1: "hemisphere", **TIER_NAMES}

ROAM_CHOICES = (0, 2, 5, 8, 15, 30, 60)

LAUNCHERS = ("auto", "rofi", "fuzzel", "wofi", "tofi", "window", "settings")
LAUNCHER_LABELS = {"auto": "Automatic", "settings": "This settings window"}


# ---- packs ------------------------------------------------------------------
def tier_of(meta):
    """Tier K of a pack: its height is 4^-K of the world (ny tiles of 2^zoom)."""
    try:
        z, ny = int(meta["zoom"]), float(meta["ny"])
    except (KeyError, TypeError, ValueError):
        return None
    if ny <= 0:
        return None
    return max(0, round(-math.log(ny / 2.0 ** z, 4)))


def scale_label(tier):
    """'Regional map', 'Ski resort map', 'Whole globe'."""
    if tier is None:
        return "Map"
    if tier == 0:
        return "Whole globe"
    name = SCALE_NAMES.get(tier, f"tier {tier}")
    return f"{name[0].upper()}{name[1:]} map"


def fmt_size(n):
    """Decimal units, like GNOME Files: 980 kB, 12.3 MB, 1.2 GB."""
    n = max(0, int(n or 0))
    if n < 1000:
        return f"{n} bytes" if n != 1 else "1 byte"
    for unit in ("kB", "MB", "GB", "TB"):
        n /= 1000.0
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if n >= 100 else f"{n:.1f} {unit}"
    return ""


def fmt_age(days):
    days = max(0, int(days))
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def pack_title(name, meta=None):
    """Human name for a pack: its stored display name, else the slug in words."""
    meta = meta or {}
    for k in ("display", "title"):
        if isinstance(meta.get(k), str) and meta[k].strip():
            return meta[k].strip()
    if name == "earth":
        return "Earth"
    words = [w for w in name.replace("_", "-").split("-") if w]
    return " ".join(w if any(c.isdigit() for c in w) else w.capitalize() for w in words) or name


@dataclass
class Pack:
    name: str
    title: str
    tier: object
    size: int
    scaffold: bool = False
    auto: bool = False
    is_home: bool = False
    is_current: bool = False
    preview: str = ""
    mtime: float = 0.0
    children: list = field(default_factory=list)    # its scaffold rungs

    @property
    def scale(self):
        return scale_label(self.tier)

    @property
    def deletable(self):
        return self.name != "earth"


def scaffold_children(name, metas):
    """Connector rungs built for `name`: scaffold packs named `<name>-...`."""
    return sorted(n for n, m in metas.items()
                  if n != name and n.startswith(name + "-") and (m or {}).get("scaffold"))


def load_packs(home="", current=""):
    """Every installed pack as a Pack, sorted globe-first then by name."""
    metas = dict(util.list_packs())
    out = []
    root = paths.areas_dir()
    for name, m in metas.items():
        pv = root / name / "preview.png"
        try:
            mtime = (root / name / "terrain.bin").stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append(Pack(name=name, title=pack_title(name, m), tier=tier_of(m),
                        size=util.pack_size(name), scaffold=bool(m.get("scaffold")),
                        auto=bool(m.get("auto")), is_home=(name == home),
                        is_current=(name == current),
                        preview=str(pv) if pv.is_file() else "", mtime=mtime,
                        children=scaffold_children(name, metas)))
    out.sort(key=lambda p: (p.name != "earth", p.title.lower()))
    return out


def packs_fingerprint():
    """Cheap change detector for the pack list (no meta.json parsing)."""
    root = paths.areas_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return ()
    fp = []
    for n in names:
        try:
            fp.append((n, (root / n / "terrain.bin").stat().st_mtime))
        except OSError:
            pass
    return tuple(fp)


def delete_packs(names):
    """Remove packs by name; never `earth`, never anything outside areas/."""
    root = paths.areas_dir().resolve()
    removed = []
    for n in names:
        if not n or n == "earth" or "/" in n or n in (".", ".."):
            continue
        d = (root / n).resolve()
        if d.parent != root or not d.is_dir():
            continue
        shutil.rmtree(d)
        removed.append(n)
    return removed


# ---- displays ---------------------------------------------------------------
def parse_outputs(s):
    """config `outputs` -> None (all displays) or a list of connector names."""
    s = (s or "").strip()
    if not s or s.lower() == "all":
        return None
    out = []
    for x in s.split(","):
        x = x.strip()
        if x and x not in out:
            out.append(x)
    return out or None


def format_outputs(names):
    """Inverse of parse_outputs; an empty selection means all displays."""
    if not names:
        return "all"
    out = []
    for n in names:
        n = n.strip()
        if n and n not in out:
            out.append(n)
    return ",".join(out) if out else "all"


def toggle_output(value, name, on, connected=()):
    """New `outputs` value after ticking/unticking one display. Turning one
    off from 'all' starts from every connected display."""
    cur = parse_outputs(value)
    if cur is None:
        cur = list(connected)
    if on and name not in cur:
        cur.append(name)
    elif not on:
        cur = [n for n in cur if n != name]
    return format_outputs(cur)


def monitor_subtitle(manufacturer="", model="", width_mm=0, height_mm=0,
                     width_px=0, height_px=0, description=""):
    """'Dell U2720Q · 27″ · 3840×2160'."""
    parts = []
    name = description or " ".join(x for x in (manufacturer, model) if x and x.strip())
    if name:
        parts.append(name.strip())
    if width_mm and height_mm and width_mm > 0 and height_mm > 0:
        inch = math.hypot(width_mm, height_mm) / 25.4
        if 5 <= inch <= 200:
            parts.append(f"{inch:.0f}″")
    if width_px and height_px:
        parts.append(f"{width_px}×{height_px}")
    return " · ".join(parts)


# ---- motion -----------------------------------------------------------------
def roam_label(minutes):
    m = float(minutes or 0)
    if m <= 0:
        return "Off"
    if m >= 60 and m % 60 == 0:
        h = int(m // 60)
        return "Every hour" if h == 1 else f"Every {h} hours"
    return f"Every {m:g} minutes"


def roam_options(current):
    """(values, labels) for the roam combo; a hand-edited value gets its own entry."""
    vals = list(ROAM_CHOICES)
    try:
        c = float(current)
    except (TypeError, ValueError):
        c = None
    if c is not None and c >= 0 and not any(abs(c - v) < 1e-6 for v in vals):
        vals.append(c)
        vals.sort()
    return vals, [roam_label(v) for v in vals]


def roam_index(vals, current):
    try:
        c = float(current)
    except (TypeError, ValueError):
        return 0
    for i, v in enumerate(vals):
        if abs(v - c) < 1e-6:
            return i
    return 0


def speed_label(v):
    return f"{float(v):.2g}×"


def flight_label(v):
    return f"{float(v):.0f} s"


# ---- clock / weather --------------------------------------------------------
def fmt_location(name, lat, lon):
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return name or ""
    ns = "N" if la >= 0 else "S"
    ew = "E" if lo >= 0 else "W"
    coords = f"{abs(la):.2f}° {ns}, {abs(lo):.2f}° {ew}"
    return f"{name} ({coords})" if name else coords


def read_weather(path=None, now=None):
    """Summary of the HUD's weather.txt: dict(temp, cond, age_min) or None."""
    p = path or paths.default_weather_file()
    try:
        with open(p, encoding="utf-8") as f:
            kv = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
    except OSError:
        return None
    try:
        ts = float(kv.get("ts", "0"))
    except ValueError:
        ts = 0.0
    now = time.time() if now is None else now
    return dict(temp=kv.get("temp", ""), cond=kv.get("cond", "").strip(),
                age_min=max(0, int((now - ts) / 60)) if ts else None)


def weather_summary(w, units="imperial"):
    if not w:
        return "No weather fetched yet"
    deg = "°F" if units == "imperial" else "°C"
    bits = []
    if w.get("temp"):
        bits.append(f"{w['temp']}{deg}")
    if w.get("cond"):
        bits.append(w["cond"].capitalize())
    age = w.get("age_min")
    if age is not None:
        if age < 1:
            bits.append("updated just now")
        elif age < 120:
            bits.append(f"updated {age} min ago")
        else:
            bits.append(f"updated {age // 60} h ago")
    return " · ".join(bits) or "No weather fetched yet"


def read_location_tz(path=None):
    p = path or paths.default_location_file()
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("tz", "")
    except (OSError, ValueError, AttributeError):
        return ""


# ---- general ----------------------------------------------------------------
def launcher_label(name, which=shutil.which):
    label = LAUNCHER_LABELS.get(name, name)
    if name not in LAUNCHER_LABELS and not which(name):
        label += " (not installed)"
    return label


def desktop_is_gnome(env=None):
    env = os.environ if env is None else env
    desk = (env.get("XDG_CURRENT_DESKTOP", "") + ":" + env.get("XDG_SESSION_DESKTOP", "")).lower()
    return "gnome" in desk.split(":") or "ubuntu" in desk.split(":")


def doctor_summary(results):
    """(n_ok, n_warn, n_fail) for doctor.run_checks() output."""
    c = {"ok": 0, "warn": 0, "fail": 0}
    for r in results or ():
        s = r.get("status", "warn")
        c[s if s in c else "warn"] += 1
    return c["ok"], c["warn"], c["fail"]


def last_line(text, limit=90):
    """Last non-empty line of a progress stream, trimmed for a subtitle."""
    for line in reversed((text or "").replace("\r", "\n").splitlines()):
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""
