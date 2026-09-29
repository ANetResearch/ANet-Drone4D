"""GatewayClock：TIME 合成、全局 epoch 与 `gw.epoch`、`gw.seen` 持久化（M11-FR-048、FR-052、FR-053；M11 §6.3.6、§6.4.8、§6.4.9；AWR-17 §6.4、§6.10、§9.6）。

- `t_srv_ns` = 头部 `heartbeat_ns − gw_t0`（Gateway 单调时钟，相对 api 进程启动时刻），与 `t_sim_ns` 来自同一次头部一致性读；
- `state` 先取头部 `clock_state`，再按生产者健康覆盖：STALLED、DOWN、UNATTACHED → STALLED；RESTARTING → RESTARTING；
  FAILED → FAILED（bit7 REPLAY 保留）；尚无生产者时发 `t_sim_ns = 0`、`t_srv_ns = 当前单调时钟 − gw_t0`；
- TIME 10 Hz（k % 6 == 0），state、rate 或 epoch 变化时在同一 tick 立即发送；
- 全局 epoch：线上 u16（只做相等比较），`/dev/shm/awr/<run>/gw.epoch` 为 u32 小端，写 tmp 后 `os.replace`；api 重启后沿用；
  `gw.seen` 为 M11 内部格式 msgpack `{v: 1, producers: {name: [segment, epoch]}}`。
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path

import msgpack

from awr.contracts import frame as F
from awr.contracts.enums import TimeState

__all__ = ["GatewayClock", "Health"]

TIME_EVERY_TICKS = 6  # 60 Hz tick 下 10 Hz


class Health:
    OK = "OK"
    STALLED = "STALLED"
    DOWN = "DOWN"
    RESTARTING = "RESTARTING"
    FAILED = "FAILED"
    UNATTACHED = "UNATTACHED"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class GatewayClock:
    def __init__(self, run_dir: Path | None, *, mono_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self.mono_ns = mono_ns
        self.gw_t0 = mono_ns()
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self.global_epoch = 1
        self.seen: dict[str, tuple[int, int]] = {}
        if self.run_dir is not None:
            with contextlib.suppress(OSError, ValueError):
                self.global_epoch = int.from_bytes((self.run_dir / "gw.epoch").read_bytes()[:4], "little") or 1
            with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
                d = msgpack.unpackb((self.run_dir / "gw.seen").read_bytes(), raw=False)
                self.seen = {k: (int(v[0]), int(v[1])) for k, v in d["producers"].items()}
        self.state = int(TimeState.STALLED)
        self.rate = 1.0
        self.t_sim_ns = 0
        self.t_srv_ns = 0
        self.last_key: tuple | None = None
        self.time_dirty = True
        self.bumps = 0

    @property
    def epoch_u16(self) -> int:
        return self.global_epoch & 0xFFFF

    def update(self, h, health: str, *, replay: bool = False) -> None:
        """用本 tick 的头部一致性读结果合成 TIME 字段；state、rate、epoch 变化时置 time_dirty。"""
        now = self.mono_ns()
        if h is None:
            st, t_sim, t_srv, rate = int(TimeState.STOPPED), 0, now - self.gw_t0, 1.0
        else:
            st, t_sim, t_srv, rate = int(h.clock_state), int(h.t_sim_ns), int(h.heartbeat_ns) - self.gw_t0, h.rate_milli / 1000.0
        if health in (Health.STALLED, Health.DOWN, Health.UNATTACHED):
            st = (st & 0x80) | int(TimeState.STALLED)
        elif health == Health.RESTARTING:
            st = (st & 0x80) | int(TimeState.RESTARTING)
        elif health == Health.FAILED:
            st = (st & 0x80) | int(TimeState.FAILED)
        if replay:
            st |= 0x80
        self.t_sim_ns, self.t_srv_ns, self.rate = t_sim, t_srv, float(rate)
        key = (st, self.rate, self.epoch_u16)
        if key != self.last_key:
            self.time_dirty, self.last_key, self.state = True, key, st

    def time_bytes(self) -> bytes:
        return F.encode_time(self.state & 0x0F, self.global_epoch, self.rate, self.t_sim_ns, self.t_srv_ns,
                             replay=bool(self.state & 0x80))

    def time_bytes_if_due(self, k: int) -> bytes | None:
        if self.time_dirty or k % TIME_EVERY_TICKS == 0:
            self.time_dirty = False
            return self.time_bytes()
        return None

    def sim_now_ns(self, now_mono: int | None = None) -> int:
        """PLAYING/LIVE 时按 rate 外推（上限 1 s），其余冻结（pong.sim_ns，M11-FR-053）。"""
        if (self.state & 0x0F) not in (TimeState.PLAYING, TimeState.LIVE):
            return self.t_sim_ns
        now = self.mono_ns() if now_mono is None else now_mono
        dt = min(max(0, now - self.gw_t0 - self.t_srv_ns), 1_000_000_000)
        return self.t_sim_ns + int(self.rate * dt)

    def srv_now_ns(self) -> int:
        return self.mono_ns() - self.gw_t0

    def bump(self, reason: str) -> None:
        self.global_epoch = (self.global_epoch + 1) & 0xFFFFFFFF
        self.bumps += 1
        self.persist()
        self.last_key = (self.state, self.rate, self.epoch_u16)
        self.time_dirty = False

    def persist(self) -> None:
        if self.run_dir is None:
            return
        with contextlib.suppress(OSError):
            self.run_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.run_dir / "gw.epoch", (self.global_epoch & 0xFFFFFFFF).to_bytes(4, "little"))
            _atomic_write(self.run_dir / "gw.seen", msgpack.packb({"v": 1, "producers": {k: [v[0], v[1]] for k, v in
                                                                                         self.seen.items()}}))

    def note_seen(self, name: str, segment: int, epoch: int) -> None:
        if self.seen.get(name) != (segment, epoch):
            self.seen[name] = (segment, epoch)
            self.persist()
