"""M13 GNSS 分片推进的 numba 融合核（M13-FR-020、FR-021；M13 §6.5.3；FX2-R3，ADR-070）。

`GnssBank.step_part` 每 50 Hz 推进 1/5 的机体（每机 10 Hz）：Gauss-Markov 误差状态 `z ← φ·z + √(1 − φ²)·w`
（φ = exp(−Δt/τ[fix])）与 fix 状态机（注入、捕获、收敛）。numpy 实现约 20 个花式下标与掩码运算，N = 1000（每片 200 架）
时约 0.33 ms/次。本核一次遍历完成同一计算（只读写给定 slot 行），返回 fix 发生变化的行数；变化行的事件仍由调用方按原
实现发出（大机群稳态下 fix 恒为 RTK_FIXED，不变化）。运算与 numpy 实现逐项相同（`math.exp`、`math.sqrt` 与 numpy 在本机
走同一 libm；乘加不合并为 FMA）；`tests/sensors/test_gnss_kernel.py` 与 numpy 路径逐位对拍。白噪声仍由调用方以
`rng.standard_normal((n, 3))` 生成（RNG 流与次序不变）。
"""

from __future__ import annotations

import math
import os

import numpy as np

try:
    from numba import njit

    HAVE_NUMBA = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover
    HAVE_NUMBA = False

    def njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f

        return deco(a[0]) if a and callable(a[0]) else deco

__all__ = ["HAVE_NUMBA", "gnss_step", "warmup"]

_NO_FIX, _SINGLE, _DGPS, _RTK_FLOAT, _RTK_FIXED = 0, 1, 2, 3, 4


@njit(cache=True, fastmath=False)
def gnss_step(slots, fix, denied, tau_tab, t_acq_ns, t_float_ns, t_fixed_ns, rtk, dgps, max_fix, gn_z, noise, gn_t_state,
              t_ns, dt, out_new, out_reason):
    """slots 对应的误差状态与 fix 状态机推进一步；fix 为本片各行的当前 fix（int64），out_new、out_reason 写新 fix 与原因
    （0 无、1 注入、2 捕获、3 收敛）。返回 fix 变化的行数。"""
    n = slots.shape[0]
    nch = 0
    for k in range(n):
        s = slots[k]
        f = fix[k]
        fi = f if f > _SINGLE else _SINGLE
        ph = math.exp(-dt / tau_tab[fi])
        sq = math.sqrt(1.0 - ph * ph)
        for d in range(3):
            gn_z[s, d] = ph * gn_z[s, d] + sq * noise[k, d]
        age = t_ns - gn_t_state[s]
        new = f
        rs = 0
        if denied[k]:
            if f != _NO_FIX:
                new = _NO_FIX
                rs = 1
            else:
                gn_t_state[s] = t_ns
        else:
            if f == _NO_FIX and age >= t_acq_ns and max_fix >= _SINGLE:
                new = _SINGLE
                rs = 2
            elif f == _SINGLE and age >= t_float_ns:
                if rtk and max_fix >= _RTK_FLOAT:
                    new = _RTK_FLOAT
                    rs = 3
                elif dgps and max_fix >= _DGPS:
                    new = _DGPS
                    rs = 3
            elif f == _RTK_FLOAT and age >= t_fixed_ns and rtk and max_fix >= _RTK_FIXED:
                new = _RTK_FIXED
                rs = 3
        out_new[k] = new
        out_reason[k] = rs
        if new != f:
            nch += 1
    return nch


def warmup() -> None:
    """以运行期类型调用一次（slot int64、fix int64、denied bool、τ 表 float64、状态块数组 C 连续）。"""
    if not HAVE_NUMBA:
        return
    sl = np.zeros(1, np.int64)
    gnss_step(sl, np.full(1, 4, np.int64), np.zeros(1, np.bool_), np.ones(5), 1, 1, 1, True, False, 4, np.zeros((4, 3)),
              np.zeros((1, 3)), np.zeros(4, np.int64), 0, 0.1, np.zeros(1, np.int64), np.zeros(1, np.int8))
