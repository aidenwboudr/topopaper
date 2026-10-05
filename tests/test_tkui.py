"""The Tk settings window and picker (topopaper/tkui/).

The pure parts run everywhere; the window tests need tkinter and a display
(they skip without them, e.g. on a headless Linux box with no $DISPLAY).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from topopaper import config, themes, util  # noqa: E402
from topopaper.tkui import helpers, look, picker, settings, store, work  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    for v in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
              "TOPOPAPER_DIR", "TOPOPAPER_DATA", "TOPOPAPER_CONFIG", "TOPOPAPER_SHARE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOPOPAPER_DIR", str(tmp_path / "tp"))
    monkeypatch.setenv("TOPOPAPER_CONFIG", str(tmp_path / "config.ini"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    return tmp_path


def make_pack(root, name, zoom=12, ny=4, size=1000, **meta):
    d = root / "tp" / "areas" / name
    d.mkdir(parents=True)
    (d / "terrain.bin").write_bytes(b"\0" * size)
    (d / "meta.json").write_text(json.dumps(dict(zoom=zoom, ny=ny, nx=ny, **meta)))
    return d


def test_modules_import_without_a_display():
    # settings and picker defer every Tk call to main(), so importing is safe
    assert callable(settings.main) and callable(picker.main)
    assert "gi.repository.Gtk" not in sys.modules


def test_tk_modules_import():
    pytest.importorskip("tkinter")
    from topopaper.tkui import pages, places, widgets
    assert widgets.px(10) >= 10 and places.PlacesPage.name == "places"
    assert {c.name for c in (pages.AppearancePage, pages.MotionPage, pages.ClockPage,
                             pages.GeneralPage, pages.AboutPage)} == \
        set(helpers.PAGES) - {"places"}


# ---- arguments and the first page ------------------------------------------------
def test_parse_args_matches_the_gtk_options():
    a = helpers.parse_args(["--page", "motion", "--search", "Zermatt"])
    assert (a.page, a.search) == ("motion", "Zermatt")
    assert helpers.parse_args(["--page", "displays"]).page == "displays"   # GTK-only, accepted
    with pytest.raises(SystemExit):
        helpers.parse_args(["--page", "nonsense"])


@pytest.mark.parametrize("page,search,exists,out", [
    (None, None, True, "places"),
    ("motion", None, True, "motion"),
    ("displays", None, True, "general"),
    ("welcome", None, True, "clock"),
    (None, None, False, "clock"),              # first run: set the location first
    ("motion", None, False, "clock"),
    (None, "Zermatt", False, "places"),        # an explicit search wins
])
def test_first_page(page, search, exists, out):
    assert helpers.first_page(page, search, exists) == out


# ---- the picker's filter ---------------------------------------------------------
ITEMS = [dict(name="zermatt", title="Zermatt", scale="Local map", current=True),
         dict(name="earth", title="Earth", scale="Whole globe", current=False),
         dict(name="saas-fee", title="Saas-Fée", scale="Ski resort map", current=False),
         dict(name="zermatt-breuil-cervinia", title="Zermatt Breuil Cervinia",
              scale="Ski resort map", current=False),
         dict(name="grand-teton", title="Grand Teton", scale="Area map", current=False)]


def names(items):
    return [it["name"] for it in items]


def test_rank_empty_query_keeps_everything_in_order():
    assert names(helpers.rank("", ITEMS)) == names(ITEMS)
    assert names(helpers.rank("   ", ITEMS)) == names(ITEMS)


def test_rank_prefix_and_word_matches():
    assert names(helpers.rank("zer", ITEMS)) == ["zermatt", "zermatt-breuil-cervinia"]
    assert names(helpers.rank("Zermatt", ITEMS))[0] == "zermatt"            # exact first
    assert names(helpers.rank("cerv", ITEMS)) == ["zermatt-breuil-cervinia"]
    assert names(helpers.rank("teton", ITEMS)) == ["grand-teton"]
    assert names(helpers.rank("gr te", ITEMS)) == ["grand-teton"]           # word prefixes


def test_rank_folds_accents_case_and_punctuation():
    assert names(helpers.rank("saas fee", ITEMS)) == ["saas-fee"]
    assert names(helpers.rank("SAAS-FÉE", ITEMS)) == ["saas-fee"]


def test_rank_subsequence_is_a_weak_last_resort():
    assert names(helpers.rank("zrmt", ITEMS))[:1] == ["zermatt"]
    assert helpers.rank("zz", ITEMS) == []                  # too short to guess
    assert helpers.rank("hokkaido", ITEMS) == []


def test_picker_entries_end_with_a_world_search():
    out = picker.entries("zer", ITEMS)
    assert names(out[:-1]) == ["zermatt", "zermatt-breuil-cervinia"]
    assert out[-1] == {"search": "zer"}
    assert picker.entries("", ITEMS) == ITEMS
    assert picker.entries("hokkaido ", ITEMS) == [{"search": "hokkaido"}]


def test_picker_items_hide_rungs_and_put_the_current_map_first():
    packs = [("earth", {"zoom": 4, "ny": 16}),
             ("saas-fee", {"zoom": 15, "ny": 8, "display": "Saas-Fee"}),
             ("zermatt", {"zoom": 12, "ny": 4}),
             ("zermatt-r8", {"zoom": 8, "ny": 4, "scaffold": True}),
             ("zermatt-r6", {"zoom": 6, "ny": 4, "scaffold": True})]
    out = helpers.picker_items(packs, current="zermatt-r6")
    assert names(out) == ["zermatt-r6", "earth", "saas-fee", "zermatt"]
    assert out[0]["current"] and not out[1]["current"]
    assert out[1]["scale"] == "Whole globe" and out[2]["title"] == "Saas-Fee"


def test_parse_args_of_the_picker():
    assert picker.parse_args(["grand", "teton"]).text == ["grand", "teton"]
    assert picker.parse_args([]).text == []


# ---- small formatters -------------------------------------------------------------
def test_can_delete_never_the_globe_home_or_screen():
    assert helpers.can_delete("zermatt", home="earth", current="saas-fee")
    assert not helpers.can_delete("earth")
    assert not helpers.can_delete("zermatt", home="zermatt")
    assert not helpers.can_delete("zermatt", current="zermatt")
    assert not helpers.can_delete("")


def test_job_subtitle():
    assert helpers.job_subtitle({"state": "running", "stage": "building the map"}) == \
        "Building the map…"
    assert helpers.job_subtitle({"state": "running"}) == "Starting…"
    assert helpers.job_subtitle({"state": "done", "stage": "ready"}) == "Ready"
    assert helpers.job_subtitle({"state": "failed", "stage": "build failed"}) == \
        "Failed — build failed"


def test_maps_summary():
    assert helpers.maps_summary(1, 0) == "1 map"
    assert helpers.maps_summary(4, 1) == "4 maps · 1 connector map hidden"
    assert helpers.maps_summary(0, 3) == "0 maps · 3 connector maps hidden"


def test_parse_coords():
    assert helpers.parse_coords(" 46.02 ", "7,75") == (46.02, 7.75)
    assert helpers.parse_coords("-90", "180") == (-90.0, 180.0)
    for lat, lon in (("91", "0"), ("0", "-181"), ("north", "7"), ("", "")):
        with pytest.raises(ValueError):
            helpers.parse_coords(lat, lon)


# ---- palette ---------------------------------------------------------------------
@pytest.mark.parametrize("name", list(themes.THEMES))
def test_palette_is_readable_in_every_theme(name):
    p = look.palette(name)
    c = {k: look.from_hex(v) for k, v in p.items()}
    assert look.contrast(c["text"], c["card"]) >= 7.0
    assert look.contrast(c["text"], c["bg"]) >= 7.0
    for k in ("dim", "accent", "danger", "ok", "warn"):
        assert look.contrast(c[k], c["card"]) >= 4.5, k
    assert look.contrast(c["on_accent"], c["accent"]) >= 4.5
    assert look.contrast(c["on_danger"], c["danger"]) >= 4.5
    assert look.luminance(c["bg"]) < 0.05                 # dark, like the wallpaper


def test_palette_falls_back_to_the_default_theme():
    assert look.palette("no-such-theme") == look.palette(config.BY_NAME["theme"].default)


def test_colour_maths():
    assert look.to_hex((1, 0.5, 0)) == "#ff8000"
    assert look.from_hex("#ff8000") == (1.0, 128 / 255.0, 0.0)
    assert look.contrast((0, 0, 0), (1, 1, 1)) == pytest.approx(21.0)
    dark = (0.1, 0.1, 0.12)
    assert look.contrast(look.readable((0.2, 0.2, 0.25), dark, 4.5), dark) >= 4.5


# ---- the config writer -------------------------------------------------------------
class FakeClock:
    """Stands in for Tk's after/after_cancel."""

    def __init__(self):
        self.timers = {}
        self.n = 0

    def after(self, ms, fn):
        self.n += 1
        self.timers[self.n] = fn
        return self.n

    def cancel(self, h):
        self.timers.pop(h, None)

    def fire(self):
        for h, fn in list(self.timers.items()):
            self.timers.pop(h)()


def test_store_writes_at_once_and_keeps_hand_edits(home):
    path = str(home / "config.ini")
    s = store.Store(path)
    assert not s.exists()
    s.set("labels", False)
    assert s.exists() and config.Config(path).get("labels") is False
    other = config.Config(path)                  # a hand edit while the window is open
    other.set("units", "metric")
    other.save()
    s.set("theme", "nord")
    fresh = config.Config(path)
    assert (fresh.get("theme"), fresh.get("units"), fresh.get("labels")) == \
        ("nord", "metric", False)


def test_store_debounces_slider_writes(home):
    path = str(home / "config.ini")
    clock = FakeClock()
    s = store.Store(path, after=clock.after, cancel=clock.cancel)
    for v in ("0.50", "0.75", "1.25"):
        s.set("animation_speed", v, debounce=True)
    assert s.get("animation_speed") == 1.25          # reads see it at once
    assert not os.path.exists(path)                   # but nothing is written yet
    assert len(clock.timers) == 1                     # one pending save, not three
    clock.fire()
    assert config.Config(path).get("animation_speed") == 1.25
    assert s.pending == {}


def test_store_flush_and_reload(home):
    path = str(home / "config.ini")
    clock = FakeClock()
    s = store.Store(path, after=clock.after, cancel=clock.cancel)
    seen = []
    s.connect(seen.append)
    s.set("fps", 30, debounce=True)
    assert s.reload() == set()                        # flushes our own write first
    assert config.Config(path).get("fps") == 30 and not clock.timers
    other = config.Config(path)
    other.set("theme", "gruvbox")
    other.save()
    assert s.reload() == {"theme"}
    assert seen == [{"theme"}] and s.get("theme") == "gruvbox"


def test_store_save_all_creates_the_file(home):
    path = str(home / "config.ini")
    s = store.Store(path)
    s.save_all()
    assert os.path.exists(path)
    assert config.Config(path).get("theme") == config.BY_NAME["theme"].default


# ---- background work ----------------------------------------------------------------
def test_resolve_place_reports_what_search_said(monkeypatch):
    shown = []
    monkeypatch.setattr(util, "notify", lambda msg, ms=0: shown.append(msg))
    from topopaper import net, search
    monkeypatch.setattr(net, "prefer_ipv4", lambda: None)

    def fake_resolve(text):
        util.notify(f'couldn\'t find "{text}"', 4000)
        return 1
    monkeypatch.setattr(search, "resolve", fake_resolve)
    rc, msgs = work.resolve_place("atlantis")
    assert rc == 1 and msgs == ['couldn\'t find "atlantis"']
    assert shown == msgs                              # still passed on to the desktop
    assert work.resolve_error("atlantis", rc, msgs) == 'Couldn\'t find "atlantis"'
    assert work.resolve_error("x", 0, msgs) == ""
    assert work.resolve_error("x", 1, []) == "Couldn’t find “x”"
    assert "boom" in work.resolve_error("x", None, None, RuntimeError("boom"))


def test_read_tail(tmp_path):
    p = tmp_path / "log"
    p.write_text("first line\n" + "x" * 50 + "\nlast line\n")
    assert work.read_tail(p).endswith("last line\n")
    tail = work.read_tail(p, limit=20)
    assert tail == "last line\n"                       # the cut-off line is dropped
    assert work.read_tail(tmp_path / "missing") == ""


def test_starter_as_job(home):
    s = work.Starter(home / "starter.log")
    (home / "starter.log").write_text("downloading earth.tar.gz\n42%\n")
    s.state = "running"
    j = s.as_job()
    assert (j["slug"], j["state"], j["stage"]) == ("__globe__", "running", "42%")


# ---- the windows (need tkinter and a display) -----------------------------------------
def _display_or_skip():
    pytest.importorskip("tkinter")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no $DISPLAY")
    import tkinter
    try:
        r = tkinter.Tk()
        r.destroy()
    except tkinter.TclError as e:
        pytest.skip(f"no usable display: {e}")


def _pump(root, n=20):
    for _ in range(n):
        root.update()


def test_settings_window_builds_every_page(home):
    _display_or_skip()
    from topopaper.tkui.widgets import make_root
    make_pack(home, "earth", zoom=4, ny=16)
    make_pack(home, "zermatt", display="Zermatt")
    make_pack(home, "zermatt-r8", zoom=8, scaffold=True)
    (home / "tp" / "area").write_text("zermatt\n")
    root = make_root()
    try:
        win = settings.Window(root, "clock")
        root.deiconify()                              # key events need a mapped window
        assert win.current == "clock"
        assert not any(isinstance(p, settings._BrokenPage) for p in win.pages.values())
        for name in helpers.PAGES:
            win.select(name)
            _pump(root)
            assert win.current == name
        win.step(1)
        assert win.current == helpers.PAGES[0]
        nav = win.nav[win.current].f                  # keyboard: Down in the sidebar
        nav.focus_force()
        _pump(root)
        nav.event_generate("<Down>")
        assert win.current == helpers.PAGES[1]
        places = win.pages["places"]
        assert places._packs and {p.name for p in places._packs} >= {"earth", "zermatt"}
        win.select("appearance")
        win.pages["appearance"]._pick("nord")
        _pump(root)
        assert win.theme == "nord" and config.Config(None).get("theme") == "nord"
        assert win.current == "appearance"            # the rebuild kept the page
        win.store.set("show_clock", False)
        assert config.Config(None).get("show_clock") is False
    finally:
        root.destroy()


def test_picker_filters_and_moves(home):
    _display_or_skip()
    from topopaper.tkui.widgets import make_root
    make_pack(home, "earth", zoom=4, ny=16)
    make_pack(home, "zermatt")
    make_pack(home, "saas-fee", zoom=15, ny=8)
    root = make_root()
    try:
        pk = picker.Picker(root, "")
        assert names(pk.shown) == ["earth", "saas-fee", "zermatt"]
        pk.var.set("zer")
        assert names(pk.shown[:-1]) == ["zermatt"] and pk.shown[-1] == {"search": "zer"}
        pk.move(1)
        assert pk.sel == 1
        pk.move(5)
        assert pk.sel == len(pk.shown) - 1
        pk.var.set("")
        assert pk.sel == 0
    finally:
        root.destroy()
