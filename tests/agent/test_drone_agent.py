"""M14-AC-009、022：健康推导与 task.quote；thermal.imaging 两段式处理器（fake SimBridge）。"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from fakes.s3 import build_s3, run, start

from awr.agent.runtime.drone_agent import THERMAL_FRAME_BYTES, derive_health
from awr.agent.runtime.types import EffectStatus

HEALTH_CASES = [("fs", "UNKNOWN", "FS_UNKNOWN"), ("fs", "ELAND", "FS_ELAND"), ("fs", "FAILSAFE", "FS_FAILSAFE"),
                ("fs", "CRASHED", "FS_CRASHED"), ("lifecycle", "DEGRADED", "LIFECYCLE_DEGRADED"),
                ("owner", "OPERATOR", "LEASE_OPERATOR"), ("owner", "PILOT", "LEASE_PILOT"), ("battery", 255, "BATTERY_UNKNOWN")]


def _quote(core, fake, sched, vid: str, cap: str = "thermal.imaging"):
    async def main():
        await start(core, fake)
        aid = core.by_vehicle[vid]
        ix = await core.net.delegate(core.coord_aid, aid, "task.quote",
                                     {"capability": cap, "target_enu_m": [-24.0, -1253.0, None], "args": {}}, task_id="T-q")
        fut = asyncio.ensure_future(core.net.result(core.coord_aid, ix, 3.0))
        await run(sched, fake, t_end_s=sched.now_s() + 3.5, until=fut.done)
        return fut.result()

    return asyncio.run(main())


@pytest.mark.parametrize("field,value,reason", HEALTH_CASES, ids=[c[2] for c in HEALTH_CASES])
def test_unhealthy_quote_unavailable(field: str, value, reason: str) -> None:
    sched, fake, core = build_s3()
    v = fake.vehicles["p600-b2"]
    if field == "fs":
        orig = fake.vehicle_row

        def row(vid: str):
            r = orig(vid)
            return r if vid != "p600-b2" else r.__class__(**{**r.__dict__, "flight_state": value})

        fake.vehicle_row = row  # type: ignore[method-assign]
    elif field == "lifecycle":
        v.lifecycle = value
    elif field == "owner":
        v.owner = value
    else:
        v.soc = -1.0
        orig2 = fake.vehicle_row

        def row2(vid: str):
            r = orig2(vid)
            return r if vid != "p600-b2" else r.__class__(**{**r.__dict__, "battery_pct": 255})

        fake.vehicle_row = row2  # type: ignore[method-assign]
    res = _quote(core, fake, sched, "p600-b2")
    assert res.effect.status is EffectStatus.UNAVAILABLE and res.effect.message == reason


def test_healthy_and_x500_quote_ok() -> None:
    sched, fake, core = build_s3()
    res = _quote(core, fake, sched, "p600-b2")
    assert res.effect.status is EffectStatus.OK and res.effect.metrics["feasible"] == 1.0
    assert 0.7 < res.effect.metrics["conf_expected"] < 0.85
    # x500（battery = null，battery_pct 恒 255）不据此判不健康
    row = fake.vehicle_row("p600-b2")
    x = row.__class__(**{**row.__dict__, "battery_pct": 255, "has_battery": False, "profile_id": "x500"})
    assert derive_health(x) is None and derive_health(x, has_battery=True) == "BATTERY_UNKNOWN"


def test_thermal_handler_two_stage() -> None:
    sched, fake, core = build_s3()
    fake.vehicles["p600-b1"].mission_path = [(-24.0, -1300.0, 80.0)]

    async def main():
        await start(core, fake)
        await run(sched, fake, t_end_s=60.0)  # b1 起飞并飞往待命点
        aid = core.by_vehicle["p600-b1"]
        ix = await core.net.delegate(core.coord_aid, aid, "thermal.imaging",
                                     {"target_enu_m": [-24.0, -1253.0, None], "target_id": "t1", "dwell_s": 10, "alt_agl_m": 60,
                                      "orbit_radius_m": 20}, task_id="T-0042")
        ups = []

        async def reader():
            ups.extend([u async for u in core.net.updates(core.coord_aid, ix)])

        rt = asyncio.ensure_future(reader())
        await run(sched, fake, t_end_s=sched.now_s() + 200.0, until=rt.done)
        un = await core.net.delegate(core.coord_aid, aid, "lidar.mapping", {}, task_id="T-0043")
        fut = asyncio.ensure_future(core.net.result(core.coord_aid, un, 5.0))
        await run(sched, fake, t_end_s=sched.now_s() + 3.0, until=fut.done)
        return ups, fut.result(), aid

    ups, unserved, aid = asyncio.run(main())
    first, final = ups[0], ups[-1]
    assert first.kind == "first" and first.effect.status is EffectStatus.UNVERIFIED and first.effect.metrics["eta_s"] > 0
    phases = [u.phase.value for u in ups if u.kind == "progress"]
    assert phases == ["lease", "enroute", "on_station", "executing", "returning"]
    e = final.effect
    assert final.receipt_ok and e.status is EffectStatus.OK and e.verify_trust == 4 and e.simulated
    assert e.metrics["confidence"] == 0.9 and e.metrics["detections"] >= 1
    assert all(a.path.startswith("thermal/T-0042/") and a.path.endswith(".pgm") and a.size_bytes == THERMAL_FRAME_BYTES
               for a in e.artifacts) and e.artifacts
    tests = {t["id"]: t["status"] for t in final.records["tests"]}
    assert tests == {"station_reached": 1, "dwell_complete": 1}
    ops = [op for _t, uav, op, _c in fake.cmd_log if uav == "p600-b1"]
    assert ops[:1] == ["goto"] and ops[-1] == "hover" and "orbit" in ops
    lease_ev = [r["type"] for r in core.ledger(aid).rows if r["type"].startswith("agent.lease")]
    assert lease_ev == ["agent.lease.acquired", "agent.lease.released"]
    assert fake.vehicles["p600-b1"].owner == "MISSION"  # previous 弹栈恢复 MISSION
    assert unserved.effect.status is EffectStatus.UNAVAILABLE and unserved.effect.message == "not served"  # 未注册：daemon 解析失败


def test_declared_capability_not_served_in_d1() -> None:
    import copy

    from fakes.s3 import S3_AGENTS

    agents = copy.deepcopy(S3_AGENTS)
    agents["members"][2]["capabilities"] = ["thermal.imaging", "lidar.mapping", "flight.goto"]
    sched, fake, core = build_s3(agents=agents)

    async def main():
        await start(core, fake)
        aid = core.by_vehicle["p600-b2"]
        out = []
        for cap in ("lidar.mapping", "flight.goto"):
            ix = await core.net.delegate(core.coord_aid, aid, cap, {}, task_id="T-0050")
            fut = asyncio.ensure_future(core.net.result(core.coord_aid, ix, 5.0))
            await run(sched, fake, t_end_s=sched.now_s() + 3.0, until=fut.done)
            out.append(fut.result())
        return out

    for r in asyncio.run(main()):
        assert r.effect.status is EffectStatus.UNAVAILABLE and r.effect.message == "not served in D1" and r.receipt_ok


def test_artifact_size_matches_m13_renderer() -> None:
    try:
        mod = importlib.import_module("awr.sim.sensors.thermal_mock")
    except ImportError:
        pytest.skip("M13 render_thermal_frame 未交付（awr.sim.sensors.thermal_mock）")
    params = {"w": 160, "h": 120, "u": 80, "v": 60, "size_px": 1.6, "t_bg_c": 12.0, "t_tgt_c": 34.0, "tau": 0.99, "netd_k": 0.05,
              "seed": "12345"}
    assert len(mod.render_thermal_frame(params)) == THERMAL_FRAME_BYTES
