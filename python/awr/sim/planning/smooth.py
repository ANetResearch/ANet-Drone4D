"""轨迹生成流水线（M10-FR-032、FR-033；M10 §6.5.5–§6.5.7；r25 §3.4、§3.5）。

    折线圆角（偏差 ≤ e_max = 1.0 m）→ 1 m 重采样 → TOPP-lite（v_max、a_tan、a_lat、vz_up、vz_dn）→ 按 ts = 0.5 s
    时间等间隔采样得控制点（Schoenberg，首尾补齐为静止）→ 按控制点导数凸包均匀拉伸时间 → 校验
    校验：样条按 0.1 s 采样；距起终点水平 > 4 m 的样点以 M04 `path_valid(buffer_m = 1.0)` 校验（巡航段）；全部样点
          `z ≥ column_max_within(xy, r_col) + 0.5 m`（端点柱规则）
    失败：圆角偏差减半重试；再退化为"折线 + 路口停顿"（按构造安全），metrics 标注 degraded

`e_max = 1.0 m` 小于 Height_map 的 4 m 水平膨胀，圆角不会切进障碍。numba 可用时 TOPP 前后向两遍为 njit 核
（`fastmath = False`），否则为等价 numpy/Python 实现（逐位相同的循环）。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import bspline as BS

__all__ = ["DEFAULT_LIMITS", "Limits", "TrajResult", "decimate", "fillet", "limits_from", "make_trajectory",
           "make_trajectory_dense", "polyline_stop_at_corners", "topp_lite", "validate", "validate_polyline", "warmup"]

E_MAX_M = 1.0
FRAC = 0.45
DS_M = 1.0
TOL = 1.05
R_COL_DEFAULT_M = 0.49
END_CLEAR_M = 0.5
FAR_XY_M = 4.0

try:  # numba 只允许出现在 awr/sim/planning 与 sim/fleet/kernels_*（TECH-FR-004）
    from numba import njit as _njit

    HAVE_NUMBA = True
except Exception:  # pragma: no cover - numba 缺失时退回 Python 循环
    HAVE_NUMBA = False

    def _njit(*a, **k):
        def deco(f):
            return f
        return deco if not (a and callable(a[0])) else a[0]


@dataclass(frozen=True)
class Limits:
    v_max_mps: float = 5.0        # 本条轨迹的巡航速度上限（已按限速配置截断）
    a_tan_mps2: float = 2.0
    a_lat_mps2: float = 3.0
    vz_up_mps: float = 3.0
    vz_dn_mps: float = 1.5
    a_max_mps2: float = 3.0       # 拉伸时的加速度上限（MPC_ACC_HOR）

    def to_json(self) -> dict:
        return {k: float(v) for k, v in self.__dict__.items()}


DEFAULT_LIMITS = Limits()


def limits_from(d: dict | Limits | None, speed_mps: float | None = None) -> Limits:
    """dict（`Limits` 字段名）或 Limits；给出 speed_mps 时巡航速度取 min(speed, v_max_mps)（v_max_mps 为限速配置上限）。"""
    from dataclasses import replace

    if isinstance(d, Limits):
        base = d
    else:
        dd = dict(d or {})
        base = Limits(**{k: float(dd[k]) for k in Limits.__dataclass_fields__ if k in dd})
    if speed_mps is not None and speed_mps > 0:
        base = replace(base, v_max_mps=min(float(speed_mps), base.v_max_mps))
    return base


# ------------------------------------------------------------------------------------------------ 圆角
def _dedupe(P: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    P = np.asarray(P, np.float64)
    if len(P) < 2:
        return P
    keep = np.r_[True, np.linalg.norm(np.diff(P, axis=0), axis=1) > eps]
    return P[keep]


def fillet(P: np.ndarray, e_max_m: float = E_MAX_M, frac: float = FRAC, ds_m: float = DS_M) -> tuple[np.ndarray, np.ndarray]:
    """每个内角用与两边相切的圆弧替换：R = e_max/(1/cos(φ/2) − 1)，切线长 l = R·tan(φ/2) ≤ frac·min(L_prev, L_next)；
    在两段所在平面内画弧，最后按 ds 重采样。返回 (P_resampled (k, 3), s (k,))。"""
    P = _dedupe(P)
    if len(P) < 2:
        return np.repeat(P[:1], 2, axis=0), np.array([0.0, 0.0])
    out = [P[0]]
    for i in range(1, len(P) - 1):
        a, b, c = P[i - 1], P[i], P[i + 1]
        u = b - a
        w = c - b
        Lu = float(np.linalg.norm(u))
        Lw = float(np.linalg.norm(w))
        if Lu < 1e-9 or Lw < 1e-9:
            continue
        u = u / Lu
        w = w / Lw
        phi = math.acos(float(np.clip(u @ w, -1.0, 1.0)))
        if phi < 1e-3:
            out.append(b)
            continue
        half = phi / 2.0
        if half >= math.pi / 2.0 - 1e-6:        # 近乎折返：不做圆角，停在顶点
            out.append(b)
            continue
        denom = 1.0 / math.cos(half) - 1.0
        R = e_max_m / max(denom, 1e-12)
        ll = R * math.tan(half)
        lmax = frac * min(Lu, Lw)
        if ll > lmax:
            ll = lmax
            R = ll / math.tan(half)
        p0 = b - u * ll
        p1 = b + w * ll
        bis = w - u
        nb = float(np.linalg.norm(bis))
        if nb < 1e-9:
            out.append(b)
            continue
        ctr = b + bis / nb * (R / math.cos(half))
        r0 = p0 - ctr
        r1 = p1 - ctr
        n = max(2, math.ceil(R * phi / ds_m))
        sphi = math.sin(phi)
        for t in np.linspace(0.0, 1.0, n):
            ang = phi * t
            out.append(ctr + (math.sin(phi - ang) * r0 + math.sin(ang) * r1) / sphi)
    out.append(P[-1])
    Q = _dedupe(np.asarray(out))
    seg = np.linalg.norm(np.diff(Q, axis=0), axis=1)
    s = np.r_[0.0, np.cumsum(seg)]
    total = float(s[-1])
    n = max(2, math.ceil(total / ds_m) + 1)
    ss = np.linspace(0.0, total, n)
    R3 = np.c_[np.interp(ss, s, Q[:, 0]), np.interp(ss, s, Q[:, 1]), np.interp(ss, s, Q[:, 2])]
    # 保留原折线的顶点（未做圆角的尖点、停顿点）：插值会把它们抹掉，偏差 ≤ ds/2 已在 e_max 预算内
    return R3, ss


# ------------------------------------------------------------------------------------------------ TOPP-lite
@_njit(cache=True, fastmath=False)
def _topp_passes(vlim: np.ndarray, seg: np.ndarray, a_tan: float) -> np.ndarray:
    v = vlim.copy()
    n = v.shape[0]
    for i in range(1, n):
        c = math.sqrt(v[i - 1] * v[i - 1] + 2.0 * a_tan * seg[i - 1])
        if c < v[i]:
            v[i] = c
    for i in range(n - 2, -1, -1):
        c = math.sqrt(v[i + 1] * v[i + 1] + 2.0 * a_tan * seg[i])
        if c < v[i]:
            v[i] = c
    return v


def topp_lite(P: np.ndarray, s: np.ndarray, v_max: float, a_tan: float, a_lat: float, vz_up: float, vz_dn: float,
              v0: float = 0.0, v1: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """返回 (v (k,), t (k,))：κ 限速、竖直分量限速、前向与后向加速度约束（r25 §3.5）。"""
    P = np.asarray(P, np.float64)
    s = np.asarray(s, np.float64)
    if len(P) < 2 or s[-1] <= 0:
        return np.zeros(len(P)), np.zeros(len(P))
    d1 = np.gradient(P, s, axis=0)
    d2 = np.gradient(d1, s, axis=0)
    k = np.linalg.norm(np.cross(d1, d2), axis=1) / np.maximum(np.linalg.norm(d1, axis=1) ** 3, 1e-9)
    vlim = np.minimum(v_max, np.sqrt(a_lat / np.maximum(k, 1e-9)))
    dz = d1[:, 2] / np.maximum(np.linalg.norm(d1, axis=1), 1e-9)
    vlim = np.where(dz > 1e-6, np.minimum(vlim, vz_up / np.maximum(dz, 1e-9)), vlim)
    vlim = np.where(dz < -1e-6, np.minimum(vlim, vz_dn / np.maximum(-dz, 1e-9)), vlim)
    vlim[0] = min(vlim[0], v0)
    vlim[-1] = min(vlim[-1], v1)
    seg = np.diff(s)
    v = _topp_passes(np.ascontiguousarray(vlim), np.ascontiguousarray(seg), float(a_tan))
    vm = 0.5 * (v[1:] + v[:-1])
    # 静止起止段：vm → 0 时以匀加速的精确时间 2·Δs/(v_a + v_b) 为界
    t = np.r_[0.0, np.cumsum(seg / np.maximum(vm, 1e-3))]
    return v, t


# ------------------------------------------------------------------------------------------------ B-spline 生成
def _bspline_from_samples(P: np.ndarray, s: np.ndarray, t: np.ndarray, ts: float, lim: Limits,
                          tol: float = TOL) -> tuple[np.ndarray, float, float, tuple]:
    T = float(t[-1])
    tk = np.arange(0.0, T + 1e-9, ts)
    if tk[-1] < T - 1e-9:
        tk = np.r_[tk, T]
    sk = np.interp(tk, t, s)
    pk = np.c_[np.interp(sk, s, P[:, 0]), np.interp(sk, s, P[:, 1]), np.interp(sk, s, P[:, 2])]
    Q = np.vstack([pk[:1], pk[:1], pk, pk[-1:], pk[-1:]])
    V = np.diff(Q, axis=0) / ts
    A = np.diff(Q, 2, axis=0) / ts ** 2
    vxy = float(np.linalg.norm(V[:, :2], axis=1).max())
    vzu = max(float(V[:, 2].max()), 0.0)
    vzd = max(float(-V[:, 2].min()), 0.0)
    ra = math.sqrt(float(np.linalg.norm(A, axis=1).max()) / lim.a_max_mps2)
    parts = (vxy / (lim.v_max_mps * tol), vzu / (lim.vz_up_mps * tol), vzd / (lim.vz_dn_mps * tol), ra / math.sqrt(tol))
    ratio = max(1.0, *parts)
    return Q, ts * ratio, ratio, parts


# ------------------------------------------------------------------------------------------------ 校验
def decimate(S: np.ndarray, step_m: float = 1.0) -> np.ndarray:
    S = np.asarray(S, np.float64)
    if len(S) < 2:
        return S
    keep = [0]
    acc = 0.0
    d = np.linalg.norm(np.diff(S, axis=0), axis=1)
    for i in range(1, len(S)):
        acc += float(d[i - 1])
        if acc >= step_m:
            keep.append(i)
            acc = 0.0
    if keep[-1] != len(S) - 1:
        keep.append(len(S) - 1)
    return S[np.asarray(keep)]


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    d = np.diff(np.r_[0, mask.astype(np.int8), 0])
    return list(zip(np.flatnonzero(d == 1).tolist(), (np.flatnonzero(d == -1) - 1).tolist(), strict=True))


def _path_valid_chunked(world: Any, P: np.ndarray, zones: list[str] | None, buffer_m: float = 1.0,
                        endpoint_radius_m: float | None = None) -> tuple[bool, str | None, np.ndarray | None]:
    """M04 `path_valid` 单次 ≤ 1000 顶点、≤ 20 km：按 999 段一块、相邻块共享端点切分。"""
    n = len(P)
    if n < 2:
        return True, None, None
    i = 0
    while i < n - 1:
        j = min(n - 1, i + 999)
        blk = P[i:j + 1]
        while float(np.linalg.norm(np.diff(blk, axis=0), axis=1).sum()) > 19_000.0 and len(blk) > 2:
            j = i + max(1, (j - i) // 2)
            blk = P[i:j + 1]
        er = endpoint_radius_m if (i == 0 or j == n - 1) else None
        r = world.path_valid(blk, buffer_m=buffer_m, active_zone_ids=zones, endpoint_radius_m=er,
                             endpoint_clear_m=END_CLEAR_M)
        if not r.ok:
            return False, r.reason, None if r.point_enu_m is None else np.asarray(r.point_enu_m)
        i = j
    return True, None, None


def validate(Q: np.ndarray, ts: float, world: Any, A: np.ndarray, B: np.ndarray, r_col_m: float = R_COL_DEFAULT_M,
             zones: list[str] | None = None, dt_s: float = 0.1) -> tuple[bool, dict]:
    """§6.5.7：巡航段 path_valid(buffer 1 m) + 全程端点柱规则。world 为 None 时只做数值检查。"""
    T = BS.duration(Q, ts)
    tq = np.arange(0.0, T + 1e-9, dt_s)
    S = BS.eval_bspline(Q, ts, tq)
    info: dict = {"n_samples": len(S)}
    if not np.all(np.isfinite(S)):
        return False, info | {"reason": "NAN"}
    if world is None:
        return True, info
    A = np.asarray(A, np.float64)
    B = np.asarray(B, np.float64)
    far = (np.hypot(S[:, 0] - A[0], S[:, 1] - A[1]) > FAR_XY_M) & (np.hypot(S[:, 0] - B[0], S[:, 1] - B[1]) > FAR_XY_M)
    for i0, i1 in _runs(far):
        run = decimate(S[i0:i1 + 1], 1.0)
        if len(run) < 2:
            continue
        ok, reason, pt = _path_valid_chunked(world, run, zones)
        if not ok:
            return False, info | {"reason": reason or "PATH_OBSTACLE", "point_enu_m": None if pt is None else pt.tolist()}
    top = np.asarray(world.column_max_within(S[:, :2], r_col_m), np.float64)
    clr = S[:, 2] - top
    info["min_end_clear_m"] = round(float(clr.min()), 3)
    if float(clr.min()) < END_CLEAR_M - 1e-6:
        k = int(np.argmin(clr))
        return False, info | {"reason": "PATH_OBSTACLE", "point_enu_m": S[k].tolist(), "rule": "ENDPOINT_COLUMN"}
    try:
        dsm = np.asarray(world.height_dsm(S[:, :2]), np.float64)
        info["min_clear_dsm_m"] = round(float((S[:, 2] - dsm).min()), 3)
    except Exception:
        pass
    return True, info


def validate_polyline(P3: np.ndarray, world: Any, r_col_m: float = R_COL_DEFAULT_M,
                      zones: list[str] | None = None) -> tuple[bool, dict]:
    """输入折线本身的细校验（follow_path 的航点；§6.5.7 端点柱规则对应 M04 `endpoint_radius_m = r_col`）。"""
    if world is None:
        return True, {}
    P = _dedupe(np.asarray(P3, np.float64))
    if len(P) < 2:
        return True, {}
    dense = []
    for a, b in itertools.pairwise(P):
        L = float(np.linalg.norm(b - a))
        n = max(1, math.ceil(L / 500.0))
        dense.extend(a + (b - a) * (k / n) for k in range(n))
    dense.append(P[-1])
    ok, reason, pt = _path_valid_chunked(world, np.asarray(dense), zones, endpoint_radius_m=r_col_m)
    if not ok:
        return False, {"reason": reason or "PATH_OBSTACLE", "point_enu_m": None if pt is None else pt.tolist()}
    goal_top = float(world.column_max_within(P[-1:, :2], r_col_m)[0])
    # 目标点规则：z_goal ≥ column_max_within(goal, r_col) + END_CLEAR_M（沿用 GOAL_IN_OBSTACLE）
    if P[-1, 2] < goal_top + END_CLEAR_M and float(np.hypot(*(P[-1, :2] - P[-2, :2]))) > 0.01:
        return False, {"reason": "GOAL_IN_OBSTACLE", "point_enu_m": P[-1].tolist()}
    return True, {}


# ------------------------------------------------------------------------------------------------ 轨迹
@dataclass
class TrajResult:
    status: str                    # ok、degraded、infeasible、error
    Q: np.ndarray | None = None
    ts_s: float = BS.TS_DEFAULT
    stretch_ratio: float = 1.0
    degraded: bool = False
    detail: str | None = None
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "degraded")

    @property
    def duration_s(self) -> float:
        return BS.duration(self.Q, self.ts_s) if self.Q is not None else 0.0


def _dense_to_traj(Pf: np.ndarray, s: np.ndarray, lim: Limits, ts: float, v0: float = 0.0,
                   v1: float = 0.0) -> tuple[np.ndarray, float, float, dict]:
    _v, t = topp_lite(Pf, s, lim.v_max_mps, lim.a_tan_mps2, lim.a_lat_mps2, lim.vz_up_mps, lim.vz_dn_mps, v0, v1)
    Q, ts2, ratio, parts = _bspline_from_samples(Pf, s, t, ts, lim)
    return Q, ts2, ratio, {"T_topp_s": round(float(t[-1]), 3), "ratio_parts": [round(float(x), 4) for x in parts],
                           "len_m": round(float(s[-1]), 2)}


def polyline_stop_at_corners(P3: np.ndarray, lim: Limits, ts: float = BS.TS_DEFAULT) -> tuple[np.ndarray, float, float]:
    """退化轨迹：不做圆角，在每个顶点停住（折线按构造安全）；返回 (Q, ts, ratio)。"""
    P = _dedupe(np.asarray(P3, np.float64))
    pts, ss = [P[0]], [0.0]
    for a, b in itertools.pairwise(P):
        L = float(np.linalg.norm(b - a))
        n = max(2, math.ceil(L / DS_M) + 1)
        for f in np.linspace(0.0, 1.0, n)[1:]:
            pts.append(a + (b - a) * f)
            ss.append(ss[-1] + L / (n - 1))
    Pd = np.asarray(pts)
    s = np.asarray(ss)
    # 顶点处限速为 0：在每个原顶点把 vlim 置零（TOPP 前后向两遍给出梯形速度剖面）
    v_all, _t = topp_lite(Pd, s, lim.v_max_mps, lim.a_tan_mps2, 1e9, lim.vz_up_mps, lim.vz_dn_mps)
    cum = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(P, axis=0), axis=1))]
    vlim = v_all.copy()
    for c in cum:
        vlim[int(np.argmin(np.abs(s - c)))] = 0.0
    v = _topp_passes(np.ascontiguousarray(vlim), np.ascontiguousarray(np.diff(s)), float(lim.a_tan_mps2))
    vm = 0.5 * (v[1:] + v[:-1])
    t = np.r_[0.0, np.cumsum(np.diff(s) / np.maximum(vm, 1e-3))]
    Q, ts2, ratio, _parts = _bspline_from_samples(Pd, s, t, ts, lim)
    return Q, ts2, ratio


def make_trajectory(P3: np.ndarray, lim: Limits, world: Any = None, *, r_col_m: float = R_COL_DEFAULT_M,
                    zones: list[str] | None = None, ts: float = BS.TS_DEFAULT, check_input: bool = True) -> TrajResult:
    """折线 → 轨迹（§6.5.7 make_trajectory）：输入折线细校验 → 圆角 e_max 1.0、0.5 → 退化为路口停顿。"""
    P = _dedupe(np.asarray(P3, np.float64))
    if len(P) < 2:
        if len(P) == 1:
            P = np.vstack([P, P])
        else:
            return TrajResult("error", detail="EMPTY")
        Q = np.repeat(P[:1], 4, axis=0)
        return TrajResult("ok", Q, ts, 1.0, stats={"len_m": 0.0})
    if check_input and world is not None:
        ok, info = validate_polyline(P, world, r_col_m, zones)
        if not ok:
            return TrajResult("infeasible", detail=info.get("reason", "PATH_OBSTACLE"), stats=info)
    stats: dict = {}
    for e_max in (E_MAX_M, E_MAX_M / 2.0):
        Pf, s = fillet(P, e_max)
        Q, ts2, ratio, st = _dense_to_traj(Pf, s, lim, ts)
        ok, info = validate(Q, ts2, world, P[0], P[-1], r_col_m, zones)
        stats = st | info | {"e_max_m": e_max}
        if ok:
            return TrajResult("ok", Q, ts2, ratio, False, None, stats)
    Q, ts2, ratio = polyline_stop_at_corners(P, lim, ts)
    ok, info = validate(Q, ts2, world, P[0], P[-1], r_col_m, zones)
    return TrajResult("degraded", Q, ts2, ratio, True, None if ok else info.get("reason"),
                      stats | info | {"degraded_reason": stats.get("reason")})


def make_trajectory_dense(Pd: np.ndarray, lim: Limits, world: Any = None, *, r_col_m: float = R_COL_DEFAULT_M,
                          zones: list[str] | None = None, ts: float = BS.TS_DEFAULT, check: bool = True) -> TrajResult:
    """已光滑的稠密曲线（解析螺旋、圆弧等，点距 ≈ 1 m）→ 轨迹：跳过圆角，直接 TOPP-lite 与 Schoenberg。"""
    P = _dedupe(np.asarray(Pd, np.float64))
    s = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(P, axis=0), axis=1))]
    Q, ts2, ratio, st = _dense_to_traj(P, s, lim, ts)
    if not check or world is None:
        return TrajResult("ok", Q, ts2, ratio, stats=st)
    ok, info = validate(Q, ts2, world, P[0], P[-1], r_col_m, zones)
    if ok:
        return TrajResult("ok", Q, ts2, ratio, stats=st | info)
    return TrajResult("infeasible", Q, ts2, ratio, detail=info.get("reason", "PATH_OBSTACLE"), stats=st | info)


def warmup() -> float:
    """触发 TOPP-lite 的 JIT（或读取缓存）；返回耗时（秒，墙钟只用于日志）。"""
    import time

    t0 = time.perf_counter()
    make_trajectory(np.array([[0.0, 0.0, 10.0], [10.0, 0.0, 10.0], [10.0, 10.0, 12.0]]), Limits(), None)
    return time.perf_counter() - t0
