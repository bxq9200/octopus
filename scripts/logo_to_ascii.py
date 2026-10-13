"""
logo_to_ascii.py — 把章鱼 logo 转成密集 ASCII 字符画

输出：
  - frontend/src/components/login/octopus.txt        章鱼主体
  - frontend/src/components/login/octopus_title.txt  品牌字 OCTOPUS / AI VIDEO CREATION OS

颜色由前端 CSS gradient + background-clip:text 统一着色。
"""
import os
import random
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "frontend", "public", "logo.jpg")
DST = os.path.join(ROOT, "frontend", "src", "components", "login", "octopus.txt")
DST_TITLE = os.path.join(ROOT, "frontend", "src", "components", "login", "octopus_title.txt")

# 字符梯度：仅用 $@ 主体填充，背景为空格
RAMP = " $@"

# 输出网格尺寸 — 字符宽高比约 1:2
COLS = 90
ROWS = 45

BG_THRESHOLD = 10
DITHER = 35
SEED = 42


def _to_ascii(img: Image.Image, cols: int, rows: int, threshold: int, dither: int, ramp: str) -> str:
    img = img.resize((cols, rows), Image.LANCZOS)
    px = img.load()
    body_chars = ramp[1:] if ramp.startswith(" ") else ramp
    lines = []
    for y in range(rows):
        row = []
        for x in range(cols):
            v = px[x, y]
            if isinstance(v, tuple):
                r, g, b = v[:3]
                lum = 0.299 * r + 0.587 * g + 0.114 * b
            else:
                lum = float(v)
            if lum < threshold or not body_chars:
                row.append(" ")
                continue
            d = lum + random.uniform(-dither, dither)
            d = max(threshold, min(255, d))
            idx = int((d - threshold) / (255 - threshold) * len(body_chars))
            idx = max(0, min(len(body_chars) - 1, idx))
            row.append(body_chars[idx])
        lines.append("".join(row).rstrip())
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def gen_logo():
    random.seed(SEED)
    img = Image.open(SRC).convert("RGB")
    w, h = img.size
    img = img.crop((int(w * 0.08), int(h * 0.04), int(w * 0.92), int(h * 0.72)))
    text = _to_ascii(img, COLS, ROWS, BG_THRESHOLD, DITHER, RAMP)
    os.makedirs(os.path.dirname(DST), exist_ok=True)
    with open(DST, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[logo]  {COLS}x{len(text.splitlines())} chars -> {DST}")


def gen_title():
    """渲染品牌字 ASCII：
       - OCTOPUS — 手画字模，横划用 `-`、竖划用 `|`、拐角用 `+`
       - AI VIDEO CREATION OS — PIL 实心字 + 单字符 `·` ramp

    手画 7×7 字模保证笔画方向准确、字间距清楚。
    """
    main_text = "OCTOPUS"
    sub_text = "AI  VIDEO  CREATION  OS"

    main_ascii = _render_glyph_text(main_text, spacing=3)
    sub_ascii = _render_sub_with_dots(sub_text, target_cols=len(main_ascii.splitlines()[0]))

    text = main_ascii + "\n\n" + sub_ascii
    os.makedirs(os.path.dirname(DST_TITLE), exist_ok=True)
    with open(DST_TITLE, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[title] {len(text.splitlines())} rows -> {DST_TITLE}")


# 7 cols × 7 rows 字模 — 横用 -、竖用 |、拐角用 +
_GLYPHS = {
    "O": [
        "+-----+",
        "|     |",
        "|     |",
        "|     |",
        "|     |",
        "|     |",
        "+-----+",
    ],
    "C": [
        "+-----+",
        "|      ",
        "|      ",
        "|      ",
        "|      ",
        "|      ",
        "+-----+",
    ],
    "T": [
        "+--+--+",
        "   |   ",
        "   |   ",
        "   |   ",
        "   |   ",
        "   |   ",
        "   +   ",
    ],
    "P": [
        "+-----+",
        "|     |",
        "|     |",
        "+-----+",
        "|      ",
        "|      ",
        "+      ",
    ],
    "U": [
        "+     +",
        "|     |",
        "|     |",
        "|     |",
        "|     |",
        "|     |",
        "+-----+",
    ],
    "S": [
        "+-----+",
        "|      ",
        "|      ",
        "+-----+",
        "      |",
        "      |",
        "+-----+",
    ],
}
_GLYPH_H = 7
_GLYPH_W = 7


def _render_glyph_text(text: str, spacing: int = 3) -> str:
    rows = ["" for _ in range(_GLYPH_H)]
    for i, ch in enumerate(text):
        glyph = _GLYPHS.get(ch.upper())
        if glyph is None:
            continue
        if i > 0:
            for r in range(_GLYPH_H):
                rows[r] += " " * spacing
        for r in range(_GLYPH_H):
            rows[r] += glyph[r]
    return "\n".join(rows)


def _render_sub_with_dots(text: str, target_cols: int) -> str:
    """副标：用 PIL 渲染实心字，threshold 后只填 `·`；不留密度梯度，纯轮廓填充。

    宽度对齐到主标（target_cols），剧本居中放置。
    """
    font = ImageFont.truetype("impact.ttf", 110)
    tmp = Image.new("L", (10, 10), 0)
    d = ImageDraw.Draw(tmp)
    bbox = d.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    pad = 30
    canvas = Image.new("L", (w + pad * 2, h + pad * 2), 0)
    draw = ImageDraw.Draw(canvas)
    draw.text((pad - bbox[0], pad - bbox[1]), text, fill=255, font=font)

    aspect = canvas.height / canvas.width
    cols = target_cols
    rows_n = max(3, int(round(cols * aspect / 2)))
    img = canvas.resize((cols, rows_n), Image.LANCZOS)
    px = img.load()
    lines = []
    for y in range(rows_n):
        row = []
        for x in range(cols):
            v = px[x, y]
            row.append("·" if v >= 110 else " ")
        lines.append("".join(row).rstrip())
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def main():
    gen_logo()
    gen_title()


if __name__ == "__main__":
    main()
