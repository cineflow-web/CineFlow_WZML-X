"""
Minimal SWF compositor for "screen recording" style SWF files.

ffmpeg's swf demuxer exposes every embedded JPEG/PNG bitmap as a separate
"video frame" and ignores where the SWF places it (DefineShape bitmap fill +
PlaceObject2). For SWFs that update only parts of the screen, this means each
small patch gets stretched over the whole picture.

This module replays the main timeline itself:

    DefineBits*/DefineBitsLossless*  -> bitmap library
    DefineShape/2/3                  -> bitmap/solid filled polygons
    PlaceObject/2 / RemoveObject/2   -> display list
    ShowFrame                        -> one composited RGB frame

Only what those recordings need is supported (bitmap + solid fills, translate
and scale placement). Gradients, strokes, text, sprites, ActionScript and
clip layers are NOT rendered, so real vector animations/games are not
supported -- callers must check `SWFComposer.has_bitmaps` and the number of
layers that were drawn (`layers_drawn`).
"""

from __future__ import annotations

import lzma
import struct
import zlib
from io import BytesIO
from math import ceil, floor

from PIL import Image, ImageChops, ImageDraw

TWIPS = 20.0

_T_SHOW_FRAME = 1
_T_DEFINE_SHAPE = 2
_T_PLACE_OBJECT = 4
_T_REMOVE_OBJECT = 5
_T_DEFINE_BITS = 6
_T_JPEG_TABLES = 8
_T_BACKGROUND = 9
_T_LOSSLESS = 20
_T_JPEG2 = 21
_T_SHAPE2 = 22
_T_PLACE_OBJECT2 = 26
_T_REMOVE_OBJECT2 = 28
_T_SHAPE3 = 32
_T_JPEG3 = 35
_T_PLACE_OBJECT3 = 70
_T_SHAPE4 = 83
_T_JPEG4 = 90
_T_LOSSLESS2 = 36
_T_END = 0

_BITMAP_TAGS = {_T_DEFINE_BITS, _T_LOSSLESS, _T_JPEG2, _T_JPEG3, _T_LOSSLESS2, _T_JPEG4}
_SHAPE_TAGS = {_T_DEFINE_SHAPE: 1, _T_SHAPE2: 2, _T_SHAPE3: 3, _T_SHAPE4: 4}


class SWFError(Exception):
    pass


class _Bits:
    __slots__ = ("d", "p")

    def __init__(self, data, byte_pos=0):
        self.d = data
        self.p = byte_pos * 8

    def ub(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def sb(self, n):
        if n == 0:
            return 0
        v = self.ub(n)
        if v >> (n - 1):
            v -= 1 << n
        return v

    def align(self):
        self.p = (self.p + 7) & ~7

    def u8(self):
        self.align()
        v = self.d[self.p >> 3]
        self.p += 8
        return v

    def u16(self):
        self.align()
        v = struct.unpack_from("<H", self.d, self.p >> 3)[0]
        self.p += 16
        return v

    def skip(self, nbytes):
        self.align()
        self.p += nbytes * 8


def _rect(r):
    n = r.ub(5)
    vals = (r.sb(n), r.sb(n), r.sb(n), r.sb(n))
    r.align()
    return vals  # xmin, xmax, ymin, ymax (twips)


def _matrix(r):
    sx = sy = 1.0
    r0 = r1 = 0.0
    if r.ub(1):
        n = r.ub(5)
        sx, sy = r.sb(n) / 65536.0, r.sb(n) / 65536.0
    if r.ub(1):
        n = r.ub(5)
        r0, r1 = r.sb(n) / 65536.0, r.sb(n) / 65536.0
    n = r.ub(5)
    tx, ty = r.sb(n), r.sb(n)
    r.align()
    return (sx, r0, r1, sy, tx, ty)  # a, b, c, d, tx, ty


def _cxform(r, alpha=True):
    has_add, has_mult = r.ub(1), r.ub(1)
    n = r.ub(4)
    chans = 4 if alpha else 3
    if has_mult:
        for _ in range(chans):
            r.sb(n)
    if has_add:
        for _ in range(chans):
            r.sb(n)
    r.align()


def _inflate_salvage(data, chunk=512):
    """zlib-inflate `data`; on corruption keep everything decoded so far.
    Returns (bytes, error_or_None)."""
    d = zlib.decompressobj()
    out = bytearray()
    try:
        for i in range(0, len(data), chunk):
            out += d.decompress(data[i : i + chunk])
            if d.eof:
                break
    except zlib.error as e:
        return bytes(out), str(e)
    return bytes(out), None


def _load(path):
    with open(path, "rb") as f:
        raw = f.read()
    sig = raw[:3]
    warning = None
    if sig == b"FWS":
        body = raw[8:]
    elif sig == b"CWS":
        body, err = _inflate_salvage(raw[8:])
        if err:
            warning = f"corrupt compressed data after {len(body) // 1024} KB ({err})"
        if len(body) < 32:
            raise SWFError(f"SWF data unreadable: {err}")
    elif sig == b"ZWS":
        usize = struct.unpack_from("<I", raw, 4)[0] - 8
        try:
            body = lzma.decompress(
                raw[12:17] + struct.pack("<Q", usize) + raw[17:], format=lzma.FORMAT_ALONE
            )
        except lzma.LZMAError as e:
            # salvage a truncated/corrupt stream
            dec = lzma.LZMADecompressor(
                format=lzma.FORMAT_ALONE
            )
            hdr = raw[12:17] + struct.pack("<Q", usize)
            out = bytearray()
            try:
                out += dec.decompress(hdr + raw[17:])
            except lzma.LZMAError:
                pass
            body = bytes(out)
            warning = f"corrupt LZMA data after {len(body) // 1024} KB ({e})"
            if len(body) < 32:
                raise SWFError(f"SWF data unreadable: {e}")
    else:
        raise SWFError("Not a SWF file")
    return raw[3], body, warning


def _tags(body, pos):
    size = len(body)
    while pos + 2 <= size:
        h = struct.unpack_from("<H", body, pos)[0]
        pos += 2
        code, length = h >> 6, h & 63
        if length == 63:
            length = struct.unpack_from("<I", body, pos)[0]
            pos += 4
        if pos + length > size:  # truncated final tag (corrupt/short file)
            break
        yield code, pos, pos + length
        pos += length
        if code == _T_END:
            break


def _chain(edges):
    """Join polylines end-to-start into closed outlines."""
    by_start = {}
    for e in edges:
        by_start.setdefault(e[0], []).append(e)
    used, polys = set(), []
    for e in edges:
        if id(e) in used:
            continue
        used.add(id(e))
        pts = list(e)
        while pts[-1] != pts[0]:
            nxt = next((c for c in by_start.get(pts[-1], ()) if id(c) not in used), None)
            if nxt is None:
                break
            used.add(id(nxt))
            pts.extend(nxt[1:])
        polys.append(pts)
    return polys


def _fill_styles(r, ver):
    count = r.u8()
    if count == 0xFF and ver >= 2:
        count = r.u16()
    return _fill_styles_n(r, ver, count)


def _fill_styles_n(r, ver, count):
    styles = []
    for _ in range(count):
        ftype = r.u8()
        if ftype == 0x00:
            rgba = (r.u8(), r.u8(), r.u8(), r.u8() if ver >= 3 else 255)
            styles.append(("solid", rgba))
        elif ftype in (0x10, 0x12, 0x13):
            _matrix(r)
            r.align()
            ngrad = r.u8() & 0x0F
            for _ in range(ngrad):
                r.u8()
                r.skip(4 if ver >= 3 else 3)
            if ftype == 0x13:
                r.skip(2)
            styles.append(("gradient", None))
        elif 0x40 <= ftype <= 0x43:
            bid = r.u16()
            styles.append(("bitmap", (bid, _matrix(r))))
        else:
            raise SWFError(f"Unsupported fill style {ftype:#x}")
    return styles


def _line_styles(r, ver):
    count = r.u8()
    if count == 0xFF and ver >= 2:
        count = r.u16()
    for _ in range(count):
        r.u16()  # width
        if ver >= 4:  # LINESTYLE2
            r.align()
            flags = r.u16()
            if (flags >> 12) & 3 == 2:  # miter join
                r.u16()
            if (flags >> 11) & 1:  # has fill
                _fill_styles_n(r, ver, 1)
            else:
                r.skip(4)
        else:
            r.skip(4 if ver >= 3 else 3)


def _parse_shape(body, start, end, ver):
    """Returns (shape_id, [(style, [polygons in twips])])."""
    r = _Bits(body, start)
    shape_id = r.u16()
    _rect(r)
    if ver >= 4:
        _rect(r)  # edge bounds
        r.u8()  # scaling-stroke flags
    fills = list(_fill_styles(r, ver))
    _line_styles(r, ver)
    nfill, nline = r.ub(4), r.ub(4)
    base = 0
    x = y = 0
    fill0 = fill1 = 0
    edges = {}  # global fill index -> [polylines]

    def add(poly):
        if fill1:
            edges.setdefault(fill1, []).append(poly)
        if fill0:
            edges.setdefault(fill0, []).append(poly[::-1])

    while True:
        if r.ub(1):  # edge record
            if r.ub(1):  # straight
                nb = r.ub(4) + 2
                if r.ub(1):
                    dx, dy = r.sb(nb), r.sb(nb)
                elif r.ub(1):
                    dx, dy = 0, r.sb(nb)
                else:
                    dx, dy = r.sb(nb), 0
                add([(x, y), (x + dx, y + dy)])
                x, y = x + dx, y + dy
            else:  # quadratic curve, flattened
                nb = r.ub(4) + 2
                cx, cy = x + r.sb(nb), y + r.sb(nb)
                ax, ay = cx + r.sb(nb), cy + r.sb(nb)
                pts = [(x, y)]
                for i in range(1, 8):
                    t = i / 8.0
                    u = 1 - t
                    pts.append(
                        (
                            u * u * x + 2 * u * t * cx + t * t * ax,
                            u * u * y + 2 * u * t * cy + t * t * ay,
                        )
                    )
                pts.append((ax, ay))
                add(pts)
                x, y = ax, ay
        else:
            flags = r.ub(5)
            if flags == 0:
                break
            if flags & 1:
                n = r.ub(5)
                x, y = r.sb(n), r.sb(n)
            if flags & 2:
                v = r.ub(nfill)
                fill0 = base + v if v else 0
            if flags & 4:
                v = r.ub(nfill)
                fill1 = base + v if v else 0
            if flags & 8:
                r.ub(nline)
            if flags & 16:
                r.align()
                base = len(fills)
                fills.extend(_fill_styles(r, ver))
                _line_styles(r, ver)
                nfill, nline = r.ub(4), r.ub(4)
    out = []
    for idx, polylines in edges.items():
        if 1 <= idx <= len(fills):
            out.append((fills[idx - 1], _chain(polylines)))
    return shape_id, out


def _decode_jpeg(data):
    # old SWF encoders emit an erroneous EOI+SOI pair in front of the data
    while data[:4] == b"\xff\xd9\xff\xd8":
        data = data[4:]
    img = Image.open(BytesIO(data))
    img.load()
    return img.convert("RGBA") if img.mode in ("P", "RGBA", "LA") else img.convert("RGB")


def _decode_lossless(body, start, end, with_alpha):
    _, fmt, w, h = struct.unpack_from("<HBHH", body, start)
    pos = start + 7
    ncolors = 0
    if fmt == 3:
        ncolors = body[pos] + 1
        pos += 1
    data = zlib.decompress(body[pos:end])
    if fmt == 5:
        if len(data) < w * h * 4:
            data = data + b"\0" * (w * h * 4 - len(data))
        img = Image.frombuffer("RGBA", (w, h), data[: w * h * 4], "raw", "ARGB", 0, 1)
        if not with_alpha:
            img.putalpha(255)
        return img
    if fmt == 3:
        step = 4 if with_alpha else 3
        pal = data[: ncolors * step]
        stride = (w + 3) & ~3
        rows = data[ncolors * step : ncolors * step + stride * h]
        rows = rows + b"\0" * (stride * h - len(rows))
        idx = Image.frombytes("P", (stride, h), rows).crop((0, 0, w, h))
        rgb_pal = bytearray(768)
        lut = bytearray([255]) * 256
        for i in range(min(ncolors, 256)):
            c = pal[i * step : i * step + step]
            rgb_pal[i * 3 : i * 3 + 3] = c[:3]
            if with_alpha:
                lut[i] = c[3]
        raw_idx = Image.frombytes("L", (stride, h), rows).crop((0, 0, w, h))
        idx.putpalette(bytes(rgb_pal))
        img = idx.convert("RGB").convert("RGBA")
        if with_alpha:
            img.putalpha(raw_idx.point(list(lut)))
        return img
    if fmt == 4:  # 15-bit RGB555, rows padded to 4 bytes
        stride = ((w * 2) + 3) & ~3
        out = bytearray(w * h * 3)
        o = 0
        for row in range(h):
            base = row * stride
            for col in range(w):
                v = (data[base + col * 2] << 8) | data[base + col * 2 + 1]
                r5, g5, b5 = (v >> 10) & 31, (v >> 5) & 31, v & 31
                out[o] = (r5 << 3) | (r5 >> 2)
                out[o + 1] = (g5 << 3) | (g5 >> 2)
                out[o + 2] = (b5 << 3) | (b5 >> 2)
                o += 3
        return Image.frombytes("RGB", (w, h), bytes(out)).convert("RGBA")
    return None


class SWFComposer:
    def __init__(self, path):
        self.version, self.body, self.warning = _load(path)
        r = _Bits(self.body)
        xmin, xmax, ymin, ymax = _rect(r)
        self._xmin, self._ymin = xmin / TWIPS, ymin / TWIPS
        w, h = int(ceil((xmax - xmin) / TWIPS)), int(ceil((ymax - ymin) / TWIPS))
        # yuv420p needs even dimensions
        self.width, self.height = w + (w & 1), h + (h & 1)
        self.fps = struct.unpack_from("<H", self.body, r.p >> 3)[0] / 256.0 or 12.0
        self._first_tag = (r.p >> 3) + 4
        self.layers_drawn = 0
        self.frame_count = 0
        self.has_bitmaps = False
        self.problems = {}
        self.tag_counts = {}
        for code, _, _ in _tags(self.body, self._first_tag):
            self.tag_counts[code] = self.tag_counts.get(code, 0) + 1
            if code == _T_SHOW_FRAME:
                self.frame_count += 1
            elif code in _BITMAP_TAGS:
                self.has_bitmaps = True

    def why_unsupported(self):
        """Short human readable reason this SWF cannot be rendered (or '')."""
        tc = self.tag_counts
        if not self.has_bitmaps:
            extra = []
            if tc.get(39):
                extra.append("sprites/movie clips")
            if tc.get(12) or tc.get(59):
                extra.append("ActionScript")
            if tc.get(60):
                extra.append("embedded video stream")
            return "no bitmap frames" + (f" (found: {', '.join(extra)})" if extra else "")
        if not self.frame_count:
            return "no frames on the main timeline (content is inside sprites)"
        if tc.get(39) and not tc.get(_T_PLACE_OBJECT2, 0) + tc.get(_T_PLACE_OBJECT3, 0):
            return "frames are drawn inside sprites/movie clips"
        return ""

    @property
    def duration(self):
        return self.frame_count / self.fps

    # ---------------------------------------------------------------- layers
    def _bitmap(self, bid):
        if bid in self._bmp_cache:
            return self._bmp_cache[bid]
        ent = self._bitmaps.get(bid)
        img = None
        if ent:
            code, s, e = ent
            try:
                if code == _T_JPEG2:
                    img = _decode_jpeg(self.body[s + 2 : e])
                elif code in (_T_JPEG3, _T_JPEG4):
                    off = struct.unpack_from("<I", self.body, s + 2)[0]
                    # JPEG3: id, alphaOffset | JPEG4: id, alphaOffset, deblock(u16)
                    jpos = s + 6 if code == _T_JPEG3 else s + 8
                    img = _decode_jpeg(self.body[jpos : jpos + off])
                    if img.mode != "RGBA":
                        img = img.convert("RGBA")
                    try:
                        alpha = zlib.decompress(self.body[jpos + off : e])
                        if len(alpha) == img.width * img.height:
                            img.putalpha(Image.frombytes("L", img.size, alpha))
                    except zlib.error:
                        pass
                elif code == _T_DEFINE_BITS:
                    tables = self._jpeg_tables or b"\xff\xd8\xff\xd9"
                    data = self.body[s + 2 : e]
                    while data[:4] == b"\xff\xd9\xff\xd8":
                        data = data[4:]
                    if data[:2] == b"\xff\xd8":
                        data = data[2:]
                    tbl = tables[:-2] if tables[-2:] == b"\xff\xd9" else tables
                    img = _decode_jpeg(tbl + data)
                else:
                    img = _decode_lossless(self.body, s, e, code == _T_LOSSLESS2)
            except Exception as exc:
                img = None
                self.problems["bitmap_decode"] = f"{type(exc).__name__}: {exc}"
        if len(self._bmp_cache) > 48:
            self._bmp_cache.pop(next(iter(self._bmp_cache)))
        self._bmp_cache[bid] = img
        return img

    def _build_layer(self, char_id):
        ent = self._shapes.get(char_id)
        if not ent:
            return None
        code, s, e, ver = ent
        try:
            _, parts = _parse_shape(self.body, s, e, ver)
        except Exception as exc:
            self.problems["shape_parse"] = f"{type(exc).__name__}: {exc}"
            return None
        drawables = [(st, polys) for st, polys in parts if st[0] in ("bitmap", "solid") and polys]
        if not drawables:
            return None
        xs = [p[0] for _, polys in drawables for poly in polys for p in poly]
        ys = [p[1] for _, polys in drawables for poly in polys for p in poly]
        ox, oy = floor(min(xs) / TWIPS), floor(min(ys) / TWIPS)
        w = max(1, int(ceil(max(xs) / TWIPS)) - ox)
        h = max(1, int(ceil(max(ys) / TWIPS)) - oy)
        if w * h > 16_000_000:
            return None
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        drew_bitmap = False
        for (kind, val), polys in drawables:
            mask = Image.new("L", (w, h), 0)
            draw = ImageDraw.Draw(mask)
            for poly in polys:
                if len(poly) >= 3:
                    draw.polygon(
                        [(px / TWIPS - ox - 0.5, py / TWIPS - oy - 0.5) for px, py in poly],
                        fill=255,
                    )
            if kind == "solid":
                layer.paste(Image.new("RGBA", (w, h), val), (0, 0), mask)
                continue
            bid, (a, b, c, d, tx, ty) = val
            bmp = self._bitmap(bid)
            if bmp is None:
                continue
            fill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            if abs(a - TWIPS) < 0.02 and abs(d - TWIPS) < 0.02 and abs(b) < 0.02 and abs(c) < 0.02:
                fill.paste(bmp.convert("RGBA"), (round(tx / TWIPS) - ox, round(ty / TWIPS) - oy))
            else:
                det = a * d - b * c
                if abs(det) < 1e-9:
                    continue
                i00, i01, i10, i11 = d / det, -c / det, -b / det, a / det
                coeffs = (
                    i00 * TWIPS, i01 * TWIPS, i00 * (TWIPS * ox - tx) + i01 * (TWIPS * oy - ty),
                    i10 * TWIPS, i11 * TWIPS, i10 * (TWIPS * ox - tx) + i11 * (TWIPS * oy - ty),
                )
                fill = bmp.convert("RGBA").transform(
                    (w, h), Image.AFFINE, coeffs, resample=Image.BILINEAR
                )
            # bitmap alpha x polygon mask
            fa = fill.getchannel("A")
            layer.paste(fill, (0, 0), ImageChops.multiply(fa, mask))
            drew_bitmap = True
        if drew_bitmap:
            self.layers_drawn += 1
        return (layer.convert("RGB"), layer.getchannel("A"), ox, oy)

    def _layer(self, char_id):
        if char_id not in self._layers:
            self._layers[char_id] = self._build_layer(char_id)
        return self._layers[char_id]

    # ----------------------------------------------------------------- frames
    def _compose(self):
        canvas = Image.new("RGB", (self.width, self.height), self._bg)
        for depth in sorted(self._display):
            char_id, m = self._display[depth]
            layer = self._layer(char_id)
            if layer is None:
                continue
            rgb, mask, ox, oy = layer
            if m is None:
                x, y = ox, oy
            else:
                a, _, _, d, tx, ty = m
                if abs(a - 1) > 1e-3 or abs(d - 1) > 1e-3:
                    nw, nh = max(1, round(rgb.width * a)), max(1, round(rgb.height * d))
                    rgb, mask = rgb.resize((nw, nh), Image.BILINEAR), mask.resize((nw, nh))
                    x, y = round(ox * a + tx / TWIPS), round(oy * d + ty / TWIPS)
                else:
                    x, y = ox + round(tx / TWIPS), oy + round(ty / TWIPS)
            canvas.paste(rgb, (round(x - self._xmin), round(y - self._ymin)), mask)
        return canvas.tobytes()

    def _forget(self, char_id):
        if not any(c == char_id for c, _ in self._display.values()):
            self._layers.pop(char_id, None)

    def frames(self):
        """Yield one raw rgb24 frame (bytes) per ShowFrame of the main timeline."""
        self._bitmaps, self._shapes = {}, {}
        self._bmp_cache, self._layers, self._display = {}, {}, {}
        self._jpeg_tables = None
        self._bg = (0, 0, 0)
        self.layers_drawn = 0
        dirty, last = True, None
        for code, s, e in _tags(self.body, self._first_tag):
            if code == _T_SHOW_FRAME:
                if dirty or last is None:
                    last = self._compose()
                    dirty = False
                yield last
            elif code in _BITMAP_TAGS:
                bid = struct.unpack_from("<H", self.body, s)[0]
                if bid in self._bitmaps:  # id re-used: cached layers may be stale
                    self._layers.clear()
                self._bitmaps[bid] = (code, s, e)
                self._bmp_cache.pop(bid, None)
            elif code == _T_JPEG_TABLES:
                self._jpeg_tables = bytes(self.body[s:e])
            elif code in _SHAPE_TAGS:
                sid = struct.unpack_from("<H", self.body, s)[0]
                self._shapes[sid] = (code, s, e, _SHAPE_TAGS[code])
                self._layers.pop(sid, None)
            elif code == _T_BACKGROUND:
                self._bg = tuple(self.body[s : s + 3])
                dirty = True
            elif code in (_T_PLACE_OBJECT, _T_PLACE_OBJECT2, _T_PLACE_OBJECT3):
                self._place(code, s, e)
                dirty = True
            elif code == _T_REMOVE_OBJECT2:
                depth = struct.unpack_from("<H", self.body, s)[0]
                old = self._display.pop(depth, None)
                if old:
                    self._forget(old[0])
                dirty = True
            elif code == _T_REMOVE_OBJECT:
                depth = struct.unpack_from("<H", self.body, s + 2)[0]
                old = self._display.pop(depth, None)
                if old:
                    self._forget(old[0])
                dirty = True

    def _place(self, code, s, e):
        r = _Bits(self.body, s)
        if code == _T_PLACE_OBJECT:
            char_id, depth = r.u16(), r.u16()
            m = _matrix(r)
            self._display[depth] = (char_id, m)
            return
        flags = r.u8()
        if code == _T_PLACE_OBJECT3:
            flags2 = r.u8()
            depth = r.u16()
            if flags2 & 0x08 or (flags2 & 0x10 and flags & 2):  # class name / image class
                while r.u8():
                    pass
        else:
            depth = r.u16()
        char_id = r.u16() if flags & 2 else None
        m = _matrix(r) if flags & 4 else None
        if flags & 8:
            _cxform(r)
        has_clip = bool(flags & 0x40)
        old = self._display.get(depth)
        if flags & 1 and old and char_id is None:  # move only
            self._display[depth] = (old[0], m if m is not None else old[1])
            return
        if char_id is None:
            return
        if has_clip:  # clipping layers are not supported; don't draw them
            self._display.pop(depth, None)
            return
        if old and m is None and flags & 1:
            m = old[1]
        self._display[depth] = (char_id, m)
        if old and old[0] != char_id:
            self._forget(old[0])


def diagnose(path):
    """Print what a SWF contains and whether it can be rendered (debug helper)."""
    import traceback

    try:
        comp = SWFComposer(path)
    except Exception:
        print("PARSE FAILED:")
        traceback.print_exc()
        return
    if comp.warning:
        print("WARNING:", comp.warning)
    print(f"version={comp.version} size={comp.width}x{comp.height} fps={comp.fps:.2f} "
          f"frames={comp.frame_count} duration={comp.duration:.1f}s bitmaps={comp.has_bitmaps}")
    names = {1: "ShowFrame", 2: "Shape", 6: "Bits", 8: "JPEGTables", 9: "BG", 20: "Lossless",
             21: "JPEG2", 22: "Shape2", 26: "Place2", 28: "Remove2", 32: "Shape3", 35: "JPEG3",
             36: "Lossless2", 39: "Sprite", 70: "Place3", 83: "Shape4", 90: "JPEG4", 60: "VideoStream",
             61: "VideoFrame", 18: "SoundStreamHead", 19: "SoundStreamBlock", 12: "DoAction",
             82: "DoABC", 69: "FileAttributes", 76: "SymbolClass"}
    print("tag counts:", {names.get(k, k): v for k, v in sorted(comp.tag_counts.items())})
    first = []
    for code, s, e in _tags(comp.body, comp._first_tag):
        first.append(f"{names.get(code, code)}({e - s})")
        if len(first) >= 40:
            break
    print("first tags:", " ".join(first))
    n = 0
    try:
        for n, fr in enumerate(comp.frames(), 1):
            if n in (30, 300):
                Image.frombytes("RGB", (comp.width, comp.height), fr).save(f"swf_frame_{n}.png")
                print(f"saved swf_frame_{n}.png")
            if n >= 300:
                break
    except Exception:
        traceback.print_exc()
    print(f"rendered {n} frames, layers_drawn={comp.layers_drawn}, problems={comp.problems}")
    print("unsupported reason:", comp.why_unsupported() or "-")


if __name__ == "__main__":
    import sys

    diagnose(sys.argv[1])
