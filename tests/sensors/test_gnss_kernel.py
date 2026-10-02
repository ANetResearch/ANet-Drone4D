"""GNSS 分片推进的融合核与 numpy 路径逐位相同（FX2-R3，ADR-070；kernels_gnss.gnss_step）。

同一初始状态、同一 RNG 流各走 60 s：其中一部分机体在 5–6 s 注入 gnss_denied、20 s 解除（覆盖注入、保持、捕获、
浮点与固定收敛），比较误差状态、fix、状态时刻、卫星数、HDOP、传感器状态与 `sensor.gnss_fix` 事件序列。"""

from __future__ import annotations

import numpy as np
import pytest

from awr.sim.fleet.stages.registry import StateBlockSpec
from awr.sim.sensors import kernels_gnss as KG

pytestmark = pytest.mark.skipif(not KG.HAVE_NUMBA, reason="numba unavailable")


def _run(bench_factory, use_numba: bool) -> tuple[dict, list]:
    b = bench_factory()
    b.rt.gnss.use_numba = use_numba
    b.S.add_block(StateBlockSpec("safety", "M09", {"fault_mask": (np.dtype(np.uint8), ()), "flag_loc_ok": (np.dtype(bool), ())}))
    sb = b.S.blocks["safety"]
    rng = np.random.default_rng(4)
    for s in range(30):
        b.spawn(s, s + 1, pos=rng.uniform(-50, 50, 3) + np.array([0.0, 0.0, 60.0]))
    b.run(5.0)
    sb["fault_mask"][:12] |= 16
    b.run(1.0)
    sb["fault_mask"][3:9] &= ~np.uint8(16)
    b.run(14.0)
    sb["fault_mask"][:] = 0
    b.run(40.0)
    blk = b.S.blocks["sensors"]
    out = {k: blk[k].copy() for k in ("gn_z", "gn_fix", "gn_t_state_ns", "gn_sats", "gn_hdop", "state")}
    ev = [(e["t_sim_ns"], e["uav"], e["data"]["from"], e["data"]["to"], e["data"]["reason"])
          for e in b.events.of("sensor.gnss_fix")]
    return out, ev


def test_gnss_kernel_matches_numpy(bench_factory):
    a, ea = _run(bench_factory, False)
    b, eb = _run(bench_factory, True)
    for k in a:
        assert a[k].tobytes() == b[k].tobytes(), k
    assert ea == eb and len(ea) > 20
