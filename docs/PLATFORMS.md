# Platforms

topopaper runs on Linux (Wayland), Windows 10/11 and macOS 10.15+. The engine
core (`engine/topopaper.c`) is the same everywhere; each OS has a small
backend that makes the wallpaper surfaces and runs the main loop
(`engine/platform.h` describes the seam). The Python side is shared, with the
OS differences kept in `topopaper/system.py`, `paths.py` and `autostart.py`.

| | Linux | Windows | macOS |
|---|---|---|---|
| backend | `wayland.c`: wlr-layer-shell, EGL + GLES2 | `win32.c`: child windows in the desktop's wallpaper layer, WGL (OpenGL 2.0+) | `macos.m`: desktop-level windows, NSOpenGL 2.1 |
| behind the desktop icons | n/a | yes (see 24H2 below) | yes (Finder icons stay above) |
| pauses when covered | watcher per compositor (`topopaper.watch`) | engine: a window maximized or covering 95% of a monitor's work area, on every monitor | engine: window occlusion state |
| battery frame rate | sysfs | `GetSystemPowerStatus` | IOKit power sources |
| starts at login | compositor config / systemd unit | registry `HKCU\…\Run` | LaunchAgent |
| search shortcut | compositor binding | the session holds a global hotkey (`search_hotkey`) | not automatic: a Shortcuts app shortcut running `topopaper-ctl search` |
| settings window | GTK 4 + libadwaita (Tk if missing) | Tk | Tk |
| search box | rofi / fuzzel / wofi / tofi | Tk window | Tk window |
| install | `install.sh` (builds from source) | `install.ps1` (prebuilt zip from the release) | `install.sh` (builds with the Xcode command line tools) |

## Windows

The engine sends Explorer's `Progman` window the undocumented `0x052C`
message, which splits the desktop into the icon layer (`SHELLDLL_DefView`)
and a `WorkerW` that draws the wallpaper, then parents one window per monitor
into that layer. That's the recipe Lively Wallpaper and others use. Windows 11
24H2 changed the layout: the `WorkerW` became a child of `Progman`, and
Microsoft's guidance is a layered child window right under the icons. The
engine detects which layout it has (`WS_EX_NOREDIRECTIONBITMAP` on `Progman`)
and re-attaches when Explorer restarts (`TaskbarCreated`), when monitors
change, or when the `WorkerW` is replaced. Without an Explorer desktop
(Wine, some kiosk setups) it falls back to borderless windows pinned to the
bottom of the z-order.

On the 24H2 layout the desktop can accept the windows without DWM ever
showing them. The Windows Server 2025 runner (build 26100) does this whether
the frame comes from GL, GDI or `UpdateLayeredWindow`, and Lively Wallpaper
users report the same on some 24H2 and 25H2 builds. So a few seconds after
attaching, the engine compares points of the screen that no app window
covers with its own frame (`the desktop shows N of M sampled points` in
`engine.log`). If the desktop shows none of it, the engine switches to the
bottom-window mode. That mode is visible under every app, but it hides the
desktop icons. `TOPA_WIN_ATTACH=window` forces it, and `TOPA_WIN_DEBUG=1`
logs the desktop's window tree.

Files: the program lives in `%LOCALAPPDATA%\Programs\topopaper` (`bin\` +
`share\topopaper\`, the same shape as a Unix prefix), maps and state in
`%LOCALAPPDATA%\topopaper`, `config.ini` in `%APPDATA%\topopaper`. The map
builder runs in a venv in `%LOCALAPPDATA%\topopaper\venv`; a `topopaper.pth`
in it makes the installed package importable from Start menu shortcuts and
the sign-in entry.

There are no signals between processes on Windows, so `topopaper-ctl stop`
and `restart` leave a request file in the run folder that the session polls,
and the session stops the engine with a `WM_CLOSE` to its hidden controller
window (the engine then hands the desktop back).

The clock: Windows' C runtime knows no IANA zones, so the engine uses the UTC
offset the weather fetcher writes into `location.json` (refreshed every 15
minutes), or the system zone before the first fetch.

Build: `make windows` cross-compiles `build/windows/topopaper.exe` with
mingw-w64 (Debian/Ubuntu: `gcc-mingw-w64-x86-64`; Arch: `mingw-w64-gcc`; or
natively in MSYS2 UCRT64). `make dist-windows` makes the release zip.
Pushing a `v*` tag attaches it to the GitHub release (`.github/workflows/release.yml`).
Under Wine the engine draws in its fallback mode, which is handy for testing
the GL path on Linux:

```sh
TOPOPAPER_DATA='Z:\path\to\data' TOPA_SHOT='Z:\tmp\frame.ppm' wine build/windows/topopaper.exe
```

## macOS

One borderless window per screen at the desktop window level plus one: above
the system wallpaper, below the Finder's desktop icons (+20) and every app.
It joins every Space, stays put in Mission Control, ignores the mouse, can't
be hidden or activated, and the app has no Dock icon. Screen changes, wake
and unlock rebuild the windows. OpenGL is deprecated on macOS but still
shipped (macOS 26 removed only AGL); the engine uses a legacy 2.1 context,
which also works on the software renderer of a GPU-less VM.

Files follow the XDG layout like Linux (`~/.config/topopaper`,
`~/.local/share/topopaper`, …), with the run folder in
`~/Library/Caches/topopaper/run` when `$XDG_RUNTIME_DIR` isn't set.

## Checking a platform

`TOPA_SHOT=frame.ppm` makes the engine save the first display's frame after
`TOPA_SHOT_T` seconds (default 8) and exit; `tests/ppm2png.py` converts it
and fails on a blank frame. CI does this on Linux (headless sway), Windows
(2022 and 2025 runners, with Mesa's llvmpipe since they have no GPU) and
macOS, runs the installers end to end, and screenshots the real desktop with
the wallpaper session running.
