"""Channel 与 ChannelRegistry（M11-FR-030、FR-034、FR-038；M11 §6.3.2；AWR-17 §6.3 advertise、§6.4 记录头、§6.6）。

- channel id（u16）在 Gateway 实例内稳定：topic、encoding、schemaName 完全相同才复用；机体移除后 id 不回收（下次同 topic
  重新出现时复用同一 id）；
- `Channel.record(frame_t_ns, reset)` 按 (seq, reset, 帧时刻) 缓存编码结果（最多 2 份：RESET 与否），所有连接共享同一 bytes
  （编码一次，FR-038）；记录头 `dt_us` = 样本时刻 − 帧头 `t_sim_ns`；自包含 channel 置 KEYFRAME，生产者纪元变化后的下一条
  记录置 RESET；`ENCODES` 为全局编码计数（`perf/server.api.encodes_per_s`）；
- `swarm/state` 只作为订阅别名，advertise 只出现规范名 `swarm/uav/state`（ADR-047）；
- 通配匹配：`*` 一个 chunk，`**` 零个或多个 chunk（与 `awr.runtime.bus.key_matches` 同一实现）；按首段分桶，避免每次
  通配订阅扫描全部 channel。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from awr.contracts import frame as F
from awr.contracts import layouts as L
from awr.contracts import topics as T
from awr.runtime.bus import key_matches

__all__ = ["ENC", "ENCODES", "Channel", "ChannelRegistry", "canonical_topic"]

ENC = {"raw": F.ENC_RAW, "msgpack": F.ENC_MSGPACK, "json": F.ENC_JSON, "blob": F.ENC_BLOB}
TICK_HZ = T.TICK_HZ
ALIASES = {"swarm/state": "swarm/uav/state"}
ENCODES = [0]  # 全局编码次数（Channel.record 缓存未命中时 + 1）


def canonical_topic(topic: str) -> str:
    return ALIASES.get(topic, topic)


class Channel:
    __slots__ = (
        "_cache",
        "default_rate",
        "enc_code",
        "encodes",
        "encoding",
        "entity",
        "hits",
        "id",
        "kind",
        "layout",
        "meta",
        "native_hz",
        "payload",
        "priority",
        "producer",
        "reset_gen",
        "schema_name",
        "self_contained",
        "seq",
        "subscribers",
        "t_sim_ns",
        "topic",
    )

    def __init__(self, cid: int, topic: str, entity: dict | None = None, *, producer: str | None = None) -> None:
        spec = T.match_topic(topic)
        if spec is None:
            raise ValueError(f"topic 不在 topics.json：{topic}")
        self.id = cid
        self.topic = topic
        self.kind = spec.kind
        self.encoding = spec.encoding
        self.schema_name = spec.schema_name
        self.enc_code = ENC.get(spec.encoding, F.ENC_RAW)
        self.priority = int(spec.priority if spec.priority is not None else 3)
        self.native_hz = float(spec.native_hz)
        self.default_rate = int((spec.default_rate or {}).get("S", 1))
        self.self_contained = bool(spec.self_contained)
        self.entity = entity
        self.producer = producer
        self.seq = 0
        self.t_sim_ns = 0
        self.payload: bytes | memoryview = b""
        self.reset_gen = 0
        self.subscribers = 0
        self.encodes = 0
        self.hits = 0
        self._cache: list[tuple[tuple, bytes]] = []
        self.meta: dict[str, Any] = {}
        self.layout = None
        if self.encoding == "raw" and entity is None and self.schema_name in L.SCHEMA_HASH:
            self.layout = {"schemaName": self.schema_name, "hash": L.SCHEMA_HASH[self.schema_name],
                           "size": L.SIZES.get(self.schema_name)}

    def publish(self, payload: bytes | memoryview, t_sim_ns: int) -> None:
        self.seq = (self.seq + 1) & 0xFFFFFFFF or 1
        self.payload = payload
        self.t_sim_ns = int(t_sim_ns)

    def record(self, frame_t_ns: int, reset: bool) -> bytes:
        key = (self.seq, reset, frame_t_ns)
        for k, b in self._cache:
            if k == key:
                self.hits += 1
                return b
        dt = max(-(2 ** 31), min(2 ** 31 - 1, (self.t_sim_ns - frame_t_ns) // 1000))
        rf = (F.RF_KEYFRAME if self.self_contained else 0) | (F.RF_RESET if reset else 0)
        rec = F.encode_record(self.id, self.enc_code, rf, self.seq, dt, bytes(self.payload))
        self._cache = [(key, rec), *self._cache[:1]]
        self.encodes += 1
        ENCODES[0] += 1
        return rec

    @property
    def is_full64(self) -> bool:
        return self.schema_name == "awr.DroneState64.v1"

    def advert(self) -> dict[str, Any]:
        return {"id": self.id, "topic": self.topic, "kind": self.kind, "encoding": self.encoding,
                "schemaName": self.schema_name, "layout": self.layout, "schemaRef": None,
                "nativeHz": float(min(self.native_hz, TICK_HZ)), "defaultRate": self.default_rate,
                "priority": self.priority, "selfContained": self.self_contained, "entity": self.entity}


class ChannelRegistry:
    """channel 表；增删时回调 `on_advertise(channels)` / `on_unadvertise(ids)`（Gateway 对全部连接发增量）。"""

    def __init__(self) -> None:
        self.by_id: dict[int, Channel] = {}
        self.by_topic: dict[str, Channel] = {}
        self._ids: dict[str, int] = {}
        self._by_head: dict[str, dict[int, Channel]] = {}
        self._next = 100
        self.on_advertise: Callable[[list[Channel]], None] | None = None
        self.on_unadvertise: Callable[[list[int]], None] | None = None

    def _index(self, c: Channel) -> None:
        self.by_id[c.id] = c
        self.by_topic[c.topic] = c
        self._by_head.setdefault(c.topic.split("/", 1)[0], {})[c.id] = c

    def add_fixed(self, cid: int, topic: str) -> Channel:
        c = Channel(cid, topic)
        self._ids[topic] = cid
        self._index(c)
        return c

    def get_or_create(self, topic: str, *, entity: dict | None = None, announce: bool = True,
                      producer: str | None = None) -> Channel:
        c = self.by_topic.get(topic)
        if c is not None:
            return c
        cid = self._ids.get(topic)
        if cid is None:
            cid = self._next
            self._next += 1
            if self._next > 0xFFFF:
                raise OverflowError("channel id 耗尽")
            self._ids[topic] = cid
        c = Channel(cid, topic, entity, producer=producer)
        self._index(c)
        if announce and self.on_advertise is not None:
            self.on_advertise([c])
        return c

    def remove(self, topic: str, *, announce: bool = True) -> Channel | None:
        c = self.by_topic.pop(topic, None)
        if c is None:
            return None
        self.by_id.pop(c.id, None)
        self._by_head.get(topic.split("/", 1)[0], {}).pop(c.id, None)
        if announce and self.on_unadvertise is not None:
            self.on_unadvertise([c.id])
        return c

    def match(self, pattern: str) -> list[Channel]:
        pattern = canonical_topic(pattern)
        if "*" not in pattern:
            c = self.by_topic.get(pattern)
            return [c] if c is not None and c.kind != "event" else []
        head = pattern.split("/", 1)[0]
        pool = self.by_id.values() if head in ("*", "**") else self._by_head.get(head, {}).values()
        return sorted((c for c in pool if c.kind != "event" and key_matches(pattern, c.topic)), key=lambda c: c.id)

    def bump_reset_gen(self, producer: str | None = None) -> int:
        """生产者纪元变化：该生产者的状态类 channel 下一条记录置 RESET（17 §6.10）；返回受影响的 channel 数。"""
        n = 0
        for c in self.by_id.values():
            if c.kind == "state" and (c.entity is not None or c.topic.startswith("swarm/")) and \
                    (producer is None or c.producer in (None, producer)):
                c.reset_gen += 1
                n += 1
        return n

    def adverts(self) -> list[dict[str, Any]]:
        return [c.advert() for c in sorted(self.by_id.values(), key=lambda c: c.id)]
