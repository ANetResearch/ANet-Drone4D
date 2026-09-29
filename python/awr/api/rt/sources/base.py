"""RingSource：StateRing 读者、生产者健康与纪元判定（M11 §6.4.2、§6.4.8、§9.2 `sources/base.py`；M11-FR-046 至 FR-050）。

LiveSource（`state.sim-core`）与 ReplaySource（`state.replay`，D1-ext）共用本基类与全部下游路径（17 §9.7 第 7 条）。

每个 Gateway tick：`header()`（clock_seq 顺序锁一致性读）→ 生产者健康分类（M11 §6.4.8 `classify`：supervisor 报 FAILED →
FAILED；BACKOFF、STARTING → RESTARTING；心跳年龄 < 250 ms 且 liveliness 未丢失 → OK；< 2 s 且写者进程存活 → STALLED；否则
DOWN）→ 纪元判定（头部 `segment` 变化 → 全局 epoch + 1；`segment` 不变而生产者 `epoch` 变化 → 该生产者 channel 的 RESET；
api 首次 attach 不 + 1，但与 `gw.seen` 不同时补做分类）→ `read_latest(last_seq)`：`swarm/uav/state` 负载为槽的 Lite32 区
（单生产者直接用槽拷贝），有订阅者的 `uav/{id}/state` 按行号从 Full64 区切出 64 B；行号表 `row_of` 在 `roster_version` 变化时
（回放源每帧）从 Full64 每行首 2 字节重建，并通知 Gateway 异步拉取 roster。每 1 s 比较 `identity()` 的 inode 与写者 pid，变化即
重新 attach；`layout_id` 不一致（312）时不转发该生产者。尚未 attach 时健康为 UNATTACHED（supervisor 报 STARTING、BACKOFF 时
为 RESTARTING，FAILED 时为 FAILED），TIME 用占位头部（FR-052）。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from awr.contracts import LAYOUT_ID
from awr.runtime.statering import LOSSY, LayoutMismatch, RingNotReady, StateRing

from ..channels import Channel, ChannelRegistry
from ..clock import GatewayClock, Health

__all__ = ["OK_MS", "STALLED_MS", "PollResult", "ProducerState", "RingSource", "classify"]

ATTACH_RETRY_NS = 200_000_000
OK_MS, STALLED_MS = 250.0, 2000.0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def classify(age_ms: float, pid_alive: bool, sup_state: str | None, liveliness_lost: bool) -> str:
    """M11 §6.4.8：supervisor 状态优先，其次心跳年龄与 liveliness。"""
    if sup_state == "FAILED":
        return Health.FAILED
    if sup_state in ("BACKOFF", "STARTING"):
        return Health.RESTARTING
    if age_ms < OK_MS and not liveliness_lost:
        return Health.OK
    if age_ms < STALLED_MS and pid_alive:
        return Health.STALLED
    return Health.DOWN


@dataclass
class ProducerState:
    name: str
    ring: Any = None
    last_seq: int = 0
    segment: int | None = None
    epoch: int | None = None
    roster_version: int = -1
    health: str = Health.UNATTACHED
    row_of: dict[int, int] = field(default_factory=dict)
    full: bytes = b""
    lite: bytes = b""
    n_rows: int = 0
    t_sim_ns: int = 0
    t_pub_ns: int = 0
    last_header: Any = None
    mapped_ino: int = 0
    writer_pid: int = 0
    last_attach_try_ns: int = 0
    layout_error: str | None = None
    liveliness_lost: bool = False
    was_unhealthy: bool = False  # 自上次 segment 判定以来健康曾非 OK（无 checkpoint 重启的判据，FR-059）
    attached_once: bool = False


@dataclass
class PollResult:
    header: Any = None
    health: str = Health.UNATTACHED
    global_bump: bool = False
    reason: str = ""
    restarted: bool = False  # segment 变化且此前健康非 OK 或写者 pid 变化（无 checkpoint 重启）
    new_frame: bool = False
    frame_t_sim_ns: int = 0
    roster_changed: bool = False
    reset: bool = False


class RingSource:
    """单生产者 StateRing 源（D1；V0.2 起多生产者按 id_base 拼接）。poll(k, t_mono_ns) 在 Gateway 60 Hz tick 中调用。"""

    is_replay = False

    def __init__(self, path: Path, registry: ChannelRegistry, clock: GatewayClock, *, name: str,
                 ring_cls: type = StateRing, on_roster_change: Callable[[str], None] | None = None,
                 on_layout_error: Callable[[str], None] | None = None,
                 sup_state: Callable[[str], str | None] | None = None) -> None:
        self.path = Path(path)
        self.reg = registry
        self.clock = clock
        self.ring_cls = ring_cls
        self.p = ProducerState(name)
        self.swarm: Channel = registry.by_topic["swarm/uav/state"]
        self.uav_state: dict[int, Channel] = {}  # agent_no -> uav/{id}/state channel
        self.on_roster_change = on_roster_change
        self.on_layout_error = on_layout_error
        self.sup_state = sup_state or (lambda _name: None)
        self.stats = {"frames": 0, "attaches": 0, "reattaches": 0, "tick_age_ms": [], "row_rebuilds": 0}
        self.primary = self.p
        self.known_gens: set[int] = set()  # 回放：playback 路径已切过纪元的 gen（FR-050）

    @property
    def name(self) -> str:
        return self.p.name

    def producers(self) -> dict[str, ProducerState]:
        return {self.p.name: self.p}

    # ------------------------------------------------------------ attach
    def _attach(self, t_mono: int) -> bool:
        p = self.p
        if p.ring is not None:
            return True
        if t_mono - p.last_attach_try_ns < ATTACH_RETRY_NS:
            return False
        p.last_attach_try_ns = t_mono
        try:
            ring = self.ring_cls.attach(self.path, expect_layout_id=LAYOUT_ID)
        except (RingNotReady, FileNotFoundError):
            return False
        except LayoutMismatch as e:
            if p.layout_error is None and self.on_layout_error is not None:
                self.on_layout_error(str(e))
            p.layout_error = str(e)
            return False
        with contextlib.suppress(Exception):
            ring.register(LOSSY, "api")
        p.ring = ring
        p.mapped_ino = ring.mapped_ino
        h = ring.header()
        if p.attached_once and h.writer_pid != p.writer_pid:
            p.was_unhealthy = True
        p.writer_pid = h.writer_pid
        p.layout_error = None
        p.last_seq = 0
        p.roster_version = -1
        p.liveliness_lost = False
        p.attached_once = True
        self.stats["attaches"] += 1
        return True

    def _detach(self) -> None:
        p = self.p
        if p.ring is not None:
            with contextlib.suppress(Exception):
                p.ring.close()
        p.ring = None

    def note_liveliness(self, alive: bool) -> None:
        """liveliness `proc/<name>/alive` 的 DELETE 立即把 OK 降为 STALLED（加速判定，不替代心跳，FR-047）。"""
        self.p.liveliness_lost = not alive
        if not alive:
            self.p.was_unhealthy = True

    # ------------------------------------------------------------ poll
    def poll(self, k: int, t_mono: int) -> PollResult:
        r = PollResult()
        p = self.p
        sup = self.sup_state(p.name)
        if not self._attach(t_mono):
            p.health = {"FAILED": Health.FAILED, "BACKOFF": Health.RESTARTING,
                        "STARTING": Health.RESTARTING}.get(sup or "", Health.UNATTACHED)
            r.health = p.health
            return r
        ring = p.ring
        h = p.last_header = ring.header()
        age_ms = (t_mono - h.heartbeat_ns) / 1e6 if h.heartbeat_ns > 0 else float("inf")
        p.health = classify(age_ms, _pid_alive(h.writer_pid), sup, p.liveliness_lost)
        if p.health != Health.OK:
            p.was_unhealthy = True
        self._classify_epoch(p, h, r)
        f = ring.read_latest(p.last_seq)
        if f is not None:
            p.last_seq = f.frame_seq
            if f.roster_version != p.roster_version or self.is_replay:
                self._rebuild_rows(p, f)
                if f.roster_version != p.roster_version:
                    r.roster_changed = True
                    if self.on_roster_change is not None:
                        self.on_roster_change(p.name)
                p.roster_version = f.roster_version
            p.full, p.lite, p.n_rows, p.t_sim_ns, p.t_pub_ns = f.full, f.lite, f.n_rows, f.t_sim_ns, f.t_pub_ns
            r.new_frame = True
            self.stats["frames"] += 1
            ages = self.stats["tick_age_ms"]
            ages.append((t_mono - f.t_pub_ns) / 1e6)
            if len(ages) > 600:
                del ages[:300]
            self.swarm.publish(f.lite, f.t_sim_ns)
            mv = memoryview(f.full)
            for no, ch in self.uav_state.items():
                if ch.subscribers:
                    row = p.row_of.get(no)
                    if row is not None:
                        ch.publish(mv[row * 64:(row + 1) * 64], f.t_sim_ns)
        r.header, r.health, r.frame_t_sim_ns = h, p.health, p.t_sim_ns
        if k % 60 == 0:
            self._check_identity()
        return r

    def _classify_epoch(self, p: ProducerState, h: Any, r: PollResult) -> None:
        """FR-048：segment 变化 → 全局 epoch + 1；segment 不变而 epoch 变化 → RESET；首次 attach 与 gw.seen 比较补做。"""
        if p.segment is None:
            seen = self.clock.seen.get(p.name)
            p.segment, p.epoch = seen if seen is not None else (h.segment, h.epoch)
            if seen is None:
                self.clock.note_seen(p.name, p.segment, p.epoch)
        if h.segment != p.segment:
            if self.is_replay and h.segment in self.known_gens:
                p.segment, p.epoch = h.segment, h.epoch  # 同 gen 的环 segment 变化不重复 + 1（FR-050）
            else:
                r.global_bump, r.reason = True, "segment"
                r.restarted = p.was_unhealthy
                p.segment, p.epoch = h.segment, h.epoch
            p.was_unhealthy = False
            self.clock.note_seen(p.name, p.segment, p.epoch)
        elif h.epoch != p.epoch:
            p.epoch = h.epoch
            self.reg.bump_reset_gen(p.name)
            r.reset = True
            p.was_unhealthy = False
            self.clock.note_seen(p.name, p.segment, p.epoch)

    def _rebuild_rows(self, p: ProducerState, f: Any) -> None:
        full = np.frombuffer(f.full, np.uint8).reshape(-1, 64) if f.n_rows else np.zeros((0, 64), np.uint8)
        nos = full[:, 0].astype(np.int64) | (full[:, 1].astype(np.int64) << 8)
        p.row_of = {int(a): i for i, a in enumerate(nos)}
        self.stats["row_rebuilds"] += 1

    def _check_identity(self) -> None:
        p = self.p
        if p.ring is None:
            return
        try:
            ino, pid, _ep, _sg = p.ring.identity()
        except Exception:
            ino, pid = 0, 0
        if ino != p.mapped_ino or (pid and pid != p.writer_pid):
            self.stats["reattaches"] += 1
            self._detach()
            p.last_attach_try_ns = 0

    def close(self) -> None:
        self._detach()

    def uav_row(self, agent_no: int) -> bytes | None:
        row = self.p.row_of.get(agent_no)
        if row is None or not self.p.full:
            return None
        return bytes(self.p.full[row * 64:(row + 1) * 64])

    def fill_uav_channel(self, agent_no: int, ch: Channel) -> None:
        """新订阅的 `uav/{id}/state`（懒生产，此前未切片）：立即从最近一帧切出当前值，供下一帧 SNAPSHOT。"""
        b = self.uav_row(agent_no)
        if b is not None:
            ch.publish(b, self.p.t_sim_ns)

    def attach_uav_channel(self, agent_no: int, ch: Channel) -> None:
        self.uav_state[agent_no] = ch

    def detach_uav_channel(self, agent_no: int) -> None:
        self.uav_state.pop(agent_no, None)

    def info(self) -> dict[str, Any]:
        return {"attached": self.p.ring is not None, "health": self.p.health, "segment": self.p.segment,
                "epoch": self.p.epoch, "last_seq": self.p.last_seq}
