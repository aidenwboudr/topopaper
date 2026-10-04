"""The one way the settings window writes config.ini.

Each write re-reads the file first, so a hand edit made while the window is
open survives; slider-like controls pass debounce=True and are coalesced
into one save ~250 ms after the last change. The engine notices the new
mtime and applies the change live.
"""
import os

from gi.repository import GLib

from .. import config


class Store:
    DEBOUNCE_MS = 250

    def __init__(self, path=None):
        self.path = path
        self.cfg = config.Config(path)
        self._pending = {}
        self._timer = 0
        self._listeners = []

    # ---- reading -----------------------------------------------------------
    @property
    def file(self):
        return self.cfg.path

    def exists(self):
        return os.path.exists(self.cfg.path)

    def get(self, name):
        return self.cfg.get(name)

    def raw(self, name):
        return self.cfg.values.get(name, config.BY_NAME[name].default)

    def connect(self, fn):
        """fn() runs after the file changed underneath us (reload())."""
        self._listeners.append(fn)

    def reload(self):
        """Pick up external edits. Returns True (and notifies) if anything changed."""
        self.flush()
        fresh = config.Config(self.path)
        if fresh.values == self.cfg.values:
            return False
        self.cfg = fresh
        for fn in list(self._listeners):
            fn()
        return True

    # ---- writing -----------------------------------------------------------
    def set(self, name, value, debounce=False):
        self.set_many({name: value}, debounce)

    def set_many(self, values, debounce=False):
        for k, v in values.items():
            self.cfg.set(k, v)            # reads see the new value at once
            self._pending[k] = v
        if debounce:
            if self._timer:
                GLib.source_remove(self._timer)
            self._timer = GLib.timeout_add(self.DEBOUNCE_MS, self._on_timer)
        else:
            self.flush()

    def _on_timer(self):
        self._timer = 0
        self.flush()
        return False

    def flush(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if not self._pending:
            return
        fresh = config.Config(self.path)
        for k, v in self._pending.items():
            fresh.set(k, v)
        self._pending.clear()
        try:
            fresh.save()
        except OSError as e:
            print(f"topopaper-settings: could not save {fresh.path}: {e}")
            return
        self.cfg = fresh

    def save_all(self):
        """Write every value (creates config.ini on first run)."""
        self.flush()
        fresh = config.Config(self.path)
        fresh.values.update(self.cfg.values)
        fresh.save()
        self.cfg = fresh
