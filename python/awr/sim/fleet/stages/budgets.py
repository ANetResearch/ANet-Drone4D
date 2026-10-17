"""逐 stage CPU 预算常量（M08 §5.2；`register_stage` 的缺省 `budget_core`，FR-012）。

单位：核·秒/仿真秒（N = 1000、RTF = 1）。stage 名同时是 `stage_ms_per_s` 与 `bench-result.json` 的键（AWR-18 §7.6）。
M07、M09、M10、M13 的行是分配给对应模块的上限；表值的修改走 M08 变更请求（M08 §6.4.2）。

`fsm_min` 是 walking skeleton 的兜底 FSM（M09 未装配时由 M08 提供，见 `stages/fsm_min.py`），不在 §5.2 表内，
预算按 M09 `fsm` 行的一半登记（本工作包设定，MS4 装配 M09 后该 stage 不再注册）。

本表是 d1_250 档（N = 1000）的预算；sandbox100 档（N ≤ 12、识别物 ≤ 30）的预算与调度在 `stages/rates.py`（M08-FR-203）。
D2 追加的行（M08 §15.4.2 末段）：fw_kin 0.004（N = 1000 全为固定翼的上界）、tap_targets 0.001、targets 0.004、perception
0.025；order 区段追加 M21 086–089（自 M08 区段让出）、M18 170–174、M19 175–184、M20 185–194（M08-FR-204）。

D3 扩展点 1（AWR-05 §12.3，M24 §9.4 第 1 条）：order 195–198 归 M24（195 `mesh_contact`、198 `pose_history`，196–197 保留），
199 归 M23（`event_clock`）；包前缀 `awr.phys` → M24、`awr.sim.events` → M23；预算 mesh_contact 0.020、pose_history 0.006、
event_clock 0.0005。D3 stage 只由 `D3_PLUGINS` 登记，D2 组合根不登记，Σbudget 与构建结果不变。
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
    # D2（M08 §15.4.2）
    "fw_kin": 0.004,
    "tap_targets": 0.001,
    "targets": 0.004,
    "perception": 0.025,
    # D3（AWR-05 §12.3 扩展点 1；M24-NFR-003、M23 §6.12.1）
    "mesh_contact": 0.020,
    "pose_history": 0.006,
    "event_clock": 0.0005,
}

# D3：`mesh_contact` 声明 `writes = ("d3phys.*",)`；它对下列 M08 字段的写入是 M08 授权的例外（一字段一写者规则之外），
# 语义与 `contact` 撞墙分支相同，接受一个物理步的可见延迟（AWR-05 §12.3 扩展点 1，M24 §9.4 第 1 条）
MESH_CONTACT_WRITES: tuple[str, ...] = ("p", "v", "omega", "thrust", "thr_sp", "ctrl_mode", "crash_sub", "mode_evt")

# 不属于 pipeline stage 但计入合计的项（inbox drain、events.flush、M08 慢任务）
BUDGET_LOOP: dict[str, float] = {"drain": 0.002, "flush": 0.003, "slow": 0.010}

WARN_TOTAL_CORE = 0.40  # Σbudget 超过即拒绝构建（M08 §6.4.2；M08-AC-003）
GATE_TOTAL_CORE = 0.60

# order 区段（M08 §6.4.2）：所有者 -> [(lo, hi)]（含端点）
ORDER_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "M08": ((0, 14), (30, 85), (90, 99), (128, 129), (140, 149)),
    "M07": ((15, 24),),
    "M09": ((25, 25), (110, 124), (130, 139)),
    "M10": ((26, 29), (150, 169)),
    "M13": ((100, 109),),
    # D2（M08-FR-204）：125–127 保留
    "M21": ((86, 89),),
    "M18": ((170, 174),),
    "M19": ((175, 184),),
    "M20": ((185, 194),),
    # D3（扩展点 1）：195 mesh_contact、198 pose_history（196–197 保留）、199 event_clock
    "M24": ((195, 198),),
    "M23": ((199, 199),),
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
    # D2（M08-FR-204）
    ("awr.sim.fixedwing", "M21"),
    ("awr.sim.catalog", "M21"),
    ("awr.sim.targets", "M18"),
    ("awr.sim.perception", "M19"),
    ("awr.sim.tasking", "M20"),
    # D3（扩展点 1）
    ("awr.phys", "M24"),
    ("awr.sim.events", "M23"),
)


def base_name(name: str) -> str:
    """分片 stage `<name>.<k>` 的预算键为 `<name>`。"""
    return name.split(".", 1)[0]
