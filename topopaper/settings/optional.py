"""Lazy access to helper modules that may be missing or broken.

The settings window must open even when part of topopaper fails to import
(an older install, a missing dependency). Every control that needs one of
these modules asks here first and shows itself disabled when the answer is
None.
"""
import importlib

# module -> the attributes the settings app calls
API = {
    "session": ("is_running", "start_detached", "restart", "stop"),
    "autostart": ("status", "enable", "disable", "keybind_status", "keybind_add",
                  "keybind_remove"),
    "doctor": ("run_checks",),
    "weather": ("refresh",),
    "starter": ("main",),
}

HINT = {
    "session": "needs topopaper-session",
    "autostart": "needs the autostart helper (topopaper.autostart)",
    "doctor": "needs the setup checker (topopaper.doctor)",
    "weather": "needs the weather helper (topopaper.weather)",
    "starter": "needs the globe downloader (topopaper.starter)",
}

_cache = {}


def get(name):
    """The module if it imports and has its agreed API, else None."""
    if name not in _cache:
        mod = None
        try:
            m = importlib.import_module(f"topopaper.{name}")
            if all(callable(getattr(m, a, None)) for a in API.get(name, ())):
                mod = m
        except Exception:      # noqa: BLE001 - any import failure means "missing"
            mod = None
        _cache[name] = mod
    return _cache[name]


def has(name, attr=None):
    m = get(name)
    return m is not None and (attr is None or callable(getattr(m, attr, None)))


def hint(name):
    return HINT.get(name, f"needs topopaper.{name}")
