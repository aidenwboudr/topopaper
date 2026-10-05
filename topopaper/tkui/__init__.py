"""The settings window and place picker in Tkinter, for Windows and macOS.

    python -m topopaper.tkui.settings [--page places|appearance|motion|clock|general|about]
    python -m topopaper.tkui.picker

The GTK 4 window in topopaper/settings/ is the Linux one; this package is
its stdlib-only twin for desktops where GTK isn't at hand. Pure logic is
shared with it through topopaper.settings.logic and .optional (neither
imports Gtk). Tk 8.5 to 9.0, Python 3.9+.
"""
