"""按档调度表与按档预算表（M08-FR-202、FR-203；M08 §15.4.1、§15.4.2；ADR-090）。

D1 插件（M07、M09、M10、M13）以 d1_250 的 tick 登记 `(every, phase)`，不改注册代码；其他档位的 `(every, phase)` 与分片数由本表
给出，表中缺项时构建失败（`PipelineError: no schedule for <stage>@<profile>`）。D2 新模块一律以 `hz` 声明；表中列出的 D2 stage
（targets、tap_targets、perception、fw_kin）以表为准，用于固定它们之间的相对相位（tap_targets 在 targets 之后两个或一个
tick）。各档位的逐机采样频率保持 ADR-021 冻结值（env、guard、sensors 50 Hz，battery 10 Hz）。

sandbox100 预算按 §15.4.2 的聚合行拆到 stage（N ≤ 12、识别物 ≤ 30、×1，单位：核），构建期 Σ（含主循环项）≤ 0.16。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from . import budgets as B
from .registry import StageSpec, shard_step

if TYPE_CHECKING:
    from awr.sim.runtime.clock_profile import ClockProfile

__all__ = ["BUDGETS", "LOOP_BUDGETS", "RATES", "Rate", "ScheduleError", "budget_of", "loop_budget", "resolve_specs"]


@dataclass(frozen=True)
class Rate:
    every: int
    phases: tuple[int, ...]  # 每片一个相位；长度即该档位的分片数


class ScheduleError(ValueError):
    """按档调度解析失败（调用方转换为 PipelineError）。"""


def _r(every: int, *phases: int) -> Rate:
    return Rate(every, tuple(phases) or (0,))


# 按档调度表（§15.4.1）。d1_250 只列 D2 新增的 stage（D1 stage 按登记值运行，M08-AC-004 golden 不变）
RATES: dict[str, dict[str, Rate]] = {
    "d1_250": {
        "fw_kin": _r(5, 1),
        "tap_targets": _r(25, 21),
        "targets": _r(25, 19),        # 奇数 tick，避开 L1 组与 env 全量 tick
        "perception": _r(2, 1),       # 25 片轮转，每对 5 Hz（M19）
    },
    "sandbox100": {
        "clock": _r(1, 0), "ingest": _r(1, 0), "fsm": _r(1, 0), "fsm_min": _r(1, 0),
        "env": _r(2, 0),
        "faults": _r(1, 0), "mission": _r(1, 0),
        "l1": _r(1, 0), "refgen": _r(1, 0), "pos_ctrl": _r(1, 0), "att_ctrl": _r(1, 0), "motor": _r(1, 0),
        "aero": _r(1, 0), "integrate": _r(1, 0),
        "kinematic": _r(1, 0), "fw_kin": _r(2, 1), "contact": _r(1, 0), "collide": _r(4, 3),
        "sensors": _r(2, 1), "guard": _r(2, 1), "cmd_watch": _r(2, 1),
        "battery": _r(10, 3), "mission_guard": _r(10, 7), "battery_rtl": _r(20, 13),
        "fleet_guard": _r(10, 5),     # N ≤ 12：1 片
        "tap": _r(2, 0), "tap_targets": _r(10, 2),
        "mission_engine": _r(10, 4), "director": _r(10, 4), "coverage": _r(20, 9),
        "targets": _r(10, 1),
        "perception": _r(1, 0),       # 20 片轮转，每对 5 Hz（M19）
    },
}

# 按档预算（核）；d1_250 取 budgets.BUDGET_CORE（登记时解析）
BUDGETS: dict[str, dict[str, float]] = {
    "sandbox100": {
        # clock + ingest + fsm 0.022（fsm_min 为 M09 未装配时的兜底，与 fsm 二选一）
        "clock": 0.002, "ingest": 0.010, "fsm": 0.010, "fsm_min": 0.010,
        "env": 0.006,
        # faults + mission 跟踪器 0.006
        "faults": 0.002, "mission": 0.004,
        # l1 + contact + collide + kinematic 0.020（oracle 6 段合计等于 l1）
        "l1": 0.014, "refgen": 0.003, "pos_ctrl": 0.004, "att_ctrl": 0.002, "motor": 0.001, "aero": 0.002,
        "integrate": 0.002, "contact": 0.003, "collide": 0.001, "kinematic": 0.002,
        "fw_kin": 0.002,
        "sensors": 0.004,
        # guard + battery + mission_guard + battery_rtl + fleet_guard 0.013
        "guard": 0.004, "battery": 0.003, "mission_guard": 0.002, "battery_rtl": 0.001, "fleet_guard": 0.003,
        "cmd_watch": 0.004,
        "tap": 0.008, "tap_targets": 0.002,
        "mission_engine": 0.002, "coverage": 0.001, "director": 0.001,
        "targets": 0.004,
        "perception": 0.025,
    },
}

# 主循环项（drain、flush、慢任务含 tasking 与 state_ext）；只在 `loop_in_budget` 的档位计入构建期 Σ
LOOP_BUDGETS: dict[str, dict[str, float]] = {
    "d1_250": dict(B.BUDGET_LOOP),
    "sandbox100": {"drain": 0.004, "flush": 0.004, "slow": 0.026},
}


def loop_budget(profile: ClockProfile) -> float:
    return float(sum(LOOP_BUDGETS.get(profile.name, B.BUDGET_LOOP).values())) if profile.loop_in_budget else 0.0


def budget_of(base: str, profile: ClockProfile, fallback: float | None) -> float:
    """某 stage（基名）在该档位的总预算：按档预算表优先，其次登记时的显式值或 d1 表值（M08-FR-203）。"""
    tab = BUDGETS.get(profile.name)
    if tab is not None and base in tab:
        return float(tab[base])
    if fallback is not None:
        return float(fallback)
    if base in B.BUDGET_CORE:
        return float(B.BUDGET_CORE[base])
    raise ScheduleError(f"no budget for {base}@{profile.name}")


def _rate_for(group: list[StageSpec], profile: ClockProfile) -> Rate | None:
    """None 表示按登记值原样运行（d1_250 下的 D1 写法）。"""
    base = B.base_name(group[0].name)
    tab = RATES.get(profile.name, {})
    if base in tab:
        return tab[base]
    s0 = group[0]
    if s0.hz is not None:
        try:
            every = profile.every_for_hz(s0.hz)
        except ValueError as e:
            raise ScheduleError(f"stage {base!r}: {e}") from None
        n = len(group)
        step = shard_step(every, n) if n > 1 else 1
        p0 = s0.phase_slot % every
        return Rate(every, tuple((p0 + k * step) % every for k in range(n)))
    if profile.name == "d1_250":
        return None
    raise ScheduleError(f"no schedule for {base}@{profile.name}")


def resolve_specs(specs: list[StageSpec], profile: ClockProfile) -> list[StageSpec]:
    """把登记的 StageSpec 解析为该档位的 `(every, phase)`、分片与预算（§15.5 `resolve_schedule`）。分片 stage 按表给出的
    片数重新展开（名字 `<name>.<k>`、order 依次 +k、预算均分）；输出保持输入中各组首次出现的次序。"""
    groups: dict[str, list[StageSpec]] = {}
    for s in specs:
        groups.setdefault(B.base_name(s.name), []).append(s)
    out: list[StageSpec] = []
    for base, grp in groups.items():
        grp = sorted(grp, key=lambda x: x.order)
        rate = _rate_for(grp, profile)
        if rate is None:
            out.extend(grp)
            continue
        s0 = grp[0]
        for ph in rate.phases:
            if not 0 <= ph < rate.every:
                raise ScheduleError(f"stage {base!r}@{profile.name}: phase={ph} 不满足 0 ≤ phase < every={rate.every}")
        if profile.name == "d1_250":
            total = sum(s.budget_core for s in grp)
        else:
            fb = sum(s.budget_core for s in grp) if s0.budget_explicit else None
            total = budget_of(base, profile, fb)
        n = len(rate.phases)
        sharded = s0.shards > 1 or n > 1 or "." in s0.name
        for k, ph in enumerate(rate.phases):
            name = f"{base}.{k}" if sharded else base
            out.append(replace(s0, name=name, every=int(rate.every), phase=int(ph), order=s0.order + k,
                               budget_core=total / n, shards=n, shard=(k, n)))
    return out
