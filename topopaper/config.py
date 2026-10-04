"""config.ini: the one settings file shared by the engine, the session and the
settings app.

Plain INI. Section headers only group keys for humans: every key name is
unique across sections, because the engine's C reader ignores sections.
The engine re-reads the file whenever its mtime changes, so saving is
enough to apply a change; no restart needed (except where noted).
"""
import configparser
import os
from dataclasses import dataclass

from . import paths


@dataclass(frozen=True)
class Key:
    section: str
    name: str
    default: str
    kind: str          # bool, int, float, str, choice
    choices: tuple = ()
    doc: str = ""


SCHEMA = [
    Key("general", "home_area", "earth", "str",
        doc="pack shown at start (and where 'home' flies to)"),
    Key("general", "roam_minutes", "8", "float",
        doc="mean minutes between automatic flights to another pack; 0 = stay put"),
    Key("general", "animation_speed", "1.0", "float",
        doc="multiplier for drift, contour crawl and zoom breathing"),

    Key("appearance", "theme", "macchiato", "choice",
        ("macchiato", "mocha", "nord", "gruvbox", "tokyonight", "graphite"),
        doc="colour palette"),
    Key("appearance", "city_lights", "true", "bool", doc="night-side city lights on the globe"),
    Key("appearance", "aurora", "true", "bool", doc="aurora ovals on the night side"),
    Key("appearance", "labels", "true", "bool", doc="place, peak and lift names"),
    Key("appearance", "react_to_cpu", "true", "bool",
        doc="speed up and warm the lines while the CPU is busy"),

    Key("clock", "show_clock", "true", "bool", doc="clock on the wallpaper"),
    Key("clock", "clock_format", "12h", "choice", ("12h", "24h")),
    Key("clock", "show_weather", "true", "bool", doc="temperature and conditions under the clock"),
    Key("clock", "show_news", "true", "bool", doc="short weather heads-up line (rain soon, frost tonight)"),
    Key("clock", "units", "imperial", "choice", ("imperial", "metric"),
        doc="temperatures, and peak heights on packs built from now on"),

    Key("performance", "fps", "60", "int", doc="frame-rate cap on AC power"),
    Key("performance", "fps_battery", "24", "int", doc="frame-rate cap on battery"),
    Key("performance", "fps_covered", "3", "int", doc="frame-rate cap while windows cover the wallpaper"),
    Key("performance", "pause_when_covered", "true", "bool",
        doc="freeze the animation while windows cover the wallpaper"),

    Key("display", "outputs", "all", "str",
        doc="'all', or comma-separated output names (e.g. DP-1,eDP-1)"),
    Key("display", "layer", "bottom", "choice", ("bottom", "background"),
        doc="layer-shell layer; 'background' sits under other wallpaper tools"),

    Key("location", "location_mode", "auto", "choice", ("auto", "manual"),
        doc="auto = approximate location from your IP address (rounded to ~11 km)"),
    Key("location", "latitude", "", "str"),
    Key("location", "longitude", "", "str"),
    Key("location", "place_name", "", "str"),

    Key("search", "launcher", "auto", "choice",
        ("auto", "rofi", "fuzzel", "wofi", "tofi", "settings"),
        doc="menu used by `topopaper-ctl search`"),

    Key("advanced", "weather_file", "", "str",
        doc="read the HUD's weather from this file instead of the built-in fetcher"),
    Key("advanced", "location_file", "", "str",
        doc="read the clock's timezone from this JSON file ({\"tz\": ...})"),
    Key("advanced", "builtin_weather", "true", "bool",
        doc="run the built-in Open-Meteo fetcher in the session"),
    Key("advanced", "power_profile", "none", "choice", ("none", "sway"),
        doc="sway: switch an output's mode (and SwayFX blur) with the power source"),
    Key("advanced", "power_output", "", "str", doc="output for power_profile, e.g. eDP-1"),
    Key("advanced", "power_mode_ac", "", "str", doc="e.g. 2560x1600@120Hz"),
    Key("advanced", "power_mode_battery", "", "str", doc="e.g. 2560x1600@60Hz"),
]

BY_NAME = {k.name: k for k in SCHEMA}
SECTIONS = list(dict.fromkeys(k.section for k in SCHEMA))
TRUE = {"1", "true", "yes", "on"}


class Config:
    def __init__(self, path=None):
        self.path = path or paths.config_file()
        self.values = {k.name: k.default for k in SCHEMA}
        self.extra = {}            # unknown keys survive a save
        self.load()

    def load(self):
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(self.path, encoding="utf-8")
        except configparser.Error:
            return self
        for sec in cp.sections():
            for name, val in cp.items(sec):
                if name in BY_NAME:
                    self.values[name] = val.strip()
                else:
                    self.extra[(sec, name)] = val
        return self

    def get(self, name):
        k = BY_NAME[name]
        v = self.values.get(name, k.default)
        try:
            if k.kind == "bool":
                return v.strip().lower() in TRUE
            if k.kind == "int":
                return int(float(v))
            if k.kind == "float":
                return float(v)
        except ValueError:
            return self._typed_default(k)
        if k.kind == "choice" and v not in k.choices:
            return k.default
        return v

    @staticmethod
    def _typed_default(k):
        return {"bool": k.default in TRUE, "int": int(float(k.default or 0)),
                "float": float(k.default or 0)}.get(k.kind, k.default)

    def set(self, name, value):
        k = BY_NAME[name]
        if k.kind == "bool":
            value = "true" if value in (True, "true", "1", 1) else "false"
        self.values[name] = str(value)

    def save(self):
        lines = ["# topopaper settings. Edit by hand or with topopaper-settings;",
                 "# the wallpaper applies changes within a second.", ""]
        for sec in SECTIONS:
            lines.append(f"[{sec}]")
            for k in SCHEMA:
                if k.section != sec:
                    continue
                if k.doc:
                    lines.append(f"# {k.doc}")
                lines.append(f"{k.name} = {self.values.get(k.name, k.default)}")
            for (s, n), v in self.extra.items():
                if s == sec:
                    lines.append(f"{n} = {v}")
            lines.append("")
        for sec in dict.fromkeys(s for s, _ in self.extra):
            if sec not in SECTIONS:
                lines.append(f"[{sec}]")
                lines += [f"{n} = {v}" for (s, n), v in self.extra.items() if s == sec]
                lines.append("")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, self.path)     # atomic: the engine never reads half a file


def load():
    return Config()
