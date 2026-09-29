"""能力处理器（M14 §6.11；§9.1）：`meta`（agent.describe、agent.state、task.quote）、`observe`（thermal.imaging、rgb.zoom）、
`relay`（relay.communication，P2）。处理器是不可信指挥官：只经 HandlerCtx 与 CommandPort 行事。"""

from __future__ import annotations

from .meta import agent_describe, agent_state, task_quote
from .observe import observe
from .relay import relay_communication

__all__ = ["HANDLERS"]

HANDLERS = {
    "agent.describe": agent_describe,
    "agent.state": agent_state,
    "task.quote": task_quote,
    "thermal.imaging": observe,
    "rgb.zoom": observe,
    "relay.communication": relay_communication,
}
