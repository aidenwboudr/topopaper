import sys


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    try:
        from .app import run
    except (ImportError, ValueError) as e:
        print("topopaper-settings needs GTK 4 and libadwaita for Python (PyGObject).\n"
              "Install e.g. python-gobject + gtk4 + libadwaita (Arch), "
              "python3-gi + gir1.2-adw-1 (Debian/Ubuntu), "
              "python3-gobject + libadwaita (Fedora).\n"
              f"({e})", file=sys.stderr)
        return 1
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
