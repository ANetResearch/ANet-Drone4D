"""多机覆盖切分与分配（M10-FR-051；M10 §6.5.12 第 6–7 步；r26 §3.9）。

- 航段代价 `w_j = L_j/V + V/a + gap_j/V`；按累计代价的 k 等分点切分，**在航段内部插入切点**使各块代价相等
  （r26 原型只在航段之间切，航带少时均衡度 1.2–1.6）；
- 分块到机库：`C[i, j] = min(|home_i − entry_j| + |exit_j − home_i|，反向同理)/V`，Hungarian，记录每块是否反向。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..allocation.hungarian import hungarian
from .lanes import Seg

__all__ = ["Assignment", "assign_chunks", "chunk_cost", "split_balanced"]


def _seg_costs(seq: list[Seg], v: float, a: float) -> tuple[np.ndarray, np.ndarray]:
    L = np.array([float(np.linalg.norm(p1 - p0)) for p0, p1 in seq])
    gap = np.array([float(np.linalg.norm(seq[i + 1][0] - seq[i][1])) for i in range(len(seq) - 1)] + [0.0])
    return L, gap


def chunk_cost(chunk: list[Seg], v_mps: float, a_mps2: float = 2.0) -> float:
    if not chunk:
        return 0.0
    L, gap = _seg_costs(chunk, v_mps, a_mps2)
    return float((L / v_mps + v_mps / a_mps2).sum() + gap[:-1].sum() / v_mps)


def split_balanced(seq: list[Seg], k: int, v_mps: float, a_mps2: float = 2.0, *, exact: bool = True) -> list[list[Seg]]:
    """把有序航段切成 k 块连续序列，各块代价近似相等；exact 时在航段内部插入切点。"""
    k = max(1, int(k))
    if k == 1 or not seq:
        return [list(seq)]
    L, gap = _seg_costs(seq, v_mps, a_mps2)
    w_seg = L / v_mps + v_mps / a_mps2
    starts = np.r_[0.0, np.cumsum(w_seg + gap / v_mps)[:-1]]          # 每段开始时的累计代价
    tot = float((w_seg + gap / v_mps).sum() - gap[-1] / v_mps)
    targets = [tot * j / k for j in range(1, k)]
    chunks: list[list[Seg]] = []
    cur: list[Seg] = []
    j = 0
    pending: Seg | None = None
    pend_start = 0.0
    for t in targets:
        while j < len(seq):
            p0, p1 = pending if pending is not None else seq[j]
            seg_start = float(starts[j]) if pending is None else pend_start
            straight = float(np.linalg.norm(p1 - p0)) / v_mps
            seg_end = seg_start + straight + v_mps / a_mps2
            if t < seg_end:
                if exact and straight > 1e-9 and t > seg_start + 1e-9:
                    f = min(max((t - seg_start) / (straight + v_mps / a_mps2), 0.0), 1.0)
                    pc = p0 + (p1 - p0) * f
                    if float(np.linalg.norm(pc - p0)) > 1e-3 and float(np.linalg.norm(p1 - pc)) > 1e-3:
                        cur.append((p0, pc))
                        pending = (pc, p1)
                        pend_start = t
                        break
                break
            cur.append((p0, p1))
            pending = None
            j += 1
        chunks.append(cur)
        cur = []
    while j < len(seq):
        cur.append(pending if pending is not None else seq[j])
        pending = None
        j += 1
    chunks.append(cur)
    return [c for c in chunks if c]


@dataclass
class Assignment:
    chunk_of: np.ndarray     # 机体 i → 块号
    reversed_: np.ndarray    # 机体 i 的块是否反向飞
    cost_s: np.ndarray       # C[i, chunk_of[i]]


def assign_chunks(chunks: list[list[Seg]], homes: np.ndarray, v_mps: float) -> Assignment:
    homes = np.asarray(homes, np.float64)[:, :2]
    n, k = len(homes), len(chunks)
    C = np.zeros((n, k))
    rev = np.zeros((n, k), bool)
    for i, h in enumerate(homes):
        for j, ch in enumerate(chunks):
            e0, e1 = ch[0][0], ch[-1][1]
            d_fwd = float(np.linalg.norm(e0 - h) + np.linalg.norm(e1 - h))
            d_rev = float(np.linalg.norm(e1 - h) + np.linalg.norm(e0 - h))
            C[i, j] = min(d_fwd, d_rev) / v_mps
            # 两个方向进出总距离相同；以"先到的端点更近"判定方向
            rev[i, j] = float(np.linalg.norm(e1 - h)) < float(np.linalg.norm(e0 - h))
    a = hungarian(C)
    r = np.array([rev[i, a[i]] if a[i] >= 0 else False for i in range(n)])
    c = np.array([C[i, a[i]] if a[i] >= 0 else math.inf for i in range(n)])
    return Assignment(a, r, c)
