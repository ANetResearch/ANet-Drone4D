"""能力处理器（M14 §6.11；§9.1）：`meta`（agent.describe、agent.state、task.quote）、`observe`（thermal.imaging、rgb.zoom）、
`relay`（relay.communication，P2）。处理器是不可信指挥官：只经 HandlerCtx 与 CommandPort 行事。"""

from __future__ import annotations

from .meta import agent_describe, agent_state, task_quote
from .observe import observe, prepare_observe
from .relay import relay_communication

__all__ = ["HANDLERS", "PREPARERS"]

HANDLERS = {
    "agent.describe": agent_describe,
    "agent.state": agent_state,
    "task.quote": task_quote,
    "thermal.imaging": observe,
    "rgb.zoom": observe,
    "relay.communication": relay_communication,
}

# 委派在途预取（只读准备；DroneAgent.prepare 在 Mock 长任务请求发出时调用，§6.12.2）
PREPARERS = {
    "thermal.imaging": prepare_observe,
    "rgb.zoom": prepare_observe,
}
