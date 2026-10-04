# How topopaper fits together

```
                 config.ini  (topopaper/config.py: schema, defaults, docs)
                     │  re-read on mtime change
   ┌─────────────────┼──────────────────────────────────────────────┐
   │                 ▼                                              │
   │  topopaper (C engine, engine/topopaper.c)                      │
   │    layer-shell surface per output, GLES2, draws packs + HUD    │
   │    polls (~2.5×/s): area file, config.ini, covered flag,       │
   │    battery, build progress; (30 s) weather.txt; (20 s) tz      │
   └──────────▲──────────────▲───────────────▲──────────────────────┘
              │ area file    │ covered flag  │ weather.txt / location.json
   topopaper-ctl fly/search  │               │
   settings app        topopaper-session: supervises the engine,
                       runs the compositor watcher + weather refresh
```

The engine never touches the network and never forks. Everything else is
Python in the `topopaper` package, reached through three launchers in `bin/`:

| launcher | module | what |
|---|---|---|
| `topopaper-ctl` | `topopaper.cli` | every command (fly, search, build, doctor, …) |
| `topopaper-session` | `topopaper.session` | what autostart runs: engine + helpers |
| `topopaper-settings` | `topopaper.settings` | GTK 4 + libadwaita settings window |

## Files

| what | where (`topopaper/paths.py`) |
|---|---|
| settings | `$XDG_CONFIG_HOME/topopaper/config.ini` |
| area packs | `$XDG_DATA_HOME/topopaper/areas/<name>/` |
| Python venv (install.sh) | `$XDG_DATA_HOME/topopaper/venv/` |
| pack on screen | `$XDG_STATE_HOME/topopaper/area` (one word) |
| build logs | `$XDG_STATE_HOME/topopaper/logs/<slug>.log` |
| tile caches | `$XDG_CACHE_HOME/topopaper/{terrarium,copernicus}/` |
| HUD inputs | `$XDG_CACHE_HOME/topopaper/weather.txt`, `location.json` |
| covered flag | `$XDG_RUNTIME_DIR/topopaper-covered` (`1`/`0`) |
| build progress | `$XDG_RUNTIME_DIR/topopaper-progress.bin` |
| build jobs | `$XDG_RUNTIME_DIR/topopaper-jobs/<slug>.json` |
| shipped data | `$PREFIX/share/topopaper/` (`hud.bin`, `lights.bin`, `fonts/`) |

`TOPOPAPER_DIR=<dir>` puts packs and state in one directory (handy for
development). `TOPOPAPER_DATA`, `TOPOPAPER_SHARE` and `TOPOPAPER_CONFIG`
override single locations. The engine applies the same rules.

## Contracts between the pieces

**area file** — the pack name on screen. Anyone may write it (atomically);
the engine flies there. The engine writes it too when it auto-roams.

**weather.txt** — `key=value` lines, rewritten atomically:

| key | meaning |
|---|---|
| `ts` | unix time of the fetch; the HUD hides weather when it is >2 h old |
| `temp` | integer, already in the user's units |
| `cond` | short condition text, e.g. `PARTLY CLOUDY` |
| `news` | optional heads-up line, e.g. `FROST TONIGHT`; a leading `@` plus `neta` makes a live countdown (`@RAIN` → `RAIN IN 12 MIN`) |
| `neta` | unix time the `@` event starts |
| `apack`, `afrz` | pack name and its freezing level in metres (draws the snowline) |

**location.json** — `{"tz": "Area/City", ...}`; the clock follows `tz`.

**covered flag** — `1` while windows cover the wallpaper on every visible
workspace, else `0`. Written by `topopaper.watch` (one backend per compositor).

## Area packs

A pack is one map at one scale (`topopaper/build/area.py` documents the file
formats). Packs nest on a ladder of tiers (tier K ⇒ map height `4^-K` of the
world). `earth` (K0) is the top of every chain: `topopaper-ctl route PACK`
adds intermediate "scaffold" rungs until a pack's chain closes against it, so
any pack anywhere zooms smoothly out to the globe.
