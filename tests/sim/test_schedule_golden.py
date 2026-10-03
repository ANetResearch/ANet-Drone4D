"""调度 golden（M08-AC-004；M08-FR-013；M08 §6.4.1、§6.4.3）。

以桩插件补齐 §6.4.1 默认 pipeline 的全部 stage（M07 env、M09 faults/guard/fsm/battery/mission_guard/fleet_guard 四片、
M10 mission/mission_engine/coverage/director、M13 sensors），numba 与 oracle 两种内核各执行 50 个 tick，逐 tick 记录执行的
stage 名，与 `tests/golden/m08_schedule/schedule_50_{numba,numpy}.json` 完全一致；golden 本身由 §6.4.1 表（order、every、
phase）独立推导（`_table_schedule`），两者也必须一致。M09 `fsm` 登记后不装配兜底 `fsm_min`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from simlib import Events

from awr.sim.fleet import kernels_l1 as KL
from awr.sim.fleet.fleet import FleetConfig, FleetSim
from awr.sim.fleet.pipeline import StageCtx
from awr.sim.fleet.stages import registry as R

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "m08_schedule"
N_TICKS = 50

# §6.4.1 表（order, name, every, phase）；l1 组在 numba 模式合并为 `l1`（order 030）
TABLE_COMMON = [
    (0, "clock", 1, 0), (10, "ingest", 1, 0), (20, "env", 5, 0), (25, "faults", 2, 0), (27, "mission", 2, 1),
    (85, "kinematic", 2, 0), (90, "contact", 2, 0), (91, "collide", 10, 9), (100, "sensors", 5, 2), (110, "guard", 5, 1),
    (115, "fsm", 1, 0), (120, "battery", 25, 3), (121, "mission_guard", 25, 13), (123, "battery_rtl", 50, 17),
    (128, "cmd_watch", 5, 4), (130, "fleet_guard.0", 25, 8), (131, "fleet_guard.1", 25, 13),
    (132, "fleet_guard.2", 25, 18), (133, "fleet_guard.3", 25, 23), (140, "tap", 2, 0), (150, "mission_engine", 25, 8),
    (155, "coverage", 50, 1), (160, "director", 25, 8),
]
# ADR-070（FX2-R3）：10 Hz stage 全部落在 tick % 5 == 3 的五个相位（3、8、13、18、23）；battery_rtl 自 battery 拆出（5 Hz、
# 奇数 tick 43）；
# 机间碰撞自 contact 拆出为 `collide`（25 Hz、奇数 tick ≡ 9 mod 10）；coverage 13 → 23。
# ADR-073（P4-SIM）：×1 成对推进下按 tick 对 (2j+1, 2j+2) 错峰：battery_rtl 43 → 17，coverage 23 → 1
L1_NUMBA = [(30, "l1", 2, 0)]
L1_NUMPY = [(30, "refgen", 2, 0), (40, "pos_ctrl", 2, 0), (50, "att_ctrl", 2, 0), (60, "motor", 2, 0), (70, "aero", 2, 0),
            (80, "integrate", 2, 0)]

PLUGINS = [  # (name, every, phase, order, owner, shards)
    ("env", 5, 0, 20, "M07", 1), ("faults", 2, 0, 25, "M09", 1), ("mission", 2, 1, 27, "M10", 1),
    ("sensors", 5, 2, 100, "M13", 1), ("guard", 5, 1, 110, "M09", 1), ("fsm", 1, 0, 115, "M09", 1),
    ("battery", 25, 3, 120, "M09", 1), ("mission_guard", 25, 13, 121, "M09", 1), ("battery_rtl", 50, 17, 123, "M09", 1),
    ("fleet_guard", 25, 8, 130, "M09", 4), ("mission_engine", 25, 8, 150, "M10", 1), ("coverage", 50, 1, 155, "M10", 1),
    ("director", 25, 8, 160, "M10", 1),
]


def _table_schedule(kernel: str) -> list[list]:
    rows = sorted(TABLE_COMMON + (L1_NUMBA if kernel == "numba" else L1_NUMPY))
    return [[t, [name for _, name, every, phase in rows if (t - phase) % every == 0]] for t in range(1, N_TICKS + 1)]


def _trace(kernel: str) -> list[list]:
    with R.isolated_registry() as reg:
        for name, every, phase, order, owner, shards in PLUGINS:
            R.register_stage(name, every, phase, order, owner=owner, shards=shards)(lambda S, ctx: None)
        cfg = FleetConfig(kernel=kernel, path_capacity=1024)
        f = FleetSim(cfg, reg=reg)
        pl = f.build_pipeline()
        assert not f.uses_fallback_fsm
        tr = pl.trace_on()
        ctx = StageCtx(events=Events(), profiles=f.T, paths=f.PB, cfg=cfg)
        f.step(ctx, N_TICKS)
    out: dict[int, list[str]] = {}
    for t, name in tr:
        out.setdefault(t, []).append(name)
    return [[t, out.get(t, [])] for t in range(1, N_TICKS + 1)]


@pytest.mark.parametrize("kernel", ["numba", "numpy"])
def test_schedule_matches_golden(kernel: str) -> None:
    if kernel == "numba" and not KL.HAVE_NUMBA:
        pytest.skip("numba 不可用")
    gold = json.loads((GOLDEN / f"schedule_50_{kernel}.json").read_text(encoding="utf-8"))
    assert gold["ticks"] == _table_schedule(kernel)
    assert _trace(kernel) == gold["ticks"]


def test_worst_tick_sets() -> None:
    """§6.4.3 的错峰（ADR-070、ADR-073）：25 周期的 battery、mission_guard、fleet_guard 四片、mission_engine 与 director 在
    tick % 5 == 3；机间碰撞在奇数 tick（不与 l1 组同 tick）。×1 成对推进（ADR-070）下单步按 tick 对 (2j+1, 2j+2) 均摊，
    错峰以 tick 对为单位（ADR-073）：battery_rtl（5 Hz）在 tick 对 (17, 18)，coverage（5 Hz）在 (1, 2)，
    任何一个 tick 对内至多一个"重的低频 stage"（battery、battery_rtl、mission_guard、coverage、mission_engine；生产口径各
    0.4–1.0 ms），env 的 10 Hz 全量 tick（5、35）所在的 tick 对内没有低频 stage。"""
    sched = {t: set(n) for t, n in _table_schedule("numba")}
    assert {"env", "l1", "tap"} <= sched[10] and "l1" not in sched[5] and "env" in sched[5]
    assert "battery" in sched[3] and "guard" not in sched[3] and "sensors" not in sched[3]
    assert "mission_engine" in sched[8] and "director" in sched[8]
    assert [t for t, n in sched.items() if "cmd_watch" in n] == [4, 9, 14, 19, 24, 29, 34, 39, 44, 49]
    ten_hz = {"battery", "mission_guard", "fleet_guard.0", "fleet_guard.1", "fleet_guard.2", "fleet_guard.3",
              "mission_engine", "director"}
    fifty_hz = {"env", "guard", "sensors", "cmd_watch"}
    for t, n in sched.items():
        if n & ten_hz:
            assert t % 5 == 3 and not (n & fifty_hz), (t, n)
    assert [t for t, n in sched.items() if "battery_rtl" in n] == [17]
    assert [t for t, n in sched.items() if "coverage" in n] == [1]
    heavy = {"battery", "battery_rtl", "mission_guard", "coverage", "mission_engine"}
    for j in range(N_TICKS // 2):
        pair = sched[2 * j + 1] | sched.get(2 * j + 2, set())
        assert len(pair & heavy) <= 1, (2 * j + 1, sorted(pair & heavy))
        if 2 * j + 1 in (5, 35):
            assert not (pair & (heavy | ten_hz)), (2 * j + 1, pair)
    assert [t for t, n in sched.items() if "collide" in n] == [9, 19, 29, 39, 49]
    assert all("l1" not in sched[t] for t in (9, 19, 29, 39, 49))
