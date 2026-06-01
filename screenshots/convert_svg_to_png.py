#!/usr/bin/env python
"""Convert Textual SVG screenshots to PNG using svglib + Pillow."""

import os
import sys
from pathlib import Path

from svglib.svglib import svg2rlg
from reportlab.graphics import renderPM


def main():
    screenshot_dir = Path(__file__).parent
    svgs = sorted(screenshot_dir.glob("*.svg"))

    for svg_path in svgs:
        png_path = svg_path.with_suffix(".png")
        print(f"Converting {svg_path.name} -> {png_path.name}...")
        drawing = svg2rlg(svg_path)
        # Scale up for crisp rendering
        drawing.scale(2, 2)
        renderPM.drawToFile(drawing, str(png_path), fmt="PNG")
        size = png_path.stat().st_size
        print(f"  Done ({size / 1024:.0f} KB)")

    print(f"All {len(svgs)} screenshots converted to PNG.")


if __name__ == "__main__":
    main()
