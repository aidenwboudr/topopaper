r"""Where topopaper keeps things.

Installed layout on Linux and macOS (XDG):
    $PREFIX/share/topopaper/      code + read-only data (hud.bin, lights.bin, fonts)
    $XDG_CONFIG_HOME/topopaper/   config.ini
    $XDG_DATA_HOME/topopaper/     area packs (areas/), user-built lights.bin, venv
    $XDG_STATE_HOME/topopaper/    the `area` file (which pack is on screen)
    $XDG_CACHE_HOME/topopaper/    tile caches, weather.txt, location.json
    $XDG_RUNTIME_DIR              covered flag, build progress, session lock, snapshots
                                  (macOS without it: ~/Library/Caches/topopaper/run)

On Windows:
    %LOCALAPPDATA%\Programs\topopaper\   bin\ (engine, launchers) + share\topopaper\
    %APPDATA%\topopaper\                  config.ini
    %LOCALAPPDATA%\topopaper\             areas\, venv\, and state\ cache\ run\ below it

Overrides (the engine honours the same ones, see engine/topopaper.c):
    TOPOPAPER_DIR      single-directory mode: packs and state live here
    TOPOPAPER_DATA     data dir only
    TOPOPAPER_SHARE    read-only data dir
    TOPOPAPER_CONFIG   path of config.ini
"""
import os
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent
WINDOWS = sys.platform == "win32"


def _xdg(var, fallback):
    v = os.environ.get(var)
    return Path(v) if v and os.path.isabs(v) else Path.home() / fallback


def _local():
    """Windows: %LOCALAPPDATA%\\topopaper."""
    v = os.environ.get("LOCALAPPDATA")
    return (Path(v) if v else Path.home() / "AppData" / "Local") / "topopaper"


def share_dir() -> Path:
    v = os.environ.get("TOPOPAPER_SHARE")
    if v:
        return Path(v)
    # source checkout: <repo>/topopaper + <repo>/data
    # installed:       $PREFIX/share/topopaper/topopaper + data files beside it
    root = PKG.parent
    return root / "data" if (root / "data" / "hud.bin").exists() else root


def data_dir() -> Path:
    v = os.environ.get("TOPOPAPER_DIR") or os.environ.get("TOPOPAPER_DATA")
    if v:
        return Path(v)
    return _local() if WINDOWS else _xdg("XDG_DATA_HOME", ".local/share") / "topopaper"


def state_dir() -> Path:
    v = os.environ.get("TOPOPAPER_DIR")
    if v:
        return Path(v)
    return _local() / "state" if WINDOWS else _xdg("XDG_STATE_HOME", ".local/state") / "topopaper"


def cache_dir() -> Path:
    return _local() / "cache" if WINDOWS else _xdg("XDG_CACHE_HOME", ".cache") / "topopaper"


def config_file() -> Path:
    v = os.environ.get("TOPOPAPER_CONFIG")
    if v:
        return Path(v)
    if WINDOWS:
        ra = os.environ.get("APPDATA")
        return (Path(ra) if ra else _local()) / "topopaper" / "config.ini"
    return _xdg("XDG_CONFIG_HOME", ".config") / "topopaper" / "config.ini"


def runtime_dir() -> Path:
    if WINDOWS:
        return _local() / "run"
    v = os.environ.get("XDG_RUNTIME_DIR")
    if not v and sys.platform == "darwin":
        # per user, and the same for a LaunchAgent and a Terminal ($TMPDIR may differ)
        return Path.home() / "Library" / "Caches" / "topopaper" / "run"
    return Path(v or "/tmp")


def areas_dir() -> Path:
    return data_dir() / "areas"


def area_file() -> Path:
    return state_dir() / "area"


def hud_file() -> Path:
    return share_dir() / "hud.bin"


def lights_file() -> Path:
    """User-rebuilt lights win over the shipped copy."""
    own = data_dir() / "lights.bin"
    return own if own.exists() else share_dir() / "lights.bin"


def font_file() -> Path:
    return share_dir() / "fonts" / "JetBrainsMono-Bold.ttf"


def covered_file() -> Path:
    return runtime_dir() / "topopaper-covered"


def progress_file() -> Path:
    return runtime_dir() / "topopaper-progress.bin"


def snapshot_request_file() -> Path:
    return runtime_dir() / "topopaper-snapshot-request"


def snapshot_file() -> Path:
    return runtime_dir() / "topopaper-snapshot.ppm"


def default_weather_file() -> Path:
    return cache_dir() / "weather.txt"


def default_location_file() -> Path:
    return cache_dir() / "location.json"


def log_dir() -> Path:
    return state_dir() / "logs"


def venv_python() -> Path:
    if WINDOWS:
        return data_dir() / "venv" / "Scripts" / "python.exe"
    return data_dir() / "venv" / "bin" / "python"
