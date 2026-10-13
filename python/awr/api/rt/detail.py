"""DetailDemux、EnvCache 与任务状态拆分（M11-FR-072 至 FR-074；M11 §6.3.5；AWR-17 §9.3、§9.5、§9.7 第 5 条；ADR-025）。

DetailDemux 把低频批量消息按 agent_no 拆成逐机 channel，一律不重新编码：
- `state/{producer}/ext`（2 Hz，`[[agent_no, state_ext], ...]`）与 `state/{producer}/safety`（10 Hz，`[[agent_no, row], ...]`，
  row 为 msgpack 对象或 bin）：`Unpacker.read_array_header()` + 逐项 `skip()`/`tell()` 取每项字节区间作为 channel 负载（bin 行
  去掉 bin 头）；每条消息只建一次索引，最近一条消息保留，新订阅的 channel 立即从索引取当前值（供下一帧 SNAPSHOT）；
- `state/{producer}/sensor`（`{v, t_sim_ns, rows: bin(n·48)}`）：按 48 B 切片，行内 `agent_no`、`sensor_no` 经 roster 的
  `sensors[]` 找到 `uav/{id}/sensor/{name}/pose`；
- `state/sim-core/detail`（`{v, t_sim_ns, agent_no: u16[n], rows: bin(n·32)}`）：按 32 B 切片装入 `uav/{id}/env`；
- 只对有订阅者的逐机 channel 发布；记录 `dt_us` = 消息 `t_sim_ns` − 帧 `t_sim_ns`（由 Channel.record 计算）。

EnvCache：`evt/sim-core/env`（kind `env.keyframe`，data 为完整 EnvKeyframe）与 `state/sim-core/env` 1 Hz 心跳（完整关键帧
字节）都更新 `env/state` 的自包含 latest；心跳负载字节直接作为 channel 负载；按 (epoch, version) 只前进不后退；
`config.presets_sha256` 与 api 加载的 `presets.json`（生成物 `PRESETS_SHA256`）不一致时发 `status{id: "env.presets_mismatch"}`。
关键帧事件的缺口由 EventSubscriber 的 seq 补拉（`_replay`）处理，心跳 version 跳号只计数。

任务状态：`state/sim-core/mission`（2 Hz，awr.mission.status.v1 的批）→ `mission/{mid}/status`（首次出现时创建并增量 advertise）。

StreamDemux（D2，M11-FR-211；17 §17.7.2、§17.10.2）：
- `state/sim-core/targets`（`{v, t_sim_ns, roster_version, rows: bin(32·n)}`，M08-FR-217）：行字节原样作为 `swarm/target/state`
  一条记录的负载（`awr.TargetLite32.v1` × n，不重新编码）；
- `state/sim-core/<stream>`（`{v, t_sim_ns, items: {<id>: <payload>}}`；也接受 `items` 为数组，项内以 `id`、`uav`、`tid`
  给出实体）：按实体拆分为 `target/{id}/detail`、`uav/{id}/perception`、`uav/{id}/payload`、`uav/{id}/sensor/nav`、
  `uav/{id}/sensor/radar/tracks`、`uav/{id}/sensor/acoustic/bearings`、`uav/{id}/sensor/lidar/scan`（blob，去掉 bin 头）、
  `nest/{id}/status`、`task/{tid}/status`、`relay/{tid}/stats`。映射形式的子对象按字节区间切片、不重新编码；数组形式解码后逐项
  重新打包。无人机流的键可以是 id 或 agent_no（经 roster），只为 roster 中的无人机建 channel；识别物流的键为 id 或 target_no；
  机巢、任务与接力为全集，批中不再出现的实体其 channel 删除（增量 unadvertise）。channel 首次出现时增量 advertise。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

import msgpack

from awr.contracts import bus_keys
from awr.contracts.presets import PRESETS_SHA256

from .channels import Channel
from .protocol import status_msg

if TYPE_CHECKING:
    from .gateway import Gateway

__all__ = ["DetailDemux", "EnvCache", "StreamDemux", "parse_pairs"]

log = logging.getLogger("awr.api.detail")

SENSOR_ROW = 48
ENV_ROW = 32
_BIN_HDR = {0xC4: 2, 0xC5: 3, 0xC6: 5}


def parse_pairs(raw: bytes) -> dict[int, tuple[int, int]]:
    """`[[agent_no, item], ...]` → {agent_no: (start, end)}：item 的字节区间（bin 去掉头部）；只 skip，不解码 item。"""
    up = msgpack.Unpacker(raw=False, strict_map_key=False, max_buffer_size=max(1024, len(raw) + 16))
    up.feed(raw)
    out: dict[int, tuple[int, int]] = {}
    n = up.read_array_header()
    for _ in range(n):
        m = up.read_array_header()
        no = up.unpack()
        start = up.tell()
        up.skip()
        end = up.tell()
        for _ in range(max(0, m - 2)):
            up.skip()
        hdr = _BIN_HDR.get(raw[start]) if start < len(raw) else None
        if hdr is not None:
            start += hdr
        if isinstance(no, int):
            out[no] = (start, end)
    return out


class _Batch:
    __slots__ = ("index", "raw", "t_sim_ns")

    def __init__(self, raw: bytes, index: dict[int, tuple[int, int]], t_sim_ns: int) -> None:
        self.raw = raw
        self.index = index
        self.t_sim_ns = t_sim_ns

    def slice(self, agent_no: int) -> bytes | None:
        r = self.index.get(agent_no)
        return None if r is None else self.raw[r[0]:r[1]]


class DetailDemux:
    def __init__(self, gw: Gateway) -> None:
        self.gw = gw
        self.last: dict[str, _Batch] = {}
        self.stats = {"ext": 0, "safety": 0, "sensor": 0, "detail": 0, "mission": 0, "malformed": 0}

    # ------------------------------------------------------------ [[agent_no, item]] 批
    def feed_pairs(self, suffix: str, raw: bytes, *, all_channels: bool = False) -> None:
        """suffix ∈ state_ext、safety：建索引、保留最近一批，发布到有订阅者的 `uav/{id}/<suffix>`。"""
        try:
            idx = parse_pairs(raw)
        except Exception:
            self.stats["malformed"] += 1
            return
        self.stats["ext" if suffix == "state_ext" else "safety"] += 1
        b = self.last[suffix] = _Batch(raw, idx, self.gw.frame_t_sim_ns)
        gw = self.gw
        for no, (s, e) in idx.items():
            vid = gw.by_agent_no.get(no)
            if vid is None:
                continue
            ch = gw.registry.by_topic.get(f"uav/{vid}/{suffix}")
            if ch is not None and (ch.subscribers or all_channels):
                ch.publish(raw[s:e], b.t_sim_ns)

    def fill(self, ch: Channel) -> None:
        """新订阅且尚无值的逐机 msgpack channel：从最近一批的索引立即取当前值（下一帧 SNAPSHOT 即带当前状态）。"""
        ent = ch.entity
        if not ent or ch.seq != 0:
            return
        suffix = ch.topic.rsplit("/", 1)[-1]
        b = self.last.get(suffix)
        if b is None:
            return
        no = self.gw.agent_no_of(ent["id"])
        if no is None:
            return
        data = b.slice(no)
        if data is not None:
            ch.publish(data, b.t_sim_ns)

    # ------------------------------------------------------------ raw 行
    def feed_sensor(self, raw: bytes, *, all_channels: bool = False) -> None:
        try:
            m = msgpack.unpackb(raw, raw=False, strict_map_key=False)
            rows = bytes(m["rows"])
            t = int(m.get("t_sim_ns", self.gw.frame_t_sim_ns))
        except Exception:
            self.stats["malformed"] += 1
            return
        self.stats["sensor"] += 1
        gw = self.gw
        for off in range(0, len(rows) - SENSOR_ROW + 1, SENSOR_ROW):
            no = rows[off] | (rows[off + 1] << 8)
            ch = gw.sensor_channel(no, rows[off + 2])
            if ch is not None and (ch.subscribers or all_channels):
                ch.publish(rows[off:off + SENSOR_ROW], t)

    def feed_detail(self, raw: bytes) -> None:
        try:
            m = msgpack.unpackb(raw, raw=False, strict_map_key=False)
            rows = bytes(m["rows"])
            nos = list(m["agent_no"])
            t = int(m.get("t_sim_ns", self.gw.frame_t_sim_ns))
        except Exception:
            self.stats["malformed"] += 1
            return
        self.stats["detail"] += 1
        gw = self.gw
        for i, no in enumerate(nos):
            off = i * ENV_ROW
            if off + ENV_ROW > len(rows):
                break
            vid = gw.by_agent_no.get(int(no))
            if vid is None:
                continue
            ch = gw.registry.by_topic.get(f"uav/{vid}/env")
            if ch is not None and ch.subscribers:
                ch.publish(rows[off:off + ENV_ROW], t)

    # ------------------------------------------------------------ 任务状态
    def feed_mission(self, raw: bytes) -> None:
        try:
            m = msgpack.unpackb(raw, raw=False, strict_map_key=False)
        except Exception:
            self.stats["malformed"] += 1
            return
        if isinstance(m, dict):
            items = m.get("items") or m.get("missions") or ([m] if "mid" in m else [])
        elif isinstance(m, list):
            items = m
        else:
            items = []
        self.stats["mission"] += 1
        gw = self.gw
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("mid"), str):
                continue
            topic = f"mission/{it['mid']}/status"
            try:
                ch = gw.registry.get_or_create(topic, entity=None, producer=gw.producer)
            except ValueError:
                continue
            ch.publish(msgpack.packb(it, use_bin_type=True), int(it.get("t_ns") or gw.frame_t_sim_ns))


    # ------------------------------------------------------------ agent-runtime（ext，M14）
    def feed_agents(self, raw: bytes) -> None:
        """`state/agent-runtime/agents`（1 Hz 批：列表或 `{items}`，每项带 `aid`）→ `agent/{aid}/status`（首次出现时创建）。"""
        try:
            m = msgpack.unpackb(raw, raw=False, strict_map_key=False)
        except Exception:
            self.stats["malformed"] += 1
            return
        if isinstance(m, dict):
            items = m.get("items") or m.get("agents") or []
        elif isinstance(m, list):
            items = m
        else:
            items = []
        gw = self.gw
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("aid"), str):
                continue
            try:
                ch = gw.registry.get_or_create(f"agent/{it['aid']}/status", entity={"kind": "agent", "id": it["aid"]},
                                               producer="agent-runtime")
            except ValueError:
                continue
            ch.publish(msgpack.packb(it, use_bin_type=True), gw.frame_t_sim_ns)

    def feed_tasks(self, raw: bytes) -> None:
        """`state/agent-runtime/tasks`（自包含最新，≤ 64 条）→ `agent/tasks`（负载原样）。"""
        ch = self.gw.registry.by_topic.get("agent/tasks")
        if ch is not None:
            ch.publish(bytes(raw), self.gw.frame_t_sim_ns)


class StreamDemux:
    """D2 数据产品的拆分（见模块说明）。"""

    # stream 名 → 总线 key（会话命名空间，生成物常量）
    KEYS: ClassVar[dict[str, str]] = {"target_detail": bus_keys.STATE_TARGET_DETAIL, "perception": bus_keys.STATE_PERCEPTION,
            "payload": bus_keys.STATE_PAYLOAD, "nav": bus_keys.STATE_NAV, "radar": bus_keys.STATE_RADAR,
            "acoustic": bus_keys.STATE_ACOUSTIC, "lidar": bus_keys.STATE_LIDAR, "nests": bus_keys.STATE_NESTS,
            "tasks": bus_keys.STATE_TASKS, "relay": bus_keys.STATE_RELAY,
            # D2.1 场视图与读数（17 §17.20.6；d2-WP-19b-to-M11 第 2 条）
            "field": bus_keys.STATE_FIELD, "target_quant": bus_keys.STATE_TARGET_QUANT}
    # stream 名 → (topic 模板, 实体类别, 是否全集)
    TOPICS: ClassVar[dict[str, tuple[str, str, bool]]] = {"target_detail": ("target/{id}/detail", "target", True), "perception": ("uav/{id}/perception", "uav", False),
              "payload": ("uav/{id}/payload", "uav", True), "nav": ("uav/{id}/sensor/nav", "uav", False),
              "radar": ("uav/{id}/sensor/radar/tracks", "uav", False),
              "acoustic": ("uav/{id}/sensor/acoustic/bearings", "uav", False),
              "lidar": ("uav/{id}/sensor/lidar/scan", "uav", False), "nests": ("nest/{id}/status", "nest", True),
              "tasks": ("task/{id}/status", "task", True), "relay": ("relay/{id}/stats", "relay", True),
              "field": ("field/{id}", "field", True), "target_quant": ("target/{id}/quant", "target", False)}
    CAPS: ClassVar[dict[str, str]] = {"target_detail": "targets", "perception": "perception", "radar": "perception", "acoustic": "perception",
            "nests": "tasking", "tasks": "tasking", "relay": "tasking", "field": "perception", "target_quant": "perception"}
    FIELD_KEYS = ("acoustic", "visual", "thermal", "radar", "volumes")
    ID_FIELDS = ("id", "uav", "tid", "target")
    TARGET_TOPIC = "swarm/target/state"

    def __init__(self, gw: Gateway) -> None:
        self.gw = gw
        self.stats: dict[str, int] = {"targets": 0, "malformed": 0, "unknown_entity": 0}

    # ------------------------------------------------------------ 识别物摘要
    def feed_targets(self, raw: bytes) -> None:
        gw = self.gw
        try:
            m = msgpack.unpackb(raw, raw=False, strict_map_key=False)
            rows = bytes(m["rows"])
            t = int(m.get("t_sim_ns", gw.frame_t_sim_ns))
        except Exception:
            self.stats["malformed"] += 1
            return
        if len(rows) % 32:
            self.stats["malformed"] += 1
            return
        self.stats["targets"] += 1
        try:
            ch = gw.registry.get_or_create(self.TARGET_TOPIC, entity=None, producer=gw.producer)
        except ValueError:
            return
        ch.publish(rows, t)
        gw.note_cap("targets")

    # ------------------------------------------------------------ 按实体拆分
    def _entity_id(self, kind: str, key: Any) -> str | None:
        gw = self.gw
        if isinstance(key, bool):
            return None
        if isinstance(key, int):
            if kind == "uav":
                return gw.by_agent_no.get(key)
            if kind == "target":
                return gw.target_by_no.get(key)
            return str(key)
        if isinstance(key, str) and key and "/" not in key and "*" not in key and len(key) <= 64:
            if kind == "uav" and key not in gw.roster:
                return None
            if kind == "field" and key not in self.FIELD_KEYS:
                return None
            return key
        return None

    def _items(self, name: str, raw: bytes) -> tuple[int, list[tuple[Any, bytes]]] | None:
        """(t_sim_ns, [(键, 负载字节)])：映射形式按字节区间切片，数组形式逐项重新打包。"""
        up = msgpack.Unpacker(raw=False, strict_map_key=False, max_buffer_size=max(1024, len(raw) + 16))
        up.feed(raw)
        n = up.read_map_header()
        t: int | None = None
        out: list[tuple[Any, bytes]] = []
        for _ in range(n):
            k = up.unpack()
            if k == "t_sim_ns":
                v = up.unpack()
                t = int(v) if isinstance(v, int) and not isinstance(v, bool) else None
            elif k == "items":
                head = raw[up.tell()] if up.tell() < len(raw) else 0
                if 0x80 <= head <= 0x8F or head in (0xDE, 0xDF):
                    m = up.read_map_header()
                    for _ in range(m):
                        key = up.unpack()
                        a = up.tell()
                        up.skip()
                        b = up.tell()
                        hdr = _BIN_HDR.get(raw[a]) if a < len(raw) else None
                        out.append((key, raw[a + hdr:b] if hdr is not None else raw[a:b]))
                else:
                    arr = up.unpack()
                    for it in arr if isinstance(arr, list) else []:
                        if isinstance(it, dict):
                            key = next((it[f] for f in self.ID_FIELDS if f in it), None)
                            out.append((key, msgpack.packb(it, use_bin_type=True)))
            else:
                up.skip()
        return (t if t is not None else self.gw.frame_t_sim_ns), out

    def feed(self, name: str, raw: bytes) -> None:
        spec = self.TOPICS.get(name)
        if spec is None:
            return
        tpl, kind, full = spec
        try:
            r = self._items(name, raw)
        except Exception:
            self.stats["malformed"] += 1
            return
        if r is None:
            return
        t, items = r
        gw = self.gw
        self.stats[name] = self.stats.get(name, 0) + 1
        seen: set[str] = set()
        for key, payload in items:
            eid = self._entity_id(kind, key)
            if eid is None:
                self.stats["unknown_entity"] += 1
                continue
            seen.add(eid)
            topic = tpl.format(id=eid)
            try:
                ch = gw.registry.get_or_create(topic, entity={"kind": kind, "id": eid}, producer=gw.producer)
            except ValueError:
                continue
            ch.publish(payload, t)
        if full and kind in ("nest", "task", "relay"):
            head = tpl.split("/", 1)[0]
            tail = tpl.rsplit("/", 1)[1]
            stale = [c for c in list(gw.registry.by_topic.values())
                     if c.topic.startswith(head + "/") and c.topic.endswith("/" + tail) and c.entity
                     and c.entity.get("id") not in seen]
            ids = [c.id for c in stale if gw.registry.remove(c.topic, announce=False) is not None]
            if ids:
                gw._unadvertise_delta(ids)
        cap = self.CAPS.get(name)
        if cap is not None and items:
            gw.note_cap(cap)


class EnvCache:
    def __init__(self, gw: Gateway, channel: Channel) -> None:
        self.gw = gw
        self.ch = channel
        self.epoch: int | None = None
        self.version: int = -1
        self.presets_sha256: str | None = None
        self.stats = {"keyframes": 0, "heartbeats": 0, "applied": 0, "version_gaps": 0}

    def reset(self) -> None:
        """回放 open、seek、close：下一帧（backfill 或心跳）无条件生效。"""
        self.epoch, self.version = None, -1

    def _newer(self, epoch: Any, version: Any) -> bool:
        if not isinstance(version, int):
            return False
        if epoch != self.epoch:
            return True
        if version > self.version + 1 and self.version >= 0:
            self.stats["version_gaps"] += 1
        return version > self.version

    def _apply(self, frame: dict, payload: bytes, t_sim_ns: int) -> None:
        self.epoch, self.version = frame.get("epoch"), int(frame["version"])
        self.ch.publish(payload, t_sim_ns)
        self.stats["applied"] += 1

    def _check_presets(self, frame: dict) -> None:
        cfg = frame.get("config") if isinstance(frame.get("config"), dict) else {}
        sha = cfg.get("presets_sha256")
        if isinstance(sha, str) and sha != self.presets_sha256:
            self.presets_sha256 = sha
            if sha != PRESETS_SHA256:
                self.gw.set_status(status_msg("env.presets_mismatch", "warning", "环境预设与 api 加载的 presets.json 不一致",
                                              source="sim-core"))
            else:
                self.gw.clear_status("env.presets_mismatch")

    def on_keyframe(self, ev: dict) -> None:
        self.stats["keyframes"] += 1
        data = ev.get("data")
        if not isinstance(data, dict):
            return
        self._check_presets(data)
        if not self._newer(data.get("epoch"), data.get("version")):
            return
        t = data.get("t_ns") if isinstance(data.get("t_ns"), int) else ev.get("t_sim_ns", 0)
        self._apply(data, msgpack.packb(data, use_bin_type=True), int(t))

    def on_heartbeat(self, raw: bytes) -> None:
        self.stats["heartbeats"] += 1
        try:
            frame = msgpack.unpackb(raw, raw=False, strict_map_key=False)
        except Exception:
            return
        if not isinstance(frame, dict):
            return
        self._check_presets(frame)
        t = frame.get("t_ns") if isinstance(frame.get("t_ns"), int) else self.gw.frame_t_sim_ns
        if not self._newer(frame.get("epoch"), frame.get("version")):
            # INT-1（M07-to-M11 第 1 条）：同纪元同版本的心跳照常下发（锚点 t_ns 前进，客户端据此不进入 STALE，
            # 迟到客户端拿到最新锚点），只是不计入 applied；更旧的版本或旧纪元仍丢弃
            if frame.get("epoch") == self.epoch and frame.get("version") == self.version:
                self.ch.publish(bytes(raw), int(t))
            return
        self._apply(frame, bytes(raw), int(t))
