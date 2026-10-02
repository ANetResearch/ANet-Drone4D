"""replay-worker 宿主：`python -m awr.recorder.replay`（M12 §6.7、§7.4；FR-041 至 FR-051；M11-FR-050、FR-078、FR-079）。

api 经 supervisor `sys/start{name: replay-worker, args{run, segment}}` 按需启动（参数映射为 `--run`、`--segment`）；本进程
服务 `ctl/replay-worker/{open,seek,play,pause,speed,close,query}` 与 `ctl/replay/roster`（回调线程只入队，主循环 ≤ 250 Hz
处理），写回放环 `/dev/shm/awr/<当前运行>/state.replay`（头部 flags bit0 REPLAY），事件发往 `evt/replay/*`，低频发往
`state/replay/*`。写入顺序固定为：新一代复合帧 → 发出回复 → `set_segment(gen)`，Gateway 只以回复切换全局 epoch。
关闭后 60 s（墙钟）无新 open 则退出（api 未能调用 `sys/stop` 时的兜底，W12）；启动后 60 s 内无 open 同样退出。
兼容性以当前运行的绑定判定（world、`contentVersion`、`coordinate.sha256`、`layout_id` 不一致返回 122）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import queue
import time
from pathlib import Path
from typing import Any

from awr.contracts import LAYOUT_ID, bus_keys
from awr.contracts.reasons import Reason
from awr.runtime.bus import Bus
from awr.runtime.source import SourceIncompatible, SourceSpec
from awr.runtime.statering import RING_FLAG_REPLAY, StateRing

from .assembler import ReplayEventPublisher, ReplayPublishers
from .config import ReplayCfg, load_replay_cfg
from .mcap_source import McapSource, ReplayError

__all__ = ["ReplayHost", "main"]

log = logging.getLogger("awr.recorder.replay")

OPS = ("open", "seek", "play", "pause", "speed", "close", "query")


class ReplayHost:
    def __init__(self, bus: Bus, *, run_dir: Path, runs_dir: Path, world_id: str, current_binding: dict[str, Any],
                 cfg: ReplayCfg | None = None, ring_cls: type[StateRing] = StateRing) -> None:
        self.bus = bus
        self.cfg = cfg or ReplayCfg()
        self.run_dir = Path(run_dir)
        self.runs_dir = Path(runs_dir)
        self.world_id = world_id
        self.binding = current_binding
        self.ring_cls = ring_cls
        self.src = McapSource(self.cfg, runs_dir=self.runs_dir, bus=bus)
        # 入队时刻（单调 ns）随请求一起排队：回复带 `queue_ms`（回调入队到主循环处理的等待），用于区分 worker 排队与网络或网关时延
        self.inbox: queue.SimpleQueue[tuple[str, Any, int]] = queue.SimpleQueue()
        self.handles = [bus.serve(bus_keys.ctl_replay_worker(op), lambda r, op=op: self.inbox.put((op, r, time.monotonic_ns())))
                        for op in OPS]
        self.handles.append(bus.serve(bus_keys.ctl_roster("replay"), lambda r: self.inbox.put(("roster", r, time.monotonic_ns()))))
        self.slow_ops: list[tuple[str, float]] = []  # 处理耗时 ≥ 100 ms 的控制请求（诊断）
        self.events = ReplayEventPublisher(bus, "replay", 0)
        self.pubs = ReplayPublishers(bus)
        self.ring: StateRing | None = None
        self.roster_sent: bytes | None = None  # 最近一次随 open/seek 回复发出的 roster（seek 时不变则不再随回复发送）
        self.pending_segment: int | None = None
        self.idle_since = time.monotonic()
        self.ops = {op: 0 for op in (*OPS, "roster")}

    @property
    def ring_path(self) -> Path:
        return self.run_dir / "state.replay"

    def alive(self) -> bool:
        if self.src.state != "idle":
            self.idle_since = time.monotonic()
            return True
        return time.monotonic() - self.idle_since < self.cfg.idle_exit_s

    # ------------------------------------------------------------ 控制
    def drain_ctl(self, limit: int = 64) -> int:
        n = 0
        while n < limit:
            try:
                op, req, t_in = self.inbox.get_nowait()
            except queue.Empty:
                break
            n += 1
            self.ops[op] = self.ops.get(op, 0) + 1
            t_start = time.monotonic_ns()
            try:
                rep = self.handle(op, req.msg() or {})
            except Exception:
                log.exception("replay control failed", extra={"kv": {"op": op}})
                rep = {"v": 1, "status": "rejected", "code": int(Reason.INTERNAL_ERROR)}
            if op != "roster":
                rep["queue_ms"] = round((t_start - t_in) / 1e6, 3)
            req.reply_msg(rep)
            took_ms = (time.monotonic_ns() - t_start) / 1e6
            if took_ms >= 100:
                self.slow_ops.append((op, round(took_ms, 1)))
                del self.slow_ops[:-32]
                log.info("replay control slow", extra={"kv": {"op": op, "took_ms": round(took_ms, 1),
                                                              "queue_ms": round((t_start - t_in) / 1e6, 1)}})
            if self.pending_segment is not None and self.ring is not None:
                self.ring.set_segment(self.pending_segment)  # 回复发出之后才写 gen（M12 §7.4）
                self.pending_segment = None
        return n

    def handle(self, op: str, m: dict[str, Any]) -> dict[str, Any]:
        base = {"v": 1, "cid": m.get("cid"), "code": 0}
        if op == "roster":
            return self.src.roster() or {"v": 1, "producer": "replay", "roster_version": 0, "id_base": 0, "id_count": 0, "entries": []}
        try:
            if op == "open":
                return base | self._open(str(m.get("run", "")), int(m.get("segment", 0)))
            if self.src.state == "idle":
                if op == "close":
                    return base | {"status": "accepted", "state": "idle"}
                raise ReplayError(int(Reason.REPLAY_NOT_OPEN), "not open")
            if op == "seek":
                t = self.src.seek(int(m.get("t_ns", m.get("seek_ns", 0))))
                self.pending_segment = self.src.gen
                return base | {"status": "accepted", "t_ns": t, "t_sample_ns": self.src.t_sample, "gen": self.src.gen,
                               "ring_head": self.ring.head if self.ring is not None else 0, "backfill": self._backfill(),
                               "worker_ms": round(self.src.last_seek_ms[-1], 3) if self.src.last_seek_ms else None}
            if op == "play":
                self.src.play()
                return base | {"status": "accepted", "state": self.src.state}
            if op == "pause":
                self.src.pause()
                return base | {"status": "accepted", "state": self.src.state}
            if op == "speed":
                sp, warn = self.src.set_speed(float(m.get("speed", 1.0)))
                return base | {"status": "accepted", "speed": sp, "warnings": warn}
            if op == "query":
                return base | {"status": "accepted"} | self.src.query(m.get("query") if isinstance(m.get("query"), dict) else m)
            if op == "close":
                self._close()
                return base | {"status": "accepted", "state": "idle"}
        except SourceIncompatible as e:
            return base | {"status": "rejected", "code": int(Reason.RECORDING_INCOMPATIBLE), "detail": str(e)}
        except ReplayError as e:
            return base | {"status": "rejected", "code": e.code, "detail": str(e)}
        return base | {"status": "rejected", "code": int(Reason.BAD_REQUEST)}

    def _open(self, run: str, segment: int) -> dict[str, Any]:
        if self.src.state != "idle":
            self._close()
        b = self.binding
        spec = SourceSpec(run=run, segment=segment, world_id=self.world_id, layout_id=int(b.get("layout_id", LAYOUT_ID)),
                          content_version=b.get("content_version") or None, coordinate_sha256=b.get("coordinate_sha256") or None)
        ring = self.ring_cls.create(self.ring_path, layout_id=LAYOUT_ID, flags=RING_FLAG_REPLAY)
        self.ring = ring
        try:
            info = self.src.open(spec, ring, self.events, pubs=self.pubs)
        except BaseException:
            self._drop_ring()
            raise
        self.pending_segment = self.src.gen
        return {"status": "accepted", "data_start_ns": info.data_start_ns, "data_end_ns": info.data_end_ns,
                "speed_max": info.speed_max, "decimation_s": self.src.decimation_s, "lineage": self.src.lineage,
                "binding": self.src.index.binding() if self.src.index else {}, "gen": self.src.gen,
                "backfill": self._backfill(always_roster=True), "warnings": info.warnings}

    def _backfill(self, *, always_roster: bool = False) -> dict[str, Any]:
        """backfill 包；seek 时 roster 与上次发出的相同则置 None（N = 1000 时 roster 约 90 KB，占 seek 回复一半；网关对 None 保留
        当前 roster，不重建，FX2-R2-gateway）。open 总是带 roster。"""
        bf = self.src.backfill_bundle()
        ros = bf.get("roster")
        if not always_roster and ros is not None and ros == self.roster_sent:
            bf["roster"] = None
        else:
            self.roster_sent = ros
        return bf

    def _close(self) -> None:
        self.roster_sent = None
        self.src.close()
        self.events.flush()
        self._drop_ring()
        self.idle_since = time.monotonic()

    def _drop_ring(self) -> None:
        r, self.ring = self.ring, None
        if r is None:
            return
        r.close()
        remove = getattr(self.ring_cls, "remove", None)
        if remove is not None:
            remove(self.ring_path)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.ring_path)

    # ------------------------------------------------------------ 循环体
    def step(self, now_mono_ns: int | None = None) -> None:
        self.drain_ctl()
        self.src.step(time.monotonic_ns() if now_mono_ns is None else now_mono_ns)
        self.events.flush()

    def close(self) -> None:
        self._close()
        for h in self.handles:
            h.close()
        self.events.close()
        self.pubs.close()


def _current_binding(persist_dir: Path, world_id: str) -> dict[str, Any]:
    try:
        m = json.loads((persist_dir / "meta.json").read_text(encoding="utf-8"))
        b = dict(m.get("binding") or {})
    except (OSError, ValueError):
        b = {}
    b.setdefault("world_id", world_id)
    b.setdefault("layout_id", LAYOUT_ID)
    return b


def main() -> int:
    from awr.runtime.bus import ZenohBus
    from awr.runtime.child import init_child
    from awr.runtime.heartbeat import Heartbeat

    ap = argparse.ArgumentParser(description="replay-worker（M12）")
    ap.add_argument("--run", default="")
    ap.add_argument("--segment", type=int, default=0)
    a, _rest = ap.parse_known_args()
    ctx = init_child("replay-worker")
    cfg = load_replay_cfg()
    bus = ZenohBus.open("replay-worker", ctx)
    runs_dir = Path(os.environ.get("AWR_RUNS_DIR") or ctx.persist_dir.parent)
    host = ReplayHost(bus, run_dir=ctx.run_dir, runs_dir=runs_dir, world_id=ctx.world_id,
                      current_binding=_current_binding(ctx.persist_dir, ctx.world_id), cfg=cfg)
    hb = Heartbeat(ctx.hb_path)
    ready = bus.ready()
    period = int(1e9 / cfg.loop_hz_max)
    log.info("replay-worker ready", extra={"kv": {"run": a.run, "segment": a.segment}})
    try:
        while not ctx.stopping and host.alive():
            t0 = time.monotonic_ns()
            hb.beat()
            host.step(t0)
            dt = t0 + period - time.monotonic_ns()
            if dt > 0:
                time.sleep(dt / 1e9)
    finally:
        host.close()
        ready.close()
        bus.close()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
