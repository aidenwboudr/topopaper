
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from topopaper import config, paths, util  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    for v in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
              "TOPOPAPER_DIR", "TOPOPAPER_DATA", "TOPOPAPER_CONFIG", "TOPOPAPER_SHARE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_xdg_defaults(home):
    assert paths.config_file() == home / ".config/topopaper/config.ini"
    assert paths.areas_dir() == home / ".local/share/topopaper/areas"
    assert paths.area_file() == home / ".local/state/topopaper/area"
    assert paths.cache_dir() == home / ".cache/topopaper"


def test_single_dir_mode(home, monkeypatch):
    monkeypatch.setenv("TOPOPAPER_DIR", str(home / "dev"))
    assert paths.areas_dir() == home / "dev/areas"
    assert paths.area_file() == home / "dev/area"


def test_share_dir_is_checkout_data():
    assert (paths.share_dir() / "hud.bin").is_file()
    assert (paths.share_dir() / "lights.bin").is_file()
    assert paths.font_file().is_file()


def test_config_defaults_and_roundtrip(home):
    c = config.load()
    assert c.get("theme") == "macchiato"
    assert c.get("roam_minutes") == 8.0
    assert c.get("show_clock") is True
    c.set("theme", "nord")
    c.set("show_clock", False)
    c.set("fps", 30)
    c.save()
    c2 = config.load()
    assert c2.get("theme") == "nord"
    assert c2.get("show_clock") is False
    assert c2.get("fps") == 30


def test_config_bad_values_fall_back(home):
    p = paths.config_file()
    p.parent.mkdir(parents=True)
    p.write_text("[appearance]\ntheme = neon\n[performance]\nfps = fast\n[x]\nfoo = bar\n")
    c = config.load()
    assert c.get("theme") == "macchiato"
    assert c.get("fps") == 60
    c.save()
    assert "foo = bar" in p.read_text()          # unknown keys survive a save


def test_config_keys_unique():
    # the engine's reader ignores [sections], so names must be unique
    names = [k.name for k in config.SCHEMA]
    assert len(names) == len(set(names))


def test_slugify():
    assert util.slugify("Mont Blanc / Monte Bianco") == "mont-blanc-monte-bianco"
    assert util.slugify("  Zermatt ") == "zermatt"


def test_fly_writes_area_file(home):
    util.fly("earth", quiet=True)
    assert util.current_area() == "earth"


def test_route_closure_math():
    from topopaper.build import route
    earth = dict(name="earth", mx0=0, my0=0, msx=1, msy=1, zoom=4)
    k1 = dict(name="r", mx0=0.25, my0=0.25, msx=0.25, msy=0.25, zoom=6)
    k5 = dict(name="p", mx0=0.3, my0=0.3, msx=1 / 1024, msy=1 / 1024, zoom=12)
    assert route.contains(earth, k5)
    assert route.closed_link(earth, k1)          # ratio 4
    assert not route.closed_link(earth, k5)      # ratio 1024: needs rungs
    wide_k4 = dict(name="w", mx0=0.3, my0=0.3, msx=0.0068, msy=1 / 256, zoom=10)
    assert not route.closed_link(earth, wide_k4) # wide, but 256x too coarse


def test_geocode_scoring():
    from topopaper import geocode
    assert geocode.ocover(["resevoir"], ["reservoir"]) > 0.85
    assert geocode.ocover(["steamboat"], ["steamboat", "springs"]) == 1.0
    assert abs(geocode.merc_y(0.0) - 0.5) < 1e-9


def test_cli_help_lists_commands(capsys):
    from topopaper import cli
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for c in ("fly", "search", "settings", "doctor"):
        assert c in out



