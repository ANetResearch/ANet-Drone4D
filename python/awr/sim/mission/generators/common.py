"""生成器公共部分（M10-FR-027；M10 §6.5.10 通用规则）。

生成器先产出几何，再对每个航段查询 Height_map（`heightmap_top_along`，已含 5×5 膨胀与 10 m 安全距离）抬升到安全
高度（x01 §3.8：全局固定巡航高度会撞楼），禁止使用全局固定巡航高度；段间高度变化在顶点处竖直过渡（竖直柱位于两段
共同端点上，两段巡航高度都不低于该点的 Height_map，按构造安全）。输出每机的作业项草稿与最小安全高度剖面
`{s_m[], z_path_m[], z_min_safe_m[]}`（UI 预览，14 §6.8）。转场段不在生成器中产生，由任务引擎在运行期按 §6.5.1 规划。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = ["GenContext", "GenError", "GenOutput", "ItemDraft", "VehicleCtx", "lift_polyline", "min_safe_profile",
           "resample_2d", "z_from"]


class GenError(ValueError):
    """生成失败：code 为 reasons.json 码（110 参数、125 规划），detail 为 JSON Pointer 或原因。"""

    def __init__(self, code: int, detail: str, remedy: str | None = None):
        super().__init__(f"{code} {detail}")
        self.code = int(code)
        self.detail = detail
        self.remedy = remedy


@dataclass
class VehicleCtx:
    vehicle_id: str
    home_enu_m: np.ndarray
    pos_enu_m: np.ndarray
    v_limit_mps: float = 12.0        # 限速配置的水平速度上限
    cruise_mps: float = 5.0          # 限速配置的巡航速度（缺省任务速度）
    r_col_m: float = 0.49
    priority_key: tuple = ()
    yawrate_max_rad_s: float = math.inf  # 自动模式偏航角速度上限（orbit 航向朝心时检查 v/R，ADR-062）


@dataclass
class GenContext:
    world: Any
    vehicles: list[VehicleCtx]
    constraints: dict = field(default_factory=dict)
    zones: list[str] | None = None
    camera: dict = field(default_factory=lambda: {"hfov_deg": 60.0, "vfov_deg": 42.1})
    mission_id: str = ""


@dataclass
class ItemDraft:
    kind: str                         # leg、orbit、dwell、transit
    primitive: str                    # follow_path、orbit、hover
    polyline: np.ndarray | None = None        # (k, 3) 折线（按 §6.5.5 圆角）
    dense: np.ndarray | None = None           # (k, 3) 已光滑的稠密曲线（解析螺旋），跳过圆角
    orbit: dict | None = None                 # {center_enu_m[3], radius_m, turns, cw, speed_mps, yaw}
    speed_mps: float = 5.0
    yaw: dict = field(default_factory=lambda: {"mode": "lookahead", "t_fwd_s": 1.0})
    gimbal: dict | None = None
    actions: list[dict] = field(default_factory=list)
    group: dict | None = None                 # 编队成员：{gid, slot_off_flu[3], heading_mode, tau_psi_s}
    meta: dict = field(default_factory=dict)

    @property
    def start(self) -> np.ndarray:
        if self.dense is not None:
            return np.asarray(self.dense[0], np.float64)
        if self.polyline is not None:
            return np.asarray(self.polyline[0], np.float64)
        o = self.orbit or {}
        c = np.asarray(o.get("center_enu_m", [0, 0, 0]), np.float64)
        th = float(o.get("theta0_rad", 0.0))
        return c + np.array([math.cos(th), math.sin(th), 0.0]) * float(o.get("radius_m", 0.0))

    @property
    def end(self) -> np.ndarray:
        if self.dense is not None:
            return np.asarray(self.dense[-1], np.float64)
        if self.polyline is not None:
            return np.asarray(self.polyline[-1], np.float64)
        return self.start


@dataclass
class GenOutput:
    items: dict[str, list[ItemDraft]]                     # vehicle_id → 作业项草稿
    region: list[list[float]] | None = None               # ENU 多边形 [[x, y]]
    formation: dict | None = None
    profiles: dict[str, dict] = field(default_factory=dict)   # vehicle_id → 最小安全高度剖面
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def z_from(params: dict, world: Any, xy: np.ndarray, default_agl: float | None = None) -> float:
    """`z_m`（world z）优先；否则 `agl_m` 相对 xy 处 DTM；都没有时用 default_agl。"""
    if params.get("z_m") is not None:
        return float(params["z_m"])
    agl = params.get("agl_m", default_agl)
    if agl is None:
        raise GenError(110, "/params/z_m")
    g = float(world.ground_dtm(np.asarray(xy, np.float64).reshape(1, 2))[0]) if world is not None else 0.0
    return g + float(agl)


def resample_2d(P: np.ndarray, ds: float) -> np.ndarray:
    P = np.asarray(P, np.float64)
    seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
    s = np.r_[0.0, np.cumsum(seg)]
    if s[-1] <= 0:
        return P[:1]
    n = max(2, math.ceil(s[-1] / ds) + 1)
    ss = np.linspace(0.0, s[-1], n)
    return np.stack([np.interp(ss, s, P[:, k]) for k in range(P.shape[1])], 1)


def _hm_top(world: Any, A: np.ndarray, B: np.ndarray) -> np.ndarray:
    if world is None:
        return np.full(len(A), -np.inf)
    try:
        return np.asarray(world.heightmap_top_along(A, B, exact=True), np.float64)
    except TypeError:
        return np.asarray(world.heightmap_top_along(A, B), np.float64)


LIFT_MARGIN_M = 1.0


def lift_polyline(P: np.ndarray, world: Any, *, z_floor: np.ndarray | float | None = None) -> np.ndarray:
    """逐段抬升：段 k 的巡航高度 z_k = max(段 k 两端给定高度, Height_map 沿段最大值 + LIFT_MARGIN_M)；顶点处竖直过渡。

    LIFT_MARGIN_M = 1.0 m 与圆角偏差上限 e_max 相同：顶点处的竖直台阶经圆角后向内切角至多 e_max，余量保证样条上
    没有低于 Height_map 的点（M10-AC-013）。P 为 (n, 3)（z 为期望高度）或 (n, 2)（配合 z_floor）。返回 3D 折线（去重）。"""
    P = np.asarray(P, np.float64)
    if P.shape[1] == 2:
        zf = np.broadcast_to(np.asarray(z_floor if z_floor is not None else 0.0, np.float64), (len(P),))
        P = np.c_[P, zf]
    if len(P) < 2:
        return P.copy()
    A, B = P[:-1, :2], P[1:, :2]
    top = _hm_top(world, A, B)
    zseg = np.maximum(np.maximum(P[:-1, 2], P[1:, 2]), top + LIFT_MARGIN_M)
    out = [np.r_[P[0, :2], zseg[0]]]
    for k in range(len(zseg)):
        out.append(np.r_[P[k + 1, :2], zseg[k]])
        if k + 1 < len(zseg) and abs(zseg[k + 1] - zseg[k]) > 1e-6:
            out.append(np.r_[P[k + 1, :2], zseg[k + 1]])
    Q = np.asarray(out)
    keep = np.r_[True, np.linalg.norm(np.diff(Q, axis=0), axis=1) > 1e-6]
    return Q[keep]


def min_safe_profile(P3: np.ndarray, world: Any, ds: float = 10.0, max_pts: int = 500) -> dict:
    """沿折线（水平弧长）采样：规划高度与最小安全高度（Height_map）。"""
    P3 = np.asarray(P3, np.float64)
    if world is None or len(P3) < 2:
        return {"s_m": [], "z_path_m": [], "z_min_safe_m": []}
    seg = np.hypot(np.diff(P3[:, 0]), np.diff(P3[:, 1]))
    s = np.r_[0.0, np.cumsum(seg)]
    total = float(s[-1])
    n = min(max_pts, max(2, int(total / ds) + 1))
    ss = np.linspace(0.0, total, n)
    x = np.interp(ss, s, P3[:, 0])
    y = np.interp(ss, s, P3[:, 1])
    z = np.interp(ss, s, P3[:, 2])
    try:
        hm = np.asarray(world.terrain_profile(np.c_[x, y], ds_m=max(1.0, min(50.0, total / max(n - 1, 1))))["hm_z_m"],
                        np.float64)
        hm = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(hm)), hm)
    except Exception:
        hm = np.full(n, np.nan)
    return {"s_m": [round(float(v), 1) for v in ss], "z_path_m": [round(float(v), 2) for v in z],
            "z_min_safe_m": [round(float(v), 2) for v in hm]}
