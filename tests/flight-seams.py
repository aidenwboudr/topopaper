#!/usr/bin/env python3
"""Seam report for a flight recorded by tests/flight-rec.sh (DBG=N).

A seam is where the ladder hands the screen from one pack to another and the
two render the same ground differently. Every DBG frame carries the slot
shares (d*_1) and each slot rendered alone (d*_2..4), so the hand-off zones
and the disagreement inside them can be measured directly:

  tone   low-pass colour difference between two slots where both contribute
         and their shares change across the screen (the visible band)
  pops   frame-to-frame jumps of the final image beyond what motion explains
         (the median of the neighbouring frames' change)
  voids  screen no slot covers (drawn as bare background), once the planet
         fills the frame

    tests/flight-seams.py REC_DIR [--max-tone X] [--max-pop X] [--max-void X] [--maps]

Exits 1 if a limit is exceeded. --maps writes seam-NNNNN.png overlays.
"""
import argparse
import glob
import os
import re
import sys

import numpy as np
from PIL import Image


def load(path):
    return np.asarray(Image.open(path), dtype=np.float32)


def box(a, r):
    """Box blur of radius r (separable, edge-clamped), any channel count."""
    if r <= 0:
        return a
    k = 2 * r + 1
    for ax in (0, 1):
        p = np.pad(a, [(r, r) if i == ax else (0, 0) for i in range(a.ndim)], mode="edge")
        c = np.cumsum(p, axis=ax, dtype=np.float64)
        z = np.zeros_like(np.take(c, [0], axis=ax))
        c = np.concatenate([z, c], axis=ax)
        a = (np.take(c, range(k, c.shape[ax]), axis=ax)
             - np.take(c, range(0, c.shape[ax] - k), axis=ax)) / k
    return a.astype(np.float32)


def trace(rec):
    rows = {}
    for ln in open(os.path.join(rec, "trace.txt")):
        f = ln.split()
        m = re.search(r"slots=(\S+) w=", ln)
        slots = [(s.rsplit(":", 1)[0], float(s.rsplit(":", 1)[1])) for s in m.group(1).split(",")]
        rows[int(f[0])] = dict(fly=int(f[1]), tau=float(f[2]), h=float(f[3]), slots=slots)
    return rows


def tone(rec, n, tr, maps):
    sh = load(f"{rec}/d{n:05d}_1.ppm") / 255.0
    mag = (sh[..., 0] > 0.9) & (sh[..., 1] < 0.1) & (sh[..., 2] > 0.9)   # nothing covers
    void = float(mag.mean()) if tr[n]["h"] < 0.12 else 0.0
    sh = np.where(mag[..., None], 0.0, sh)
    cs = [box(load(f"{rec}/d{n:05d}_{k}.ppm"), 6) for k in (2, 3, 4)]
    # where a share changes across the screen (the hand-off band)
    g = np.zeros(sh.shape[:2], np.float32)
    for i in range(3):
        s = box(sh[..., i], 2)
        gy, gx = np.gradient(s)
        g = np.maximum(g, np.hypot(gx, gy))
    band = g > 1.0 / 600.0
    worst, where = 0.0, None
    slots = tr[n]["slots"]
    seam = np.zeros(sh.shape[:2], np.float32)
    for i in range(3):
        for j in range(i + 1, 3):
            both = band & (sh[..., i] > 0.04) & (sh[..., j] > 0.04)
            if both.sum() < 200:
                continue
            d = np.abs(cs[i] - cs[j]).max(axis=2)
            seam = np.maximum(seam, np.where(both, d, 0))
            v = float(np.percentile(d[both], 95))
            if v > worst:
                worst, where = v, f"{slots[i][0]}|{slots[j][0]}"
    if maps:
        base = load(f"{rec}/f{n:05d}.ppm")
        ov = base.copy()
        ov[..., 0] = np.clip(base[..., 0] + seam * 12, 0, 255)
        Image.fromarray(ov.astype(np.uint8)).save(f"{rec}/seam-{n:05d}.png")
    return worst, where, void


def pops(rec, tr):
    fs = sorted(glob.glob(f"{rec}/f*.ppm"))
    prev, diffs = None, []
    for f in fs:
        a = load(f)
        # 8x8 tiles: a local pop must not drown in a full-frame mean
        h, w = a.shape[0] // 8 * 8, a.shape[1] // 8 * 8
        t = a[:h, :w].reshape(8, h // 8, 8, w // 8, 3).mean(axis=(1, 3))
        diffs.append(np.zeros((8, 8)) if prev is None else np.abs(t - prev).max(axis=2))
        prev = t
    d = np.stack(diffs)                       # frames x 8 x 8
    out = []
    for n in range(2, len(d) - 2):
        ctx = np.median(np.stack([d[n - 2], d[n - 1], d[n + 1], d[n + 2]]), axis=0)
        ex = d[n] - ctx                       # change beyond the motion trend
        k = np.unravel_index(np.argmax(ex), ex.shape)
        out.append((float(ex[k]), n, k))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rec")
    ap.add_argument("--max-tone", type=float, default=None)
    ap.add_argument("--max-pop", type=float, default=None)
    ap.add_argument("--max-void", type=float, default=None)
    ap.add_argument("--maps", action="store_true")
    a = ap.parse_args()
    tr = trace(a.rec)
    dbg = sorted(int(re.search(r"d(\d+)_1", p).group(1))
                 for p in glob.glob(f"{a.rec}/d*_1.ppm"))
    worst_t, worst_v = (0.0, None, None), (0.0, None)
    print("frame  tau    h          tone  void   pair")
    for n in dbg:
        v, where, vd = tone(a.rec, n, tr, a.maps)
        r = tr[n]
        if v > 0 or vd > 0:
            print(f"{n:5d}  {r['tau']:.3f}  {r['h']:.3e}  {v:5.1f}  {vd:.3f}  {where}")
        if v > worst_t[0]:
            worst_t = (v, n, where)
        if vd > worst_v[0]:
            worst_v = (vd, n)
    pp = sorted(pops(a.rec, tr), reverse=True)[:8]
    print("\nlargest pops (tile excess over neighbour median, 0-255):")
    for v, n, k in pp:
        r = tr.get(n, {})
        print(f"  {v:5.1f}  frame {n}  tile {k}  tau {r.get('tau', 0):.3f}  "
              f"{' '.join(f'{s}:{w:.3f}' for s, w in r.get('slots', []))}")
    print(f"\nworst tone {worst_t[0]:.1f} (frame {worst_t[1]}, {worst_t[2]}); "
          f"worst pop {pp[0][0] if pp else 0:.1f}; "
          f"worst void {worst_v[0]:.3f} (frame {worst_v[1]})")
    bad = (a.max_tone is not None and worst_t[0] > a.max_tone) or \
          (a.max_pop is not None and pp and pp[0][0] > a.max_pop) or \
          (a.max_void is not None and worst_v[0] > a.max_void)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
