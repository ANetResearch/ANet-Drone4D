"""事件与状态批发布（M14 §7.2、§7.3；M14-FR-037、FR-063、FR-064；NFR-007）。

- 事件：`evt/agent-runtime/agent`（EventPublisher，每 50 ms【墙钟】至多一次 put，`_replay` 环 4096）；每条证据记录同时以同名
  `agent.*` 事件发出（`data` = payload 加 `chain`、`seq`、`id`）。
- `state/agent-runtime/agents`：1 Hz【墙钟】，全部 agent 的 status 批 `{v, t_sim_ns, items: [awr.agent.status.v1]}`；
  Gateway 拆为 `agent/{aid}/status`。
- `state/agent-runtime/tasks`：变化驱动（≤ 4 Hz 合批）加 1 Hz 心跳，完整 `awr.agent.tasks.v1` 帧；单帧 ≤ 16 KiB（超出时丢最旧的
  终态任务），Gateway 原样转为 `agent/tasks`。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from awr.contracts import bus_keys
from awr.runtime.bus import Bus, pack
from awr.runtime.events import EventPublisher

__all__ = ["MAX_TASKS_FRAME_BYTES", "Publisher", "fit_tasks_frame"]

log = logging.getLogger("awr.agent.publisher")

MAX_TASKS_FRAME_BYTES = 16 * 1024
EVENT_FLUSH_S = 0.05
TASKS_MIN_INTERVAL_S = 0.25
HEARTBEAT_S = 1.0


def fit_tasks_frame(frame: dict[str, Any], limit: int = MAX_TASKS_FRAME_BYTES) -> tuple[dict[str, Any], bytes]:
    """按 msgpack 大小裁剪：先去掉最旧的终态任务，再去掉最旧的任务，直到 ≤ limit。"""
    tasks = list(frame.get("tasks") or [])
    while True:
        f = {**frame, "tasks": tasks}
        raw = pack(f)
        if len(raw) <= limit or not tasks:
            return f, raw
        idx = next((i for i in range(len(tasks) - 1, -1, -1)
                    if tasks[i].get("state") in ("completed", "failed", "canceled", "rejected")), len(tasks) - 1)
        tasks.pop(idx)


class Publisher:
    def __init__(self, bus: Bus, *, epoch: int = 1, mono: Callable[[], float] = time.monotonic) -> None:
        self.bus = bus
        self.events = EventPublisher(bus, "agent-runtime", epoch)
        self.mono = mono
        self._agents_pub = bus.publisher(bus_keys.state_agent("agents"))
        self._tasks_pub = bus.publisher(bus_keys.state_agent("tasks"))
        self._dirty = False
        self._t_tasks = 0.0
        self._t_flush = 0.0
        self.core: Any = None
        self.stats = {"events": 0, "tasks_frames": 0, "agents_batches": 0, "max_tasks_bytes": 0}

    def emit(self, kind: str, data: dict[str, Any], level: int) -> None:
        self.stats["events"] += 1
        sim_t = self.core.sched.now_ns() if self.core is not None else 0
        uav = data.get("vehicle_id") if isinstance(data.get("vehicle_id"), str) else None
        self.events.emit(kind, t_sim_ns=int(sim_t), severity=int(level), uav=uav, fields=dict(data))

    def mark_dirty(self, *_a: Any) -> None:
        self._dirty = True

    def flush_events(self) -> int:
        now = self.mono()
        if now - self._t_flush < EVENT_FLUSH_S:
            self.events.serve_replays()
            return 0
        self._t_flush = now
        return self.events.flush()

    def publish_tasks(self, *, force: bool = False) -> bool:
        if self.core is None:
            return False
        now = self.mono()
        due_hb = now - self._t_tasks >= HEARTBEAT_S
        if not (force or due_hb or (self._dirty and now - self._t_tasks >= TASKS_MIN_INTERVAL_S)):
            return False
        _f, raw = fit_tasks_frame(self.core.tm.tasks_frame())
        self._tasks_pub.put(raw)
        self._dirty = False
        self._t_tasks = now
        self.stats["tasks_frames"] += 1
        self.stats["max_tasks_bytes"] = max(self.stats["max_tasks_bytes"], len(raw))
        return True

    def publish_agents(self) -> None:
        if self.core is None:
            return
        self._agents_pub.put(pack({"v": 1, "t_sim_ns": self.core.sched.now_ns(), "items": self.core.status_rows()}))
        self.stats["agents_batches"] += 1

    def emit_runtime_state(self, state: str) -> None:
        self.emit("agent.runtime.state", {"state": state}, 2 if state == "DEGRADED" else 1)

    def close(self) -> None:
        self.events.close()
        self._agents_pub.close()
        self._tasks_pub.close()
