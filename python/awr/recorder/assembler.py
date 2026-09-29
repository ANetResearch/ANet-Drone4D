"""回放帧与低频发布（M12 §6.7.4、§6.7.5、§6.7.8；FR-043、FR-044、FR-049）。

- 复合帧：一个环槽 = 同一录制时刻的整群 Lite32 块（n 行原字节）+ 该时刻有 Full64 消息的标记机行（m ≤ n，行首 agent_no
  自描述），SlotHeader `flags.REPLAY`、`roster_version` 取块头；Gateway 与实时一样每 tick 只读最新槽；
- 事件：按 log_time 顺序经 `ReplayEventPublisher`（producer = `replay`，epoch = 回放生成号 gen）发布到 `evt/replay/<cat>`，
  保留原事件的 kind、severity、t_sim_ns、t_wall_ns、uav、cid、data（缺口由 EventPublisher 自带的 `_replay` 补拉）；
- 低频：`state_ext`、`safety` 为关键块 + 增量合并后的每机最新值，以实时相同的 `[[agent_no, item], ...]` 批发往
  `state/replay/{ext,safety}`；传感器位姿行拼接为 `{v, t_sim_ns, rows}` 发往 `state/replay/sensor`；任务状态批发往
  `state/replay/mission`；EnvKeyframe 心跳原字节发往 `state/replay/env`。
"""

from __future__ import annotations

from typing import Any

import msgpack
import numpy as np

from awr.contracts import bus_keys
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32
from awr.runtime.bus import Bus
from awr.runtime.events import EventPublisher
from awr.runtime.statering import SLOT_REPLAY, StateRing

from .formats import BLOCK_HDR, FULL_ROW, LITE_ROW

__all__ = ["ReplayEventPublisher", "ReplayPublishers", "publish_composite", "replay_state_key"]

PRODUCER = "replay"


def replay_state_key(const: str) -> str:
    """`state/sim-core/<x>` → `state/replay/<x>`（mission、env 没有按生产者的构造函数，M11 同法）。"""
    parts = const.split("/")
    parts[1] = PRODUCER
    return "/".join(parts)


class ReplayEventPublisher(EventPublisher):
    """回放事件：seq、epoch、producer 由回放分配，其余字段保留录制值（M12-AC-044 比较除 seq、epoch、producer 外的字段）。"""

    def emit_recorded(self, ev: dict[str, Any]) -> int:
        data = ev.get("data")
        seq = self.emit(str(ev.get("kind", "")), t_sim_ns=int(ev.get("t_sim_ns", 0) or 0), severity=int(ev.get("severity", 0) or 0),
                        uav=ev.get("uav"), cid=ev.get("cid"), batch_id=ev.get("batch_id"),
                        fields=dict(data) if isinstance(data, dict) else {})
        last = self._ring[-1]
        if "t_wall_ns" in ev:
            last["t_wall_ns"] = ev["t_wall_ns"]
        return seq


class ReplayPublishers:
    def __init__(self, bus: Bus) -> None:
        self.ext = bus.publisher(bus_keys.state_ext(PRODUCER))
        self.safety = bus.publisher(bus_keys.state_safety(PRODUCER))
        self.sensor = bus.publisher(bus_keys.state_sensor(PRODUCER))
        self.mission = bus.publisher(replay_state_key(bus_keys.STATE_MISSION))
        self.env = bus.publisher(replay_state_key(bus_keys.STATE_ENV))
        self.stats = {"ext": 0, "safety": 0, "sensor": 0, "mission": 0, "env": 0}

    def put(self, which: str, payload: bytes) -> None:
        getattr(self, which).put(payload)
        self.stats[which] += 1

    def close(self) -> None:
        for p in (self.ext, self.safety, self.sensor, self.mission, self.env):
            p.close()


def publish_composite(ring: StateRing, t_b: int, block_payload: bytes | memoryview, full_rows: list[bytes | memoryview]) -> int:
    """复合帧写入回放环；返回 frame_seq。block_payload 为去掉 RecPrefix8 的 SwarmLite32Block。"""
    n, rv, _ = BLOCK_HDR.unpack_from(block_payload, 0)
    n = min(n, (len(block_payload) - BLOCK_HDR.size) // LITE_ROW, ring.capacity)
    lite = np.frombuffer(block_payload, SWARM_LITE32, count=n, offset=BLOCK_HDR.size)
    m = min(len(full_rows), n)
    full = np.frombuffer(b"".join(bytes(r[:FULL_ROW]) for r in full_rows[:m]), DRONE_STATE64, count=m) if m else np.zeros(0, DRONE_STATE64)
    return ring.publish(full, lite, int(t_b), int(rv), flags=SLOT_REPLAY)


def pack(x: Any) -> bytes:
    return msgpack.packb(x, use_bin_type=True)
