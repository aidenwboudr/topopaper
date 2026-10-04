"""Turn free text ("tetons", "hawaii", "zermatt") into a pack build plan.

Nominatim does the lookup; when its exact-word match is weak, Photon (a
typo-tolerant OSM search) proposes spellings that are re-asked of Nominatim,
so the ranking and record shape always come from one source. The chosen
place's true extent is snapped onto the tier grid: a town gets a K5 leaf, an
island chain or a range gets the K4/K3/K2 pack it actually is, a ski resort
gets a K6 site.
"""
import difflib
import math
import re
import time
import unicodedata
import urllib.parse

from . import net, util

TIER_ZOOM = {6: 15, 5: 12, 4: 11, 3: 9, 2: 7}
TIER_NAMES = {6: "ski resort", 5: "local", 4: "area", 3: "regional", 2: "continental"}


class GeocodeError(Exception):
    """kind: 'fetch' (network), 'notfound', 'toobig'."""

    def __init__(self, kind, msg=""):
        super().__init__(msg or kind)
        self.kind = kind


_last = [0.0]


def nominatim(text):
    # dedupe=0: Nominatim otherwise collapses same-name results and may keep
    # the bare node over the bounded area (a range's summit instead of the
    # range) — we want every representative so the scorer can prefer the one
    # with a real extent. Usage policy: at most 1 request per second.
    dt = time.time() - _last[0]
    if dt < 1.05:
        time.sleep(1.05 - dt)
    _last[0] = time.time()
    return net.fetch_json("https://nominatim.openstreetmap.org/search?format=json&limit=8"
                          "&dedupe=0&accept-language=en&q=" + urllib.parse.quote(text))


def toks(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def ocover(qt, nt):
    """Typed words matched to name words IN ORDER (subsequence alignment):
    a typo scores ~0.9 ("resevoir"~"reservoir"), a partial name 1.0
    ("steamboat" in "Steamboat Springs"), and lookalike words in another
    order stay low."""
    if not qt or not nt:
        return 0.0
    dp = [[0.0] * (len(nt) + 1) for _ in range(len(qt) + 1)]
    for i in range(1, len(qt) + 1):
        for j in range(1, len(nt) + 1):
            dp[i][j] = max(dp[i][j - 1], dp[i - 1][j],
                           dp[i - 1][j - 1] + ratio(qt[i - 1], nt[j - 1]))
    return dp[-1][-1] / len(qt)


def merc_y(latd):
    lr = math.radians(latd)
    return (1.0 - math.log(math.tan(lr) + 1.0 / math.cos(lr)) / math.pi) / 2.0


def near_installed(q, cur=""):
    """A near-miss of an installed pack name is that pack — typo tolerance
    without the network."""
    slug = util.slugify(q)
    best, bn = 0.0, ""
    for n, m in util.list_packs():
        if m.get("scaffold") and n != cur:
            continue                             # rungs are plumbing, not places
        r = ratio(slug, n)
        if r > best:
            best, bn = r, n
    return bn if best >= 0.86 else ""


def _photon(text, bias=None):
    p = {"q": text, "limit": 8, "lang": "en"}
    if bias:
        p["lat"], p["lon"] = f"{bias[0]:.3f}", f"{bias[1]:.3f}"
    try:
        d = net.fetch_json("https://photon.komoot.io/api/?" + urllib.parse.urlencode(p))
    except Exception:
        return []
    out = []
    for f in d.get("features", []):
        pr = f.get("properties") or {}
        if not pr.get("name"):
            continue
        ctx = set()                              # address words = context, not name
        for k in ("state", "county", "city", "district", "country"):
            ctx |= set(toks(pr.get(k) or ""))
        out.append((pr["name"], ctx))
    return out


def plan(q, cur=""):
    """-> dict(slug, display, addresstype, bbox=(s,w,n,e), zoom, tier, fuzzy).
    Raises GeocodeError."""
    try:
        rs = nominatim(q)
    except Exception as e:
        raise GeocodeError("fetch", str(e)) from e

    def sim(r):
        """How well a record's NAME matches what was typed; typed words found
        verbatim in its address are context ("... colorado"), not name."""
        name = r.get("name") or r["display_name"].split(",")[0]
        nt = toks(name)
        ctx = set(toks(r["display_name"])) - set(nt)
        qt = [t for t in toks(q) if t not in ctx] or toks(q)
        return ocover(qt, nt)

    def imp(r):
        return float(r.get("importance") or 0.0)

    fuzzy = False
    weak = not rs or max(sim(x) for x in rs) < 0.8 or max(imp(x) for x in rs) < 0.06
    if weak:
        # the relaxed pass leans toward the map in view ("search near what
        # I'm looking at"): the atlas's own pack, never the user's location
        bias = None
        m = util.read_meta(cur) if cur else None
        if m and m.get("bbox") and m["bbox"][3] - m["bbox"][1] < 90.0:
            b = m["bbox"]
            bias = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

        def relax(text):
            # keep each long word's head — typos live in the tail
            return " ".join(t[:max(3, math.ceil(0.6 * len(t)))] if len(t) >= 5 else t
                            for t in toks(text))
        props = {}
        order = 0
        for names in (_photon(q), _photon(relax(q), bias)):
            for nm, ctx in names:
                order += 1
                nt = toks(nm)
                qt = [t for t in toks(q) if t not in ctx - set(nt)] or toks(q)
                c = ocover(qt, nt)
                key = nm.lower()
                if c >= 0.75 and key != q.lower() and key not in props:
                    props[key] = (c, -order, nm)
            if any(c >= 0.88 for c, _, _ in props.values()):
                break                            # a confident spelling: stop asking
        for c, _, nm in sorted(props.values(), reverse=True)[:3]:
            try:
                rs = rs + nominatim(nm)
            except Exception:
                pass
        pool = [x for x in rs if sim(x) >= 0.75]
        if pool:
            rs, fuzzy = pool, True
    if not rs:
        raise GeocodeError("notfound")

    def spans(r):
        bb = [float(x) for x in r["boundingbox"]]      # s, n, w, e
        return bb, bb[1] - bb[0], bb[3] - bb[2]

    def score(r):
        _, la, lo = spans(r)
        s = imp(r) + (0.15 if max(la, lo) > 0.005 else 0.0)
        if fuzzy:                                # closer spellings break near-ties
            s += 0.5 * (sim(r) - 0.75)
        return s

    r = max(rs, key=score)
    lat, lon = float(r["lat"]), float(r["lon"])
    bb, la, lo = spans(r)

    # point-only results (ranges are often bare nodes) get a type-sized box,
    # cos-scaled so the synthetic box lands on its tier at any latitude
    range_types = {"mountain_range", "region", "archipelago", "ridge", "massif"}
    if max(la, lo) <= 0.005:
        cosl = max(0.2, math.cos(math.radians(lat)))
        half = max(0.10, 0.432 * cosl) if r.get("type") in range_types else 0.14
        hlon = half * 1.5 / cosl
        bb = [lat - half, lat + half, lon - hlon, lon + hlon]
    else:
        py, px = la * 0.08, lo * 0.08                  # 8% breathing room
        bb = [bb[0] - py, bb[1] + py, bb[2] - px, bb[3] + px]
    bb[0] = max(-84.0, bb[0]); bb[1] = min(84.0, bb[1])
    bb[2] = max(-179.9, bb[2]); bb[3] = min(179.9, bb[3])

    sm = abs(merc_y(bb[0]) - merc_y(bb[1]))
    # smallest tier whose fixed pack height (msy = 4^-K) holds the box, with
    # 15% headroom for the tile snap (snapping trims edge rows)
    tier = next((k for k in (5, 4, 3, 2) if sm <= 0.85 * 4.0 ** -k), None)
    if tier is None:
        raise GeocodeError("toobig")
    # a ski resort is a K6 site (z15 runs + lifts), not a K5 town map
    if r.get("type") == "winter_sports" or (
            r.get("class") == "leisure" and re.search(r"\bski\b", r["display_name"], re.I)):
        tier = 6

    disp = r["display_name"].split(",")[0].strip()
    slug = util.slugify(disp) or "place"
    addr = r.get("addresstype", "place")
    # slug collision with a different-scale pack ("hawaii" the island vs the
    # state): suffix the address type -> hawaii-state
    m = util.read_meta(slug)
    if m and util.pack_exists(slug) and m.get("zoom") != TIER_ZOOM[tier]:
        slug = f"{slug}-{util.slugify(addr)}"
    return dict(slug=slug, display=disp, addresstype=addr,
                bbox=(bb[0], bb[2], bb[1], bb[3]), zoom=TIER_ZOOM[tier],
                tier=tier, fuzzy=fuzzy, lat=lat, lon=lon,
                full_name=r["display_name"])


def lookup_point(q):
    """Best single match as (lat, lon, short name) — for the weather location."""
    rs = nominatim(q)
    if not rs:
        raise GeocodeError("notfound")
    r = max(rs, key=lambda x: float(x.get("importance") or 0.0))
    return float(r["lat"]), float(r["lon"]), r["display_name"].split(",")[0].strip()

