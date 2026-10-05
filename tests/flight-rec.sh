#!/usr/bin/env bash
# Flight recorder: run the engine under a headless sway in record mode
# (TOPA_REC, a virtual 30 fps clock) and keep every frame of one or more
# flights, for tests/flight-seams.py. Deterministic apart from GPU rounding:
# the sun is pinned, roaming and the CPU tint are off, the HUD is dropped.
# Needs sway and mesa (llvmpipe is fine, just slow) and a data dir of packs.
#
#   tests/flight-rec.sh DATA_DIR OUT_DIR START FLY[,FLY...] [WxH]
#
# Env: SUN_T (epoch, default 18:00 UTC in late May), FLIGHT_SECONDS, DBG=N
# (every Nth frame also dumps slot shares + each slot alone, see TOPA_REC_DBG).
set -euo pipefail
DATA="$1" OUT="$2" START="$3" FLY="$4" SIZE="${5:-1440x960}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$OUT"
rm -f "$OUT"/[fd]*.ppm "$OUT"/[fd]*.png "$OUT/trace.txt"
make -s -C "$ROOT" >/dev/null

export XDG_RUNTIME_DIR; XDG_RUNTIME_DIR="$(mktemp -d)"; chmod 700 "$XDG_RUNTIME_DIR"
export WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman
export HOME; HOME="$(mktemp -d)"
export TOPOPAPER_DATA="$DATA" XDG_STATE_HOME="$HOME/state" XDG_CONFIG_HOME="$HOME/config"
printf 'output HEADLESS-1 mode %s\n' "$SIZE" > "$HOME/sway.conf"
mkdir -p "$XDG_CONFIG_HOME/topopaper" "$XDG_STATE_HOME/topopaper"
cat > "$XDG_CONFIG_HOME/topopaper/config.ini" <<EOF
[general]
roam_minutes = 0
react_to_cpu = false
flight_seconds = ${FLIGHT_SECONDS:-20}
EOF
echo "$START" > "$XDG_STATE_HOME/topopaper/area"

sway -c "$HOME/sway.conf" > "$OUT/sway.log" 2>&1 &
SWAY=$!
trap 'kill $SWAY 2>/dev/null || true' EXIT
for _ in $(seq 100); do [ -S "$XDG_RUNTIME_DIR/wayland-1" ] && break; sleep 0.1; done
export WAYLAND_DISPLAY=wayland-1

TOPA_SUN_T="${SUN_T:-1780077600}" TOPA_NOHUD=1 TOPA_REC_DBG="${DBG:-0}" TOPA_REC="$OUT" TOPA_REC_FLY="$FLY" TOPA_REC_HOLD="${HOLD:-1}" TOPA_REC_FRAMES="${TOPA_REC_FRAMES:-}" \
    "$ROOT/build/topopaper" > "$OUT/engine.log" 2>&1
grep -h 'flight\|error\|warn' "$OUT/engine.log" || true
echo "$(find "$OUT" -maxdepth 1 -name 'f*.ppm' | wc -l) frames in $OUT"
