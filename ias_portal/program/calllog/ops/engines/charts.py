"""Small PNG charts for the e-mails (Outlook does not draw SVG): a ring ("donut") pie, in pure Python - no package needed.

``ring_png(path, segments)`` writes the chart; ``segments`` = [(value, "#rrggbb")]. Drawn 3x larger and averaged down, so the
edges are smooth. A chart with nothing to show is a grey ring."""
import math
import struct
import zlib

TRACK = (229, 231, 235)


def _rgb(colour):
    c = colour.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _png(path, width, height, rows):
    """Write RGBA ``rows`` (bytes per row, without the filter byte) as a PNG file."""
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ring_png(path, segments, size=180, hole=0.6, scale=3):
    """A ring pie of ``segments`` [(value, colour)] clockwise from 12 o'clock, ``size`` px square, transparent outside."""
    total = float(sum(max(0.0, float(v or 0)) for v, _ in segments))
    stops, acc = [], 0.0
    for v, colour in segments:
        v = max(0.0, float(v or 0))
        if v <= 0 or total <= 0:
            continue
        acc += v / total
        stops.append((acc, _rgb(colour)))
    big = size * scale
    c = (big - 1) / 2.0
    r_out, r_in = big / 2.0 - scale, (big / 2.0 - scale) * hole
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            rs = gs = bs = a = 0
            for sy in range(scale):
                for sx in range(scale):
                    px, py = x * scale + sx - c, y * scale + sy - c
                    d = math.hypot(px, py)
                    if d > r_out or d < r_in:
                        continue
                    if not stops:
                        col = TRACK
                    else:
                        frac = ((math.atan2(px, -py) / (2 * math.pi)) + 1.0) % 1.0     # 0 at 12 o'clock, clockwise
                        col = next((cc for edge, cc in stops if frac <= edge), stops[-1][1])
                    rs += col[0]
                    gs += col[1]
                    bs += col[2]
                    a += 1
            n = scale * scale
            if a:
                row += bytes((rs // a, gs // a, bs // a, 255 * a // n))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    _png(path, size, size, rows)
    return path
