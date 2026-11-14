"""Draws the Zetta mark the sidebar shows in its header, into zetta3d/icons/ (nothing to install: plain Python).

    python3 tools/make_icons.py

The compact mark from the brand kit (brand/logo/zetta3d-mark-compact-*.svg): the version made for small sizes,
"legible at 16px". Two tones, because a custom icon is drawn as it is — Blender does not tint it with the theme:
Paper for dark themes (Blender's default) and Ink for light ones.
"""

import os
import struct
import zlib

# The compact mark's three planes, in its 200 × 200 viewBox.
PLANES = [
    [(26, 44), (122, 29), (174, 44), (78, 59)],
    [(26, 100), (122, 85), (174, 100), (78, 115)],
    [(26, 156), (122, 141), (174, 156), (78, 171)],
]
TONES = {"paper": (0xFA, 0xF8, 0xF4), "ink": (0x15, 0x13, 0x0F)}
SIZE = 64
# The part of the viewBox drawn: the mark spans 26–174 × 29–171, with a little room around it.
VIEW = (16, 184)
SAMPLES = 8  # per side of a pixel: soft, even edges at the small size Blender draws it


def inside(x, y, quad):
    """A convex quad, either winding."""
    sign = 0
    for (x1, y1), (x2, y2) in zip(quad, quad[1:] + quad[:1]):
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if cross:
            if sign and (cross > 0) != (sign > 0):
                return False
            sign = cross
    return True


def coverage():
    low, high = VIEW
    unit = (high - low) / SIZE
    rows = []
    for py in range(SIZE):
        row = []
        for px in range(SIZE):
            hits = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x = low + (px + (sx + 0.5) / SAMPLES) * unit
                    y = low + (py + (sy + 0.5) / SAMPLES) * unit
                    hits += any(inside(x, y, quad) for quad in PLANES)
            row.append(hits / (SAMPLES * SAMPLES))
        rows.append(row)
    return rows


def png(rows, tone):
    raw = b"".join(b"\0" + b"".join(bytes((*tone, round(a * 255))) for a in row) for row in rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    out = os.path.join(os.path.dirname(__file__), "..", "zetta3d", "icons")
    os.makedirs(out, exist_ok=True)
    rows = coverage()
    for name, tone in TONES.items():
        with open(os.path.join(out, f"zetta-mark-{name}.png"), "wb") as handle:
            handle.write(png(rows, tone))
    print(f"Wrote zetta3d/icons: {', '.join(f'zetta-mark-{name}.png' for name in TONES)}")


if __name__ == "__main__":
    main()
