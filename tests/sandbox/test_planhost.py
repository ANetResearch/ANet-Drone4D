"""共享规划服务宿主（M17-FR-032 至 FR-034、AC-017 的宿主部分）：PlanServer 的构造参数取 `sandbox.planning`（worker 数、nice、
叶子组、世界 LRU、每会话上限、冻结策略）；会话总线 attach/detach 幂等；构造失败时池照常运行（规划不可用）。真实 PlanServer
的公平队列、超时强杀与冻结由 M10 的 tests/planning 覆盖；此处另以真实类构造一次，确认参数兼容。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sbharness import make_sb

from awr.sandbox.config import PoolSettings
from awr.sandbox.planhost import PlanHost


class StubServer:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.attached: list[str] = []
        self.detached: list[str] = []
        self.closed = False

    def attach_bus(self, sid: str, bus: Any) -> list:
        self.attached.append(sid)
        return []

    def detach_bus(self, sid: str) -> None:
        self.detached.append(sid)

    def stats(self) -> dict:
        return {"kills": 0}

    def worker_pids(self) -> list[int]:
        return [123]

    def close(self) -> None:
        self.closed = True


def test_host_kwargs_and_attach() -> None:
    s = PoolSettings(sb=make_sb(planning={"workers": 2, "world_lru": 3, "freeze": {"timeouts": 4}}), allow_derive=False,
                     worlds_dir=Path("/w"), shm_root=Path("/dev/shm/awr/x"))
    h = PlanHost(s, cgroup_leaf="sandbox/plan", server_factory=StubServer)
    kw = h.server.kw
    assert kw["workers"] == 2 and kw["lru_worlds"] == 3 and kw["allow_derive"] is False and kw["cgroup_leaf"] == "sandbox/plan"
    assert kw["nice"] == 10 and kw["queue_max"] == 8 and kw["freeze_count"] == 4 and kw["freeze_s"] == 300
    h.attach("sb-aaaaaaaaaa", object())
    h.attach("sb-aaaaaaaaaa", object())
    h.detach("sb-aaaaaaaaaa")
    h.detach("sb-aaaaaaaaaa")
    assert h.server.attached == ["sb-aaaaaaaaaa"] and h.server.detached == ["sb-aaaaaaaaaa"]
    assert h.stats()["available"] is True and h.worker_pids() == [123]
    srv = h.server
    h.close()
    assert srv.closed and not h.available


def test_host_survives_factory_failure() -> None:
    def boom(**_kw: Any) -> Any:
        raise RuntimeError("no plan kinds")

    h = PlanHost(PoolSettings(sb=make_sb()), cgroup_leaf=None, server_factory=boom)
    assert not h.available and "no plan kinds" in (h.error or "")
    h.attach("sb-aaaaaaaaaa", object())
    assert h.stats() == {"available": False, "error": h.error} and h.worker_pids() == []
    assert not PlanHost(PoolSettings(sb=make_sb()), cgroup_leaf=None, enabled=False).available


def test_real_plan_server_accepts_host_kwargs(tmp_path: Path) -> None:
    s = PoolSettings(sb=make_sb(), worlds_dir=Path(__file__).resolve().parents[2] / "worlds", allow_derive=True,
                     shm_root=tmp_path / "shm")
    h = PlanHost(s, cgroup_leaf=None)
    try:
        assert h.available, h.error
        st = h.stats()
        assert st["available"] is True
    finally:
        h.close()
