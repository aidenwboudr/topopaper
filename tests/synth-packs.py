#!/usr/bin/env python3
"""Synthetic area packs for the flight seam test (no network).

Writes DATA/areas/synth-* : a globe, a region, two overlapping same-scale
areas and two overlapping ski-site sized packs, nested like a real ladder
(msy = 4^-K). Every pack samples ONE elevation function (box-filtered to its
own texel size, like a downsampled DEM), carries roads and a lake where its
tier would, so overlapping packs agree the way real ones should, and a
flight through them exercises every hand-off: scale rungs, siblings, more
than three packs in view, coverage overhangs.

    tests/synth-packs.py DATA_DIR
"""
import os
import struct
import sys

import numpy as np

CX, CY = 0.52778, 0.35970          # mercator centre of the scene (~45N 10E)
ROAD = (0.961, 0.663, 0.498)       # the builders' macchiato road / lift keys
RUN, LIFT = (0.490, 0.769, 0.894), (0.929, 0.529, 0.588)

# elevation: (wavelength in mercator units, amplitude m) — 2000 km .. 600 m
WAVES = [(0.05, 2400), (0.011, 1300), (0.0023, 650), (0.0005, 320),
         (0.00011, 140), (0.000024, 60)]
LAKE = (CX + 0.0009, CY + 0.0006, 0.00035)          # centre x,y and radius
TRUNK = [(CY - 0.0002, 0.35), (CY + 0.0011, -0.6)]  # y at CX, slope


def elevation(x, y, texel):
    e = np.full(x.shape, 900.0)
    for i, (lam, amp) in enumerate(WAVES):
        att = np.sinc(texel / lam) ** 2             # box filter of one texel
        e += amp * att * np.sin(2 * np.pi * x / lam + i) * np.sin(2.26 * np.pi * y / lam + 2 * i)
    return e


def seg_dist(x, y, x0, y0, x1, y1):
    dx, dy = x1 - x0, y1 - y0
    t = np.clip(((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy), 0, 1)
    return np.hypot(x - (x0 + t * dx), y - (y0 + t * dy))


def write_pack(data, name, mx0, my0, msx, msy, w, h, roads=False, streets=False,
               ski=False, lake=True):
    d = os.path.join(data, "areas", name)
    os.makedirs(d, exist_ok=True)
    texel = msy / h
    # texel centres, rows SOUTH-first (row 0 = largest y)
    xs = mx0 + (np.arange(w) + 0.5) * msx / w
    ys = my0 + msy - (np.arange(h) + 0.5) * msy / h
    x, y = np.meshgrid(xs, ys)
    e = elevation(x, y, texel)
    lo, hi = float(e.min()), float(e.max())
    q = np.round((e - lo) / max(hi - lo, 1.0) * 65535).astype(np.uint16)
    ter = np.stack([q >> 8, q & 255], axis=-1).astype(np.uint8)
    with open(os.path.join(d, "terrain.bin"), "wb") as f:
        f.write(b"TOPOTER1" + struct.pack("<II", w, h) + ter.tobytes())
    # water: ocean below 0 m, plus the lake once it spans a few texels
    sd = e / 50.0                                   # signed, land > 0 (rough texels)
    if lake and LAKE[2] / texel > 3:
        sd = np.minimum(sd, (np.hypot(x - LAKE[0], y - LAKE[1]) - LAKE[2]) / texel)
    wat = np.clip(128 + 8 * sd, 0, 255).astype(np.uint8)
    with open(os.path.join(d, "water.bin"), "wb") as f:
        f.write(b"TOPOWTR1" + struct.pack("<II", w, h) + wat.tobytes())
    # features: L = roads (trunk; + a street grid) or runs, A = lifts
    dl = np.full(x.shape, 1e9)
    da = np.full(x.shape, 1e9)
    if roads:
        for yc, sl in TRUNK:
            dl = np.minimum(dl, seg_dist(x, y, CX - 0.05, yc - 0.05 * sl, CX + 0.05, yc + 0.05 * sl))
    if streets:                                     # a town grid
        g = 0.000012
        grid = np.minimum(np.abs((x - CX) % g - g / 2), np.abs((y - CY) % g - g / 2))
        dl = np.minimum(dl, np.where(np.hypot(x - CX, y - CY + 0.00005) < 0.00006, grid, 1e9))
    if ski:
        for k in range(6):
            x0 = CX - 0.00012 + k * 0.00005
            da = np.minimum(da, seg_dist(x, y, x0, CY + 0.0001, x0 + 0.00003, CY - 0.0001))
            dl = np.minimum(dl, seg_dist(x, y, x0 - 0.00002, CY + 0.00012, x0 + 0.00004, CY - 0.00011))
    feat = np.stack([np.clip(dl / texel * 8, 0, 255), np.clip(da / texel * 8, 0, 255)],
                    axis=-1).astype(np.uint8)
    with open(os.path.join(d, "features.bin"), "wb") as f:
        f.write(b"TOPORDS1" + struct.pack("<II", w, h) + feat.tobytes())
    stl = (*(RUN if ski else ROAD), 0.7, 1.7, 0.5 if (roads or ski) else 0.0)
    sta = (*(LIFT if ski else ROAD), 0.9, 2.1, 0.62 if ski else 0.0)
    with open(os.path.join(d, "meta.bin"), "wb") as f:
        f.write(b"TOPOAR01" + struct.pack("<4d", mx0, my0, msx, msy) +
                struct.pack("<5f", lo, hi, 60.0, 0.5, 0.5) +
                struct.pack("<6f", *stl) + struct.pack("<6f", *sta))
    print(f"{name}: {w}x{h} K{np.log(1 / msy) / np.log(4):.1f} {lo:.0f}..{hi:.0f} m")


def centred(k, aspect=1.5, dx=0.0, dy=0.0):
    msy = 4.0 ** -k
    msx = msy * aspect
    return CX - msx / 2 + dx * msx, CY - msy / 2 + dy * msy, msx, msy


def main():
    data = sys.argv[1]
    write_pack(data, "synth-world", 0.0, 0.0, 1.0, 1.0, 1024, 1024, lake=False)
    write_pack(data, "synth-region", *centred(2), 768, 512, lake=False)
    write_pack(data, "synth-area", *centred(4), 768, 512, roads=True)
    write_pack(data, "synth-area-east", *centred(4, dx=0.85, dy=0.2), 768, 512, roads=True)
    write_pack(data, "synth-site", *centred(6, dx=-0.2), 384, 256, roads=True, streets=True, ski=True)
    write_pack(data, "synth-site-b", *centred(6, dx=0.55, dy=0.3), 384, 256, roads=True, ski=True)


if __name__ == "__main__":
    main()
