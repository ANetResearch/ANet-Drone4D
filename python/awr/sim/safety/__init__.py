"""M09 安全与健康：Flight FSM、FastGuard、MissionGuard、FleetGuard、geofence、battery、faults（所有者：M09，AWR-03 §4.3）。

组合根按 `configs/runtime.yaml` 的 `plugins: [awr.sim.safety]` 导入本包；**导入即登记**（M08 §7.1.1 的注册表函数，M08 不 import
M09）：状态块 `safety`、`battery`，stage `faults`（025）、`guard`（110）、`fsm`（115）、`battery`（120）、`mission_guard`（121）、
`fleet_guard.0–3`（130–133），准入检查 `safety.state`（④）与 `safety.geofence`（⑧），EnergyModel、SafetyHooks，剧本度量
（`min_separation_m`、`guard_events`、`pos_err_max_m`、`energy_rtl_count`、`battery_soc_min`、`flight_state`），慢任务
`safety.rows`（兴趣集 safety 行 10 Hz）、查询路由 `safety/fault` 与命令路由 `fault/inject`、`fault/clear`（ext）。

测试用法：`with registry.isolated_registry(), metrics.isolated_metrics(): svc = install()` 在隔离的登记表中装配；
`uninstall()` 把导入时登记到全局表的内容移除（tests/safety/conftest.py 在收集阶段调用，避免影响同一 pytest 进程中不装配
M09 的 M08、M11 用例）。进程内 sim-core 需要 M09 而本包已被导入过时调用 `ensure_installed()`。
"""

from __future__ import annotations

import os

from awr.sim.core import metrics as MET
from awr.sim.fleet.stages import registry as R

from .flight_fsm import whitelist_selfcheck
from .params import SafetyParams, SelfCheckError, selfcheck
from .service import SafetyRuntime, SafetyService
from .state import BATTERY_FIELDS, SAFETY_FIELDS

__all__ = ["SERVICE", "SafetyParams", "SafetyRuntime", "SafetyService", "SelfCheckError", "ensure_installed", "install",
           "uninstall"]

STAGES = (("faults", 2, 0, 25, 1, ("thrust_scale", "motor_ok")),
          ("guard", 5, 1, 110, 1, ()),
          ("fsm", 1, 0, 115, 1, ("safety.fs", "safety.sub", "safety.flag_failsafe", "safety.flag_alert", "safety.flag_gcs",
                                 "safety.flag_fcu", "safety.flag_loc_ok", "safety.flag_loc_deg", "safety.locked",
                                 "safety.severity")),
          ("battery", 25, 3, 120, 1, ("battery.soc", "battery.battery_pct", "battery.p_avg_w")),
          ("mission_guard", 25, 13, 121, 1, ("safety.d_free_fence_m",)),
          ("battery_rtl", 50, 17, 123, 1, ()),
          ("fleet_guard", 25, 8, 130, 4, ()))
# 10 Hz stage 全部落在 tick % 5 == 3（不与 50 Hz 的 env、guard、sensors、cmd_watch 同 tick），按 25 tick 周期内的
# 3、8、13、18、23 五个相位均摊（ADR-070，修订 M09 §5.2 相位安排）：battery 3；mission_guard 13；fleet_guard 四片 8、13、18、
# 23；M10 mission_engine/director 8。battery_rtl（RTL 终点与时间的轮转刷新，此前在 battery 内与能量积分同一次调用，N = 1000
# 时合计约 1.6 ms）为 50 tick 周期、相位 17：每次刷新 n/5 架，每机仍每秒一次。ADR-073 第 3 条：×1 下主循环成对推进奇、偶
# tick（ADR-070），单步按一对 tick 均摊，错峰的单位是"tick 对"而不是单个 tick；battery_rtl 此前在相位 43（生产口径约
# 1.4 ms），所在的 tick 对 (43, 44) 与 cmd_watch 叠加是全部 tick 对中最重的一类，改到最轻的一类 tick 对 (17, 18)（拆成两次
# 各 n/10 架时每次仍约 1.0 ms：固定开销为主，未采用）。
METRICS = ("min_separation_m", "guard_events", "pos_err_max_m", "energy_rtl_count", "battery_soc_min", "flight_state")

SERVICE: SafetyService | None = None


def install(params: SafetyParams | None = None, *, metrics: bool = True, sync_rows: bool = False,
            kernel: str | None = None, warm: bool = True) -> SafetyService:
    """在当前登记表（`registry()`，测试中为隔离表）中装配 M09；返回 SafetyService。启动自检失败抛 SelfCheckError。"""
    svc = SafetyService(params, kernel=kernel, sync_rows=sync_rows)
    selfcheck(svc.params)
    errs = whitelist_selfcheck()
    if errs:
        raise SelfCheckError("M09 selfcheck failed: " + "; ".join(errs))
    R.register_state_block("safety", owner="M09", fields=SAFETY_FIELDS)
    R.register_state_block("battery", owner="M09", fields=BATTERY_FIELDS)
    for name, every, phase, order, shards, writes in STAGES:
        R.register_stage(name, every, phase, order, owner="M09", writes=writes, shards=shards)(svc.stage(name))
    R.register_admission_check(4, "safety.state", svc.admit_state, owner="M09")
    R.register_admission_check(8, "safety.geofence", svc.admit_geofence, owner="M09")
    R.register_energy_model(svc.energy)
    R.register_safety_hooks(svc)
    R.register_slow_task("safety.rows", svc.slow_rows, period_sim_s=0.1, budget_us=200)
    R.register_query("safety/fault", svc.fault_query)
    # 故障注入的命令路由（M08-to-M09 第 1 条）：`ctl/sim-core/cmd` 的 `fault/inject`、`fault/clear`（剧本导演与 REST R16、R62），
    # 经 CommandEngine 验签、角色与席位检查，写输入日志，生命周期 accepted → running → succeeded（ADR-058）
    for op in ("fault/inject", "fault/clear"):
        R.register_command_handler(op, svc.fault_command(op), owner="M09")
    if metrics:
        for m in METRICS:
            MET.register_metric(m, getattr(svc, m), owner="M09")
    if warm and svc.kernel == "numba":
        from . import kernels

        kernels.warmup()
    return svc


def uninstall(svc: SafetyService | None = None) -> None:
    """从当前登记表移除 M09 的全部登记（只用于测试进程；sim-core 不调用）。"""
    global SERVICE
    reg = R.registry()
    target = svc or SERVICE
    reg.stages[:] = [s for s in reg.stages if s.owner != "M09"]
    for b in ("safety", "battery"):
        if b in reg.blocks and reg.blocks[b].owner == "M09":
            del reg.blocks[b]
    reg.admission[:] = [c for c in reg.admission if c.owner != "M09"]
    reg.slow[:] = [t for t in reg.slow if t.name != "safety.rows"]
    reg.queries.pop("safety/fault", None)
    for op in ("fault/inject", "fault/clear"):
        spec = reg.commands.get(op)
        if spec is not None and spec.owner == "M09":
            del reg.commands[op]
    if target is not None and reg.energy is getattr(target, "energy", None):
        reg.energy = None
    if target is not None and reg.hooks is target:
        reg.hooks = None
    for m in METRICS:
        spec = next((x for x in MET.list_metrics() if x.name == m), None)
        if spec is not None and spec.owner == "M09":
            MET.unregister_metric(m)
    if svc is None or svc is SERVICE:
        SERVICE = None


def ensure_installed() -> SafetyService:
    global SERVICE
    if SERVICE is None or R.safety_hooks() is not SERVICE:
        SERVICE = install()
    return SERVICE


if os.environ.get("AWR_SAFETY_AUTOINSTALL", "1") != "0":
    SERVICE = install()
