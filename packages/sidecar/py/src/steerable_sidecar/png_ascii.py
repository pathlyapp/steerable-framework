"""Stdlib PNG → grayscale ASCII so text-only models can read board images."""

from __future__ import annotations

import struct
import zlib

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ASCII_RAMP = " .:-=+*#%@"
_MAX_RASTER_EDGE = 4096


def raster_header_size(raw: bytes) -> tuple[int, int] | None:
    """Width×height from a PNG IHDR or BMP DIB header. No pixel decode."""
    if raw.startswith(_PNG_MAGIC) and len(raw) >= 24 and raw[12:16] == b"IHDR":
        width, height = struct.unpack(">II", raw[16:24])
        if 0 < width <= _MAX_RASTER_EDGE and 0 < height <= _MAX_RASTER_EDGE:
            return width, height
        return None
    if len(raw) >= 26 and raw[:2] == b"BM":
        width, height_s = struct.unpack_from("<ii", raw, 18)
        height = abs(height_s)
        if 0 < width <= _MAX_RASTER_EDGE and 0 < height <= _MAX_RASTER_EDGE:
            return width, height
    return None


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


def _bmp_gray_rows(raw: bytes) -> tuple[int, int, list[list[int]]] | None:
    """Uncompressed 24/32-bit BMP (Doom ``/tmp/frame.bmp``, QEMU screenshots)."""
    if len(raw) < 54 or raw[:2] != b"BM":
        return None
    pixel_off = struct.unpack_from("<I", raw, 10)[0]
    dib = struct.unpack_from("<I", raw, 14)[0]
    if dib < 40 or pixel_off < 14 or pixel_off >= len(raw):
        return None
    width, height_s = struct.unpack_from("<ii", raw, 18)
    planes, bpp = struct.unpack_from("<HH", raw, 26)
    compression = struct.unpack_from("<I", raw, 30)[0]
    if (
        width <= 0
        or planes != 1
        or compression != 0
        or bpp not in (24, 32)
        or width > 4096
    ):
        return None
    top_down = height_s < 0
    height = abs(height_s)
    if height <= 0 or height > 4096:
        return None
    bpp_bytes = bpp // 8
    stride = ((width * bpp_bytes + 3) // 4) * 4
    if pixel_off + stride * height > len(raw):
        return None
    rows: list[list[int]] = []
    for y in range(height):
        src_y = y if top_down else height - 1 - y
        off = pixel_off + src_y * stride
        row: list[int] = []
        for x in range(width):
            i = off + x * bpp_bytes
            row.append((raw[i + 2] + raw[i + 1] + raw[i]) // 3)
        rows.append(row)
    return width, height, rows


def ascii_png_preview(raw: bytes, *, max_w: int = 80, max_h: int = 80) -> str | None:
    """Bounded ASCII preview for 8-bit PNG, baseline JPEG, or uncompressed BMP."""
    parsed = _png_gray_rows(raw)
    label = "PNG"
    if parsed is None:
        parsed = _bmp_gray_rows(raw)
        label = "BMP"
    if parsed is None:
        from .jpeg_ascii import jpeg_gray_rows

        parsed = jpeg_gray_rows(raw)
        label = "JPEG"
    if parsed is None:
        return None
    width, height, rows = parsed
    scale = max(width / max_w, height / max_h, 1.0)
    nw = max(1, int(width / scale))
    nh = max(1, int(height / scale))
    ramp = _ASCII_RAMP
    lines = [
        f"{label} {width}x{height} ASCII preview ({nw}x{nh}). Darker = denser char."
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
    tiles = _board_tile_grid(width, height, rows)
    if tiles:
        lines.append(tiles)
    return "\n".join(lines)


def _board_tile_grid(
    width: int, height: int, rows: list[list[int]]
) -> str | None:
    """Labeled 8x8 brightness and occupancy grids for square diagrams."""
    if abs(width - height) > max(8, min(width, height) // 16):
        return None
    side = min(width, height)
    if side < 64:
        return None
    tile = side // 8
    if tile < 8:
        return None
    stats: list[list[tuple[int, int]]] = []
    for rank in range(8):
        y0 = rank * tile
        y1 = side if rank == 7 else (rank + 1) * tile
        row_stats: list[tuple[int, int]] = []
        for file in range(8):
            x0 = file * tile
            x1 = side if file == 7 else (file + 1) * tile
            total = n = 0
            for y in range(y0, y1):
                row = rows[y]
                for x in range(x0, x1):
                    total += row[x]
                    n += 1
            mean = (total // n) if n else 0
            mad_sum = 0
            for y in range(y0, y1):
                row = rows[y]
                for x in range(x0, x1):
                    mad_sum += abs(row[x] - mean)
            row_stats.append((mean, (mad_sum // n) if n else 0))
        stats.append(row_stats)
    mads = sorted(mad for row in stats for _mean, mad in row)
    occupied_cut = max(16, mads[len(mads) // 2] + 12)
    lines = [
        "8x8 mean-brightness (0 dark .. 9 light). Rank 8 at top, file a at left."
    ]
    for rank, row in enumerate(stats):
        cells = [str(min(9, mean * 10 // 256)) for mean, _mad in row]
        lines.append(f"{8 - rank} | {' '.join(cells)}")
    lines.append("    a b c d e f g h")
    lines.append(
        "occupancy (#=internal contrast / piece-like, .=flat square)."
    )
    occupied: list[str] = []
    for rank, row in enumerate(stats):
        cells = ["#" if mad >= occupied_cut else "." for _mean, mad in row]
        lines.append(f"{8 - rank} | {' '.join(cells)}")
        for file, (_mean, mad) in enumerate(row):
            if mad >= occupied_cut:
                occupied.append(f"{chr(ord('a') + file)}{8 - rank}")
    lines.append("    a b c d e f g h")
    if occupied:
        lines.append("occupied squares: " + " ".join(occupied))
    return "\n".join(lines)
