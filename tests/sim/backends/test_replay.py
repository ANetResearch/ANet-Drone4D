"""ReplayBackend L0（M08-AC-031；M08-FR-068；ADR-020）。

- 加载 SIH 黄金数据 inst1（reposition 段，`LOCAL_POSITION_NED` + `ATTITUDE`）：在原始采样时刻的插值位置误差 ≤ 1e-6 m；
- 插值后速度连续：相邻 L1 tick 速度差 ≤ 0.5 m/s；姿态四元数单位且与原始 ATTITUDE 一致（采样时刻）；
- 任何命令得 109（后端 dispatch 与 sim-core 准入第 ⑦ 步）；
- 暂停与单步下与世界时钟一致：暂停期间位置不变，单步 n 个 tick 后位置等于轨迹在 `t_world − t0` 处的插值。
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts.reasons import Reason
from awr.sim.backends.base import EntitySpec, Kind
from awr.sim.backends.replay import ReplayBackend, load_sih_csv
from awr.sim.fleet.fleet import FleetConfig, FleetSim
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import registry as R

GOLD = Path(__file__).resolve().parents[2] / "golden" / "sih_x500_px4-1.18rc1"


def _mark(name: str) -> float:
    with open(GOLD / "marks.csv", encoding="utf-8") as fh:
        for r in csv.reader(fh):
            if r[0] == name:
                return float(r[1])
    raise KeyError(name)


@pytest.fixture(scope="module")
def track():
    return load_sih_csv(GOLD / "inst1.csv", t0_wall=_mark("reposition"), duration_s=25.0)


def _fleet():
    ctx = R.isolated_registry()
    reg = ctx.__enter__()
    f = FleetSim(FleetConfig(path_capacity=1024), reg=reg)
    f.build_pipeline()
    return f, ctx


def test_samples_exact_and_velocity_continuous(track) -> None:
    f, ctx = _fleet()
    try:
        be = ReplayBackend(f)
        v = be.spawn(EntitySpec("ghost-1", Kind.UAV, "x500", None, (0.0, 0.0, 0.0), 0.0, source={"track": track}))
        k = f.kinematic
        t0 = k.t0[v.slot]
        err = 0.0
        for i in range(0, len(track.t_s), 7):
            _, pos, vel, q, _ = k.sample(t0 + float(track.t_s[i]))
            err = max(err, float(np.abs(pos[0] - track.pos[i]).max()))
            assert np.allclose(q[0], track.q_xyzw[i], atol=1e-9) or np.allclose(q[0], -track.q_xyzw[i], atol=1e-9)
        assert err <= 1e-6, err
        S = f.S
        prev = None
        dmax = 0.0
        for _ in range(int(20.0 / 0.008)):
            be.step(be.ctx.tick + 2)
            vel = S.enu.vel[v.slot].copy()
            if prev is not None:
                dmax = max(dmax, float(np.linalg.norm(vel - prev)))
            prev = vel
            assert abs(np.linalg.norm(S.enu.q_xyzw[v.slot]) - 1.0) < 1e-9
        assert dmax <= 0.5, dmax
        assert v.derive_state()["pose_src"] == "KINEMATIC"
        res = be.dispatch_batch([{"op": op, "slots": [v.slot], "args": {}} for op in ("goto", "hover", "rtl", "land")])
        assert {r.code for r in res} == {int(Reason.BACKEND_UNSUPPORTED)}
    finally:
        ctx.__exit__(None, None, None)


def test_core_commands_109_and_clock_consistency(track) -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg)
        try:
            vid = h.core.add_vehicle("x500", (80.0, 0.0, 0.0), 0.0, vehicle_id="ghost-1", backend="replay", track=track)
            assert h.until(lambda: h.core.roster.resolve(vid).lifecycle == 4, 3.0)
            for op, args in (("goto", {"pos": [0.0, 0.0, 10.0]}), ("hover", {}), ("rtl", {}), ("land", {}),
                             ("takeoff", {"alt_m": 5.0})):
                adm = h.cmd(op, args, uav=vid)
                assert adm["status"] == "rejected" and adm["code"] == int(Reason.BACKEND_UNSUPPORTED), (op, adm)
            s = h.slot(vid)
            k = h.core.fleet.kinematic
            assert h.clock("pause")["code"] == 0
            p0 = h.pos(vid)
            for _ in range(100):
                h.W[0] += 10 * TICK_NS
                h.core.iterate()
            assert np.array_equal(h.pos(vid), p0)  # 暂停：世界时钟冻结，幽灵机不动
            assert h.clock("step", {"ticks": 26})["code"] == 0
            while h.core.clock.state != 2:
                h.W[0] += TICK_NS
                h.core.iterate()
            t_world = h.core.clock.t_ns * 1e-9
            _, pos, _, _, _ = k.sample(t_world)
            # kinematic 每 2 tick 更新：单步结束于 tap/kinematic tick 时与世界时钟处的插值一致
            assert h.core.clock.tick % 2 == 0
            assert np.allclose(h.pos(vid), pos[list(k._slots).index(s)], atol=1e-9)
        finally:
            h.close()
