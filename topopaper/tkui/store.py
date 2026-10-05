"""The one way the Tk windows write config.ini (the Tk twin of settings/store.py).

Each write re-reads the file first, so a hand edit made while the window is
open survives; sliders pass debounce=True and are coalesced into one save
~250 ms after the last change. The timer comes from the caller (Tk's
`after`/`after_cancel`), so this module itself never imports tkinter and
tests can drive it with a fake clock.
"""
import os

from .. import config


class Store:
    DEBOUNCE_MS = 250

    def __init__(self, path=None, after=None, cancel=None):
        """after(ms, fn) -> handle and cancel(handle) schedule the debounced
        save; without them a debounced write is saved at once."""
        self.path = path
        self.cfg = config.Config(path)
        self._after, self._cancel = after, cancel
        self._pending = {}
        self._timer = None
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
        """fn(changed_keys) runs after the file changed underneath us (reload())."""
        self._listeners.append(fn)

    def reload(self):
        """Pick up external edits. Returns the set of keys that changed."""
        self.flush()
        fresh = config.Config(self.path)
        changed = {k for k in fresh.values if fresh.values[k] != self.cfg.values.get(k)}
        if not changed:
            return changed
        self.cfg = fresh
        for fn in list(self._listeners):
            fn(changed)
        return changed

    # ---- writing -----------------------------------------------------------
    def set(self, name, value, debounce=False):
        self.set_many({name: value}, debounce)

    def set_many(self, values, debounce=False):
        for k, v in values.items():
            self.cfg.set(k, v)            # reads see the new value at once
            self._pending[k] = self.cfg.values[k]
        if debounce and self._after:
            self._stop_timer()
            self._timer = self._after(self.DEBOUNCE_MS, self._on_timer)
        else:
            self.flush()

    @property
    def pending(self):
        return dict(self._pending)

    def _on_timer(self):
        self._timer = None
        self.flush()

    def _stop_timer(self):
        if self._timer is not None and self._cancel:
            try:
                self._cancel(self._timer)
            except Exception:        # noqa: BLE001 - the window may be gone already
                pass
        self._timer = None

    def flush(self):
        """Save pending changes now. Returns False when the file couldn't be written."""
        self._stop_timer()
        if not self._pending:
            return True
        fresh = config.Config(self.path)
        for k, v in self._pending.items():
            fresh.set(k, v)
        self._pending.clear()
        try:
            fresh.save()
        except OSError as e:
            print(f"topopaper-settings: could not save {fresh.path}: {e}")
            return False
        self.cfg = fresh
        return True

    def save_all(self):
        """Write every value (creates config.ini on first run)."""
        self.flush()
        fresh = config.Config(self.path)
        fresh.values.update(self.cfg.values)
        fresh.save()
        self.cfg = fresh
