"""Generate sanitized slider-captcha fixture images for offline tests.

Run once to create (or re-create) the PNG fixtures:

    python tests/fixtures/captcha/generate.py

Each fixture is a 280×160 greyscale PNG image that simulates a TikTok slider
puzzle background.  The "gap" is a rectangular shadow column at a known X
offset; the gap-detection algorithm should return that offset within ±1 px.

No real screenshots are included — all content is synthetic.
"""

import io
from pathlib import Path

from PIL import Image, ImageDraw

_FIXTURES = [
    ("slider_gap40.png",  40),
    ("slider_gap120.png", 120),
    ("slider_gap200.png", 200),
]

_WIDTH  = 280
_HEIGHT = 160
_GAP_W  = 10   # shadow column width (pixels)
_BG     = 160  # background mean brightness
_SHADOW = 60   # gap shadow brightness (distinctly darker)


def make_slider_fixture(gap_x: int, width: int = _WIDTH, height: int = _HEIGHT) -> bytes:
    """Create a greyscale PNG with a dark shadow column at ``gap_x``.

    The surrounding background is a uniform mid-grey with subtle texture; the
    gap column is noticeably darker so the column-minimum detection algorithm
    reliably finds it.
    """
    import random
    rng = random.Random(gap_x)  # deterministic per gap position

    img = Image.new("L", (width, height), color=_BG)
    draw = ImageDraw.Draw(img)

    # Light texture: scatter a few slightly darker patches so the image
    # isn't a flat uniform block (which would also confuse min-column detection).
    for _ in range(200):
        x = rng.randint(0, width - 1)
        y = rng.randint(0, height - 1)
        # Keep texture brighter than the shadow so the gap is still darkest.
        shade = rng.randint(_SHADOW + 20, _BG + 10)
        img.putpixel((x, y), min(255, shade))

    # Draw the gap shadow column (clearly darkest region).
    for col in range(gap_x, min(gap_x + _GAP_W, width)):
        for row in range(height):
            img.putpixel((col, row), _SHADOW)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> None:
    out_dir = Path(__file__).parent
    for filename, gap_x in _FIXTURES:
        path = out_dir / filename
        path.write_bytes(make_slider_fixture(gap_x))
        print(f"wrote {path}  (gap_x={gap_x})")


if __name__ == "__main__":
    main()
