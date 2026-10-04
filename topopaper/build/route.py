#!/usr/bin/env python3
"""`topopaper-ctl route PACK`: guarantee a pack has a clean LOD route into the atlas.

The ladder blends the two rungs bracketing the camera height. That reads as a
single continuous world ONLY if every blend completes while the finer pack
still covers the whole view — otherwise the coarse rung shows through at the
edges (multiple LODs in one frame) or, before the coverage floor, bare
background.

CLOSURE TEST between a child pack and its smallest containing parent:
    ratio = parent.msy / child.msy
    blend starts (15% log inset) at view height 0.48*child.msy*ratio^0.15,
    view width = 1.5x that; the child must still cover that view:
    closed  <=>  contains(parent, child) AND
                 0.72 * ratio^0.15 <= 0.95 * (child.msx / child.msy)

For every failing/missing link this script BUILDS the rung: zoom-2 (≈4x
scale), bbox = child ×4 centered, regional roads + top peaks + towns, named
<pack>-region (then <rung>-rN above that). The chain ends in the whole-globe
`earth` pack, which contains everything: rungs keep coming until the last
one closes against it (about tier K1, a quarter of the planet tall). With no
earth pack installed it is built first (`topopaper-ctl build --earth`).

Usage:  topopaper-ctl route <packname> [--dry]
"""
import argparse, json, math, os, subprocess, sys

from .. import paths, util

AREAS = str(paths.areas_dir())
CEILING_ZOOM = 4       # nothing coarser than this is ever built as a rung
MAX_RATIO = 12.0       # coarsest parent/child scale jump a crossfade may span

def pack_tier(p):
    # tier K <=> msy = 4^-K; off-grid packs round to the nearest tier
    return round(-math.log(p["msy"]) / math.log(4.0))

def region_args(zoom, tier):
    # recipes live in build/area.py's TIERS table — a rung just declares its
    # tier. K<=2 boxes are continental umbrellas: the unfiltered border/road/
    # water queries shed or time out over a continent on every Overpass
    # mirror, and a connector rung's job is routing, so it gets the marquee
    # treatment (terrain, top peaks, DEM water). TIERS carries K>=3.
    if tier <= 2:
        return ["--tier", str(max(tier, 0)),
                "--features", "none", "--labels", "peaks",
                "--peak-min-ele", "3500", "--peak-top", "8", "--peak-sep", "90",
                "--water-from-dem"]
    return ["--tier", str(tier)]

def load_packs():
    out = {}
    if not os.path.isdir(AREAS):
        return out
    for d in sorted(os.listdir(AREAS)):
        mj = f"{AREAS}/{d}/meta.json"
        if not os.path.isfile(mj):
            continue
        m = json.load(open(mj))
        n = 2 ** m["zoom"]
        out[d] = dict(name=d, zoom=m["zoom"], bbox=m["bbox"],
                      mx0=m["x0"] / n, my0=m["y0"] / n,
                      msx=m["nx"] / n, msy=m["ny"] / n)
    return out

def contains(parent, child, eps=1e-9):
    return (parent["mx0"] <= child["mx0"] + eps and
            parent["my0"] <= child["my0"] + eps and
            parent["mx0"] + parent["msx"] >= child["mx0"] + child["msx"] - eps and
            parent["my0"] + parent["msy"] >= child["my0"] + child["msy"] - eps)

def closed_link(parent, child):
    ratio = parent["msy"] / child["msy"]
    if ratio <= 8.0:
        # a couple-octave LOD mismatch in the brief overhang window is
        # imperceptible — the approved original chain lives at 4-4.4x and
        # lake-powell-region→conus sat at 6.4x, a hair past the old 6.0
        # cutoff, spawning a pointless giant z7 rung
        return True
    # past 8x a link can still close when the child is wide enough to cover
    # the view through the whole blend — but only up to MAX_RATIO: beyond
    # that the parent is so much coarser that the crossfade dissolves sharp
    # terrain into a blur (a K4 pack against the z4 globe is 256x)
    if ratio > MAX_RATIO:
        return False
    need = 0.72 * ratio ** 0.15
    return need <= 0.95 * (child["msx"] / child["msy"])

def rung_bbox(child_bbox, factor=4.0):
    s, w, n, e = child_bbox
    cy, cx = (s + n) / 2, (w + e) / 2
    hlat = (n - s) / 2 * factor
    hlon = (e - w) / 2 * factor
    return (max(-84.0, cy - hlat), max(-179.9, cx - hlon),
            min(84.0, cy + hlat), min(179.9, cx + hlon))

def build(name, bbox, zoom, extra, scaffold=False):
    args = ([name, "--bbox", *[f"{v:.4f}" for v in bbox], "--zoom", str(zoom)]
            + list(extra))
    run_build(args, name, zoom, scaffold)

def run_build(args, name, zoom, scaffold=False):
    cmd, env = util.ctl_cmd("build", *args)
    print(f"route: building {name} (z{zoom})", flush=True)
    env["TOPO_PROGRESS_PREFIX"] = "route · "
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        sys.exit(f"route: build of {name} FAILED")
    if scaffold:
        # mark auto-built rungs so the search menu can hide them: they
        # are LOD plumbing, not destinations (the r6/r8/-region clutter)
        mj = f"{AREAS}/{name}/meta.json"
        try:
            m = json.load(open(mj))
            m["scaffold"] = True
            with open(mj, "w") as fh:
                json.dump(m, fh, indent=1)
        except Exception as exc:
            print(f"route: could not mark {name} as scaffold ({exc})")

def main(argv=None):
    ap = argparse.ArgumentParser(prog="topopaper-ctl route")
    ap.add_argument("pack")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    if "earth" not in load_packs() and a.pack != "earth":
        if a.dry:
            print("route: would build the earth pack first")
        else:
            run_build(["--earth"], "earth", 4)

    for _ in range(8):                       # chains are short; 8 = paranoia cap
        packs = load_packs()
        if a.pack not in packs:
            sys.exit(f"route: no such pack '{a.pack}'")
        cur = packs[a.pack]
        ps = []
        while True:                          # climb through already-closed links
            ps = [p for p in packs.values()
                  if p["name"] != cur["name"] and contains(p, cur)
                  and p["msy"] > cur["msy"] * 1.05]
            if not ps:
                break
            best = min(ps, key=lambda p: p["msy"])
            if not closed_link(best, cur):
                break
            cur = best
        if not ps:                           # nothing above: cur is earth itself
            print(f"route: chain closed ({cur['name']} is the top)")
            return
        if cur["zoom"] - 2 < CEILING_ZOOM:
            print(f"route: {cur['name']} is as coarse as rungs go — closed enough")
            return
        # need a rung above cur (too-wide link, or open sky still too fine)
        rung = (f"{a.pack}-region" if cur["name"] == a.pack
                else f"{cur['name']}-r{cur['zoom'] - 2}")
        if rung in packs:
            print(f"route: rung {rung} exists but chain not closed — abort")
            return
        if a.dry:
            print(f"route: would build {rung} (z{cur['zoom']-2}, over {cur['name']})")
            return
        build(rung, rung_bbox(cur["bbox"]), cur["zoom"] - 2,
              region_args(cur["zoom"] - 2, pack_tier(cur) - 1), scaffold=True)
    print("route: cap reached")

if __name__ == "__main__":
    main()
