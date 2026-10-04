"""topopaper-settings: the GTK 4 + libadwaita settings window.

    python -m topopaper.settings [--page places|appearance|motion|clock|displays|general|about|welcome]

Pages edit config.ini through store.Store; the engine applies changes live.
Pure logic lives in logic.py (importable without Gtk, for tests).
"""
