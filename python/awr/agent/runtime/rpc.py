"""`ctl/agent-runtime/{task,cancel,query}` queryable（M14 §7.3；M14-FR-062、FR-063；17 §9.3）。

| key | 请求 | 回复 |
|---|---|---|
| `task` | `{v: 1, op: submit、assign、retry, spec?, task_id?, provider_aid?, principal, idem_key}` | `{status, code, task_id, state, merged_into}` |
| `cancel` | `{v: 1, task_id, principal}` | `{status, code, state}` |
| `query` | `{v: 1, op: agents、manifest、tasks、task、board、evidence, args}` | 数据（服务 R43、R44、R73–R76） |

REST 提交的任务请求方为协调者 AID，`principal_id` 为操作员（审计 `entry = api`）；回放或关闭中写操作返回 118。
`idem_key` 在 60 s【墙钟】内重放首个回复。
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from awr.contracts import bus_keys
from awr.runtime.bus import Bus, Handle, Request

from .tasks import TaskError
from .types import TaskSpec

__all__ = ["RuntimeRpc", "spec_from_submit"]

log = logging.getLogger("awr.agent.rpc")

IDEM_TTL_S = 60.0
IDEM_MAX = 1024


def spec_from_submit(d: Mapping[str, Any], *, requester: str, principal_id: str) -> TaskSpec:
    tgt = d.get("target_enu_m")
    return TaskSpec(str(d.get("capability", "")), dict(d.get("args") or {}), tuple(tgt) if isinstance(tgt, list) else None,
                    d.get("accept"), d.get("negative_scope"), str(d.get("strategy", "auction")),  # type: ignore[arg-type]
                    d.get("provider_aid"), int(d.get("max_retries", 2)), "operator", requester, principal_id,
                    None, str(d.get("note", ""))[:200])


class RuntimeRpc:
    def __init__(self, core: Any, *, mono: Callable[[], float] = time.monotonic) -> None:
        self.core = core
        self.mono = mono
        self._idem: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self.handles: list[Handle] = []

    def serve(self, bus: Bus) -> None:
        self.handles.append(bus.serve(bus_keys.ctl_agent("task"), self._on_task))
        self.handles.append(bus.serve(bus_keys.ctl_agent("cancel"), self._on_cancel))
        self.handles.append(bus.serve(bus_keys.ctl_agent("query"), self._on_query))

    # ------------------------------------------------------------ 处理（事件循环线程）
    async def _on_task(self, req: Request) -> None:
        req.reply_msg(self.handle_task(req.msg() or {}))

    async def _on_cancel(self, req: Request) -> None:
        req.reply_msg(self.handle_cancel(req.msg() or {}))

    async def _on_query(self, req: Request) -> None:
        req.reply_msg(self.handle_query(req.msg() or {}))

    def _writable(self) -> int:
        st = self.core.session_state
        return 118 if st in ("replay", "closing") else 0

    def _idem_get(self, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        now = self.mono()
        while self._idem and now - next(iter(self._idem.values()))[0] > IDEM_TTL_S:
            self._idem.popitem(last=False)
        hit = self._idem.get(key)
        return None if hit is None else hit[1]

    def _idem_put(self, key: str | None, rep: dict[str, Any]) -> None:
        if not key:
            return
        self._idem[key] = (self.mono(), rep)
        while len(self._idem) > IDEM_MAX:
            self._idem.popitem(last=False)

    def handle_task(self, m: Mapping[str, Any]) -> dict[str, Any]:
        pr = m.get("principal") or {}
        key = f"{pr.get('principal_id')}|{m.get('idem_key')}" if m.get("idem_key") else None
        hit = self._idem_get(key)
        if hit is not None:
            return {**hit, "duplicate": True}
        code = self._writable()
        if code:
            return {"status": "rejected", "code": code}
        tm = self.core.tm
        op = m.get("op")
        try:
            if op == "submit":
                spec = spec_from_submit(m.get("spec") or {}, requester=self.core.coord_aid, principal_id=str(pr.get("principal_id", "")))
                tid, state, merged = tm.submit(spec)
                rep = {"status": "accepted", "code": 0, "task_id": tid, "state": state.value, "merged_into": merged}
            elif op in ("assign", "retry"):
                prov = m.get("provider_aid") if op == "assign" else None
                state = tm.assign(str(m.get("task_id")), prov)
                rep = {"status": "accepted", "code": 0, "task_id": m.get("task_id"), "state": state.value, "merged_into": None}
            else:
                rep = {"status": "rejected", "code": 300, "detail": {"op": op}}
        except TaskError as ex:
            rep = {"status": "rejected", "code": ex.code, "detail": ex.detail}
        except Exception:
            log.exception("task rpc failed")
            rep = {"status": "rejected", "code": 470}
        self._idem_put(key, rep)
        return rep

    def handle_cancel(self, m: Mapping[str, Any]) -> dict[str, Any]:
        code = self._writable()
        if code:
            return {"status": "rejected", "code": code}
        try:
            st = self.core.tm.cancel(str(m.get("task_id")))
            return {"status": "accepted", "code": 0, "state": st.value}
        except TaskError as ex:
            return {"status": "rejected", "code": ex.code, "detail": ex.detail}

    def handle_query(self, m: Mapping[str, Any]) -> dict[str, Any]:
        op = m.get("op")
        a = m.get("args") or {}
        core = self.core
        if op == "agents":
            v = core.agents_view()
            net = a.get("network")
            if net and net != v["network"]:
                v = {**v, "items": []}
            return {"code": 0, **v}
        if op == "manifest":
            man = core.manifest(str(a.get("aid", "")))
            return {"code": 0, "manifest": man} if man is not None else {"code": 305}
        if op == "tasks":
            return {"code": 0, "items": core.tm.list(state=a.get("state"), capability=a.get("capability"),
                                                     limit=int(a.get("limit") or 64))}
        if op == "task":
            t = core.tm.tasks.get(str(a.get("task_id", "")))
            return {"code": 0, "task": core.tm.status(t, full=True)} if t is not None else {"code": 305}
        if op == "board":
            b = core.board_view(str(a.get("task_id", "")))
            return {"code": 0, **b} if b is not None else {"code": 305}
        if op == "evidence":
            e = core.evidence_view(str(a.get("task_id", "")))
            return {"code": 0, **e} if e is not None else {"code": 305}
        if op == "state":
            return {"code": 0, "state": getattr(core, "runtime_state", "READY"), "session": core.session_state}
        return {"code": 300}

    def close(self) -> None:
        for h in self.handles:
            h.close()
        self.handles.clear()
