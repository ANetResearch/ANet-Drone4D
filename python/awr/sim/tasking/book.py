"""任务、工作项与架次的运行时记录（M20 §6.2.2、§6.2.3、§6.11.1、§6.11.2；契约 `tasking/task.schema.json`、`rt/payloads/task_status`）。

任务状态取契约枚举：PLANNING、READY、RUNNING、PAUSED、DONE、CANCELED、FAILED、INFEASIBLE（PRD 的 DORMANT 在契约中不是任务状态，
接力的休眠以 `relay.state = DORMANT` 与 warning `dormant` 表达）。工作项：PENDING、ASSIGNED、ACTIVE、DONE、CANCELED、INFEASIBLE；
架次：ASSIGNED → LAUNCHING → TRANSIT → WORKING → (STANDBY) → RETURNING → RECOVERING → ENDED，另有 EVADING、PREEMPTED、DROPPED。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from awr.tasking.model import WorkItem

__all__ = ["ACTIVE_TASK_STATES", "AIR_STATES", "ItemRT", "SortieRT", "TaskRT"]

ACTIVE_TASK_STATES = ("PLANNING", "READY", "RUNNING", "PAUSED")
AIR_STATES = ("LAUNCHING", "TRANSIT", "WORKING", "STANDBY", "RETURNING", "EVADING", "PREEMPTED")


@dataclass
class ItemRT:
    w: WorkItem
    state: str = "PENDING"
    uav: str | None = None
    eta_s: float | None = None
    reason: str | None = None
    laps: int = 0
    dwell_s: float = 0.0
    relief_for: str | None = None        # 接替的在岗机
    t_assigned: float = 0.0
    attempts: int = 0
    t_created: float | None = None        # 接力站位工作项的创建时刻（未分配的接替工作项看门狗）

    def wire(self) -> dict:
        return {"wid": self.w.wid, "kind": self.w.kind, "uav": self.uav, "state": self.state,
                "eta_s": None if self.eta_s is None else round(float(self.eta_s), 1), "reason": self.reason}


@dataclass
class TaskRT:
    tid: str
    spec: dict
    state: str = "PLANNING"
    revision: int = 0
    created_ns: int = 0
    items: dict[str, ItemRT] = field(default_factory=dict)
    geometry: dict = field(default_factory=dict)
    preview: dict = field(default_factory=dict)
    children: list[str] = field(default_factory=list)
    parent: str | None = None
    warnings: list[str] = field(default_factory=list)
    last_error: dict | None = None
    assignments: list[dict] = field(default_factory=list)
    started_s: float | None = None
    done_s: float | None = None
    plan_job: str | None = None
    auto_start: bool = False
    relay: Any = None                    # RelayRT（relay_watch）
    visits: Any = None                   # VisitGrid（巡查与值守）
    mode: str | None = None              # area_guard：merged | split
    relayed: set[str] = field(default_factory=set)   # on_detect = relay 已生成接力的识别物
    scan_detail: dict | None = None

    @property
    def type(self) -> str:
        return str(self.spec.get("type"))

    @property
    def params(self) -> dict:
        return dict(self.spec.get("params") or {})

    @property
    def common(self) -> dict:
        return dict(self.spec.get("common") or {})

    @property
    def on_detect(self) -> str:
        return str(self.common.get("on_detect") or ("relay" if self.type == "area_guard" else "notify"))

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_TASK_STATES


@dataclass
class SortieRT:
    uav: str
    slot: int
    nest_id: str | None
    sortie_id: str
    tid: str | None = None
    wid: str | None = None
    state: str = "ASSIGNED"
    t_state: float = 0.0
    t_launch: float = 0.0
    cid: str | None = None
    call_op: str | None = None
    actions: list[dict] = field(default_factory=list)
    action: dict | None = None
    hover_until: float | None = None
    resume: str | None = None
    released: bool = False
    principal: str = "tasking:m20"
    look: tuple[float, float, float] | None = None
    target: str | None = None
    station: tuple[float, float, float] | None = None
    e0_wh: float = 0.0
    t_launched: float | None = None
    n_calls: int = 0
    standby_since: float | None = None
    evade_from: str | None = None
    evade_goal: tuple | None = None                          # 撤离点（水平），多旋翼 5 Hz 重发去抖
    evade_climb: tuple | None = None                         # 多旋翼先爬升撤离：(x, y, z, v)，爬升完成后的水平段
    goto_goal: tuple | None = None                           # 最近一条 goto 的目标点（守卫按到点即停预测多旋翼轨迹）
    fail_n: int = 0
    last_pos: np.ndarray | None = None
    path_cur: np.ndarray | None = None       # 最近一次 follow_path 的航点（航线复核，M20-FR-045）
    path_speed: float | None = None
    reroute_t: float = -1e18                 # 最近一次航线复核重规划的仿真时刻
    loiter_r: float | None = None
    rtl0: int = 0
    delay_noted: bool = False
    follow_goal: tuple[float, float, float] | None = None   # 运动目标跟随：最近一次 goto 的目标与时刻（RelayController._follow）
    follow_t: float = -1e18
    land_wait_t: float | None = None                         # 返航到达机巢上方后登记的回收时刻（Executor._land）
    retry_t: float | None = None                             # 转场或返航的重试时刻（安全动作进行中，Executor.on_result）
    retry_op: str | None = None
    ret_climb: bool = False                                  # 返航第一段：原地垂直爬升（Executor.return_home）
    climb_first: bool = False                                # 转场第一段：原地垂直爬升（Executor.start_transit）
    home_wait_t0: float | None = None                        # 返航航线止于机巢外：开始等待的时刻
    force_home: bool = False                                 # 等待超时或能量临界：直飞机巢降落（守卫让步）
    relief_wait_t0: float | None = None                      # 持续性航线的接替机在会合点等待在岗机离开的起始时刻
    evade_rej_t: float = -1e18                               # 最近一次撤离航线被拒的时刻（固定翼撤离冷却，guard.FW_EVADE_REJECT_S）
    yaw_goal: float | None = None                            # 分步转向的目标机头朝向（ENU，弧度；executor.yaw_goto）
    yield_t0: float | None = None                            # 间隔让行后续飞前的避让开始时刻（executor._yield）
    evade_ts: list[float] = field(default_factory=list)      # 最近的撤离时刻（executor.evade_pad：反复撤离时规划圆外扩）

    @property
    def airborne(self) -> bool:
        return self.state in ("LAUNCHING", "TRANSIT", "WORKING", "STANDBY", "RETURNING", "EVADING", "PREEMPTED")
