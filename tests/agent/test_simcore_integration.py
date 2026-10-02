"""SimBridge 与真实 sim-core（M08 SimCore，LocalBus + LocalRing，同线程单步驱动）的契约联调（M14 §7.3、§7.6）。

覆盖：roster、`ctl/sim-core/estimate`、agent principal 的命令准入（K_entry 验签、隐式 AGENT 租约）与 `cmd.*` 结果事件、
StateRing 行缓存与健康推导、伪造 principal 被生产者拒绝（115）、agent 发 safety_stop 被生产者防御性拒绝（115）。
`ctl/sim-core/lease` 对 agent 角色的 acquire/release（M14-to-M08 第 1 条，FX-SIM2 交付）：acquire(AGENT) 与 release(previous)。
"""

from __future__ import annotations

import asyncio
import secrets

import pytest

pytest.importorskip("awr.sim.runtime.main")

from awr.agent.anet_mock import identity as ID
from awr.agent.guard.llm_gateway import ScriptedCommander
from awr.agent.guard.pipeline import TrustedGuard
from awr.agent.runtime.bridge_sim import BusSimBridge
from awr.agent.runtime.clock import SimScheduler
from awr.agent.runtime.drone_agent import derive_health
from awr.contracts import LAYOUT_ID
from awr.runtime.bus import LocalBus
from awr.runtime.principal import derive_key
from awr.runtime.statering import LOSSY, LocalRing
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.runtime.config import SimConfig
from awr.sim.runtime.main import SimCore

UAV = "p600-01"


class Rig:
    def __init__(self) -> None:
        self.W = [1_000_000_000]
        self.secret = secrets.token_bytes(32)
        self.run_id = "rm14" + secrets.token_hex(3)
        ns = f"awr/test/{self.run_id}"
        self.path = f"/m14/{self.run_id}/state.sim-core"
        self.ring, _ = LocalRing.open_or_create(self.path, capacity=1024, slots=32, layout_id=LAYOUT_ID, id_base=0, id_count=1024)
        self.sim_bus = LocalBus.open("sim-core", namespace=ns)
        self.core = SimCore(SimConfig(run_id=self.run_id, n_vehicles=1, load_world=False, autoplay=True), self.sim_bus, self.ring,
                            secret=self.secret, wall_ns=lambda: self.W[0])
        self.core.start()
        self.ns = ns

    def step(self, n: int = 8) -> None:
        for _ in range(n):
            self.W[0] += 8 * TICK_NS
            self.core.iterate()

    def close(self) -> None:
        self.core.stop()
        self.sim_bus.close()
        LocalRing.remove(self.path)


@pytest.fixture
def rig():
    r = Rig()
    yield r
    r.close()


async def _drive(rig: Rig, until, *, max_iter: int = 20_000) -> None:
    for _ in range(max_iter):
        if until():
            return
        rig.step(1)
        await asyncio.sleep(0)


async def _call(rig: Rig, coro):
    fut = asyncio.ensure_future(coro)
    await _drive(rig, fut.done)
    return fut.result()


def _bridge(rig: Rig, loop) -> BusSimBridge:
    rig.step(80)  # 约 2.5 s【仿真】：Mock 生命周期 PENDING → STARTING → BOOTED → READY
    bus = LocalBus.open("agent-runtime", namespace=rig.ns, loop=loop)
    ring = LocalRing.attach(rig.path, expect_layout_id=LAYOUT_ID)
    ring.register(LOSSY, "agent-runtime")
    return BusSimBridge(bus, ring=ring, loop=loop)


def test_roster_estimate_rows(rig: Rig) -> None:
    async def main():
        br = _bridge(rig, asyncio.get_running_loop())
        roster = await _call(rig, br.refresh_roster())
        assert [e["id"] for e in roster] == [UAV]
        def ready() -> bool:
            br.pump()
            br.read_rows()
            r = br.vehicle_row(UAV)
            return r is not None and r.lifecycle == "READY"

        await _drive(rig, ready)
        row = br.vehicle_row(UAV)
        assert row is not None and row.agent_no == roster[0]["agent_no"]
        est = await _call(rig, br.estimate(UAV, [row.pos[0] + 30.0, row.pos[1], 20.0], 10.0, "thermal.imaging"))
        assert est["v"] == 1 and "feasible" in est and est.get("eta_s", 0) > 0
        return row

    row = asyncio.run(main())
    assert row.flight_state in ("DISARMED", "READY") and derive_health(row) in (None, "BATTERY_UNKNOWN")


def test_agent_command_admission_and_result(rig: Rig) -> None:
    async def main():
        loop = asyncio.get_running_loop()
        br = _bridge(rig, loop)
        await _call(rig, br.refresh_roster())
        sched = SimScheduler(0)
        g = TrustedGuard(bridge=br, k_entry=derive_key(rig.secret, rig.run_id, "entry"), sched=sched)
        aid = ID.aid("test", UAV)
        cid = g.next_cid(aid)
        msg = {"v": 1, "cid": cid, "op": "takeoff", "uav": UAV, "args": {"alt_m": 5.0}, "principal": g.principal(aid, cid),
               "lease": None, "t_wall_ns": 0, "epoch_seen": 1, "batch_id": None}
        adm = await _call(rig, br.command(msg))
        assert adm["status"] == "accepted", adm
        fut = asyncio.ensure_future(br.wait_result(cid, 60.0))

        def done() -> bool:
            br.pump()
            return fut.done()

        await _drive(rig, done)
        res = fut.result()
        assert res["status"] == "succeeded" and res["effect"]["status"] == "OK"
        forged = await _call(rig, br.command(ScriptedCommander.forged_command(UAV, "hover")))
        cid2 = g.next_cid(aid)
        stop = await _call(rig, br.command({**msg, "cid": cid2, "op": "safety_stop", "args": {}, "principal": g.principal(aid, cid2)}))
        await _drive(rig, lambda: br.read_rows() > 0)
        return forged, stop, br.vehicle_row(UAV)

    forged, stop, row = asyncio.run(main())
    assert forged["status"] == "rejected" and forged["code"] == 115
    assert stop["status"] == "rejected" and stop["code"] == 115
    assert row.owner == "AGENT"  # 隐式 acquire(AGENT)（M08 LeaseManager.check）


def test_agent_explicit_lease(rig: Rig) -> None:
    async def main():
        br = _bridge(rig, asyncio.get_running_loop())
        await _call(rig, br.refresh_roster())
        g = TrustedGuard(bridge=br, k_entry=derive_key(rig.secret, rig.run_id, "entry"), sched=SimScheduler(0))
        aid = ID.aid("test", UAV)
        cid = g.next_cid(aid)
        acq = await _call(rig, br.lease("acquire", UAV, g.principal(aid, cid), cid=cid))
        cid2 = g.next_cid(aid)
        rel = await _call(rig, br.lease("release", UAV, g.principal(aid, cid2), cid=cid2, return_to="previous"))
        return acq, rel

    acq, rel = asyncio.run(main())
    assert acq["status"] == "accepted" and acq["lease"]["owner"] == "AGENT", acq
    assert rel["status"] == "accepted" and rel["lease"]["owner"] == "NONE", rel
