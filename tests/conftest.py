"""Keep every test out of the real profile: on Windows topopaper's folders
come from %APPDATA% / %LOCALAPPDATA% (paths.py), not $HOME, so those point
into a temporary home too."""
import pytest


@pytest.fixture(autouse=True)
def _windows_profile(tmp_path_factory, monkeypatch):
    h = tmp_path_factory.mktemp("profile")
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("APPDATA", str(h / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(h / "AppData" / "Local"))
