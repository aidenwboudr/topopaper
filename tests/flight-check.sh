#!/usr/bin/env bash
# Flight seam check: build the synthetic packs (tests/synth-packs.py), record
# a set of flights through them (tests/flight-rec.sh) and hold every flight to
# the limits below (tests/flight-seams.py). No network; CI runs it under
# headless sway. Fails if any flight leaves part of the screen bare, pops, or
# blends packs that disagree.
#
#   tests/flight-check.sh OUT_DIR
set -euo pipefail
OUT="$1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$OUT"
python3 "$ROOT/tests/synth-packs.py" "$OUT/data"
# the engine takes a pack once its files have been quiet a few seconds
find "$OUT/data" -exec touch -d '-1 minute' {} +

# limits (0-255 colour units): no void at all; a frame-to-frame jump beyond
# the motion trend of at most 8; overlapping packs within 30 of each other
# where they hand over. The engine before these checks scored voids, pops up
# to 19 and disagreements up to 78 on these flights.
LIMITS=(--max-void 0 --max-pop 8 --max-tone 30)
fail=0
for flight in "dive synth-world synth-site" "hop synth-site synth-area-east" \
              "back synth-area-east synth-site" "wide synth-world synth-site-b" \
              "up synth-site-b synth-world"; do
    read -r name from to <<< "$flight"
    DBG=5 FLIGHT_SECONDS=10 "$ROOT/tests/flight-rec.sh" "$OUT/data" "$OUT/$name" "$from" "$to" 640x400 \
        > "$OUT/$name.rec.log" 2>&1
    if python3 "$ROOT/tests/flight-seams.py" "$OUT/$name" --data "$OUT/data" "${LIMITS[@]}" \
            > "$OUT/$name.txt" 2> "$OUT/$name.err"; then
        echo "ok    $name: $(tail -1 "$OUT/$name.txt")"
    else
        echo "FAIL  $name: $(tail -1 "$OUT/$name.txt")"
        tail -5 "$OUT/$name.err" 2>/dev/null || true
        fail=1
    fi
    # keep the reports and a few frames, not ~1 GB of them
    find "$OUT/$name" -name '*.ppm' ! -name 'f00*00.ppm' -delete
done
exit $fail
