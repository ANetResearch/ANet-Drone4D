"""协调者 agent（`gcs`；M14 §1.5、§6.8；M14-FR-013）：代表地面站，提供 `blackboard.add|snapshot|conclude`，
作为操作员与剧本发起任务时的请求方。D1 中黑板在协调者进程内（V1.0 经 `blackboard.*` 委派）。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from ..anet_mock.identity import COORDINATOR_ID
from .blackboard import Blackboard, BoardError
from .network import InvokeSink
from .types import CapabilityCall, Effect, EffectStatus

__all__ = ["Coordinator"]


class Coordinator:
    coordinator = True
    agent_no = 0
    vehicle_id = COORDINATOR_ID
    name = COORDINATOR_ID
    profile_id = ""

    def __init__(self, aid: str, manifest: Mapping[str, Any], board: Blackboard, *, ledger: Any = None) -> None:
        self.aid = aid
        self.id = aid
        self.manifest = dict(manifest)
        self.board = board
        self.ledger = ledger

    def capabilities(self) -> list[str]:
        return ["blackboard.add", "blackboard.snapshot", "blackboard.conclude", "agent.describe"]

    def describe(self) -> dict[str, Any]:
        return self.manifest

    def health(self) -> str | None:
        return None

    def invoke_timeout(self, capability: str) -> float | None:
        return 60.0

    async def invoke(self, call: CapabilityCall, sink: InvokeSink | None = None) -> AsyncIterator[Effect]:
        a = dict(call.args)
        try:
            if call.capability == "blackboard.add":
                uid = self.board.add(call.caller_aid, str(a["task_id"]), str(a["type"]), dict(a.get("body") or {}))
                yield Effect(EffectStatus.OK, verify_trust=4, simulated=True, observed_state=uid)
            elif call.capability == "blackboard.snapshot":
                snap = self.board.snapshot(str(a["task_id"]))
                yield Effect(EffectStatus.OK, verify_trust=4, simulated=True, metrics={"units": float(len(snap))},
                             observed_state=json.dumps(snap, sort_keys=True, ensure_ascii=False))
            elif call.capability == "blackboard.conclude":
                self.board.conclude(str(a["task_id"]))
                yield Effect(EffectStatus.OK, verify_trust=4, simulated=True)
            elif call.capability == "agent.describe":
                yield Effect(EffectStatus.OK, verify_trust=4, simulated=True, observed_state=json.dumps(self.manifest, sort_keys=True))
            else:
                yield Effect(EffectStatus.UNAVAILABLE, message="not served")
        except (BoardError, KeyError) as ex:
            yield Effect(EffectStatus.FAILED, message=f"code=110 {type(ex).__name__}")
