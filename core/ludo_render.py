"""Ludo board renderer (Pillow). Draws a real 15x15 Ludo board and builds ANIMATED GIFs:

    roll_gif   - dice tumbling in the side panel
    move_gif   - pawn hopping cell by cell, kill burst + victim flying back to base, "home-ward" glow, home confetti
    win_gif    - winner celebration (confetti + trophy)
    still      - one static PNG of the current position

Everything is cached (board background + pawn sprites) and meant to run in a thread (asyncio.to_thread).
State handed in is plain data:  players = [{"color": "red", "name": "x", "pawns": [-1, 3, 20, 56], "kills": 0}, ...]
Pawn progress p:  -1 base | 0..50 track (relative to the colour's start) | 51..55 home column | 56 home.
"""
import io
import math
import random
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

CELL = 36
BOARD = CELL * 15                    # 540
PANEL = 210
W, H = BOARD + PANEL, BOARD
SS = 3                               # board supersampling

COLORS = ("red", "green", "yellow", "blue")           # clockwise = turn order
RGB = {"red": (231, 64, 64), "green": (52, 178, 88), "yellow": (250, 200, 40), "blue": (58, 120, 230)}
DARK = {"red": (150, 30, 40), "green": (24, 110, 52), "yellow": (176, 130, 10), "blue": (30, 70, 160)}
LIGHT = {"red": (255, 205, 205), "green": (200, 240, 210), "yellow": (255, 240, 190), "blue": (205, 224, 255)}
ORIGIN = {"red": (0, 0), "green": (9, 0), "yellow": (9, 9), "blue": (0, 9)}
START = {"red": 0, "green": 13, "yellow": 26, "blue": 39}
SAFE = {0, 8, 13, 21, 26, 34, 39, 47}

_TRACK_RAW = (
    [(x, 6) for x in range(1, 6)] + [(6, y) for y in range(5, -1, -1)] + [(7, 0), (8, 0)] +
    [(8, y) for y in range(1, 6)] + [(x, 6) for x in range(9, 15)] + [(14, 7), (14, 8)] +
    [(x, 8) for x in range(13, 8, -1)] + [(8, y) for y in range(9, 15)] + [(7, 14), (6, 14)] +
    [(6, y) for y in range(13, 8, -1)] + [(x, 8) for x in range(5, -1, -1)] + [(0, 7), (0, 6)]
)
TRACK = tuple(_TRACK_RAW)
assert len(TRACK) == 52
HOMECOL = {"red": [(x, 7) for x in range(1, 6)], "green": [(7, y) for y in range(1, 6)],
           "yellow": [(x, 7) for x in range(13, 8, -1)], "blue": [(7, y) for y in range(13, 8, -1)]}
CENTER_DIR = {"red": (-1, 0), "green": (0, -1), "yellow": (1, 0), "blue": (0, 1)}


# ---------------------------------------------------------------- fonts
@lru_cache(maxsize=None)
def font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


def clean(name: str, n: int = 11) -> str:
    s = "".join(c for c in (name or "") if 32 <= ord(c) < 0x250).strip() or "Player"
    return s if len(s) <= n else s[:n - 1] + "."


# ---------------------------------------------------------------- geometry
def cell_px(x: float, y: float):
    return ((x + 0.5) * CELL, (y + 0.5) * CELL)


def base_slot(color: str, i: int):
    ox, oy = ORIGIN[color]
    return ((ox + (2 if i % 2 == 0 else 4)) * CELL, (oy + (2 if i < 2 else 4)) * CELL)


def home_spot(color: str, i: int):
    dx, dy = CENTER_DIR[color]
    cx, cy = 7.5 * CELL, 7.5 * CELL
    off = (i - 1.5) * 0.2 * CELL
    return (cx + dx * 0.78 * CELL + (off if dx == 0 else 0), cy + dy * 0.78 * CELL + (off if dy == 0 else 0))


def pawn_xy(color: str, p: int, i: int):
    if p < 0:
        return base_slot(color, i)
    if p <= 50:
        return cell_px(*TRACK[(START[color] + p) % 52])
    if p <= 55:
        return cell_px(*HOMECOL[color][p - 51])
    return home_spot(color, i)


def track_index(color: str, p: int):
    return (START[color] + p) % 52 if 0 <= p <= 50 else None


# ---------------------------------------------------------------- board background
def _star(d, cx, cy, r, fill):
    pts = []
    for k in range(10):
        a = -math.pi / 2 + k * math.pi / 5
        rr = r if k % 2 == 0 else r * 0.42
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=fill)


@lru_cache(maxsize=1)
def board_bg() -> Image.Image:
    s = SS
    im = Image.new("RGB", (BOARD * s, BOARD * s), (250, 246, 236))
    d = ImageDraw.Draw(im)
    C = CELL * s

    def box(x0, y0, x1, y1, **kw):
        d.rectangle([x0 * C, y0 * C, x1 * C - 1, y1 * C - 1], **kw)

    # quadrants
    for col, (ox, oy) in ORIGIN.items():
        box(ox, oy, ox + 6, oy + 6, fill=RGB[col])
        d.rounded_rectangle([(ox + 0.8) * C, (oy + 0.8) * C, (ox + 5.2) * C, (oy + 5.2) * C], radius=int(0.5 * C),
                            fill=LIGHT[col])
        for i in range(4):
            bx, by = base_slot(col, i)
            r = 0.62 * C
            d.ellipse([bx * s - r, by * s - r, bx * s + r, by * s + r], fill=RGB[col], outline=DARK[col], width=int(0.08 * C))
    # track cells
    for idx, (x, y) in enumerate(TRACK):
        fill = (255, 255, 255)
        for col in COLORS:
            if idx == START[col]:
                fill = RGB[col]
        d.rectangle([x * C, y * C, (x + 1) * C - 1, (y + 1) * C - 1], fill=fill, outline=(70, 70, 80), width=max(1, s))
    for col in COLORS:
        for (x, y) in HOMECOL[col]:
            d.rectangle([x * C, y * C, (x + 1) * C - 1, (y + 1) * C - 1], fill=RGB[col], outline=(70, 70, 80), width=max(1, s))
    # safe stars
    for idx in SAFE:
        x, y = TRACK[idx]
        cx, cy = (x + 0.5) * C, (y + 0.5) * C
        starts = [START[c] for c in COLORS]
        _star(d, cx, cy, 0.34 * C, (255, 255, 255) if idx in starts else (150, 150, 160))
    # centre: four triangles
    c0, c1 = 6 * C, 9 * C
    mid = 7.5 * C
    tri = {"red": [(c0, c0), (c0, c1), (mid, mid)], "green": [(c0, c0), (c1, c0), (mid, mid)],
           "yellow": [(c1, c0), (c1, c1), (mid, mid)], "blue": [(c0, c1), (c1, c1), (mid, mid)]}
    for col, pts in tri.items():
        d.polygon(pts, fill=RGB[col], outline=(60, 60, 70))
    # arrows on the home-column entrances
    for col in COLORS:
        x, y = TRACK[(START[col] - 2) % 52] if False else HOMECOL[col][0]
    d.rectangle([0, 0, BOARD * s - 1, BOARD * s - 1], outline=(40, 40, 50), width=3 * s)
    return im.resize((BOARD, BOARD), Image.LANCZOS)


# ---------------------------------------------------------------- sprites
@lru_cache(maxsize=None)
def pawn_sprite(color: str, num: int, size: int = 34) -> Image.Image:
    s = 4
    N = size * s
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx = N / 2
    # shadow
    d.ellipse([N * .18, N * .78, N * .82, N * .96], fill=(0, 0, 0, 90))
    # body (cone) + head
    d.polygon([(cx, N * .22), (N * .82, N * .86), (N * .18, N * .86)], fill=RGB[color], outline=DARK[color])
    d.ellipse([N * .22, N * .70, N * .78, N * .92], fill=RGB[color], outline=DARK[color], width=s)
    d.ellipse([N * .30, N * .05, N * .70, N * .45], fill=RGB[color], outline=DARK[color], width=s)
    d.ellipse([N * .38, N * .11, N * .52, N * .22], fill=(255, 255, 255, 170))      # shine
    # number badge
    r = N * .19
    d.ellipse([cx - r, N * .56 - r, cx + r, N * .56 + r], fill=(255, 255, 255, 255), outline=DARK[color], width=s)
    f = font(int(N * .30))
    d.text((cx, N * .56), str(num), font=f, fill=DARK[color], anchor="mm")
    return im.resize((size, size), Image.LANCZOS)


PIPS = {1: [(0, 0)], 2: [(-1, -1), (1, 1)], 3: [(-1, -1), (0, 0), (1, 1)], 4: [(-1, -1), (1, -1), (-1, 1), (1, 1)],
        5: [(-1, -1), (1, -1), (0, 0), (-1, 1), (1, 1)], 6: [(-1, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (1, 1)]}


@lru_cache(maxsize=None)
def die_sprite(value: int, size: int, tint: tuple = (255, 255, 255)) -> Image.Image:
    s = 4
    N = size * s
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([N * .04, N * .08, N * .96, N * .98], radius=int(N * .2), fill=(0, 0, 0, 80))
    d.rounded_rectangle([N * .02, N * .02, N * .94, N * .94], radius=int(N * .2), fill=tint, outline=(60, 60, 70), width=s * 2)
    for (px, py) in PIPS[value]:
        cx, cy = N * .48 + px * N * .22, N * .48 + py * N * .22
        r = N * .075 if value != 1 else N * .11
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(200, 40, 50) if value == 1 else (30, 30, 40))
    return im.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- drawing one frame
def _positions(players, override=None):
    """-> list of dicts {x, y, color, num, lift, glow} ; override = {(pi, k): (x, y)} lets an animation place a pawn."""
    items, groups = [], {}
    for pi, pl in enumerate(players):
        for k, p in enumerate(pl["pawns"]):
            if override and (pi, k) in override:
                continue
            x, y = pawn_xy(pl["color"], p, k)
            items.append({"pi": pi, "k": k, "x": x, "y": y, "color": pl["color"], "num": k + 1, "p": p})
            if 0 <= p <= 55:
                groups.setdefault((round(x), round(y)), []).append(items[-1])
    for g in groups.values():
        if len(g) > 1:
            pat = [(-7, -6), (7, -6), (-7, 6), (7, 6)]
            for j, it in enumerate(g):
                it["x"] += pat[j % 4][0]
                it["y"] += pat[j % 4][1]
    return items


def _paste_pawn(im, x, y, color, num, lift=0, scale=1.0, glow=False, angle=0):
    sp = pawn_sprite(color, num)
    if scale != 1.0 or angle:
        n = max(8, int(sp.width * scale))
        sp = sp.resize((n, n), Image.LANCZOS)
        if angle:
            sp = sp.rotate(angle, resample=Image.BICUBIC, expand=False)
    if glow:
        g = Image.new("RGBA", im.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(g)
        for r, a in ((24, 40), (18, 70), (12, 110)):
            gd.ellipse([x - r, y - lift - r, x + r, y - lift + r], fill=(255, 225, 90, a))
        im.alpha_composite(g)
    im.alpha_composite(sp, (int(x - sp.width / 2), int(y - lift - sp.height * 0.62)))


def draw_panel(im, players, turn, dice, caption, dice_img=None, banner=None):
    d = ImageDraw.Draw(im)
    x0 = BOARD
    d.rectangle([x0, 0, W, H], fill=(30, 31, 38))
    d.rectangle([x0, 0, x0 + 3, H], fill=(20, 20, 26))
    d.text((x0 + PANEL / 2, 22), "LUDO", font=font(26), fill=(255, 200, 60), anchor="mm")
    y = 52
    for i, pl in enumerate(players):
        col = pl["color"]
        cur = i == turn
        d.rounded_rectangle([x0 + 10, y, x0 + PANEL - 10, y + 52], radius=10, fill=(52, 54, 64) if not cur else DARK[col],
                            outline=RGB[col] if cur else (70, 72, 84), width=3 if cur else 1)
        d.ellipse([x0 + 18, y + 10, x0 + 38, y + 30], fill=RGB[col], outline=(255, 255, 255), width=2)
        d.text((x0 + 46, y + 8), clean(pl["name"]) + (" (away)" if pl.get("away") else ""), font=font(14), fill=(255, 255, 255))
        home = sum(1 for p in pl["pawns"] if p >= 56)
        d.text((x0 + 18, y + 34), f"Home {home}/4   Kills {pl.get('kills', 0)}", font=font(12), fill=(200, 205, 220))
        if cur:
            d.polygon([(x0 + PANEL - 24, y + 20), (x0 + PANEL - 14, y + 26), (x0 + PANEL - 24, y + 32)], fill=(255, 255, 255))
        y += 58
    # dice zone
    dy = 52 + 4 * 58 + 6
    d.rounded_rectangle([x0 + 10, dy, x0 + PANEL - 10, dy + 92], radius=12, fill=(40, 42, 52))
    if dice_img is not None:
        im.alpha_composite(dice_img, (int(x0 + PANEL / 2 - dice_img.width / 2), int(dy + 46 - dice_img.height / 2)))
    elif dice:
        sp = die_sprite(dice, 62)
        im.alpha_composite(sp, (int(x0 + PANEL / 2 - 31), dy + 15))
    else:
        d.text((x0 + PANEL / 2, dy + 46), "Roll the dice!", font=font(15), fill=(180, 185, 200), anchor="mm")
    if caption:
        words, lines, cur_l = caption.split(), [], ""
        for w in words:
            if len(cur_l) + len(w) + 1 > 21:
                lines.append(cur_l)
                cur_l = w
            else:
                cur_l = (cur_l + " " + w).strip()
        lines.append(cur_l)
        for j, ln in enumerate(lines[:3]):
            d.text((x0 + PANEL / 2, dy + 108 + j * 17), ln, font=font(13), fill=(255, 235, 170), anchor="mm")
    if banner:
        text, colr = banner
        d.rounded_rectangle([x0 + 10, 12, x0 + PANEL - 10, 40], radius=8, fill=colr)
        d.text((x0 + PANEL / 2, 26), text, font=font(18), fill=(255, 255, 255), anchor="mm")


def frame(players, turn=None, dice=0, caption="", override_pawns=None, extra=None, dice_img=None, banner=None, hl=None):
    """override_pawns: [(pi,k,x,y,opts)] pawns placed by an animation. extra: callable(RGBA image) draws effects."""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    im.paste(board_bg(), (0, 0))
    ov = {(pi, k): True for pi, k, *_ in (override_pawns or [])}
    if hl:                                                     # highlight selectable pawns (ring)
        d = ImageDraw.Draw(im)
        for (pi, k) in hl:
            x, y = pawn_xy(players[pi]["color"], players[pi]["pawns"][k], k)
            d.ellipse([x - 18, y - 18, x + 18, y + 18], outline=(255, 255, 255), width=3)
    for it in sorted(_positions(players, ov), key=lambda t: t["y"]):
        _paste_pawn(im, it["x"], it["y"], it["color"], it["num"])
    for (pi, k, x, y, opts) in (override_pawns or []):
        _paste_pawn(im, x, y, players[pi]["color"], k + 1, **opts)
    if extra:
        extra(im)
    draw_panel(im, players, turn, dice, caption, dice_img, banner)
    return im


# ---------------------------------------------------------------- outputs
def _png(im) -> bytes:
    b = io.BytesIO()
    im.convert("RGB").save(b, "PNG", optimize=True)
    return b.getvalue()


def _gif(frames, durations) -> bytes:
    pal = [f.convert("RGB").quantize(colors=96, method=Image.MEDIANCUT, dither=Image.NONE) for f in frames]
    b = io.BytesIO()
    pal[0].save(b, "GIF", save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=False, disposal=1)
    return b.getvalue()


def still(players, turn=None, dice=0, caption="", hl=None, banner=None) -> bytes:
    return _png(frame(players, turn, dice, caption, hl=hl, banner=banner))


def roll_gif(players, turn, final: int, caption="") -> tuple:
    """-> (gif bytes, seconds it plays)"""
    rnd = random.Random()
    frames, dur = [], []
    faces = [rnd.randint(1, 6) for _ in range(9)]
    faces.append(final)
    for i, v in enumerate(faces):
        ang = rnd.randint(-35, 35) if i < len(faces) - 1 else 0
        jitter = (rnd.randint(-10, 10), rnd.randint(-8, 8)) if i < len(faces) - 1 else (0, 0)
        die = die_sprite(v, 62).rotate(ang, resample=Image.BICUBIC)
        canvas = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
        canvas.alpha_composite(die, (17 + jitter[0] // 2, 17 + jitter[1] // 2))
        frames.append(frame(players, turn, 0, caption or "Rolling...", dice_img=canvas))
        dur.append(70 + i * 22)
    frames.append(frame(players, turn, final, caption, banner=(f"Rolled {final}!", (200, 70, 60) if final != 6 else (40, 150, 70))))
    dur.append(700)
    return _gif(frames, dur), sum(dur) / 1000


def _confetti(n, cx, cy, spread, rnd):
    return [{"x": cx, "y": cy, "vx": rnd.uniform(-spread, spread), "vy": rnd.uniform(-spread * 1.3, -spread * .2),
             "c": rnd.choice(list(RGB.values()) + [(255, 255, 255)]), "r": rnd.randint(2, 4)} for _ in range(n)]


def _step_confetti(parts):
    for p in parts:
        p["x"] += p["vx"]
        p["y"] += p["vy"]
        p["vy"] += 2.2


def _draw_confetti(im, parts):
    d = ImageDraw.Draw(im)
    for p in parts:
        d.rectangle([p["x"] - p["r"], p["y"] - p["r"], p["x"] + p["r"], p["y"] + p["r"]], fill=p["c"])


def _burst(im, x, y, t, color):
    """kill explosion at (x, y): t 0..1"""
    d = ImageDraw.Draw(im)
    r = 8 + t * 34
    d.ellipse([x - r, y - r, x + r, y + r], outline=(255, 255, 255), width=3)
    d.ellipse([x - r * .6, y - r * .6, x + r * .6, y + r * .6], outline=color, width=4)
    for k in range(12):
        a = k * math.pi / 6 + t
        r0, r1 = r * .5, r * 1.15
        d.line([x + r0 * math.cos(a), y + r0 * math.sin(a), x + r1 * math.cos(a), y + r1 * math.sin(a)],
               fill=(255, 220, 80) if k % 2 else (255, 90, 60), width=3)
    if t < 0.7:
        _star(d, x, y, 14 + t * 14, (255, 245, 150))


def move_gif(players, turn, dice, pi, k, p_from, p_to, killed=None, caption="") -> tuple:
    """Pawn k of player pi walks from p_from to p_to. killed = [(vpi, vk, vp)] pawns that get sent home.
    -> (gif bytes, seconds). `players` is the state AFTER the move; the moving pawn / victims are animated explicitly."""
    pl = players[pi]
    color = pl["color"]
    seq = [pawn_xy(color, p_from, k)]
    if p_from < 0:
        seq.append(pawn_xy(color, 0, k))
    else:
        for p in range(p_from + 1, p_to + 1):
            seq.append(pawn_xy(color, p, k))
    homeward = p_to >= 50 or p_from >= 50
    hold = {(vpi, vk) for vpi, vk, _ in (killed or [])}
    frames, dur = [], []

    def base(ovr, **kw):
        return frame(players, turn, dice, caption, override_pawns=ovr, **kw)

    # pawns of victims drawn at their old spot during the walk
    def victim_ovr(positions):
        return [(vpi, vk, *positions[(vpi, vk)], {}) for (vpi, vk, _) in (killed or [])]
    vpos = {(vpi, vk): pawn_xy(players[vpi]["color"], vp, vk) for vpi, vk, vp in (killed or [])}

    for a, b in zip(seq, seq[1:]):
        for t in (0.25, 0.5, 0.75, 1.0):
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            lift = math.sin(math.pi * t) * 9
            ovr = [(pi, k, x, y, {"lift": lift, "glow": homeward})] + victim_ovr(vpos)
            frames.append(base(ovr))
            dur.append(60)
        if p_from < 0:
            break
    end = seq[-1]
    # kill: burst on the landing cell, victims fly back
    if killed:
        for t in [i / 5 for i in range(1, 6)]:
            ovr = [(pi, k, end[0], end[1], {})] + victim_ovr(vpos)
            frames.append(base(ovr, extra=lambda im, t=t: _burst(im, end[0], end[1], t, RGB[color]),
                               banner=("KILL!", (200, 40, 50))))
            dur.append(70)
        for t in [i / 8 for i in range(1, 9)]:
            ovr = [(pi, k, end[0], end[1], {})]
            for vpi, vk, vp in killed:
                vx, vy = vpos[(vpi, vk)]
                bx, by = base_slot(players[vpi]["color"], vk)
                x, y = vx + (bx - vx) * t, vy + (by - vy) * t
                ovr.append((vpi, vk, x, y, {"lift": math.sin(math.pi * t) * 40, "angle": t * 360, "scale": 1.0 - 0.2 * math.sin(math.pi * t)}))
            frames.append(base(ovr, banner=("KILL!", (200, 40, 50))))
            dur.append(60)
    # home: confetti
    if p_to >= 56:
        rnd = random.Random()
        cx, cy = home_spot(color, k)
        parts = _confetti(46, cx, cy, 7, rnd)
        for i in range(10):
            _step_confetti(parts)
            frames.append(base([(pi, k, cx, cy, {"glow": True})], extra=lambda im, pp=[dict(q) for q in parts]: _draw_confetti(im, pp),
                               banner=("HOME!", (40, 150, 70))))
            dur.append(80)
    frames.append(frame(players, turn, dice, caption))
    dur.append(500)
    return _gif(frames, dur), sum(dur) / 1000


def _trophy(im, cx, cy, sc=1.0):
    d = ImageDraw.Draw(im)
    gold, dk = (255, 205, 50), (190, 140, 20)
    d.polygon([(cx - 38 * sc, cy - 48 * sc), (cx + 38 * sc, cy - 48 * sc), (cx + 26 * sc, cy + 6 * sc), (cx - 26 * sc, cy + 6 * sc)], fill=gold, outline=dk)
    d.arc([cx - 62 * sc, cy - 44 * sc, cx - 22 * sc, cy - 6 * sc], 90, 270, fill=gold, width=int(7 * sc))
    d.arc([cx + 22 * sc, cy - 44 * sc, cx + 62 * sc, cy - 6 * sc], -90, 90, fill=gold, width=int(7 * sc))
    d.rectangle([cx - 7 * sc, cy + 6 * sc, cx + 7 * sc, cy + 30 * sc], fill=gold, outline=dk)
    d.rectangle([cx - 28 * sc, cy + 30 * sc, cx + 28 * sc, cy + 44 * sc], fill=gold, outline=dk)
    _star(d, cx, cy - 22 * sc, 15 * sc, (255, 250, 200))


def win_gif(players, winner_pi, caption="") -> tuple:
    rnd = random.Random()
    pl = players[winner_pi]
    parts = _confetti(70, BOARD / 2, BOARD * .35, 9, rnd)
    frames, dur = [], []
    for i in range(16):
        _step_confetti(parts)
        if i % 3 == 0:
            parts += _confetti(18, rnd.randint(80, BOARD - 80), 20, 5, rnd)

        def fx(im, pp=[dict(q) for q in parts], i=i):
            ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(ov)
            od.rectangle([40, 150, BOARD - 40, 400], fill=(15, 15, 22, 215), outline=RGB[pl["color"]], width=5)
            im.alpha_composite(ov)
            _draw_confetti(im, pp)
            _trophy(im, BOARD / 2, 235 + math.sin(i / 2.2) * 6, 1.0 + 0.04 * math.sin(i / 2))
            d = ImageDraw.Draw(im)
            d.text((BOARD / 2, 318), "WINNER!", font=font(40), fill=RGB[pl["color"]], anchor="mm")
            d.text((BOARD / 2, 366), clean(pl["name"], 16), font=font(26), fill=(255, 255, 255), anchor="mm")
        frames.append(frame(players, winner_pi, 0, caption, extra=fx))
        dur.append(110)
    return _gif(frames, dur), sum(dur) / 1000
