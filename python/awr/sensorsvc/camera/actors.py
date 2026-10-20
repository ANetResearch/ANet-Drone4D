"""演员代理几何与可见度归约（M25 §6.4.4、§6.6.1；M23 §6.6.3）。

P0a 代理资产（`<class>-proxy@1`）由程序生成：人为腿、躯干、头三段八棱柱（下装、上装、肤色），车为车身、座舱与四轮，狗为躯干
与头，机器与物体为盒体。局部帧原点在脚底中心，x 为朝向、z 向上；尺寸取磁带头 `size_mm`（l、w、h）。颜色为线性 RGB 反照率，
渲染时按 `c = albedo·(k_amb + k_dif·max(0, n·L_sun))` 着色（k_amb 0.55、k_dif 0.45）。

可见度归约在交付分辨率的整数数组上进行（与线程调度无关）：`instance` 为可见实例（静态 0、演员 idx + 1），`instance_unocc`
为只渲染演员的实例，`semantic` 为前景语义；`invalid` 非 0 的像素不计入。

几何参照（D3-AC-09 投影与遮挡验收）：`proxy_centroid` 为代理网格的体积质心（各部件为闭合外法线棱柱，按有向四面体体积求和），
`proxy_markers` 为代理主体盒的 8 个顶点（网格顶点，局部帧），`actor_to_world` 按渲染同一约定（绕 z 偏航后平移）换到世界系；
`two_pass_composite` 为两遍合成参照（只静态一遍与只演员一遍按平面深度合成，演员深度同样朝相机偏置 1 cm），`mask_iou` 为掩膜 IoU。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["ACTOR_DEPTH_BIAS_M", "GLSL_VIS_CS", "K_AMB", "K_DIF", "SEM_CODE", "SUN_AZ_EL_DEG", "VIS_EMPTY", "VIS_STRIDE", "actor_to_world",
           "mask_iou", "objects_from_stats", "proxy_centroid", "proxy_markers", "proxy_mesh", "sun_dir", "two_pass_composite", "vis_init",
           "visibility_objects", "visibility_stats"]

SEM_CODE = {"person": 32, "vehicle": 33, "dog": 34, "uav": 35, "machine": 36, "object": 37}
K_AMB, K_DIF = 0.55, 0.45
SUN_AZ_EL_DEG = (135.0, 50.0)          # 世界未给出 bakedSunAzElDeg 时的缺省（方位自北顺时针）
SKIN = (205, 168, 140)
DARK = (28, 30, 34)
GLASS = (40, 52, 60)
ACTOR_DEPTH_BIAS_M = 0.01             # 演员深度测试值朝相机的偏置（raster.ACTOR_VS），平面深度附件不偏置


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


VIS_STRIDE = 266               # 可见度统计每实例的 u32 个数：nU、nV、无遮挡外接框 (x0, y0, x1, y1)、可见外接框、遮挡物语义直方图 256
VIS_EMPTY = 0xFFFFFFFF         # 外接框最小值的初值（该实例无像素）

GLSL_VIS_CS = """
#version 450
layout(local_size_x = 16, local_size_y = 16) in;
layout(std430, binding = 7) buffer Vis { uint vis[]; };
uniform usampler2D u_inst;
uniform usampler2D u_unocc;
uniform usampler2D u_sem;
uniform ivec2 u_size;
uniform uint u_K;
void main() {
    ivec2 p = ivec2(gl_GlobalInvocationID.xy);
    if (p.x >= u_size.x || p.y >= u_size.y) return;
    uint iu = texelFetch(u_unocc, p, 0).r;
    uint iv = texelFetch(u_inst, p, 0).r;
    uint x = uint(p.x), y = uint(p.y);
    if (iu > 0u && iu < u_K) {
        uint b = iu * 266u;
        atomicAdd(vis[b], 1u);
        atomicMin(vis[b + 2u], x); atomicMin(vis[b + 3u], y); atomicMax(vis[b + 4u], x); atomicMax(vis[b + 5u], y);
        if (iv != iu) atomicAdd(vis[b + 10u + texelFetch(u_sem, p, 0).r], 1u);
    }
    if (iv > 0u && iv < u_K) {
        uint b = iv * 266u;
        atomicAdd(vis[b + 1u], 1u);
        atomicMin(vis[b + 6u], x); atomicMin(vis[b + 7u], y); atomicMax(vis[b + 8u], x); atomicMax(vis[b + 9u], y);
    }
}
"""


def vis_init(K: int) -> np.ndarray:
    """可见度统计缓冲的初值 (K, VIS_STRIDE) uint32：计数与直方图 0，外接框最小值 VIS_EMPTY、最大值 0。"""
    a = np.zeros((K, VIS_STRIDE), np.uint32)
    a[:, [2, 3, 6, 7]] = VIS_EMPTY
    return a


def visibility_stats(inst: np.ndarray, inst_unocc: np.ndarray, sem: np.ndarray, invalid: np.ndarray | None, K: int | None = None) -> np.ndarray:
    """逐实例可见度统计 (K, VIS_STRIDE) uint32（与 GPU 归约 `GLSL_VIS_CS` 同一布局与语义）：只计任一实例通道非 0 且未标记无效的像素；
    nU、nV 为无遮挡、可见像素数，外接框为像素列、行的最小与最大值，直方图为无遮挡像素中可见实例不是本实例者的语义码计数。"""
    H, W = inst.shape
    pix = np.flatnonzero(np.bitwise_or(inst_unocc, inst) != 0)            # 任一实例通道非 0（整幅两遍；布尔数组的 nonzero 最快）
    if invalid is not None and len(pix):
        pix = pix[invalid.ravel()[pix] == 0]
    iu = inst_unocc.ravel()[pix].astype(np.int64)
    iv = inst.ravel()[pix].astype(np.int64)
    if K is None:
        K = int(max(iu.max(initial=0), iv.max(initial=0))) + 1
    st = vis_init(K)
    if not len(pix):
        return st
    sm = sem.ravel()[pix].astype(np.int64)
    yy, xx = np.divmod(pix, W)
    st[:, 0] = np.bincount(iu, minlength=K)[:K]
    st[:, 1] = np.bincount(iv, minlength=K)[:K]

    def spans(ids: np.ndarray, pos: np.ndarray, n: int, col: int) -> None:
        occ = np.bincount(ids * n + pos, minlength=K * n)[:K * n].reshape(K, n) > 0
        any_ = occ.any(1)
        st[any_, col] = occ[any_].argmax(1)
        st[any_, col + 2] = n - 1 - occ[any_][:, ::-1].argmax(1)

    spans(iu, xx, W, 2)
    spans(iu, yy, H, 3)
    spans(iv, xx, W, 6)
    spans(iv, yy, H, 7)
    occm = (iu > 0) & (iv != iu)
    st[:, 10:] = np.bincount(iu[occm] * 256 + sm[occm], minlength=K * 256)[:K * 256].reshape(K, 256)
    st[0] = vis_init(1)[0]                                                  # 实例 0（背景）不统计，与 GPU 归约一致
    return st


def objects_from_stats(st: np.ndarray, actors: list[dict], width: int, height: int) -> list[dict]:
    """可见度统计 → `objects[]`（px_unoccluded > 0 的演员，按 inst 升序；字段见 M25 §6.6.1）。"""
    W, H = int(width), int(height)
    st = np.asarray(st)
    n_u = st[:, 0].astype(np.int64)
    out = []
    for i in np.flatnonzero(n_u[1:]) + 1:
        i = int(i)
        r = st[i]
        nu, nv = int(r[0]), int(r[1])
        bu = (int(r[2]), int(r[3]), int(r[4]), int(r[5]))
        if nv:
            bv = (int(r[6]), int(r[7]), int(r[8]), int(r[9]))
            bbox_v = [bv[0], bv[1], bv[2] - bv[0] + 1, bv[3] - bv[1] + 1]
            hv = bv[3] - bv[1] + 1
        else:
            bbox_v, hv = [0, 0, 0, 0], 0
        a = actors[i - 1]
        h = r[10:]
        out.append({"inst": i, "actor": a["id"], "class": SEM_CODE.get(a["class"], 13), "px_visible": nv, "px_unoccluded": nu,
                    "h_px_unocc": bu[3] - bu[1] + 1, "h_px_visible": hv, "visible_ratio": round(float(nv) / float(nu), 6),
                    "bbox_xywh": bbox_v, "bbox_unocc_xywh": [bu[0], bu[1], bu[2] - bu[0] + 1, bu[3] - bu[1] + 1],
                    "occluder_sem_px": {str(int(k)): int(h[k]) for k in np.flatnonzero(h)},
                    "flags": ["truncated"] if bu[0] == 0 or bu[1] == 0 or bu[2] == W - 1 or bu[3] == H - 1 else []})
    return out


def visibility_objects(inst: np.ndarray, inst_unocc: np.ndarray, sem: np.ndarray, invalid: np.ndarray | None,
                       actors: list[dict], stats: np.ndarray | None = None) -> list[dict]:
    """M25 §6.6.1 可见度归约：返回 px_unoccluded > 0 的演员（按 inst 升序）。逐实例的像素数、外接框与遮挡物语义直方图由
    `visibility_stats` 以 `bincount` 一次求出（实例 × 行、实例 × 列的出现表取首末位置），不逐实例扫描像素。`stats` 给出时
    （光栅相机的 GPU 归约，`RenderOut.vis`）直接使用，调用方须保证 `invalid` 不含任何实例像素（只有无几何下视位时成立）。"""
    if stats is None:
        stats = visibility_stats(inst, inst_unocc, sem, invalid)
    return objects_from_stats(stats, actors, inst.shape[1], inst.shape[0])


def proxy_centroid(actor: dict) -> np.ndarray:
    """代理网格体积质心（局部帧，float64）：Σ Vᵢ·cᵢ / Σ Vᵢ，Vᵢ 为原点与三角形构成的有向四面体体积，cᵢ 为其质心。"""
    P = proxy_mesh(actor)[0].astype(np.float64).reshape(-1, 3, 3)
    a, b, c = P[:, 0], P[:, 1], P[:, 2]
    v = np.einsum("ij,ij->i", a, np.cross(b, c)) / 6.0
    if not v.sum() > 0:
        raise ValueError(f"{actor.get('id')}: proxy mesh is not closed with outward winding")
    return (v[:, None] * (a + b + c) / 4.0).sum(0) / v.sum()


def proxy_markers(actor: dict) -> np.ndarray:
    """代理主体盒的 8 个顶点（局部帧，(8, 3) float64，按 z、y、x 由小到大）：机器与物体为整个盒体，车为车身，狗为躯干；人无盒体部件，
    返回包含全部顶点的轴对齐盒（不全是网格顶点，投影验收只对盒体类别使用）。"""
    cls = actor["class"]
    ln, w, h = (s / 1000.0 for s in actor["size_mm"])
    if cls == "vehicle":
        x0, x1, y0, y1, z0, z1 = -ln / 2, ln / 2, -w / 2, w / 2, 0.18 * h, 0.62 * h
    elif cls == "dog":
        x0, x1, y0, y1, z0, z1 = -ln / 2, 0.3 * ln, -w / 2, w / 2, 0.45 * h, 0.85 * h
    elif cls == "person":
        P = proxy_mesh(actor)[0].astype(np.float64)
        (x0, y0, z0), (x1, y1, z1) = P.min(0), P.max(0)
    else:
        x0, x1, y0, y1, z0, z1 = -ln / 2, ln / 2, -w / 2, w / 2, 0.0, h
    return np.array([[x, y, z] for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)], np.float64)


def actor_to_world(X_local: np.ndarray, pos_enu_m, yaw_rad: float) -> np.ndarray:
    """局部帧点 → 世界 ENU：`R_z(yaw)·X + pos`（与光栅相机的演员模型矩阵相同）。"""
    c, s_ = math.cos(float(yaw_rad)), math.sin(float(yaw_rad))
    Rz = np.array([[c, -s_, 0.0], [s_, c, 0.0], [0.0, 0.0, 1.0]])
    return np.asarray(X_local, np.float64) @ Rz.T + np.asarray(pos_enu_m, np.float64)


def two_pass_composite(depth_static: np.ndarray, depth_actors: np.ndarray, inst_actors: np.ndarray,
                       bias_m: float = ACTOR_DEPTH_BIAS_M) -> np.ndarray:
    """两遍合成参照（r3 §5 C1）：只静态场景一遍的平面深度（几何外 0）与只演员一遍的平面深度、实例 → 合成实例图。演员像素在
    静态无几何或 `d_actor − bias < d_static` 时可见（与联合 z-buffer 的 reversed-Z GREATER 比较同向）。"""
    fg = inst_actors > 0
    vis = fg & ((depth_static <= 0) | (depth_actors - bias_m < depth_static))
    return np.where(vis, inst_actors, 0).astype(inst_actors.dtype)


def mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    """布尔掩膜 IoU；两者皆空时为 1.0。"""
    a = np.asarray(a, bool)
    b = np.asarray(b, bool)
    u = int((a | b).sum())
    return 1.0 if u == 0 else int((a & b).sum()) / u
