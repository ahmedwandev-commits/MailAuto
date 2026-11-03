"""Small PNG charts for the e-mails (Outlook does not draw SVG): a ring ("donut") pie, in pure Python - no package needed.

``ring_png(path, segments)`` writes the chart; ``segments`` = [(value, "#rrggbb")]. Drawn 3x larger and averaged down, so the
edges are smooth. A chart with nothing to show is a grey ring."""
import math
import os
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


INK = (31, 41, 51)
MUTED = (107, 114, 128)
# A smooth stroke font for the numbers and captions in the middle of a ring (no font file is needed on the machine): every
# letter is a few lines and arcs in a box 10 wide and 16 high (y grows downwards), drawn thick and anti-aliased.
def _arc(cx, cy, rx, ry, a0, a1, n=20):
    return [(cx + rx * math.cos(math.radians(a0 + (a1 - a0) * k / n)), cy + ry * math.sin(math.radians(a0 + (a1 - a0) * k / n)))
            for k in range(n + 1)]


_GLYPHS = {
    "0": [_arc(5, 8, 4, 7, 0, 360, 28)],
    "1": [[(2.5, 4), (6, 1), (6, 15)]],
    "2": [_arc(5, 4.6, 3.9, 3.6, 180, 410, 16) + [(1, 15), (9, 15)]],
    "3": [_arc(5, 4.4, 3.7, 3.4, 200, 450, 16), _arc(5, 11.6, 4, 3.6, 270, 520, 16)],
    "4": [[(7, 15), (7, 1), (0.8, 10.8), (9.6, 10.8)]],
    "5": [[(8.6, 1), (2.2, 1), (1.8, 7.4)] + _arc(4.9, 11, 4.1, 4, 236, 500, 16)],
    "6": [[(8, 1.4), (4.2, 3.6), (1.3, 9.5)], _arc(5, 10.8, 3.9, 4.2, 0, 360, 24)],
    "7": [[(1, 1), (9, 1), (3.6, 15)]],
    "8": [_arc(5, 4.5, 3.4, 3.5, 0, 360, 22), _arc(5, 11.5, 4, 3.5, 0, 360, 22)],
    "9": [[(2, 14.6), (5.8, 12.4), (8.7, 6.5)], _arc(5, 5.2, 3.9, 4.2, 0, 360, 24)],
    "%": [_arc(2.9, 3.4, 2.1, 2.1, 0, 360, 14), _arc(7.1, 12.6, 2.1, 2.1, 0, 360, 14), [(9, 1), (1, 15)]],
    "/": [[(8.5, 1), (1.5, 15)]],
    "-": [[(1.5, 8), (8.5, 8)]],
    "A": [[(0.6, 15), (5, 1), (9.4, 15)], [(2.4, 10.4), (7.6, 10.4)]],
    "C": [_arc(5.6, 8, 4.4, 7, -40, -320, 26)],
    "E": [[(8.5, 1), (1.5, 1), (1.5, 15), (8.5, 15)], [(1.5, 8), (7, 8)]],
    "I": [[(5, 1), (5, 15)]],
    "K": [[(1.5, 1), (1.5, 15)], [(8.5, 1), (1.5, 9)], [(3.6, 7.3), (8.6, 15)]],
    "L": [[(1.5, 1), (1.5, 15), (8.5, 15)]],
    "M": [[(1.2, 15), (1.2, 1), (5, 10), (8.8, 1), (8.8, 15)]],
    "N": [[(1.5, 15), (1.5, 1), (8.5, 15), (8.5, 1)]],
    "S": [_arc(5, 4.6, 3.8, 3.6, -25, -270, 16) + _arc(5, 11.4, 4, 3.6, -90, 155, 16)[1:]],
    "T": [[(1, 1), (9, 1)], [(5, 1), (5, 15)]],
    " ": [],
}
_ADVANCE = 11.6                                                       # one letter and the gap after it, in glyph units


def _text_width(text, u):
    """Pixels the ``text`` takes when a glyph unit is ``u`` pixels (without the last gap)."""
    return max(0.0, (len(text) * _ADVANCE - (_ADVANCE - 10)) * u)


def _draw_text(rows, size, text, cy, height, colour, weight=2.2):
    """Draw ``text`` centred on the picture with its middle on row ``cy``: letters of ``_GLYPHS``, ``height`` pixels tall, the
    stroke ``weight`` glyph units thick, soft edges. ``rows`` = a list of bytearrays of RGBA pixels."""
    u = height / 16.0
    half = weight * u / 2.0
    x0 = (size - _text_width(text, u)) / 2.0
    y0 = cy - height / 2.0
    for n, ch in enumerate(text):
        segs = []
        for line in _GLYPHS.get(ch, []):
            for a, b in zip(line, line[1:]):
                segs.append((x0 + (n * _ADVANCE + a[0]) * u, y0 + a[1] * u, x0 + (n * _ADVANCE + b[0]) * u, y0 + b[1] * u))
        if not segs:
            continue
        lo_x = int(min(min(s[0], s[2]) for s in segs) - half - 2)
        hi_x = int(max(max(s[0], s[2]) for s in segs) + half + 2)
        lo_y = int(min(min(s[1], s[3]) for s in segs) - half - 2)
        hi_y = int(max(max(s[1], s[3]) for s in segs) + half + 2)
        for y in range(max(0, lo_y), min(size, hi_y + 1)):
            row = rows[y]
            for x in range(max(0, lo_x), min(size, hi_x + 1)):
                px, py = x + 0.5, y + 0.5
                d = 1e9
                for ax, ay, bx, by in segs:
                    dx, dy = bx - ax, by - ay
                    ln = dx * dx + dy * dy
                    k = 0.0 if not ln else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ln))
                    e = math.hypot(px - ax - k * dx, py - ay - k * dy)
                    if e < d:
                        d = e
                cov = max(0.0, min(1.0, half + 0.5 - d))
                if cov <= 0:
                    continue
                o = x * 4
                da = row[o + 3] / 255.0
                out_a = cov + da * (1 - cov)
                for c in range(3):
                    row[o + c] = int(round((colour[c] * cov + row[o + c] * da * (1 - cov)) / out_a))
                row[o + 3] = int(round(out_a * 255))


# The numbers are written in the same font as the mail itself (Segoe UI from the machine's Windows fonts, else Arial, Calibri ...):
# the font file is read here - outlines, curves flattened, soft edges - so no package is needed. With no font file at all the
# stroke font above is used.
_FONT_FILES = {"bold": ("segoeuib.ttf", "arialbd.ttf", "calibrib.ttf", "tahomabd.ttf", "DejaVuSans-Bold.ttf"),
               "text": ("segoeui.ttf", "arial.ttf", "calibri.ttf", "tahoma.ttf", "DejaVuSans.ttf")}
_FONT_DIRS = (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"), "/usr/share/fonts/truetype/dejavu",
              "/Library/Fonts", "/System/Library/Fonts/Supplemental")
_fonts = {}


def _u16(d, o):
    return struct.unpack(">H", d[o:o + 2])[0]


def _i16(d, o):
    return struct.unpack(">h", d[o:o + 2])[0]


def _u32(d, o):
    return struct.unpack(">I", d[o:o + 4])[0]


class _TrueType:
    """The outlines and widths of the letters of one .ttf file (simple glyphs only: digits, letters and signs are)."""

    def __init__(self, path):
        d = open(path, "rb").read()
        if d[:4] not in (b"\x00\x01\x00\x00", b"true"):
            raise ValueError("not a TrueType font")
        self.d = d
        self.tables = {}
        for i in range(_u16(d, 4)):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i:28 + 16 * i])
            self.tables[tag.decode("latin1")] = off
        head = self.tables["head"]
        self.upm = _u16(d, head + 18)
        self.long_loca = _i16(d, head + 50)
        self.n_hm = _u16(d, self.tables["hhea"] + 34)
        self.cmap = self._cmap()
        self.cache = {}

    def _cmap(self):
        d, base = self.d, self.tables["cmap"]
        for i in range(_u16(d, base + 2)):
            platform, encoding, offset = struct.unpack(">HHI", d[base + 4 + 8 * i:base + 12 + 8 * i])
            if (platform, encoding) in ((3, 1), (0, 3), (0, 4)) and _u16(d, base + offset) == 4:
                return base + offset
        raise ValueError("no usable cmap")

    def glyph_id(self, ch):
        d, sub, code = self.d, self.cmap, ord(ch)
        segs = _u16(d, sub + 6) // 2
        ends, starts = sub + 14, sub + 16 + 2 * segs
        deltas, ranges = starts + 2 * segs, starts + 4 * segs
        for s in range(segs):
            if code <= _u16(d, ends + 2 * s):
                if code < _u16(d, starts + 2 * s):
                    return 0
                delta, ro = _i16(d, deltas + 2 * s), _u16(d, ranges + 2 * s)
                if ro == 0:
                    return (code + delta) & 0xFFFF
                gid = _u16(d, ranges + 2 * s + ro + 2 * (code - _u16(d, starts + 2 * s)))
                return (gid + delta) & 0xFFFF if gid else 0
        return 0

    def advance(self, gid):
        hmtx = self.tables["hmtx"]
        return _u16(self.d, hmtx + 4 * min(gid, self.n_hm - 1))

    def outline(self, ch):
        """[[(x, y)]] closed polygons of the letter in font units (y up), curves flattened; cached."""
        if ch in self.cache:
            return self.cache[ch]
        d, gid = self.d, self.glyph_id(ch)
        loca, glyf = self.tables["loca"], self.tables["glyf"]
        if self.long_loca:
            a, b = _u32(d, loca + 4 * gid), _u32(d, loca + 4 * gid + 4)
        else:
            a, b = 2 * _u16(d, loca + 2 * gid), 2 * _u16(d, loca + 2 * gid + 2)
        polys = []
        if b > a and _i16(d, glyf + a) > 0:
            g = glyf + a
            nc = _i16(d, g)
            ends = [_u16(d, g + 10 + 2 * i) for i in range(nc)]
            n = ends[-1] + 1
            p = g + 12 + 2 * nc + _u16(d, g + 10 + 2 * nc)
            flags = []
            while len(flags) < n:
                f = d[p]
                p += 1
                flags.append(f)
                if f & 8:
                    flags.extend([f] * d[p])
                    p += 1
            coords = []
            for short, same, step in ((2, 16, "x"), (4, 32, "y")):
                v, out = 0, []
                for f in flags:
                    if f & short:
                        dv = d[p]
                        p += 1
                        v += dv if f & same else -dv
                    elif not f & same:
                        v += _i16(d, p)
                        p += 2
                    out.append(v)
                coords.append(out)
            start = 0
            for e in ends:
                pts = [(coords[0][i], coords[1][i], bool(flags[i] & 1)) for i in range(start, e + 1)]
                start = e + 1
                polys.append(self._flatten(pts))
        self.cache[ch] = polys
        return polys

    @staticmethod
    def _flatten(pts):
        if not pts[0][2]:
            if pts[-1][2]:
                pts = [pts[-1]] + pts[:-1]
            else:
                pts = [((pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2, True)] + pts
        poly, cur, ctrl = [(pts[0][0], pts[0][1])], (pts[0][0], pts[0][1]), None

        def quad(p0, c, p1):
            for k in range(1, 9):
                s = k / 8.0
                poly.append(((1 - s) ** 2 * p0[0] + 2 * (1 - s) * s * c[0] + s * s * p1[0],
                             (1 - s) ** 2 * p0[1] + 2 * (1 - s) * s * c[1] + s * s * p1[1]))
        for x, y, on in pts[1:] + [pts[0]]:
            if on:
                if ctrl is None:
                    poly.append((x, y))
                else:
                    quad(cur, ctrl, (x, y))
                    ctrl = None
                cur = (x, y)
            else:
                if ctrl is not None:
                    mid = ((ctrl[0] + x) / 2, (ctrl[1] + y) / 2)
                    quad(cur, ctrl, mid)
                    cur = mid
                ctrl = (x, y)
        return poly

    def digit_height(self):
        ys = [y for poly in self.outline("0") for _, y in poly]
        return max(ys) - min(ys)

    def width(self, text, scale, tracking=0.0):
        return sum(self.advance(self.glyph_id(ch)) * scale for ch in text) + tracking * self.upm * scale * max(0, len(text) - 1)


def _font(kind):
    if kind not in _fonts:
        _fonts[kind] = None
        for name in _FONT_FILES[kind]:
            for folder in _FONT_DIRS:
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    try:
                        _fonts[kind] = _TrueType(path)
                    except Exception:                               # noqa: BLE001 - an unreadable font: the next one
                        _fonts[kind] = None
                    if _fonts[kind]:
                        return _fonts[kind]
    return _fonts[kind]


def _draw_ttf(rows, size, text, cy, height, colour, font, tracking=0.0):
    """Write ``text`` in ``font`` centred on the picture, its digits ``height`` pixels tall and their middle on row ``cy``."""
    scale = height / font.digit_height()
    x = (size - font.width(text, scale, tracking)) / 2.0
    base = cy + height / 2.0
    edges = []
    for ch in text:
        for poly in font.outline(ch):
            pts = [(x + gx * scale, base - gy * scale) for gx, gy in poly]
            edges += [(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:] + pts[:1]) if a[1] != b[1]]
        x += font.advance(font.glyph_id(ch)) * scale + tracking * font.upm * scale
    if not edges:
        return
    sub = 4
    top, bottom = int(min(min(e[1], e[3]) for e in edges)) - 1, int(max(max(e[1], e[3]) for e in edges)) + 2
    for y in range(max(0, top), min(size, bottom)):
        cov = [0.0] * size
        for k in range(sub):
            ys = y + (k + 0.5) / sub
            hits = sorted((e[0] + (ys - e[1]) * (e[2] - e[0]) / (e[3] - e[1]), 1 if e[3] > e[1] else -1)
                          for e in edges if min(e[1], e[3]) <= ys < max(e[1], e[3]))
            wind, left = 0, 0.0
            for hx, direction in hits:
                if wind == 0:
                    left = hx
                wind += direction
                if wind == 0 and hx > left:
                    for px in range(max(0, int(left)), min(size, int(hx) + 1)):
                        cov[px] += (min(hx, px + 1) - max(left, px)) / sub
        row = rows[y]
        for px in range(size):
            c = min(1.0, cov[px])
            if c <= 0.003:
                continue
            o = px * 4
            da = row[o + 3] / 255.0
            out_a = c + da * (1 - c)
            for i in range(3):
                row[o + i] = int(round((colour[i] * c + row[o + i] * da * (1 - c)) / out_a))
            row[o + 3] = int(round(out_a * 255))


def _put_text(rows, size, text, cy, height, colour, kind, max_width, tracking=0.0, weight=2.2):
    """Write ``text`` in the middle of the picture, as tall as ``height`` but never wider than ``max_width``; returns the
    height used. The mail's own font when a font file is found, the stroke font otherwise."""
    font = _font(kind)
    if font:
        try:
            wide = font.width(text, height / font.digit_height(), tracking)
            if wide > max_width:
                height *= max_width / wide
            _draw_ttf(rows, size, text, cy, height, colour, font, tracking)
            return height
        except Exception:                                           # noqa: BLE001 - a font that cannot be drawn: the stroke font
            pass
    wide = _text_width(text, height / 16.0)
    if wide > max_width:
        height *= max_width / wide
    _draw_text(rows, size, text, cy, height, colour, weight)
    return height


def ring_png(path, segments, size=180, hole=0.6, scale=3, center="", caption=""):
    """A ring pie of ``segments`` [(value, colour)] clockwise from 12 o'clock, ``size`` px square, transparent outside.
    ``center`` = the number written in the hole, ``caption`` = a small word under it (both upper case / digits)."""
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
        rows.append(row)
    center, caption = str(center or ""), str(caption or "").upper()
    if center:
        inner = 2 * r_in / scale * 0.82                                     # the room inside the ring
        mid = size / 2.0 - (size * 0.055 if caption else 0)
        height = _put_text(rows, size, center, mid, size * 0.2, INK, "bold", inner)
        if caption:
            small = size * 0.072
            _put_text(rows, size, caption, mid + height / 2 + size * 0.045 + small / 2, small, MUTED, "text", inner * 0.92,
                      tracking=0.06, weight=1.9)
    _png(path, size, size, [bytes(r) for r in rows])
    return path
