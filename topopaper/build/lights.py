#!/usr/bin/env python3
"""`topopaper-ctl lights`: bake lights.bin: the world's cities for the globe's
night side.

One Overpass query (place=city with population >= 250k), split into five
latitude bands so a shed band can retry alone. Output:
  lights.bin: 'TOPOLTS1' | u32 count | count x (f32 lat_rad, lon_rad, w)
w is a log-population brightness in [0.35, 1.0]. Re-run whenever; the
engine loads it at boot (absent file -> no lights pass, nothing else
changes).
"""
import argparse
import json
import math
import struct
import sys
import time
import urllib.parse

from .. import net, paths

EPS = net.OVERPASS_ENDPOINTS
BANDS = [(-60, -10), (-10, 20), (20, 32), (32, 45), (45, 75)]
MIN_POP = 250000


def fetch(band, tries=6):
    s, n = band
    q = (f'[out:json][timeout:120];node["place"="city"]'
         f'(if:number(t["population"])>={MIN_POP})({s},-180,{n},180);out;')
    delay = 8.0
    for i in range(tries):
        ep = EPS[i % len(EPS)]
        try:
            with net.request(ep, data=urllib.parse.urlencode({"data": q}).encode(),
                             timeout=150) as r:
                d = json.load(r)
            if "remark" in d and "error" in str(d.get("remark", "")).lower():
                raise RuntimeError(d["remark"])
            return d.get("elements", [])
        except Exception as exc:
            print(f"  band {band}: retry in {delay:.0f}s ({exc})", file=sys.stderr)
            time.sleep(delay)
            delay *= 1.7
    raise SystemExit(f"band {band}: overpass shed it — rerun later")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="topopaper-ctl lights")
    ap.add_argument("--out", default=str(paths.data_dir() / "lights.bin"))
    out = ap.parse_args(argv).out
    pts = []
    for b in BANDS:
        els = fetch(b)
        for e in els:
            try:
                pop = float(str(e["tags"]["population"]).replace(",", ""))
            except Exception:
                continue
            if pop < MIN_POP:
                continue
            w = 0.35 + 0.4 * math.log10(pop / MIN_POP + 1.0)
            pts.append((math.radians(e["lat"]), math.radians(e["lon"]),
                        min(1.0, w)))
        print(f"  band {b}: {len(els)} cities (total {len(pts)})")
        time.sleep(3)
    if len(pts) > 8192:
        pts.sort(key=lambda p: -p[2])
        pts = pts[:8192]
    with open(out, "wb") as f:
        f.write(b"TOPOLTS1")
        f.write(struct.pack("<I", len(pts)))
        for la, lo, w in pts:
            f.write(struct.pack("<fff", la, lo, w))
    print(f"lights.bin: {len(pts)} cities")


if __name__ == "__main__":
    main()
