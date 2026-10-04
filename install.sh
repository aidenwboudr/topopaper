#!/usr/bin/env bash
# topopaper installer.
#
#   ./install.sh                      from a checkout: install for this user (~/.local)
#   curl -fsSL https://raw.githubusercontent.com/aidenwboudr/topopaper/main/install.sh | bash
#
# Options:
#   --prefix DIR       install under DIR (default ~/.local; /usr/local needs root)
#   --yes              answer yes to every question (packages, autostart, shortcut)
#   --no-deps          don't install system packages, just check them
#   --no-starter       don't download the starter globe now
#   --no-autostart     don't ask about starting at login / the search shortcut
#   --no-launch        don't start the wallpaper or open settings at the end
#   --ref REF          git branch/tag to install when run without a checkout
#
# It never edits your compositor config without asking, and everything it does
# can be undone with uninstall.sh (or `topopaper-ctl autostart disable`).
set -euo pipefail

REPO_URL="https://github.com/aidenwboudr/topopaper"
PREFIX="$HOME/.local"
YES=0 DEPS=1 STARTER=1 AUTOSTART=1 LAUNCH=1 REF="main"

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift ;;
        --prefix=*) PREFIX="${1#*=}" ;;
        --yes|-y) YES=1 ;;
        --no-deps) DEPS=0 ;;
        --no-starter) STARTER=0 ;;
        --no-autostart) AUTOSTART=0 ;;
        --no-launch) LAUNCH=0 ;;
        --ref) REF="$2"; shift ;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'
else B="" G="" Y="" R="" N=""; fi
say()  { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

ask() {   # ask "question" -> 0 for yes
    [ "$YES" = 1 ] && return 0
    [ -r /dev/tty ] || return 1
    local a
    printf '%s [y/N] ' "$1" > /dev/tty
    read -r a < /dev/tty || return 1
    case "$a" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

as_root() {
    if [ "$(id -u)" = 0 ]; then "$@"
    elif command -v sudo >/dev/null; then sudo "$@"
    elif command -v doas >/dev/null; then doas "$@"
    else die "need root to run: $* (install sudo, or run the command yourself)"; fi
}

# ---- 0. source ---------------------------------------------------------------
SRC=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/Makefile" ] \
   && [ -d "$(dirname "${BASH_SOURCE[0]}")/engine" ]; then
    SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

# ---- 1. desktop --------------------------------------------------------------
say "Checking your desktop"
desk="${XDG_CURRENT_DESKTOP:-}"
if [ -n "${SWAYSOCK:-}" ]; then ok "Sway"
elif [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; then ok "Hyprland"
elif [ -n "${NIRI_SOCKET:-}" ]; then ok "niri"
elif [[ "$desk" == *KDE* ]]; then ok "KDE Plasma"
elif [[ "$desk" == *GNOME* ]]; then
    warn "GNOME can't show topopaper: it doesn't support the layer-shell protocol."
    warn "Installing anyway (it works on Sway, Hyprland, niri, river, Wayfire, KDE Plasma)."
elif [ -n "${WAYLAND_DISPLAY:-}" ]; then ok "a Wayland session (${desk:-unknown compositor})"
else warn "not running inside a Wayland session right now; installing anyway"; fi

# ---- 2. system packages ------------------------------------------------------
say "Checking build and runtime dependencies"
# shellcheck disable=SC1091
. /etc/os-release 2>/dev/null || true
family=""
for id in ${ID:-} ${ID_LIKE:-}; do
    case "$id" in
        arch|manjaro|endeavouros|cachyos) family=arch; break ;;
        debian|ubuntu|linuxmint|pop) family=debian; break ;;
        fedora|rhel|centos) family=fedora; break ;;
        opensuse*|suse|sles) family=suse; break ;;
        void) family=void; break ;;
        alpine) family=alpine; break ;;
    esac
done

case "$family" in
    arch)   PKGS="base-devel git pkgconf wayland mesa python python-gobject gtk4 libadwaita python-numpy python-pillow libnotify zstd"
            INSTALL="pacman -S --needed --noconfirm" ;;
    debian) PKGS="build-essential git pkg-config libwayland-dev libegl-dev libgles-dev python3 python3-venv python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-numpy python3-pil libnotify-bin zstd"
            INSTALL="apt-get install -y" ;;
    fedora) PKGS="gcc make git pkgconf-pkg-config wayland-devel mesa-libEGL-devel mesa-libGLES-devel python3 python3-gobject gtk4 libadwaita python3-numpy python3-pillow libnotify zstd"
            INSTALL="dnf install -y" ;;
    suse)   PKGS="gcc make git pkgconf wayland-devel Mesa-libEGL-devel Mesa-libGLESv2-devel python3 python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 python3-numpy python3-Pillow libnotify-tools zstd"
            INSTALL="zypper --non-interactive install" ;;
    void)   PKGS="base-devel git pkg-config wayland-devel MesaLib-devel python3 python3-gobject gtk4 libadwaita python3-numpy python3-Pillow libnotify zstd"
            INSTALL="xbps-install -Sy" ;;
    alpine) PKGS="build-base git pkgconf wayland-dev mesa-dev python3 py3-gobject3 gtk4.0 libadwaita py3-numpy py3-pillow libnotify zstd"
            INSTALL="apk add" ;;
esac

PYBIN=""
pick_python() {   # a Python that has GObject introspection (for the settings app)
    for p in /usr/bin/python3 python3; do
        if command -v "$p" >/dev/null && "$p" -c 'import gi' 2>/dev/null; then
            PYBIN="$(command -v "$p")"; return 0; fi
    done
    PYBIN="$(command -v python3 || true)"
    return 1
}

missing() {
    local m=()
    command -v cc >/dev/null || command -v gcc >/dev/null || m+=("C compiler")
    command -v make >/dev/null || m+=("make")
    command -v pkg-config >/dev/null || command -v pkgconf >/dev/null || m+=("pkg-config")
    if command -v pkg-config >/dev/null; then
        for pc in wayland-client wayland-egl egl glesv2; do
            pkg-config --exists "$pc" || m+=("$pc (dev)")
        done
    fi
    if pick_python; then
        "$PYBIN" - <<'EOF' || m+=("GTK 4 / libadwaita for Python")
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa
EOF
        "$PYBIN" -c 'import numpy, PIL' 2>/dev/null || m+=("numpy + Pillow")
    else
        m+=("Python 3 with PyGObject")
    fi
    printf '%s\n' "${m[@]}"
}

need="$(missing)"
if [ -n "$need" ]; then
    warn "missing: $(echo "$need" | paste -sd, - | sed 's/,/, /g')"
    if [ -n "$family" ] && [ "$DEPS" = 1 ]; then
        echo "  The fix is to install: $PKGS"
        if ask "  Install them now with $(echo "$INSTALL" | cut -d' ' -f1) (needs your password)?"; then
            [ "$family" = debian ] && as_root apt-get update -qq
            # shellcheck disable=SC2086
            as_root $INSTALL $PKGS || {
                [ "$family" = arch ] && warn "if pacman couldn't find packages, sync first: sudo pacman -Syu"
                die "package installation failed"; }
            need="$(missing)"
        fi
    fi
    if [ -n "$need" ]; then
        [ -n "$family" ] || warn "unknown distribution; install the equivalents of: a C compiler, make, pkg-config, wayland + EGL + GLESv2 dev files, Python 3 with PyGObject, GTK 4, libadwaita, numpy, Pillow"
        die "still missing: $(echo "$need" | paste -sd, - | sed 's/,/, /g')"
    fi
fi
ok "all dependencies present"
pick_python || true

# ---- 3. get the source -----------------------------------------------------------
if [ -z "$SRC" ]; then
    command -v git >/dev/null || die "git is needed to download topopaper"
    SRC="$(mktemp -d)/topopaper"
    say "Downloading topopaper ($REF)"
    git clone -q --depth 1 --branch "$REF" "$REPO_URL" "$SRC"
fi

# ---- 4. build + install ------------------------------------------------------
say "Building"
make -s -C "$SRC" >/dev/null
ok "engine built"
say "Installing to $PREFIX"
if [ -w "$(dirname "$PREFIX")" ] || [ -w "$PREFIX" ] || mkdir -p "$PREFIX" 2>/dev/null; then
    make -s -C "$SRC" install PREFIX="$PREFIX" >/dev/null
else
    as_root make -s -C "$SRC" install PREFIX="$PREFIX" >/dev/null
fi
ok "installed"
BIN="$PREFIX/bin"

# ---- 5. Python environment for the map builder -----------------------------------
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/topopaper"
VENV="$DATA/venv"
say "Setting up the map builder"
mkdir -p "$DATA"
if [ ! -x "$VENV/bin/python" ]; then
    if "$PYBIN" -m venv --system-site-packages "$VENV" 2>/dev/null; then
        ok "created $VENV"
    else
        warn "python venv unavailable; using $PYBIN directly (timezone lookup and fast unpacking will be off)"
    fi
fi
if [ -x "$VENV/bin/python" ]; then
    # optional extras: offline timezones for far-away maps, and zstd unpacking
    if "$VENV/bin/python" -m pip install -q --disable-pip-version-check timezonefinder zstandard 2>/dev/null; then
        ok "optional extras installed (timezonefinder, zstandard)"
    else
        warn "couldn't install optional extras (offline?) — everything still works"
    fi
fi

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) warn "$BIN is not on your PATH — add it in your shell profile, e.g.:"
       echo "      echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.profile" ;;
esac
CTL="$BIN/topopaper-ctl"

# ---- 6. starter globe --------------------------------------------------------
if [ "$STARTER" = 1 ]; then
    say "Getting the starter globe (about 30 MB)"
    "$CTL" starter || warn "couldn't get the globe now; the wallpaper will fetch it on first start"
fi

# ---- 7. autostart + shortcut ----------------------------------------------------
if [ "$AUTOSTART" = 1 ]; then
    if ask "Start topopaper automatically when you log in?"; then
        "$CTL" autostart enable || warn "autostart: see the message above"
    fi
    if ask "Add a Super+Shift+B shortcut to search the map?"; then
        "$CTL" keybind add || warn "shortcut: see the message above"
    fi
fi

# ---- 8. go -------------------------------------------------------------------
say "Done"
"$CTL" doctor --offline || true
if [ "$LAUNCH" = 1 ] && [ -n "${WAYLAND_DISPLAY:-}" ]; then
    "$CTL" restart >/dev/null 2>&1 || true
    "$CTL" settings >/dev/null 2>&1 || true
    echo "The wallpaper is starting, and the settings window is opening."
else
    echo "Start it with:  topopaper-ctl restart     Settings:  topopaper-settings"
fi
