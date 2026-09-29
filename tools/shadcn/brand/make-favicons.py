#!/usr/bin/env python3
"""favicon-16.png and favicon-32.png from avatar-96.png (M15-FR-109; AWR-15 §4.1: the favicon is the avatar mark).

Standard library only (zlib, struct): decode the 8-bit RGBA PNG, box-filter with premultiplied alpha by the exact integer
factors 3 (96 -> 32) and 6 (96 -> 16), encode a new RGBA PNG. Deterministic, so brand.lock.json stays reproducible.
Usage: python3 tools/shadcn/brand/make-favicons.py
"""

import struct
import zlib
from pathlib import Path

BRAND = Path(__file__).resolve().parents[3] / "apps/web/public/brand"


def read_png(path: Path):
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG")
    i, idat, w, h = 8, b"", 0, 0
    while i < len(data):
        (length,) = struct.unpack(">I", data[i : i + 4])
        kind = data[i + 4 : i + 8]
        body = data[i + 8 : i + 8 + length]
        if kind == b"IHDR":
            w, h, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
            if (depth, ctype, interlace) != (8, 6, 0):
                raise ValueError("only 8-bit non-interlaced RGBA is supported")
        elif kind == b"IDAT":
            idat += body
        i += 12 + length
    raw = zlib.decompress(idat)
    stride = w * 4
    rows, prev, pos = [], bytearray(stride), 0
    for _y in range(h):
        ftype = raw[pos]
        line = bytearray(raw[pos + 1 : pos + 1 + stride])
        pos += 1 + stride
        for x in range(stride):
            a = line[x - 4] if x >= 4 else 0
            b = prev[x]
            c = prev[x - 4] if x >= 4 else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 255
            elif ftype == 2:
                line[x] = (line[x] + b) & 255
            elif ftype == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 255
        rows.append(line)
        prev = line
    return w, h, rows


def downscale(w, h, rows, factor):
    ow, oh = w // factor, h // factor
    out = []
    n = factor * factor
    for oy in range(oh):
        line = bytearray(ow * 4)
        for ox in range(ow):
            r = g = b = a = 0
            for dy in range(factor):
                src = rows[oy * factor + dy]
                for dx in range(factor):
                    k = (ox * factor + dx) * 4
                    alpha = src[k + 3]
                    r += src[k] * alpha
                    g += src[k + 1] * alpha
                    b += src[k + 2] * alpha
                    a += alpha
            k = ox * 4
            if a:
                line[k], line[k + 1], line[k + 2] = (round(r / a), round(g / a), round(b / a))
            line[k + 3] = round(a / n)
        out.append(line)
    return ow, oh, out


def write_png(path: Path, w, h, rows):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def main():
    w, h, rows = read_png(BRAND / "avatar-96.png")
    for size in (32, 16):
        ow, oh, out = downscale(w, h, rows, w // size)
        write_png(BRAND / f"favicon-{size}.png", ow, oh, out)
        print(f"make-favicons: favicon-{size}.png {ow}x{oh}")


if __name__ == "__main__":
    main()
