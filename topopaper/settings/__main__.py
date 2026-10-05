import sys


def main(argv=None):
    """The GTK 4 + libadwaita window on Linux; the Tk one on Windows and
    macOS, and on Linux when PyGObject is missing."""
    argv = list(sys.argv if argv is None else argv)
    gtk_error = None
    if sys.platform.startswith("linux"):
        try:
            from .app import run
        except (ImportError, ValueError) as e:
            gtk_error = e
        else:
            return run(argv)
    try:
        from ..tkui import settings
    except ImportError as e:
        print("topopaper-settings needs GTK 4 and libadwaita for Python (PyGObject), or Tk.\n"
              "Install e.g. python-gobject + gtk4 + libadwaita (Arch), "
              "python3-gi + gir1.2-adw-1 (Debian/Ubuntu), "
              "python3-gobject + libadwaita (Fedora).\n"
              f"({gtk_error or e})", file=sys.stderr)
        return 1
    if gtk_error and sys.stderr is not None:
        print(f"topopaper-settings: GTK unavailable ({gtk_error}); using the Tk window",
              file=sys.stderr)
    return settings.main(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
