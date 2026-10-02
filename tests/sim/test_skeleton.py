"""SK-B sim-core walking skeleton（D1-MS3；M08-FR-001 至 FR-015、FR-060 至 FR-075、FR-089；M08-AC-001、AC-003、AC-030）。

- 扩展点 API 的冻结签名（registry、metrics、motion provider）与登记校验（重名、order 区段、预算、单例）；
- SimClock 的 play、pause、step、speed 与追赶；
- SimCore 以注入的假墙钟单步驱动（不睡眠、确定性）：座席 → 生命周期 READY → takeoff → goto → land，ACK 语义
  （accepted、running、succeeded，effect OK），幂等重发、准入拒绝码（115、116、110、109、NO_VEHICLE、签名）、StateRing 发布；
- 登记的 stage 与准入检查在主循环中生效。
平坦地面（`load_world = False`）：不依赖 worlds/ 构建产物。
"""

from __future__ import annotations

import inspect
import secrets

import numpy as np
import pytest

from awr.contracts import LAYOUT_ID
from awr.contracts.enums import FlightState, TimeState
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32
from awr.contracts.reasons import Reason
from awr.runtime.bus import LocalBus
from awr.runtime.principal import Principal, derive_key, sign_principal
from awr.runtime.statering import LocalRing
from awr.sim.core import metrics as M
from awr.sim.core.admission import AdmitResult
from awr.sim.core.command import register_motion_provider, reset_motion_providers
from awr.sim.core.state_model import admission_matrix
from awr.sim.fleet.pipeline import TICK_NS, Pipeline, PipelineError
from awr.sim.fleet.stages import registry as R
from awr.sim.runtime.clock import SimClock
from awr.sim.runtime.config import SimConfig
from awr.sim.runtime.main import SimCore

UAV = "p600-01"


# ---------------------------------------------------------------- 冻结签名（M08 §7.1.1）
def _params(fn) -> list[tuple[str, str]]:
    return [(p.name, p.kind.name) for p in inspect.signature(fn).parameters.values()]


def test_extension_signatures_frozen() -> None:
    assert _params(R.register_stage) == [
        ("name", "POSITIONAL_OR_KEYWORD"), ("every", "POSITIONAL_OR_KEYWORD"), ("phase", "POSITIONAL_OR_KEYWORD"),
        ("order", "POSITIONAL_OR_KEYWORD"), ("owner", "KEYWORD_ONLY"), ("fidelity", "KEYWORD_ONLY"),
        ("budget_core", "KEYWORD_ONLY"), ("writes", "KEYWORD_ONLY"), ("reads", "KEYWORD_ONLY"), ("shards", "KEYWORD_ONLY")]
    assert [n for n, _ in _params(R.register_admission_check)] == ["step", "name", "fn", "owner"]
    assert [n for n, _ in _params(R.register_slow_task)] == ["name", "fn", "period_wall_s", "period_sim_s", "budget_us"]
    assert [n for n, _ in _params(R.register_query)] == ["name", "fn"]
    assert [n for n, _ in _params(R.register_energy_model)] == ["model"]
    assert [n for n, _ in _params(R.register_safety_hooks)] == ["hooks"]
    assert [n for n, _ in _params(R.register_state_block)] == ["name", "owner", "fields", "checkpoint"]
    assert [n for n, _ in _params(register_motion_provider)] == ["provider"]
    assert [n for n, _ in _params(M.register_metric)] == ["name", "fn", "owner"]


def test_register_stage_validation_and_shards() -> None:
    with R.isolated_registry() as reg:
        @R.register_stage("guard", every=5, phase=1, order=110, owner="M09")
        def guard(S, ctx) -> None:
            return None

        assert [s.name for s in reg.stages] == ["guard"] and reg.stages[0].budget_core == pytest.approx(0.050)
        with pytest.raises(ValueError):
            R.register_stage("guard", every=5, phase=1, order=111, owner="M09")(guard)  # 重名
        with pytest.raises(ValueError):
            R.register_stage("x1", every=3, phase=0, order=112, owner="M09", budget_core=0.001)(guard)  # 250 % 3 ≠ 0
        with pytest.raises(ValueError):
            R.register_stage("x2", every=5, phase=5, order=112, owner="M09", budget_core=0.001)(guard)  # phase ≥ every
        with pytest.raises(ValueError):
            R.register_stage("x3", every=5, phase=0, order=60, owner="M09", budget_core=0.001)(guard)  # M08 区段
        with pytest.raises(KeyError):
            R.register_stage("no_budget", every=5, phase=0, order=113, owner="M09")(guard)  # 不在预算表
        specs = R.make_stage("fleet_guard", 25, 4, 130, guard, owner="M09", shards=4)
        assert [(s.name, s.order, s.phase) for s in specs] == [("fleet_guard.0", 130, 4), ("fleet_guard.1", 131, 9),
                                                               ("fleet_guard.2", 132, 14), ("fleet_guard.3", 133, 19)]
        assert sum(s.budget_core for s in specs) == pytest.approx(0.012)
        # Σbudget 超过 0.40 拒绝构建；一字段一写者
        big = R.make_stage("m09.heavy", 1, 0, 120, guard, owner="M09", budget_core=0.5)
        with pytest.raises(PipelineError):
            Pipeline.build(big, None)
        w1 = R.make_stage("a", 1, 0, 121, guard, owner="M09", budget_core=0.001, writes=("soc",))
        w2 = R.make_stage("b", 1, 0, 122, guard, owner="M09", budget_core=0.001, writes=("soc",))
        with pytest.raises(PipelineError):
            Pipeline.build([*w1, *w2], None)


def test_singletons_and_metrics() -> None:
    class Energy:
        def estimate(self, *a, **k): return None
        def rtl_plan(self, *a, **k): return None
        def path_wh(self, *a, **k): return 0.0

    with R.isolated_registry():
        with pytest.raises(TypeError):
            R.register_energy_model(object())
        R.register_energy_model(Energy())
        with pytest.raises(ValueError):
            R.register_energy_model(Energy())
        assert R.energy_model() is not None
        R.register_query("test/echo", lambda m, ctx: b"ok")
        with pytest.raises(ValueError):
            R.register_query("test/echo", lambda m, ctx: b"ok")
        R.register_slow_task("t", lambda ctx: None, period_wall_s=1.0)
        with pytest.raises(ValueError):
            R.register_slow_task("t2", lambda ctx: None, budget_us=0)

        @R.register_admission_check(4, "deny_orbit", owner="M09")
        def deny(req, ctx):
            return AdmitResult(int(Reason.STATE)) if req.op == "orbit" else None

        with pytest.raises(ValueError):
            R.register_admission_check(5, "bad", deny)  # type: ignore[arg-type]
    assert R.energy_model() is None  # 隔离表退出后恢复
    with M.isolated_metrics():
        M.register_metric("coverage_ratio", lambda **kw: 0.5 * kw.get("k", 1), owner="M10")
        assert M.metric("coverage_ratio", k=2) == 1.0
        assert [m.name for m in M.list_metrics()] == ["coverage_ratio"]
        with pytest.raises(ValueError):
            M.register_metric("coverage_ratio", lambda **kw: 0.0)
        with pytest.raises(KeyError):
            M.metric("nope")
        M.unregister_metric("coverage_ratio")
        assert M.list_metrics() == []


def test_admission_matrix_shape() -> None:
    mat = admission_matrix()
    assert mat and all(isinstance(k, str) for k in mat)


# ---------------------------------------------------------------- SimClock
def test_sim_clock_ops() -> None:
    W = [0]
    c = SimClock(wall_ns=lambda: W[0], max_batch_base=8)
    assert c.state == TimeState.STOPPED and c.steps_due() == 0
    assert c.apply("play").code == 0 and c.state == TimeState.PLAYING
    W[0] += 10 * TICK_NS
    n = c.steps_due()
    assert 1 <= n <= 10

    def run(k: int) -> None:  # tick 由调用方（SimCore 的 pipeline）逐个推进
        c.tick += k
        c.advanced(k)

    run(n)
    assert c.apply("pause").code == 0 and c.state == TimeState.PAUSED
    W[0] += 100 * TICK_NS
    assert c.steps_due() == 0
    assert c.apply("step", {"ticks": 3}).code == 0 and c.state == TimeState.STEPPING
    k = c.steps_due()
    assert 0 < k <= 3
    run(k)
    while c.state == TimeState.STEPPING:
        run(c.steps_due())
    assert c.state == TimeState.PAUSED and c.tick == n + 3
    assert c.apply("speed", {"rate": 2.0}).code == 0 and c.rate == 2.0
    assert c.apply("speed", {"rate": 3.0}).code != 0  # 只接受 0.25、0.5、1、2、5、10
    assert c.apply("warp").code != 0


# ---------------------------------------------------------------- SimCore 单步驱动
class Harness:
    def __init__(self, *, reg: R.Registry | None = None, n: int = 1) -> None:
        self.W = [1_000_000_000]
        self.secret = secrets.token_bytes(32)
        self.run_id = "rskb" + secrets.token_hex(3)
        ns = f"awr/test/{self.run_id}"
        self.path = f"/skb/{self.run_id}/state.sim-core"
        self.ring, _ = LocalRing.open_or_create(self.path, capacity=1024, slots=32, layout_id=LAYOUT_ID, id_base=0,
                                                id_count=1024)
        self.bus = LocalBus.open("sim-core", namespace=ns)
        cfg = SimConfig(run_id=self.run_id, n_vehicles=n, load_world=False, autoplay=True)
        self.core = SimCore(cfg, self.bus, self.ring, secret=self.secret, wall_ns=lambda: self.W[0], reg=reg)
        self.core.start()
        self.k_entry = derive_key(self.secret, self.run_id, "entry")
        self.n = 0

    def principal(self, cid: str, pid: str = "p-op", role: str = "operator", seat: bool = True) -> dict:
        p = Principal(pid, role, "api", "c-1", seat)
        d = p.fields()
        d["sig"] = sign_principal(p, cid, self.k_entry)
        return d

    def advance(self, seconds: float) -> None:
        end = self.core.clock.t_ns + int(seconds * 1e9)
        guard = 0
        while self.core.clock.t_ns < end and guard < 100_000:
            self.W[0] += 8 * TICK_NS
            self.core.iterate()
            guard += 1

    def until(self, pred, timeout_s: float) -> bool:
        t_end = self.core.clock.t_ns + int(timeout_s * 1e9)
        while self.core.clock.t_ns < t_end:
            if pred():
                return True
            self.advance(0.1)
        return bool(pred())

    def cmd(self, op: str, args: dict | None = None, *, cid: str | None = None, uav: str = UAV, **pk) -> dict:
        self.n += 1
        cid = cid or f"c-t{self.n:04d}"
        return self.core.engine.handle({"v": 1, "cid": cid, "op": op, "uav": uav, "args": args or {},
                                        "principal": self.principal(cid, **pk), "lease": None, "t_wall_ns": 0,
                                        "epoch_seen": 1, "batch_id": None})

    def call(self, cid: str):
        ent = self.core.engine.idem.get(cid)
        return ent.call if ent is not None else None

    def seat(self, pid: str = "p-op") -> dict:
        return self.core._lease_op({"v": 1, "cid": "seat-1", "op": "seat_claim", "principal": self.principal("seat-1", pid)})

    def pos(self) -> np.ndarray:
        e = self.core.roster.resolve(UAV)
        return self.core.fleet.S.pos_enu_view()[e.slot].copy()

    def close(self) -> None:
        self.core.stop()
        self.bus.close()
        LocalRing.remove(self.path)


@pytest.fixture
def h():
    x = Harness()
    yield x
    x.close()


def test_takeoff_goto_land(h: Harness) -> None:
    core = h.core
    assert [e.id for e in core.roster.by_slot.values()] == [UAV]
    # 座席：viewer 不能占席；operator 占席后幂等
    assert h.core._lease_op({"v": 1, "cid": "s0", "op": "seat_claim",
                             "principal": h.principal("s0", "p-v", "viewer", False)})["code"] == int(Reason.ROLE_FORBIDDEN)
    rep = h.seat()
    assert rep["status"] == "accepted" and rep["seat"] == {"state": "HELD", "holder": "p-op"}
    assert h.seat()["code"] == 0
    assert h.core._lease_op({"v": 1, "cid": "s2", "op": "seat_claim", "principal": h.principal("s2", "p-other")})["code"] \
        == int(Reason.SEAT_TAKEN)
    # 生命周期未 READY：108
    early = h.cmd("takeoff", {"alt_m": 5})
    assert early["status"] == "rejected" and early["code"] == int(Reason.LINK_ERROR)
    assert h.until(lambda: core.roster.resolve(UAV).lifecycle == 4, 10.0)  # READY
    # 准入拒绝：非席位持有者 116、viewer 115、未知机体、越界 110、骨架未实现 109、签名错误
    assert h.cmd("takeoff", {"alt_m": 5}, pid="p-other")["code"] == int(Reason.SEAT_TAKEN)
    assert h.cmd("takeoff", {"alt_m": 5}, role="viewer", seat=False)["code"] == int(Reason.ROLE_FORBIDDEN)
    assert h.cmd("takeoff", {"alt_m": 5}, uav="nobody")["code"] == int(Reason.NO_VEHICLE)
    assert h.cmd("takeoff", {"alt_m": 500})["code"] == int(Reason.PARAM_OUT_OF_RANGE)
    bad = {"v": 1, "cid": "c-badsig", "op": "takeoff", "uav": UAV, "args": {"alt_m": 5},
           "principal": h.principal("other-cid"), "lease": None, "t_wall_ns": 0, "epoch_seen": 1, "batch_id": None}
    assert core.engine.handle(bad)["status"] == "rejected"
    # takeoff：accepted（apply_tick = 下一 tick）→ running → succeeded
    adm = h.cmd("takeoff", {"alt_m": 5}, cid="c-takeoff")
    assert adm["status"] == "accepted" and adm["code"] == 0 and adm["apply_tick"] == core.clock.tick + 1
    h.advance(0.5)
    assert h.call("c-takeoff").status == "running"
    assert h.until(lambda: h.call("c-takeoff").final, 30.0)
    c = h.call("c-takeoff")
    assert (c.status, c.code, c.effect["status"]) == ("succeeded", 0, "OK"), c
    assert abs(h.pos()[2] - 5.0) < 0.5
    # 幂等：同 cid 重发返回 duplicate 与终态
    dup = h.cmd("takeoff", {"alt_m": 5}, cid="c-takeoff")
    assert dup["status"] == "duplicate" and dup["call_state"]["status"] == "succeeded"
    # 未装配 M09（兜底 FSM）时 escalate 无执行者：109
    esc = h.cmd("escalate", {"confirm_token": "x"})
    assert esc["code"] == int(Reason.BACKEND_UNSUPPORTED)
    # goto
    assert h.cmd("goto", {"pos": [10.0, 0.0, 5.0]}, cid="c-goto")["status"] == "accepted"
    assert h.until(lambda: h.call("c-goto").final, 30.0)
    c = h.call("c-goto")
    assert c.status == "succeeded" and c.effect["metrics"]["dist_err_m"] < 1.0
    assert np.linalg.norm(h.pos() - np.array([10.0, 0.0, 5.0])) < 1.0
    # StateRing：Full64 与 Lite32 与内部状态一致
    f = LocalRing.attach(h.path, expect_layout_id=LAYOUT_ID).read_latest(0)
    full = np.frombuffer(f.full, DRONE_STATE64)
    lite = np.frombuffer(f.lite, SWARM_LITE32)
    assert f.n_rows == 1 and len(full) == 1 and len(lite) == 1
    assert np.allclose(full["pos"][0], h.pos(), atol=0.2)
    assert full["flight_state"][0] & 0x0F == int(FlightState.FLYING)  # 低 4 位 FS，高 4 位 sub
    # land：落地后 succeeded（landed / disarmed）
    assert h.cmd("land", {}, cid="c-land")["status"] == "accepted"
    assert h.until(lambda: h.call("c-land").final, 40.0)
    assert h.call("c-land").status == "succeeded"
    assert h.pos()[2] < 0.3
    h.advance(3.0)
    fs = core.fleet.S.blocks["safety"]["fs"][core.roster.resolve(UAV).slot] if "safety" in core.fleet.S.blocks else None
    assert fs is None or int(fs) in (int(FlightState.LANDED), int(FlightState.DISARMED))


def test_clock_service_and_heartbeat(h: Harness) -> None:
    rep = h.core._clock_op({"v": 1, "cid": "k1", "op": "pause", "args": {}, "principal": h.principal("k1")})
    assert rep["code"] == int(Reason.SEAT_TAKEN)  # 需要席位
    h.seat()
    rep = h.core._clock_op({"v": 1, "cid": "k2", "op": "pause", "args": {}, "principal": h.principal("k2")})
    assert rep["status"] == "accepted" and rep["clock"]["state"] == "PAUSED"
    t0 = h.core.clock.t_ns
    h.W[0] += 100 * TICK_NS
    h.core.iterate()
    assert h.core.clock.t_ns == t0
    rep = h.core._clock_op({"v": 1, "cid": "k3", "op": "step", "args": {"ticks": 5}, "principal": h.principal("k3")})
    assert rep["code"] == 0
    for _ in range(10):
        h.W[0] += TICK_NS
        h.core.iterate()
    assert h.core.clock.t_ns == t0 + 5 * TICK_NS
    hdr = h.ring.header()
    assert hdr.t_sim_ns == h.core.clock.t_ns and hdr.clock_state == int(TimeState.PAUSED)


def test_registered_stage_and_admission_check() -> None:
    calls = []
    with R.isolated_registry() as reg:
        @R.register_stage("m09.probe", every=5, phase=0, order=115, owner="M09", budget_core=0.001)
        def probe(S, ctx) -> None:
            calls.append(ctx.tick)

        @R.register_admission_check(4, "m09.no_takeoff", owner="M09")
        def no_takeoff(req, ctx):
            return AdmitResult(int(Reason.SAFETY_ACTIVE), detail="TEST") if req.op == "takeoff" else None

        x = Harness(reg=reg)
        try:
            x.seat()
            assert x.until(lambda: x.core.roster.resolve(UAV).lifecycle == 4, 10.0)
            assert calls and all(t % 5 == 0 for t in calls)
            assert x.cmd("takeoff", {"alt_m": 5})["code"] == int(Reason.SAFETY_ACTIVE)
        finally:
            x.close()


def test_motion_provider_extends_command_set(h: Harness) -> None:
    started: list[tuple[str, str, list[int]]] = []

    class Orbit:
        name = "m10.orbit"
        ops = ("orbit",)

        def start(self, call, slots, args, apply_tick) -> None:
            started.append((call.cid, call.op, [int(x) for x in slots]))

        def cancel(self, call, slots) -> None:
            return None

    h.seat()
    assert h.until(lambda: h.core.roster.resolve(UAV).lifecycle == 4, 10.0)
    assert h.cmd("takeoff", {"alt_m": 5}, cid="c-to")["status"] == "accepted"
    assert h.until(lambda: h.call("c-to").final, 30.0)
    args = {"center": [0.0, 20.0, 5.0], "radius_m": 20.0}
    assert h.cmd("orbit", args, cid="c-native")["status"] == "accepted"  # 未登记提供者：M08 原生 ORBIT
    h.advance(0.1)
    try:
        register_motion_provider(Orbit())
        with pytest.raises(ValueError):
            register_motion_provider(Orbit())
        adm = h.cmd("orbit", args, cid="c-orbit")
        assert adm["status"] == "accepted", adm
        assert h.call("c-native").code == int(Reason.SUPERSEDED)
        h.advance(0.1)
        assert started == [("c-orbit", "orbit", [h.core.roster.resolve(UAV).slot])]
        assert int(h.core.fleet.S.ctrl_mode[h.core.roster.resolve(UAV).slot]) == 15  # TRAJ
    finally:
        reset_motion_providers()
