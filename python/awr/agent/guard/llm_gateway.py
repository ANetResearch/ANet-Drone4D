"""LLM/MCP 指挥官接口与对抗测试替身（M14 §6.14.3；M14-FR-059、FR-060；n03 §2.5、§3.7–§3.8）。

- `LlmCommander`：LLM 只产出 TaskIntent `{capability, target_enu_m, args, accept?, rationale}`，**不产出命令**；分配由确定性
  Allocator 完成。D1 只交付接口与拒绝一切的默认实现 `DenyAllCommander`（真实接入在 V1.0）。
- `ToolTier`：READ_ONLY（agents.list、agents.manifest、tasks.list、tasks.get、board.snapshot）、NORMAL（tasks.submit 且能力在
  允许清单内、strategy = auction；tasks.cancel 本 LLM 提交的任务）、CRITICAL（strategy = direct、抢占 OPERATOR 的任务、
  未登记的工具一律 CRITICAL，需要操作员确认令牌）。
- `ScriptedCommander`：按脚本以 agent 身份发出越权、越界、注入文本、伪造 principal、超频请求（M14-AC-029）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

__all__ = ["READ_ONLY_TOOLS", "DenyAllCommander", "LlmCommander", "ScriptedCommander", "TaskIntent", "ToolTier", "tool_tier"]


class ToolTier(StrEnum):
    READ_ONLY = "READ_ONLY"
    NORMAL = "NORMAL"
    CRITICAL = "CRITICAL"


READ_ONLY_TOOLS = frozenset({"agents.list", "agents.manifest", "tasks.list", "tasks.get", "board.snapshot"})
LLM_RATE_PER_S = 1.0
LLM_MAX_ACTIVE = 8


@dataclass(frozen=True)
class TaskIntent:
    capability: str
    target_enu_m: tuple[float, float, float | None] | None
    args: Mapping[str, Any]
    accept: Mapping[str, Any] | None = None
    rationale: str = ""  # 只作显示并经净化；业务逻辑不解析


def tool_tier(tool: str, args: Mapping[str, Any] | None = None, *, allowed_caps: Iterable[str] = ("thermal.imaging", "rgb.zoom"),
              own_task: bool = True) -> ToolTier:
    a = args or {}
    if tool in READ_ONLY_TOOLS:
        return ToolTier.READ_ONLY
    if tool == "tasks.submit":
        if a.get("strategy", "auction") != "auction" or a.get("provider_aid") or a.get("preempt_operator"):
            return ToolTier.CRITICAL
        return ToolTier.NORMAL if a.get("capability") in set(allowed_caps) else ToolTier.CRITICAL
    if tool == "tasks.cancel":
        return ToolTier.NORMAL if own_task else ToolTier.CRITICAL
    return ToolTier.CRITICAL


class LlmCommander(Protocol):
    async def propose(self, context: Mapping[str, Any]) -> Sequence[TaskIntent]: ...

    def tier(self, tool: str, args: Mapping[str, Any]) -> ToolTier: ...


class DenyAllCommander:
    """D1 默认实现：不提出任何意图，任何工具调用都视为 CRITICAL 且拒绝。"""

    async def propose(self, context: Mapping[str, Any]) -> Sequence[TaskIntent]:
        return ()

    def tier(self, tool: str, args: Mapping[str, Any]) -> ToolTier:
        return ToolTier.CRITICAL

    def allow(self, tool: str, args: Mapping[str, Any]) -> bool:
        return False


@dataclass(frozen=True)
class ScriptStep:
    op: str
    uav: str | None
    args: Mapping[str, Any]
    ix: str | None = None
    aid: str | None = None
    label: str = ""


class ScriptedCommander:
    """对抗测试替身：按脚本经 TrustedGuard（或伪造 principal 直连 bus）发出请求，收集结果。"""

    def __init__(self, guard: Any, aid: str) -> None:
        self.guard = guard
        self.aid = aid
        self.results: list[tuple[ScriptStep, dict[str, Any]]] = []

    async def run(self, steps: Iterable[ScriptStep]) -> list[tuple[ScriptStep, dict[str, Any]]]:
        for s in steps:
            adm = await self.guard.submit(s.aid or self.aid, s.op, s.uav, dict(s.args), ix=s.ix)
            self.results.append((s, adm))
        return self.results

    @staticmethod
    def forged_command(uav: str, op: str = "goto", args: Mapping[str, Any] | None = None, *, aid: str = "bafyreiforged") -> dict[str, Any]:
        """伪造 principal（签名错误）的直连命令：生产者验签失败返回 115（M08-NFR-017、ARCH-AC-019）。"""
        return {"v": 1, "cid": "forged-0001", "op": op, "uav": uav, "args": dict(args or {}),
                "principal": {"principal_id": f"agent:{aid}", "role": "agent", "entry": "agent-runtime", "conn_id": None,
                              "seat": False, "sig": b"\x00" * 32},
                "lease": None, "t_wall_ns": 0, "epoch_seen": 0, "batch_id": None}


ScriptedCommander.Step = ScriptStep  # type: ignore[attr-defined]
