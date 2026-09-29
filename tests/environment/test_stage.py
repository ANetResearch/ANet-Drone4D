"""env stage 与插件（M07-FR-017、FR-019、FR-022–FR-024；M07-AC-008 功能部分、M07-AC-010、M07-AC-014 模块部分）。

- FleetSim（tests/sim/simlib.Rig，隔离登记表）+ 本模块 env stage：风以 NED 空气速度写入 S.wind，rho、flags、gust 写入；
  关键帧事件；8 m/s 均匀廓线下 x500 悬停出现稳态倾角（风只经相对空速进入动力学，由 M08 施加）。
- SimCore（LocalBus + LocalRing）：插件登记后 pipeline 构建通过（sumbudget、order 区段、一字段一写者），心跳发布、
  env/query 经查询路由、EnvSample32 detail 消息。
"""

from __future__ import annotations

import importlib
import math
import secrets
import sys
from pathlib import Path

import msgpack
import numpy as np
import pytest

from awr.contracts.layouts import ENV_SAMPLE32

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "sim"))


@pytest.fixture()
def stage_mod():
    import awr.environment.stage as st

    st.reset_plugin_state()
    yield st
    st.reset_plugin_state()


def _rig(stage_mod, n: int = 2):
    from simlib import Rig

    return Rig("x500", n=n, env=stage_mod.env_stage)


def test_stage_writes_fleet_state(stage_mod):
    rig = _rig(stage_mod)
    try:
        rig.air([0, 1], 30.0, settle_s=1.0)
        env = stage_mod.service()
        assert env is not None and rig.ctx.env is env
        S = rig.S
        # 深圳 clear：3 m/s 来自 270（西风）-> ENU 去向 +E；NED = (N, E, -U)
        w_enu = env.rows["wind"][:2].astype(np.float64)  # 本 env tick 的查询结果（零阶保持到下一个 tick）
        assert np.allclose(S.wind[:2, 1], w_enu[:, 0], atol=1e-5) and np.allclose(S.wind[:2, 0], w_enu[:, 1], atol=1e-5)
        assert np.allclose(S.wind[:2, 2], -w_enu[:, 2], atol=1e-5)
        assert np.all(S.wind[:2, 1] > 1.0)
        assert np.all(S.rho[:2] > 1.1) and np.all(S.env_flags[:2] & 1)
        kinds = rig.events.kinds()
        assert "env.keyframe" in kinds
        kf = next(kw for k, kw in rig.events.items if k == "env.keyframe")
        assert kf["fields"]["schema"] == "awr.env.keyframe.v1" and kf["fields"]["version"] == 1
        assert env.row_valid[0] and env.rows[0]["flags"] & 1
    finally:
        rig.close()


def test_wind_tilts_hovering_vehicle(stage_mod):
    """8 m/s 均匀廓线：悬停稳态俯仰明显非零且位置保持（g08 §9.3 为 −7.85°，精确回归属 M08 tests/sim）。"""
    from awr.environment.keyframe import EnvOp

    rig = _rig(stage_mod, n=1)
    try:
        rig.air([0], 30.0, settle_s=0.5)
        env = stage_mod.service()
        env.apply(EnvOp("set", patch={"wind": {"speed_ref_mps": 8.0, "dir_from_deg": 270.0}, "config": {"wind": {
            "profile": {"kind": "uniform"}, "turbulence": {"model": "off"}}}}, duration_s=0), rig.ctx.tick + 1)
        rig.step_s(12.0)
        S = rig.S
        assert abs(S.wind[0, 1] - 8.0) < 1e-9 and abs(S.wind[0, 0]) < 1e-9
        q = S.enu.q_xyzw[0]
        x, y, z, w = q
        bz = np.array([2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)])
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, bz[2]))))
        assert tilt > 3.0
        assert bz[0] < 0  # 推力向量倾向上风（西，-E），抵抗吹向 +E 的风
    finally:
        rig.close()


def test_stage_determinism(stage_mod):
    outs = []
    for _ in range(2):
        stage_mod.reset_plugin_state()
        rig = _rig(stage_mod, n=2)
        try:
            rig.air([0, 1], 40.0, settle_s=0.2)
            from awr.environment.keyframe import EnvOp

            stage_mod.service().apply(EnvOp("preset", name="thunderstorm", duration_s=5), rig.ctx.tick + 1)
            rig.step_s(8.0)
            outs.append((rig.S.wind[:2].copy(), rig.S.env_gust[:2].copy(), stage_mod.service().kf.encode()))
        finally:
            rig.close()
    assert np.array_equal(outs[0][0], outs[1][0]) and np.array_equal(outs[0][1], outs[1][1]) and outs[0][2] == outs[1][2]


def test_handle_command_codes(stage_mod):
    rig = _rig(stage_mod, n=1)
    try:
        rig.step_s(0.1)
        hc = stage_mod.handle_command
        tick = rig.ctx.tick + 1
        env = stage_mod.service()
        v0 = env.kf.version
        cases = [({"op": "env/set", "args": {"patch": {"wind": {"speed_ref_mps": 99}}}}, 110),
                 ({"op": "env/set", "args": {"patch": {"wind": {"speed_ref_mps": float("nan")}}}}, 110),
                 ({"op": "env/set", "args": {"patch": {"wind": {"nope": 1}}}}, 441),
                 ({"op": "env/preset", "args": {"name": "tornado"}}, 440),
                 ({"op": "env/preset", "args": {"name": "rain", "duration_s": 601}}, 110),
                 ({"op": "env/set", "args": {"patch": {"config": {"wind": {"level": 3}}}}}, 442),
                 ({"op": "env/set", "args": {"patch": {"cloud": {"base_m": 2450}}}}, 110)]
        for msg, code in cases:
            r = hc(msg, tick, rig.ctx)
            assert r["status"] == "rejected" and r["code"] == code, (msg, r)
        assert env.kf.version == v0 and not env.pending
        ok = hc({"op": "env/preset", "args": {"name": "rain"}}, tick, rig.ctx)
        assert ok["status"] == "accepted" and ok["result"]["version"] == v0 + 1
        rig.step_s(0.1)
        assert env.kf.version == v0 + 1 and env.kf.to_preset == "rain"
    finally:
        rig.close()


def test_detail_message_layout(stage_mod):
    rig = _rig(stage_mod, n=2)
    try:
        rig.air([0, 1], 30.0, settle_s=0.3)
        env = stage_mod.service()
        raw = stage_mod.detail_message(env, rig.S, np.array([1], np.uint16), rig.ctx.t_ns)
        m = msgpack.unpackb(raw, raw=False)
        assert m["v"] == 1 and m["agent_no"] == [1] and len(m["rows"]) == 32
        r = np.frombuffer(m["rows"], ENV_SAMPLE32)[0]
        assert abs(float(r["wind"][0]) - rig.S.wind[1, 1]) < 1e-5
        assert int(r["flags"]) & 1 and 1.0 < float(r["rho"]) * 3e-5 < 1.3
    finally:
        rig.close()


def test_sim_core_plugin_integration(stage_mod):
    """SimCore 组合：插件登记、pipeline 校验、心跳（state/sim-core/env）、env/query 查询路由。"""
    from awr.contracts import LAYOUT_ID, bus_keys
    from awr.runtime.bus import LocalBus
    from awr.runtime.statering import LocalRing
    from awr.sim.fleet.stages import registry as R
    from awr.sim.runtime.config import SimConfig
    from awr.sim.runtime.main import SimCore

    with R.isolated_registry() as reg:
        importlib.reload(stage_mod)
        stage_mod.reset_plugin_state()
        assert "env" in reg.stage_names() and "env/query" in reg.queries
        assert {t.name for t in reg.slow} >= {"env.heartbeat", "env.detail"}
        run_id = "renv" + secrets.token_hex(3)
        path = f"/envt/{run_id}/state.sim-core"
        ring, _ = LocalRing.open_or_create(path, capacity=1024, slots=32, layout_id=LAYOUT_ID, id_base=0, id_count=1024)
        bus = LocalBus.open("sim-core", namespace=f"awr/test/{run_id}")
        got: list[bytes] = []
        bus.subscribe(bus_keys.STATE_ENV, lambda k, raw: got.append(raw))
        W = [1_000_000_000]
        core = SimCore(SimConfig(run_id=run_id, n_vehicles=1, load_world=False, autoplay=True), bus, ring, wall_ns=lambda: W[0], reg=reg)
        try:
            core.start()
            for _ in range(400):
                W[0] += 8 * 4_000_000
                core.iterate()
            assert core.ctx.env is stage_mod.service()
            from awr.environment.keyframe import decode

            assert got, "no heartbeat published"
            hb = decode(got[-1])
            assert hb.version >= 1 and hb.world_id
            rep = stage_mod.query_route({"op": "env/query", "args": {"points": [[0, 0, 50]]}}, core.ctx)
            assert rep["code"] == 0 and rep["n"] == 1
        finally:
            core.stop()
            bus.close()
            LocalRing.remove(path)
