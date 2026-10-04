import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from topopaper import autostart  # noqa: E402

EXE = "/opt/tp/bin/topopaper-session"
CTL = "/opt/tp/bin/topopaper-ctl"
SYSTEMCTL = []


@pytest.fixture
def home(tmp_path, monkeypatch):
    for v in ("XDG_CONFIG_HOME", "SWAYSOCK", "HYPRLAND_INSTANCE_SIGNATURE", "NIRI_SOCKET",
              "XDG_CURRENT_DESKTOP", "KDE_FULL_SESSION"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(autostart, "launcher",
                        lambda n: {"topopaper-session": EXE, "topopaper-ctl": CTL}[n])
    monkeypatch.setattr(autostart, "reload_compositor", lambda c: None)
    SYSTEMCTL.clear()
    monkeypatch.setattr(autostart, "_systemctl", lambda *a: (SYSTEMCTL.append(a), (True, ""))[1])
    return tmp_path


def use(monkeypatch, comp):
    monkeypatch.setenv("TOPOPAPER_COMPOSITOR", comp)


def backups(d):
    return [p for p in d.iterdir() if ".topopaper-bak-" in p.name]


def test_sway_block_roundtrip(home, monkeypatch):
    use(monkeypatch, "sway")
    cfg = home / ".config/sway/config"
    cfg.parent.mkdir(parents=True)
    orig = "set $mod Mod4\nbindsym $mod+Return exec foot\n"
    cfg.write_text(orig)
    ok, msg = autostart.enable()
    assert ok and str(cfg) in msg
    text = cfg.read_text()
    assert f"exec {EXE}" in text and text.startswith(orig)
    assert autostart.status()["enabled"]
    assert len(backups(cfg.parent)) == 1
    autostart.enable()                              # idempotent: no second block
    assert cfg.read_text().count("exec /opt") == 1
    ok, _ = autostart.disable()
    assert ok and cfg.read_text() == orig
    assert not autostart.status()["enabled"]


def test_sway_uses_config_d_when_included(home, monkeypatch):
    use(monkeypatch, "sway")
    cfg = home / ".config/sway/config"
    cfg.parent.mkdir(parents=True)
    orig = "include /etc/sway/config.d/*\ninclude ~/.config/sway/config.d/*.conf\n"
    cfg.write_text(orig)
    assert autostart.sway_dropin_ok()
    autostart.enable()
    drop = home / ".config/sway/config.d/topopaper.conf"
    assert f"exec {EXE}" in drop.read_text()
    assert cfg.read_text() == orig and not backups(cfg.parent)
    autostart.keybind_add()
    assert "bindsym Mod4+Shift+b exec /opt/tp/bin/topopaper-ctl search" in \
        (home / ".config/sway/config.d/topopaper-keys.conf").read_text()
    autostart.keybind_remove()
    autostart.disable()
    assert not drop.exists() and cfg.read_text() == orig


def test_system_include_alone_does_not_count(home, monkeypatch):
    use(monkeypatch, "sway")
    cfg = home / ".config/sway/config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("include /etc/sway/config.d/*\n")
    assert not autostart.sway_dropin_ok()
    cfg.write_text("include $HOME/.config/sway/config.d/*\n")
    assert autostart.sway_dropin_ok()
    cfg.write_text("include config.d/*\n")
    assert autostart.sway_dropin_ok()


def test_hyprland(home, monkeypatch):
    use(monkeypatch, "hyprland")
    cfg = home / ".config/hypr/hyprland.conf"
    cfg.parent.mkdir(parents=True)
    orig = "monitor=,preferred,auto,1\nsource = ~/.config/hypr/more.conf"   # no trailing newline
    cfg.write_text(orig)
    assert autostart.enable()[0]
    assert f"exec-once = {EXE}" in cfg.read_text()
    assert autostart.keybind_add("Super+Shift+B")[0]
    assert f"bind = SUPER SHIFT, B, exec, {CTL} search" in cfg.read_text()
    assert autostart.keybind_status()["enabled"]
    autostart.keybind_remove()
    autostart.disable()
    assert cfg.read_text() == orig + "\n"


def test_niri(home, monkeypatch):
    use(monkeypatch, "niri")
    cfg = home / ".config/niri/config.kdl"
    cfg.parent.mkdir(parents=True)
    orig = 'input {\n}\n\nbinds {\n    Mod+T { spawn "foot"; }\n}\n'
    cfg.write_text(orig)
    autostart.enable()
    autostart.keybind_add()
    text = cfg.read_text()
    assert f'spawn-at-startup "{EXE}"' in text
    assert "// >>> topopaper >>>" in text
    body = text[text.index("binds {"):]
    assert f'Mod+Shift+B {{ spawn "{CTL}" "search"; }}' in body.split("}\n}")[0] + "}"
    assert text.count("binds {") == 1
    autostart.keybind_add()                           # idempotent
    assert cfg.read_text() == text
    autostart.keybind_remove()
    autostart.disable()
    assert cfg.read_text() == orig


def test_niri_without_binds_block(home, monkeypatch):
    use(monkeypatch, "niri")
    cfg = home / ".config/niri/config.kdl"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("input {\n}\n")
    autostart.keybind_add("Ctrl+Alt+M")
    assert 'binds {\n    Ctrl+Alt+M { spawn' in cfg.read_text()
    autostart.keybind_remove()
    assert cfg.read_text() == "input {\n}\n"


def test_kde(home, monkeypatch):
    use(monkeypatch, "kde")
    ok, _ = autostart.enable()
    f = home / ".config/autostart/topopaper.desktop"
    assert ok and f"Exec={EXE}" in f.read_text()
    assert autostart.status() == {"enabled": True, "method": "xdg-autostart", "detail": str(f)}
    ok, msg = autostart.keybind_add()
    assert not ok and "System Settings" in msg
    autostart.disable()
    assert not f.exists()


def test_systemd(home, monkeypatch):
    use(monkeypatch, "other")
    ok, msg = autostart.enable()
    unit = home / ".config/systemd/user/topopaper.service"
    assert ok and "graphical-session.target" in msg
    assert f"ExecStart={EXE}" in unit.read_text()
    assert ("enable", "topopaper.service") in SYSTEMCTL
    autostart.disable()
    assert not unit.exists() and ("disable", "topopaper.service") in SYSTEMCTL


def test_key_conversion():
    assert autostart.sway_bind("Super+Shift+B", "x") == "bindsym Mod4+Shift+b exec x"
    assert autostart.hypr_bind("ctrl+alt+F5", "x") == "bind = CTRL ALT, F5, exec, x"
    assert autostart.niri_bind("Super+Shift+B", ["a", "b"]) == 'Mod+Shift+B { spawn "a" "b"; }'
    with pytest.raises(ValueError):
        autostart.parse_keys("Hyper+B")


def test_compositor_detection(monkeypatch):
    monkeypatch.delenv("TOPOPAPER_COMPOSITOR", raising=False)
    assert autostart.compositor({"XDG_CURRENT_DESKTOP": "sway"}) == "sway"
    assert autostart.compositor({"XDG_CURRENT_DESKTOP": "Hyprland"}) == "hyprland"
    assert autostart.compositor({"XDG_CURRENT_DESKTOP": "niri"}) == "niri"
    assert autostart.compositor({"XDG_CURRENT_DESKTOP": "KDE"}) == "kde"
    assert autostart.compositor({"XDG_CURRENT_DESKTOP": "river"}) == "other"


def test_niri_refuses_duplicate_binding(home, monkeypatch):
    use(monkeypatch, "niri")
    cfg = home / ".config/niri/config.kdl"
    cfg.parent.mkdir(parents=True)
    orig = 'binds {\n    Mod+Shift+B { spawn "x"; }\n}\n'
    cfg.write_text(orig)
    ok, msg = autostart.keybind_add()
    assert not ok and "already bound" in msg and cfg.read_text() == orig
