"""EventIngest 与全局 EventRing（M11-FR-065 至 FR-070；M11 §6.4.12；AWR-17 §6.12、§9.5）。

`awr.runtime.events.EventSubscriber` 订阅 `evt/**`（回调线程只入队），在 Gateway tick 中 `pump()`：按 (producer, epoch)
去重、缺口补拉 `_replay`、重排后按生产者序交付（缺口补齐前该生产者的后续事件暂存，1 s 未补齐则报告缺口并放行）。交付的事件：
- `cmd.*`：先交给 RpcRouter 转成 `result`/`progress`（只发给发起连接）；批量子调用（带 `batch_id`）只汇总，不进 EventRing；
  `cmd.progress` 不进 EventRing；
- `env.keyframe`：只更新 `env/state` 的自包含 latest（EnvCache），不进 EventRing；
- `sim.started`、`sim.reset`：触发 roster 重取，并作纪元判定的一致性校验（与头部矛盾时以头部为准，计 `epoch_mismatch`）；
- `proc.state`（supervisor）：更新进程状态表与 `status{id: "proc.<name>"}`；`seat.*`（sim-core）：刷新席位缓存；
- 其余并入全局 EventRing（65,536 条），分配 Gateway 全局 seq（实例内从 1 起），再按各连接订阅的 `filter{types, levelMin}` 放入
  其待发列表，每 tick 1 条用 `event`、≥ 2 条合并为 `events`（≤ 256 项/条）。
api 自身产生的事件（`fleet.batch.progress`、`cmd.rejected`、`proc.state`、`sys.shutting_down`、`session.*`）经 `emit_api()`
直接并入同一 EventRing（producer = "api"）。无法补齐的缺口：订阅了事件的连接下一帧置 GAP，并发 `status{id: "events.gap"}`。
WS 事件字段：`seq`、`t_sim_ns`、`t_wall_ns`（十进制字符串）、`type`（= kind）、`level`（= severity）、`producer`、`uav`、`cid`、`data`。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from itertools import islice
from typing import TYPE_CHECKING, Any

from awr.runtime.events import EventSubscriber

from .protocol import status_msg

if TYPE_CHECKING:
    from .gateway import Gateway

__all__ = ["EVENT_RING", "EventIngest"]

log = logging.getLogger("awr.api.events")

EVENT_RING = 65536
# 首次见到生产者时，首条 seq ≤ 1024 即从 1 起补拉（生产者 _replay 环 4096 条，不会截断）：api 晚于 sim-core 启动时，
# sim-core 启动阶段的事件（scenario.loaded、mission.created 等）也进入 EventRing（M16-to-M11 第 1 条，INT-1）
BACKFILL_FIRST_MAX = 1024
NOT_IN_RING = {"cmd.progress", "env.keyframe", "path.changed"}
EPOCH_CHECK_NS = 1_000_000_000


class EventIngest:
    def __init__(self, gw: Gateway, *, subscribe: bool = True) -> None:
        self.gw = gw
        self.ring: deque[dict[str, Any]] = deque(maxlen=EVENT_RING)
        self.gseq = 0
        self.sub = EventSubscriber(gw.bus, on_events=self._on_events, on_gap=self._on_gap, subscribe=subscribe,
                                   backfill_first_max=BACKFILL_FIRST_MAX)
        self.stats = {"delivered": 0, "gaps": 0, "api_events": 0}
        self._epoch_checks: list[tuple[int, int, int, str]] = []  # (segment, epoch, t_mono, reason)

    @property
    def oldest(self) -> int:
        return self.ring[0]["seq"] if self.ring else self.gseq + 1

    @property
    def newest(self) -> int:
        return self.gseq

    def pump(self) -> None:
        self.sub.pump()
        if self._epoch_checks:
            self._check_epoch_consistency()

    def _on_events(self, producer: str, evs: list[dict]) -> None:
        gw = self.gw
        for ev in evs:
            kind = str(ev.get("kind", ""))
            if kind.startswith("cmd."):
                gw.rpc.on_cmd_event(ev)
            if ev.get("batch_id"):
                continue
            if kind == "env.keyframe":
                gw.env.on_keyframe(ev)
                continue
            if kind == "path.changed":  # M10 内部事件：api 据此推送 uav/{id}/path，不转发客户端
                gw.on_path_changed(ev.get("data") if isinstance(ev.get("data"), dict) else {})
                continue
            if kind in NOT_IN_RING:
                continue
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            if kind in ("sim.started", "sim.reset") and producer == gw.settings.producer:
                gw.on_producer_started(ev)
                if isinstance(data.get("segment"), int) and isinstance(data.get("epoch"), int):
                    self._epoch_checks.append((data["segment"], data["epoch"], time.monotonic_ns(),
                                               str(data.get("reason", ""))))
            elif kind == "proc.state":
                gw.on_proc_state(data)
            elif kind.startswith("seat.") and producer == gw.settings.producer:
                gw.on_seat_event(kind, data)
            self.append(kind, ev.get("t_sim_ns", 0), ev.get("t_wall_ns", 0), ev.get("severity", 0), producer,
                        ev.get("uav"), ev.get("cid"), data)

    def _check_epoch_consistency(self) -> None:
        """`sim.started{reason}` 只作一致性校验：1 s 内头部未出现对应 (segment, epoch) 时以头部为准并计 `epoch_mismatch`。"""
        p = self.gw.source.p
        now = time.monotonic_ns()
        keep = []
        for seg, ep, t, reason in self._epoch_checks:
            if p.segment == seg and p.epoch == ep:
                continue
            if p.segment is not None and seg < p.segment:
                continue  # 已被更新的 segment 取代
            if now - t < EPOCH_CHECK_NS:
                keep.append((seg, ep, t, reason))
                continue
            self.gw.metrics.counters["epoch_mismatch"] += 1
            log.warning("epoch classification mismatch; ring header wins",
                        extra={"kv": {"event_segment": seg, "event_epoch": ep, "header_segment": p.segment,
                                      "header_epoch": p.epoch, "reason": reason}})
        self._epoch_checks = keep

    def append(self, kind: str, t_sim_ns: int, t_wall_ns: int, level: int, producer: str, uav: str | None,
               cid: str | None, data: dict) -> dict:
        self.gseq += 1
        e = {"seq": self.gseq, "t_sim_ns": int(t_sim_ns or 0), "t_wall_ns": str(int(t_wall_ns or 0)), "type": kind,
             "level": max(0, min(3, int(level or 0))), "producer": producer, "uav": uav, "cid": cid, "data": data}
        self.ring.append(e)
        self.stats["delivered"] += 1
        for s in self.gw.sessions:
            if s.hello and s.events_on and not s.closing and s.event_ok(e):
                s.pending_events.append(e)
        return e

    def emit_api(self, kind: str, level: int, data: dict, *, uav: str | None = None, cid: str | None = None) -> dict:
        self.stats["api_events"] += 1
        return self.append(kind, self.gw.clock.t_sim_ns, time.time_ns(), level, "api", uav, cid, data)

    def since(self, seq: int, limit: int | None = None) -> list[dict]:
        """seq 在环内连续递增：从 `seq + 1` 对应的位置切片（不逐条比较）。"""
        start = max(0, seq + 1 - self.oldest)
        stop = None if limit is None else start + limit
        return list(islice(self.ring, start, stop))

    def _on_gap(self, producer: str, epoch: int, lo: int, hi: int) -> None:
        self.stats["gaps"] += 1
        for s in self.gw.sessions:
            if s.events_on:
                s.event_gap = True
                s.send_ctrl(status_msg("events.gap", "warning", f"事件缺口 {producer} [{lo}, {hi}]", source=producer,
                                       code=319))

    def close(self) -> None:
        self.sub.close()
