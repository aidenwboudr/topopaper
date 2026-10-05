"""Windows: a global shortcut for the place search, held by the session.

Windows has no compositor config to put a binding in, so the session
registers `search_hotkey` (e.g. "Super+Shift+B") with RegisterHotKey on a
thread of its own and runs the search whenever it fires.
"""
import ctypes
import logging
import threading
from ctypes import wintypes

from .autostart import parse_keys

log = logging.getLogger("topopaper.session")

MOD = {"super": 0x0008, "mod4": 0x0008, "win": 0x0008, "meta": 0x0008, "mod": 0x0008,
       "logo": 0x0008, "shift": 0x0004, "ctrl": 0x0002, "control": 0x0002,
       "alt": 0x0001, "mod1": 0x0001}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012


def virtual_key(key):
    """'B' -> 0x42, 'F5' -> 0x74, 'space' -> 0x20. ValueError if unknown."""
    k = key.strip()
    if len(k) == 1 and k.isalnum():
        return ord(k.upper())
    if k.upper().startswith("F") and k[1:].isdigit() and 1 <= int(k[1:]) <= 24:
        return 0x6F + int(k[1:])
    named = {"space": 0x20, "return": 0x0D, "enter": 0x0D, "slash": 0xBF, "period": 0xBE,
             "comma": 0xBC, "minus": 0xBD, "equal": 0xBB, "tab": 0x09}
    if k.lower() in named:
        return named[k.lower()]
    raise ValueError(f"unsupported key {key!r}")


def combo(keys):
    """'Super+Shift+B' -> (modifier flags, virtual key)."""
    mods, key = parse_keys(keys)
    flags = MOD_NOREPEAT
    for m in mods:
        if m not in MOD:
            raise ValueError(f"unknown modifier {m!r}")
        flags |= MOD[m]
    return flags, virtual_key(key)


def start(keys, on_press):
    """A daemon thread holding the hotkey; on_press() runs on each press."""
    ready = threading.Event()

    def run():
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        t.native = ctypes.windll.kernel32.GetCurrentThreadId()
        try:
            flags, vk = combo(keys)
        except ValueError as e:
            log.info("search shortcut %r: %s", keys, e)
            ready.set()
            return
        if not user32.RegisterHotKey(None, 1, flags, vk):
            log.info("search shortcut %s is taken by another program", keys)
            ready.set()
            return
        log.info("search shortcut %s registered", keys)
        ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                try:
                    on_press()
                except Exception as e:      # noqa: BLE001 - never lose the shortcut
                    log.info("search shortcut: %r", e)
        user32.UnregisterHotKey(None, 1)

    t = threading.Thread(target=run, name="hotkey", daemon=True)
    t.native = 0
    t.start()
    ready.wait(2.0)
    return t


def stop(t):
    if t.is_alive() and t.native:
        ctypes.windll.user32.PostThreadMessageW(t.native, WM_QUIT, 0, 0)
        t.join(2.0)
