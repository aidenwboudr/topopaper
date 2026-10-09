#!/usr/bin/env bash
# Render smoke test: run the engine under a headless sway, fly to a pack, and
# screenshot it. Fails if the frame is blank. Needs sway, grim, mesa (llvmpipe)
# and a data dir with at least the earth pack.
#
#   tests/render-smoke.sh DATA_DIR OUT_DIR [PACK]
set -euo pipefail
DATA="$1" OUT="$2" PACK="${3:-earth}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$OUT"
[ -x "$ROOT/build/topopaper" ] || make -s -C "$ROOT"

export XDG_RUNTIME_DIR; XDG_RUNTIME_DIR="$(mktemp -d)"; chmod 700 "$XDG_RUNTIME_DIR"
export WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman
export HOME; HOME="$(mktemp -d)"
export TOPOPAPER_DATA="$DATA" XDG_STATE_HOME="$HOME/state"
printf 'output HEADLESS-1 mode 1280x800\n' > "$HOME/sway.conf"
sway -c "$HOME/sway.conf" > "$OUT/sway.log" 2>&1 &
SWAY=$!
trap 'kill $SWAY 2>/dev/null || true' EXIT
for _ in $(seq 100); do [ -S "$XDG_RUNTIME_DIR/wayland-1" ] && break; sleep 0.1; done
export WAYLAND_DISPLAY=wayland-1

mkdir -p "$XDG_STATE_HOME/topopaper"
echo "$PACK" > "$XDG_STATE_HOME/topopaper/area"
# pin the sun (noon UTC, late May): the default globe view is on its night
# side, so city lights and the aurora get exercised too
TOPA_SUN_T=1780056000 "$ROOT/build/topopaper" > "$OUT/engine.log" 2>&1 &
sleep 8
grim "$OUT/$PACK.png"
python3 - "$OUT/$PACK.png" <<'EOF'
import sys
from PIL import Image, ImageStat
im = Image.open(sys.argv[1]).convert("L")
sd = ImageStat.Stat(im).stddev[0]
print(f"{sys.argv[1]}: luminance stddev {sd:.1f}")
sys.exit(0 if sd > 4.0 else "frame looks blank")
EOF

# `topopaper-ctl snapshot`: the engine answers with the display's frame minus
# the HUD, also while a fullscreen window covers it (no frame callbacks then)
snap() {
    PYTHONPATH="$ROOT" python3 -m topopaper.cli snapshot "$1"
    python3 - "$1" <<'EOF'
import sys
from PIL import Image, ImageStat
im = Image.open(sys.argv[1])
sd = ImageStat.Stat(im.convert("L")).stddev[0]
print(f"{sys.argv[1]}: {im.size[0]}x{im.size[1]}, luminance stddev {sd:.1f}")
sys.exit(0 if im.size == (1280, 800) and sd > 4.0 else "bad snapshot")
EOF
}
snap "$OUT/snapshot.png"
if command -v foot >/dev/null; then
    SWAYSOCK=$(ls "$XDG_RUNTIME_DIR"/sway-ipc.*.sock) swaymsg -q 'exec foot -F'
    sleep 2
    snap "$OUT/snapshot-covered.png"
fi
