"""Baseline sequential JPEG (SOF0) → grayscale rows for ASCII preview."""

from __future__ import annotations

import math
import struct

_ZIGZAG = (
    0, 1, 5, 6, 14, 15, 27, 28,
    2, 4, 7, 13, 16, 26, 29, 42,
    3, 8, 12, 17, 25, 30, 41, 43,
    9, 11, 18, 24, 31, 40, 44, 53,
    10, 19, 23, 32, 39, 45, 52, 54,
    20, 22, 33, 38, 46, 51, 55, 60,
    21, 34, 37, 47, 50, 56, 59, 61,
    35, 36, 48, 49, 57, 58, 62, 63,
)
_COS = tuple(
    math.cos((2 * x + 1) * u * math.pi / 16) for u in range(8) for x in range(8)
)


def jpeg_gray_rows(raw: bytes) -> tuple[int, int, list[list[int]]] | None:
    """Decode baseline SOF0 JPEG into 0–255 grayscale rows, or None."""
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        return None
    try:
        return _JpegDecoder(raw).decode()
    except (ValueError, struct.error, IndexError, ZeroDivisionError):
        return None


class _Huff:
    __slots__ = ("min_code", "max_code", "val_ptr", "symbols")

    def __init__(self, counts: bytes, symbols: bytes) -> None:
        self.symbols = symbols
        self.min_code = [0] * 17
        self.max_code = [0] * 17
        self.val_ptr = [0] * 17
        code = 0
        si = 0
        for i in range(1, 17):
            self.val_ptr[i] = si
            self.min_code[i] = code
            code += counts[i - 1]
            self.max_code[i] = code - 1 if counts[i - 1] else -1
            code <<= 1
            si += counts[i - 1]


class _BitReader:
    __slots__ = ("data", "pos", "end", "nbits", "bits")

    def __init__(self, data: bytes, pos: int, end: int) -> None:
        self.data = data
        self.pos = pos
        self.end = end
        self.nbits = 0
        self.bits = 0

    def _fill(self) -> None:
        while self.nbits <= 24 and self.pos < self.end:
            b = self.data[self.pos]
            self.pos += 1
            if b == 0xFF:
                while self.pos < self.end and self.data[self.pos] == 0xFF:
                    self.pos += 1
                if self.pos >= self.end:
                    break
                marker = self.data[self.pos]
                self.pos += 1
                if marker == 0x00:
                    b = 0xFF
                elif 0xD0 <= marker <= 0xD7:
                    continue
                else:
                    self.pos -= 2
                    break
            self.bits = (self.bits << 8) | b
            self.nbits += 8

    def receive(self, n: int) -> int:
        if n <= 0:
            return 0
        self._fill()
        if self.nbits < n:
            raise ValueError("truncated entropy")
        self.nbits -= n
        return (self.bits >> self.nbits) & ((1 << n) - 1)

    def decode(self, table: _Huff) -> int:
        self._fill()
        code = 0
        for i in range(1, 17):
            code = (code << 1) | self.receive(1)
            if table.min_code[i] <= code <= table.max_code[i]:
                return table.symbols[table.val_ptr[i] + code - table.min_code[i]]
        raise ValueError("bad huffman")


class _JpegDecoder:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.pos = 2
        self.width = 0
        self.height = 0
        self.qt: list[list[int] | None] = [None] * 4
        self.dc_huff: list[_Huff | None] = [None] * 4
        self.ac_huff: list[_Huff | None] = [None] * 4
        self.comps: list[tuple[int, int, int, int]] = []

    def decode(self) -> tuple[int, int, list[list[int]]] | None:
        sos_start = None
        while self.pos + 4 <= len(self.raw):
            if self.raw[self.pos] != 0xFF:
                self.pos += 1
                continue
            while self.pos < len(self.raw) and self.raw[self.pos] == 0xFF:
                self.pos += 1
            if self.pos >= len(self.raw):
                break
            marker = self.raw[self.pos]
            self.pos += 1
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            if self.pos + 2 > len(self.raw):
                break
            length = struct.unpack(">H", self.raw[self.pos : self.pos + 2])[0]
            data = self.raw[self.pos + 2 : self.pos + length]
            next_pos = self.pos + length
            if marker == 0xC0:
                self._sof0(data)
            elif marker == 0xDB:
                self._dqt(data)
            elif marker == 0xC4:
                self._dht(data)
            elif marker == 0xDA:
                sos_start = next_pos
                self._sos(data)
                break
            elif marker in (0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                return None
            self.pos = next_pos
        if sos_start is None or not self.width or not self.height or not self.comps:
            return None
        if self.width > 4096 or self.height > 4096:
            return None
        return self._scan(sos_start)

    def _sof0(self, data: bytes) -> None:
        if len(data) < 9 or data[0] != 8:
            raise ValueError("sof")
        self.height, self.width = struct.unpack(">HH", data[1:5])
        n = data[5]
        off = 6
        comps: list[tuple[int, int, int, int]] = []
        for _ in range(n):
            cid, samp, tq = data[off], data[off + 1], data[off + 2]
            comps.append((cid, samp >> 4, samp & 0x0F, tq))
            off += 3
        self.comps = comps

    def _dqt(self, data: bytes) -> None:
        off = 0
        while off + 65 <= len(data):
            info = data[off]
            off += 1
            if info >> 4:
                raise ValueError("q16")
            table = [0] * 64
            for nat in range(64):
                table[nat] = data[off + _ZIGZAG[nat]]
            self.qt[info & 0x0F] = table
            off += 64

    def _dht(self, data: bytes) -> None:
        off = 0
        while off + 17 <= len(data):
            info = data[off]
            counts = data[off + 1 : off + 17]
            nsym = sum(counts)
            off += 17
            symbols = data[off : off + nsym]
            off += nsym
            table = _Huff(counts, symbols)
            idx = info & 0x0F
            if info >> 4:
                self.ac_huff[idx] = table
            else:
                self.dc_huff[idx] = table

    def _sos(self, data: bytes) -> None:
        n = data[0]
        mapping = {cid: i for i, (cid, *_rest) in enumerate(self.comps)}
        off = 1
        rebuilt: list[tuple[int, int, int, int, int, int]] = []
        for _ in range(n):
            cid, tdta = data[off], data[off + 1]
            off += 2
            i = mapping[cid]
            h, v, tq = self.comps[i][1], self.comps[i][2], self.comps[i][3]
            rebuilt.append((cid, h, v, tq, tdta >> 4, tdta & 0x0F))
        self.comps_scan = rebuilt

    def _scan(self, start: int) -> tuple[int, int, list[list[int]]]:
        end = self.raw.rfind(b"\xff\xd9")
        if end < start:
            end = len(self.raw)
        bits = _BitReader(self.raw, start, end)
        max_h = max(c[1] for c in self.comps_scan)
        max_v = max(c[2] for c in self.comps_scan)
        mcu_w = 8 * max_h
        mcu_h = 8 * max_v
        nx = (self.width + mcu_w - 1) // mcu_w
        ny = (self.height + mcu_h - 1) // mcu_h
        planes = [
            [[0] * ((nx * mcu_w * c[1]) // max_h) for _ in range((ny * mcu_h * c[2]) // max_v)]
            for c in self.comps_scan
        ]
        pred = [0] * len(self.comps_scan)
        for my in range(ny):
            for mx in range(nx):
                for ci, (_cid, h, v, tq, td, ta) in enumerate(self.comps_scan):
                    qt = self.qt[tq]
                    dc_t = self.dc_huff[td]
                    ac_t = self.ac_huff[ta]
                    if qt is None or dc_t is None or ac_t is None:
                        raise ValueError("tables")
                    for vy in range(v):
                        for hx in range(h):
                            block = self._block(bits, dc_t, ac_t, qt, pred, ci)
                            gray = _idct8(block)
                            bx = mx * h + hx
                            by = my * v + vy
                            plane = planes[ci]
                            for y in range(8):
                                row = plane[by * 8 + y]
                                src = gray[y * 8 : y * 8 + 8]
                                x0 = bx * 8
                                row[x0 : x0 + 8] = src
        y_plane = planes[0]
        if len(planes) == 1:
            rows = [
                [max(0, min(255, y_plane[y][x])) for x in range(self.width)]
                for y in range(self.height)
            ]
            return self.width, self.height, rows
        # Upsample chroma to Y size, then BT.601-ish gray from Y (skip chroma).
        rows = [
            [max(0, min(255, y_plane[y][x])) for x in range(self.width)]
            for y in range(self.height)
        ]
        return self.width, self.height, rows

    def _block(
        self,
        bits: _BitReader,
        dc_t: _Huff,
        ac_t: _Huff,
        qt: list[int],
        pred: list[int],
        ci: int,
    ) -> list[int]:
        cat = bits.decode(dc_t)
        diff = _extend(bits.receive(cat), cat) if cat else 0
        pred[ci] += diff
        zz = [0] * 64
        zz[0] = pred[ci]
        k = 1
        while k < 64:
            rs = bits.decode(ac_t)
            if rs == 0:
                break
            k += rs >> 4
            acat = rs & 0x0F
            if acat:
                if k >= 64:
                    break
                zz[k] = _extend(bits.receive(acat), acat)
                k += 1
            else:
                k += 1
        nat = [0] * 64
        for nat_i in range(64):
            nat[nat_i] = zz[_ZIGZAG[nat_i]] * qt[nat_i]
        return nat


def _extend(v: int, t: int) -> int:
    if t == 0:
        return 0
    vt = 1 << (t - 1)
    return v if v >= vt else v - (1 << t) + 1


def _idct8(block: list[int]) -> list[int]:
    out = [0] * 64
    for y in range(8):
        for x in range(8):
            s = 0.0
            for u in range(8):
                cu = 0.7071067811865476 if u == 0 else 1.0
                for v in range(8):
                    cv = 0.7071067811865476 if v == 0 else 1.0
                    s += (
                        cu
                        * cv
                        * block[v * 8 + u]
                        * _COS[u * 8 + x]
                        * _COS[v * 8 + y]
                    )
            out[y * 8 + x] = int(s / 4 + 128)
    return out
