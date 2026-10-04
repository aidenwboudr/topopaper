#!/usr/bin/env python3
"""`topopaper-ctl site PACK`: SITE rungs beneath a pack: the ski resorts on its mountains.

A pack built for a town or a range is the K5/K4 map of the place. The resort
on its mountain is a K6 SITE (z15: downhill runs + lifts with tower crossbars
+ lift names). Sites are detected from DATA, never
from the name: OSM aerialway ways (chair lifts, gondolas, mixed lifts, cable
cars) are grouped by the landuse=winter_sports polygon they sit in (or by
proximity when a resort has none), and a group with >= MIN_LIFTS real lifts
is a resort worth its own rung. Each site is built on the tier grid
(--tier 6 recipe) and routed into the atlas by `route` (its parent
contains it at ratio 4 -> closed; a K4 parent gets a scaffold K5 rung).

    topopaper-ctl site <pack>            probe + build + route (background-safe)
    topopaper-ctl site <pack> --dry      probe + report only
    topopaper-ctl site <pack> --force    re-probe a pack already stamped

The parent's meta.json is stamped "sites": [slugs] so a probe is never
repeated (search runs this after every auto-build; the stamp is what
keeps it idempotent). Site packs are destinations (not scaffold) and carry
auto=true like every search-built pack.
"""
import argparse, json, math, os, subprocess, sys

from .. import net, paths, util

AREAS = str(paths.areas_dir())
MIN_LIFTS = 4          # a town hill with a chair or two is not a resort worth a rung
MAX_SITES = 3          # per parent pack, ranked by lift count
CLUSTER_KM = 2.5       # lifts closer than this belong to one (unnamed) resort
PAD = 0.12             # bbox breathing room each side before the tier snap
REAL_LIFTS = ("chair_lift", "gondola", "mixed_lift", "cable_car")


def load_builder():
    from . import area
    return area


slugify = util.slugify


def km(a, b):
    la1, lo1 = map(math.radians, a)
    la2, lo2 = map(math.radians, b)
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def bounds_of(el):
    b = el.get("bounds")
    if not b:
        return None
    return (b["minlat"], b["minlon"], b["maxlat"], b["maxlon"])


def union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def inside(pt, bb):
    return bb[0] <= pt[0] <= bb[2] and bb[1] <= pt[1] <= bb[3]


def probe(ma, bbox):
    """Overpass: real lifts + resort polygons in the pack bbox -> resorts
    [(name or None, bbox, nlifts)] ranked by lift count."""
    s, w, n, e = bbox
    bs = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
    kinds = "|".join(REAL_LIFTS)
    q = f'''[out:json][timeout:90];(
      way["aerialway"~"^({kinds})$"]{bs};
      way["landuse"="winter_sports"]{bs};
      relation["landuse"="winter_sports"]{bs};
    );out tags bb;'''
    d = ma.overpass(q)
    lifts, areas = [], []
    for el in d.get("elements", []):
        t = el.get("tags", {})
        bb = bounds_of(el)
        if not bb:
            continue
        if t.get("aerialway") in REAL_LIFTS:
            lifts.append((((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2), bb))
        elif t.get("landuse") == "winter_sports":
            areas.append((ma.pick_name(t), bb,
                          (bb[2] - bb[0]) * (bb[3] - bb[1])))
    areas.sort(key=lambda a: a[2])           # smallest polygon claims a lift
    groups = [dict(name=nm, bbox=bb, lifts=[]) for nm, bb, _ in areas]
    loose = []
    for c, bb in lifts:
        for gr in groups:
            if inside(c, gr["bbox"]):
                gr["lifts"].append((c, bb)); break
        else:
            loose.append((c, bb))
    # proximity clusters for resorts without a polygon (single linkage)
    clusters = []
    for c, bb in loose:
        hit = [cl for cl in clusters if any(km(c, c2) <= CLUSTER_KM for c2, _ in cl)]
        if not hit:
            clusters.append([(c, bb)])
        else:
            merged = [(c, bb)]
            for cl in hit:
                merged += cl; clusters.remove(cl)
            clusters.append(merged)
    for cl in clusters:
        groups.append(dict(name=None, bbox=None, lifts=cl))
    out = []
    for gr in groups:
        if len(gr["lifts"]) < MIN_LIFTS:
            continue
        bb = gr["bbox"]
        for _, lb in gr["lifts"]:
            bb = lb if bb is None else union(bb, lb)
        out.append((gr["name"], bb, len(gr["lifts"])))
    out.sort(key=lambda x: -x[2])
    print(f"site probe: {len(lifts)} lifts, {len(areas)} resort polygons -> "
          f"{len(out)} resort(s) with >= {MIN_LIFTS} lifts")
    return out


def name_unnamed(bb):
    """A lift cluster with no landuse=winter_sports polygon: ask nominatim
    what sits at its centre (a resort feature by name if one is mapped there,
    else the locality + "ski"). Never the parent pack's name — Thunder Bay's
    hill is not "isle-royale-ski"."""
    lat, lon = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    url = ("https://nominatim.openstreetmap.org/reverse?format=json&zoom=14"
           f"&accept-language=en&lat={lat:.5f}&lon={lon:.5f}")
    try:
        r = net.fetch_json(url)
    except Exception:
        return None
    if (r.get("class") in ("leisure", "landuse", "tourism", "amenity")
            and r.get("type") in ("winter_sports", "ski_resort", "sports_centre", "resort")
            and r.get("name")):
        return r["name"]
    ad = r.get("address", {})
    loc = ad.get("village") or ad.get("town") or ad.get("hamlet") or ad.get("city")
    return f"{loc} Ski" if loc else None


def existing_site_for(bb, packs):
    """A pack of site scale (z>=14) whose bbox holds the resort centre."""
    cy, cx = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    for name, m in packs.items():
        if m["zoom"] < 14:
            continue
        pb = m["bbox"]
        if pb[0] <= cy <= pb[2] and pb[1] <= cx <= pb[3]:
            return name
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="topopaper-ctl site")
    ap.add_argument("pack")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    mj = f"{AREAS}/{a.pack}/meta.json"
    if not os.path.isfile(mj):
        sys.exit(f"site: no such pack '{a.pack}'")
    meta = json.load(open(mj))
    if "sites" in meta and not a.force:
        print(f"site: {a.pack} already probed (sites={meta['sites']}); --force to redo")
        return
    n = 2 ** meta["zoom"]
    tier = -math.log(meta["ny"] / n) / math.log(4.0)
    if tier < 3.5 or tier > 5.5:
        # K<=3 boxes hold whole states of resorts, K6 IS a site
        print(f"site: {a.pack} is tier K{tier:.1f} — sites are probed for K4/K5 packs only")
        return
    ma = load_builder()
    packs = {}
    for d in os.listdir(AREAS):
        try:
            packs[d] = json.load(open(f"{AREAS}/{d}/meta.json"))
        except Exception:
            pass
    resorts = probe(ma, meta["bbox"])[:MAX_SITES]
    sites = []
    for name, bb, nl in resorts:
        have = existing_site_for(bb, packs)
        if have:
            print(f"  {name or 'unnamed resort'} ({nl} lifts): already served by pack '{have}'")
            sites.append(have)
            continue
        if not name:
            name = name_unnamed(bb)
        base = slugify(name) if name else f"{a.pack}-ski"
        slug, k = base, 2
        while slug in packs or slug in sites:
            slug = f"{base}-{k}"; k += 1
        hlat = (bb[2] - bb[0]) * PAD; hlon = (bb[3] - bb[1]) * PAD
        box = (bb[0] - hlat, bb[1] - hlon, bb[2] + hlat, bb[3] + hlon)
        print(f"  {name or 'unnamed resort'} ({nl} lifts) -> site '{slug}' "
              f"bbox {box[0]:.4f} {box[1]:.4f} {box[2]:.4f} {box[3]:.4f}")
        if a.dry:
            sites.append(slug)
            continue
        cmd, env = util.ctl_cmd("build", slug,
                                "--bbox", *[f"{v:.4f}" for v in box],
                                "--zoom", "15", "--tier", "6", "--auto")
        env["TOPO_PROGRESS_PREFIX"] = "site · "
        print(f"site: building {slug} (z15, K6)", flush=True)
        if subprocess.run(cmd, env=env).returncode != 0:
            print(f"site: build of {slug} FAILED"); continue
        cmd, _ = util.ctl_cmd("route", slug)
        if subprocess.run(cmd, env=env).returncode != 0:
            print(f"site: route for {slug} FAILED")
        sites.append(slug)
        util.notify(f"ski map ready — {name or slug} ({nl} lifts)", 3200)
    if a.dry:
        print(f"site: dry — would stamp sites={sites}")
        return
    meta = json.load(open(mj))                # re-read: builds may have run long
    meta["sites"] = sites
    with open(mj, "w") as fh:
        json.dump(meta, fh, indent=1)
    print(f"site: {a.pack} sites={sites}")


if __name__ == "__main__":
    main()
