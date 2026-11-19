"""Draws the extra icons the mini games need (rock / paper / scissors / tap / team shields / signal lights).
Run once:  python tools/make_game_icons.py   (needs pillow + numpy)
The Kon-yellow ones use the same palette as tools/make_assets.py, the team + light icons keep real colours
(red / blue / green) because their colour is the meaning.
"""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

PALETTE = [(0xFF, 0xD6, 0x5A), (0xFF, 0xB8, 0x32), (0xFF, 0x9B, 0x22), (0xF5, 0x82, 0x19), (0xD9, 0x5A, 0x12)]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "icons")
S = 4                    # supersampling
N = 128 * S


def canvas():
    return Image.new("RGBA", (N, N), (0, 0, 0, 0))


def sc(v):
    return [int(x * S) for x in v] if isinstance(v, (list, tuple)) else int(v * S)


def pts(points):
    return [(x * S, y * S) for x, y in points]


def finish(im: Image.Image) -> Image.Image:
    return im.resize((128, 128), Image.LANCZOS)


def recolor(im: Image.Image) -> Image.Image:
    a = np.array(im.convert("RGBA")).astype(float)
    alpha = a[..., 3]
    h, w = alpha.shape
    ys, xs = np.nonzero(alpha > 8)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    yy, xx = np.mgrid[0:h, 0:w]
    ty = (yy - y0) / max(1, y1 - y0)
    tx = (xx - x0) / max(1, x1 - x0)
    t = np.clip(0.65 * ty + 0.35 * tx, 0, 1)
    pos = t * (len(PALETTE) - 1)
    i = np.clip(np.floor(pos).astype(int), 0, len(PALETTE) - 2)
    f = (pos - i)[..., None]
    pal = np.array(PALETTE, float)
    rgb = pal[i] * (1 - f) + pal[i + 1] * f
    lum = a[..., :3].max(axis=2)
    shade = np.clip(lum / 255.0, 0, 1)[..., None]
    rgb = rgb * (0.62 + 0.38 * shade)
    return Image.fromarray(np.dstack([np.clip(rgb, 0, 255), alpha]).astype(np.uint8), "RGBA")


def gray(v):
    return (v, v, v, 255)


# ------------------------------------------------------------ rock
def rock():
    im = canvas()
    d = ImageDraw.Draw(im)
    body = [(20, 78), (26, 52), (46, 34), (72, 28), (98, 40), (110, 64), (104, 92), (80, 104), (44, 104)]
    d.polygon(pts(body), fill=gray(255))
    d.polygon(pts([(26, 52), (46, 34), (60, 58), (40, 74), (20, 78)]), fill=gray(205))        # left facet
    d.polygon(pts([(46, 34), (72, 28), (80, 52), (60, 58)]), fill=gray(235))                    # top facet
    d.polygon(pts([(72, 28), (98, 40), (110, 64), (86, 66), (80, 52)]), fill=gray(215))         # right facet
    d.polygon(pts([(40, 74), (60, 58), (86, 66), (80, 104), (44, 104)]), fill=gray(170))        # belly shade
    d.polygon(pts([(86, 66), (110, 64), (104, 92), (80, 104)]), fill=gray(140))
    d.line(pts([(60, 58), (66, 80), (58, 96)]), fill=gray(110), width=sc(2.5))                   # crack
    return finish(im)


# ------------------------------------------------------------ paper
def paper():
    im = canvas()
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(pts([(24, 12), (104, 116)]), radius=sc(9), fill=gray(255))
    d.polygon(pts([(78, 12), (104, 38), (104, 12)]), fill=(0, 0, 0, 0))                           # cut corner
    d.polygon(pts([(78, 12), (104, 38), (82, 38)]), fill=gray(175))                               # fold
    d.polygon(pts([(78, 12), (82, 38), (78, 38)]), fill=gray(175))
    for y, x2 in ((52, 88), (66, 88), (80, 88), (94, 70)):
        d.rounded_rectangle(pts([(38, y), (x2, y + 6)]), radius=sc(3), fill=gray(150))
    d.rounded_rectangle(pts([(38, 26), (62, 32)]), radius=sc(3), fill=gray(150))
    return finish(im)


# ------------------------------------------------------------ scissors
def scissors():
    im = canvas()
    d = ImageDraw.Draw(im)
    # blades (tapered)
    d.polygon(pts([(88, 8), (98, 14), (64, 66), (54, 60)]), fill=gray(255))
    d.polygon(pts([(40, 8), (30, 14), (64, 66), (74, 60)]), fill=gray(225))
    # handle arms
    d.line(pts([(64, 63), (88, 96)]), fill=gray(215), width=sc(9))
    d.line(pts([(64, 63), (40, 96)]), fill=gray(245), width=sc(9))
    # rings
    d.ellipse(pts([(66, 84), (116, 122)]), outline=gray(255), width=sc(9))
    d.ellipse(pts([(12, 84), (62, 122)]), outline=gray(235), width=sc(9))
    d.ellipse(pts([(58, 56), (70, 68)]), fill=gray(120))                                          # pivot
    return finish(im)


# ------------------------------------------------------------ tap (cursor + ripples)
def tap():
    im = canvas()
    d = ImageDraw.Draw(im)
    for r, w, g in ((50, 6, 190), (36, 6, 225)):
        d.ellipse(pts([(60 - r, 60 - r), (60 + r, 60 + r)]), outline=gray(g), width=sc(w))
    d.ellipse(pts([(46, 46), (74, 74)]), fill=gray(255))
    arrow = [(66, 62), (66, 120), (82, 104), (96, 126), (106, 120), (92, 98), (114, 96)]
    d.polygon(pts(arrow), fill=gray(255), outline=gray(110), width=sc(3))
    return finish(im)


# ------------------------------------------------------------ team shields (real colours)
def shield(top, bottom):
    im = canvas()
    outline = [(64, 6), (112, 20), (112, 62), (96, 94), (64, 122), (32, 94), (16, 62), (16, 20)]
    grad = Image.new("RGBA", (N, N))
    gp = grad.load()
    for y in range(N):
        t = y / N
        c = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)) + (255,)
        for x in range(N):
            gp[x, y] = c
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).polygon(pts(outline), fill=255)
    im.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(im)
    d.line(pts(outline + [outline[0]]), fill=(255, 255, 255, 255), width=sc(5), joint="curve")
    inner = [(64, 24), (96, 34), (96, 62), (84, 84), (64, 102), (44, 84), (32, 62), (32, 34)]
    hl = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(hl).polygon(pts([(64, 24), (96, 34), (96, 52), (64, 52)]), fill=(255, 255, 255, 70))
    im = Image.alpha_composite(im, hl)
    return finish(im)


# ------------------------------------------------------------ signal lights
def light(inner, outer):
    im = canvas()
    d = ImageDraw.Draw(im)
    d.ellipse(pts([(10, 10), (118, 118)]), fill=outer + (255,))
    d.ellipse(pts([(20, 20), (108, 108)]), fill=inner + (255,))
    glow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(pts([(34, 28), (74, 58)]), fill=(255, 255, 255, 120))
    glow = glow.filter(ImageFilter.GaussianBlur(S * 3))
    im = Image.alpha_composite(im, glow)
    return finish(im)


def main():
    os.makedirs(OUT, exist_ok=True)
    kon = {"rock": rock(), "paper": paper(), "scissors": scissors(), "tap": tap()}
    for name, im in kon.items():
        recolor(im).save(os.path.join(OUT, f"{name}.png"))
    shield((255, 110, 110), (170, 20, 36)).save(os.path.join(OUT, "team_red.png"))
    shield((110, 170, 255), (24, 60, 170)).save(os.path.join(OUT, "team_blue.png"))
    light((255, 80, 80), (120, 14, 20)).save(os.path.join(OUT, "light_red.png"))
    light((70, 235, 110), (14, 96, 38)).save(os.path.join(OUT, "light_green.png"))
    print("done")


if __name__ == "__main__":
    main()
