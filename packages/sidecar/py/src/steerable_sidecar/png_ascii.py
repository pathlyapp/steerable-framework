"""Stdlib PNG → grayscale ASCII so text-only models can read board images."""

from __future__ import annotations

import struct
import zlib

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ASCII_RAMP = " .:-=+*#%@"


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_gray_rows(raw: bytes) -> tuple[int, int, list[list[int]]] | None:
    if not raw.startswith(_PNG_MAGIC):
        return None
    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    n = len(raw)
    while pos + 12 <= n:
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        data_end = pos + 8 + length
        if data_end + 4 > n:
            return None
        data = raw[pos + 8 : data_end]
        pos = data_end + 4
        if ctype == b"IHDR":
            if len(data) < 13:
                return None
            width, height, bit_depth, color_type = struct.unpack(">IIBB", data[:10])
        elif ctype == b"IDAT":
            idat.extend(data)
        elif ctype == b"IEND":
            break
    if (
        not width
        or not height
        or bit_depth != 8
        or color_type not in (0, 2, 4, 6)
    ):
        return None
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    try:
        stream = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    stride = width * bpp
    if len(stream) < height * (1 + stride):
        return None
    prev = bytes(stride)
    rows: list[list[int]] = []
    off = 0
    for _ in range(height):
        filt = stream[off]
        scan = bytearray(stream[off + 1 : off + 1 + stride])
        off += 1 + stride
        if filt == 1:
            for i in range(stride):
                left = scan[i - bpp] if i >= bpp else 0
                scan[i] = (scan[i] + left) & 255
        elif filt == 2:
            for i in range(stride):
                scan[i] = (scan[i] + prev[i]) & 255
        elif filt == 3:
            for i in range(stride):
                left = scan[i - bpp] if i >= bpp else 0
                scan[i] = (scan[i] + ((left + prev[i]) // 2)) & 255
        elif filt == 4:
            for i in range(stride):
                left = scan[i - bpp] if i >= bpp else 0
                up = prev[i]
                ul = prev[i - bpp] if i >= bpp else 0
                scan[i] = (scan[i] + _paeth(left, up, ul)) & 255
        elif filt != 0:
            return None
        prev = bytes(scan)
        gray: list[int] = []
        for x in range(width):
            i = x * bpp
            if color_type == 0:
                gray.append(scan[i])
            else:
                gray.append((scan[i] + scan[i + 1] + scan[i + 2]) // 3)
        rows.append(gray)
    return width, height, rows


def ascii_png_preview(raw: bytes, *, max_w: int = 80, max_h: int = 80) -> str | None:
    """Return a bounded ASCII preview, or None when the bytes are not an 8-bit PNG."""
    parsed = _png_gray_rows(raw)
    if parsed is None:
        return None
    width, height, rows = parsed
    scale = max(width / max_w, height / max_h, 1.0)
    nw = max(1, int(width / scale))
    nh = max(1, int(height / scale))
    ramp = _ASCII_RAMP
    lines = [
        f"PNG {width}x{height} ASCII preview ({nw}x{nh}). Darker = denser char."
    ]
    for y in range(nh):
        src_y = min(height - 1, int(y * scale))
        row = rows[src_y]
        chars: list[str] = []
        for x in range(nw):
            src_x = min(width - 1, int(x * scale))
            v = row[src_x]
            chars.append(ramp[min(len(ramp) - 1, v * len(ramp) // 256)])
        lines.append("".join(chars))
    return "\n".join(lines)
