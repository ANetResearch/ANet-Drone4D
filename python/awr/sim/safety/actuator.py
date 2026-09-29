"""SafetyActuator 协议（M09 定义）与 Mock 实现（M09 §6.3.3；FR-009、FR-123）。

Mock 实现把动作写入 M08 的 SupervisorQueue（`ctx.supervisor`，`push(slots, SupAction, sub, reason, **kw)`）；M08 ingest 在下一
tick 按 slot 升序先于 staged 命令执行（固定 1 tick 延迟，ADR-049）。目标点一律 World ENU：回拉目标经 SupervisorQueue 的
`correct()` 入队，由 M08 在入队时换算为 NED（M09 不做 NED 换算，AWR-03 §5.3 第 3 条）。V0.2 的 PX4 镜像实现只交付接口。

每个动作同时登记"期望运动模式"（safety 块 `sup_expect`），fsm 在下一 tick 据此区分 Supervisor 引起的运动模式变化与
操作员命令引起的变化（M09 §6.2 执行链）。
"""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np

from awr.sim.core.supervisor_queue import SupAction
from awr.sim.fleet.state import CtrlMode

__all__ = ["ACTION_MODE", "MockActuator", "SafetyActuator"]

# Supervisor 动作 -> M08 执行后的运动模式（M08 ingest `apply_supervisor`）
ACTION_MODE: dict[SupAction, int] = {
    SupAction.HOLD: int(CtrlMode.HOLD), SupAction.RESUME: int(CtrlMode.HOLD), SupAction.CORRECT: int(CtrlMode.GOTO),
    SupAction.RTL: int(CtrlMode.RTL), SupAction.LAND: int(CtrlMode.LAND), SupAction.ELAND: int(CtrlMode.ELAND),
    SupAction.FAILSAFE_DESCENT: int(CtrlMode.DESCENT_FF), SupAction.KILL: int(CtrlMode.KILLED),
    SupAction.DISARM: int(CtrlMode.IDLE),
}


class SafetyActuator(Protocol):
    def hold(self, slots: np.ndarray, reason: str) -> None: ...
    def correct(self, slots: np.ndarray, target_enu_m: np.ndarray, sub: int, reason: str) -> None: ...
    def rtl(self, slots: np.ndarray, reason: str, z_rtl_m: np.ndarray | None = None) -> None: ...
    def land(self, slots: np.ndarray, reason: str) -> None: ...
    def eland(self, slots: np.ndarray, reason: str) -> None: ...
    def failsafe(self, slots: np.ndarray, reason: str) -> None: ...
    def kill(self, slots: np.ndarray, reason: str) -> None: ...
    def disarm(self, slots: np.ndarray, reason: str) -> None: ...
    def resume_hover(self, slots: np.ndarray, reason: str) -> None: ...


class MockActuator:
    """SupervisorQueue 适配（Mock 后端）。`queue` 为 M08 SupervisorQueue；`expect` 为 safety 块的 `sup_expect` 数组。"""

    def __init__(self, queue: Any, expect: np.ndarray, eland_mps: float = 0.5, failsafe_ff_mps: float = 1.0) -> None:
        self.q = queue
        self.expect = expect
        self.eland_mps = eland_mps
        self.failsafe_ff_mps = failsafe_ff_mps
        self.pushed: list[tuple[str, np.ndarray]] = []  # 测试与 AC-004 计时核对
        self.dirty = False  # 本 tick 有下发（fsm 下一 tick 据此读取并清空 `sup_expect`）

    def _push(self, slots: np.ndarray, action: SupAction, sub: int, reason: str, **kw: Any) -> None:
        s = np.asarray(slots, np.int32).reshape(-1)
        if s.size == 0:
            return
        self.expect[s] = ACTION_MODE[action]
        self.dirty = True
        self.pushed.append((action.name, s.copy()))
        if len(self.pushed) > 256:
            del self.pushed[:128]
        if self.q is not None:
            self.q.push(s, action, int(sub), reason, **kw)

    def hold(self, slots: np.ndarray, reason: str, sub: int = 0) -> None:
        self._push(slots, SupAction.HOLD, sub, reason)

    def correct(self, slots: np.ndarray, target_enu_m: np.ndarray, sub: int, reason: str) -> None:
        s = np.asarray(slots, np.int32).reshape(-1)
        tgt = np.asarray(target_enu_m, np.float64).reshape(-1, 3)
        if s.size == 0:
            return
        self.expect[s] = ACTION_MODE[SupAction.CORRECT]
        self.dirty = True
        self.pushed.append((SupAction.CORRECT.name, s.copy()))
        if self.q is None:
            return
        for k in range(s.size):  # SupervisorQueue.correct 按 ENU 入队并换算（M08 §6.3.4）
            self.q.correct(s[k:k + 1], tgt[k:k + 1])

    def rtl(self, slots: np.ndarray, reason: str, z_rtl_m: float | None = None) -> None:
        self._push(slots, SupAction.RTL, 0, reason, z_rtl_m=None if z_rtl_m is None else float(z_rtl_m))

    def land(self, slots: np.ndarray, reason: str) -> None:
        self._push(slots, SupAction.LAND, 0, reason)

    def eland(self, slots: np.ndarray, reason: str) -> None:
        self._push(slots, SupAction.ELAND, 0, reason, rate_mps=self.eland_mps)

    def failsafe(self, slots: np.ndarray, reason: str) -> None:
        self._push(slots, SupAction.FAILSAFE_DESCENT, 0, reason, rate_mps=self.failsafe_ff_mps)

    def kill(self, slots: np.ndarray, reason: str) -> None:
        self._push(slots, SupAction.KILL, 0, reason)

    def disarm(self, slots: np.ndarray, reason: str) -> None:
        self._push(slots, SupAction.DISARM, 0, reason)

    def resume_hover(self, slots: np.ndarray, reason: str) -> None:
        self._push(slots, SupAction.RESUME, 0, reason)
