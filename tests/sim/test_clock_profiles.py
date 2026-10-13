"""时钟档与按档调度（M08-AC-201、AC-203、AC-210；M08-FR-201 至 FR-204、FR-220；ADR-090；AWR-04 §4.5；M08 §15.4.1、§15.4.2）。

- 档位常量：d1_250 主时钟 4 ms、单步上限 2500 tick；sandbox100 主时钟 10 ms、单步上限 1000 tick（仿真 10 s）；`t_sim_ns = tick × tick_ns`；
- 调度 golden：以桩插件补齐默认 pipeline 的全部 D1 stage，sandbox100 下 100 个 tick 的执行日志与 §15.4.1 表一致
  （`tests/golden/m08_schedule/schedule_100_sandbox100_{numba,numpy}.json`，由表独立推导，两者也必须一致）；d1_250 的 golden 不变
  （`test_schedule_golden.py`）；
- `hz` 写法：按档解析 every 与 phase；不整除构建失败；D1 写法的 stage 在沙盒档调度表缺项时构建失败；
- 预算：sandbox100 Σ（含主循环项）> 0.16 构建失败；M18–M21 的 stage 落在各自区段，越区段登记失败；
- checkpoint：档位不一致的代拒绝恢复（355）；
- 手动控制时钟锁（AC-210）：×5 下开始速度遥控，生效当 tick 倍率变为 1；锁定期间 `sim/speed` 返回 117（MANUAL_CONTROL）；
  `velocity_stop` 后 2 s ± 0.1 s【墙钟】解锁；d1_250 下倍率 ≠ 1 时 velocity 仍为 117（ADR-045）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from simlib import CoreHarness, Events

from awr.contracts.reasons import Reason
from awr.sim.fleet import kernels_l1 as KL
from awr.sim.fleet.fleet import FleetConfig, FleetSim
from awr.sim.fleet.pipeline import PipelineError, StageCtx
from awr.sim.fleet.stages import registry as R
from awr.sim.runtime import clock_profile as CP
from awr.sim.runtime.clock import SimClock

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "m08_schedule"
N_TICKS = 100

# §15.4.1 sandbox100 列（order, name, every, phase）；l1 组在 numba 模式合并为 `l1`；fleet_guard 1 片
SBX_COMMON = [
    (0, "clock", 1, 0), (10, "ingest", 1, 0), (20, "env", 2, 0), (25, "faults", 1, 0), (27, "mission", 1, 0),
    (85, "kinematic", 1, 0), (90, "contact", 1, 0), (91, "collide", 4, 3), (100, "sensors", 2, 1), (110, "guard", 2, 1),
    (115, "fsm", 1, 0), (120, "battery", 10, 3), (121, "mission_guard", 10, 7), (123, "battery_rtl", 20, 13),
    (128, "cmd_watch", 2, 1), (130, "fleet_guard.0", 10, 5), (140, "tap", 2, 0), (150, "mission_engine", 10, 4),
    (155, "coverage", 20, 9), (160, "director", 10, 4),
]
SBX_NUMBA = [(30, "l1", 1, 0)]
SBX_NUMPY = [(30, "refgen", 1, 0), (40, "pos_ctrl", 1, 0), (50, "att_ctrl", 1, 0), (60, "motor", 1, 0), (70, "aero", 1, 0),
             (80, "integrate", 1, 0)]
PLUGINS = [  # D1 登记值（不改注册代码，M08-FR-202）
    ("env", 5, 0, 20, "M07", 1), ("faults", 2, 0, 25, "M09", 1), ("mission", 2, 1, 27, "M10", 1),
    ("sensors", 5, 2, 100, "M13", 1), ("guard", 5, 1, 110, "M09", 1), ("fsm", 1, 0, 115, "M09", 1),
    ("battery", 25, 3, 120, "M09", 1), ("mission_guard", 25, 13, 121, "M09", 1), ("battery_rtl", 50, 17, 123, "M09", 1),
    ("fleet_guard", 25, 8, 130, "M09", 4), ("mission_engine", 25, 8, 150, "M10", 1), ("coverage", 50, 1, 155, "M10", 1),
    ("director", 25, 8, 160, "M10", 1),
]


def table_schedule(kernel: str, n: int = N_TICKS) -> list[list]:
    rows = sorted(SBX_COMMON + (SBX_NUMBA if kernel == "numba" else SBX_NUMPY))
    return [[t, [name for _, name, every, phase in rows if (t - phase) % every == 0]] for t in range(1, n + 1)]


def _build(reg: R.Registry, kernel: str = "numba", profile: str = "sandbox100") -> tuple[FleetSim, object]:
    cfg = FleetConfig(kernel=kernel, path_capacity=1024, clock_profile=profile)
    f = FleetSim(cfg, reg=reg)
    return f, f.build_pipeline()


def _trace(kernel: str) -> list[list]:
    with R.isolated_registry() as reg:
        for name, every, phase, order, owner, shards in PLUGINS:
            R.register_stage(name, every, phase, order, owner=owner, shards=shards)(lambda S, ctx: None)
        f, pl = _build(reg, kernel)
        assert not f.uses_fallback_fsm
        tr = pl.trace_on()
        ctx = StageCtx(events=Events(), profiles=f.T, paths=f.PB, cfg=f.cfg)
        f.step(ctx, N_TICKS)
        assert ctx.t_ns == N_TICKS * 10_000_000  # t_sim_ns = tick × 1e7（M08-AC-201）
    out: dict[int, list[str]] = {}
    for t, name in tr:
        out.setdefault(t, []).append(name)
    return [[t, out.get(t, [])] for t in range(1, N_TICKS + 1)]


def test_profile_constants_and_simclock() -> None:
    d1, sb = CP.get("d1_250"), CP.get("sandbox100")
    assert (d1.tick_hz, d1.tick_ns, d1.max_step_ticks, d1.budget_total_core) == (250, 4_000_000, 2500, 0.40)
    assert (sb.tick_hz, sb.tick_ns, sb.max_step_ticks, sb.budget_total_core) == (100, 10_000_000, 1000, 0.16)
    assert sb.max_step_ticks * sb.tick_ns == d1.max_step_ticks * d1.tick_ns == 10_000_000_000  # 单步上限仿真 10 s
    assert sb.every_for_hz(50) == 2 and sb.every_for_hz(10) == 10 and d1.every_for_hz(50) == 5
    with pytest.raises(ValueError):
        d1.every_for_hz(100)
    W = [10_000_000_000]
    c = SimClock(wall_ns=lambda: W[0], profile="sandbox100")
    c.tick = 12345
    assert c.t_ns == 12345 * 10_000_000 and c.tick_ns == 10_000_000
    assert c.apply("pause").code == 0
    assert c.apply("step", {"ticks": 1001}).code == int(Reason.PARAM_OUT_OF_RANGE)
    assert c.apply("step", {"ticks": 1000}).code == 0
    with CP.use("sandbox100"):
        assert CP.current().name == "sandbox100"
    assert CP.from_env({"AWR_CLOCK_PROFILE": "sandbox100"}).name == "sandbox100"
    assert CP.from_env({}).name == "d1_250"


@pytest.mark.parametrize("kernel", ["numba", "numpy"])
def test_schedule_golden_sandbox100(kernel: str) -> None:
    if kernel == "numba" and not KL.HAVE_NUMBA:
        pytest.skip("numba 不可用")
    gold = json.loads((GOLDEN / f"schedule_100_sandbox100_{kernel}.json").read_text(encoding="utf-8"))
    assert gold["profile"] == "sandbox100" and gold["ticks"] == table_schedule(kernel)
    assert _trace(kernel) == gold["ticks"]


def test_hz_registration_resolves_per_profile() -> None:
    for prof, want in (("d1_250", (5, 1)), ("sandbox100", (2, 1))):
        with R.isolated_registry() as reg:
            R.register_stage("probe", hz=50, phase_slot=1, order=105, owner="M13", budget_core=0.001)(lambda S, ctx: None)
            _f, pl = _build(reg, profile=prof)
            sp = pl.spec("probe")
            assert (sp.every, sp.phase) == want, (prof, sp)
    with R.isolated_registry() as reg:  # 100 Hz 不整除 250：d1_250 构建失败
        R.register_stage("probe", hz=100, order=105, owner="M13", budget_core=0.001)(lambda S, ctx: None)
        with pytest.raises(PipelineError):
            _build(reg, profile="d1_250")
        _f, pl = _build(reg, profile="sandbox100")
        assert (pl.spec("probe").every, pl.spec("probe").phase) == (1, 0)
    with pytest.raises(ValueError):
        R.make_stage("x", 5, 0, 105, lambda S, ctx: None, owner="M13", budget_core=0.0, hz=50)  # hz 与 every 二选一


def test_missing_schedule_entry_fails() -> None:
    with R.isolated_registry() as reg:
        R.register_stage("legacy_probe", 5, 2, 105, owner="M13", budget_core=0.001)(lambda S, ctx: None)
        _build(reg, profile="d1_250")  # D1 写法在 d1_250 下原样运行
        with pytest.raises(PipelineError, match="no schedule for legacy_probe@sandbox100"):
            _build(reg, profile="sandbox100")


def test_budget_limit_and_order_ranges() -> None:
    with R.isolated_registry() as reg:
        _f, pl = _build(reg, profile="sandbox100")
        assert pl.budget_total() <= 0.16 + 1e-12
        R.register_stage("heavy", hz=10, order=105, owner="M13", budget_core=0.2)(lambda S, ctx: None)
        with pytest.raises(PipelineError, match=r"0\.16"):
            _build(reg, profile="sandbox100")
    with R.isolated_registry():
        R.register_stage("targets", hz=10, order=170, owner="M18")(lambda S, ctx: None)
        R.register_stage("perception", 2, 1, 175, owner="M19")(lambda S, ctx: None)
        R.register_stage("fw_kin", hz=50, phase_slot=1, order=86, owner="M21")(lambda S, ctx: None)
        with pytest.raises(ValueError):
            R.register_stage("bad18", hz=10, order=150, owner="M18", budget_core=0.0)(lambda S, ctx: None)
        with pytest.raises(ValueError):  # 086–089 已让给 M21
            R.register_stage("bad08", 2, 0, 87, owner="M08", budget_core=0.0)(lambda S, ctx: None)
        with pytest.raises(ValueError):
            R.register_stage("bad20", hz=1, order=171, owner="M20", budget_core=0.0)(lambda S, ctx: None)
    with R.isolated_registry() as reg:  # D2 stage 的按档调度：targets 10 Hz 相位 1，tap_targets 之前（§15.4.1）
        R.register_stage("targets", hz=10, order=170, owner="M18")(lambda S, ctx: None)
        R.register_stage("perception", 2, 1, 175, owner="M19")(lambda S, ctx: None)
        _f, pl = _build(reg, profile="sandbox100")
        assert (pl.spec("targets").every, pl.spec("targets").phase) == (10, 1)
        assert (pl.spec("perception").every, pl.spec("perception").phase) == (1, 0)
        _f, pl = _build(reg, profile="d1_250")
        assert (pl.spec("targets").every, pl.spec("targets").phase) == (25, 19)


def test_checkpoint_profile_mismatch_rejected() -> None:
    from awr.sim.runtime.ckpt import CheckpointIncompatible, capture, restore

    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg)
        try:
            arrays, meta = capture(h.core)
            arrays = {k: np.array(v, copy=True) for k, v in arrays.items()}
        finally:
            h.close()
    assert meta["clock"]["profile"] == "d1_250"
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg, fleet_cfg={"clock_profile": "sandbox100"})
        try:
            with pytest.raises(CheckpointIncompatible) as ei:
                restore(h.core, arrays, meta)
            assert ei.value.code == int(Reason.RESIM_INCOMPATIBLE)
            a2, m2 = capture(h.core)
            assert m2["clock"]["profile"] == "sandbox100"
            restore(h.core, {k: np.array(v, copy=True) for k, v in a2.items()}, m2)  # 同档恢复正常
        finally:
            h.close()


def _sp(h: CoreHarness, vel_enu, seq: int) -> None:
    from awr.contracts.layouts import BUS_SETPOINT32

    s = h.slot()
    ag = next(a for a, sl in h.core.agent_slot.items() if sl == s)
    r = np.zeros(1, BUS_SETPOINT32)
    r["agent_no"] = ag
    r["seq"] = seq
    r["vel"] = vel_enu
    h.core.on_setpoint(r.tobytes())


def test_manual_lock_sandbox100() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg, fleet_cfg={"clock_profile": "sandbox100"})
        try:
            h.takeoff(10.0)
            assert h.clock("speed", {"rate": 5.0})["code"] == 0 and h.core.clock.rate == 5.0
            adm = h.cmd("velocity", {"frame": "world"}, cid="mv-1")
            assert adm["status"] == "accepted", adm  # 沙盒档：倍率 ≠ 1 时 velocity 不再 117（时钟锁定 ×1）
            apply_tick = adm["apply_tick"]
            seen = None
            for k in range(30):
                _sp(h, (1.0, 0.0, 0.0), k)
                h.W[0] += h.core.clock.tick_ns
                h.core.iterate()
                if h.core.clock.rate == 1.0 and seen is None:
                    seen = h.core.clock.tick
            assert seen is not None and seen - apply_tick <= 1 + 5 * 5, (seen, apply_tick)  # 生效的那一批内置 1
            assert h.core.clock.manual_locked
            r = h.clock("speed", {"rate": 2.0})
            assert r["code"] == int(Reason.CLOCK_CONSTRAINT) and (r.get("detail") or {}).get("why") == "MANUAL_CONTROL"
            assert h.clock("pause")["code"] == 0 and h.clock("play")["code"] == 0  # pause、play 照常
            assert h.core.perf_msg()["clock_lock"] == "manual"
            assert h.cmd("velocity_stop", {}, cid="mv-stop")["status"] == "accepted"
            # velocity_stop 生效后 2 s ± 0.1 s【墙钟】解锁（按墙钟逐 10 ms 推进计）
            h.W[0] += h.core.clock.tick_ns
            h.core.iterate()
            assert not h.S.vel_sess[h.slot()]
            t_free = h.W[0]
            unlocked_at = None
            for _ in range(500):
                h.W[0] += 10_000_000
                h.core.iterate()
                if not h.core.clock.manual_locked:
                    unlocked_at = h.W[0]
                    break
            assert unlocked_at is not None
            dt = (unlocked_at - t_free) * 1e-9
            assert 2.0 - 0.1 <= dt <= 2.0 + 0.1, dt
            assert h.core.clock.rate == 1.0  # 解锁后倍率保持 ×1，由治理器重新授予
            assert h.clock("speed", {"rate": 2.0})["code"] == 0
        finally:
            h.close()


def test_d1_velocity_rate_still_rejected() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg)
        try:
            h.takeoff(10.0)
            assert h.clock("speed", {"rate": 2.0})["code"] == 0
            adm = h.cmd("velocity", {"frame": "world"})
            assert adm["status"] == "rejected" and adm["code"] == int(Reason.CLOCK_CONSTRAINT)
            assert not h.core.clock.manual_locked
        finally:
            h.close()
