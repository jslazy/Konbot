"""Icon sheet -> transparent 128x128 PNG emojis + control panel image.
Run once:  python tools/make_assets.py  (needs pillow, numpy, scipy)
Input sheet: assets/icon_sheet.png
"""
import os, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHEET = os.path.join(ROOT, "assets", "icon_sheet.png")
OUT = os.path.join(ROOT, "assets", "icons")

# index in the sheet (row-major, 130 icons) -> emoji name
NAMES = """lock unlock hide show ban mute volume user_add users signal edit
settings tools shield_check shield_alert star_outline crown diamond gift_outline gift tag bookmark
bell bell_ring mail message messages headset help info warning check_outline cross_filled refresh
database database_add database_arrow members user_spark user_arrow award shield bolt sparkles wand palette
ticket gift_box party trophy calendar clock stopwatch hourglass target gamepad dice gamepad_fill
wallet coins upload download swap swap_small login logout home folder image video
music play pause stop next prev repeat shuffle menu grid link external
speaker mic waveform headphones phone monitor mobile controller code terminal globe pin
heart heart_broken star_fill star_line thumbs_up thumbs_down fire leaf rocket skull ghost cat
crown_line info_diamond diamond_fill crown_fill badge user_star user_minus_arrow user_plus_arrow user_minus shield_star shield_ok shield_asterisk
fast_forward rewind play_circle stop_circle check cross plus minus up down left right""".split()
assert len(NAMES) == 130, len(NAMES)


def find_boxes(im):
    a = np.array(im).astype(int)
    mask = (a[:, :, 1] > 110) & (a[:, :, 1] > a[:, :, 0] + 10) & (a[:, :, 1] > a[:, :, 2] + 40)
    lab, n = ndi.label(mask.any(axis=1))
    bands = []
    for i in range(1, n + 1):
        ys = np.where(lab == i)[0]
        bands.append((ys[0], ys[-1]))
    boxes = []
    for (y0, y1) in bands:
        ya, yb = max(0, y0 - 4), y1 + 5
        band = mask[ya:yb]
        cl, m = ndi.label(ndi.binary_dilation(band.any(axis=0), iterations=8))
        for c in range(1, m + 1):
            xs = np.where(cl == c)[0]
            x0, x1 = xs[0] + 6, xs[-1] - 6
            sub = mask[ya:yb, x0:x1 + 1]
            ys = np.where(sub.any(axis=1))[0]
            boxes.append((x0, ya + ys[0], x1 + 1, ya + ys[-1] + 1))
    return boxes


def to_rgba(crop):
    """Un-premultiply the glyph from the dark tile: brightness -> alpha."""
    a = np.array(crop.convert("RGB")).astype(float)
    mx = a.max(axis=2)
    alpha = np.clip((mx - 18) / (150 - 18), 0, 1)
    alpha = np.clip(alpha * 1.15, 0, 1)
    rgb = np.where(alpha[..., None] > 0.02, a / np.maximum(alpha[..., None], 0.02), 0)
    # lift colours so the glyph stays bright on dark & light themes
    rgb = np.clip(rgb * 1.12, 0, 255)
    out = np.dstack([rgb, alpha * 255]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def main():
    sheet = Image.open(SHEET).convert("RGB")
    boxes = find_boxes(sheet)
    assert len(boxes) == 130, f"expected 130 icons, found {len(boxes)}"
    os.makedirs(OUT, exist_ok=True)
    for name, (x0, y0, x1, y1) in zip(NAMES, boxes):
        crop = sheet.crop((x0 - 6, y0 - 6, x1 + 6, y1 + 6))
        g = to_rgba(crop)
        bb = g.getbbox()
        if bb:
            g = g.crop(bb)
        side = 128
        inner = 100
        k = inner / max(g.size)
        g = g.resize((max(1, round(g.width * k)), max(1, round(g.height * k))), Image.LANCZOS)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(g, ((side - g.width) // 2, (side - g.height) // 2), g)
        canvas.save(os.path.join(OUT, f"{name}.png"), optimize=True)
    print("icons written:", len(NAMES))
    make_panel()


# ---------- control panel image (same layout as the original, green icons) ----------
PANEL = [  # (icon, small caption, label)
    ("lock", "LOCK", "Lock VC"), ("unlock", "UNLOCK", "Unlock VC"), ("hide", "HIDE", "Hide VC"),
    ("show", "UNHIDE", "Unhide VC"), ("logout", "VCKICK", "Kick User"),
    ("ban", "BANUSER", "Ban User"), ("user_add", "PERMITUSER", "Permit User"), ("users", "LIMIT", "Set Limit"),
    ("signal", "STATUS", "VC Status"), ("edit", "RENAME", "Rename VC"),
    ("volume", "BITRATE", "Bitrate"), ("crown", "TRANSFER", "Transfer"),
]


def make_panel():
    W, H = 1150, 580
    cols, tw, th, gx, gy = 5, 195, 150, 20, 25
    img = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    f_small = ImageFont.truetype("/usr/share/fonts/opentype/inter/Inter-Bold.otf", 13)
    f_big = ImageFont.truetype("/usr/share/fonts/opentype/inter/Inter-SemiBold.otf", 17)
    for i, (icon, cap, label) in enumerate(PANEL):
        r, c = divmod(i, cols)
        x = 45 + c * (tw + gx)
        y = 45 + r * (th + gy)
        d.rounded_rectangle([x, y, x + tw, y + th], radius=22, fill=(4, 14, 8, 255), outline=(10, 28, 16, 255))
        ic = Image.open(os.path.join(OUT, f"{icon}.png"))
        ic = ic.crop(ic.getbbox())
        k = 56 / max(ic.size)
        ic = ic.resize((round(ic.width * k), round(ic.height * k)), Image.LANCZOS)
        img.alpha_composite(ic, (x + (tw - ic.width) // 2, y + 20 + (56 - ic.height) // 2))
        d.text((x + tw / 2, y + 100), cap, font=f_small, fill=(74, 222, 128, 255), anchor="mm")
        d.text((x + tw / 2, y + 122), label, font=f_big, fill=(255, 255, 255, 255), anchor="mm")
    img.convert("RGB").save(os.path.join(ROOT, "assets", "panel.png"), optimize=True)
    print("panel written")


if __name__ == "__main__":
    main()
