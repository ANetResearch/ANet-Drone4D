#!/usr/bin/env python3
"""PWA and home-screen icons from the avatar mark (D2.1; AWR-04 §15.8.7 item 1; M15-FR-234; AWR-15 §4.1).

Outputs in apps/web/public/brand (listed in brand.lock.json by brand-lock.py, checked by check-brand BRAND-01):
  pwa-192.png, pwa-512.png        the avatar (avatar-460.png) resampled, transparent background
  pwa-maskable-512.png            the avatar inside the 80 % safe zone on the scene background (--g950 of theme.css)
  apple-touch-icon.png            180 x 180 on the scene background (iOS draws no transparency)
Standard library only (zlib, struct): premultiplied-alpha bilinear resampling for enlargements and box averaging for
reductions, deterministic, so the lock stays reproducible. The background colour is read from the theme primitives, so no
colour value is written here.
Usage: python3 tools/shadcn/brand/make-pwa-icons.py
"""

import importlib.util
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BRAND = ROOT / "apps/web/public/brand"
THEME = ROOT / "apps/web/src/styles/theme.css"

_spec = importlib.util.spec_from_file_location("make_favicons", HERE / "make-favicons.py")
_fav = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fav)
read_png, write_png = _fav.read_png, _fav.write_png


def scene_background():
    """the --g950 primitive (#RRGGBB) of theme.css as an RGB tuple"""
    m = re.search(r"--g950:\s*#([0-9A-Fa-f]{6})", THEME.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("make-pwa-icons: --g950 not found in theme.css")
    v = m.group(1)
    return tuple(int(v[i : i + 2], 16) for i in (0, 2, 4))


def resample(w, h, rows, size):
    """premultiplied RGBA resample to size x size: area average when reducing, bilinear when enlarging"""
    out = []
    scale = w / size
    for oy in range(size):
        line = bytearray(size * 4)
        for ox in range(size):
            if scale >= 1:
                x0, x1 = ox * scale, (ox + 1) * scale
                y0, y1 = oy * scale, (oy + 1) * scale
                r = g = b = a = wsum = 0.0
                for sy in range(int(y0), min(h, int(y1 + 0.999999))):
                    wy = min(sy + 1, y1) - max(sy, y0)
                    if wy <= 0:
                        continue
                    src = rows[sy]
                    for sx in range(int(x0), min(w, int(x1 + 0.999999))):
                        wx = min(sx + 1, x1) - max(sx, x0)
                        if wx <= 0:
                            continue
                        k = sx * 4
                        al = src[k + 3] / 255.0
                        wt = wx * wy
                        r += src[k] * al * wt
                        g += src[k + 1] * al * wt
                        b += src[k + 2] * al * wt
                        a += al * wt
                        wsum += wt
            else:
                fx = (ox + 0.5) * scale - 0.5
                fy = (oy + 0.5) * scale - 0.5
                ix = max(0, min(w - 1, int(fx)))
                iy = max(0, min(h - 1, int(fy)))
                tx, ty = min(max(fx - ix, 0.0), 1.0), min(max(fy - iy, 0.0), 1.0)
                r = g = b = a = wsum = 0.0
                for dy, wy in ((0, 1 - ty), (1, ty)):
                    src = rows[min(h - 1, iy + dy)]
                    for dx, wx in ((0, 1 - tx), (1, tx)):
                        k = min(w - 1, ix + dx) * 4
                        al = src[k + 3] / 255.0
                        wt = wx * wy
                        r += src[k] * al * wt
                        g += src[k + 1] * al * wt
                        b += src[k + 2] * al * wt
                        a += al * wt
                        wsum += wt
            k = ox * 4
            if a > 0:
                line[k], line[k + 1], line[k + 2] = (min(255, round(r / a)), min(255, round(g / a)), min(255, round(b / a)))
            line[k + 3] = min(255, round(255 * a / wsum)) if wsum else 0
        out.append(line)
    return out


def on_background(rows, size, canvas, bg):
    """place a size x size RGBA image in the centre of a canvas x canvas opaque background"""
    off = (canvas - size) // 2
    out = []
    for y in range(canvas):
        line = bytearray(canvas * 4)
        for x in range(canvas):
            k = x * 4
            r, g, b = bg
            sy, sx = y - off, x - off
            if 0 <= sy < size and 0 <= sx < size:
                s = rows[sy]
                j = sx * 4
                al = s[j + 3] / 255.0
                r = round(s[j] * al + r * (1 - al))
                g = round(s[j + 1] * al + g * (1 - al))
                b = round(s[j + 2] * al + b * (1 - al))
            line[k], line[k + 1], line[k + 2], line[k + 3] = r, g, b, 255
        out.append(line)
    return out


def main():
    w, h, rows = read_png(BRAND / "avatar-460.png")
    bg = scene_background()
    for size in (192, 512):
        write_png(BRAND / f"pwa-{size}.png", size, size, resample(w, h, rows, size))
        print(f"make-pwa-icons: pwa-{size}.png")
    inner = round(512 * 0.8)
    write_png(BRAND / "pwa-maskable-512.png", 512, 512, on_background(resample(w, h, rows, inner), inner, 512, bg))
    print("make-pwa-icons: pwa-maskable-512.png")
    write_png(BRAND / "apple-touch-icon.png", 180, 180, on_background(resample(w, h, rows, 160), 160, 180, bg))
    print("make-pwa-icons: apple-touch-icon.png")


if __name__ == "__main__":
    main()
