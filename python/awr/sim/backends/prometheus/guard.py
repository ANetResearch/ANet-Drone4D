"""Prometheus 发送防护纯函数（g04 §8.4；M08-FR-070）：

- 非 COMMAND_CONTROL（control_state ≠ 2）时不发 `Agent_CMD`（108）帧，返回 `10 NOT_IN_CONTROL`；
- RTL 由适配层仿真（`AUTO.RTL` 会被机上主循环改回），不直接发模式切换；
- kill 不支持 → `109 BACKEND_UNSUPPORTED`；
- ABSOLUTE 锁（SafetyStop）由适配层跟踪：锁定期间除解锁（EXIT_ABSOLUTE）与安全类外返回 `114 LOCKED`；
- `rtl_emulation()`：RTL 的适配层仿真序列（升到返航高度 → home 上方 → 降到 home 上方 10 m → Land），全部为 108
  `UAVCommand`（Agent_CMD 4 XYZ_POS、最后 Agent_CMD 3）；
- `VelocityWatchdog`：velocity 会话 500 ms【墙钟】无新 setpoint（或 GS 连接重建）时立即发 108 `{Agent_CMD: 2}` 悬停。
"""

from __future__ import annotations

from dataclasses import dataclass

from awr.contracts.reasons import Reason

__all__ = ["NOT_IN_CONTROL", "SendDecision", "VelocityWatchdog", "guard_send", "rtl_emulation", "uav_command"]

NOT_IN_CONTROL = 10
P_COMMAND = 2
_SAFETY = {"land", "hover", "safety_stop", "resume"}


@dataclass(frozen=True)
class SendDecision:
    send: bool
    code: int
    via: str  # native、gateway、none


def guard_send(op: str, control_state: int, *, locked: bool = False) -> SendDecision:
    if op == "kill":
        return SendDecision(False, int(Reason.BACKEND_UNSUPPORTED), "none")
    if locked and op not in _SAFETY:
        return SendDecision(False, int(Reason.LOCKED), "none")
    if op == "rtl":
        return SendDecision(control_state == P_COMMAND, 0 if control_state == P_COMMAND else NOT_IN_CONTROL, "gateway")
    if op in ("orbit", "follow_path", "pause"):
        return SendDecision(control_state == P_COMMAND, 0 if control_state == P_COMMAND else NOT_IN_CONTROL, "gateway")
    if control_state != P_COMMAND and op not in ("takeoff",):
        return SendDecision(False, NOT_IN_CONTROL, "none")
    return SendDecision(True, 0, "native")


AGENT_CURRENT_HOVER, AGENT_LAND, AGENT_MOVE = 2, 3, 4
MOVE_XYZ_POS = 0
MSG_UAVCOMMAND = 108


def uav_command(agent_cmd: int, command_id: int, *, pos=None, yaw: float = 0.0) -> dict:
    """108 `UAVCommand` 的 JSON 体（字段名按 prometheus_msgs/UAVCommand.msg）。"""
    d = {"Agent_CMD": int(agent_cmd), "Command_ID": int(command_id)}
    if pos is not None:
        d.update({"Move_mode": MOVE_XYZ_POS, "position_ref": [float(x) for x in pos], "yaw_ref": float(yaw)})
    return d


def rtl_emulation(pos_local, home_local, z_rtl: float, *, first_id: int = 1, descend_alt: float = 10.0) -> list[dict]:
    """RTL 适配层仿真（g04 §8.4 F12：不发 SET_PX4_MODE AUTO.RTL）：局部 ENU 坐标（机体 local 系）的 108 序列。"""
    x, y, z = (float(v) for v in pos_local)
    hx, hy, hz = (float(v) for v in home_local)
    zr = max(z, float(z_rtl))
    legs = [(x, y, zr), (hx, hy, zr), (hx, hy, hz + descend_alt)]
    out = [uav_command(AGENT_MOVE, first_id + k, pos=p) for k, p in enumerate(legs)]
    out.append(uav_command(AGENT_LAND, first_id + len(legs)))
    return out


@dataclass
class VelocityWatchdog:
    """velocity 会话看门狗（墙钟）：`feed(t)` 记录 setpoint；`check(t)` 超时时返回一次悬停命令，会话结束。"""

    timeout_s: float = 0.5
    last_s: float | None = None
    active: bool = False

    def start(self, t_s: float) -> None:
        self.active, self.last_s = True, t_s

    def feed(self, t_s: float) -> None:
        if self.active:
            self.last_s = t_s

    def reconnect(self, t_s: float, command_id: int) -> dict | None:
        """GS 连接断开重连：立即悬停。"""
        if not self.active:
            return None
        self.active = False
        return uav_command(AGENT_CURRENT_HOVER, command_id)

    def check(self, t_s: float, command_id: int) -> dict | None:
        if not self.active or self.last_s is None or t_s - self.last_s < self.timeout_s:
            return None
        self.active = False
        return uav_command(AGENT_CURRENT_HOVER, command_id)
