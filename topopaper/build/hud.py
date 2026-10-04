#!/usr/bin/env python3
"""`topopaper-ctl hud`: bake the topopaper HUD glyph atlas (clock + future info lines).

hud.bin: 'TOPOHUD1' | u16 count,cellw,cellh,baseoff | count LATIN-1 charset
         bytes | atlas (L,A) byte pairs, one uniform cell per glyph stacked
         vertically (index -> y = index*cellh). The charset in the file IS the
         index contract — the engine reads it, so growing it later is just a
         re-run (no C change). Latin-1 keeps one byte per glyph (° = \xB0,
         · = \xB7 — the C side uses those literals). baseoff = baseline height
         from the cell BOTTOM (pad+descent), for baseline-aligning mixed sizes.

Ink recipe matches pack labels (build/area.py render_label): JetBrains Mono
Bold, 3x3-dilated halo alpha, baked at S=2 for the hi-dpi buffer.
JetBrains Mono is monospaced, so every cell shares one advance — layout in
the engine is a fixed grid plus tracking.
"""
import argparse
import struct

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .. import fonts, paths

S = 2                                        # raster supersample (hi-dpi)
SIZE = 30                                    # logical pt; baked at SIZE*S
PAD = 3 * S                                  # halo bleed, as in render_label
CHARSET = "0123456789:°·-/'&ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # clock + weather line


def main(argv=None):
    ap = argparse.ArgumentParser(prog="topopaper-ctl hud")
    ap.add_argument("--out", default=str(paths.hud_file()))
    out = ap.parse_args(argv).out
    font = ImageFont.truetype(fonts.find_font(), SIZE * S)
    asc, desc = font.getmetrics()
    adv = int(round(font.getlength("0")))
    cw, ch = adv + 2 * PAD, asc + desc + 2 * PAD
    n = len(CHARSET)
    Lm = np.zeros((n * ch, cw), np.uint8)
    Am = np.zeros((n * ch, cw), np.uint8)
    for i, c in enumerate(CHARSET):
        timg = Image.new("L", (cw, ch), 0)
        himg = Image.new("L", (cw, ch), 0)
        dt, dh = ImageDraw.Draw(timg), ImageDraw.Draw(himg)
        for ox in (-S, 0, S):
            for oy in (-S, 0, S):
                dh.text((PAD + ox, PAD + oy), c, font=font, fill=255)
        dt.text((PAD, PAD), c, font=font, fill=255)
        Lm[i * ch:(i + 1) * ch] = np.asarray(timg)
        Am[i * ch:(i + 1) * ch] = np.asarray(himg)

    with open(out, "wb") as f:
        f.write(b"TOPOHUD1")
        f.write(struct.pack("<HHHH", n, cw, ch, PAD + desc))
        f.write(CHARSET.encode("latin-1"))
        f.write(np.dstack([Lm, Am]).tobytes())
    print(f"hud.bin: {n} glyphs, cell {cw}x{ch}, baseline {PAD + desc}, "
          f"atlas {cw}x{n * ch}")
