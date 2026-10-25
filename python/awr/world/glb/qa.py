"""GLB 世界的标定与调平 QA（M22 §6.6.4、§6.7 L1–L4 与 R4、R5，FR-012、FR-013、FR-015、FR-022；D3-AC-02）。

本模块在世界帧（`T_world_source` 作用后的米制 ENU）中独立复测配准记录给出的调平与尺度，统计量只写 `qa/report.json`
（随 ISA 变化的量不进入内容文件，M22 §13 第 4 条），并替换 D2 通用门禁中对 GLB 不成立的两条：

- G-04、G-05：D2 `ingest_arrays` 以 `leveled = False` 调用 `run_gates`，两条门禁恒为通过（G-04 取值为空也通过、G-05 超限也通过）。
  GLB 世界一律视为已调平：G-04 取调平后地面残差 MAD（有 DEM 时为 DEM 回归残差，否则为地面候选面的平面残差），取不到值即失败；
  G-05 取主方法在世界帧中复测的剩余倾角，≥ 0.5° 即失败。
- G-L1（公里级）：DEM 回归与墙面竖直度两法剩余梯度之差折合角度 ≤ 0.5°；超出或缺少任一方法时告警并要求控制点。
- G-L2（小场景）：墙面法按立面方位奇偶分成两个子集，两子集差与自助法 CI95 半宽均 ≤ 0.3°。
- G-L34：≥ 3 处已知水平面（OSM 水体、广场与人工增补多边形，面积 ≥ 2,000 m²）全部坡度 ≤ 0.2°（小场景 ≤ 0.5°），或 ≥ 20 个
  分散控制点的 AGL 残差 RMS ≤ 2 m，或（公里级）L5 公开 DEM 交叉核对成立，任一满足即通过（G-13）。
- L5（用户 2026-10-19 决定以公开 DEM 交叉核对替代控制点判据，W07 起草 ADR）：主 DEM（SRTM 1″）、交叉核对 DEM（配方 `qa.dem`，
  Copernicus GLO-30）与墙面竖直度三法：两两之差 ≤ 0.5°，各法剩余倾角 ≤ 0.5°，三法剩余梯度均值（共识）的倾角 ≤ 0.2°，各 DEM 的
  竖向尺度比 k ∈ [0.85, 1.15]。此时 L3 水平面判据只作告警（G-16）。
- G-R4（公里级）：DEM 竖向尺度比 k ∈ [0.85, 1.15]。
- G-SCALE：以 OSM 建筑轮廓对网格墙面的截断倒角精配独立测得尺度（配准记录的尺度只作初值），与 r2 实测值之差 ≤ 0.5%，
  且 20 次扰动重启的尺度标准差 / 均值 ≤ 0.5%（R5）。

两法梯度的符号约定（r2 §2.4）：模型地面法线为 (gE, gN, 1) 时 `level_matrix(g)` 把它转到 +Z。DEM 回归 `h = k·z + gE·E + gN·N + c`
给出 g = (gE, gN) / k；墙面法由近竖直面 `n_z ≈ c − gE·n_x − gN·n_y` 回归得到 g（c 为立面整体外倾，摄影测量网格的立面普遍
略朝上）。世界帧中复测的是剩余梯度（理想为 0），经 `normalize.source_gradient` 换算回源帧梯度后与配准记录的 `applied_g` 对照。
"""

from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy import optimize
from scipy.spatial import cKDTree

from awr.world.georef.frames import lla_to_world, world_to_lla
from awr.world.georef.types import Anchor
from awr.world.glb.normalize import level_matrix, source_gradient, tilt_deg
from awr.world.ingest.normalize import ground_plane_mad
from awr.world.ingest.types import GateResult
from awr.world.terrain.dtm import dtm_opening

__all__ = [
    "R2_SCALE_M_PER_UNIT",
    "DemFit",
    "GlbQa",
    "GridDem",
    "HgtDem",
    "OsmData",
    "QaInputs",
    "ScaleFit",
    "WallFit",
    "calibrate",
    "control_point_rms",
    "dem_cross_check",
    "dem_fit",
    "face_geometry",
    "glb_gates",
    "gradient_delta_deg",
    "ground_faces",
    "ground_height_fn",
    "huber_irls",
    "levelling_report",
    "load_control_points",
    "load_dem",
    "load_horizontal_planes",
    "measure_scale",
    "parse_osm",
    "plane_slopes",
    "read_geotiff_dem",
    "replace_gates",
    "rings_to_world",
    "run_glb_qa",
    "wall_fit",
]

# r2 实测尺度（m/单位；M22 §8.1"预期尺度"，r2 §2.2 与 §1）：G-SCALE 的对照值，配方 `qa.reference_m_per_unit` 可覆写
R2_SCALE_M_PER_UNIT = {"oryahovo-crop1": 20.935, "smalltown-01": 8.374}

KM_SCALE_DIAG_M = 1000.0            # 公里级判据：包围盒水平对角线 ≥ 1 km（FR-012）
L1_MAX_DEG = 0.5                    # 两法差（FR-012，D3-AC-02）
L2_MAX_DEG = 0.3                    # 小场景子集一致性与 CI95 半宽（§6.7 L2）
L3_MIN_PLANES = 3
L3_MAX_SLOPE_DEG = 0.2              # 公里级；小场景 0.5°（§6.7 L3）
L3_MAX_SLOPE_SMALL_DEG = 0.5
L3_MIN_AREA_M2 = 2000.0
L4_MIN_POINTS = 20
L4_MAX_RMS_M = 2.0
L5_PAIR_MAX_DEG = 0.5               # 三法两两之差（同 L1）
L5_RESIDUAL_MAX_DEG = 0.5           # 各法剩余倾角（同 G-05）
L5_CONSENSUS_MAX_DEG = 0.2          # 三法共识剩余倾角（与 L3 水平面坡度同一门限）
G04_MAX_MAD_M = 5.0
G05_MAX_DEG = 0.5
R4_RANGE = (0.85, 1.15)
SCALE_MAX_REL = 0.005               # 与 r2 差 ≤ 0.5%（D3-AC-02）；R5 不确定度同一门限
WALL_BAND = 0.1                     # 墙面：|n_z| < 0.1（偏离竖直 ≤ 5.7°）；0.35 的宽带混入陡坡屋面，迭代发散（W02 实测）
WALL_ITERS = 3                      # 按当前估计重新调平法线后再选墙面，消除选带截断偏差
GROUND_NZ = 0.9                     # 地面候选：n_z > 0.9 且高出 10 m DTM ≤ 1 m
PLANE_SEGMENT_M = 800.0             # 覆盖内延伸超过该长度的水面多边形（河流）沿主轴分段，每段单独拟合
PLANE_EDGE_M = 15.0                 # 多边形边界内缩：排除岸线、码头与轮廓配准误差带


# ---------------------------------------------------------------- 基础量


def face_geometry(V: np.ndarray, F: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """三角形质心、单位法线（按顶点顺序右手定则）与面积。"""
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    n = np.cross(b - a, c - a)
    two_a = np.linalg.norm(n, axis=1)
    n = n / np.maximum(two_a, 1e-300)[:, None]
    return (a + b + c) / 3.0, n, 0.5 * two_a


def huber_irls(X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None, *, iters: int = 20, c: float = 1.5,
               beta0: np.ndarray | None = None) -> np.ndarray:
    """加权 Huber IRLS（尺度取 1.4826·MAD），迭代次数固定以保证同输入同输出。"""
    w0 = np.ones(len(y)) if w is None else np.asarray(w, np.float64)
    beta = np.linalg.lstsq(X * np.sqrt(w0)[:, None], y * np.sqrt(w0), rcond=None)[0] if beta0 is None else np.asarray(beta0, np.float64)
    for _ in range(iters):
        r = y - X @ beta
        s = 1.4826 * float(np.median(np.abs(r))) + 1e-12
        ww = w0 * np.minimum(1.0, c * s / np.maximum(np.abs(r), 1e-12))
        A = (X * ww[:, None]).T @ X
        beta = np.linalg.solve(A + 1e-12 * np.trace(A) * np.eye(len(A)), (X * ww[:, None]).T @ y)
    return beta


def gradient_delta_deg(g1: Sequence[float], g2: Sequence[float]) -> float:
    """两个水平梯度向量之差折合的角度（FR-012）。"""
    return math.degrees(math.atan(math.hypot(float(g1[0]) - float(g2[0]), float(g1[1]) - float(g2[1]))))


def _seed(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "little")


def _pick(n_total: int, cap: int, seed: int) -> np.ndarray:
    if n_total <= cap:
        return np.arange(n_total)
    return np.sort(np.random.default_rng(seed).choice(n_total, cap, replace=False))


def _r(v: float | None, nd: int) -> float | None:
    return None if v is None or not math.isfinite(v) else round(float(v), nd)


# ---------------------------------------------------------------- 墙面竖直度


@dataclass(frozen=True)
class WallFit:
    g: tuple[float, float]              # 输入帧（世界帧）中的剩余梯度
    faces: int
    lean: float                         # 立面整体外倾 c（n_z 截距）
    ci95_half_deg: float
    halves: tuple[tuple[float, float], tuple[float, float]] | None
    halves_delta_deg: float | None

    @property
    def tilt_deg(self) -> float:
        return tilt_deg(self.g)

    def to_json(self) -> dict[str, Any]:
        return {"g": [round(v, 6) for v in self.g], "tilt_deg": round(self.tilt_deg, 4), "faces": self.faces, "lean": round(self.lean, 5),
                "ci95_half_deg": _r(self.ci95_half_deg, 4),
                "halves": None if self.halves is None else [[round(v, 6) for v in h] for h in self.halves],
                "halves_delta_deg": _r(self.halves_delta_deg, 4)}


def _wall_beta(n: np.ndarray, w: np.ndarray, *, iters: int = 20, beta0: np.ndarray | None = None) -> np.ndarray | None:
    """`n_z = c + b_E·n_x + b_N·n_y`；立面朝向不足以分离截距时（只有两个朝向）退化为无截距模型（c = 0）。"""
    X3 = np.c_[n[:, :2], np.ones(len(n))]
    G = (X3 * w[:, None]).T @ X3
    if len(n) >= 3 and np.linalg.matrix_rank(G, tol=1e-9 * max(float(np.trace(G)), 1e-300)) == 3:
        return huber_irls(X3, n[:, 2], w, iters=iters, beta0=beta0)
    G2 = G[:2, :2]
    if len(n) < 2 or np.linalg.matrix_rank(G2, tol=1e-9 * max(float(np.trace(G2)), 1e-300)) < 2:
        return None
    b = huber_irls(n[:, :2], n[:, 2], w, iters=iters, beta0=None if beta0 is None else beta0[:2])
    return np.r_[b, 0.0]


def wall_fit(N: np.ndarray, area: np.ndarray, *, band: float = WALL_BAND, cap: int = 200_000, boot: int = 200,
             seed_key: str = "lvl") -> WallFit | None:
    """墙面竖直度（r2 e03f 的改进版）：近竖直面 `n_z ≈ c − gE·n_x − gN·n_y` 的面积加权 Huber IRLS。

    N、area 为世界帧三角形单位法线与面积。先在 |n_z| < 0.5 的面中按固定种子抽取至多 `cap` 个，再迭代 `WALL_ITERS` 次：按当前
    估计调平法线、选取 |n_z| < `band` 的墙面、拟合剩余梯度并合成。CI95 半宽为 `boot` 次自助重采样梯度与点估计之差折合角度的
    95% 分位；两子集按立面方位（调平后 `atan2(n_y, n_x)`）20° 扇区序号的奇偶划分（M22 §6.6.4 `split_by_facade_azimuth_parity`；
    20° 使正交街区的四个朝向两两分入不同子集，每个子集都含两个正交朝向）。
    """
    pre = np.flatnonzero((np.abs(N[:, 2]) < 0.5) & (area > 0))
    if pre.size < 2:
        return None
    pre = pre[_pick(pre.size, cap, _seed(seed_key + ":cap"))]
    n0, w0 = N[pre], area[pre]
    g = (0.0, 0.0)
    beta = None
    for _ in range(WALL_ITERS):
        n = n0 @ np.array(level_matrix(g)).T
        m = np.abs(n[:, 2]) < band
        beta = _wall_beta(n[m], w0[m])
        if beta is None:
            return None
        g = source_gradient(g, (-float(beta[0]), -float(beta[1])))
    n = n0 @ np.array(level_matrix(g)).T
    m = np.abs(n[:, 2]) < band
    n, w = n[m], w0[m]
    b0 = _wall_beta(n, w)
    if b0 is None:
        return None
    r0 = (-float(b0[0]), -float(b0[1]))
    rng = np.random.default_rng(_seed(seed_key + ":boot"))
    d = []
    for _ in range(boot):
        k = rng.integers(0, len(n), len(n))
        bb = _wall_beta(n[k], w[k], iters=8, beta0=b0)
        if bb is not None:
            d.append(gradient_delta_deg((-bb[0], -bb[1]), r0))
    ci = float(np.percentile(d, 95)) if d else math.inf
    sector = np.floor((np.degrees(np.arctan2(n[:, 1], n[:, 0])) % 360.0) / 20.0).astype(np.int64)
    hs = []
    for par in (0, 1):
        sel = sector % 2 == par
        bh = _wall_beta(n[sel], w[sel])
        hs.append(None if bh is None else source_gradient(g, (-float(bh[0]), -float(bh[1]))))
    halves = None if hs[0] is None or hs[1] is None else (hs[0], hs[1])
    g_fin = source_gradient(g, r0)
    return WallFit(g_fin, len(n), float(b0[2]), ci, halves, None if halves is None else gradient_delta_deg(*halves))


# ---------------------------------------------------------------- DEM 回归


class HgtDem:
    """SRTM `.hgt`（1″ 3601² 或 3″ 1201²，大端 int16，−32768 为空，可为 `.hgt.gz`）的双线性采样；文件名给出瓦片西南角。"""

    def __init__(self, tiles: dict[tuple[int, int], np.ndarray], sources: list[dict[str, Any]]) -> None:
        self.tiles = tiles
        self.sources = sources

    @classmethod
    def load(cls, paths: Sequence[str | Path]) -> HgtDem:
        tiles: dict[tuple[int, int], np.ndarray] = {}
        src = []
        for p in map(Path, paths):
            mt = re.match(r"([NS])(\d{2})([EW])(\d{3})", p.name)
            if mt is None:
                raise ValueError(f"{p.name}: not an SRTM tile name")
            raw = p.read_bytes()
            data = gzip.decompress(raw) if p.suffix == ".gz" else raw
            n = math.isqrt(len(data) // 2)
            if n * n * 2 != len(data):
                raise ValueError(f"{p.name}: {len(data)} B is not a square int16 grid")
            a = np.frombuffer(data, ">i2").reshape(n, n).astype(np.float64)
            a[a == -32768] = np.nan
            lat0 = int(mt.group(2)) * (1 if mt.group(1) == "N" else -1)
            lon0 = int(mt.group(4)) * (1 if mt.group(3) == "E" else -1)
            tiles[(lat0, lon0)] = a
            src.append({"name": p.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "posts": n})
        return cls(tiles, src)

    def sample(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
        out = np.full(lat.shape, np.nan)
        la0, lo0 = np.floor(lat).astype(np.int64), np.floor(lon).astype(np.int64)
        for key, a in self.tiles.items():
            m = (la0 == key[0]) & (lo0 == key[1])
            if not m.any():
                continue
            n = a.shape[0] - 1
            r = (key[0] + 1 - lat[m]) * n                 # 行 0 为北缘
            c = (lon[m] - key[1]) * n
            r0, c0 = np.clip(np.floor(r).astype(np.int64), 0, n - 1), np.clip(np.floor(c).astype(np.int64), 0, n - 1)
            fr, fc = r - r0, c - c0
            out[m] = (a[r0, c0] * (1 - fr) * (1 - fc) + a[r0, c0 + 1] * (1 - fr) * fc + a[r0 + 1, c0] * fr * (1 - fc)
                      + a[r0 + 1, c0 + 1] * fr * fc)
        return out


_TIFF_TYPES = {1: "B", 2: "s", 3: "H", 4: "I", 5: "II", 11: "f", 12: "d", 16: "Q"}
_TIFF_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 11: 4, 12: 8, 16: 8}


def _tiff_tags(b: bytes) -> dict[int, tuple]:
    """小端经典 TIFF 首个 IFD 的标签表（{tag: 值元组}）；只用于下面的 GeoTIFF 读取。"""
    import struct

    if b[:4] != b"II*\x00":
        raise ValueError("not a little-endian classic TIFF")
    off = struct.unpack_from("<I", b, 4)[0]
    n = struct.unpack_from("<H", b, off)[0]
    out: dict[int, tuple] = {}
    for i in range(n):
        tag, typ, cnt, val = struct.unpack_from("<HHII", b, off + 2 + 12 * i)
        if typ not in _TIFF_TYPES:
            continue
        p = off + 2 + 12 * i + 8 if _TIFF_SIZES[typ] * cnt <= 4 else val
        out[tag] = (b[p:p + cnt],) if typ == 2 else struct.unpack_from("<" + _TIFF_TYPES[typ] * cnt, b, p)
    return out


def read_geotiff_dem(path: str | Path) -> tuple[np.ndarray, tuple[float, float, float, float], dict[str, Any]]:
    """单波段 float32 地理坐标 GeoTIFF（Copernicus DEM GLO-30 COG 的形式）读取：分块或分条、无压缩或 Deflate，预测器 1/2/3。

    返回 (高程数组, (北缘纬度, 西缘经度, 纬向步长, 经向步长), 来源记录)，数组行 0 为北缘，元素为像元中心高程（PixelIsPoint
    时 tiepoint 即像元中心；PixelIsArea 时向内平移半像元），空值（GDAL_NODATA 或 −32767 以下）为 NaN。只读主分辨率 IFD。
    """
    import zlib

    p = Path(path)
    raw = p.read_bytes()
    t = _tiff_tags(raw)
    W, H = int(t[256][0]), int(t[257][0])
    if t.get(258, (32,))[0] != 32 or t.get(339, (3,))[0] != 3 or t.get(277, (1,))[0] != 1:
        raise ValueError(f"{p.name}: only single-band float32 GeoTIFF is supported")
    comp, pred = int(t.get(259, (1,))[0]), int(t.get(317, (1,))[0])
    if comp not in (1, 8, 32946) or pred not in (1, 2, 3):
        raise ValueError(f"{p.name}: compression {comp} / predictor {pred} not supported")
    if 322 in t:
        tw, th, offs, cnts = int(t[322][0]), int(t[323][0]), t[324], t[325]
    else:
        tw, th, offs, cnts = W, int(t.get(278, (H,))[0]), t[273], t[279]
    nx = -(-W // tw)
    a = np.full((-(-H // th) * th, nx * tw), np.nan, np.float32)
    for k, (o, c) in enumerate(zip(offs, cnts, strict=True)):
        buf = raw[o:o + c]
        if comp != 1:
            buf = zlib.decompress(buf)
        rows = len(buf) // (tw * 4)
        u = np.frombuffer(buf, np.uint8)[:rows * tw * 4].reshape(rows, tw * 4)
        if pred == 3:                                       # 浮点预测器：逐行字节差分累加，再由字节平面（高位在前）重组
            u = np.cumsum(u, axis=1, dtype=np.uint8)
            v = u.reshape(rows, 4, tw).transpose(0, 2, 1).copy().view(">f4")[..., 0]
        else:
            v = u.copy().view("<f4")
            if pred == 2:
                v = np.cumsum(v, axis=1, dtype=np.float32)
        ty, tx = divmod(k, nx) if 322 in t else (k, 0)
        a[ty * th:ty * th + rows, tx * tw:tx * tw + tw] = v
    a = a[:H, :W].astype(np.float64)
    nod = t.get(42113)
    if nod:
        with_nod = float(nod[0].rstrip(b"\x00").decode() or "nan")
        a[a == with_nod] = np.nan
    a[a < -32767.0] = np.nan
    sx, sy = float(t[33550][0]), float(t[33550][1])
    ti = t[33922]
    lon0, lat0 = float(ti[3]) - float(ti[0]) * sx, float(ti[4]) + float(ti[1]) * sy
    keys = t.get(34735, ())
    rtype = next((keys[i + 3] for i in range(4, len(keys), 4) if keys[i] == 1025), 1)
    if rtype == 1:                                          # PixelIsArea：tiepoint 为像元角点
        lon0, lat0 = lon0 + sx / 2, lat0 - sy / 2
    src = {"name": p.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "posts": [H, W]}
    return a, (lat0, lon0, sy, sx), src


class GridDem:
    """经纬度规则格网 DEM（像元中心，行 0 为北缘）的双线性采样，接口同 `HgtDem`（Copernicus GLO-30 等 GeoTIFF）。"""

    def __init__(self, grids: list[tuple[np.ndarray, tuple[float, float, float, float]]], sources: list[dict[str, Any]]) -> None:
        self.grids = grids
        self.sources = sources

    @classmethod
    def load(cls, paths: Sequence[str | Path]) -> GridDem:
        grids, src = [], []
        for p in paths:
            a, geo, s = read_geotiff_dem(p)
            grids.append((a, geo))
            src.append(s)
        return cls(grids, src)

    def sample(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
        out = np.full(lat.shape, np.nan)
        for a, (lat0, lon0, dy, dx) in self.grids:
            r, c = (lat0 - lat) / dy, (lon - lon0) / dx
            m = (r >= 0) & (c >= 0) & (r <= a.shape[0] - 1) & (c <= a.shape[1] - 1) & np.isnan(out)
            if not m.any():
                continue
            r0 = np.clip(np.floor(r[m]).astype(np.int64), 0, a.shape[0] - 2)
            c0 = np.clip(np.floor(c[m]).astype(np.int64), 0, a.shape[1] - 2)
            fr, fc = r[m] - r0, c[m] - c0
            out[m] = (a[r0, c0] * (1 - fr) * (1 - fc) + a[r0, c0 + 1] * (1 - fr) * fc + a[r0 + 1, c0] * fr * (1 - fc)
                      + a[r0 + 1, c0 + 1] * fr * fc)
        return out


def load_dem(paths: Sequence[str | Path]) -> HgtDem | GridDem:
    """按扩展名选读取器：`.tif`/`.tiff` 为 GeoTIFF（`GridDem`），其余为 SRTM `.hgt`（`HgtDem`）；同一组文件须为同一种格式。"""
    ps = [Path(p) for p in paths]
    tif = [p.suffix.lower() in (".tif", ".tiff") for p in ps]
    if any(tif) and not all(tif):
        raise ValueError("one DEM set must not mix GeoTIFF and SRTM .hgt files")
    return GridDem.load(ps) if all(tif) and ps else HgtDem.load(ps)


@dataclass(frozen=True)
class DemFit:
    g: tuple[float, float]              # (gE, gN) / k，世界帧剩余梯度
    k: float                            # 竖向尺度比（模型 z 为米，理想为 1；R4）
    rmse_m: float
    mad_m: float
    samples: int

    @property
    def tilt_deg(self) -> float:
        return tilt_deg(self.g)

    def to_json(self) -> dict[str, Any]:
        return {"g": [round(v, 6) for v in self.g], "tilt_deg": round(self.tilt_deg, 4), "k": round(self.k, 4),
                "rmse_m": round(self.rmse_m, 3), "mad_m": round(self.mad_m, 3), "samples": self.samples}


def ground_faces(C: np.ndarray, N: np.ndarray, area: np.ndarray, *, cell_m: float = 10.0, max_hag_m: float = 1.0) -> np.ndarray:
    """地面候选面：n_z > 0.9 且质心高出 10 m DTM（M03 `dtm_opening`）不超过 `max_hag_m`。"""
    up = np.flatnonzero((N[:, 2] > GROUND_NZ) & (area > 0))
    if up.size < 3:
        return up
    dtm, gi = dtm_opening(C[up], cell=cell_m)
    hag = C[up, 2] - dtm[gi.iy, gi.ix]
    return up[np.abs(hag) <= max_hag_m]


def _stratified(C: np.ndarray, idx: np.ndarray, n: int, seed: int) -> np.ndarray:
    """按水平格分层抽样：格边长取使格数约为 n，每格取一个（格内按固定种子随机），再截到 n。"""
    if idx.size <= n:
        return idx
    lo, hi = C[idx, :2].min(0), C[idx, :2].max(0)
    cell = max(float(np.sqrt(np.prod(np.maximum(hi - lo, 1e-6)) / n)), 1e-6)
    key = np.floor((C[idx, 0] - lo[0]) / cell).astype(np.int64) * 1_000_003 + np.floor((C[idx, 1] - lo[1]) / cell).astype(np.int64)
    r = np.random.default_rng(seed).random(idx.size)
    o = np.lexsort((r, key))
    first = o[np.r_[True, key[o][1:] != key[o][:-1]]]
    pick = idx[first]
    if pick.size > n:
        pick = pick[_pick(pick.size, n, seed + 1)]
    return np.sort(pick)


def dem_fit(C: np.ndarray, gidx: np.ndarray, dem: HgtDem, anchor: Anchor, *, n: int = 2000, seed_key: str = "dem") -> DemFit | None:
    """DEM 回归（r2 e03c；M22 §6.6.4）：地面候选面质心分层抽样 n 个，`h = k·z + gE·E + gN·N + c`，Huber IRLS。"""
    pick = _stratified(C, gidx, n, _seed(seed_key))
    if pick.size < 10:
        return None
    P = C[pick]
    lat, lon, _ = world_to_lla(P, anchor)
    h = dem.sample(np.ravel(lat), np.ravel(lon))
    ok = np.isfinite(h)
    if ok.sum() < 10:
        return None
    P, h = P[ok], h[ok]
    X = np.c_[P[:, 2], P[:, 0], P[:, 1], np.ones(len(P))]
    beta = huber_irls(X, h)
    k, gE, gN = (float(v) for v in beta[:3])
    if abs(k) < 1e-6:
        return None
    r = h - X @ beta
    return DemFit((gE / k, gN / k), k, float(np.sqrt(np.mean(r ** 2))), float(np.median(np.abs(r - np.median(r)))), len(P))


# ---------------------------------------------------------------- OSM、水平面与控制点


@dataclass
class OsmData:
    """OSM XML（API 0.6 `map` 或 Overpass `out meta`）中的建筑轮廓与已知水平面候选（经纬度闭合环 [lat, lon]）。"""

    buildings: list[np.ndarray] = field(default_factory=list)
    planes: list[dict[str, Any]] = field(default_factory=list)             # {id, kind, name, ring}
    sources: list[dict[str, Any]] = field(default_factory=list)


def _plane_kind(tags: dict[str, str]) -> str | None:
    if tags.get("natural") == "water" or tags.get("waterway") == "riverbank" or (tags.get("water") and tags.get("natural") in (None, "water")):
        return "water"
    if tags.get("place") == "square" or (tags.get("highway") == "pedestrian" and tags.get("area") == "yes"):
        return "square"
    return None


def _join_rings(segs: list[list[int]]) -> list[list[int]]:
    """多边形关系外环拼接：首尾相接的路径段合成闭合环（不能闭合的残段丢弃）。"""
    segs = [s[:] for s in segs if len(s) >= 2]
    rings = []
    while segs:
        cur = segs.pop(0)
        changed = True
        while cur[0] != cur[-1] and changed:
            changed = False
            for i, s in enumerate(segs):
                if s[0] == cur[-1]:
                    cur += s[1:]
                elif s[-1] == cur[-1]:
                    cur += s[::-1][1:]
                elif s[-1] == cur[0]:
                    cur = s[:-1] + cur
                elif s[0] == cur[0]:
                    cur = s[::-1][:-1] + cur
                else:
                    continue
                segs.pop(i)
                changed = True
                break
        if cur[0] == cur[-1] and len(cur) >= 4:
            rings.append(cur)
    return rings


def parse_osm(paths: str | Path | Sequence[str | Path]) -> OsmData:
    """读取一个或多个 OSM XML 并按元素 id 去重合并（建筑：闭合 way；水平面：闭合 way 与 multipolygon 关系外环）。"""
    plist = [Path(paths)] if isinstance(paths, (str, Path)) else [Path(p) for p in paths]
    nodes: dict[int, tuple[float, float]] = {}
    ways: dict[int, tuple[list[int], dict[str, str]]] = {}
    rels: dict[int, tuple[list[tuple[str, int, str]], dict[str, str]]] = {}
    out = OsmData()
    for p in plist:
        raw = p.read_bytes()
        out.sources.append({"name": p.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        root = ET.fromstring(raw)
        for nd in root.iter("node"):
            nodes[int(nd.get("id"))] = (float(nd.get("lat")), float(nd.get("lon")))
        for w in root.iter("way"):
            ways[int(w.get("id"))] = ([int(x.get("ref")) for x in w.iter("nd")], {t.get("k"): t.get("v") for t in w.iter("tag")})
        for r in root.iter("relation"):
            rels[int(r.get("id"))] = ([(m.get("type"), int(m.get("ref")), m.get("role") or "") for m in r.iter("member")],
                                      {t.get("k"): t.get("v") for t in r.iter("tag")})

    def ring(ids: list[int]) -> np.ndarray | None:
        if len(ids) < 4 or ids[0] != ids[-1] or any(i not in nodes for i in ids):
            return None
        return np.array([nodes[i] for i in ids], np.float64)

    for wid in sorted(ways):
        ids, tags = ways[wid]
        rg = ring(ids)
        if rg is None:
            continue
        if "building" in tags:
            out.buildings.append(rg)
        kind = _plane_kind(tags)
        if kind:
            out.planes.append({"id": f"w{wid}", "kind": kind, "name": tags.get("name", ""), "ring": rg})
    for rid in sorted(rels):
        mem, tags = rels[rid]
        kind = _plane_kind(tags)
        if tags.get("type") != "multipolygon" or not kind:
            continue
        outer = [ways[ref][0] for t, ref, role in mem if t == "way" and role in ("outer", "") and ref in ways]
        for k, ids in enumerate(_join_rings(outer)):
            rg = ring(ids)
            if rg is not None:
                out.planes.append({"id": f"r{rid}-{k}", "kind": kind, "name": tags.get("name", ""), "ring": rg})
    return out


def rings_to_world(rings: Sequence[np.ndarray], anchor: Anchor) -> list[np.ndarray]:
    """经纬度环 → 世界帧 (E, N)（M02 `lla_to_world`，高程取 0）。"""
    return [lla_to_world(r[:, 0], r[:, 1], np.zeros(len(r)), anchor)[:, :2] for r in rings]


def load_horizontal_planes(path: str | Path, anchor: Anchor) -> list[dict[str, Any]]:
    """`horizontal_planes.geojson`（M22 §7.6.3）：Polygon 要素；`properties.crs = "world-enu"` 时坐标为世界帧 (E, N)，否则为 (lon, lat)。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for i, f in enumerate(doc.get("features", [])):
        if (f.get("geometry") or {}).get("type") != "Polygon":
            continue
        props = f.get("properties", {}) or {}
        rg = np.asarray(f["geometry"]["coordinates"][0], np.float64)
        xy = rg[:, :2] if props.get("crs") == "world-enu" else lla_to_world(rg[:, 1], rg[:, 0], np.zeros(len(rg)), anchor)[:, :2]
        out.append({"id": str(props.get("id", f"m{i}")), "kind": str(props.get("kind", "manual")), "name": str(props.get("name", "")),
                    "xy": xy})
    return out


def _poly_area(xy: np.ndarray) -> float:
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _inside(P: np.ndarray, xy: np.ndarray) -> np.ndarray:
    """偶奇规则点在多边形内（逐边射线法，点向量化）。"""
    x, y = P[:, 0], P[:, 1]
    inside = np.zeros(len(P), bool)
    for a, b, c, d in zip(xy[:-1, 0], xy[:-1, 1], xy[1:, 0], xy[1:, 1], strict=True):
        if b == d:
            continue
        cond = (b > y) != (d > y)
        inside ^= cond & (x < a + (y - b) * (c - a) / (d - b))
    return inside


def _edge_distance(P: np.ndarray, xy: np.ndarray, chunk: int = 4096) -> np.ndarray:
    """点到多边形边界的最小距离。"""
    A, B = xy[:-1], xy[1:]
    AB = B - A
    L2 = np.maximum((AB ** 2).sum(1), 1e-300)
    out = np.empty(len(P))
    for i in range(0, len(P), chunk):
        Q = P[i:i + chunk, None, :]
        t = np.clip(((Q - A) * AB).sum(2) / L2, 0.0, 1.0)
        out[i:i + chunk] = np.sqrt((((A + t[..., None] * AB) - Q) ** 2).sum(2)).min(1)
    return out


def plane_slopes(C: np.ndarray, N: np.ndarray, area: np.ndarray, planes: Sequence[dict[str, Any]], *,
                 min_area_m2: float = L3_MIN_AREA_M2, edge_m: float = PLANE_EDGE_M, segment_m: float = PLANE_SEGMENT_M,
                 exclude: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """已知水平面坡度（FR-013 L3；M22 §6.6.4 末段）：多边形内近水平面按面积加权拟合 `z = a·E + b·N + c`，坡度 = atan(|(a, b)|)。

    语义图层落地前以法线（n_z > 0.9）代替 `cls ∈ {water, road_surface, ground}`，并排除距多边形边界 `edge_m` 以内的面（岸线、
    码头与 OSM 轮廓误差带）；拟合用 Huber IRLS 抑制多边形内的船只与桥。覆盖内沿主轴延伸超过 `segment_m` 的多边形（河流）
    等分为 `ceil(延伸 / segment_m)` 段（≥ 2），每段一个平面（M22 §8.1"多瑙河水面覆盖内分 ≥ 2 段"）。网格面积不足
    `min_area_m2` 的平面不计入评估；`exclude` 中的 id（人工核实并非水平的面，附原因）记入结果但不计入。
    """
    exclude = exclude or {}
    flat = np.flatnonzero((N[:, 2] > GROUND_NZ) & (area > 0))
    Cf = C[flat]
    out = []
    for pl in planes:
        xy = np.asarray(pl["xy"], np.float64)
        if not np.array_equal(xy[0], xy[-1]):
            xy = np.vstack([xy, xy[:1]])
        lo, hi = xy.min(0), xy.max(0)
        bb = np.flatnonzero((Cf[:, 0] >= lo[0]) & (Cf[:, 0] <= hi[0]) & (Cf[:, 1] >= lo[1]) & (Cf[:, 1] <= hi[1]))
        sel = bb[_inside(Cf[bb], xy)] if bb.size else bb
        if sel.size and edge_m > 0:
            sel = sel[_edge_distance(Cf[sel, :2], xy) >= edge_m]
        groups = [(pl["id"], sel)]
        if sel.size >= 2:
            P2 = Cf[sel, :2] - Cf[sel, :2].mean(0)
            axis = np.linalg.svd(P2, full_matrices=False)[2][0]
            u = P2 @ axis
            ext = float(u.max() - u.min())
            if ext > segment_m:
                k = max(2, math.ceil(ext / segment_m))
                j = np.minimum(((u - u.min()) / ext * k).astype(np.int64), k - 1)
                groups = [(f"{pl['id']}#{q + 1}", sel[j == q]) for q in range(k)]
        for gid, s in groups:
            a_mesh = float(area[flat[s]].sum()) if s.size else 0.0
            rec = {"id": gid, "kind": pl.get("kind", ""), "name": pl.get("name", ""), "polygon_area_m2": round(_poly_area(xy), 1),
                   "mesh_area_m2": round(a_mesh, 1), "faces": int(s.size), "slope_deg": None, "evaluated": False}
            if pl["id"] in exclude or gid in exclude:
                rec["excluded"] = exclude.get(gid, exclude.get(pl["id"]))
            elif a_mesh >= min_area_m2 and s.size >= 10:
                P = Cf[s]
                ga, gb, _ = huber_irls(np.c_[P[:, 0], P[:, 1], np.ones(len(P))], P[:, 2], area[flat[s]])
                rec["slope_deg"] = round(math.degrees(math.atan(math.hypot(ga, gb))), 4)
                rec["evaluated"] = True
            out.append(rec)
    return out


def load_control_points(path: str | Path, anchor: Anchor) -> list[dict[str, Any]]:
    """`control_points.json`：`{"points": [{id, lat_deg, lon_deg, h_msl_m} | {id, e_m, n_m, z_world_m}]}`（只取带高程的点）。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    h0 = 0.0 if anchor.h_msl_m is None else float(anchor.h_msl_m)
    out = []
    for i, p in enumerate(doc.get("points", [])):
        if "e_m" in p and "z_world_m" in p:
            e, n, z = float(p["e_m"]), float(p["n_m"]), float(p["z_world_m"])
        elif p.get("h_msl_m") is not None:
            w = lla_to_world(np.array([p["lat_deg"]]), np.array([p["lon_deg"]]), np.zeros(1), anchor)[0]
            e, n, z = float(w[0]), float(w[1]), float(p["h_msl_m"]) - h0
        else:
            continue
        out.append({"id": str(p.get("id", f"cp{i}")), "e": e, "n": n, "z": z})
    return out


def control_point_rms(height_at: Callable[[np.ndarray], np.ndarray], cps: Sequence[dict[str, Any]], *,
                      min_spacing_m: float = 50.0) -> dict[str, Any]:
    """控制点 AGL 残差（FR-013 L4）：网格地面高 `height_at(E, N)` 与控制点世界帧高程之差的 RMS。

    "分散"按两两最小间距判定：与已计入点距离小于 `min_spacing_m` 的点不计入（按输入顺序贪心）。
    """
    if not cps:
        return {"points": 0, "rms_m": None, "max_abs_m": None, "residuals": []}
    EN = np.array([[c["e"], c["n"]] for c in cps], np.float64)
    keep: list[int] = []
    for i in range(len(EN)):
        if all(np.hypot(*(EN[i] - EN[j])) >= min_spacing_m for j in keep):
            keep.append(i)
    z = height_at(EN)
    r = z - np.array([c["z"] for c in cps])
    ok = np.zeros(len(cps), bool)
    ok[keep] = True
    ok &= np.isfinite(r)
    return {"points": int(ok.sum()), "rms_m": _r(float(np.sqrt(np.mean(r[ok] ** 2))), 3) if ok.any() else None,
            "max_abs_m": _r(float(np.abs(r[ok]).max()), 3) if ok.any() else None,
            "residuals": [{"id": c["id"], "dz_m": _r(float(v), 3), "counted": bool(k)} for c, v, k in zip(cps, r, ok, strict=True)]}


def ground_height_fn(V: np.ndarray, F: np.ndarray, gidx: np.ndarray) -> Callable[[np.ndarray], np.ndarray]:
    """地面候选三角形上的竖向插值高程：(E, N) 落入的地面三角形按重心坐标插值，多个命中取最低（地面）；未命中为 NaN。"""
    T = V[F[gidx]]
    cen = T[:, :, :2].mean(1)
    rmax = float(np.sqrt(((T[:, :, :2] - cen[:, None, :]) ** 2).sum(2)).max()) if len(T) else 0.0
    tree = cKDTree(cen) if len(T) else None

    def at(EN: np.ndarray) -> np.ndarray:
        out = np.full(len(EN), np.nan)
        if tree is None:
            return out
        for i, cand in enumerate(tree.query_ball_point(np.asarray(EN, np.float64), rmax + 1e-9)):
            if not cand:
                continue
            t = T[np.asarray(cand)]
            a, b, c = t[:, 0, :2], t[:, 1, :2], t[:, 2, :2]
            d = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
            ok = np.abs(d) > 1e-12
            if not ok.any():
                continue
            p = EN[i]
            with np.errstate(divide="ignore", invalid="ignore"):
                l1 = ((b[:, 1] - c[:, 1]) * (p[0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (p[1] - c[:, 1])) / d
                l2 = ((c[:, 1] - a[:, 1]) * (p[0] - c[:, 0]) + (a[:, 0] - c[:, 0]) * (p[1] - c[:, 1])) / d
            l3 = 1.0 - l1 - l2
            hit = ok & (l1 >= -1e-9) & (l2 >= -1e-9) & (l3 >= -1e-9)
            if hit.any():
                out[i] = float((l1 * t[:, 0, 2] + l2 * t[:, 1, 2] + l3 * t[:, 2, 2])[hit].min())
        return out

    return at


# ---------------------------------------------------------------- 尺度独立复测


@dataclass(frozen=True)
class ScaleFit:
    rho: float                          # OSM 米 / 模型米：实测尺度 = 配准尺度 × rho
    yaw_deg: float
    t_m: tuple[float, float]
    cost_m: float                       # 截断倒角距离均值
    buildings: int
    points: int
    restarts_rel_std: float             # R5：20 次扰动重启 rho 的标准差 / 均值

    def to_json(self) -> dict[str, Any]:
        return {"rho": round(self.rho, 6), "yaw_deg": round(self.yaw_deg, 4), "t_m": [round(v, 3) for v in self.t_m],
                "cost_m": round(self.cost_m, 4), "buildings": self.buildings, "points": self.points,
                "restarts_rel_std": self.restarts_rel_std}


def measure_scale(C: np.ndarray, N: np.ndarray, area: np.ndarray, buildings_xy: Sequence[np.ndarray], *, step_m: float = 1.5,
                  trunc_m: float = 3.0, margin_m: float = 20.0, restarts: int = 20, min_buildings: int = 10,
                  seed_key: str = "scale") -> ScaleFit | None:
    """尺度独立复测（R5；D3-AC-02"尺度与 r2 结果差 ≤ 0.5%"）：OSM 建筑轮廓（世界帧，按 `step_m` 等距取点）对网格墙面质心
    （|n_z| < 0.15）的二维截断倒角距离，以 Nelder–Mead 同时优化（尺度比 rho、偏航、平移）；随后从 `restarts` 个扰动初值重启，
    以 rho 的相对标准差作为尺度不确定度。只用完全落在网格覆盖内（外缩 `margin_m`）的建筑。
    """
    m = (np.abs(N[:, 2]) < 0.15) & (area > 0.2)
    if m.sum() < 100:
        return None
    lo, hi = C[m, :2].min(0) + margin_m, C[m, :2].max(0) - margin_m
    B = [b for b in buildings_xy if (b.min(0) >= lo).all() and (b.max(0) <= hi).all()]
    if len(B) < min_buildings:
        return None
    pts = []
    for b in B:
        for a, c in itertools.pairwise(b):
            k = max(int(np.linalg.norm(c - a) // step_m), 1)
            pts.append(a + ((np.arange(k) + 0.5) / k)[:, None] * (c - a))
    Q = np.concatenate(pts)
    c0 = Q.mean(0)
    Q0 = Q - c0
    tree = cKDTree(C[m, :2])

    def cost(x: np.ndarray) -> float:
        rho, th, tx, ty = (float(v) for v in x)
        if rho <= 0:
            return trunc_m
        ct, st = math.cos(th), math.sin(th)
        P = np.c_[ct * Q0[:, 0] + st * Q0[:, 1], -st * Q0[:, 0] + ct * Q0[:, 1]] / rho + c0 - (tx, ty)
        d, _ = tree.query(P, distance_upper_bound=trunc_m)
        return float(np.mean(np.minimum(d, trunc_m)))

    step = np.array([0.01, 0.005, 2.0, 2.0])                 # 单纯形初始步长（各参数量纲不同，不用 SciPy 缺省的 5% 相对步长）

    def solve(x0: np.ndarray, maxfev: int) -> Any:
        simplex = np.vstack([x0, x0 + np.diag(step)])
        return optimize.minimize(cost, x0, method="Nelder-Mead",
                                 options={"xatol": 1e-6, "fatol": 1e-5, "maxfev": maxfev, "initial_simplex": simplex})

    best = solve(np.array([1.0, 0.0, 0.0, 0.0]), 1500)
    rng = np.random.default_rng(_seed(seed_key))
    rhos = []
    for _ in range(restarts):
        r = solve(best.x + rng.normal(0.0, 1.0, 4) * (0.005, 0.003, 1.5, 1.5), 800)
        rhos.append(float(r.x[0]))
        if r.fun < best.fun - 1e-9:
            best = r
    rel = float(np.std(rhos) / np.mean(rhos)) if rhos else math.inf
    return ScaleFit(float(best.x[0]), math.degrees(float(best.x[1])), (float(best.x[2]), float(best.x[3])), float(best.fun), len(B),
                    len(Q), rel)


# ---------------------------------------------------------------- 输入、汇总与门禁


@dataclass
class QaInputs:
    """标定 QA 的外部输入（配方 `qa` 段，路径相对 `data/raw/d3-glb/` 并经入库清单校验；人工增补文件在配方目录）。"""

    dem: HgtDem | GridDem | None = None
    dem_cross: list[tuple[str, HgtDem | GridDem]] = field(default_factory=list)     # 交叉核对 DEM（配方 `qa.dem`，按 model 分组）
    osm: OsmData | None = None
    planes_extra: list[dict[str, Any]] = field(default_factory=list)       # 世界帧多边形（horizontal_planes.geojson）
    control_points: list[dict[str, Any]] = field(default_factory=list)
    reference_m_per_unit: float | None = None
    planes_exclude: dict[str, str] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_recipe(cls, recipe: Any, anchor: Anchor, *, verify: Callable[..., Path] | None = None) -> QaInputs:
        """配方顶层 `dem`、`osm`（与 W03 的 `derivedFrom` 共用，单个对象或列表）加 `qa` 段：`osm_extra`（水面等补充抽取）、
        `dem`（交叉核对 DEM，按 `model` 分组，每组一次独立回归，L5）、`reference_m_per_unit`（缺省取 `R2_SCALE_M_PER_UNIT`）、
        `planes_exclude`（{id: 原因}）。"""
        from awr.world.glb.recipe import verify_raw

        verify = verify or verify_raw
        d = recipe.doc
        q = d.get("qa") or {}
        out = cls(reference_m_per_unit=q.get("reference_m_per_unit", R2_SCALE_M_PER_UNIT.get(recipe.world_id)),
                  planes_exclude={str(k): str(v) for k, v in (q.get("planes_exclude") or {}).items()})

        def entries(*vals: Any) -> list[dict[str, Any]]:
            out_: list[dict[str, Any]] = []
            for v in vals:
                out_ += [] if not v else ([v] if isinstance(v, dict) else list(v))
            return out_

        dem = entries(d.get("dem"))
        osm = entries(d.get("osm"), q.get("osm_extra"))
        if dem:
            out.dem = load_dem([verify(e["path"], e.get("sha256")) for e in dem])
            out.files += [{**s, "use": "levelling"} for s in out.dem.sources]
        groups: dict[str, list[dict[str, Any]]] = {}
        for e in entries(q.get("dem")):
            groups.setdefault(str(e.get("model", "dem")), []).append(e)
        for model, es in groups.items():
            dm = load_dem([verify(e["path"], e.get("sha256")) for e in es])
            out.dem_cross.append((model, dm))
            out.files += [{**s, "use": f"levelling cross-check ({model})"} for s in dm.sources]
        if osm:
            out.osm = parse_osm([verify(e["path"], e.get("sha256")) for e in osm])
            out.files += [{**s, "use": "levelling planes, scale check"} for s in out.osm.sources]
        hp = recipe.dir / "horizontal_planes.geojson"
        if hp.exists():
            out.planes_extra = load_horizontal_planes(hp, anchor)
        cp = recipe.dir / "control_points.json"
        if cp.exists():
            out.control_points = load_control_points(cp, anchor)
        return out


@dataclass
class GlbQa:
    gates: list[GateResult]
    report: dict[str, Any]              # qa/report.json `ingest.glb`：leveling、scale、inputs（M22 §7.3.8；非内容文件）
    needs_control_points: bool


def dem_cross_check(dem: DemFit | None, cross: Sequence[tuple[str, DemFit | None]], wall: WallFit | None,
                    applied_g: Sequence[float]) -> dict[str, Any] | None:
    """L5 公开 DEM 交叉核对（公里级）：主 DEM、交叉核对 DEM 与墙面竖直度各自测得的世界帧剩余梯度，判定两两之差、各法剩余倾角、
    三法共识（剩余梯度均值）倾角与各 DEM 的竖向尺度比。没有交叉核对 DEM 时返回 None；任一方法缺失时 `pass = false`。"""
    if not cross:
        return None
    ag = (float(applied_g[0]), float(applied_g[1]))
    fits: list[tuple[str, DemFit | WallFit | None]] = [("dem", dem), *((f"dem:{m}", f) for m, f in cross), ("wall", wall)]
    have = [(k, f) for k, f in fits if f is not None]
    missing = [k for k, f in fits if f is None]
    pairs = [{"a": a, "b": b, "delta_deg": round(gradient_delta_deg(fa.g, fb.g), 4)} for (a, fa), (b, fb) in itertools.combinations(have, 2)]
    res = {k: round(f.tilt_deg, 4) for k, f in have}
    ks = {k: round(f.k, 4) for k, f in have if isinstance(f, DemFit)}
    cons = None if not have else (float(np.mean([f.g[0] for _, f in have])), float(np.mean([f.g[1] for _, f in have])))
    ok = (not missing and all(p["delta_deg"] <= L5_PAIR_MAX_DEG for p in pairs) and all(v <= L5_RESIDUAL_MAX_DEG for v in res.values())
          and cons is not None and tilt_deg(cons) <= L5_CONSENSUS_MAX_DEG and all(R4_RANGE[0] <= v <= R4_RANGE[1] for v in ks.values()))
    return {"methods": [k for k, _ in fits], "missing": missing,
            "fits": {k: f.to_json() for k, f in have if isinstance(f, DemFit) and k != "dem"},
            "pairs": pairs, "max_pair_delta_deg": max((p["delta_deg"] for p in pairs), default=None),
            "residual_tilt_deg": res, "k": ks,
            "consensus_g": None if cons is None else [round(v, 6) for v in cons],
            "consensus_tilt_deg": None if cons is None else round(tilt_deg(cons), 4),
            "suggested_applied_g": None if cons is None else [round(v, 9) for v in source_gradient(ag, cons)],
            "limits": {"pair_deg": L5_PAIR_MAX_DEG, "residual_deg": L5_RESIDUAL_MAX_DEG, "consensus_deg": L5_CONSENSUS_MAX_DEG,
                       "k": list(R4_RANGE)},
            "pass": bool(ok)}


def levelling_report(*, applied_g: Sequence[float], km_scale: bool, dem: DemFit | None, wall: WallFit | None,
                     planes: list[dict[str, Any]], cps: dict[str, Any], ground_mad_m: float | None,
                     dem_cross: Sequence[tuple[str, DemFit | None]] = ()) -> dict[str, Any]:
    """M22 §7.3.8 `glb.leveling`：主方法、两法剩余梯度与源帧倾角、差值、CI95、水平面逐个坡度、控制点残差、L5 公开 DEM 交叉
    核对（`cross_check`）、`needs_control_points`。"""
    ag = (float(applied_g[0]), float(applied_g[1]))
    primary = "dem" if km_scale and dem is not None else ("wall" if wall is not None else None)
    res = None if primary is None else (dem.g if primary == "dem" else wall.g)          # type: ignore[union-attr]
    delta = gradient_delta_deg(dem.g, wall.g) if dem is not None and wall is not None else None
    ev = [p for p in planes if p["evaluated"]]
    lim = L3_MAX_SLOPE_DEG if km_scale else L3_MAX_SLOPE_SMALL_DEG
    max_slope = max((p["slope_deg"] for p in ev), default=None)
    l1 = delta is not None and delta <= L1_MAX_DEG
    l2 = (wall is not None and wall.halves_delta_deg is not None and wall.halves_delta_deg <= L2_MAX_DEG
          and wall.ci95_half_deg <= L2_MAX_DEG)
    l3 = len(ev) >= L3_MIN_PLANES and max_slope is not None and max_slope <= lim
    l4 = cps["points"] >= L4_MIN_POINTS and cps["rms_m"] is not None and cps["rms_m"] <= L4_MAX_RMS_M
    xc = dem_cross_check(dem, dem_cross, wall, ag) if km_scale else None
    l5 = bool(xc and xc["pass"])
    needs = not ((l1 if km_scale else l2) and (l3 or l4 or l5))
    src = {k: (None if f is None else source_gradient(ag, f.g)) for k, f in (("dem", dem), ("wall", wall))}
    return {"primary": primary, "km_scale": km_scale, "applied_g": [round(v, 9) for v in ag], "applied_tilt_deg": round(tilt_deg(ag), 4),
            "residual_g": None if res is None else [round(v, 6) for v in res], "residual_tilt_deg": None if res is None else round(tilt_deg(res), 4),
            "dem": None if dem is None else {**dem.to_json(), "source_g": [round(v, 6) for v in src["dem"]],          # type: ignore[union-attr]
                                             "source_tilt_deg": round(tilt_deg(src["dem"]), 4)},                       # type: ignore[arg-type]
            "wall": None if wall is None else {**wall.to_json(), "source_g": [round(v, 6) for v in src["wall"]],       # type: ignore[union-attr]
                                               "source_tilt_deg": round(tilt_deg(src["wall"]), 4)},                    # type: ignore[arg-type]
            "delta_deg": _r(delta, 4), "ground_mad_m": _r(ground_mad_m, 3),
            "planes": planes, "planes_evaluated": len(ev), "planes_max_slope_deg": _r(max_slope, 4), "planes_limit_deg": lim,
            "subset_delta_deg": None if wall is None else _r(wall.halves_delta_deg, 4),
            "ci95_deg": None if wall is None else _r(wall.ci95_half_deg, 4),
            "control_points": {**cps, "n": cps["points"]}, "cross_check": xc, "L1": l1, "L2": l2, "L3": l3, "L4": l4, "L5": l5,
            "needs_control_points": needs,
            "suggested_applied_g": None if res is None else [round(v, 9) for v in source_gradient(ag, res)]}


def _c(v: float | None, nd: int) -> float | None:
    """门禁取值随 `coordinate.json`（内容文件）发布：统计量按较粗位数取整，避免跨 ISA 的末位差异改变内容 sha256（D3-AC-02）。"""
    return _r(v, nd)


def glb_gates(lev: dict[str, Any], scale: dict[str, Any] | None) -> list[GateResult]:
    """GLB 标定门禁：替换 D2 的 G-04、G-05，追加 G-11（L1，公里级）或 G-12（L2，小场景）、G-13（L3 或 L4）、G-14（R4，公里级）、
    G-15（尺度复测与 R5）。判定用全精度量，门禁取值按 `_c` 取整；取不到值的门禁一律判不通过。"""
    mad, res = lev["ground_mad_m"], lev["residual_tilt_deg"]
    dem, wall = lev["dem"] or {}, lev["wall"] or {}
    gates = [
        GateResult("G-04", "ground residual MAD < 5 m after levelling", _c(mad, 2), G04_MAX_MAD_M, mad is not None and mad < G04_MAX_MAD_M,
                   "error"),
        GateResult("G-05", "tilt after levelling < 0.5 deg", _c(res, 3), G05_MAX_DEG, res is not None and res < G05_MAX_DEG, "error"),
    ]
    if lev["km_scale"]:
        gates.append(GateResult("G-11", "L1 DEM and wall levelling differ <= 0.5 deg (else control points required)",
                                {"primary": lev["primary"], "dem_deg": _c(dem.get("source_tilt_deg"), 3), "wall_deg": _c(wall.get("source_tilt_deg"), 3),
                                 "delta_deg": _c(lev["delta_deg"], 3)}, L1_MAX_DEG, bool(lev["L1"]), "warn"))
    else:
        gates.append(GateResult("G-12", "L2 wall subsets and CI95 half-width <= 0.3 deg",
                                {"halves_delta_deg": _c(wall.get("halves_delta_deg"), 3), "ci95_half_deg": _c(wall.get("ci95_half_deg"), 3)},
                                L2_MAX_DEG, bool(lev["L2"]), "warn"))
    cp = lev["control_points"]
    xc = lev.get("cross_check")
    l5 = bool(lev.get("L5"))
    if xc is None:
        gates.append(GateResult("G-13", f"L3 >= 3 horizontal planes slope <= {lev['planes_limit_deg']} deg, or L4 >= 20 control points AGL RMS <= 2 m",
                                {"planes_evaluated": lev["planes_evaluated"], "planes_max_slope_deg": _c(lev["planes_max_slope_deg"], 3),
                                 "control_points": cp["points"], "control_rms_m": _c(cp["rms_m"], 2)},
                                {"planes": L3_MIN_PLANES, "slope_deg": lev["planes_limit_deg"], "points": L4_MIN_POINTS, "rms_m": L4_MAX_RMS_M},
                                bool(lev["L3"] or lev["L4"]), "warn"))
    else:
        gates.append(GateResult("G-13", f"L3 >= 3 horizontal planes slope <= {lev['planes_limit_deg']} deg, or L4 >= 20 control points AGL RMS <= 2 m, "
                                "or L5 public-DEM cross-check (DEM, DEM, wall: pairwise <= 0.5 deg, residual <= 0.5 deg, consensus <= 0.2 deg)",
                                {"planes_evaluated": lev["planes_evaluated"], "planes_max_slope_deg": _c(lev["planes_max_slope_deg"], 3),
                                 "control_points": cp["points"], "control_rms_m": _c(cp["rms_m"], 2),
                                 "cross_max_pair_deg": _c(xc["max_pair_delta_deg"], 3), "cross_max_residual_deg":
                                     _c(max(xc["residual_tilt_deg"].values(), default=None), 3),
                                 "cross_consensus_deg": _c(xc["consensus_tilt_deg"], 3)},
                                {"planes": L3_MIN_PLANES, "slope_deg": lev["planes_limit_deg"], "points": L4_MIN_POINTS, "rms_m": L4_MAX_RMS_M,
                                 "cross_pair_deg": L5_PAIR_MAX_DEG, "cross_residual_deg": L5_RESIDUAL_MAX_DEG,
                                 "cross_consensus_deg": L5_CONSENSUS_MAX_DEG},
                                bool(lev["L3"] or lev["L4"] or l5), "warn"))
        if l5:                                              # L5 成立时水平面判据降为告警（用户 2026-10-19 决定）
            gates.append(GateResult("G-16", f"L3 advisory: >= 3 horizontal planes slope <= {lev['planes_limit_deg']} deg (L5 accepted)",
                                    {"planes_evaluated": lev["planes_evaluated"], "planes_max_slope_deg": _c(lev["planes_max_slope_deg"], 3)},
                                    {"planes": L3_MIN_PLANES, "slope_deg": lev["planes_limit_deg"]}, bool(lev["L3"]), "warn"))
    if lev["km_scale"]:
        k = dem.get("k")
        gates.append(GateResult("G-14", "R4 DEM vertical scale ratio k in [0.85, 1.15]", _c(k, 3), list(R4_RANGE),
                                k is not None and R4_RANGE[0] <= k <= R4_RANGE[1], "warn"))
    name = "independent scale within 0.5% of r2, restart spread <= 0.5% (R5)"
    if scale is None or scale.get("rel_diff") is None:
        gates.append(GateResult("G-15", name, None if scale is None else {"measured_m_per_unit": _c(scale["measured_m_per_unit"], 3)},
                                SCALE_MAX_REL, False, "warn"))
    else:
        rel, sd = float(scale["rel_diff"]), float(scale["fit"]["restarts_rel_std"])
        gates.append(GateResult("G-15", name, {"measured_m_per_unit": _c(scale["measured_m_per_unit"], 3),
                                               "reference_m_per_unit": scale["reference_m_per_unit"], "rel_diff": _c(rel, 4),
                                               "restarts_rel_std": _c(sd, 4)},
                                SCALE_MAX_REL, abs(rel) <= SCALE_MAX_REL and sd <= SCALE_MAX_REL, "error"))
    return gates


def replace_gates(gates: list[GateResult], new: list[GateResult]) -> list[GateResult]:
    """以同 id 替换并按 id 追加（保持 D2 门禁顺序，GLB 门禁接在其后）。"""
    by = {g.id: g for g in new}
    out = [by.pop(g.id, g) for g in gates]
    return out + [g for g in new if g.id in by]


def run_glb_qa(Vw: np.ndarray, F: np.ndarray, *, applied_g: Sequence[float], m_per_unit: float, anchor: Anchor,
               inputs: QaInputs) -> GlbQa:
    """世界帧网格上的完整标定 QA：两法调平、地面残差、水平面与控制点、尺度复测，返回门禁与报告段。"""
    C, N, area = face_geometry(np.asarray(Vw, np.float64), np.asarray(F))
    diag = float(np.hypot(*np.ptp(Vw[:, :2], axis=0)))
    km = diag >= KM_SCALE_DIAG_M
    gidx = ground_faces(C, N, area)
    water_xy = []
    planes_in: list[dict[str, Any]] = []
    if inputs.osm is not None:
        for p, xy in zip(inputs.osm.planes, rings_to_world([p["ring"] for p in inputs.osm.planes], anchor), strict=True):
            planes_in.append({"id": p["id"], "kind": p["kind"], "name": p["name"], "xy": xy})
            if p["kind"] == "water":
                water_xy.append(xy)
    planes_in += inputs.planes_extra
    dry = gidx
    for xy in water_xy:                                     # 水面不参与 DEM 回归（DEM 在水面无地形意义）
        lo, hi = xy.min(0), xy.max(0)
        Pg = C[dry, :2]
        bb = np.flatnonzero((Pg[:, 0] >= lo[0]) & (Pg[:, 0] <= hi[0]) & (Pg[:, 1] >= lo[1]) & (Pg[:, 1] <= hi[1]))
        if bb.size:
            wet = np.zeros(dry.size, bool)
            wet[bb[_inside(Pg[bb], xy)]] = True
            dry = dry[~wet]
    dem = dem_fit(C, dry, inputs.dem, anchor) if inputs.dem is not None and km else None
    cross = [(m, dem_fit(C, dry, d, anchor)) for m, d in inputs.dem_cross] if km else []      # 同一地面抽样（同种子），L5
    wall = wall_fit(N, area)
    if dem is not None:
        mad: float | None = dem.mad_m
    else:
        mad = ground_plane_mad(C[gidx], np.ones(gidx.size, bool)) if gidx.size else None
    planes = plane_slopes(C, N, area, planes_in, exclude=inputs.planes_exclude)
    cps = control_point_rms(ground_height_fn(Vw, F, gidx), inputs.control_points) if inputs.control_points and gidx.size else \
        control_point_rms(lambda EN: np.full(len(EN), np.nan), [])
    lev = levelling_report(applied_g=applied_g, km_scale=km, dem=dem, wall=wall, planes=planes, cps=cps, ground_mad_m=mad, dem_cross=cross)
    lev["horizontal_diag_m"] = round(diag, 1)
    scale: dict[str, Any] | None = None
    if inputs.osm is not None:
        fit = measure_scale(C, N, area, rings_to_world(inputs.osm.buildings, anchor))
        if fit is not None:
            meas = float(m_per_unit) * fit.rho
            ref = inputs.reference_m_per_unit
            scale = {"method": "osm-footprint-chamfer", "registered_m_per_unit": float(m_per_unit), "measured_m_per_unit": meas,
                     "reference_m_per_unit": ref, "reference": "r2 (M22 §8.1)" if ref is not None else None,
                     "rel_diff": None if ref is None else meas / float(ref) - 1.0, "fit": fit.to_json()}
    report = {"leveling": lev, "scale": scale, "inputs": inputs.files}
    return GlbQa(glb_gates(lev, scale), report, bool(lev["needs_control_points"]))


def calibrate(recipe: Any, reg: dict[str, Any], Vw: np.ndarray, F: np.ndarray, *, inputs: QaInputs | None = None) -> GlbQa:
    """构建流水线入口：由已接受的配准记录（`result.anchor`、`leveling.applied_g`、`m_per_unit`）与配方 `qa` 段运行 `run_glb_qa`。"""
    r = reg["result"]
    a = r["anchor"]
    h = a.get("h_msl_m")
    anchor = Anchor(kind="synthetic", datum="WGS84", lat_deg=float(a["lat_deg"]), lon_deg=float(a["lon_deg"]),
                    h_ellipsoid_m=0.0 if h is None else float(h), h_msl_m=None if h is None else float(h))
    inp = inputs if inputs is not None else QaInputs.from_recipe(recipe, anchor)
    return run_glb_qa(Vw, F, applied_g=r["leveling"]["applied_g"], m_per_unit=float(r["m_per_unit"]), anchor=anchor, inputs=inp)

