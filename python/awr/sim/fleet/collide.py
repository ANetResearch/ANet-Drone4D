"""机间碰撞判定 COLLISION_UAV（M08-FR-037；D1-ext，M08 §2.1）。

两机在最近一次检查以来的线性运动段上的最近距离（CPA）小于两机碰撞半径之和即判 CRASHED/COLLISION_UAV（`crash_sub = 3`），
两机均置 KILLED、推力 0。候选对由 8 套半格平移网格的同格配对给出（格宽 C 时距离 < C/2 的任意一对至少在一套网格中同格），
全部向量化（排序 + 相邻比较），N = 1000 时每次约数百 µs，按 25 Hz 执行（contact 每 5 次调用一次）。M09 FleetGuard 的
间距与让行（10 Hz 网格哈希 + CPA）属 M09；本模块只做几何碰撞后果。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from .state import EVT_COLLISION, CtrlMode

if TYPE_CHECKING:
    from .state import FleetState

__all__ = ["CRASH_COLLISION_UAV", "UavCollider"]

CRASH_COLLISION_UAV = 3
CELL_M = 6.0
_SHIFTS = np.array([[a, b, c] for a in (0.0, 0.5) for b in (0.0, 0.5) for c in (0.0, 0.5)]) * CELL_M


class UavCollider:
    def __init__(self, capacity: int) -> None:
        self.p_chk = np.zeros((capacity, 3))
        self.primed = np.zeros(capacity, np.bool_)

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
        a, b = pa[:, 0], pa[:, 1]
        r0 = p0[a] - p0[b]
        r1 = p1[a] - p1[b]
        d = r1 - r0
        dd = (d * d).sum(1)
        s = np.clip(np.where(dd > 1e-12, -(r0 * d).sum(1) / np.where(dd > 1e-12, dd, 1.0), 0.0), 0.0, 1.0)
        m = r0 + s[:, None] * d
        dist = np.sqrt((m * m).sum(1))
        hit = dist < radius[S.profile_id[i[a]]] + radius[S.profile_id[i[b]]]
        out = []
        for x, y in zip(i[a[hit]], i[b[hit]], strict=True):
            for sl in (int(x), int(y)):
                S.crash_sub[sl] = CRASH_COLLISION_UAV
                S.ctrl_mode[sl] = CtrlMode.KILLED
                S.thrust[sl] = 0.0
                S.thr_sp[sl] = 0.0
                S.mode_evt[sl] |= EVT_COLLISION
            out.append((int(x), int(y)))
        return out
