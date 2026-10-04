"""Non-GUI logic of the settings app (never imports Gtk)."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from topopaper import gc  # noqa: E402
from topopaper.settings import logic, optional  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    for v in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
              "TOPOPAPER_DIR", "TOPOPAPER_DATA", "TOPOPAPER_CONFIG", "TOPOPAPER_SHARE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOPOPAPER_DIR", str(tmp_path / "tp"))
    monkeypatch.setenv("TOPOPAPER_CONFIG", str(tmp_path / "config.ini"))
    return tmp_path


def make_pack(root, name, zoom, ny, size=1000, **meta):
    d = root / "tp" / "areas" / name
    d.mkdir(parents=True)
    (d / "terrain.bin").write_bytes(b"\0" * size)
    (d / "meta.json").write_text(json.dumps(dict(zoom=zoom, ny=ny, nx=ny, **meta)))
    return d


def test_gtk_not_imported():
    assert "gi.repository.Gtk" not in sys.modules


def test_tier_of():
    assert logic.tier_of({"zoom": 4, "ny": 16}) == 0           # the globe
    assert logic.tier_of({"zoom": 12, "ny": 4}) == 5           # local
    assert logic.tier_of({"zoom": 10, "ny": 4}) == 4
    assert logic.tier_of({"zoom": 8, "ny": 4}) == 3
    assert logic.tier_of({"zoom": 15, "ny": 8}) == 6           # ski site
    assert logic.tier_of({"zoom": 12, "ny": 5}) == 5           # rounds
    assert logic.tier_of({}) is None
    assert logic.tier_of({"zoom": 4, "ny": 0}) is None


def test_scale_label():
    assert logic.scale_label(0) == "Whole globe"
    assert logic.scale_label(3) == "Regional map"
    assert logic.scale_label(6) == "Ski resort map"
    assert logic.scale_label(None) == "Map"


@pytest.mark.parametrize("n,out", [
    (0, "0 bytes"), (1, "1 byte"), (999, "999 bytes"), (1000, "1.0 kB"),
    (15_000_000, "15.0 MB"), (128_400_000, "128 MB"), (2_500_000_000, "2.5 GB"),
    (None, "0 bytes"),
])
def test_fmt_size(n, out):
    assert logic.fmt_size(n) == out


def test_pack_title():
    assert logic.pack_title("earth") == "Earth"
    assert logic.pack_title("lake-como") == "Lake Como"
    assert logic.pack_title("alps-region-r8") == "Alps Region r8"
    assert logic.pack_title("x", {"display": "Mt. Hood"}) == "Mt. Hood"


@pytest.mark.parametrize("s,out", [
    ("all", None), ("", None), ("  ALL ", None), (None, None),
    ("DP-1", ["DP-1"]), ("DP-1, eDP-1,DP-1", ["DP-1", "eDP-1"]), (" , ", None),
])
def test_parse_outputs(s, out):
    assert logic.parse_outputs(s) == out


def test_format_and_toggle_outputs():
    assert logic.format_outputs(None) == "all"
    assert logic.format_outputs([]) == "all"
    assert logic.format_outputs(["DP-1", " eDP-1", "DP-1"]) == "DP-1,eDP-1"
    conn = ["eDP-1", "DP-1"]
    assert logic.toggle_output("all", "DP-1", False, conn) == "eDP-1"
    assert logic.toggle_output("eDP-1", "DP-1", True, conn) == "eDP-1,DP-1"
    assert logic.toggle_output("eDP-1", "eDP-1", False, conn) == "all"


def test_monitor_subtitle():
    assert logic.monitor_subtitle("Dell", "U2720Q", 597, 336, 3840, 2160) == \
        "Dell U2720Q · 27″ · 3840×2160"
    assert logic.monitor_subtitle(width_px=1280, height_px=800) == "1280×800"
    assert logic.monitor_subtitle(description="Built-in display") == "Built-in display"


def test_roam_options():
    vals, labels = logic.roam_options("8")
    assert vals == list(logic.ROAM_CHOICES)
    assert labels[0] == "Off" and labels[-1] == "Every hour"
    assert logic.roam_index(vals, "8") == vals.index(8)
    vals, labels = logic.roam_options("12")
    assert 12.0 in vals and "Every 12 minutes" in labels
    assert logic.roam_index(vals, "nonsense") == 0


def test_location_and_weather(tmp_path):
    assert logic.fmt_location("Bergen", "60.39", "5.32") == "Bergen (60.39° N, 5.32° E)"
    assert logic.fmt_location("", "-33.9", "-70.6") == "33.90° S, 70.60° W"
    p = tmp_path / "w.txt"
    ts = int(time.time())
    p.write_text(f"ts={ts}\ntemp=41\ncond=PARTLY CLOUDY\n")
    w = logic.read_weather(p, now=ts + 600)
    assert w["temp"] == "41" and w["age_min"] == 10
    assert logic.weather_summary(w, "metric") == "41°C · Partly cloudy · updated 10 min ago"
    assert logic.weather_summary(None) == "No weather fetched yet"
    assert logic.read_weather(tmp_path / "missing") is None


def test_misc():
    assert logic.desktop_is_gnome({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"})
    assert not logic.desktop_is_gnome({"XDG_CURRENT_DESKTOP": "sway"})
    assert logic.doctor_summary([{"status": "ok"}, {"status": "fail"}, {"status": "?"}]) == \
        (1, 1, 1)
    assert logic.last_line("a\r10%\r42%\n\n") == "42%"
    assert logic.launcher_label("rofi", which=lambda n: None) == "rofi (not installed)"
    assert logic.launcher_label("auto") == "Automatic"


def test_load_packs_and_delete(home):
    make_pack(home, "earth", 4, 16)
    make_pack(home, "alps", 12, 4, size=5000)
    make_pack(home, "alps-r8", 8, 4, scaffold=True)
    make_pack(home, "alpsish", 8, 4)
    packs = {p.name: p for p in logic.load_packs(home="alps", current="alps-r8")}
    assert list(packs)[0] == "earth"
    assert packs["alps"].is_home and packs["alps"].tier == 5
    assert packs["alps"].children == ["alps-r8"]          # not "alpsish"
    assert packs["alps-r8"].scaffold and packs["alps-r8"].is_current
    assert not packs["earth"].deletable
    assert packs["alps"].size >= 5000
    fp = logic.packs_fingerprint()
    assert logic.delete_packs(["earth", "../x", "alps", "alps-r8"]) == ["alps", "alps-r8"]
    assert (home / "tp/areas/earth").is_dir()
    assert logic.packs_fingerprint() != fp


def test_gc_api(home):
    old = time.time() - 100 * 86400
    for name, meta in (("town", dict(auto=True)), ("town-r3", dict(scaffold=True)),
                       ("fresh", dict(auto=True)), ("curated", {}), ("homey", dict(auto=True))):
        d = make_pack(home, name, 12, 4, **meta)
        if name != "fresh":
            os.utime(d, (old, old))
    (home / "config.ini").write_text("[general]\nhome_area = homey\n")
    c = gc.candidates(45)
    assert [x["group"] for x in c] == [["town", "town-r3"]]
    assert c[0]["size"] > 0 and c[0]["age"] > 99
    assert gc.delete(c) == ["town", "town-r3"]
    assert gc.delete([["earth"]]) == []
    assert gc.candidates(45) == []


def test_optional_missing_is_none():
    assert optional.get("definitely_not_a_module") is None
    assert optional.hint("session") == "needs topopaper-session"
