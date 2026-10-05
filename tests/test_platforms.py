"""The Windows and macOS pieces that run anywhere: folder layout, the search
hotkey, autostart commands, process helpers."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from topopaper import autostart, hotkey, paths, session, system  # noqa: E402


@pytest.fixture
def windows(tmp_path, monkeypatch):
    for v in ("TOPOPAPER_DIR", "TOPOPAPER_DATA", "TOPOPAPER_CONFIG"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(paths, "WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    return tmp_path


def test_windows_layout(windows):
    local = windows / "Local" / "topopaper"
    assert paths.config_file() == windows / "Roaming" / "topopaper" / "config.ini"
    assert paths.areas_dir() == local / "areas"
    assert paths.area_file() == local / "state" / "area"
    assert paths.cache_dir() == local / "cache"
    assert paths.runtime_dir() == local / "run"
    assert paths.log_dir() == local / "state" / "logs"
    assert paths.venv_python() == local / "venv" / "Scripts" / "python.exe"


def test_windows_overrides_still_win(windows, monkeypatch):
    monkeypatch.setenv("TOPOPAPER_DIR", str(windows / "dev"))
    assert paths.areas_dir() == windows / "dev" / "areas"
    assert paths.area_file() == windows / "dev" / "area"


def test_macos_runtime_dir_is_per_user(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(paths, "WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.runtime_dir() == tmp_path / "Library" / "Caches" / "topopaper" / "run"


def test_hotkey_combos():
    assert hotkey.combo("Super+Shift+B") == (0x4000 | 0x8 | 0x4, ord("B"))
    assert hotkey.combo("ctrl+alt+F5") == (0x4000 | 0x2 | 0x1, 0x74)
    assert hotkey.combo("Win+space")[1] == 0x20
    with pytest.raises(ValueError):
        hotkey.combo("Super+Shift+Pause")
    with pytest.raises(ValueError):
        hotkey.combo("Hyper+B")


def test_windows_keybind_is_a_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOPAPER_CONFIG", str(tmp_path / "config.ini"))
    monkeypatch.setenv("TOPOPAPER_COMPOSITOR", "windows")
    assert not autostart.keybind_status()["enabled"]
    ok, msg = autostart.keybind_add()
    assert ok and autostart.keybind_status()["line"] == "Win+Shift+B" and "Win+Shift+B" in msg
    ok, _ = autostart.keybind_add("Super+Shift+Pause")      # can't be registered
    assert not ok and autostart.keybind_status()["line"] == "Win+Shift+B"
    assert autostart.keybind_remove()[0]
    assert not autostart.keybind_status()["enabled"]


def test_macos_keybind_explains_shortcuts(monkeypatch):
    monkeypatch.setenv("TOPOPAPER_COMPOSITOR", "macos")
    st = autostart.keybind_status()
    assert not st["supported"] and "Shortcuts" in st["detail"]
    ok, msg = autostart.keybind_add()
    assert not ok and "Shortcuts" in msg


def test_macos_launch_agent(tmp_path, monkeypatch):
    import plistlib
    monkeypatch.setenv("TOPOPAPER_COMPOSITOR", "macos")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(autostart, "launcher", lambda n: f"/u/.local/bin/{n}")
    assert not autostart.status()["enabled"]
    assert autostart.enable()[0]
    agent = plistlib.loads(autostart.launch_agent().read_bytes())
    assert agent["ProgramArguments"] == ["/u/.local/bin/topopaper-session"]
    assert agent["RunAtLoad"] and autostart.status()["enabled"]
    assert autostart.disable()[0] and not autostart.launch_agent().exists()


def test_windows_session_command_finds_the_package():
    cmd = autostart.windows_session_cmd()
    assert repr(str(paths.PKG.parent)) in cmd and "topopaper.session" in cmd


def test_pid_alive():
    assert system.pid_alive(os.getpid())
    assert not system.pid_alive(2 ** 30)
    assert not system.pid_alive("nope") and not system.pid_alive(None)


def test_lock_is_exclusive(tmp_path):
    a = open(tmp_path / "lock", "a")
    b = open(tmp_path / "lock", "a")
    try:
        assert system.try_lock(a)
        if sys.platform != "win32":         # msvcrt locks are per process
            assert not system.try_lock(b)
        system.unlock(a)
        assert system.try_lock(b)
    finally:
        a.close()
        b.close()


def test_engine_found_in_a_checkout_build(tmp_path, monkeypatch):
    monkeypatch.delenv("TOPOPAPER_ENGINE", raising=False)
    monkeypatch.setenv("TOPOPAPER_BIN", str(tmp_path))
    exe = tmp_path / system.engine_name()
    exe.write_bytes(b"")
    exe.chmod(0o755)
    assert session.find_engine() == str(exe)
