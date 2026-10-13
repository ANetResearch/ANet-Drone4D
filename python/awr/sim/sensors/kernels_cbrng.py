"""计数器 RNG 外积键的 numba 核（M13-FR-030；M13 §6.5.4；D2-FX-R2-sim 第 2 次）。

`cbrng.normal` / `cbrng.uniform` 在 agent (n,)、ch (m,)、tick 标量时求外积键 K[i, j] = key(seed, stream, agents[i], tick,
chs[j]) 与 Box–Muller 第二键 B = sm(K ^ C3)。numpy 实现每级 `sm` 一次 `errstate` 与 8 次 uint64 运算，一次调用约
0.1–0.3 ms（M18 识别物运动、M13 IMU/GNSS 每 tick 调用）；本核一次遍历完成，uint64 回绕与 numpy 实现逐位相同。

M13-NFR-010：`import awr.sim.sensors.cbrng` 不得引入 numba，故本核单独成模块，由插件安装时的 warmup 导入；`cbrng` 只在
本模块已导入时走快路径（否则走 numpy，结果相同）。
"""

from __future__ import annotations

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

__all__ = ["HAVE_NUMBA", "outer_keys"]

_U = np.uint64
_K1, _K2, _K3 = _U(0x9E3779B97F4A7C15), _U(0xBF58476D1CE4E5B9), _U(0x94D049BB133111EB)
_S30, _S27, _S31 = _U(30), _U(27), _U(31)


@njit(cache=True, fastmath=False)
def _sm(z):
    z = z + _K1
    z = (z ^ (z >> _S30)) * _K2
    z = (z ^ (z >> _S27)) * _K3
    return z ^ (z >> _S31)


@njit(cache=True, fastmath=False)
def outer_keys(seed, stream, agents, tick, chs, K, B):
    """K[i, j] = key(seed, stream, agents[i], tick, chs[j])，B = sm(K ^ C3)（参数均为 uint64；与 `cbrng.key` 逐位相同）。"""
    k2 = _sm(_sm(seed) ^ stream)
    for i in range(agents.shape[0]):
        kt = _sm(_sm(k2 ^ agents[i]) ^ tick)
        for j in range(chs.shape[0]):
            k = _sm(kt ^ chs[j])
            K[i, j] = k
            B[i, j] = _sm(k ^ _K3)
