"""Generate the application icon from the same starburst the page draws.

One source of truth for the mark: the geometry below and the SVG in
ClaudeEnvGuard.html are the same twelve spokes, so the taskbar icon and the page
header cannot drift apart. Colours are the two taken from the Claude wordmark
image — coral #D97757 and off-white #F5F4EF on the near-black #191917 plate.

Run: python tools/make_icon.py
"""

import math
import os

from PIL import Image, ImageDraw

ORANGE = (217, 119, 87, 255)
INK = (25, 25, 23, 255)
SIZE = 1024  # drawn large, downsampled by Windows


def draw_mark(size=SIZE, plate=True, spokes=12):
    """Rounded dark plate with an orange starburst, transparent outside the plate."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if plate:
        radius = int(size * 0.22)
        draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=INK)

    center = size / 2
    # Spoke length and thickness scale with the canvas so every export size looks
    # the same. The gap in the middle is what makes it read as an asterisk rather
    # than a solid dot.
    outer = size * 0.385
    inner = size * 0.155
    width = max(2, int(size * 0.068))
    for index in range(spokes):
        angle = (2 * math.pi / spokes) * index - math.pi / 2
        x0 = center + inner * math.cos(angle)
        y0 = center + inner * math.sin(angle)
        x1 = center + outer * math.cos(angle)
        y1 = center + outer * math.sin(angle)
        draw.line([x0, y0, x1, y1], fill=ORANGE, width=width)
        # Round caps: PIL has no capstyle, so stamp a circle at both ends.
        for x, y in ((x0, y0), (x1, y1)):
            draw.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=ORANGE)

    return image


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets = os.path.join(root, "assets")
    os.makedirs(assets, exist_ok=True)

    mark = draw_mark()
    png_path = os.path.join(assets, "claude-starburst.png")
    mark.save(png_path, "PNG")

    ico_path = os.path.join(assets, "claude-env-guard.ico")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    mark.save(ico_path, "ICO", sizes=sizes)

    print("wrote", png_path)
    print("wrote", ico_path)


if __name__ == "__main__":
    main()
