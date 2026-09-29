"""后端一致性套件（M08-AC-032；M08-FR-065、FR-066、FR-068、FR-072；P-07）。

对任一 SimBackend 参数化运行（D1：Mock、Replay；V0.2 起追加 SIH）：
- 协议形状：`SimBackend`、`EntityAdapter`、`DroneAdapter`（runtime_checkable）；caps 从 `rt/caps/<backend>.json` 加载；
- spawn → 视图 READY、`pose()` 为 ENU/FLU 且时刻等于世界时钟；`step(tick)` lockstep 推进；
- `dispatch_batch`：Mock 同 tick 原生确认（hover、goto）；Replay 一律 109；
- `snapshot()` / `restore()` 往返后继续推进与不中断一致；`despawn` 后视图不再活动。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from awr.contracts.reasons import Reason
from awr.sim.backends.base import DroneAdapter, EntityAdapter, EntitySpec, Kind, Pose, SimBackend
from awr.sim.backends.mock import MockBackend
from awr.sim.backends.replay import ReplayBackend, load_traj
from awr.sim.fleet.fleet import FleetConfig, FleetSim
from awr.sim.fleet.stages import registry as R

GOLD = Path(__file__).resolve().parents[2] / "golden" / "sih_x500_px4-1.18rc1"


def _traj(tmp: Path) -> Path:
    t = np.linspace(0.0, 20.0, 201)
    rows = ["t_s,e_m,n_m,u_m,qx,qy,qz,qw"] + [f"{x:.3f},{5 * x:.6f},{2 * np.sin(x):.6f},{10 + 0.1 * x:.6f},0,0,0,1"
                                               for x in t]
    p = tmp / "traj.csv"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


def _backend(name: str, tmp: Path):
    reg_ctx = R.isolated_registry()
    reg = reg_ctx.__enter__()
    f = FleetSim(FleetConfig(path_capacity=4096), reg=reg)
    f.build_pipeline()
    be = MockBackend(f) if name == "mock" else ReplayBackend(f)
    src = None if name == "mock" else {"traj": str(_traj(tmp))}
    spec = EntitySpec(None, Kind.UAV, "x500", None, (0.0, 0.0, 0.0), 0.0, source=src)
    return be, spec, reg_ctx


@pytest.fixture(params=["mock", "replay"])
def env(request, tmp_path):
    be, spec, ctx = _backend(request.param, tmp_path)
    try:
        yield request.param, be, spec
    finally:
        ctx.__exit__(None, None, None)


def test_protocol_shape_and_caps(env) -> None:
    name, be, spec = env
    assert isinstance(be, SimBackend) and be.name == name and be.caps.backend == name
    assert be.caps.clock.mode == "lockstep" and be.caps.clock.pausable
    be.attach(None, None, None, None)
    v = be.spawn(spec)
    assert isinstance(v, EntityAdapter) and isinstance(v, DroneAdapter)
    assert v.kind == Kind.UAV and v.lifecycle() == "READY" and isinstance(v.agent_no, int)
    be.step(50)
    p = v.pose()
    assert isinstance(p, Pose) and p.t_sim_ns == 50 * 4_000_000
    assert p.pos.shape == (3,) and p.q_xyzw.shape == (4,) and abs(np.linalg.norm(p.q_xyzw) - 1) < 1e-9
    st = v.derive_state()
    assert {"fs", "sub", "pose_src"} <= set(st)
    assert st["pose_src"] == ("TRUTH" if name == "mock" else "KINEMATIC")


def test_dispatch_semantics(env) -> None:
    name, be, spec = env
    v = be.spawn(spec)
    be.step(10)
    res = be.dispatch_batch([{"op": "hover", "slots": [v.slot], "args": {}},
                             {"op": "goto", "slots": [v.slot], "args": {"pos": [5.0, 5.0, 5.0]}}])
    assert len(res) == 2 and all(r.entity_id == v.id for r in res)
    if name == "mock":
        assert all(r.ok and r.native_ack and r.code == 0 for r in res)
    else:
        assert all((not r.ok) and r.code == int(Reason.BACKEND_UNSUPPORTED) for r in res)


def test_snapshot_restore_roundtrip(env) -> None:
    name, be, spec = env
    v = be.spawn(spec)
    if name == "mock":
        be.dispatch_batch([{"op": "takeoff", "slots": [v.slot], "args": {"alt_m": 5.0}}])
    be.step(500)
    blob = be.snapshot()
    be.step(1000)
    ref = v.pose()
    be.restore(blob)
    assert be.ctx.tick == 500
    be.step(1000)
    got = v.pose()
    assert got.t_sim_ns == ref.t_sim_ns
    assert np.allclose(got.pos, ref.pos, atol=1e-9) and np.allclose(got.q_xyzw, ref.q_xyzw, atol=1e-9)


def test_despawn(env) -> None:
    _name, be, spec = env
    v = be.spawn(spec)
    be.step(4)
    be.despawn(v.id)
    assert not be.fleet.S.active[v.slot]
    assert load_traj is not None and GOLD.exists()
