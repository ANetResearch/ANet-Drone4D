"""识别物几何：关键维度、敏感体（四种形状）的世界体、内外判定、禁入体、带符号距离与进入预测（M18-FR-004、FR-009、FR-016、
FR-022；M18-M19 PRD §6.2.2、§6.3.2、§6.3.3；AWR-04 §2.2、§7.3、§8.1、§10.6；17 §17.7.6 keepout_guard 伪代码；契约
`target/keepout.schema.json`）。

坐标：World ENU（m），角度 ψ_enu（东为 0、逆时针为正，rad）。敏感体配置的 z 范围相对识别物（脚点）；`Volume` 的 `z_min_m`、
`z_max_m` 为绝对 ENU z（柱与扇形的底与顶；球与半球的 `z_min_m` 为球心高度）。

形状判定（d = p − c，r_h 水平距离，r3 三维距离）：
- sphere：r3 ≤ R；hemisphere：r3 ≤ R 且 d.z ≥ 0；cylinder：r_h ≤ R 且 z_min ≤ p.z ≤ z_max；
- sector：cylinder 且 |wrap(atan2(d.y, d.x) − ψ − off)| ≤ half（`moving_as_circle` 时按整圆）。
判定用真实方向（运动识别物也按扇形）；规划禁入体与软围栏对运动识别物按整圆（AWR-04 §7.3 表"接力与规划的禁入体"列）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

__all__ = ["Volume", "body_volumes", "critical_dim", "inside", "inside_points", "is_moving", "predict_entry", "signed_distance",
           "wrap_pi"]

TWO_PI = 2.0 * math.pi


def wrap_pi(a: float | NDArray) -> float | NDArray:
    return (np.asarray(a) + math.pi) % TWO_PI - math.pi if isinstance(a, np.ndarray) else (float(a) + math.pi) % TWO_PI - math.pi


def critical_dim(size_m: NDArray, eps_rad: float | NDArray, beta_rad: float | NDArray, *, worst: bool = False,
                 override_m: float | NDArray | None = None, lwir_hot_fraction: float | NDArray | None = None) -> float | NDArray:
    """关键维度 `d_c = √(|sin ε|·l·w + |cos ε|·(|cos β|·w·h + |sin β|·l·h))`（AWR-04 §2.2，M18-FR-004）。

    ε：自识别物看观察者的俯视角（观察者高于识别物为正）；β：观察者方位相对识别物航向的夹角。`worst = True`（规划与运动目标）
    时侧项取 `min(w·h, l·h)`；`override_m` 非空时直接返回该值（`critical_dim_m`）；`lwir_hot_fraction` 给出时再乘
    `√max(hot_fraction, 0.25)`（LWIR 关键维度）。"""
    if override_m is not None:
        out = np.asarray(override_m, float)
    else:
        if isinstance(size_m, dict):
            size_m = [size_m["l"], size_m["w"], size_m["h"]]
        s = np.asarray(size_m, float)
        ln, w, h = s[..., 0], s[..., 1], s[..., 2]
        e = np.asarray(eps_rad, float)
        b = np.asarray(beta_rad, float)
        side = np.minimum(w * h, ln * h) if worst else np.abs(np.cos(b)) * w * h + np.abs(np.sin(b)) * ln * h
        out = np.sqrt(np.abs(np.sin(e)) * ln * w + np.abs(np.cos(e)) * side)
    if lwir_hot_fraction is not None:
        out = out * np.sqrt(np.maximum(np.asarray(lwir_hot_fraction, float), 0.25))
    return float(out) if np.ndim(out) == 0 else out


@dataclass(frozen=True)
class Volume:
    """世界坐标中的敏感体或禁入体（`target/keepout.schema.json` 字段；与 `tools/fake/targets_stub.Volume` 同形）。"""

    shape: str                       # sphere, hemisphere, cylinder, sector
    center: tuple[float, float, float]
    r_m: float
    z_min_m: float
    z_max_m: float
    yaw_rad: float = 0.0             # 扇形轴（ψ_enu）
    half_angle_rad: float = math.pi
    vol_idx: int = 0
    kind: str = "visual"

    def to_wire(self, vid: str) -> dict:
        """KeepoutVolume（id 形如 `<target id>/<vol_idx>`）。"""
        d = {"id": vid, "shape": self.shape, "center_enu_m": [float(x) for x in self.center], "r_m": float(self.r_m),
             "z_min_m": float(self.z_min_m), "z_max_m": float(self.z_max_m),
             "source": {"kind": "target", "id": vid.split("/")[0], "vol_idx": int(self.vol_idx)}}
        if self.shape == "sector":
            d.update(yaw_rad=float(wrap_pi(self.yaw_rad)), half_angle_rad=float(min(math.pi, self.half_angle_rad)))
        return d


def is_moving(motion: dict | None, speed_mps: float) -> bool:
    """运动识别物：`motion.mode ≠ static` 或当前水平速度 > 0.1 m/s（M18-M19 PRD §6.3.3）。"""
    return bool((motion or {}).get("mode", "static") != "static" or speed_mps > 0.1)


def body_volumes(bodies: Sequence[dict], pos: Sequence[float], yaw_rad: float, *, margin_m: float = 0.0, moving_as_circle: bool = False,
                 kinds: tuple[str, ...] = ("visual", "acoustic"), radius_of: Sequence[float | None] | None = None,
                 enabled_only: bool = True) -> list[Volume]:
    """配置的敏感体 → 世界体（外扩 `margin_m`；`radius_of[i]` 给出时替换第 i 个体的半径，physical 有效体）。"""
    x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
    out: list[Volume] = []
    for i, b in enumerate(bodies):
        if (enabled_only and not b.get("enabled", True)) or b.get("kind") not in kinds:
            continue
        shape = b["shape"]
        r = float(b["radius_max_m"]) if radius_of is None or radius_of[i] is None else float(radius_of[i])  # type: ignore[arg-type]
        half = math.radians(float(b.get("half_angle_deg", 60.0)))
        if shape == "sector" and (moving_as_circle or half >= math.pi - 1e-9):
            shape = "cylinder"
        rr = r + margin_m
        if shape == "sector" and margin_m > 0.0:
            half = min(math.pi, half + math.asin(min(1.0, margin_m / max(rr, 1e-6))))
        if shape in ("sphere", "hemisphere"):
            zmin, zmax = z, z + rr
        else:
            zmin, zmax = z + float(b.get("z_min_m", 0.0)) - margin_m, z + float(b.get("z_max_m", 300.0)) + margin_m
        out.append(Volume(shape, (x, y, z), rr, zmin, zmax, float(wrap_pi(yaw_rad + math.radians(float(b.get("azimuth_offset_deg", 0.0))))),
                          half, i, str(b["kind"])))
    return out


def inside_points(v: Volume, P: NDArray) -> NDArray[np.bool_]:
    """点集 P[n, 3] 是否在体内（向量化）。"""
    P = np.asarray(P, float).reshape(-1, 3)
    d = P - np.asarray(v.center, float)
    if v.shape in ("sphere", "hemisphere"):
        ok = np.einsum("ij,ij->i", d, d) <= v.r_m * v.r_m
        return ok & (d[:, 2] >= 0.0) if v.shape == "hemisphere" else ok
    rh2 = d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1]
    ok = (rh2 <= v.r_m * v.r_m) & (P[:, 2] >= v.z_min_m) & (P[:, 2] <= v.z_max_m)
    if v.shape == "sector" and v.half_angle_rad < math.pi:
        a = np.abs((np.arctan2(d[:, 1], d[:, 0]) - v.yaw_rad + math.pi) % TWO_PI - math.pi)
        ok &= (a <= v.half_angle_rad) | (rh2 < 1e-12)
    return ok


def inside(v: Volume, p: Sequence[float]) -> bool:
    return bool(inside_points(v, np.asarray(p, float).reshape(1, 3))[0])


def _closest_sector_2d(q: NDArray, r: float, yaw: float, half: float) -> tuple[NDArray, bool]:
    """二维扇形（圆心在原点）内离 q 最近的点与 q 是否在扇形内。"""
    rh = math.hypot(q[0], q[1])
    full = half >= math.pi - 1e-9
    ang = math.atan2(q[1], q[0])
    in_ang = full or abs(wrap_pi(ang - yaw)) <= half
    if in_ang:
        if rh <= r:
            return q.copy(), True
        return q * (r / rh), False
    best, bd = None, math.inf
    for s in (-1.0, 1.0):
        u = np.array([math.cos(yaw + s * half), math.sin(yaw + s * half)])
        t = min(max(float(q @ u), 0.0), r)
        c = u * t
        dd = float(np.hypot(*(q - c)))
        if dd < bd:
            best, bd = c, dd
    return best, False  # type: ignore[return-value]


def signed_distance(v: Volume, p: Sequence[float]) -> tuple[float, NDArray]:
    """点到体的带符号距离（体外为正）与离开方向的单位法向（体外：由最近点指向 p；体内：近似向外）。球与半球按三维，
    柱与扇形按水平与高度合成（17 §17.7.6 keepout_guard）。"""
    p = np.asarray(p, float)
    c = np.asarray(v.center, float)
    d = p - c
    if v.shape in ("sphere", "hemisphere"):
        r3 = float(np.linalg.norm(d))
        if v.shape == "hemisphere" and d[2] < 0.0:
            rh = math.hypot(d[0], d[1])
            if rh <= v.r_m:
                return float(-d[2]), np.array([0.0, 0.0, -1.0])
            q = np.array([d[0] / rh * v.r_m, d[1] / rh * v.r_m, 0.0])
            w = d - q
            n = float(np.linalg.norm(w))
            return n, w / max(n, 1e-9)
        n = d / r3 if r3 > 1e-9 else np.array([0.0, 0.0, 1.0])
        return r3 - v.r_m, n
    half = v.half_angle_rad if v.shape == "sector" else math.pi
    q2, in2 = _closest_sector_2d(d[:2], v.r_m, v.yaw_rad, half)
    if in2:
        rh = math.hypot(d[0], d[1])
        dh = rh - v.r_m
        nh = np.array([d[0], d[1], 0.0]) / rh if rh > 1e-9 else np.array([1.0, 0.0, 0.0])
    else:
        w = d[:2] - q2
        dh = float(np.hypot(*w))
        nh = np.array([w[0], w[1], 0.0]) / max(dh, 1e-9)
    if p[2] > v.z_max_m:
        dv, nv = p[2] - v.z_max_m, np.array([0.0, 0.0, 1.0])
    elif p[2] < v.z_min_m:
        dv, nv = v.z_min_m - p[2], np.array([0.0, 0.0, -1.0])
    else:
        dv, nv = -min(p[2] - v.z_min_m, v.z_max_m - p[2]), np.array([0.0, 0.0, 1.0 if p[2] - v.z_min_m > v.z_max_m - p[2] else -1.0])
    if dh > 0.0 and dv > 0.0:
        n = np.array([nh[0] * dh, nh[1] * dh, nv[2] * dv])
        nn = float(np.linalg.norm(n))
        return nn, n / nn
    if dh >= dv:
        return dh, nh
    return dv, nv


def predict_entry(p: NDArray, v: NDArray, vols: Sequence[Volume], horizon_s: float = 3.0, step_s: float = 0.1) -> float | None:
    """沿直线 p + v·t 在 horizon_s 内首次进入任一体的时刻（s，步长 0.1 s；已在体内为 0），否则 None（M18-FR-022 前端同式）。"""
    p, v = np.asarray(p, float), np.asarray(v, float)
    n = round(horizon_s / step_s) + 1
    ts = np.arange(n) * step_s
    Q = p[None, :] + v[None, :] * ts[:, None]
    first = None
    for vol in vols:
        hit = np.flatnonzero(inside_points(vol, Q))
        if hit.size and (first is None or hit[0] < first):
            first = int(hit[0])
    return None if first is None else float(ts[first])
