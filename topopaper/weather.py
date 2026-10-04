"""`topopaper-ctl weather`: the built-in weather fetcher behind the HUD.

One fetch writes two files the engine polls (docs/ARCHITECTURE.md):
weather.txt (temperature, condition, an optional heads-up line, the snowline
of the pack on screen) and location.json (the timezone the clock follows).

Everything comes from Open-Meteo (no key). The location is either the one
set in the settings or an IP-based guess, rounded to 0.1 deg (~11 km) before
it is used for anything else, so no precise position ever leaves the machine
or lands on disk. The session calls `refresh()` every 15 minutes.
"""
import json
import math
import sys
import time
import urllib.parse

from . import config, net, paths, util

FORECAST = "https://api.open-meteo.com/v1/forecast"
IP_SERVICES = ["https://ipapi.co/json/", "https://ipwho.is/",
               "https://get.geojs.io/v1/ip/geo.json"]
IP_CACHE_S = 6 * 3600

# WMO weather interpretation codes (Open-Meteo `weather_code`). Only glyphs
# the HUD has: A-Z, space, - / & '
WMO = {
    0: "CLEAR", 1: "MOSTLY CLEAR", 2: "PARTLY CLOUDY", 3: "OVERCAST",
    45: "FOG", 48: "FREEZING FOG",
    51: "LIGHT DRIZZLE", 53: "DRIZZLE", 55: "HEAVY DRIZZLE",
    56: "FREEZING DRIZZLE", 57: "FREEZING DRIZZLE",
    61: "LIGHT RAIN", 63: "RAIN", 65: "HEAVY RAIN",
    66: "FREEZING RAIN", 67: "FREEZING RAIN",
    71: "LIGHT SNOW", 73: "SNOW", 75: "HEAVY SNOW", 77: "SNOW GRAINS",
    80: "LIGHT SHOWERS", 81: "SHOWERS", 82: "HEAVY SHOWERS",
    85: "SNOW SHOWERS", 86: "HEAVY SNOW SHOWERS",
    95: "THUNDERSTORMS", 96: "STORMS & HAIL", 99: "STORMS & HAIL",
}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
STORM_CODES = {95, 96, 99}
SOON_S = 90 * 60          # how far ahead "rain starting" looks
WET_MM = 0.1              # per 15 min; below this it's not worth a heads-up
SWING_C = 8.0             # day-to-day high change that earns a line
SNOWLINE_MAX_DEG = 30.0   # wider packs span several climates: no single level


# ---- pure parts (unit-tested) -------------------------------------------------
def describe(code):
    """Short HUD text for a WMO code; unknown codes fall back to the family."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return ""
    if code in WMO:
        return WMO[code]
    for lo, hi, text in ((1, 3, "CLOUDY"), (40, 49, "FOG"), (50, 59, "DRIZZLE"),
                         (60, 69, "RAIN"), (70, 79, "SNOW"), (80, 84, "SHOWERS"),
                         (85, 89, "SNOW SHOWERS"), (90, 99, "THUNDERSTORMS")):
        if lo <= code <= hi:
            return text
    return ""


def to_units(celsius, units):
    """Integer temperature in the user's units (round half away from zero,
    so -0.5 C reads -1 like a thermometer, not 0)."""
    v = celsius * 9 / 5 + 32 if units == "imperial" else celsius
    return int(math.floor(abs(v) + 0.5)) * (1 if v >= 0 else -1)


def _kind(code, snowfall):
    if code in STORM_CODES:
        return "STORMS"
    if code in SNOW_CODES or (snowfall or 0) > 0:
        return "SNOW"
    return "RAIN"


def precip_soon(fc, now):
    """(kind, start_unix) when rain/snow/storms start within 90 min and it is
    dry right now; else None. Uses the 15-minute series."""
    m = fc.get("minutely_15") or {}
    times = m.get("time") or []
    pr = m.get("precipitation") or []
    codes = m.get("weather_code") or []
    snow = m.get("snowfall") or []
    wet_now = False
    for i, t in enumerate(times):
        if t + 900 <= now:
            continue                       # slot already over
        if t > now + SOON_S:
            break
        p = pr[i] if i < len(pr) and pr[i] is not None else 0.0
        c = codes[i] if i < len(codes) and codes[i] is not None else 0
        wet = p >= WET_MM or c in STORM_CODES
        if t <= now:                       # the slot we're in
            wet_now = wet
            continue
        if wet and not wet_now:
            return _kind(c, snow[i] if i < len(snow) else 0), int(t)
        if wet:
            return None                    # already raining: nothing new to say
    return None


def overnight_low(fc, now):
    """Lowest hourly temperature (C) from this evening 18:00 to 09:00 tomorrow,
    local time; None without hourly data."""
    h = fc.get("hourly") or {}
    times, temps = h.get("time") or [], h.get("temperature_2m") or []
    off = fc.get("utc_offset_seconds") or 0
    local_now = now + off
    midnight = local_now - local_now % 86400
    lo_t, hi_t = midnight + 18 * 3600, midnight + 33 * 3600
    vals = [temps[i] for i, t in enumerate(times)
            if i < len(temps) and temps[i] is not None
            and lo_t <= t + off <= hi_t and t + 3600 > now]
    return min(vals) if vals else None


def derive_news(fc, now):
    """The single heads-up line for weather.txt as (news, neta-or-None).
    Priority: precipitation starting soon > frost tonight > big swing."""
    soon = precip_soon(fc, now)
    if soon:
        return "@" + soon[0], soon[1]
    d = fc.get("daily") or {}
    highs = [x for x in (d.get("temperature_2m_max") or []) if x is not None]
    low = overnight_low(fc, now)
    if low is not None and highs and low <= 0.0 and highs[0] > 4.0:
        return "FROST TONIGHT", None
    if len(highs) >= 2 and abs(highs[1] - highs[0]) >= SWING_C:
        return ("MUCH WARMER TOMORROW" if highs[1] > highs[0]
                else "MUCH COLDER TOMORROW"), None
    return "", None


def freezing_level_now(fc, now):
    h = fc.get("hourly") or {}
    times, lv = h.get("time") or [], h.get("freezing_level_height") or []
    for i, t in enumerate(times):
        if t <= now < t + 3600 and i < len(lv) and lv[i] is not None:
            return int(round(lv[i]))
    return None


def render(fc, now, units, snow=None):
    """weather.txt text for one forecast response."""
    cur = fc.get("current") or {}
    lines = [f"ts={int(now)}"]
    if cur.get("temperature_2m") is not None:
        lines.append(f"temp={to_units(cur['temperature_2m'], units)}")
    lines.append(f"cond={describe(cur.get('weather_code'))}")
    news, eta = derive_news(fc, now)
    if news:
        lines.append(f"news={news}")
        if eta:
            lines.append(f"neta={eta}")
    if snow:
        lines += [f"apack={snow[0]}", f"afrz={snow[1]}"]
    return "\n".join(lines) + "\n"


def parse_ip_location(d):
    """(lat, lon, tz, city) from any of the three IP services' JSON."""
    if d.get("success") is False or d.get("error"):
        return None
    try:
        lat, lon = float(d["latitude"]), float(d["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    tz = d.get("timezone")
    if isinstance(tz, dict):           # ipwho.is
        tz = tz.get("id")
    return lat, lon, tz or "", d.get("city") or ""


def round_coord(v):
    return round(float(v), 1)


# ---- I/O ----------------------------------------------------------------------
def _ip_cache():
    return paths.cache_dir() / "ip-location.json"


def ip_location(max_age=IP_CACHE_S):
    """Approximate location from the IP address, cached ~6 h. Only the
    rounded coordinates are kept."""
    try:
        c = json.loads(_ip_cache().read_text())
        if time.time() - c["ts"] < max_age:
            return c
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for url in IP_SERVICES:
        try:
            got = parse_ip_location(net.fetch_json(url, timeout=8))
        except Exception:               # any failure: next service
            got = None
        if got:
            c = {"ts": int(time.time()), "lat": round_coord(got[0]),
                 "lon": round_coord(got[1]), "tz": got[2], "city": got[3],
                 "source": "ip"}
            util.write_atomic(str(_ip_cache()), json.dumps(c))
            return c
    return None


def location(cfg):
    """{'lat','lon','city','source'} or None. Manual settings win."""
    if cfg.get("location_mode") == "manual":
        try:
            return {"lat": round_coord(cfg.get("latitude")),
                    "lon": round_coord(cfg.get("longitude")),
                    "city": cfg.get("place_name"), "tz": "", "source": "manual"}
        except (TypeError, ValueError):
            return None
    return ip_location()


def forecast(lat, lon):
    q = {
        "latitude": f"{lat:.1f}", "longitude": f"{lon:.1f}",
        "current": "temperature_2m,weather_code",
        "minutely_15": "precipitation,snowfall,weather_code",
        "hourly": "temperature_2m",
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": "auto", "timeformat": "unixtime",
        "forecast_days": 2, "forecast_minutely_15": 12,
    }
    return net.fetch_json(f"{FORECAST}?{urllib.parse.urlencode(q)}", timeout=15)


def snowline(now):
    """(pack, freezing level m) for the pack on screen, or None."""
    name = util.current_area()
    meta = util.read_meta(name) if name else None
    if not meta or name == "earth" or not meta.get("bbox"):
        return None
    s, w, n, e = meta["bbox"]
    if n - s > SNOWLINE_MAX_DEG or e - w > SNOWLINE_MAX_DEG:
        return None
    q = {"latitude": f"{(s + n) / 2:.2f}", "longitude": f"{(w + e) / 2:.2f}",
         "hourly": "freezing_level_height", "timeformat": "unixtime",
         "timezone": "GMT", "forecast_days": 1}
    try:
        lv = freezing_level_now(net.fetch_json(
            f"{FORECAST}?{urllib.parse.urlencode(q)}", timeout=15), now)
    except Exception:                   # snowline is decoration
        return None
    return (name, lv) if lv else None


def refresh(verbose=False):
    """Fetch once and rewrite weather.txt + location.json. True on success;
    on failure the old files stay (the HUD hides stale weather after 2 h)."""
    net.prefer_ipv4()
    cfg = config.load()
    loc = location(cfg)
    if not loc:
        if verbose:
            print("weather: no location (set one in the settings, or check the network)",
                  file=sys.stderr)
        return False
    try:
        fc = forecast(loc["lat"], loc["lon"])
    except Exception as e:
        if verbose:
            print(f"weather: Open-Meteo request failed: {e}", file=sys.stderr)
        return False
    now = int(time.time())
    text = render(fc, now, cfg.get("units"), snowline(now))
    util.write_atomic(str(paths.default_weather_file()), text)
    tz = fc.get("timezone") or loc.get("tz") or ""
    if tz in ("GMT", "UTC") and loc.get("tz"):
        tz = loc["tz"]
    lj = {"tz": tz, "lat": loc["lat"], "lon": loc["lon"],
          "place": loc.get("city") or "", "source": loc["source"], "ts": now}
    util.write_atomic(str(paths.default_location_file()), json.dumps(lj, indent=1) + "\n")
    if verbose:
        print(text, end="")
        print(f"location: {lj['place'] or '?'} ({lj['lat']}, {lj['lon']}) tz={tz}")
    return True


def main(argv=None):
    argv = list(argv or [])
    if "-h" in argv or "--help" in argv:
        print("usage: topopaper-ctl weather [--quiet]\n\nFetch the weather once and "
              f"write\n  {paths.default_weather_file()}\n  {paths.default_location_file()}")
        return 0
    ok = refresh(verbose="--quiet" not in argv)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
