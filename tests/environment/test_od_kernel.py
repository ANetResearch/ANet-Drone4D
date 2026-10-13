"""路径光学厚度逐对核与 numpy 实现逐位相同（M07-FR-006、FR-020；D2-FX-R2-sim 第 2 次，`atmosphere/kernels_od.py`）。

`EnvironmentField.optical_depth` 在 numba 可用时走 `kernels_od.od_pairs`，否则走 `optics.optical_depth_arr` /
`optical_depth_lambda_arr`；两条路径对全部天气预设、550/850/905 nm 与 LWIR、含零长度与水平射线的随机线段逐位相同。
"""

from __future__ import annotations

import zlib

import numpy as np
import pytest

from awr.environment import field as EF
from awr.environment.atmosphere import kernels_od as KOD
from awr.environment.field import EnvironmentServiceImpl
from awr.environment.weather.presets import presets

BOUNDS = ((-900.0, -1000.0, 0.0), (900.0, 1000.0, 300.0))
COORD = {"ground": {"zM": 2.0}, "anchor": {"hMslM": 12.2}}

pytestmark = pytest.mark.skipif(not KOD.HAVE_NUMBA, reason="numba 不可用（AWR_KERNEL=numpy）")


def _segments(rng: np.random.Generator, n: int) -> tuple[np.ndarray, np.ndarray]:
    p0 = rng.uniform([-800, -800, 0.0], [800, 800, 400.0], (n, 3))
    p1 = rng.uniform([-800, -800, 0.0], [800, 800, 400.0], (n, 3))
    p1[0] = p0[0]                                   # 零长度
    p1[1, 2] = p0[1, 2]                             # 水平
    p1[2, :2] = p0[2, :2]                           # 竖直
    p0[3, 2], p1[3, 2] = 2.0, 1500.0                # 穿越雾顶与云底
    return p0, p1


@pytest.mark.parametrize("preset", list(presets().ids))
def test_kernel_matches_numpy(preset: str, monkeypatch: pytest.MonkeyPatch) -> None:
    env = EnvironmentServiceImpl(world_id="t", world_seed=1, bounds=BOUNDS, initial_preset=preset, coordinate=COORD,
                                 load_assets=False)
    env.on_env_tick_all(0)
    rng = np.random.default_rng(zlib.crc32(preset.encode()))
    p0, p1 = _segments(rng, 64)
    t = env.t_grid_ns
    for lam in (550.0, 850.0, 905.0, 10000.0):
        a = env.optical_depth(p0, p1, t, wavelength_nm=lam)
        monkeypatch.setattr(EF._KOD, "HAVE_NUMBA", False)
        b = env.optical_depth(p0, p1, t, wavelength_nm=lam)
        monkeypatch.setattr(EF._KOD, "HAVE_NUMBA", True)
        assert a.dtype == b.dtype and a.shape == b.shape
        # numba 内核与 numpy 参考在不同 CPU 指令集（AVX-512 与 AVX2 的向量化与 FMA 融合）上末位可能不同：按 1e-12 相对容差比较
        # （与 test_wind_fused、test_gimbal 的口径一致），逐位一致只在固定开发机的基线中出现，不作为跨 CPU 判据
        np.testing.assert_allclose(a, b, rtol=1e-12, atol=1e-15, err_msg=f"{preset} {lam}")
