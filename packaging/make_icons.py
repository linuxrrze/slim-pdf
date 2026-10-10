"""Draw the application icon and write it in the formats the packages need.

    python packaging/make_icons.py            # -> packaging/build-icons/
    python packaging/make_icons.py --package  # also src/pdfsigner/gui/icon.png

A document with a signature stroke and a seal, on a blue rounded square.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "build-icons"
S = 1024


def draw() -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((40, 40, S - 40, S - 40), radius=200, fill=(33, 95, 170, 255))
    # page with a folded corner
    x1, y1, x2, y2, fold = 250, 170, 774, 854, 150
    d.polygon([(x1, y1), (x2 - fold, y1), (x2, y1 + fold), (x2, y2), (x1, y2)],
              fill=(255, 255, 255, 255))
    d.polygon([(x2 - fold, y1), (x2 - fold, y1 + fold), (x2, y1 + fold)],
              fill=(200, 214, 232, 255))
    for i in range(4):  # text lines
        y = 330 + i * 70
        d.rounded_rectangle((x1 + 60, y, x2 - (60 if i else 200), y + 26), radius=13,
                            fill=(196, 204, 214, 255))
    # signature stroke
    pts = []
    for t in range(0, 101):
        u = t / 100
        x = x1 + 60 + u * 380
        y = 690 - 70 * math.sin(u * math.pi * 3) * (1 - u * 0.5) - u * 30
        pts.append((x, y))
    d.line(pts, fill=(25, 50, 120, 255), width=26, joint="curve")
    # seal
    cx, cy, r = 690, 760, 95
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(200, 40, 40, 255))
    d.ellipse((cx - r + 22, cy - r + 22, cx + r - 22, cy + r - 22),
              outline=(255, 220, 220, 255), width=12)
    d.line([(cx - 40, cy), (cx - 8, cy + 32), (cx + 45, cy - 35)],
           fill=(255, 255, 255, 255), width=22, joint="curve")
    return im


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    im = draw()
    im.save(OUT / "pdfsigner.png")
    im.save(OUT / "pdfsigner.ico",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    im.save(OUT / "pdfsigner.icns")
    if "--package" in sys.argv:
        im.resize((256, 256), Image.LANCZOS).save(ROOT / "src/pdfsigner/gui/icon.png")
    print(f"icons written to {OUT}")


if __name__ == "__main__":
    main()
