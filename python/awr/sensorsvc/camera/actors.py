"""演员代理几何与可见度归约（M25 §6.4.4、§6.6.1；M23 §6.6.3）。

P0a 代理资产（`<class>-proxy@1`）由程序生成：人为腿、躯干、头三段八棱柱（下装、上装、肤色），车为车身、座舱与四轮，狗为躯干
与头，机器与物体为盒体。局部帧原点在脚底中心，x 为朝向、z 向上；尺寸取磁带头 `size_mm`（l、w、h）。颜色为线性 RGB 反照率，
渲染时按 `c = albedo·(k_amb + k_dif·max(0, n·L_sun))` 着色（k_amb 0.55、k_dif 0.45）。

可见度归约在交付分辨率的整数数组上进行（与线程调度无关）：`instance` 为可见实例（静态 0、演员 idx + 1），`instance_unocc`
为只渲染演员的实例，`semantic` 为前景语义；`invalid` 非 0 的像素不计入。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["K_AMB", "K_DIF", "SEM_CODE", "SUN_AZ_EL_DEG", "proxy_mesh", "sun_dir", "visibility_objects"]

SEM_CODE = {"person": 32, "vehicle": 33, "dog": 34, "uav": 35, "machine": 36, "object": 37}
K_AMB, K_DIF = 0.55, 0.45
SUN_AZ_EL_DEG = (135.0, 50.0)          # 世界未给出 bakedSunAzElDeg 时的缺省（方位自北顺时针）
SKIN = (205, 168, 140)
DARK = (28, 30, 34)
GLASS = (40, 52, 60)


def _lin(rgb) -> np.ndarray:
    c = np.asarray(rgb, np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def sun_dir(az_el_deg: tuple[float, float] | None) -> np.ndarray:
    """ENU 中指向太阳的单位向量；方位自北顺时针。"""
    az, el = az_el_deg or SUN_AZ_EL_DEG
    a, e = math.radians(az), math.radians(el)
    return np.array([math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), math.sin(e)])


def _prism(cx: float, cy: float, z0: float, z1: float, rx: float, ry: float, col, n: int = 8) -> tuple[list, list, list]:
    """椭圆截面 n 棱柱（侧面、顶面、底面），外法线。"""
    P, N, C = [], [], []
    ang = [2 * math.pi * (k + 0.5) / n for k in range(n)]
    ring = [(cx + rx * math.cos(a), cy + ry * math.sin(a)) for a in ang]
    for k in range(n):
        (x0, y0), (x1, y1) = ring[k], ring[(k + 1) % n]
        nx, ny = y1 - y0, -(x1 - x0)
        ln = math.hypot(nx, ny) or 1.0
        nn = (nx / ln, ny / ln, 0.0)
        quad = [(x0, y0, z0), (x1, y1, z0), (x1, y1, z1), (x0, y0, z1)]
        for t in ((0, 1, 2), (0, 2, 3)):
            P += [quad[i] for i in t]
            N += [nn] * 3
    for z, s in ((z1, 1.0), (z0, -1.0)):
        for k in range(n):
            (x0, y0), (x1, y1) = ring[k], ring[(k + 1) % n]
            tri = [(cx, cy, z), (x0, y0, z), (x1, y1, z)] if s > 0 else [(cx, cy, z), (x1, y1, z), (x0, y0, z)]
            P += tri
            N += [(0.0, 0.0, s)] * 3
    C += [col] * (len(P))
    return P, N, C


def _box(x0, x1, y0, y1, z0, z1, col) -> tuple[list, list, list]:
    return _prism((x0 + x1) / 2, (y0 + y1) / 2, z0, z1, (x1 - x0) / 2 * math.sqrt(2), (y1 - y0) / 2 * math.sqrt(2), col, n=4)


def proxy_mesh(actor: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """演员表一行 → (pos (N, 3), nrm (N, 3), albedo_lin (N, 3)) float32，非索引三角形。"""
    cls = actor["class"]
    ln, w, h = (s / 1000.0 for s in actor["size_mm"])
    ap = actor.get("appearance", {})
    tint = ap.get("tint_rgb")
    parts: list[tuple[list, list, list]] = []
    if cls == "person":
        top = ap.get("top_rgb") or tint or (120, 120, 125)
        bot = ap.get("bottom_rgb") or ([max(0, int(c * 0.45)) for c in tint] if tint else (60, 60, 66))
        legs, torso_top = 0.47 * h, 0.82 * h
        rx, ry = 0.5 * ln * 0.8, 0.5 * w * 0.9
        parts.extend(_prism(0.0, s * ry * 0.45, 0.0, legs, rx * 0.45, ry * 0.42, bot) for s in (-1.0, 1.0))
        parts.append(_prism(0.0, 0.0, legs, torso_top, rx * 0.75, ry, top))
        parts.append(_prism(0.0, 0.0, torso_top, torso_top + 0.03 * h, rx * 0.3, ry * 0.3, SKIN, n=6))
        parts.append(_prism(0.0, 0.0, torso_top + 0.03 * h, h, 0.075 * h * 0.62, 0.075 * h * 0.55, SKIN))
    elif cls == "vehicle":
        body = ap.get("body_rgb") or tint or (190, 190, 195)
        wr = 0.18 * h
        parts.append(_box(-ln / 2, ln / 2, -w / 2, w / 2, wr, 0.62 * h, body))
        parts.append(_box(-0.32 * ln, 0.18 * ln, -0.45 * w, 0.45 * w, 0.62 * h, 0.95 * h, GLASS))
        parts.append(_box(-0.30 * ln, 0.16 * ln, -0.43 * w, 0.43 * w, 0.95 * h, h, body))
        parts.extend(_box(sx - wr, sx + wr, sy - 0.06 * w, sy + 0.06 * w, 0.0, 2 * wr, DARK)
                     for sx in (-0.33 * ln, 0.33 * ln) for sy in (-0.42 * w, 0.42 * w))
    elif cls == "dog":
        fur = ap.get("body_rgb") or tint or (150, 110, 70)
        parts.append(_box(-ln / 2, 0.3 * ln, -w / 2, w / 2, 0.45 * h, 0.85 * h, fur))
        parts.append(_box(0.3 * ln, ln / 2, -0.4 * w, 0.4 * w, 0.6 * h, h, fur))
        parts.extend(_box(sx - 0.05 * ln, sx + 0.05 * ln, sy - 0.12 * w, sy + 0.12 * w, 0.0, 0.45 * h, fur)
                     for sx in (-0.35 * ln, 0.2 * ln) for sy in (-0.3 * w, 0.3 * w))
    else:
        c = ap.get("body_rgb") or tint or ((96, 104, 112) if cls == "machine" else (150, 120, 80))
        parts.append(_box(-ln / 2, ln / 2, -w / 2, w / 2, 0.0, h, c))
    P = np.array([p for pp in parts for p in pp[0]], np.float32)
    N = np.array([n for pp in parts for n in pp[1]], np.float32)
    C = np.array([_lin(c) for pp in parts for c in pp[2]], np.float32)
    return P, N, C


def visibility_objects(inst: np.ndarray, inst_unocc: np.ndarray, sem: np.ndarray, invalid: np.ndarray | None,
                       actors: list[dict]) -> list[dict]:
    """M25 §6.6.1 可见度归约：返回 px_unoccluded > 0 的演员（按 inst 升序）。只遍历任一实例通道非 0 的像素（演员通常只占画面一小部分）。"""
    H, W = inst.shape
    fg = (inst_unocc > 0) | (inst > 0)
    if invalid is not None:
        fg &= invalid == 0
    pix = np.flatnonzero(fg)
    iu = inst_unocc.ravel()[pix].astype(np.int64)
    iv = inst.ravel()[pix].astype(np.int64)
    sm = sem.ravel()[pix].astype(np.int64)
    yy, xx = np.divmod(pix, W)
    out = []
    for i in np.unique(iu[iu > 0]):
        mu = iu == i
        mv = iv == i
        bu = (int(xx[mu].min()), int(yy[mu].min()), int(xx[mu].max()), int(yy[mu].max()))
        if mv.any():
            bv = (int(xx[mv].min()), int(yy[mv].min()), int(xx[mv].max()), int(yy[mv].max()))
            bbox_v = [bv[0], bv[1], bv[2] - bv[0] + 1, bv[3] - bv[1] + 1]
            hv = bv[3] - bv[1] + 1
        else:
            bbox_v, hv = [0, 0, 0, 0], 0
        occ = mu & (iv != i)
        hist = np.bincount(sm[occ], minlength=256)
        a = actors[int(i) - 1]
        o = {"inst": int(i), "actor": a["id"], "class": SEM_CODE.get(a["class"], 13), "px_visible": int(mv.sum()), "px_unoccluded": int(mu.sum()),
             "h_px_unocc": bu[3] - bu[1] + 1, "h_px_visible": hv, "visible_ratio": round(float(mv.sum()) / float(mu.sum()), 6),
             "bbox_xywh": bbox_v, "bbox_unocc_xywh": [bu[0], bu[1], bu[2] - bu[0] + 1, bu[3] - bu[1] + 1],
             "occluder_sem_px": {str(k): int(v) for k, v in enumerate(hist) if v},
             "flags": ["truncated"] if bu[0] == 0 or bu[1] == 0 or bu[2] == W - 1 or bu[3] == H - 1 else []}
        out.append(o)
    return out
