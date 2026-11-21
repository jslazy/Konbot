"""Draws the Ludo pawn icons (one per colour) into assets/icons.  Run once:  python tools/make_ludo_icons.py"""
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import ludo_render as R  # noqa: E402

OUT = os.path.join(ROOT, "assets", "icons")


def pawn(color: str) -> Image.Image:
    s, N = 4, 128 * 4
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c, dk = R.RGB[color], R.DARK[color]
    cx = N / 2
    d.ellipse([N * .14, N * .80, N * .86, N * .98], fill=(0, 0, 0, 70))
    d.polygon([(cx, N * .20), (N * .84, N * .88), (N * .16, N * .88)], fill=c, outline=dk)
    d.ellipse([N * .20, N * .70, N * .80, N * .94], fill=c, outline=dk, width=s * 3)
    d.ellipse([N * .28, N * .03, N * .72, N * .47], fill=c, outline=dk, width=s * 3)
    d.ellipse([N * .37, N * .10, N * .53, N * .22], fill=(255, 255, 255, 190))
    return im.resize((128, 128), Image.LANCZOS)


for col in R.COLORS:
    pawn(col).save(os.path.join(OUT, f"pawn_{col}.png"))
    print("pawn_" + col)
