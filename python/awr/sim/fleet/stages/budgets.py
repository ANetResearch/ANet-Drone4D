"""逐 stage CPU 预算常量（M08 §5.2；`register_stage` 的缺省 `budget_core`，FR-012）。

单位：核·秒/仿真秒（N = 1000、RTF = 1）。stage 名同时是 `stage_ms_per_s` 与 `bench-result.json` 的键（AWR-18 §7.6）。
M07、M09、M10、M13 的行是分配给对应模块的上限；表值的修改走 M08 变更请求（M08 §6.4.2）。

`fsm_min` 是 walking skeleton 的兜底 FSM（M09 未装配时由 M08 提供，见 `stages/fsm_min.py`），不在 §5.2 表内，
预算按 M09 `fsm` 行的一半登记（本工作包设定，MS4 装配 M09 后该 stage 不再注册）。
"""

from __future__ import annotations

TICK_HZ = 250

BUDGET_CORE: dict[str, float] = {
    "clock": 0.003,
    "ingest": 0.005,
    "env": 0.080,
    "faults": 0.002,
    "mission": 0.012,
    "l1": 0.070,
    # oracle 模式（AWR_KERNEL=numpy）下 l1 拆成的 6 个 stage，合计等于 l1（M08 §6.4.1）
    "refgen": 0.015,
    "pos_ctrl": 0.020,
    "att_ctrl": 0.012,
    "motor": 0.005,
    "aero": 0.010,
    "integrate": 0.008,
    "kinematic": 0.002,
    "contact": 0.007,
    "collide": 0.003,  # ADR-070：机间碰撞从 contact 拆出（25 Hz、奇数 tick），合计不变
    "sensors": 0.010,
    "guard": 0.050,
    "fsm": 0.005,
    "fsm_min": 0.0025,
    "battery": 0.004,
    "battery_rtl": 0.002,  # ADR-070：RTL 轮转刷新从 battery 拆出为独立的 10 Hz stage（相位 23），合计不变
    "mission_guard": 0.005,
    "cmd_watch": 0.005,
    "fleet_guard": 0.012,
    "tap": 0.020,
    "mission_engine": 0.003,
    "coverage": 0.005,
    "director": 0.002,
}

# 不属于 pipeline stage 但计入合计的项（inbox drain、events.flush、M08 慢任务）
BUDGET_LOOP: dict[str, float] = {"drain": 0.002, "flush": 0.003, "slow": 0.010}

WARN_TOTAL_CORE = 0.40  # Σbudget 超过即拒绝构建（M08 §6.4.2；M08-AC-003）
GATE_TOTAL_CORE = 0.60

# order 区段（M08 §6.4.2）：所有者 -> [(lo, hi)]（含端点）
ORDER_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "M08": ((0, 14), (30, 99), (128, 129), (140, 149)),
    "M07": ((15, 24),),
    "M09": ((25, 25), (110, 124), (130, 139)),
    "M10": ((26, 29), (150, 169)),
    "M13": ((100, 109),),
}

# 包前缀 -> 所有者（`owner` 缺省推断；M08 §6.4.2）
PACKAGE_OWNER: tuple[tuple[str, str], ...] = (
    ("awr.sim.runtime", "M08"),
    ("awr.sim.fleet", "M08"),
    ("awr.sim.core", "M08"),
    ("awr.sim.backends", "M08"),
    ("awr.environment", "M07"),
    ("awr.sim.safety", "M09"),
    ("awr.sim.mission", "M10"),
    ("awr.sim.planning", "M10"),
    ("awr.sim.sensors", "M13"),
)


def base_name(name: str) -> str:
    """分片 stage `<name>.<k>` 的预算键为 `<name>`。"""
    return name.split(".", 1)[0]
