"""tap 与发布、state_ext（M08-AC-033、AC-034 的 sim 部分；M08-FR-006、FR-049 至 FR-052；D1-AC-13）。

- StateRing 中 Full64 行的 pos、vel、q、omega 等于 ENU 视图（float32 量化），ENU 视图与 M02 `ned_frd_to_enu_flu_batch`
  逐元素相等，并与 M02 golden（`packages/contracts/golden/frames/enu_ned.json`）满足混合容差；Lite32 等于契约生成物
  `lite_from_full(Full64)`（TS 端生成解码器的往返由 `make test-contracts` 覆盖）；
- `flight_state`、`flags`、`ctrl` 与 fuser 合成值一致（SafetyStop 黄金向量见 test_lease.py）；
- 快进（rate 10）时 tap 按墙钟 ≥ 4 ms 节流、批内最后一个 tick 必发布且置 FASTFWD；rate 1 时不置；
- `state/sim-core/ext` 载荷符合 `rt/payloads/uav_state_ext.schema.json`，含 §7.5 全部字段；px4 段与 `mock_emulate_px4` 一致；
- N = 1000 发布 p99 ≤ 300 µs 属 perf 用例（并行阶段不跑）。
"""

from __future__ import annotations

import json
import math
from functools import cache
from pathlib import Path

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts import LAYOUT_ID
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32, lite_from_full
from awr.runtime.statering import SLOT_FASTFWD, LocalRing
from awr.sim.core import state_model as SM
from awr.sim.fleet.stages import registry as R
from awr.sim.fleet.state import FleetState
from awr.world.georef import frames as F

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "packages" / "contracts"


@cache
def _validator(rel: str):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    reg = Registry()
    for p in CONTRACTS.rglob("*.schema.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        if "$id" in d:
            reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    return Draft202012Validator(json.loads((CONTRACTS / rel).read_text(encoding="utf-8")), registry=reg)


@pytest.fixture
def h():
    with R.isolated_registry() as reg:
        x = CoreHarness(n=3, reg=reg, spacing=20.0)
        try:
            yield x
        finally:
            x.close()


def _frame(h: CoreHarness):
    return LocalRing.attach(h.path, expect_layout_id=LAYOUT_ID).read_latest(0)


def test_full64_matches_enu_view_and_lite(h: CoreHarness) -> None:
    h.takeoff(10.0)
    p = h.pos()
    h.cmd("goto", {"pos": [p[0] + 30.0, p[1] + 10.0, p[2] + 3.0]})
    h.advance(2.0)
    assert h.clock("pause")["code"] == 0
    assert h.clock("step", {"ticks": 3})["code"] == 0  # 单步结束于奇数 tick 也补发一次（C04）
    h.W[0] += 4_000_000
    h.core.iterate()
    f = _frame(h)
    full = np.frombuffer(f.full, DRONE_STATE64)
    lite = np.frombuffer(f.lite, SWARM_LITE32)
    S = h.S
    idx = np.flatnonzero(S.active)
    assert f.n_rows == idx.size == 3 and f.t_sim_ns == S.t_ns
    assert full["agent_no"].tolist() == S.agent_no[idx].tolist()  # 行序按 slot 升序
    for fld, view in (("pos", S.enu.pos), ("vel", S.enu.vel), ("q", S.enu.q_xyzw), ("omega", S.enu.omega_flu)):
        assert np.array_equal(full[fld], view[idx].astype(np.float32)), fld
    assert np.array_equal(lite, lite_from_full(full.copy()))
    assert not (f.flags & SLOT_FASTFWD)
    # 与 M02 的唯一换算实现逐元素相等
    n = idx.size
    op, ov, oq = np.empty((n, 3)), np.empty((n, 3)), np.empty((n, 4))
    F.ned_frd_to_enu_flu_batch(S.p[idx].copy(), S.v[idx].copy(), S.q[idx].copy(), op, ov, oq)
    assert np.array_equal(op, S.enu.pos[idx]) and np.array_equal(ov, S.enu.vel[idx]) and np.array_equal(oq, S.enu.q_xyzw[idx])


def test_enu_view_vs_m02_golden() -> None:
    g = json.loads((CONTRACTS / "golden/frames/enu_ned.json").read_text(encoding="utf-8"))
    tol = g["tolerance"]
    qc = [c for c in g["cases"] if c["fn"] == "q_enuflu_from_nedfrd"]
    vc = [c for c in g["cases"] if c["fn"] == "enu_to_ned"]
    n = max(len(qc), len(vc))
    S = FleetState(n)
    S.active[:n] = True
    S.q[:] = (1.0, 0.0, 0.0, 0.0)
    S.q[:len(qc)] = [c["args"]["q_wxyz"] for c in qc]
    ned = np.array([c["out"]["v"] for c in vc])  # enu_to_ned 的逆：视图应还原 args
    S.p[:len(vc)] = ned
    S.touch()
    q = S.enu.q_xyzw[:len(qc)]
    exp_q = np.array([c["out"]["q_xyzw"] for c in qc])
    same = np.minimum(np.abs(q - exp_q).max(1), np.abs(q + exp_q).max(1))  # 四元数双覆盖
    assert (same <= tol["atol"]["dimensionless"] + tol["rtol"]).all()
    pos = S.enu.pos[:len(vc)]
    exp_p = np.array([c["args"]["v"] for c in vc])
    assert (np.abs(pos - exp_p) <= tol["atol"]["position_m"] + tol["rtol"] * np.abs(exp_p)).all()


def test_fastforward_throttle_and_flag(h: CoreHarness) -> None:
    tap = h.core.fleet.tap
    assert h.clock("speed", {"rate": 10.0})["code"] == 0
    pub0, skip0 = tap.published, tap.skipped
    t0 = h.core.clock.tick
    for _ in range(50):
        h.W[0] += 4_000_000  # 每轮墙钟 4 ms：到期 10 个 tick，其中 5 次 tap 调用
        h.core.iterate()
    ticks = h.core.clock.tick - t0
    assert ticks >= 400
    assert tap.published - pub0 <= 51  # 每 4 ms 墙钟至多一次
    assert tap.skipped - skip0 >= 150
    assert _frame(h).flags & SLOT_FASTFWD
    assert h.clock("speed", {"rate": 1.0})["code"] == 0
    h.advance(0.1, wall_step_ticks=1)
    assert not (_frame(h).flags & SLOT_FASTFWD)


def test_state_ext_schema_and_px4_emulation(h: CoreHarness) -> None:
    h.takeoff(10.0)
    items = h.core.state_ext_items()
    assert len(items) == 3
    v = _validator("rt/payloads/uav_state_ext.schema.json")
    sb = h.S.blocks["safety"]
    for agent_no, ext in items:
        errs = [f"{list(e.absolute_path)}: {e.message}" for e in v.iter_errors(ext)]
        assert not errs, errs
        assert {"lifecycle", "lease", "loc", "battery", "mission", "accel_mps2", "home_enu_m", "frames", "link",
                "gcs_loss_policy", "px4"} <= set(ext)
        s = h.core.agent_slot[agent_no]
        px = SM.mock_emulate_px4(SM.FS(int(sb["fs"][s])), int(sb["sub"][s]), SM.Intent())
        assert ext["px4"]["custom_mode"] == px.custom_mode and ext["px4"]["landed_state"] == px.landed
        assert all(math.isfinite(x) for x in ext["accel_mps2"])


@pytest.mark.perf
def test_publish_p99_n1000() -> None:
    """N = 1000 发布 p99 ≤ 300 µs（tap stage 计时，M08-AC-033；perf 用例）。"""
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1000, reg=reg, spacing=8.0, ready=False)
        try:
            for _ in range(600):
                h.W[0] += 2 * 4_000_000
                h.core.iterate()
            assert h.core.fleet.pipeline.p99_us("tap") <= 300.0
        finally:
            h.close()
