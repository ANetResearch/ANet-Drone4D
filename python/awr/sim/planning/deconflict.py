"""任务内 4D 冲突检查与消解（M10-FR-056；M10 §6.5.13；r25 §3.9(1)；D1-ext）。

    对同一任务的全部轨迹按 dt = 0.1 s 采样，椭球距离 rho = ‖(Δx, Δy, z_scale·Δz)‖（z_scale = 0.5），阈值 clearance = min_sep_m
    高优先级先占；对优先级较低者依次尝试 (delay, dz) 候选，排序键 delay + 0.5·|dz|；dz ≠ 0 时须通过净空复核（validate）
    消解不了：记入 partial（emit deconflict.partial），轨迹按原样占位，交由 FleetGuard 兜底

时间对齐：各轨迹以同一时刻 t = 0 为起点（任务在首个作业项前做一次同步，engine `_advance`）；延迟 d 期间停在起点，
结束后停在终点（保守：视为悬停）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from . import bspline as BS

__all__ = ["DELAYS_S", "DZS_M", "DeconflictResult", "candidates", "deconflict_mission", "min_ellipsoid_dist",
           "sample_traj"]

DELAYS_S = tuple(range(0, 31, 2))
DZS_M = (0.0, 4.0, 8.0, -4.0)


@dataclass
class DeconflictResult:
    delays_s: list[float]
    layers_m: list[float]
    residual: list[tuple[int, int, float]] = field(default_factory=list)    # (i, j, rho_min) 仍小于阈值的对
    partial: list[int] = field(default_factory=list)
    checks: int = 0

    @property
    def ok(self) -> bool:
        return not self.partial

    def to_json(self, ids: Sequence[str] | None = None) -> dict:
        name = (lambda i: ids[i]) if ids is not None else (lambda i: i)
        return {"delays_s": {str(name(i)): float(d) for i, d in enumerate(self.delays_s)},
                "layers_m": {str(name(i)): float(z) for i, z in enumerate(self.layers_m)},
                "residual": [[str(name(i)), str(name(j)), round(float(r), 3)] for i, j, r in self.residual],
                "partial": [str(name(i)) for i in self.partial], "checks": int(self.checks)}


def sample_traj(Q: np.ndarray, ts_s: float, dt_s: float = 0.1) -> np.ndarray:
    Q = np.asarray(Q, np.float64)
    T = BS.duration(Q, ts_s)
    t = np.arange(0.0, T + 1e-9, dt_s)
    if t[-1] < T - 1e-9:
        t = np.r_[t, T]
    return BS.eval_bspline(Q, ts_s, t)


def candidates(delays: Sequence[float] = DELAYS_S, dzs: Sequence[float] = DZS_M) -> list[tuple[float, float]]:
    return sorted(((float(d), float(z)) for d in delays for z in dzs), key=lambda c: (c[0] + 0.5 * abs(c[1]), c[0]))


def _shifted(P: np.ndarray, delay_n: int, dz: float, n_total: int) -> np.ndarray:
    out = np.empty((n_total, 3))
    d = min(delay_n, n_total)
    out[:d] = P[0]
    m = max(0, min(len(P), n_total - d))
    out[d:d + m] = P[:m]
    out[d + m:] = P[-1]
    out[:, 2] += dz
    return out


def min_ellipsoid_dist(A: np.ndarray, B: np.ndarray, z_scale: float = 0.5) -> float:
    d = A - B
    return float(np.sqrt(d[:, 0] ** 2 + d[:, 1] ** 2 + (z_scale * d[:, 2]) ** 2).min())


def _bbox(P: np.ndarray) -> np.ndarray:
    return np.r_[P.min(0), P.max(0)]


def _bbox_far(a: np.ndarray, b: np.ndarray, clearance: float, z_scale: float) -> bool:
    gap = np.maximum(0.0, np.maximum(a[:3] - b[3:], b[:3] - a[3:]))
    gap[2] *= z_scale
    return float(np.linalg.norm(gap)) >= clearance


def deconflict_mission(samples: Sequence[np.ndarray], prio: Sequence[float], *, clearance_m: float = 10.0,
                       dt_s: float = 0.1, z_scale: float = 0.5, delays: Sequence[float] = DELAYS_S,
                       dzs: Sequence[float] = DZS_M,
                       validate: Callable[[int, float], bool] | None = None) -> DeconflictResult:
    """`samples[k]`：第 k 条轨迹按 dt 采样的位置（n_k×3，t = 0 起）；`prio` 越大越先占。

    `validate(k, dz)`：dz ≠ 0 时对第 k 条轨迹整体平移 dz 后的净空复核（None 表示不复核）。"""
    K = len(samples)
    Ps = [np.asarray(P, np.float64).reshape(-1, 3) for P in samples]
    cands = candidates(delays, dzs)
    max_dn = round(max(delays) / dt_s) if delays else 0
    n_total = max((len(P) for P in Ps), default=1) + max_dn
    order = sorted(range(K), key=lambda i: (-float(prio[i]), i))
    delays_out = [0.0] * K
    dz_out = [0.0] * K
    reserved: list[tuple[int, np.ndarray, np.ndarray]] = []
    res = DeconflictResult(delays_out, dz_out)
    vcache: dict[tuple[int, float], bool] = {}
    for k in order:
        placed = False
        for d, z in cands:
            if z != 0.0 and validate is not None:
                key = (k, z)
                if key not in vcache:
                    vcache[key] = bool(validate(k, z))
                if not vcache[key]:
                    continue
            T = _shifted(Ps[k], round(d / dt_s), z, n_total)
            bb = _bbox(T)
            ok = True
            for _, R, rbb in reserved:
                if _bbox_far(bb, rbb, clearance_m, z_scale):
                    continue
                res.checks += 1
                if min_ellipsoid_dist(T, R, z_scale) < clearance_m:
                    ok = False
                    break
            if ok:
                delays_out[k], dz_out[k] = d, z
                reserved.append((k, T, bb))
                placed = True
                break
        if not placed:
            res.partial.append(k)
            T = _shifted(Ps[k], 0, 0.0, n_total)
            reserved.append((k, T, _bbox(T)))
    for a in range(len(reserved)):
        for b in range(a + 1, len(reserved)):
            i, A, _ = reserved[a]
            j, B, _ = reserved[b]
            r = min_ellipsoid_dist(A, B, z_scale)
            if r < clearance_m:
                res.residual.append((min(i, j), max(i, j), r))
    return res
