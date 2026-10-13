"""沙盒会话（`AWR_PLAN_POOL = remote`）中规划池尚未就绪时，M20 作业留在本地等待并在就绪后交给共享规划服务，不在 sim-core 内同步
执行（D2-FX-R2；ADR-113；D2-AC-28：就绪后立即的 dry-run 曾在会话进程内装入 scipy.optimize 与 scipy.linalg）。等待超过上限时退回同步执行。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from awr.sim.tasking import jobs as J


class _Pool:
    def __init__(self) -> None:
        self.reqs: list[Any] = []

    def submit(self, req: Any, done: Any) -> None:
        self.reqs.append(req)


def _runner(monkeypatch: pytest.MonkeyPatch, *, remote: bool) -> tuple[J.JobRunner, dict]:
    monkeypatch.setenv("AWR_PLAN_POOL", "remote" if remote else "process")
    rt = SimpleNamespace(tick=10, ctx=SimpleNamespace(rate=1.0), world=None)
    r = J.JobRunner(rt)
    box: dict[str, Any] = {"pp": None}
    monkeypatch.setattr(r, "_pool", lambda: box["pp"])
    return r, box


def test_remote_session_defers_until_the_pool_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(J, "run_kind", lambda kind, payload, world: ran.append(kind) or {"ok": True})
    r, box = _runner(monkeypatch, remote=True)
    jid = r.submit("tasking.scan", {"spec": {}}, lambda extra: None, key="t1")
    assert ran == [] and r.stats["deferred"] == 1 and r.pending() == 1 and r.flush_inline() == 0
    r.drain(11)                                         # 仍未就绪：继续等待
    assert ran == [] and len(r.waiting) == 1
    pool = _Pool()
    box["pp"] = (pool, SimpleNamespace(world_key=("synthcity", "v", "sha")))
    r.drain(12)
    assert ran == [] and not r.waiting and r.stats["pool"] == 1
    assert pool.reqs[0].job_id == jid and pool.reqs[0].kind == "tasking.scan"


def test_remote_wait_times_out_to_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(J, "run_kind", lambda kind, payload, world: ran.append(kind) or {"ok": True})
    monkeypatch.setattr(J, "REMOTE_WAIT_S", 0.0)
    r, _box = _runner(monkeypatch, remote=True)
    got: list[dict] = []
    r.submit("tasking.allocate", {}, got.append)
    r.drain(11)                                         # 等待超时：同步执行，下一次 drain 生效
    assert ran == ["tasking.allocate"] and r.stats["inline"] == 1
    r.drain(12)
    assert got == [{"ok": True}]


def test_non_remote_keeps_the_inline_path(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(J, "run_kind", lambda kind, payload, world: ran.append(kind) or {"ok": True})
    r, _box = _runner(monkeypatch, remote=False)
    r.submit("tasking.station", {}, lambda extra: None)
    assert ran == ["tasking.station"] and r.stats["inline"] == 1 and r.stats["deferred"] == 0
