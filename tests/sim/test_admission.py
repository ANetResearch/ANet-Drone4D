"""准入 ④–⑩ 的原因码优先级（M08-AC-025；M08-FR-054、FR-055；AWR-12 §5.1.2；M08 §6.10.2）。

每一步构造一例，并与后一步的违例叠加，断言先报前一步的码：
④ 状态：108 生命周期 → 117 时钟 → 114 锁 → 101/105 准入矩阵 → 113 定位 → 登记在第 4 步的检查（103）；
⑤ 租约：116（第二个 operator）；⑥ 参数边界：110；⑦ 后端能力：109（caps 无该命令）；⑧ 围栏粗校验（登记的检查，102）；
⑧ 细校验（`needs_fine`）：准入 accepted，细校验失败后调用 failed 102 并悬停；⑨ 分发：apply_tick = tick + 1；⑩ 审计写入。
同步准入 p99 ≤ 5 ms（50 条/s 混合，perf 标记，并行阶段不跑）。
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts.enums import FlightState, Lifecycle
from awr.contracts.reasons import Reason
from awr.sim.core.admission import AdmitResult
from awr.sim.fleet import actions as ACT
from awr.sim.fleet import kernels_l1 as K
from awr.sim.fleet.stages import registry as R

BAD_SPEED = {"speed_mps": 99.0}


class Checks:
    """登记在第 4、8 步的桩检查（M09 预检与围栏的替身）。"""

    def __init__(self) -> None:
        self.pre = False
        self.fence = False
        self.fine = False

    def step4(self, req, ctx):
        return AdmitResult(int(Reason.PREFLIGHT_FAILED), {"check": "stub"}) if self.pre and req.op == "goto" else None

    def step8(self, req, ctx):
        if req.op != "goto":
            return None
        if self.fence:
            return AdmitResult(int(Reason.GEOFENCE_REJECT), {"zone": "stub"})
        return AdmitResult(0, needs_fine=self.fine)


@pytest.fixture
def env():
    with R.isolated_registry() as reg:
        ck = Checks()
        R.register_admission_check(4, "stub_pre", ck.step4, owner="M09")
        R.register_admission_check(8, "stub_fence", ck.step8, owner="M09")
        h = CoreHarness(n=1, reg=reg)
        try:
            h.takeoff(10.0)
            yield h, ck
        finally:
            h.close()


def _goto(h: CoreHarness, dy: float = 10.0, **kw) -> dict:
    p = h.pos()
    args = {"pos": [p[0], p[1] + dy, p[2]]}
    args.update({k: v for k, v in kw.items() if k in ("speed_mps", "tol_m")})
    pk = {k: v for k, v in kw.items() if k in ("pid", "role", "seat")}
    return h.cmd("goto", args, **pk)


def test_step4_lifecycle_before_lease_and_params(env) -> None:
    h, _ = env
    s = h.slot()
    lc = int(h.S.lifecycle[s])
    h.S.lifecycle[s] = int(Lifecycle.LOST)
    assert _goto(h, pid="p-op2", **BAD_SPEED)["code"] == int(Reason.LINK_ERROR)
    h.S.lifecycle[s] = lc


def test_step4_clock_and_lock(env) -> None:
    h, _ = env
    assert h.clock("speed", {"rate": 2.0})["code"] == 0
    assert h.cmd("velocity", {"frame": "world", "vmax_mps": -1}, pid="p-op2")["code"] == int(Reason.CLOCK_CONSTRAINT)
    assert h.clock("speed", {"rate": 1.0})["code"] == 0
    assert h.cmd("safety_stop", {})["status"] == "accepted"
    h.advance(1.0)
    assert _goto(h, pid="p-op2", **BAD_SPEED)["code"] == int(Reason.LOCKED)


def test_step4_matrix_101_and_105(env) -> None:
    h, _ = env
    s = h.slot()
    assert h.cmd("takeoff", {"alt_m": 5.0}, pid="p-op2")["code"] in (int(Reason.STATE), int(Reason.DUPLICATE))
    ACT.begin_eland(h.S, [s], 0.5, h.t)
    h.advance(0.05)
    assert int(h.S.blocks["safety"]["fs"][s]) == int(FlightState.ELAND)
    assert _goto(h, pid="p-op2", **BAD_SPEED)["code"] == int(Reason.SAFETY_ACTIVE)


def test_step4_loc_and_registered_check(env) -> None:
    h, ck = env
    s = h.slot()
    sb = h.S.blocks["safety"]
    sb["flag_loc_ok"][s] = False  # 定位未就绪（兜底 FSM 下一 tick 会重置，这里在同一步内准入）
    assert _goto(h, pid="p-op2", **BAD_SPEED)["code"] == int(Reason.LOC_NOT_READY)
    sb["flag_loc_ok"][s] = True
    ck.pre = True
    adm = _goto(h, pid="p-op2", **BAD_SPEED)
    assert adm["code"] == int(Reason.PREFLIGHT_FAILED) and adm["detail"] == {"check": "stub"}
    ck.pre = False


def test_step5_to_step8_order(env, monkeypatch) -> None:
    h, ck = env
    ck.fence = True
    assert _goto(h, pid="p-op2", **BAD_SPEED)["code"] == int(Reason.SEAT_TAKEN)  # ⑤ 先于 ⑥
    caps = h.core.engine.caps

    class NoGoto:  # 后端能力替身：caps 不含 goto
        backend = caps.backend

        def cmd_impl(self, op: str) -> str:
            return "none" if op == "goto" else caps.cmd_impl(op)

    monkeypatch.setattr(h.core.engine, "caps", NoGoto())
    assert _goto(h, **BAD_SPEED)["code"] == int(Reason.PARAM_OUT_OF_RANGE)  # ⑥ 先于 ⑦
    assert _goto(h)["code"] == int(Reason.BACKEND_UNSUPPORTED)  # ⑦ 先于 ⑧
    monkeypatch.setattr(h.core.engine, "caps", caps)
    adm = _goto(h)
    assert adm["code"] == int(Reason.GEOFENCE_REJECT) and adm["detail"] == {"zone": "stub"}  # ⑧
    ck.fence = False
    adm = _goto(h)
    assert adm["status"] == "accepted" and adm["apply_tick"] == h.core.clock.tick + 1  # ⑨


def test_fine_check_fails_after_accept(env) -> None:
    h, ck = env
    ck.fine = True
    h.core.engine.world = None
    h.n += 1
    adm = h.cmd("goto", {"pos": list(h.pos() + np.array([0.0, 30.0, 0.0]))}, cid="fine-1")
    assert adm["status"] == "accepted"
    c = h.call("fine-1")
    h.core.engine.fine_result("fine-1", False)
    h.advance(0.1)
    assert c.status == "failed" and c.code == int(Reason.GEOFENCE_REJECT)
    assert h.S.ctrl_mode[h.slot()] == K.M_HOLD


def test_audit_written(env) -> None:
    h, _ = env
    seen: list[dict] = []
    h.core.engine.audit = seen.append
    _goto(h, pid="p-op2")
    _goto(h)
    assert [r["code"] for r in seen] == [int(Reason.SEAT_TAKEN), 0]
    assert all(r["kind"] == "cmd.admission" and r["detail"]["op"] == "goto" for r in seen)


@pytest.mark.perf
def test_admission_p99_under_5ms(env) -> None:
    h, _ = env
    ops = ["goto", "hover", "rtl", "follow_path"]
    ts = []
    for k in range(500):
        op = ops[k % 4]
        p = h.pos()
        args = {"goto": {"pos": [p[0] + 5, p[1], p[2]]}, "hover": {}, "rtl": {"land": False},
                "follow_path": {"waypoints": [[p[0] + 5, p[1], p[2]], [p[0] + 5, p[1] + 5, p[2]]]}}[op]
        t0 = time.perf_counter()
        h.cmd(op, args)
        ts.append(time.perf_counter() - t0)
        h.advance(0.02)
    assert float(np.percentile(ts, 99)) <= 5e-3
