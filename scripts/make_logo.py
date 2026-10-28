#!/usr/bin/env python3
"""Generate a 1024x1024 logo for the Cortensor Canary BUIDL.

Deterministic, dependency-light (Pillow only). Run from the repo root:
    python3 scripts/make_logo.py artifacts/logo-1024.png
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SIZE = 1024
INK = (11, 17, 32)          # #0B1120
INK_2 = (17, 26, 45)        # #111A2D
AMBER = (245, 197, 66)      # #F5C542
LIME = (163, 230, 53)       # #A3E635
WHITE = (248, 250, 252)

FONT_CANDIDATES = [
    "/System/Library/Fonts/SFNSDisplay-Bold.otf",
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/Library/Fonts/Arial Bold.ttf",
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    grad = Image.new("RGB", (1, size), top)
    px = grad.load()
    for y in range(size):
        t = y / max(1, size - 1)
        px[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
    return grad.resize((size, size), Image.NEAREST)


def main(out: str) -> None:
    img = vertical_gradient(SIZE, INK_2, INK).convert("RGB")

    # Warm glow behind the badge, so the mark reads on dark and light backgrounds.
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((150, 90, 880, 820), fill=AMBER + (46,))
    img = Image.alpha_composite(img.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(120)))

    # Subtle grid, 64px, 4% white.
    grid = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for i in range(0, SIZE, 64):
        gd.line([(i, 0), (i, SIZE)], fill=(255, 255, 255, 9), width=1)
        gd.line([(0, i), (SIZE, i)], fill=(255, 255, 255, 9), width=1)
    img = Image.alpha_composite(img, grid)

    # Gate: two posts and a lintel, drawn as rounded strokes.
    gate = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(gate)
    gdraw.rounded_rectangle((268, 300, 756, 340), radius=20, fill=WHITE + (235,))
    gdraw.rounded_rectangle((286, 300, 326, 690), radius=20, fill=WHITE + (235,))
    gdraw.rounded_rectangle((700, 300, 740, 690), radius=20, fill=WHITE + (235,))

    # Check passing through the gate: the "replacement accepted" signal.
    check = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    cdraw = ImageDraw.Draw(check)
    cdraw.line([(372, 520), (470, 620), (700, 392)], fill=AMBER + (255,), width=64, joint="curve")
    for xy in ((372, 520), (470, 620), (700, 392)):
        cdraw.ellipse((xy[0] - 32, xy[1] - 32, xy[0] + 32, xy[1] + 32), fill=AMBER + (255,))

    # Small "canary" dot leaving the gate — the agent that reports the verdict.
    cdraw.ellipse((742, 648, 806, 712), fill=LIME + (255,))

    img = Image.alpha_composite(img, gate)
    img = Image.alpha_composite(img, check)

    draw = ImageDraw.Draw(img)
    font = load_font(96)
    text = "CANARY"
    spacing = 10
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = (SIZE - total) / 2
    for ch, w in zip(text, widths):
        draw.text((x, 776), ch, font=font, fill=WHITE + (235,))
        x += w + spacing

    sub = load_font(40)
    label = "RELEASE GATE FOR LLM ENDPOINTS"
    lw = draw.textlength(label, font=sub)
    draw.text(((SIZE - lw) / 2, 896), label, font=sub, fill=(148, 163, 184, 255))

    img.convert("RGB").save(out, "PNG", optimize=True)
    print(f"wrote {out} {img.size}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "artifacts/logo-1024.png")
