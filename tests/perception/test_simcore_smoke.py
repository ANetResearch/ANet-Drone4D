"""真实 sim-core 装配（M08 管线构建与查询路由）：M13、M18、M19 插件在隔离登记表中装配后，sandbox100 与 d1_250 两个时钟档的管线
都能构建（order 区段、按档调度表、预算 Σ），perception stage 按档运行；`ctl/sim-core/query` 的 `perception/*` 查询经 sim-core 的
慢任务路由到 M19 处理函数（与 SB-38、SB-39 同一路径）。"""

from __future__ import annotations

import pytest
from simlib import CoreHarness

from awr.sim.fleet.stages import registry as R


@pytest.mark.parametrize("clock", ["sandbox100", "d1_250"])
def test_pipeline_builds_and_queries_route(clock):
    with R.isolated_registry() as reg:
        from awr.sim.perception import plugin as pp
        from awr.sim.sensors import plugin as sp
        from awr.sim.targets import plugin as tp

        sp.install()
        tp.install()
        prt = pp.install()
        try:
            h = CoreHarness(n=1, reg=reg, fleet_cfg={"clock_profile": clock})
            try:
                stages = {s.name: s for s in h.core.fleet.pipeline.stages}
                st = stages["perception"]
                assert st.order == 175 and st.owner == "M19"
                assert st.every == (1 if clock == "sandbox100" else 2)
                h.advance(2.0)
                assert prt.stats["calls"] > 0
                q = reg.queries["perception/detections"].fn({"op": "perception/detections", "args": {}}, h.core.ctx)
                assert q["code"] == 0 and q["item"]["items"] == []
            finally:
                h.close()
        finally:
            pp.uninstall()
            tp.uninstall()
            sp.uninstall()
