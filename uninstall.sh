#!/usr/bin/env bash
# Remove topopaper (Linux and macOS; Windows: uninstall.ps1).
#
#   ./uninstall.sh                 remove the program; keep your maps and settings
#   ./uninstall.sh --purge         also delete maps, settings, caches and logs
#   ./uninstall.sh --prefix DIR    if you installed with --prefix
set -euo pipefail
PREFIX="$HOME/.local"
PURGE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift ;;
        --prefix=*) PREFIX="${1#*=}" ;;
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

CTL="$PREFIX/bin/topopaper-ctl"
if [ -x "$CTL" ]; then
    "$CTL" autostart disable >/dev/null 2>&1 || true
    "$CTL" keybind remove >/dev/null 2>&1 || true
    "$CTL" stop >/dev/null 2>&1 || true
fi
pkill -u "$(id -u)" -x topopaper 2>/dev/null || true

run() { if [ -w "$PREFIX" ]; then "$@"; else sudo "$@"; fi; }
for f in bin/topopaper bin/topopaper-ctl bin/topopaper-session bin/topopaper-settings \
         share/applications/io.github.aidenwboudr.topopaper.desktop \
         share/icons/hicolor/scalable/apps/io.github.aidenwboudr.topopaper.svg \
         share/metainfo/io.github.aidenwboudr.topopaper.metainfo.xml; do
    run rm -f "$PREFIX/$f"
done
run rm -rf "$PREFIX/share/topopaper"
[ "$(uname -s)" = Darwin ] && rm -rf "$HOME/Applications/Topopaper.app"
echo "topopaper removed from $PREFIX"

data="${XDG_DATA_HOME:-$HOME/.local/share}/topopaper"
if [ "$PURGE" = 1 ]; then
    rm -rf "$data" \
           "${XDG_CONFIG_HOME:-$HOME/.config}/topopaper" \
           "${XDG_STATE_HOME:-$HOME/.local/state}/topopaper" \
           "${XDG_CACHE_HOME:-$HOME/.cache}/topopaper"
    [ "$(uname -s)" = Darwin ] && rm -rf "$HOME/Library/Caches/topopaper"
    echo "maps, settings, caches and logs deleted"
else
    echo "kept your maps and settings ($data, ~/.config/topopaper); --purge deletes them"
fi
