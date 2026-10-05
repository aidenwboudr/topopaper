"""Convert the engine's TOPA_SHOT frame (binary PPM) to PNG and fail when it
is blank. Used by CI on every platform.

    python tests/ppm2png.py FRAME.ppm OUT.png
"""
import sys

from PIL import Image, ImageStat

im = Image.open(sys.argv[1]).convert("RGB")
im.save(sys.argv[2])
sd = ImageStat.Stat(im.convert("L")).stddev[0]
print(f"{sys.argv[2]}: {im.size[0]}x{im.size[1]}, luminance stddev {sd:.1f}")
sys.exit(0 if sd > 4.0 else "frame looks blank")
