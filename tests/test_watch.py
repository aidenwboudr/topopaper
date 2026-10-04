import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from topopaper import watch  # noqa: E402
from topopaper.watch import hyprland, niri, sway  # noqa: E402


def test_detect():
    assert watch.detect({"SWAYSOCK": "/x"}) == "sway"
    assert watch.detect({"HYPRLAND_INSTANCE_SIGNATURE": "abc"}) == "hyprland"
    assert watch.detect({"NIRI_SOCKET": "/x"}) == "niri"
    assert watch.detect({"XDG_CURRENT_DESKTOP": "KDE"}) == "kde"
    assert watch.detect({"KDE_FULL_SESSION": "true"}) == "kde"
    assert watch.detect({"XDG_CURRENT_DESKTOP": "river"}) == "fallback"
    assert watch.detect({}) == "fallback"


def test_flag_writes_only_changes(tmp_path):
    f = watch.Flag(tmp_path / "c")
    f.set(True)
    assert (tmp_path / "c").read_text() == "1"
    (tmp_path / "c").write_text("x")
    f.set(True)
    assert (tmp_path / "c").read_text() == "x"      # unchanged value: no write
    f.set(False)
    assert (tmp_path / "c").read_text() == "0"


# ---- sway --------------------------------------------------------------------------
def ws(name, nodes=(), floating=()):
    return {"type": "workspace", "name": name, "nodes": list(nodes),
            "floating_nodes": list(floating)}


def tree(*workspaces):
    return {"type": "root", "nodes": [{"type": "output", "nodes": list(workspaces)}]}


def test_sway_covered():
    win = {"type": "con", "nodes": []}
    flt = {"type": "floating_con", "nodes": [], "fullscreen_mode": 0}
    vis = [{"name": "1", "visible": True}, {"name": "2", "visible": False}]
    assert sway.covered_in(tree(ws("1", [win]), ws("2")), vis)
    assert not sway.covered_in(tree(ws("1"), ws("2", [win])), vis)
    assert not sway.covered_in(tree(ws("1", floating=[flt])), vis)
    assert sway.covered_in(tree(ws("1", floating=[dict(flt, fullscreen_mode=1)])), vis)
    # two outputs: both visible workspaces must be covered
    vis2 = [{"name": "1", "visible": True}, {"name": "2", "visible": True}]
    assert not sway.covered_in(tree(ws("1", [win]), ws("2")), vis2)
    assert sway.covered_in(tree(ws("1", [win]), ws("2", [win])), vis2)
    assert not sway.covered_in(tree(), [])


def test_sway_modes():
    assert sway.parse_mode("2560x1600@120Hz") == (2560, 1600, 120000)
    assert sway.parse_mode("1920x1080@60.001Hz") == (1920, 1080, 60001)
    assert sway.parse_mode("1920x1080") == (1920, 1080, 0)
    assert sway.parse_mode("fast") is None
    cur = {"width": 2560, "height": 1600, "refresh": 120000}
    assert sway.mode_matches(cur, (2560, 1600, 120000))
    assert not sway.mode_matches(cur, (2560, 1600, 60001))
    assert sway.mode_matches(cur, (2560, 1600, 0))


def test_sway_power_off_unless_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(sway, "sm", lambda *a: calls.append(a) or b"[]")
    monkeypatch.setattr(sway.Power, "settings", staticmethod(lambda: None))
    p = sway.Power()
    p.tick(True)
    assert calls == []


def test_sway_power_debounce_and_cover_gate(monkeypatch):
    state = {"bat": False, "t": 1000.0, "mode": {"width": 100, "height": 50, "refresh": 120000}}
    calls = []

    def fake_sm(*a):
        calls.append(a)
        if a[:2] == ("output", "eDP-1"):
            w, h, r = sway.parse_mode(a[3])
            state["mode"] = {"width": w, "height": h, "refresh": r}
        return b""

    monkeypatch.setattr(sway, "sm", fake_sm)
    monkeypatch.setattr(sway, "on_battery", lambda: state["bat"])
    monkeypatch.setattr(sway, "locked", lambda: False)
    monkeypatch.setattr(sway.time, "time", lambda: state["t"])
    monkeypatch.setattr(sway.Power, "current_mode", staticmethod(lambda out: state["mode"]))
    monkeypatch.setattr(sway.Power, "settings", staticmethod(
        lambda: ("eDP-1", "100x50@120Hz", "100x50@60Hz", (100, 50, 120000), (100, 50, 60000))))
    p = sway.Power()
    p.tick(False)                     # adopt AC; mode already right
    assert p.pend is None
    state["bat"] = True
    p.tick(False)                     # first sight: candidate only
    state["t"] += 5
    p.tick(False)                     # still settling
    assert p.pend is None
    state["t"] += 30
    p.tick(False)                     # confirmed, but the wallpaper is visible
    assert p.pend and not calls
    p.tick(True)                      # windows cover it: apply now
    assert ("output", "eDP-1", "mode", "100x50@60Hz") in calls
    assert ("blur", "disable") in calls
    calls.clear()
    p.tick(True)
    assert calls == []


# ---- hyprland -------------------------------------------------------------------------
def test_hypr_events():
    assert hyprland.parse_event(b"openwindow>>80a6f50,2,kitty,~\n") == ("openwindow", "80a6f50,2,kitty,~")
    assert hyprland.parse_event(b"workspacev2>>3,3") == ("workspacev2", "3,3")
    assert hyprland.parse_event(b"activewindow>>kitty,~")[0] not in hyprland.EVENTS


def test_hypr_socket_dir(tmp_path):
    env = {"HYPRLAND_INSTANCE_SIGNATURE": "sig", "XDG_RUNTIME_DIR": str(tmp_path)}
    assert hyprland.socket_dir(env) == str(tmp_path / "hypr" / "sig")


def test_hypr_covered():
    mons = [{"activeWorkspace": {"id": 1}, "specialWorkspace": {"id": 0}},
            {"activeWorkspace": {"id": 2}, "specialWorkspace": {"id": 0}}]
    tiled = lambda w: {"workspace": {"id": w}, "floating": False, "fullscreen": 0, "mapped": True}
    assert hyprland.covered_in(mons, [tiled(1), tiled(2)])
    assert not hyprland.covered_in(mons, [tiled(1)])
    assert not hyprland.covered_in(mons, [tiled(1), dict(tiled(2), floating=True)])
    assert hyprland.covered_in(mons, [tiled(1), dict(tiled(2), floating=True, fullscreen=2)])
    assert not hyprland.covered_in(mons, [tiled(1), dict(tiled(2), hidden=True)])
    sp = [dict(mons[0], specialWorkspace={"id": -98})]
    assert hyprland.covered_in(sp, [tiled(-98)])
    assert not hyprland.covered_in([], [tiled(1)])
    assert hyprland.covered_in([mons[0], dict(mons[1], disabled=True)], [tiled(1)])


# ---- niri ------------------------------------------------------------------------------
def test_niri_covered():
    wss = [{"id": 1, "output": "A", "is_active": True}, {"id": 2, "output": "A", "is_active": False},
           {"id": 3, "output": "B", "is_active": True}]
    w = lambda ws, fl=False: {"id": 9, "workspace_id": ws, "is_floating": fl}
    assert niri.covered_in(wss, [w(1), w(3)])
    assert not niri.covered_in(wss, [w(1), w(2)])
    assert not niri.covered_in(wss, [w(1), w(3, fl=True)])
    assert niri.covered_in(wss[:1], [{"workspace_id": 1}])      # older niri: no is_floating
    assert not niri.covered_in([], [])
