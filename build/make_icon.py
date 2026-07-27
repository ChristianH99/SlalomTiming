"""Draw the Windows icon for the packaged build (shortcut, taskbar, Setup).

Generated rather than checked in as a binary: the app has no logo file of its own,
and the shapes below are just the design-system tokens from static/css/main.css —
--ink for the body, --flame for the hand — so the icon can't drift away from the
palette the app is actually painted in.

    python build/make_icon.py <output.ico>
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

INK = (0x16, 0x23, 0x3F, 255)      # --ink
FLAME = (0xE8, 0x49, 0x1D, 255)    # --flame
WHITE = (0xFF, 0xFF, 0xFF, 255)

SIZES = [16, 24, 32, 48, 64, 128, 256]
S = 1024  # drawn large, then downsampled — Windows shows all of these


def draw():
    image = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)

    # Body: the app's dark surface, rounded like every card in the UI.
    pen.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=INK)

    cx, cy, r = S / 2, S * 0.55, S * 0.30
    ring = int(S * 0.055)

    # Stopwatch: crown and stem above the dial.
    pen.rounded_rectangle(
        [cx - S * 0.075, cy - r - S * 0.135, cx + S * 0.075, cy - r + S * 0.01],
        radius=int(S * 0.028), fill=WHITE,
    )

    # Dial.
    pen.ellipse([cx - r, cy - r, cx + r, cy + r], outline=WHITE, width=ring)

    # The hand, in flame: the one moving part, and the app's accent colour.
    pen.line(
        [cx, cy, cx + r * 0.62, cy - r * 0.60],
        fill=FLAME, width=int(S * 0.052),
    )
    pen.ellipse(
        [cx - S * 0.035, cy - S * 0.035, cx + S * 0.035, cy + S * 0.035],
        fill=FLAME,
    )
    return image


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else 'SlalomTiming.ico')
    out.parent.mkdir(parents=True, exist_ok=True)
    master = draw()
    master.save(out, format='ICO', sizes=[(s, s) for s in SIZES])
    print(f'wrote {out} ({", ".join(str(s) for s in SIZES)})')


if __name__ == '__main__':
    main()
