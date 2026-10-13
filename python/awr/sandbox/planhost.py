"""共享规划服务宿主（M17-FR-032 至 FR-034；AWR-04 §4.9、ADR-113；M10 WP-15 的 `PlanServer`，见 `d2-WP-15-to-M17.md`）。

池启动时构造一次 `PlanServer`：worker 数 `planning.workers`（1，可配 2）、`nice +10`、叶子组 `sandbox/plan/`（`cpu.max 0.5` 核、
`memory.max 250M`，由池在构造前建好，worker 自己 join；无委派时只以 nice 运行）、世界 LRU `planning.world_lru`（2）、公开模式
`allow_derive = false`（缓存缺失以 123 失败，绝不在线派生）。会话 SPAWNING 时把该会话的总线会话交给 `attach_bus(sid, bus)`
（声明 `ctl/plan/submit` 与 `ctl/plan/cancel`），结束或切换底图时 `detach_bus(sid)`（排队作业回复 canceled、删除结果文件、
撤销 queryable）。公平队列、超时强杀与重建、10 min 内 3 次超时冻结 5 min（586）、每会话在途与排队 ≤ 8 均由 PlanServer 执行。

构造失败（例如规划插件不可导入）时池照常运行，规划作业由 sim-core 的 PlanPoolClient 得到无回复（M10 按 213 处理），并记错误日志。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from .config import PoolSettings

__all__ = ["PlanHost"]

log = logging.getLogger("awr.sandbox.planhost")


class PlanHost:
    def __init__(self, settings: PoolSettings, *, cgroup_leaf: str | None, enabled: bool = True,
                 server_factory: Any = None) -> None:
        self.s = settings
        self.server: Any = None
        self.attached: set[str] = set()
        self.error: str | None = None
        if not enabled:
            return
        p = settings.sb.planning
        try:
            if server_factory is None:
                from awr.sim.planning.server import PlanServer as server_factory
            self.server = server_factory(workers=p.workers, worlds_dir=settings.worlds_dir, allow_derive=settings.allow_derive,
                                         cgroup_leaf=cgroup_leaf, lru_worlds=p.world_lru, shm_root=settings.shm_root,
                                         nice=p.nice, queue_max=p.pending_per_session, freeze_window_s=p.freeze.window_s,
                                         freeze_count=p.freeze.timeouts, freeze_s=p.freeze.freeze_s)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log.exception("plan server unavailable; sandbox planning disabled")

    @property
    def available(self) -> bool:
        return self.server is not None

    def attach(self, sid: str, bus: Any) -> None:
        if self.server is None or sid in self.attached:
            return
        try:
            self.server.attach_bus(sid, bus)
            self.attached.add(sid)
        except Exception:
            log.exception("plan attach failed", extra={"kv": {"sid": sid}})

    def detach(self, sid: str) -> None:
        if self.server is None or sid not in self.attached:
            return
        self.attached.discard(sid)
        with contextlib.suppress(Exception):
            self.server.detach_bus(sid)

    def stats(self) -> dict[str, Any]:
        if self.server is None:
            return {"available": False, "error": self.error}
        try:
            return {"available": True, **self.server.stats()}
        except Exception as e:
            return {"available": True, "error": str(e)}

    def worker_pids(self) -> list[int]:
        if self.server is None:
            return []
        with contextlib.suppress(Exception):
            return list(self.server.worker_pids())
        return []

    def close(self) -> None:
        if self.server is not None:
            with contextlib.suppress(Exception):
                self.server.close()
            self.server = None
