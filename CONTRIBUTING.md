# Contributing

Bug reports, maps that look wrong, and pull requests are all welcome.

## Running from a checkout

```sh
make                         # builds build/topopaper (Linux, macOS)
make windows                 # cross-builds build/windows/topopaper.exe (mingw-w64)
bin/topopaper-ctl status     # the launchers in bin/ run the checkout's Python
bin/topopaper-settings
```

[docs/PLATFORMS.md](docs/PLATFORMS.md) has the per-system details, including
running the Windows build under Wine.

Nothing needs installing to try changes. To keep a development copy away from
your real maps and settings, point it at its own directory:

```sh
export TOPOPAPER_DIR=$PWD/dev TOPOPAPER_CONFIG=$PWD/dev/config.ini
bin/topopaper-ctl starter          # or: bin/topopaper-ctl build --earth
bin/topopaper-session              # engine + watcher + weather, Ctrl+C to stop
```

`TOPOPAPER_DIR` puts packs, the `area` file and logs in one directory.

## Checks

```sh
make check                   # unit tests (pytest) + byte-compile
ruff check topopaper tests
shellcheck install.sh uninstall.sh bin/topopaper-* tests/*.sh
python3 -m topopaper.themes | diff engine/themes.h -   # after editing themes.py
tests/render-smoke.sh DATA_DIR OUT_DIR [PACK]          # headless sway + screenshot
```

CI runs all of these, plus `install.sh` on Arch, Debian, Ubuntu and Fedora
containers, and builds, installs and renders on Windows and macOS runners.

## Where things are

- `engine/topopaper.c`: the renderer. All motion is integrated phases, never
  time × speed. The comments explain the camera, the scale ladder, and why
  each blend works the way it does. Read those before changing any of it.
  `engine/platform.h` is its interface to the per-OS backends.
- `topopaper/build/area.py`: builds one map ("area pack"). The file formats
  are documented at the top.
- `topopaper/build/route.py`: links a map to the globe with intermediate rungs.
- `topopaper/config.py`: every setting, with its default. The engine reads
  the same file (`cfg_set` in topopaper.c).
- `topopaper/themes.py`: colour themes. Regenerate `engine/themes.h` after
  editing it.
- `docs/ARCHITECTURE.md`: how the pieces talk to each other.

## Style

Match the code around you: small functions, comments that explain *why*,
stdlib first in Python, no new runtime dependencies without a good reason.
Keep commits focused.

By contributing you agree that your work is licensed under the GPL-3.0-or-later.
