"""机间碰撞判定 COLLISION_UAV（M08-FR-037；D1-ext，M08 §2.1）。

两机在最近一次检查以来的线性运动段上的最近距离（CPA）小于两机碰撞半径之和即判 CRASHED/COLLISION_UAV（`crash_sub = 3`），
两机均置 KILLED、推力 0。候选对由 8 套半格平移网格的同格配对给出（格宽 C 时距离 < C/2 的任意一对至少在一套网格中同格），
全部向量化（排序 + 相邻比较），N = 1000 时每次约数百 µs，按 25 Hz 执行（contact 每 5 次调用一次）。M09 FleetGuard 的
间距与让行（10 Hz 网格哈希 + CPA）属 M09；本模块只做几何碰撞后果。
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

import numpy as np

from . import kernels_contact as KC
from .kernels_l1 import HAVE_NUMBA as KC_OK
from .state import EVT_COLLISION, CtrlMode

if TYPE_CHECKING:
    from .state import FleetState

__all__ = ["CRASH_COLLISION_UAV", "UavCollider"]

CRASH_COLLISION_UAV = 3
CELL_M = 6.0
SMALL_N = 48  # 不超过该机数时逐对比较（n(n−1)/2 ≤ 1128 对 × 8 套网格），否则排序配对
_SHIFTS = np.array([[a, b, c] for a in (0.0, 0.5) for b in (0.0, 0.5) for c in (0.0, 0.5)]) * CELL_M


@cache
def _pairs(n: int) -> tuple[np.ndarray, np.ndarray]:
    """`np.triu_indices(n, 1)` 的只读缓存（小机群每 25 Hz 调用，n ≤ SMALL_N）。"""
    iu, ju = np.triu_indices(n, 1)
    iu.flags.writeable = False
    ju.flags.writeable = False
    return iu, ju


class UavCollider:
    def __init__(self, capacity: int) -> None:
        self.p_chk = np.zeros((capacity, 3))
        self.primed = np.zeros(capacity, np.bool_)
        self.use_kernel = KC_OK
        self._hits_buf = np.zeros((4 * capacity, 2), np.int64)

    def check(self, S: FleetState, idx: np.ndarray, radius: np.ndarray) -> list[tuple[int, int]]:
        """返回本次新判定的碰撞对（slot 升序）；并更新机体状态。只考虑空中且未坠毁的机体。"""
        ok = ~S.landed[idx] & (S.crash_sub[idx] == 0)
        i = idx[ok].astype(np.int64)
        p1 = S.p[i]
        p0 = np.where(self.primed[i][:, None], self.p_chk[i], p1)
        self.p_chk[idx] = S.p[idx]
        self.primed[idx] = True
        self.primed[S.landed] = False
        if i.size < 2:
            return []
        mid = 0.5 * (p0 + p1)
        if i.size <= SMALL_N:
            # 小机群：全部无序对逐套网格比较同格（与下方排序配对得到同一候选集与同一升序，FX-SIM1 固定开销优化）
            iu, ju = _pairs(int(i.size))
            key = np.floor((mid[None, :, :] + _SHIFTS[:, None, :]) / CELL_M)
            same = np.all(key[:, iu, :] == key[:, ju, :], axis=2).any(axis=0)
            if not same.any():
                return []
            return self._hits(S, i, p0, p1, iu[same].astype(np.int64), ju[same].astype(np.int64), radius)
        if self.use_kernel:
            # 大机群：numba 排序扫描（中点各轴之差 < CELL_M 为候选，覆盖网格法的全部候选）与 CPA 判定一次完成；命中对按
            # (a, b) 升序，与下方网格法同一判据、同一施加顺序（FX2-R2：N = 1000 时约 3 ms → 数十 µs）
            rad = radius[S.profile_id[i]].astype(np.float64)
            nh = KC.uav_hits(p0, p1, rad, CELL_M, self._hits_buf)
            if nh >= 0:
                if nh == 0:
                    return []
                h = self._hits_buf[:nh]
                return self._apply(S, i, h[:, 0], h[:, 1])
        cand: set[tuple[int, int]] = set()
        for sh in _SHIFTS:
            key = np.floor((mid + sh) / CELL_M).astype(np.int64)
            order = np.lexsort((key[:, 2], key[:, 1], key[:, 0]))
            ks = key[order]
            same = np.all(ks[1:] == ks[:-1], axis=1)
            if not same.any():
                continue
            for k in np.flatnonzero(same):
                a, b = int(order[k]), int(order[k + 1])
                cand.add((min(a, b), max(a, b)))
                # 同格超过两机时补齐同格内全部配对
                j = k + 2
                while j < len(order) and np.all(ks[j] == ks[k]):
                    c = int(order[j])
                    cand.add((min(a, c), max(a, c)))
                    cand.add((min(b, c), max(b, c)))
                    j += 1
        if not cand:
            return []
        pa = np.array(sorted(cand), np.int64)
        return self._hits(S, i, p0, p1, pa[:, 0], pa[:, 1], radius)

    @staticmethod
    def _hits(S: FleetState, i: np.ndarray, p0: np.ndarray, p1: np.ndarray, a: np.ndarray, b: np.ndarray,
              radius: np.ndarray) -> list[tuple[int, int]]:
        r0 = p0[a] - p0[b]
        r1 = p1[a] - p1[b]
        d = r1 - r0
        dd = (d * d).sum(1)
        s = np.clip(np.where(dd > 1e-12, -(r0 * d).sum(1) / np.where(dd > 1e-12, dd, 1.0), 0.0), 0.0, 1.0)
        m = r0 + s[:, None] * d
        dist = np.sqrt((m * m).sum(1))
        hit = dist < radius[S.profile_id[i[a]]] + radius[S.profile_id[i[b]]]
        return UavCollider._apply(S, i, a[hit], b[hit])

    @staticmethod
    def _apply(S: FleetState, i: np.ndarray, a: np.ndarray, b: np.ndarray) -> list[tuple[int, int]]:
        """命中对的后果（两机 KILLED、推力 0、COLLISION_UAV），按给定顺序施加；返回 slot 对。"""
        out = []
        for x, y in zip(i[a], i[b], strict=True):
            for sl in (int(x), int(y)):
                S.crash_sub[sl] = CRASH_COLLISION_UAV
                S.ctrl_mode[sl] = CtrlMode.KILLED
                S.thrust[sl] = 0.0
                S.thr_sp[sl] = 0.0
                S.mode_evt[sl] |= EVT_COLLISION
            out.append((int(x), int(y)))
        return out
