"""小机群的选择与掩码核（D2-AC-08；M08-NFR-201）：把每 tick 在整容量数组上的多步 numpy 筛选（`flatnonzero(a & (b != 0) & …)`、
`np.where(...).astype(...)`、`(x & bit).any()`）合并为一次 numba 遍历。

sandbox100 的 10 架会话中，这类表达式每步各自分配临时数组并遍历整容量（1024 槽），在主循环的冷缓存下单个表达式约 15–30 µs，
而一次融合遍历约 2–4 µs（`l1_indices`、`rtl_phase_of`、M10 跟踪器的 TRAJ 槽位集合等，每秒 100 次）。结果与原 numpy 表达式
逐位相同（`tests/sim/test_kernels_sel.py` 对拍）；numba 不可用时调用方保留原表达式。
"""

from __future__ import annotations

import numpy as np

from .kernels_l1 import HAVE_NUMBA, njit

__all__ = ["HAVE_NUMBA", "any_bits", "any_bits_at", "any_eq_at", "any_ne_at", "any_nonzero_at", "nonzero2", "rtl_phase",
           "sel_bits", "sel_eq_nonzero", "sel_ne2_active", "take_nonzero", "unequal_finite2", "warmup"]


@njit(cache=True, fastmath=False)
def sel_bits(active, fid, any_mask, none_mask, out):
    """`flatnonzero(active & ((fid & any_mask) != 0) & ((fid & none_mask) == 0))` 写入 out（int32，升序）；返回个数。"""
    k = 0
    for i in range(active.shape[0]):
        if active[i] and (fid[i] & any_mask) != 0 and (fid[i] & none_mask) == 0:
            out[k] = i
            k += 1
    return k


@njit(cache=True, fastmath=False)
def rtl_phase(fs, sub, rtl_code, out):
    """`np.where(fs == rtl_code, sub, 0).astype(uint8)` 写入 out。"""
    for i in range(fs.shape[0]):
        out[i] = sub[i] if fs[i] == rtl_code else 0


@njit(cache=True, fastmath=False)
def any_bits(a, mask):
    """`((a & mask) != 0).any()`。"""
    for i in range(a.shape[0]):  # noqa: SIM110 - numba 核内不用生成器
        if (a[i] & mask) != 0:
            return True
    return False


@njit(cache=True, fastmath=False)
def sel_eq_nonzero(kind, mode, val, out):
    """`i` 满足 `kind[i] != 0 and mode[i] == val`，升序写入 out（int32）；返回个数（= `flatnonzero(kind != 0)` 再按
    `mode[idx] == val` 筛选）。"""
    k = 0
    for i in range(kind.shape[0]):
        if kind[i] != 0 and mode[i] == val:
            out[k] = i
            k += 1
    return k


@njit(cache=True, fastmath=False)
def take_nonzero(idx, a, out):
    """`idx[a[idx] != 0]` 写入 out（与 idx 同 dtype）；返回个数。"""
    k = 0
    for j in range(idx.shape[0]):
        if a[idx[j]] != 0:
            out[k] = idx[j]
            k += 1
    return k


@njit(cache=True, fastmath=False)
def nonzero2(a, ii, kk):
    """二维 `np.nonzero(a)`（行主序）写入 ii、kk（int64）；返回个数。"""
    k = 0
    for i in range(a.shape[0]):
        for j in range(a.shape[1]):
            if a[i, j] != 0:
                ii[k] = i
                kk[k] = j
                k += 1
    return k


@njit(cache=True, fastmath=False)
def unequal_finite2(f, g, ii, kk):
    """`np.nonzero(np.isfinite(f) & np.isfinite(g) & (f != g))`（二维，行主序）写入 ii、kk；返回个数。"""
    k = 0
    for i in range(f.shape[0]):
        for j in range(f.shape[1]):
            a = f[i, j]
            b = g[i, j]
            if np.isfinite(a) and np.isfinite(b) and a != b:
                ii[k] = i
                kk[k] = j
                k += 1
    return k


@njit(cache=True, fastmath=False)
def sel_ne2_active(a, v1, v2, active, out):
    """`flatnonzero((a != v1) & (a != v2) & active)` 写入 out（int64）；返回个数。"""
    k = 0
    for i in range(a.shape[0]):
        if a[i] != v1 and a[i] != v2 and active[i]:
            out[k] = i
            k += 1
    return k


@njit(cache=True, fastmath=False)
def any_ne_at(idx, a, b):
    """`(a[idx] != b[idx]).any()`。"""
    for j in range(idx.shape[0]):  # noqa: SIM110 - numba 核内不用生成器
        if a[idx[j]] != b[idx[j]]:
            return True
    return False


@njit(cache=True, fastmath=False)
def any_nonzero_at(idx, a):
    """`a[idx].any()`（数值数组：任一非零）。"""
    for j in range(idx.shape[0]):  # noqa: SIM110 - numba 核内不用生成器
        if a[idx[j]] != 0:
            return True
    return False


@njit(cache=True, fastmath=False)
def any_eq_at(idx, a, v):
    """`(a[idx] == v).any()`。"""
    for j in range(idx.shape[0]):  # noqa: SIM110 - numba 核内不用生成器
        if a[idx[j]] == v:
            return True
    return False


@njit(cache=True, fastmath=False)
def any_bits_at(idx, a, mask):
    """`((a[idx] & mask) != 0).any()`。"""
    for j in range(idx.shape[0]):  # noqa: SIM110 - numba 核内不用生成器
        if (a[idx[j]] & mask) != 0:
            return True
    return False


def warmup() -> bool:
    """按运行期签名各调用一次（`kernels_l1.warmup` 调用；FleetState 的 active 为 bool、fidelity 与 frame 位为 uint8，safety 块
    的 fs、sub 为 uint8，M10 跟踪块的 kind 为 uint8、ctrl_mode 为 uint8，done 为 uint8）。"""
    if not HAVE_NUMBA:
        return False
    n = 4
    act = np.zeros(n, np.bool_)
    u8 = np.zeros(n, np.uint8)
    o32 = np.empty(n, np.int32)
    sel_bits(act, u8, 3, 16, o32)
    rtl_phase(u8, u8, 5, np.empty(n, np.uint8))
    any_bits(u8, 16)
    sel_eq_nonzero(u8, u8, 15, o32)
    take_nonzero(o32[:0], u8, np.empty(n, np.int32))
    # M13 载荷块：src 为 uint8 (n, NG)，f / f_cmd 为 float64 (n, NG)，band 为 uint8
    i64 = np.empty(n * 2, np.int64)
    nonzero2(np.zeros((n, 2), np.uint8), i64, i64.copy())
    f2 = np.zeros((n, 2))
    unequal_finite2(f2, f2, i64, i64.copy())
    sel_ne2_active(u8, 255, 0, act, np.empty(n, np.int64))
    # M09 FSM 与 FastGuard：活动槽位（flatnonzero，int64）上的 lifecycle、lc_shadow、mode_evt、fs、fidelity（均 uint8）
    a64 = np.arange(2, dtype=np.int64)
    any_ne_at(a64, u8, u8.copy())
    any_nonzero_at(a64, u8)
    any_eq_at(a64, u8, 9)
    any_bits_at(a64, u8, 16)
    any_eq_at(np.arange(2, dtype=np.int32), u8, 7)  # L1 的 Velocity 槽位判断：l1_indices 为 int32
    return True
