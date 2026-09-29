"""FakeDaemon：anet daemon 控制面（29 个路由中 D1 桥接用到的 7 个）的契约替身（M14-FR-067；d05 §4.2）。

`/version`（X-ANet-Wire）、`/hub-register`、`/find`、`/delegate`、`/results`、`/end`、`/evidence`；多个 daemon 共用一个 FakeHub。
委派由 `service` 模块转发到能力端点的效果封顶 V1（d05 §0 第 6 条）。
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from typing import Any

from awr.agent.anet_mock.hub import match_pattern


class FakeHub:
    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}
        self.inbox: dict[str, list[dict[str, Any]]] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.ids = itertools.count(1)


class FakeDaemon:
    def __init__(self, hub: FakeHub, aid: str, *, wire: str = "1", service: Callable[[str, Mapping[str, Any]], dict] | None = None) -> None:
        self.hub = hub
        self.aid = aid
        self._wire = wire
        self.service = service
        self.routes: list[str] = []
        self.evidence: list[dict[str, Any]] = []

    async def wire(self) -> str:
        self.routes.append("/version")
        return self._wire

    async def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        self.routes.append(path)
        h = self.hub
        if path == "/hub-register":
            h.agents[str(body["aid"])] = {"aid": body["aid"], "name": body.get("name", ""), "capabilities": list(body["capabilities"])}
            return {"ok": True}
        if path == "/find":
            pat = str(body["capability"])
            return {"agents": [a for a in h.agents.values() if any(match_pattern(pat, c) for c in a["capabilities"])]}
        if path == "/delegate":
            ix = f"ia-{next(h.ids):04d}"
            prov = str(body["provider"])
            cap = body["task_doc"]["capability"]
            if prov not in h.agents or not any(c == cap or cap.startswith(c + ".") for c in h.agents[prov]["capabilities"]):
                h.results[ix] = {"interaction_id": ix, "status": "failed", "deliverable": {"status": "UNAVAILABLE",
                                 "message": "not served"}, "receipt_verified": True}
            else:
                svc = self.service or (lambda c, a: {"status": "OK", "verify_trust": 4, "simulated": True, "metrics": {"confidence": 0.9}})
                h.results[ix] = {"interaction_id": ix, "status": "completed", "deliverable": svc(cap, body["task_doc"]["args"]),
                                 "receipt_verified": True, "receipt": {"ix": ix, "provider": prov}}
            return {"interaction_id": ix}
        if path == "/results":
            r = h.results.get(str(body["interaction_id"]))
            return {"results": [r] if r else []}
        if path == "/end":
            r = h.results.get(str(body["interaction_id"]))
            if r is not None and r["status"] not in ("completed", "failed"):
                r["status"] = "canceled"
            return {"ok": True}
        if path == "/evidence":
            self.evidence.append(dict(body))
            return {"ok": True}
        return {"error": "unknown route"}
