"""PathBuffer（CSR）与 TOPP-lite（M08-FR-023；M08 §6.3.3、§6.5.2）。

- `PathBuffer`：M = 524,288 行预分配（1000 架各 256 点；每条路径占 2·(n+1) 行：航点列 `pts`（NED）、`yaw`（逐航点偏航，
  NaN 表示沿切线）、`v_wp` 用前 n+1 行，段表 `seg`（列见 `kernels_l1.SG_*`）用前 2n−1 行）；首次适配空闲链表分配，
  单次命令最多 1000 点，空间不足返回 None（准入 110 PATH_BUFFER_FULL）；
- `topp_lite(w, ...)`：航点转弯限速 `√(a·d·tan(alpha/2))`（PX4 `computeMaxSpeedInWaypoint`）+ 前后向梯形速度剖面，航点处以
  半径 v²/a 的相切圆弧过渡（见 `kernels_path` 模块说明；与 M08 §6.5.2 伪代码的差异见实现报告偏差表）；numba 不可用时
  以同一函数的纯 Python 形式执行。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import kernels_l1 as K
from . import params_px4 as P

__all__ = ["EMPTY_PATH", "MAX_PATH_LEN_M", "MAX_PATH_POINTS", "PathBuffer", "topp_lite", "topp_lite_py", "total_time"]

MAX_PATH_POINTS = 1000
MAX_PATH_LEN_M = 20_000.0
PATH_CAPACITY = 524_288


@dataclass
class _Block:
    off: int
    n: int


class PathBuffer:
    def __init__(self, capacity: int = PATH_CAPACITY) -> None:
        m = self.capacity = int(capacity)
        self.pts = np.zeros((m, 3))
        self.yaw = np.full(m, np.nan)
        self.v_wp = np.zeros(m)
        self.seg = np.zeros((m, K.SG_COLS))
        self.free: list[_Block] = [_Block(0, m)]
        self.used: dict[int, int] = {}

    @staticmethod
    def rows_for(n_points: int) -> int:
        return 2 * int(n_points)

    # ------------------------------------------------------------ 分配（首次适配）
    def alloc(self, n: int) -> int | None:
        for k, b in enumerate(self.free):
            if b.n >= n:
                off = b.off
                if b.n == n:
                    self.free.pop(k)
                else:
                    self.free[k] = _Block(b.off + n, b.n - n)
                self.used[off] = n
                return off
        return None

    def release(self, off: int) -> None:
        n = self.used.pop(int(off), None)
        if n is None:
            return
        self.free.append(_Block(int(off), n))
        self.free.sort(key=lambda b: b.off)
        merged: list[_Block] = []
        for b in self.free:
            if merged and merged[-1].off + merged[-1].n == b.off:
                merged[-1] = _Block(merged[-1].off, merged[-1].n + b.n)
            else:
                merged.append(b)
        self.free = merged

    def free_rows(self) -> int:
        return sum(b.n for b in self.free)

    # ------------------------------------------------------------ 写入
    def write(self, off: int, w: np.ndarray, yaw: np.ndarray, seg: np.ndarray, v_wp: np.ndarray) -> None:
        n1 = len(w)
        sl = slice(off, off + n1)
        self.pts[sl] = w
        self.yaw[sl] = yaw
        self.v_wp[sl] = v_wp
        self.seg[off:off + len(seg)] = seg

    def checkpoint_arrays(self, copy: bool = True) -> dict[str, np.ndarray]:
        hi = max((o + n for o, n in self.used.items()), default=0)  # 高水位以下的行（checkpoint 不写全表）
        return {k: (getattr(self, k)[:hi].copy() if copy else getattr(self, k)[:hi]) for k in ("pts", "yaw", "v_wp", "seg")}

    def checkpoint_meta(self) -> dict:
        return {"free": [[b.off, b.n] for b in self.free], "used": [[k, v] for k, v in self.used.items()]}

    def restore(self, arrays: dict[str, np.ndarray], meta: dict) -> None:
        for k, a in arrays.items():
            getattr(self, k)[:len(a)] = a
        self.free = [_Block(int(o), int(n)) for o, n in meta.get("free", [])]
        self.used = {int(o): int(n) for o, n in meta.get("used", [])}


EMPTY_PATH = PathBuffer(2)


def topp_lite(w: np.ndarray, v_start: float, v_c: float, a: float, vz_up: float = P.MPC_Z_V_AUTO_UP,
              vz_dn: float = P.MPC_Z_V_AUTO_DN, d_acc: float = P.NAV_ACC_RAD, *,
              kernel: str = "numba") -> tuple[np.ndarray, np.ndarray]:
    """返回 (段表 m×SG_COLS, 航点速度 n+1)；总时长为 `seg[-1, SG_T0] + seg[-1, SG_T]`（`total_time`）。"""
    from .kernels_path import topp_lite_nb

    w = np.ascontiguousarray(w, np.float64)
    n = len(w) - 1
    out = np.zeros((max(2 * n - 1, 1), K.SG_COLS))
    vw = np.zeros(n + 1)
    fn = topp_lite_nb if kernel == "numba" and K.HAVE_NUMBA else getattr(topp_lite_nb, "py_func", topp_lite_nb)
    m = fn(w, float(v_start), float(v_c), float(a), float(vz_up), float(vz_dn), float(d_acc), out, vw)
    return out[:m], vw


def topp_lite_py(w: np.ndarray, v_start: float, v_c: float, a: float, vz_up: float = P.MPC_Z_V_AUTO_UP,
                 vz_dn: float = P.MPC_Z_V_AUTO_DN, d_acc: float = P.NAV_ACC_RAD) -> tuple[np.ndarray, np.ndarray]:
    """纯 Python 执行（同一函数体的 `py_func`，供无 numba 环境与对拍）。"""
    return topp_lite(w, v_start, v_c, a, vz_up, vz_dn, d_acc, kernel="numpy")


def total_time(seg: np.ndarray) -> float:
    return float(seg[-1, K.SG_T0] + seg[-1, K.SG_T]) if len(seg) else 0.0


def dedupe(w: np.ndarray, eps: float = 1e-3) -> tuple[np.ndarray, np.ndarray]:
    """去掉相邻重复航点（段长 < eps）；返回 (w, keep 下标)。"""
    w = np.asarray(w, np.float64)
    keep = [0]
    for k in range(1, len(w)):
        if np.linalg.norm(w[k] - w[keep[-1]]) >= eps:
            keep.append(k)
    ki = np.asarray(keep)
    return w[ki], ki
