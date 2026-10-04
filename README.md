# topopaper

A live topographic wallpaper for Wayland. Your desktop becomes a slowly
breathing contour map of real terrain — from a globe lit by the actual sun,
down through mountain ranges and valleys, to the runs and lifts of a single
ski resort — with a quiet clock and the weather in the corner.

![The globe at night, and a ski resort's contour map](docs/screenshots/hero.png)

- **Real terrain anywhere on Earth.** Type a place ("Zermatt", "Yosemite",
  "Hokkaido") and topopaper builds a map of it from open elevation and
  OpenStreetMap data, then flies there.
- **One continuous atlas.** Every map is linked to the globe, so flights zoom
  smoothly from the planet down to a valley and back.
- **Alive, but calm.** Contours drift, the camera breathes, the day/night line
  and aurora follow the real sun, and the wallpaper freezes when windows
  cover it so it costs nothing while you work.
- **Clock and weather** (Open-Meteo, no account needed), themes, multi-monitor,
  and a settings app.

## Requirements

- A Wayland compositor that supports **wlr-layer-shell**: Sway/SwayFX,
  Hyprland, niri, river, Wayfire, KDE Plasma 6, and most wlroots-based
  compositors. **GNOME is not supported** (it has no layer-shell).
- OpenGL ES 2 (any GPU driver, or Mesa's software renderer).
- Linux with Python 3, GTK 4 and libadwaita (the installer handles this on
  Arch, Debian/Ubuntu, Fedora, openSUSE, Void and Alpine).

## Install

```sh
git clone https://github.com/aidenwboudr/topopaper
cd topopaper
./install.sh
```

or in one line:

```sh
curl -fsSL https://raw.githubusercontent.com/aidenwboudr/topopaper/main/install.sh | bash
```

The installer checks your desktop, offers to install missing system packages
(it shows the exact command first), builds the wallpaper, installs it into
`~/.local`, downloads the starter globe (~30 MB), and asks whether to start at
login and whether to add a **Super+Shift+B** search shortcut. Nothing in your
compositor config changes without a yes.

Then open **Topopaper** from your app launcher (or run `topopaper-settings`)
and tell it where home is.

![The settings app](docs/screenshots/settings.png)

To remove it: `./uninstall.sh` (keeps your maps and settings) or
`./uninstall.sh --purge`.

## Using it

| | |
|---|---|
| **Settings** | `topopaper-settings` — maps, theme, clock and weather, motion, displays, autostart |
| **Go somewhere** | Super+Shift+B (if you added the shortcut), or `topopaper-ctl search` — pick a map or type any place |
| **Fly to a map** | `topopaper-ctl fly zermatt` (no name: the next map) |
| **Start / restart / stop** | `topopaper-ctl restart`, `topopaper-ctl stop` |
| **Something wrong?** | `topopaper-ctl doctor` checks the install and your desktop |
| **Everything else** | `topopaper-ctl --help` |

Building a new map takes a few minutes (the data comes from public servers);
the wallpaper keeps running and flies there when it's ready. Maps you searched
for are kept until you delete them in Settings → Places, or clean up old ones
with `topopaper-ctl gc`.

Settings live in `~/.config/topopaper/config.ini` — the settings app edits it,
and so can you; the wallpaper picks up changes within a second.

### Starting at login by hand

If you'd rather add it to your compositor config yourself, run
`topopaper-session` at startup:

| compositor | line |
|---|---|
| Sway | `exec topopaper-session` |
| Hyprland | `exec-once = topopaper-session` |
| niri | `spawn-at-startup "topopaper-session"` |
| river / Wayfire / others | run `topopaper-session` from your autostart |
| KDE Plasma | System Settings → Autostart → add `topopaper-session` |

## Privacy

topopaper talks to a few public services, only when it needs to:
elevation tiles (AWS Open Data), OpenStreetMap's Overpass and Nominatim
(when you build a map or search), Photon (spelling suggestions), Open-Meteo
(weather), and — only if you leave the weather location on *automatic* — an
IP-geolocation service. Automatic locations are rounded to about 11 km before
being used anywhere. Nothing else leaves your machine, and there is no
telemetry.

## Data sources and credits

Maps are built from open data. If you share screenshots or maps, please keep
the credits:

- Map data © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, available under the ODbL.
- Elevation: [AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/)
  (Mapzen Terrarium; SRTM, GMTED, ETOPO1, USGS NED and others) and the
  [Copernicus DEM GLO-30](https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model)
  (© DLR e.V. 2010–2014 and © Airbus Defence and Space GmbH 2014–2018,
  provided under COPERNICUS by the European Union and ESA).
- Lakes on the globe: [Natural Earth](https://www.naturalearthdata.com/) (public domain).
- Weather: [Open-Meteo](https://open-meteo.com/) (CC BY 4.0).
- Geocoding: [Nominatim](https://nominatim.org/) and [Photon](https://photon.komoot.io/).
- Font: [JetBrains Mono](https://www.jetbrains.com/lp/mono/) (SIL Open Font License 1.1).

Please be kind to the free public servers: topopaper rate-limits itself and
caches everything it downloads.

## How it works

A small C engine draws the map on a Wayland layer-shell surface with OpenGL
ES 2; everything else (map building, search, weather, the settings app) is
Python. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the pieces and
[CONTRIBUTING.md](CONTRIBUTING.md) to hack on it.

## License

topopaper is free software under the [GNU General Public License v3.0 or
later](LICENSE). The bundled JetBrains Mono font is under the SIL Open Font
License 1.1 (`data/fonts/OFL.txt`); the generated Wayland protocol files keep
their MIT-style licenses.
