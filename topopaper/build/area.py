#!/usr/bin/env python3
"""Build a topopaper AREA PACK (`topopaper-ctl build`).

An area pack is a directory <data>/areas/<name>/ holding
everything the engine needs to render one map at one scale:

    terrain.bin   'TOPOTER1' | u32 W,H | W*H (hi,lo) byte pairs, rows SOUTH-first
                  16-bit heightmap normalized to the pack's true elev min/max.
    features.bin  'TOPORDS1' | u32 W,H | W*H (L,A) bytes, rows SOUTH-first
                  two distance-field channels (texel dist * 8, clamped 255).
                  Channel semantics per --features preset:
                    roads-region: L = major roads, A = motorways
                    ski:   L = downhill runs, A = lifts (with tower crossbars)
    labels.bin    'TOPOPKS2' | u32 count,atlasW,atlasH |
                  per label: f32 u,v (terrain uv, south-origin)
                             f32 hmin,hmax (visible when hmin < view-height < hmax,
                                            view-height in normalized-mercator units;
                                            engine soft-fades at both edges)
                             u16 ax,ay (anchor px in rect, y from TOP)
                             u16 rx,ry,rw,rh (atlas rect, y from TOP)
                  | atlas (L,A) byte pairs   (engine also reads legacy TOPOPKS1)
    meta.bin      'TOPOAR01' | f64 mx0,my0,msx,msy (normalized web-mercator:
                  x/2^z, y/2^z of the tile origin + span; y grows SOUTH)
                  | f32 elev_lo, elev_hi, ctr_step_m
                  | f32 focus_u, focus_v (idle-camera orbit centre, terrain uv;
                    ski packs use the run/lift centroid, else 0.5,0.5)
                  | f32 chL r,g,b,w0,w1,op | f32 chA r,g,b,w0,w1,op
                  (w0,w1 = smoothstep edges in screen px; op = stroke opacity)
    meta.json     human/tool sidecar (bbox, tiles, labels, provenance).
    preview.png   contours + hillshade + features + label marks, north-up.

Contour steps are ABSOLUTE-elevation referenced (lev = elev_m / step) and the
step is picked from the halving family {60,30,15} m so a coarser pack's lines
are a subset of a finer pack's — crossfades between scales read as detail
fading in, not lines sliding.

Peaks come from OSM (natural=peak) and are SNAPPED to the DEM local max, with
the label elevation read from the DEM — placements are verified against data,
never recalled coordinates (hand-placed coordinates once put one peak's
name on its neighbour).

Usage:
  topopaper-ctl build NAME --bbox LAT_S LON_W LAT_N LON_E --zoom Z
               [--tier K] [--features roads-region|ski|states|none]
               [--labels peaks,places,lifts] [--peak-min-ele M] [--step M]
  topopaper-ctl build --earth          the whole-globe pack every route ends in
"""
import argparse, io, json, math, os, re, struct, sys, time, unicodedata, warnings
import urllib.parse, urllib.request
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .. import config, fonts, net, paths

CACHE    = str(paths.cache_dir() / "terrarium")
TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
OVERPASS_ENDPOINTS = net.OVERPASS_ENDPOINTS
UA       = net.UA
_op_last_ok = 0            # sticky: start retries at the last endpoint that worked
_op_last_t  = 0.0          # politeness spacing between queries

# Catppuccin Macchiato, linear-ish floats as the shader uses them
PEACH    = (0.961, 0.663, 0.498)   # roads
SAPPHIRE = (0.490, 0.769, 0.894)   # ski runs
RED      = (0.929, 0.529, 0.588)   # lifts

# ---- mercator ---------------------------------------------------------------
def lonlat_to_tilef(lon, lat, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    r = math.radians(lat)
    y = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
    return x, y

def tiley_to_lat(yf, z):
    n = 2 ** z
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yf / n))))

# ---- network ----------------------------------------------------------------
def fetch_tile(z, x, y):
    p = f"{CACHE}/{z}/{x}/{y}.png"
    if os.path.exists(p):
        return Image.open(p).convert("RGB")
    for attempt in range(3):
        try:
            req = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y), headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            time.sleep(0.05)
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5)

class OverpassLoadShed(Exception):
    # HTTP 200 with empty elements and the error only in "remark" — Overpass
    # load-shedding a query whose SCAN phase blew the time budget (a server-
    # side (if:) filter can't help; it runs after the scan). Retrying the same
    # box is futile — callers must shrink the box or fail loud, never bake an
    # empty result as if the region were empty.
    pass

def overpass(q, tries=7):
    # 429/outage-resilient: sticky endpoint + rotation + progressive backoff
    # (rate limits hit VPN exit IPs hard; mirrors have their own bad nights)
    global _op_last_ok, _op_last_t
    wait = 3.0 - (time.time() - _op_last_t)
    if wait > 0:
        time.sleep(wait)
    delay = 8.0
    for attempt in range(tries):
        url = OVERPASS_ENDPOINTS[(_op_last_ok + attempt) % len(OVERPASS_ENDPOINTS)]
        try:
            req = urllib.request.Request(
                url, data=("data=" + urllib.parse.quote(q)).encode(), headers=UA)
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.load(r)
            if "error" in out.get("remark", "").lower() and not out.get("elements"):
                raise OverpassLoadShed(out["remark"])
            _op_last_ok = (_op_last_ok + attempt) % len(OVERPASS_ENDPOINTS)
            _op_last_t = time.time()
            return out
        except OverpassLoadShed:
            _op_last_t = time.time()
            raise
        except Exception as e:
            if attempt == tries - 1:
                raise
            print(f"  overpass retry in {delay:.0f}s "
                  f"({url.split('/')[2]}: {e})", flush=True)
            left = delay
            while left > 0:                  # keep the wallpaper meter alive
                time.sleep(min(5.0, left))
                left -= 5.0
                if PROG:
                    PROG.heartbeat()
            delay *= 1.7

# ---- in-wallpaper progress meter --------------------------------------------
# The engine can't rasterize text (labels arrive pre-baked), so the BUILDER
# bakes the whole meter — label text + hairline bar, map-label styling — into
# $XDG_RUNTIME_DIR/topopaper-progress.bin ('TOPOPRG1' | u16 w,h | L,A bytes).
# topopaper fades it in bottom-left while the file exists and is fresh; the
# heartbeat keeps mtime alive through long network backoffs. Atomic replace
# so the engine never reads a torn frame.
PROG = None

class Progress:
    def __init__(self, name):
        self.path = str(paths.progress_file())
        self.prefix = os.environ.get("TOPO_PROGRESS_PREFIX", "")
        self.name = name
        self.last_t = 0.0
        self.last_frac = -1.0
        self.last_stage = ""
        try:
            self.font = ImageFont.truetype(fonts.find_font(), 24)
        except Exception:
            self.font = None

    def set(self, frac, stage):
        if not self.font:
            return
        now = time.time()
        if (stage == self.last_stage and now - self.last_t < 0.4
                and frac - self.last_frac < 0.02):
            return
        self.last_t, self.last_frac, self.last_stage = now, frac, stage
        try:
            self._bake(max(0.0, min(1.0, frac)), f"{self.prefix}{self.name} · {stage}")
        except Exception:
            pass

    def heartbeat(self):
        try:
            os.utime(self.path)
        except OSError:
            pass

    def done(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _bake(self, frac, label):
        S2 = 2
        pad, gap, bh = 5 * S2, 4 * S2, 2 * S2
        bw = 190 * S2
        tb = self.font.getbbox(label)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        w = max(tw, bw) + 2 * pad
        h = pad + th + gap + bh + pad
        Li = Image.new("L", (w, h), 0)
        Ai = Image.new("L", (w, h), 0)
        dl, da = ImageDraw.Draw(Li), ImageDraw.Draw(Ai)
        tx, ty = pad, pad - tb[1]
        for ox in (-S2, 0, S2):
            for oy in (-S2, 0, S2):
                da.text((tx + ox, ty + oy), label, font=self.font, fill=255)
        dl.text((tx, ty), label, font=self.font, fill=255)
        y0 = pad + th + gap
        da.rectangle([pad, y0, pad + bw, y0 + bh], fill=120)      # track (dark)
        dl.rectangle([pad, y0, pad + bw, y0 + bh], fill=36)
        fw = int(bw * frac)
        if fw > 0:
            da.rectangle([pad, y0, pad + fw, y0 + bh], fill=255)  # fill (light)
            dl.rectangle([pad, y0, pad + fw, y0 + bh], fill=235)
        inter = np.dstack([np.asarray(Li), np.asarray(Ai)])
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(b"TOPOPRG1")
            f.write(struct.pack("<HH", w, h))
            f.write(inter.tobytes())
        os.replace(tmp, self.path)

# ---- DEM --------------------------------------------------------------------
def fetch_dem(bbox, z, tier=None):
    lat_s, lon_w, lat_n, lon_e = bbox
    x0f, y1f = lonlat_to_tilef(lon_w, lat_s, z)
    x1f, y0f = lonlat_to_tilef(lon_e, lat_n, z)
    n_t = 2 ** z                             # whole-world bboxes land exactly on
    x0, x1 = max(0, int(x0f)), min(n_t - 1, int(x1f))   # the n boundary — clamp
    y0, y1 = max(0, int(y0f)), min(n_t - 1, int(y1f))
    # TIER SNAP: tier-K packs have msy = 4^-K exactly, i.e. 2^(z-2K) tile
    # rows — always an integer. Same-tier packs then share the atlas's scale
    # grid (uniform 4x handoffs, per-tier recipes enforceable). Rows adjust
    # symmetrically about the requested centre; columns (aspect) stay free.
    if tier is not None:
        want = max(1, 2 ** (z - 2 * tier))
        have = y1 - y0 + 1
        if have != want:
            mid = (y0 + y1 + 1) // 2
            ny0 = max(0, min(n_t - want, mid - want // 2))
            print(f"  tier {tier}: rows {have} -> {want} (msy=4^-{tier})")
            y0, y1 = ny0, ny0 + want - 1
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    W, H = nx * 256, ny * 256
    print(f"zoom {z}: tiles x {x0}..{x1} ({nx})  y {y0}..{y1} ({ny})  -> {W}x{H}px")
    mosaic = np.zeros((H, W), dtype=np.float32)
    total, done = nx * ny, 0
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            rgb = np.asarray(fetch_tile(z, tx, ty), dtype=np.float32)
            mosaic[(ty - y0) * 256:(ty - y0 + 1) * 256,
                   (tx - x0) * 256:(tx - x0 + 1) * 256] = \
                rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0
            done += 1
            if PROG:
                PROG.set(0.70 * done / total, f"tiles {done}/{total}")
            if done % 20 == 0 or done == total:
                print(f"  {done}/{total} tiles")
    # terrarium void/spike sanitizer (audit: -1213m hole at sawtooth-region,
    # +1139m spike neighbor): a cell >800m off its 8-neighbor envelope is a
    # data hole, not terrain — repair with the neighbor mean. Memory-light
    # running min/max/sum over shifted views (no 9-layer stack).
    p = np.pad(mosaic, 1, mode="edge")
    nmin = np.full((H, W), np.inf, np.float32)
    nmax = np.full((H, W), -np.inf, np.float32)
    nsum = np.zeros((H, W), np.float32)
    for dy in range(3):
        for dx in range(3):
            if dy == 1 and dx == 1:
                continue
            v = p[dy:dy + H, dx:dx + W]
            np.minimum(nmin, v, out=nmin)
            np.maximum(nmax, v, out=nmax)
            nsum += v
    bad = (mosaic > nmax + 800.0) | (mosaic < nmin - 800.0)
    if bad.any():
        mosaic[bad] = (nsum / 8.0)[bad]
        print(f"  dem sanitizer: {int(bad.sum())} void/spike texel(s) repaired")
    meta = {"zoom": z, "x0": x0, "y0": y0, "nx": nx, "ny": ny, "W": W, "H": H,
            "bbox_req": list(bbox),
            "bbox": [tiley_to_lat(y1 + 1, z), x0 / 2**z * 360.0 - 180.0,
                     tiley_to_lat(y0, z), (x1 + 1) / 2**z * 360.0 - 180.0]}
    return mosaic, meta                     # north-first rows, meters

def pick_step(span_m):
    # halving/doubling family {15,30,60,120,240}: coarser packs' contour lines
    # stay a subset of finer packs' (absolute-elevation referenced)
    step = 60.0
    while span_m / step < 22.0 and step > 15.0:
        step /= 2.0
    while span_m / step > 45.0 and step < 240.0:
        step *= 2.0
    return step

# ---- feature rasterization --------------------------------------------------
SS = 2                                       # supersample before SDF

def way_pts(el, to_px):
    return [to_px(g["lon"], g["lat"]) for g in el.get("geometry", [])]

def draw_ways(draw, pts, width_ss, ticks=False, dash=None):
    p = [(x * SS, y * SS) for x, y in pts]
    if len(p) < 2:
        return
    if dash:
        on, off = dash[0] * SS, dash[1] * SS
        period = on + off
        carry = 0.0
        for (x1, y1), (x2, y2) in zip(p, p[1:]):
            seg = math.hypot(x2 - x1, y2 - y1)
            if seg < 1e-6:
                continue
            ux, uy = (x2 - x1) / seg, (y2 - y1) / seg
            d = 0.0
            while d < seg:
                ph = (carry + d) % period
                if ph < on:                          # inside an "on" dash
                    run = min(on - ph, seg - d)
                    draw.line([(x1 + ux * d, y1 + uy * d),
                               (x1 + ux * (d + run), y1 + uy * (d + run))],
                              fill=255, width=width_ss)
                else:
                    run = min(period - ph, seg - d)
                d += run
            carry = (carry + seg) % period
        return
    draw.line(p, fill=255, width=width_ss)
    if not ticks:
        return
    # tower crossbars: perpendicular ticks every ~30 texels along the line
    spacing, half = 30.0 * SS, 4.0 * SS
    carry = spacing * 0.5
    for (x1, y1), (x2, y2) in zip(p, p[1:]):
        seg = math.hypot(x2 - x1, y2 - y1)
        if seg < 1e-6:
            continue
        ux, uy = (x2 - x1) / seg, (y2 - y1) / seg
        px, py = -uy, ux
        d = carry
        while d <= seg:
            cx, cy = x1 + ux * d, y1 + uy * d
            draw.line([(cx - px * half, cy - py * half),
                       (cx + px * half, cy + py * half)], fill=255, width=SS + 1)
            d += spacing
        carry = d - seg

def chamfer(inside):
    """~Euclidean distance (texels) to the True region, via iterative 8-neigh min."""
    d = np.where(inside, 0.0, 1e6).astype(np.float32)
    for _ in range(40):
        p = np.pad(d, 1, constant_values=1e6)
        d = np.minimum.reduce([
            d,
            p[0:-2, 1:-1] + 1, p[2:, 1:-1] + 1, p[1:-1, 0:-2] + 1, p[1:-1, 2:] + 1,
            p[0:-2, 0:-2] + 1.4, p[0:-2, 2:] + 1.4, p[2:, 0:-2] + 1.4, p[2:, 2:] + 1.4])
    return d

def sdf8(img, W, H):
    m = np.asarray(img.resize((W, H), Image.BILINEAR), dtype=np.float32)
    return np.clip(chamfer(m > 90.0) * 8.0, 0, 255).astype(np.uint8)

def feat_roads_region(bbox_s, to_px, W, H, classes="trunk|primary"):
    # regional scale: interstates + through-highways; park packs add
    # "secondary" so their scenic loop roads (Grand Loop, Going-to-the-Sun,
    # Teton Park Rd) show up — those are the roads that MAKE those maps
    q = f'''[out:json][timeout:120];(
      way["highway"="motorway"]{bbox_s};
      way["highway"~"^({classes})$"]{bbox_s};
    );out geom;'''
    try:
        d = overpass(q)
    except OverpassLoadShed as e:
        # a road-dense continental box (alps z8) can't be fetched in one
        # query; a roadless rung beats a failed chain. LOUD, not silent.
        print(f"  roads: overpass shed the box — building WITHOUT roads ({e})")
        d = {"elements": []}
    imgL = Image.new("L", (W * SS, H * SS), 0)
    imgA = Image.new("L", (W * SS, H * SS), 0)
    dL, dA = ImageDraw.Draw(imgL), ImageDraw.Draw(imgA)
    n = {"L": 0, "A": 0}
    for el in d.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        if el.get("tags", {}).get("highway") == "motorway":
            draw_ways(dA, way_pts(el, to_px), 4); n["A"] += 1
        else:
            draw_ways(dL, way_pts(el, to_px), 3); n["L"] += 1
    print(f"region roads: {n['L']} trunk/primary ways, {n['A']} motorway ways")
    styleL = (*PEACH, 0.7, 1.7, 0.50)
    styleA = (*PEACH, 1.2, 2.6, 0.55)
    return imgL, imgA, styleL, styleA, []

STATELINE = (0.502, 0.529, 0.635)            # Macchiato overlay1 — quiet borders

def feat_states(bbox_s, to_px, W, H):
    # state lines (admin_level=4 ways), drawn DASHED like a real small-scale map
    q = f'''[out:json][timeout:180];
      way["boundary"="administrative"]["admin_level"="4"]{bbox_s};out geom;'''
    d = overpass(q)
    imgL = Image.new("L", (W * SS, H * SS), 0)
    imgA = Image.new("L", (W * SS, H * SS), 0)
    dL = ImageDraw.Draw(imgL)
    n = 0
    for el in d.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        draw_ways(dL, way_pts(el, to_px), 3, dash=(10, 7))
        n += 1
    print(f"state lines: {n} boundary ways (dashed)")
    styleL = (*STATELINE, 1.0, 2.2, 0.60)
    styleA = (*PEACH, 1.2, 2.6, 0.0)         # A channel unused at this scale
    return imgL, imgA, styleL, styleA, []

# ---- water (lakes/reservoirs: natural=water ways + multipolygon relations) --
def _stitch_rings(ways):
    """Join OSM member-way segments into closed rings by shared endpoints."""
    pool = [list(w) for w in ways if len(w) >= 2]
    rings = []
    while pool:
        ring = pool.pop()
        for _ in range(len(pool) + 1):
            if len(ring) > 2 and ring[0] == ring[-1]:
                break
            for i, seg in enumerate(pool):
                if seg[0] == ring[-1]:
                    ring += seg[1:]; pool.pop(i); break
                if seg[-1] == ring[-1]:
                    ring += seg[-2::-1]; pool.pop(i); break
                if seg[-1] == ring[0]:
                    ring = seg[:-1] + ring; pool.pop(i); break
                if seg[0] == ring[0]:
                    ring = seg[::-1][:-1] + ring; pool.pop(i); break
            else:
                break                        # no joinable segment left
        if len(ring) > 3 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings

def signed_from_mask(inside):
    """Boolean water mask -> signed shore-distance byte map (<128 = water)."""
    if not inside.any():
        return None
    return np.clip(128.0 + chamfer(inside) * 8.0 - chamfer(~inside) * 8.0,
                   0, 255).astype(np.uint8)

# ---- TIER RECIPES: the atlas consistency principle, made structural --------
# Tier K packs have msy = 4^-K exactly (rows = 2^(z-2K)); same tier => same
# content vocabulary, so overlapping packs agree where they overlap (roads,
# labels, water). --tier applies these as DEFAULTS; explicit flags win.
# Earth (K0) and the conus/r6 ceiling band (~K1.5) stay bespoke.
TIERS = {
    2: dict(features="states", labels="peaks,states",
            peak_min_ele=3500.0, peak_top=13, peak_sep=100,
            water="dem"),                    # continental (rockies-class)
    3: dict(features="roads-region", roads_classes="trunk|primary",
            labels="peaks,places-region", peak_top=8, peak_sep=90,
            water="osm"),                    # regional
    4: dict(features="roads-region", roads_classes="trunk|primary",
            labels="peaks,places-region", peak_top=8, peak_sep=80,
            water="osm"),                    # area
    5: dict(features="roads-region", roads_classes="trunk|primary|secondary",
            labels="peaks,places", peak_top=8, peak_sep=28,
            water="osm"),                    # local (a town and its hills)
    6: dict(features="ski", labels="peaks,lifts", peak_top=6, peak_sep=120,
            water="osm"),                    # site: a ski resort
}

# ---- DEM voids: a second elevation authority ----------------------------
# Terrarium (AWS terrain tiles) has holes where its SRTM-era pipeline
# water-masked land: Isle Royale's NE half is solid lake-level at z11-z13
# (the island is simply not in the dataset) and the z9/z10 products flatten
# it too. Copernicus GLO-30 (radar-derived, keyless AWS open data, 1x1 deg
# float32 COGs) is an independent DEM that does have it. A void is a 256px
# source tile whose OSM-land pixels lie flat at that tile's own water level;
# its land is refilled from Copernicus (full replace — the seam to a good
# neighbour is a modest DSM/DTM offset, never a cliff).
COP_DIR = str(paths.cache_dir() / "copernicus")
_COP = {}


def _cop_tile(lat0, lon0):
    """Copernicus GLO-30 tile (SW corner lat0,lon0) as float32, row 0 = north
    edge; None where the dataset has no tile (open ocean squares)."""
    key = (lat0, lon0)
    if key in _COP:
        return _COP[key]
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    name = (f"Copernicus_DSM_COG_10_{ns}{abs(lat0):02d}_00_"
            f"{ew}{abs(lon0):03d}_00_DEM")
    os.makedirs(COP_DIR, exist_ok=True)
    path = f"{COP_DIR}/{name}.tif"
    if not os.path.exists(path):
        url = f"https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"
        try:
            req = urllib.request.Request(
                url, headers=UA)
            with urllib.request.urlopen(req, timeout=300) as r, \
                    open(path + ".tmp", "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            os.replace(path + ".tmp", path)
        except Exception as exc:
            print(f"  copernicus {name}: unavailable ({exc})")
            _COP[key] = None
            return None
    try:
        _COP[key] = np.asarray(Image.open(path), np.float32)
    except Exception as exc:
        print(f"  copernicus {name}: decode failed ({exc})")
        _COP[key] = None
    return _COP[key]


def repair_dem_voids(elev, wmask, meta):
    """Refill terrarium void tiles (see above) from Copernicus. wmask = the
    pack's water (OSM + marine); land = ~wmask. Returns a new elev."""
    H, W = elev.shape
    z, x0, y0 = meta["zoom"], meta["x0"], meta["y0"]
    n = 2 ** z
    land = ~wmask
    out = elev.copy()
    fixed = 0
    py, px = np.mgrid[0:256, 0:256]
    # a tile with no water of its own levels against the pack's dominant
    # water (an island's interior tile — Isle Royale's spine — has no lake
    # pixels to compare with, yet it is exactly where the hole lives)
    glvl = float(np.median(elev[wmask])) if wmask.any() else None
    for ty in range(H // 256):
        for tx in range(W // 256):
            ys, xs = slice(ty * 256, (ty + 1) * 256), slice(tx * 256, (tx + 1) * 256)
            e, l = elev[ys, xs], land[ys, xs]
            nl = int(l.sum())
            wv = e[~l]
            if nl < 1500:
                continue                         # no land to repair
            if wv.size >= 200:
                lvl = float(np.median(wv))
            elif glvl is not None:
                lvl = glvl
            else:
                continue                         # nothing to level against
            # a void tile is a CONSTANT (bit-exact) fill at water level; a flat
            # valley floor at z15 (Steamboat's Yampa bottomland, 40% within
            # 2.5 m of the river) is not — keep the band tight
            flat = float((np.abs(e[l] - lvl) < 0.6).mean())
            if flat < 0.35:
                continue                         # beaches/marsh are small; this is a hole
            lon = ((x0 + tx) + (px + 0.5) / 256.0) / n * 360.0 - 180.0
            lat = np.degrees(np.arctan(np.sinh(
                np.pi * (1.0 - 2.0 * ((y0 + ty) + (py + 0.5) / 256.0) / n))))
            cop = np.full((256, 256), np.nan, np.float32)
            for la0 in np.unique(np.floor(lat)):
                for lo0 in np.unique(np.floor(lon)):
                    t = _cop_tile(int(la0), int(lo0))
                    if t is None:
                        continue
                    sel = (np.floor(lat) == la0) & (np.floor(lon) == lo0)
                    th, tw = t.shape
                    r = np.clip((la0 + 1.0 - lat[sel]) * th - 0.5, 0, th - 1.001)
                    c = np.clip((lon[sel] - lo0) * tw - 0.5, 0, tw - 1.001)
                    r0 = np.floor(r).astype(int); c0 = np.floor(c).astype(int)
                    fr, fc = r - r0, c - c0
                    cop[sel] = (t[r0, c0] * (1 - fr) * (1 - fc) + t[r0 + 1, c0] * fr * (1 - fc)
                                + t[r0, c0 + 1] * (1 - fr) * fc + t[r0 + 1, c0 + 1] * fr * fc)
            ok = l & np.isfinite(cop)
            if ok.sum() < nl * 0.5:
                print(f"  dem void tile ({tx},{ty}): {flat*100:.0f}% of land flat at "
                      f"{lvl:.0f} m — Copernicus unavailable, left as is")
                continue
            patch = out[ys, xs]
            patch[ok] = cop[ok]
            fixed += 1
            unf = int((l & ~np.isfinite(cop)).sum())
            print(f"  dem void tile ({tx},{ty}): {flat*100:.0f}% of land flat at "
                  f"{lvl:.0f} m — refilled from Copernicus (max {np.nanmax(cop[ok]):.0f} m"
                  + (f", {unf} land px had no Copernicus data" if unf else "") + ")")
    if fixed:
        print(f"  dem voids repaired: {fixed} tile(s)")
    return out


def veto_water_by_dem(wmask, elev, tol=12.0, blk=64):
    """The DEM is the authority for LAND: a water pixel more than tol m above
    its local water level (64px-block median of water elevation) is a polygon
    error — a broken OSM ring flooding a peninsula, a dropped islet — and
    becomes land again. Rivers drop far less than tol across one block."""
    H, W = elev.shape
    Hp, Wp = (H + blk - 1) // blk * blk, (W + blk - 1) // blk * blk
    e = np.full((Hp, Wp), np.nan, np.float32)
    e[:H, :W] = np.where(wmask, elev, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        eb = e.reshape(Hp // blk, blk, Wp // blk, blk)
        med = np.nanmedian(eb, axis=(1, 3))
    # median + tol ONLY. A "real water is flat" refinement (min-based level
    # for high-spread blocks) was tried and REVERTED: terrarium carries lake
    # BATHYMETRY, so genuine Lake Superior blocks span hundreds of metres and
    # the rule un-watered squares of real lake into land (isle-royale-region
    # v3). The gentle rule catches shoreline bleed and dropped islets; large
    # false-water blobs need a source-side fix, not a DEM guess.
    lvl = np.repeat(np.repeat(med + tol, blk, 0), blk, 1)[:H, :W]
    bad = wmask & np.isfinite(lvl) & (elev > lvl)
    if bad.any():
        print(f"  water veto: {int(bad.sum())} px un-watered (land above local water level)")
    return wmask & ~bad


NE_LAKES_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
                "master/geojson/ne_10m_lakes.geojson")


def ne_lakes(to_px, W, H, min_px=6.0, label_top=4, label_min_px=600.0):
    """Continental water authority (apostle descent, 2026-09-08): --water-
    from-dem packs see only the OCEAN (terrarium bathymetry <= 0 m), so every
    lake above sea level — the Great Lakes, Winnipeg, the Great Salt Lake —
    was LAND at K<=2 and flashed into existence during a descent onto a lake
    pack. Natural Earth 10m lakes (public domain, one 5 MB GeoJSON, cached)
    rasterized into the DEM mask make the lakes exist at every scale; the
    biggest get spaced-caps names. Returns (bool mask, labels)."""
    path = f"{COP_DIR}/../ne_10m_lakes.geojson"
    path = os.path.normpath(path)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            req = urllib.request.Request(
                NE_LAKES_URL, headers=UA)
            data = urllib.request.urlopen(req, timeout=120).read()
            with open(path + ".part", "wb") as fh:
                fh.write(data)
            os.replace(path + ".part", path)
        except Exception as exc:
            print(f"  ne lakes: download failed ({exc}) — DEM water only")
            return np.zeros((H, W), bool), []
    g = json.load(open(path))
    mask = Image.new("L", (W * SS, H * SS), 0)
    dm = ImageDraw.Draw(mask)
    cands = []
    ndrawn = 0
    for f in g.get("features", []):
        geom = f.get("geometry") or {}
        polys = ([geom["coordinates"]] if geom.get("type") == "Polygon"
                 else geom.get("coordinates", []) if geom.get("type") == "MultiPolygon"
                 else [])
        area = 0.0
        bx0 = by0 = 1e18; bx1 = by1 = -1e18
        rings_px = []
        for poly in polys:
            for ri, ring in enumerate(poly):
                pts = [to_px(lo, la) for lo, la in ring]
                if ri == 0:
                    xs = [q[0] for q in pts]; ys = [q[1] for q in pts]
                    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                    if x1 < 0 or y1 < 0 or x0 > W or y0 > H:
                        continue
                    bx0, bx1 = min(bx0, x0), max(bx1, x1)
                    by0, by1 = min(by0, y0), max(by1, y1)
                    area += (x1 - x0) * (y1 - y0)
                rings_px.append((ri, pts))
        if area < min_px or not rings_px:
            continue
        for ri, pts in rings_px:
            if len(pts) > 2:
                dm.polygon([(x * SS, y * SS) for x, y in pts],
                           fill=255 if ri == 0 else 0)
        ndrawn += 1
        nm = f.get("properties", {}).get("name")
        if not nm or area < label_min_px:
            continue
        # TRUE area + centroid from the lake's own raster (a bbox ranks a
        # sinuous reservoir — Sakakawea, Diefenbaker — above the Great
        # Salt Lake; the pixels don't lie)
        x0c, x1c = int(max(0, bx0)), int(min(W, bx1)) + 2
        y0c, y1c = int(max(0, by0)), int(min(H, by1)) + 2
        if x1c - x0c < 2 or y1c - y0c < 2:
            continue
        one = Image.new("L", (x1c - x0c, y1c - y0c), 0)
        d1 = ImageDraw.Draw(one)
        for ri, pts in rings_px:
            if len(pts) > 2:
                d1.polygon([(x - x0c, y - y0c) for x, y in pts],
                           fill=255 if ri == 0 else 0)
        arr = np.asarray(one) > 0
        true_px = int(arr.sum())
        if true_px < label_min_px:
            continue
        ys, xs = np.nonzero(arr)
        cy, cx = float(ys.mean()), float(xs.mean())
        if not arr[int(cy), int(cx)]:        # crescent: centroid dry -> nearest wet
            j = np.argmin((ys - cy) ** 2 + (xs - cx) ** 2)
            cy, cx = float(ys[j]), float(xs[j])
        cands.append((true_px, nm, cx + x0c, cy + y0c))
    m = np.asarray(mask.resize((W, H), Image.BILINEAR), dtype=np.float32) > 127.0
    labels = []
    for area, nm, cx, cy in sorted(cands, reverse=True)[:label_top]:
        if not (0 <= cx < W and 0 <= cy < H):
            continue
        print(f"  lake label: {nm} ({area} px)")
        labels.append({"text": " ".join(nm.upper()), "glyph": "water",
                       "px": cx, "py": cy, "size": 13,
                       "hmin": 0.0, "hmax": 1e9})
    print(f"  ne lakes: {ndrawn} drawn ({m.mean()*100:.1f}% of the pack)")
    return m, labels


def marine_mask(elev):
    """Ocean for --water packs: below-sea-level cells CONNECTED TO THE BBOX
    EDGE. OSM maps lakes as natural=water polygons but the ocean only as a
    coastline tag, so a coastal pack built from polygons alone renders
    seafloor bathymetry as land (the dry-Pacific hawaii chain, 2026-08-27).
    The border flood fill keeps inland depressions (Death Valley class) out;
    --water-from-dem stays unconditional elev<=0 (earth needs the Caspian)."""
    below = elev <= 0.0
    if not below.any():
        return below
    marine = np.zeros_like(below)
    marine[0, :] = below[0, :];  marine[-1, :] = below[-1, :]
    marine[:, 0] = below[:, 0];  marine[:, -1] = below[:, -1]
    for _ in range(4096):                    # grows 1px/iter; cap >> any pack
        grown = marine.copy()
        grown[1:, :] |= marine[:-1, :]
        grown[:-1, :] |= marine[1:, :]
        grown[:, 1:] |= marine[:, :-1]
        grown[:, :-1] |= marine[:, 1:]
        grown &= below
        if (grown == marine).all():
            break
        marine = grown
    return marine

def feat_water(bbox_s, to_px, W, H):
    """water.bin source: filled OSM mask + names. Returns (bool_mask, labels)."""
    q = f'''[out:json][timeout:180];(
      way["natural"="water"]{bbox_s};
      relation["natural"="water"]{bbox_s};
    );out geom;'''
    d = overpass(q)
    mask = Image.new("L", (W * SS, H * SS), 0)
    dm = ImageDraw.Draw(mask)
    def px(g):
        return [(x * SS, y * SS) for x, y in (to_px(p["lon"], p["lat"]) for p in g)]
    names = []
    nply = 0
    nopen = 0
    for el in d.get("elements", []):
        t = el.get("tags", {})
        if el.get("type") == "way" and "geometry" in el:
            g = el["geometry"]
            # an OPEN way is a relation member (riverbank/lakeshore segment),
            # not an area — PIL would chord-close it across land (the
            # isle-royale-region Keweenaw flood). Members render via their
            # relation; skip the bare segment.
            if (g[0]["lon"], g[0]["lat"]) != (g[-1]["lon"], g[-1]["lat"]):
                nopen += 1
                continue
            p = px(g)
            if len(p) > 3:
                dm.polygon(p, fill=255); nply += 1
        elif el.get("type") == "relation" and "members" in el:
            outers, inners = [], []
            for m in el["members"]:
                if m.get("type") != "way" or "geometry" not in m:
                    continue
                coords = [(g["lon"], g["lat"]) for g in m["geometry"]]
                (outers if m.get("role") != "inner" else inners).append(coords)
            for ring in _stitch_rings(outers):
                p = [(x * SS, y * SS) for x, y in (to_px(lo, la) for lo, la in ring)]
                dm.polygon(p, fill=255); nply += 1
            for ring in _stitch_rings(inners):
                p = [(x * SS, y * SS) for x, y in (to_px(lo, la) for lo, la in ring)]
                dm.polygon(p, fill=0)
        nm = pick_name(t)
        if nm:
            gs = (el.get("geometry") or
                  [g for m in el.get("members", []) for g in m.get("geometry", [])])
            if gs:
                lons = [g["lon"] for g in gs]; lats = [g["lat"] for g in gs]
                # extent MIDPOINT, not vertex mean — a vertex-dense arm drags
                # the mean off the water (audit: HUNGRY HORSE ~40km adrift)
                cx, cy = to_px((max(lons) + min(lons)) / 2,
                               (max(lats) + min(lats)) / 2)
                ext = (max(lons)-min(lons)) * (max(lats)-min(lats))
                if 0 <= cx < W and 0 <= cy < H:
                    names.append((ext, nm, cx, cy))
    print(f"water: {nply} polygons filled"
          + (f" ({nopen} open way segments skipped)" if nopen else ""))
    m = np.asarray(mask.resize((W, H), Image.BILINEAR), dtype=np.float32)
    omask = m > 127.0
    labels = []
    # WORTHINESS (audit #18): spaced-caps billing is for waters that shape
    # the pack — floor at 0.35% of the bbox extent (stock ponds, washes and
    # backyard lakes fall silent; the label count stays <=2 as ever)
    bs, bw, bn, be = [float(x) for x in re.findall(r"-?[\d.]+", bbox_s)][:4]
    pext = max(1e-9, (be - bw) * (bn - bs))
    for ext, nm, cx, cy in sorted(names, reverse=True)[:2]:
        if ext < 0.0035 * pext:
            print(f"  water label: {nm} SKIPPED (trivial extent)")
            continue
        ix, iy = int(cx), int(cy)
        if not omask[iy, ix]:
            # midpoint landed dry (crescent lake) — snap to the nearest wet
            # pixel in a local window so the label sits ON its water
            y0w, y1w = max(0, iy - 60), min(H, iy + 61)
            x0w, x1w = max(0, ix - 60), min(W, ix + 61)
            win = omask[y0w:y1w, x0w:x1w]
            if win.any():
                wy, wx = np.nonzero(win)
                j = np.argmin((wy - (iy - y0w)) ** 2 + (wx - (ix - x0w)) ** 2)
                cx, cy = float(x0w + wx[j]), float(y0w + wy[j])
        print(f"  water label: {nm}")
        labels.append({"text": " ".join(nm.upper()), "glyph": "water",
                       "px": cx, "py": cy, "size": 13,
                       "hmin": 0.0, "hmax": 1e9})
    return omask, labels

LIFT_KINDS = {"chair_lift", "gondola", "mixed_lift", "platter", "t-bar", "magic_carpet"}

def feat_ski(bbox_s, to_px, W, H, msy):
    q = f'''[out:json][timeout:90];(
      way["aerialway"]{bbox_s};
      way["piste:type"="downhill"]{bbox_s};
    );out geom;'''
    d = overpass(q)
    imgL = Image.new("L", (W * SS, H * SS), 0)   # runs
    imgA = Image.new("L", (W * SS, H * SS), 0)   # lifts
    dL, dA = ImageDraw.Draw(imgL), ImageDraw.Draw(imgA)
    nrun = nlift = 0
    lift_labels = []
    cx_acc, cy_acc, n_acc = 0.0, 0.0, 0
    for el in d.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        t = el.get("tags", {})
        pts = way_pts(el, to_px)
        for x, y in pts:
            cx_acc += x; cy_acc += y; n_acc += 1
        if t.get("aerialway") in LIFT_KINDS:
            draw_ways(dA, pts, 4, ticks=(t.get("aerialway") != "magic_carpet"))
            nlift += 1
            if t.get("aerialway") == "chair_lift" and pick_name(t):
                # anchor at the arc-length midpoint of the line
                segs = [math.hypot(b[0]-a[0], b[1]-a[1]) for a, b in zip(pts, pts[1:])]
                tot = sum(segs)
                acc, mid = 0.0, pts[len(pts)//2]
                for (a, b), s in zip(zip(pts, pts[1:]), segs):
                    if acc + s >= tot * 0.5:
                        f = (tot * 0.5 - acc) / max(s, 1e-9)
                        mid = (a[0] + (b[0]-a[0]) * f, a[1] + (b[1]-a[1]) * f)
                        break
                    acc += s
                lift_labels.append({"text": pick_name(t), "glyph": "lift",
                                    "px": mid[0], "py": mid[1], "size": 12,
                                    "hmin": 0.0, "hmax": 0.9 * msy,
                                    "rank": tot})
        elif t.get("piste:type") == "downhill":
            draw_ways(dL, pts, 3)
            nrun += 1
    # ranked head: the long lifts (the mountain's spine) outrank the
    # bunny-hill chairs when a big resort (20+ lifts) hits the
    # PKMAX budget — the budget keeps a class's head, so the head must
    # be the important lifts, not OSM id order
    lift_labels.sort(key=lambda e: -e["rank"])
    focus = (0.5, 0.5)
    if n_acc:
        focus = (cx_acc / n_acc / W, 1.0 - cy_acc / n_acc / H)
        print(f"ski focus (uv): {focus[0]:.3f},{focus[1]:.3f}")
    print(f"ski: {nrun} runs, {nlift} lifts, {len(lift_labels)} lift labels")
    styleL = (*SAPPHIRE, 0.8, 1.9, 0.45)
    styleA = (*RED, 0.9, 2.1, 0.62)
    return imgL, imgA, styleL, styleA, lift_labels, focus

# ---- labels -----------------------------------------------------------------
S = 2                                        # label raster supersample (hi-dpi)

find_font = fonts.find_font

FONT_PATH = None

def render_label(text, glyph, size):
    font = ImageFont.truetype(FONT_PATH, size * S)
    pad, gap = 3 * S, 5 * S
    tb = font.getbbox(text)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    if glyph == "peak":
        gw, gh = 7 * S, 6 * S
    elif glyph == "place":
        gw = gh = 8 * S
    else:                                    # lift/state: text only
        gw = gh = 0
        gap = 0
    lw = pad + gw + gap + tw + pad
    lh = pad + max(th, gh) + pad
    timg = Image.new("L", (lw, lh), 0)
    himg = Image.new("L", (lw, lh), 0)
    dt, dh = ImageDraw.Draw(timg), ImageDraw.Draw(himg)
    ty = (lh - th) // 2 - tb[1]
    tx = pad + gw + gap
    cy = lh // 2
    shapes = []
    if glyph == "peak":
        shapes = [("poly", [(pad, cy + gh // 2), (pad + gw, cy + gh // 2),
                            (pad + gw // 2, cy - gh // 2)])]
        ax, ay = pad + gw // 2, cy - gh // 2
    elif glyph == "place":
        cr = gw // 2
        shapes = [("ellipse", [pad, cy - cr, pad + 2 * cr, cy + cr])]
        ax, ay = pad + cr, cy
    elif glyph in ("state", "water"):
        ax, ay = lw // 2, cy                 # anchor = text centre (name floats)
    else:
        ax, ay = 0, cy                       # lift: anchor = left-centre of text
    for ox in (-S, 0, S):
        for oy in (-S, 0, S):
            dh.text((tx + ox, ty + oy), text, font=font, fill=255)
            for kind, sh in shapes:
                if kind == "poly":
                    dh.polygon([(a + ox, b + oy) for a, b in sh], fill=255)
                else:
                    dh.ellipse([sh[0]+ox, sh[1]+oy, sh[2]+ox, sh[3]+oy],
                               outline=255, width=2 * S)
    dt.text((tx, ty), text, font=font, fill=255)
    for kind, sh in shapes:
        if kind == "poly":
            dt.polygon(sh, fill=255)
        else:
            dt.ellipse(sh, outline=255, width=S)
    return np.asarray(timg), np.asarray(himg), ax, ay

def pick_name(t):
    """Bakeable name: prefer name:en, else int_name, else the default name —
    passed through the FONT-TRUTH gate (bakeable). None beats boxes."""
    for k in ("name:en", "int_name", "name"):
        nm = t.get(k)
        if nm:
            b = bakeable(nm)
            if b:
                return b
    return None

# ---- FONT-TRUTH glyph gate (structural, not disciplinary) ------------------
# The old latin_ok() whitelisted codepoint RANGES and hoped the font covered
# them — it whitelisted U+02B0-02FF for the Hawaiian ʻokina, but JetBrainsMono
# has NO U+02BB glyph, so every PUʻU label baked a .notdef box (caught on
# ʻĀlika Cone, 2026-08-28). The font's own cmap is the only truth: every
# character must exist in it, with a per-char fallback chain —
#   cmap hit -> typographic substitute -> NFD base fold -> drop label (loud).
FONT_CMAP = None
AUTO = False                              # search-built pack (subject pull etc.)
GLYPH_SUBS = {0x02BB: "’", 0x2018: "’"}   # ʻokina -> ’ (standard)

def load_font_cmap():
    global FONT_CMAP
    try:
        from fontTools.ttLib import TTFont
        FONT_CMAP = set(TTFont(FONT_PATH).getBestCmap().keys())
        print(f"  glyph gate: {len(FONT_CMAP)} codepoints in {os.path.basename(FONT_PATH)}")
    except Exception as e:
        FONT_CMAP = None
        print(f"  glyph gate OFF (fontTools: {e}) — labels unverified")

def bakeable(s):
    """Return s with every char verified against the font cmap (substituting
    or folding where possible), or None if any char has no renderable form."""
    if FONT_CMAP is None:
        return s
    out = []
    for ch in s:
        o = ord(ch)
        if o in FONT_CMAP:
            out.append(ch)
            continue
        sub = GLYPH_SUBS.get(o)
        if sub and all(ord(c) in FONT_CMAP for c in sub):
            out.append(sub)
            continue
        base = "".join(c for c in unicodedata.normalize("NFD", ch)
                       if not unicodedata.combining(c) and ord(c) in FONT_CMAP)
        if base:
            out.append(base)
            continue
        print(f"  glyph gate: dropped label with unbakeable {ch!r} (U+{o:04X}) in {s!r}")
        return None
    return "".join(out)

_TZF = None


def find_tzname(lat, lon):
    """IANA timezone for a point, via the offline timezonefinder package
    (optional dependency). Baked into meta.bin so the engine's far-roam chip
    gets quarter-hour zones and DST exactly. Fail-soft to '' -> the engine
    falls back to longitude/15."""
    global _TZF
    try:
        from timezonefinder import TimezoneFinder
        if _TZF is None:
            _TZF = TimezoneFinder()
        return _TZF.timezone_at(lat=lat, lng=lon) or ""
    except Exception as exc:
        print(f"  tz lookup unavailable ({exc}) — chip will use lon/15")
        return ""


def budget_labels(entries, cap=24):
    """Bake-side roster budget (audit #19): the engine holds PKMAX=24 labels
    per pack, so a pack must RANK its way under the cap — head-truncation of
    an unranked roster is how conus ended up as twelve Mexican states. Class
    quotas in visual-priority order, order preserved within class (every
    fetcher emits ranked heads)."""
    if len(entries) <= cap:
        return entries
    order = {"peak": 0, "place": 1, "water": 2, "state": 3, "geyser": 4}
    quotas = {"peak": 10, "place": 8, "water": 3, "state": 8, "geyser": 6}
    keep = []
    classes = sorted({e["glyph"] for e in entries}, key=lambda c: order.get(c, 9))
    pools = {cls: [e for e in entries if e["glyph"] == cls] for cls in classes}
    for cls in classes:
        keep += pools[cls][:quotas.get(cls, 4)]
    # quotas are FLOORS: capacity the sparse classes leave unused flows to
    # the others in priority order (conus: 6 peaks + 3 lakes left 15 slots,
    # not 8, for state names)
    for cls in classes:
        for e in pools[cls][quotas.get(cls, 4):]:
            if len(keep) >= cap:
                break
            keep.append(e)
    keep = keep[:cap]
    print(f"  label budget: {len(entries)} -> {len(keep)} (cap {cap})")
    return keep


def declutter(entries, top_n, sep_px, key):
    """Greedy: keep by descending priority, dropping anything nearer than
    sep_px to an already-kept label. top_n<=0 or sep_px<=0 -> keep all."""
    if top_n <= 0 and sep_px <= 0:
        return entries
    entries = sorted(entries, key=key, reverse=True)
    kept = []
    for e in entries:
        if sep_px > 0 and any((e["px"]-k["px"])**2 + (e["py"]-k["py"])**2
                              < sep_px*sep_px for k in kept):
            continue
        kept.append(e)
        if top_n > 0 and len(kept) >= top_n:
            break
    return kept

PEAK_BOX_MAX = 20.0        # degrees: bigger boxes are split before asking

def _peaks_fetch(box, ef, depth=0):
    # continental boxes die in the SCAN phase no matter the (if:) filter —
    # quadtree-split until each box fits the server's budget: up front for
    # boxes over PEAK_BOX_MAX, and whenever a box is shed or keeps timing
    # out (a gateway timeout on a busy mirror is a shed by another name)
    s, w, n, e = box
    # peak OR volcano: the great volcanoes (Shasta, Mauna Kea, Mauna Loa,
    # Haleakalā...) are natural=volcano and were invisible to a peak-only
    # query — every "iconic name lost to a summit cone" in the audit traces
    # to this one missing tag
    q = (f'[out:json][timeout:120];node["natural"~"^(peak|volcano)$"]'
         f'["name"]{ef}({s:.5f},{w:.5f},{n:.5f},{e:.5f});out;')
    big = max(n - s, e - w) > PEAK_BOX_MAX and depth < 3
    try:
        if big:
            raise OverpassLoadShed("box too big to ask in one go")
        return overpass(q, tries=7 if depth >= 3 else 3).get("elements", [])
    except (OverpassLoadShed, OSError) as exc:      # OSError: timeouts, 5xx
        if depth >= 3:
            raise
        print(f"  peaks: {n-s:.0f}x{e-w:.0f} deg box not answered ({exc}), "
              f"splitting (depth {depth})", flush=True)
        cy, cx = (s + n) / 2, (w + e) / 2
        els, seen = [], set()
        for sb in ((s, w, cy, cx), (s, cx, cy, e),
                   (cy, w, n, cx), (cy, cx, n, e)):
            for el in _peaks_fetch(sb, ef, depth + 1):
                if el["id"] not in seen:
                    seen.add(el["id"])
                    els.append(el)
        return els

def labels_peaks(bbox_s, to_px, elev, meta, min_ele, top_n=0, sep_px=0,
                 msy=0.0):
    # min-ele filtered SERVER-SIDE: it can't rescue the scan phase (see
    # _peaks_fetch), but it keeps the continental PAYLOAD sane
    ef = f'(if:number(t["ele"])>={min_ele:.0f})' if min_ele > 0 else ''
    d = {"elements": _peaks_fetch(tuple(meta["bbox"]), ef)}
    W, H = meta["W"], meta["H"]
    # snap radius ~250m of ground, not a fixed texel count (12px was 160m at
    # z13 but would be 10km at z7 — enough to jump to the wrong mountain)
    lat_mid = 0.5 * (meta["bbox"][0] + meta["bbox"][2])
    res_m = 40075016.0 * math.cos(math.radians(lat_mid)) / (2 ** meta["zoom"] * 256)
    R = max(2, min(12, int(round(250.0 / res_m))))
    # elevation truth is resolution-dependent: a fine DEM (z11+) beats OSM tags
    # (v6: DEM matched survey within ~15ft), but a coarse DEM flattens summits
    # and lets a neighbour's cell outrank the true highpoint (z7 dropped Elbert
    # for Massive's cell) — so below ~60m/px, label + rank by the survey tag
    use_dem = res_m < 60.0  # noqa: F841  (kept for the ranking note below)
    # the implausibility guard exists for GARBAGE tags (a "Spring Benchmark"
    # node claimed 23,290m), not for DEM flattening: a coarse cell averages
    # away sharp summits (z6 shaved 728m off Everest, 1800m off Annapurna),
    # so the tolerance must grow with cell size
    ele_guard = 600.0 + 0.75 * res_m
    out = []
    for el in d.get("elements", []):
        t = el.get("tags", {})
        try:
            osm_ele = float(t.get("ele", "nan"))
        except ValueError:
            osm_ele = float("nan")
        # min-ele 0 keeps untagged peaks too (auto-packs rank purely by DEM)
        if min_ele > 0 and (not (osm_ele == osm_ele) or osm_ele < min_ele):
            continue
        nm_ok = pick_name(t)
        if not nm_ok:
            continue
        px, py = to_px(el["lon"], el["lat"])
        xi, yi = int(round(px)), int(round(py))
        if not (0 <= xi < W and 0 <= yi < H):
            continue
        x0, x1 = max(0, xi - R), min(W, xi + R + 1)
        y0, y1 = max(0, yi - R), min(H, yi + R + 1)
        win = elev[y0:y1, x0:x1]
        dy, dx = np.unravel_index(np.argmax(win), win.shape)
        sx, sy = x0 + dx, y0 + dy
        ele_m = float(elev[sy, sx])
        if osm_ele == osm_ele and abs(osm_ele - ele_m) > ele_guard:
            print(f"  SKIP {t['name']}: implausible ele tag {osm_ele:.0f}m "
                  f"vs DEM {ele_m:.0f}m")
            continue
        # ONE TRUTH (audit task #17): the label elevation is the survey tag
        # whenever a plausible one exists, at EVERY tier — DEM only fills
        # gaps. All rungs of a peak then agree by construction (no more
        # 28,717' Everest vs 29,032', no peak morphing through three
        # values mid-crossfade). use_dem still governs placement/ranking.
        ele_lbl = osm_ele if (osm_ele == osm_ele
                              and abs(osm_ele - ele_m) <= ele_guard) else ele_m
        hgt = (f"{int(round(ele_lbl)):,} m" if METRIC
               else f"{int(round(ele_lbl * 3.28084)):,}'")
        name = nm_ok.upper()
        for a, b in (("MOUNTAIN", "MTN"), (" PEAK", " PK")):
            name = name.replace(a, b)
        dn = 2.0 * math.hypot((sx - W / 2.0) / W, (sy - H / 2.0) / H)
        out.append({"text": f"{name} · {hgt}", "glyph": "peak",
                    "px": sx + 0.5, "py": sy + 0.5, "size": 13,
                    "hmin": 0.0, "hmax": 1e9, "name": nm_ok,
                    "ele_dem": ele_m, "ele_osm": osm_ele, "ele_pri": ele_lbl,
                    "wiki": bool(t.get("wikipedia") or t.get("wikidata")),
                    "dn": dn,
                    "snap": (abs(sx-xi), abs(sy-yi))})
    # two-tier declutter (OPT-IN via msy>0): close-cluster labels get LOD-gated
    # below the idle band. LESSON (Tetons, 2026-08-27): a gate INSIDE the idle
    # breathing band (0.34..0.62 msy) opens on every deep breath and the boxes
    # still collide — and there is no reachable height BELOW the band within a
    # pack, so within-pack gates are usually dead weight. Gates earn their keep
    # only across scales (lift names). For tight massifs use hard separation.
    # notability tiebreak: a wikipedia/wikidata-tagged peak outranks an
    # untagged near-equal (+180m — settles Mauna Kea vs its Puuwekiu summit
    # cone without letting any anonymous 4000er lose to a famous foothill).
    # SUBJECT PULL (audit #19): a pack is ABOUT its centre — corner giants
    # pay a distance penalty so the eponymous range wins the roster (strong
    # for auto/synth boxes, gentle for curated ones; sawtooth-range was
    # labelling the White Clouds across the valley).
    dn_w = 550.0 if AUTO else 150.0
    key = lambda e: (e["ele_pri"] + (180.0 if e.get("wiki") else 0.0)
                     - dn_w * e.get("dn", 0.0))
    fine = declutter(out, top_n, sep_px, key=key)
    far_sep = max(sep_px, 0.06 * meta["H"])
    far = declutter(fine, top_n, far_sep, key=key)
    far_ids = {id(e) for e in far}
    for e in fine:
        if id(e) not in far_ids and msy > 0:
            e["hmax"] = 0.45 * msy
    out = fine
    for e in out:
        gate = "" if e["hmax"] > 8e8 else "  [zoom-gated]"
        print(f"  peak {e['name']:24s} label {e['ele_pri']:6.0f}m "
              f"({int(round(e['ele_pri']*3.28084)):,}')  DEM {e['ele_dem']:6.0f}m"
              f"  snap {e['snap'][0]},{e['snap'][1]}px{gate}")
    return out

def labels_states(to_px, meta):
    """State names from place=state nodes INSIDE the pack (recon 2026-08-27:
    all six Rockies-overlap states have in-bbox nodes; states that end exactly
    at the bbox edge — NM/AZ at 37N, ND/SD at 104W — self-exclude)."""
    bs, bw, bn, be = meta["bbox"]
    q = (f'[out:json][timeout:60];node["place"="state"]'
         f'({bs:.5f},{bw:.5f},{bn:.5f},{be:.5f});out;')
    d = overpass(q)
    W, H = meta["W"], meta["H"]
    out = []
    for el in d.get("elements", []):
        nm = el.get("tags", {}).get("name")
        if not nm:
            continue
        px, py = to_px(el["lon"], el["lat"])
        if not (0 <= px < W and 0 <= py < H):
            continue
        out.append({"text": " ".join(nm.upper()), "glyph": "state",
                    "px": px, "py": py, "size": 15,
                    "hmin": 0.0, "hmax": 1e9,
                    "dn": 2.0 * math.hypot((px - W / 2) / W, (py - H / 2) / H)})
    # centre-out ranking (audit #19: conus's unranked state roster head-
    # truncated to twelve Mexican states and none of the US ones — the pack's
    # subject lives at its centre, edge states yield first)
    out.sort(key=lambda e: e["dn"])
    # SPREAD (conus regen 2026-09-08): centre-out alone filled the budget
    # with eight Midwest neighbours (Iowa..Wisconsin) and left the West
    # bald. Greedy separation at ~9% of the pack width keeps the centre-
    # first order but walks outward evenly — the names tile the map.
    out = declutter(out, 0, 0.09 * max(W, H), key=lambda e: -e["dn"])
    for e in out:
        print(f"  state {e['text'].replace(' ', '')} (dn {e['dn']:.2f})")
    return out

def labels_geysers(bbox_s, to_px, meta, top_n=6, sep_px=50):
    """Named geysers (Yellowstone pack): circle glyph, title text. The greedy
    keep is by name-length-then-alphabet — no better prominence signal in OSM,
    and it happens to put Old Faithful in front of obscure spouters."""
    q = f'[out:json][timeout:60];node["natural"="geyser"]["name"]{bbox_s};out;'
    d = overpass(q)
    W, H = meta["W"], meta["H"]
    FAMOUS = {"Old Faithful": 9, "Steamboat Geyser": 8, "Grand Geyser": 7,
              "Castle Geyser": 6, "Riverside Geyser": 5, "Great Fountain Geyser": 5}
    out = []
    for el in d.get("elements", []):
        nm = el.get("tags", {}).get("name")
        if not nm:
            continue
        px, py = to_px(el["lon"], el["lat"])
        if not (0 <= px < W and 0 <= py < H):
            continue
        out.append({"text": nm.replace(" Geyser", ""), "glyph": "place",
                    "px": px, "py": py, "size": 12,
                    "hmin": 0.0, "hmax": 1e9,
                    "rank": (FAMOUS.get(nm, 0), -len(nm))})
    out = declutter(out, top_n, sep_px, key=lambda e: e["rank"])
    for e in out:
        print(f"  geyser {e['text']}")
    return out

def labels_world(to_px, meta):
    """Ocean + continent names (place=ocean/continent nodes) for the earth
    pack — spaced caps, always visible; the sphere shows ~4 at a time."""
    q = ('[out:json][timeout:60];('
         'node["place"="ocean"];node["place"="continent"];);out;')
    d = overpass(q)
    W, H = meta["W"], meta["H"]
    out = []
    for el in d.get("elements", []):
        t = el.get("tags", {})
        nm = pick_name(t)
        if not nm or len(nm) > 24:
            continue
        px, py = to_px(el["lon"], el["lat"])
        if not (0 <= px < W and 0 <= py < H):
            continue
        print(f"  world: {nm} ({t.get('place')})")
        out.append({"text": " ".join(nm.upper()), "glyph": "state",
                    "px": px, "py": py,
                    "size": 14 if t.get("place") == "continent" else 12,
                    "hmin": 0.0, "hmax": 1e9})
    return out

def labels_places_region(bbox_s, to_px, meta, top_n=10, sep_px=60):
    """Regional scale: cities + towns only, city > town > population priority,
    decluttered by separation."""
    q = f'[out:json][timeout:60];node["place"~"^(city|town)$"]{bbox_s};out;'
    try:
        d = overpass(q)
    except OverpassLoadShed as e:
        print(f"  places: overpass shed the box — building WITHOUT towns ({e})")
        d = {"elements": []}
    W, H = meta["W"], meta["H"]
    out = []
    for el in d.get("elements", []):
        t = el.get("tags", {})
        nm = pick_name(t)
        if not nm:
            continue
        px, py = to_px(el["lon"], el["lat"])
        if not (0 <= px < W and 0 <= py < H):
            continue
        try:
            pop = int(t.get("population", "0").replace(",", ""))
        except ValueError:
            pop = 0
        out.append({"text": nm, "glyph": "place", "px": px, "py": py,
                    "size": 13, "hmin": 0.0, "hmax": 1e9,
                    "rank": (1 if t.get("place") == "city" else 0, pop)})
    out = declutter(out, top_n, sep_px, key=lambda e: e["rank"])
    for e in out:
        print(f"  place {e['text']} (rank {e['rank']})")
    return out


PLACE_CLASS = {"city": 3, "town": 2, "village": 1, "hamlet": 0}

def labels_places(bbox_s, to_px, meta):
    # Ranked + decluttered like the -region variant: unbounded, a dense box
    # (Lombardy) bakes hundreds of villages and blows the engine's PKMAX=24
    # label contract (alps came out 283 -> rejected -> bald, 2026-08-31).
    # Cap keeps peaks(8) + places(14) + water(2) exactly at the contract.
    q = f'[out:json][timeout:60];node["place"~"^(city|town|village|hamlet)$"]{bbox_s};out;'
    try:
        d = overpass(q)
    except OverpassLoadShed as e:
        print(f"  places: overpass shed the box — building WITHOUT towns ({e})")
        d = {"elements": []}
    W, H = meta["W"], meta["H"]
    out = []
    for el in d.get("elements", []):
        t = el.get("tags", {})
        nm = pick_name(t)
        if not nm:
            continue
        if t.get("place") == "hamlet":
            continue                         # too many, often abandoned sidings
        px, py = to_px(el["lon"], el["lat"])
        if not (0 <= px < W and 0 <= py < H):
            continue
        try:
            pop = int("".join(ch for ch in t.get("population", "") if ch.isdigit()) or 0)
        except ValueError:
            pop = 0
        out.append({"text": nm, "glyph": "place", "px": px, "py": py,
                    "size": 13, "hmin": 0.0, "hmax": 1e9,
                    "rank": (PLACE_CLASS.get(t.get("place"), 0),
                             pop * (1.4 - 0.8 * 2.0 * math.hypot(
                                 (px - W / 2.0) / W, (py - H / 2.0) / H)
                                    if AUTO else 1.0))})
    out = declutter(out, 14, 60, key=lambda e: e["rank"])
    for e in out:
        print(f"  place {e['text']} (rank {e['rank']})")
    return out

# ---- pack writing -----------------------------------------------------------
def write_pack(outdir, name, elev, meta, imgL, imgA, styleL, styleA, entries, step,
               focus=(0.5, 0.5)):
    os.makedirs(outdir, exist_ok=True)
    W, H = meta["W"], meta["H"]
    lo, hi = float(elev.min()), float(elev.max())
    print(f"elevation {lo:.0f}..{hi:.0f}m  span {hi-lo:.0f}m  contour step {step:.0f}m"
          f"  ({(hi-lo)/step:.1f} levels)")
    norm = np.clip((elev - lo) / max(hi - lo, 1.0), 0.0, 1.0)
    q = np.round(norm * 65535.0).astype(np.uint16)[::-1, :]      # south-first
    inter = np.dstack([(q >> 8).astype(np.uint8), (q & 0xFF).astype(np.uint8)])
    with open(f"{outdir}/terrain.bin", "wb") as f:
        f.write(b"TOPOTER1"); f.write(struct.pack("<II", W, H)); f.write(inter.tobytes())

    if PROG:
        PROG.set(0.86, "distance fields")
    L8 = sdf8(imgL, W, H); A8 = sdf8(imgA, W, H)
    feat = np.dstack([L8, A8])[::-1, :, :]                       # south-first
    with open(f"{outdir}/features.bin", "wb") as f:
        f.write(b"TOPORDS1"); f.write(struct.pack("<II", W, H)); f.write(feat.tobytes())

    if PROG:
        PROG.set(0.94, "labels")
    # labels -> atlas (vertical stack)
    rendered = []
    for e in entries:
        timg, himg, ax, ay = render_label(e["text"], e["glyph"], e["size"])
        rendered.append((e, timg, himg, ax, ay))
    if rendered:
        aw = max(r[1].shape[1] for r in rendered)
        ah = sum(r[1].shape[0] for r in rendered)
        Lm = np.zeros((ah, aw), np.uint8); Am = np.zeros((ah, aw), np.uint8)
        y = 0
        with open(f"{outdir}/labels.bin", "wb") as f:
            f.write(b"TOPOPKS2")
            f.write(struct.pack("<III", len(rendered), aw, ah))
            for e, timg, himg, ax, ay in rendered:
                hh, ww = timg.shape
                Lm[y:y+hh, :ww] = timg; Am[y:y+hh, :ww] = himg
                u = e["px"] / W
                v = 1.0 - e["py"] / H
                f.write(struct.pack("<ffffHHHHHH", u, v, e["hmin"], e["hmax"],
                                    ax, ay, 0, y, ww, hh))
                y += hh
            f.write(np.dstack([Lm, Am]).tobytes())
        print(f"labels.bin: {len(rendered)} labels, atlas {aw}x{ah}")

    n = 2 ** meta["zoom"]
    with open(f"{outdir}/meta.bin", "wb") as f:
        f.write(b"TOPOAR01")
        f.write(struct.pack("<dddd", meta["x0"] / n, meta["y0"] / n,
                            meta["nx"] / n, meta["ny"] / n))
        f.write(struct.pack("<fff", lo, hi, step))
        f.write(struct.pack("<ff", *focus))
        f.write(struct.pack("<6f", *styleL))
        f.write(struct.pack("<6f", *styleA))
        # optional tail: IANA tz name (32 bytes) — old engines never read
        # this far, new engines fall back to lon/15 when it's absent/empty
        f.write(struct.pack("<32s", meta.get("tz", "").encode("ascii",
                                                              "ignore")[:32]))

    meta_out = dict(meta, name=name, elev_lo=lo, elev_hi=hi, step=step, focus=focus,
                    styleL=styleL, styleA=styleA,
                    labels=[{k: e[k] for k in ("text", "glyph", "hmin", "hmax")}
                            for e in entries])
    with open(f"{outdir}/meta.json", "w") as f:
        json.dump(meta_out, f, indent=1)

    # preview: contours(abs step) + hillshade + features + label marks
    lev = elev / step
    fr = np.abs((lev % 1.0) - 0.5)
    minor = np.clip(1.0 - fr / 0.10, 0, 1) ** 2
    gx = np.gradient(norm, axis=1); gy = np.gradient(norm, axis=0)
    nz = 1.0 / np.sqrt(1 + (gx * 80) ** 2 + (gy * 80) ** 2)
    sh = np.clip((-gx * 80 * -0.55 + gy * 80 * 0.55 + 0.62) * nz / 1.0, 0, 1)
    base = np.array([0.118, 0.125, 0.188])
    img = base[None, None, :] * (0.72 + 0.55 * sh[..., None])
    lc = np.array([0.286, 0.302, 0.392])
    img = img + (lc - img) * (minor[..., None] * 0.45)
    fL = np.asarray(imgL.resize((W, H))) / 255.0
    fA = np.asarray(imgA.resize((W, H))) / 255.0
    img = img + (np.array(styleL[:3]) - img) * (fL[..., None] * 0.85)
    img = img + (np.array(styleA[:3]) - img) * (fA[..., None] * 0.9)
    pv = Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))
    dd = ImageDraw.Draw(pv)
    pf = ImageFont.truetype(FONT_PATH, max(18, W // 90))
    for e in entries:
        x, y = e["px"], e["py"]
        col = (138, 173, 244) if e["glyph"] == "peak" else (202, 211, 245)
        dd.line([x - 7, y, x + 7, y], fill=col, width=3)
        dd.line([x, y - 7, x, y + 7], fill=col, width=3)
        dd.text((x + 10, y - 10), e["text"], font=pf, fill=col)
    pv.thumbnail((1400, 1400))
    pv.save(f"{outdir}/preview.png")
    print(f"pack written: {outdir}")

# ---- main -------------------------------------------------------------------
EARTH_ARGS = ["earth", "--bbox", "-85", "-180", "85", "179.999", "--zoom", "4",
              "--labels", "world", "--water-from-dem", "--step", "240"]


def main(argv=None):
    global FONT_PATH, METRIC, PROG
    METRIC = config.load().get("units") == "metric"
    net.prefer_ipv4()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--earth"]:
        argv = EARTH_ARGS + argv[1:]
    ap = argparse.ArgumentParser(prog="topopaper-ctl build")
    ap.add_argument("name")
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("LAT_S", "LON_W", "LAT_N", "LON_E"))
    ap.add_argument("--zoom", type=int, required=True)
    ap.add_argument("--features", default="none",
                    choices=["roads-region", "ski", "states", "none"])
    ap.add_argument("--roads-classes", default="trunk|primary",
                    help="regex alternation for roads-region L channel")
    ap.add_argument("--labels", default="",
                    help="comma list: peaks,places,places-region,lifts,states,geysers")
    ap.add_argument("--peak-min-ele", type=float, default=0.0)
    ap.add_argument("--peak-top", type=int, default=0, help="keep top-N peaks (0=all)")
    ap.add_argument("--peak-sep", type=float, default=0.0,
                    help="min px between kept peak labels (0=off)")
    ap.add_argument("--peak-gate", action="store_true",
                    help="LOD-gate close-cluster peaks instead of dropping them")
    ap.add_argument("--step", type=float, default=0.0, help="contour step m (0=auto)")
    ap.add_argument("--elev-floor", type=float, default=None,
                    help="clamp elevation at this floor (coastal/ceiling packs: "
                         "terrarium encodes bathymetry, which would crush land "
                         "contours into the top of the range)")
    ap.add_argument("--water", action="store_true",
                    help="fetch natural=water polygons -> water.bin (signed "
                         "shore-distance) + water name labels")
    ap.add_argument("--tier", type=int, default=None,
                    help="atlas scale tier K (msy=4^-K, rows=2^(z-2K)); "
                         "applies the tier's recipe defaults for any flag "
                         "left unset — explicit flags always win")
    ap.add_argument("--water-from-dem", action="store_true",
                    help="derive water.bin from elevation <= 0 (terrarium "
                         "bathymetry — free oceans for planetary packs)")
    ap.add_argument("--auto", action="store_true",
                    help="stamp meta auto=true (search build — makes the "
                         "pack a `topopaper-ctl gc` candidate)")
    a = ap.parse_args(argv)
    FONT_PATH = find_font()
    load_font_cmap()
    global AUTO
    AUTO = a.auto
    # tier recipe defaults MUST apply before anything reads a.* — the first
    # migration batch computed the label set from pre-tier a.labels (eight
    # lines too early) and baked 17 bald packs. Apply first, derive after.
    if a.tier is not None and a.tier in TIERS:
        t = TIERS[a.tier]
        d = ap.get_default
        if "features" in t and a.features == d("features"): a.features = t["features"]
        if "roads_classes" in t and a.roads_classes == d("roads_classes"):
            a.roads_classes = t["roads_classes"]
        if "labels" in t and a.labels == d("labels"): a.labels = t["labels"]
        if "peak_min_ele" in t and a.peak_min_ele == d("peak_min_ele"):
            a.peak_min_ele = t["peak_min_ele"]
        if "peak_top" in t and a.peak_top == d("peak_top"): a.peak_top = t["peak_top"]
        if "peak_sep" in t and a.peak_sep == d("peak_sep"): a.peak_sep = t["peak_sep"]
        if not a.water and not a.water_from_dem:
            if t.get("water") == "dem": a.water_from_dem = True
            elif t.get("water") == "osm": a.water = True
        print(f"  tier {a.tier} recipe: features={a.features} labels={a.labels} "
              f"water={'osm' if a.water else 'dem' if a.water_from_dem else 'off'}")
    PROG = Progress(a.name)
    want = {s for s in a.labels.split(",") if s}
    elev, meta = fetch_dem(a.bbox, a.zoom, a.tier)
    if a.elev_floor is not None:
        elev = np.maximum(elev, a.elev_floor)
    W, H = meta["W"], meta["H"]
    n = 2 ** meta["zoom"]

    def to_px(lon, lat):
        xf, yf = lonlat_to_tilef(lon, lat, meta["zoom"])
        return (xf - meta["x0"]) * 256.0, (yf - meta["y0"]) * 256.0

    bs, bw, bn, be = meta["bbox"]
    bbox_s = f"({bs:.5f},{bw:.5f},{bn:.5f},{be:.5f})"
    msy = meta["ny"] / n

    # WATER FIRST (Isle Royale, 2026-09-02): the OSM land mask is the truth
    # that exposes terrarium void tiles, and the repaired DEM must exist
    # before peaks snap to it and before contours/hillshade bake from it.
    water, wlabels = None, []
    if a.water and not a.water_from_dem:
        if PROG:
            PROG.set(0.70, "water")
        omask, wlabels = feat_water(bbox_s, to_px, W, H)
        wmask = omask | marine_mask(elev)
        elev = repair_dem_voids(elev, wmask, meta)
        wmask = veto_water_by_dem(wmask, elev)
        water = signed_from_mask(wmask)
        if water is not None:
            print(f"water (osm+ocean): {wmask.mean()*100:.1f}% of the pack")

    if PROG:
        PROG.set(0.74, "features")
    imgL = Image.new("L", (W * SS, H * SS), 0)
    imgA = Image.new("L", (W * SS, H * SS), 0)
    styleL = (*PEACH, 0.8, 1.9, 0.0)         # op 0 = channel unused
    styleA = (*PEACH, 1.2, 2.6, 0.0)
    extra, focus = [], (0.5, 0.5)
    if a.features == "roads-region":
        imgL, imgA, styleL, styleA, extra = feat_roads_region(
            bbox_s, to_px, W, H, a.roads_classes)
    elif a.features == "states":
        imgL, imgA, styleL, styleA, extra = feat_states(bbox_s, to_px, W, H)
    elif a.features == "ski":
        imgL, imgA, styleL, styleA, extra, focus = feat_ski(bbox_s, to_px, W, H, msy)

    entries = []
    if "peaks" in want:
        try:
            entries += labels_peaks(bbox_s, to_px, elev, meta, a.peak_min_ele,
                                    a.peak_top, a.peak_sep,
                                    msy if a.peak_gate else 0.0)
        except (OverpassLoadShed, OSError) as e:
            # like places: a map without peak names beats no map at all
            print(f"  peaks: Overpass unavailable — building WITHOUT peak labels ({e})")
    if "places" in want:
        entries += labels_places(bbox_s, to_px, meta)
    if "places-region" in want:
        entries += labels_places_region(bbox_s, to_px, meta)
    if "geysers" in want:
        entries += labels_geysers(bbox_s, to_px, meta)
    if "states" in want:
        entries += labels_states(to_px, meta)
    if "world" in want:
        entries += labels_world(to_px, meta)
    if "lifts" in want:
        entries += extra
    # cheap label cartography: state names are annotation, not geography —
    # displace them downward off any peak/place label box they'd collide with.
    # Boxes estimated in pack px at the idle zoom (labels draw 1:1 screen px).
    ppx = 1920.0 / (0.46 * H)                # screen px per pack px, idle
    def lbox(e):
        w = 17.5 * len(e["text"]) / ppx
        hh = 30.0 / ppx
        x0 = e["px"] - w / 2 if e["glyph"] in ("state", "water") else e["px"]
        return (x0, x0 + w, e["py"] - hh / 2, e["py"] + hh / 2)
    others = [e for e in entries if e["glyph"] != "state"]
    for st in (e for e in entries if e["glyph"] == "state"):
        for _ in range(4):
            sb = lbox(st)
            if not any(not (sb[1] < ob[0] or sb[0] > ob[1] or
                            sb[3] < ob[2] or sb[2] > ob[3])
                       for ob in map(lbox, others)):
                break
            st["py"] += H * 0.011
            print(f"  shifted state label {st['text'].replace(' ', '')} down")
    if a.water_from_dem:
        if PROG:
            PROG.set(0.80, "water")
        wm = elev <= 0.0
        nem, nlab = ne_lakes(to_px, W, H)
        wm = wm | nem
        entries += nlab
        water = signed_from_mask(wm)
        if water is not None:
            print(f"water (from DEM + NE lakes): {wm.mean()*100:.1f}% of the pack")
    elif a.water:
        entries += wlabels                   # fetched up front (WATER FIRST)

    step = a.step if a.step > 0 else pick_step(float(elev.max() - elev.min()))
    entries = budget_labels(entries)
    if a.auto:
        meta["auto"] = True
    bb = meta["bbox"]
    meta["tz"] = find_tzname((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
    if meta["tz"]:
        print(f"  tz: {meta['tz']}")
    # TOPO_OUT: stage a rebuild elsewhere (verified in the rig, then renamed
    # into areas/ in one motion) instead of overwriting a pack the live
    # engine may be reading mid-build
    outdir = os.path.abspath(os.environ.get("TOPO_OUT") or paths.areas_dir() / a.name)
    write_pack(outdir, a.name, elev, meta, imgL, imgA, styleL, styleA, entries, step,
               focus)
    if water is not None:
        with open(f"{outdir}/water.bin", "wb") as f:
            f.write(b"TOPOWTR1")
            f.write(struct.pack("<II", W, H))
            f.write(water[::-1, :].tobytes())        # rows SOUTH-first, like terrain
        print(f"wrote water.bin ({(16 + W * H) / 1e6:.1f} MB)")

def run(argv=None):
    try:
        main(argv)
    finally:
        if PROG:
            PROG.done()


METRIC = False

if __name__ == "__main__":
    run()
