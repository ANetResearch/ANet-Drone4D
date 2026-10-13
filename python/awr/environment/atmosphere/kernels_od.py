"""路径光学厚度的逐对核（D2-FX-R2-sim 第 2 次；M07-FR-006、FR-020；AWR-04 §6.3 第 4 步）。

D2 验收第 1 轮 4.1 与 d2-FX-R2-perception-to-M07：M19 每次（机体，成像传感器）评估与每次 MID-360 评估各调用一次
`EnvironmentField.optical_depth`（几到三十对），numpy 实现约 150–190 µs/次（`scalars` + `derive` 与十余次小数组运算），沙盒 10 架
约 100 次/仿真秒。本核在一次遍历中完成与 `optics.optical_depth_arr`、`optical_depth_lambda_arr` 逐元素同式的计算：

- L = √((dx² + dy²) + dz²)（与 `np.linalg.norm(dv, axis=1)` 同一归约次序），rd_z = dz / L（L > 0，否则 0）；
- 霾：`a = sigma_haze0·exp(−z0/H)`、`k = rd_z·L/H`、`f = 1`（|k| ≤ 1e−4）或 `(1 − exp(−k))/k`，`a·L·f`；
- 雾层与降水层：`_flat_len_arr` 的逐元素形式；
- mode 0（550 nm）：`(霾 + 雾) + 降水`；mode 1（其他波长）：`kim·(霾 + 雾) + 降水`；mode 3（LWIR）另加 `sigma_wv·L`。

numba 不可用时由调用方走 numpy 实现（结果相同）。
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

__all__ = ["HAVE_NUMBA", "od_pairs", "warmup"]


@njit(cache=True, fastmath=False)
def _flat_len(ro_z, rd_z, ln, top):
    big = abs(rd_z) > 1e-5
    tc = (top - ro_z) / rd_z if big else 0.0
    t1 = (ln if ln <= tc else tc) if (big and rd_z > 0) else ln
    t0 = (0.0 if tc <= 0.0 else tc) if (big and rd_z <= 0) else 0.0
    x = t1 - t0
    out = x if x >= 0.0 else 0.0
    if (not big) and ro_z > top:
        return 0.0
    return out


@njit(cache=True, fastmath=False)
def od_pairs(p0, p1, ground_z, sigma_haze0, h_haze, sigma_fog, fog_top, sigma_precip, cloud_base, mode, kim, sigma_wv, out):
    """p0[i] → p1[i] 的光学厚度写入 out[i]（mode 见模块说明）。"""
    n = p1.shape[0]
    for i in range(n):
        dx = p1[i, 0] - p0[i, 0]
        dy = p1[i, 1] - p0[i, 1]
        dz = p1[i, 2] - p0[i, 2]
        L = math.sqrt((dx * dx + dy * dy) + dz * dz)
        rd = dz / L if L > 0 else 0.0
        z0 = p0[i, 2] - ground_z
        a = sigma_haze0 * math.exp(-z0 / h_haze)
        k = rd * L / h_haze
        f = 1.0 if abs(k) <= 1e-4 else (1.0 - math.exp(-k)) / k
        od = a * L * f
        if sigma_fog > 0:
            od = od + sigma_fog * _flat_len(z0, rd, L, fog_top)
        if mode != 0:
            od = kim * od
        if sigma_precip > 0:
            od = od + sigma_precip * _flat_len(z0, rd, L, cloud_base)
        if mode == 3:
            od = od + sigma_wv * L
        out[i] = od


def warmup() -> None:
    """按运行期签名编译或读缓存（C 连续 float64 数组、float64 标量、int64 mode）。"""
    if not HAVE_NUMBA:
        return
    p0 = np.zeros((2, 3))
    p1 = np.ones((2, 3))
    out = np.empty(2)
    od_pairs(p0, p1, 0.0, 1e-4, 1500.0, 1e-3, 50.0, 1e-4, 800.0, 0, 1.0, 0.0, out)
