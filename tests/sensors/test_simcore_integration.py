"""M13 在真实 M08 sim-core（进程内 SimCore + LocalBus + LocalRing）中的装配：stage、状态块、慢任务发布
`state/sim-core/sensor`（SensorPose48，兴趣集驱动）、profile 描述；M13-FR-010、FR-013、FR-015。"""

from __future__ import annotations

import secrets

import msgpack
import numpy as np

from awr.contracts import LAYOUT_ID, bus_keys
from awr.contracts.layouts import SENSOR_POSE48
from awr.runtime.bus import LocalBus
from awr.runtime.statering import LocalRing
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import registry as R
from awr.sim.runtime.config import SimConfig
from awr.sim.runtime.main import SimCore


def test_plugin_in_simcore():
    from awr.sim.sensors import plugin

    with R.isolated_registry() as reg:
        rt = plugin.install()
        W = [1_000_000_000]
        run_id = "rm13" + secrets.token_hex(3)
        path = f"/m13/{run_id}/state.sim-core"
        ring, _ = LocalRing.open_or_create(path, capacity=1024, slots=32, layout_id=LAYOUT_ID, id_base=0, id_count=1024)
        bus = LocalBus.open("sim-core", namespace=f"awr/test/{run_id}")
        got: list[bytes] = []
        sub = bus.subscribe(bus_keys.state_sensor("sim-core"), lambda k, raw: got.append(raw))
        core = SimCore(SimConfig(run_id=run_id, n_vehicles=2, load_world=False, autoplay=True), bus, ring,
                       secret=secrets.token_bytes(32), wall_ns=lambda: W[0], reg=reg)
        try:
            core.start()
            assert any(s.name == "sensors" for s in core.fleet.pipeline.stages)
            nos = [e.agent_no for e in core.roster.by_slot.values()]
            core.ctx.interest = np.asarray(nos[:1], np.uint16)
            end = core.clock.t_ns + 1_000_000_000
            while core.clock.t_ns < end:
                W[0] += 8 * TICK_NS
                core.iterate()
            assert rt.stats["spawned"] == 2
            assert got, "no state/sim-core/sensor message"
            m = msgpack.unpackb(got[-1], raw=False)
            rows = np.frombuffer(m["rows"], SENSOR_POSE48)
            assert m["v"] == 1 and set(rows["agent_no"].tolist()) == {nos[0]} and sorted(rows["sensor_no"].tolist()) == [0, 1]
            item = core.T.describe("p600_mid360")
            assert "camera" in item["sensors"] and item["sensors"]["camera"]["intrinsics"]["w"] == 6000
            ext = rt.ext_fields_for(next(iter(core.roster.by_slot)))
            assert ext["loc"]["gnss_fix"] == 4
        finally:
            sub.close()
            core.stop()
            bus.close()
            LocalRing.remove(path)
            plugin.uninstall()
