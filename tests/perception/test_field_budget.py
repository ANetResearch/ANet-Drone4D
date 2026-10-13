"""M19-AC-026（M19-NFR-007；D2-AC-55 的 M19 部分）：场与读数的服务端成本。`perf` 标记：并行阶段不运行，MS5 按 ADR-033 性能运行协议
测量（本机 Tier S 口径）。

12 架、30 识别物，四种剖面轮流订阅（每 15 s 切换）加感知体表与选中识别物的逐对读数：会话合计 ≤ 2 ms/仿真秒；单次剖面 ≤ 0.5 ms
（12 架、单指标）、感知体表未命中 ≤ 3 ms/（体，机型）、逐对读数 ≤ 0.3 ms/次；无订阅时为 0。
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from test_field_wire import Scene

from awr.sim.perception import field as F

pytestmark = pytest.mark.perf


def test_field_cost_12x30_per_sim_second():
    s = Scene(12, 30)
    try:
        svc = s.svc
        tid = s.tids[1]
        tno = int(s.b.trt._by_id(tid).target_no)
        spent = 0
        t0 = time.perf_counter_ns
        orig = svc.tick

        def timed(ctx):
            nonlocal spent
            a = t0()
            orig(ctx)
            spent += t0() - a

        svc.tick = timed  # type: ignore[method-assign]
        for k, kind in enumerate(F.FIELD_KINDS * 2):
            s.streams(field=[F.KIND_CODE[kind], F.KIND_CODE["volumes"]], quant=[tno], fsub=[k])
            s.tick(15.0)
        sim_s = 120.0
        assert spent / 1e6 / sim_s <= 2.0, spent / 1e6 / sim_s
        # 单项
        us, en = svc.uavs(s.b.ctx), svc.env(s.b.ctx)
        a = t0()
        for _ in range(20):
            svc.profiles("acoustic", s.b.ctx, keyframe=True, uavs=us, env=en)
        assert (t0() - a) / 20 / 1e6 <= 0.5
        a = t0()
        for _ in range(20):
            svc.quant(tid, s.b.ctx, uavs=us, env=en)
        assert (t0() - a) / 20 / 1e6 <= 0.3
        T = s.b.trt._by_id(tid)
        a = t0()
        for i in range(20):
            F.volume_table(T.cfg.raw, T.cfg.raw["sensitivity"][1], us[0].sig, en, 0.3 + 0.01 * i)
        assert (t0() - a) / 20 / 1e6 <= 3.0
        # 无订阅为 0
        s.streams()
        n = dict(svc.stats)
        s.tick(5.0)
        assert svc.stats["profiles"] == n["profiles"] and svc.stats["quant"] == n["quant"]
        assert np.isfinite(spent)
    finally:
        s.close()
