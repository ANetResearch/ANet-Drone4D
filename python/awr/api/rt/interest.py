"""兴趣集下推与 GCS 链路心跳（M11-FR-024、FR-071；M11 §6.4.13；AWR-17 §9.3、§9.4；10 AD-10；ADR-026）。

InterestAggregator：取全部连接对 `uav/{id}/{state_ext,safety,env}` 与 `uav/{id}/sensor/*/pose` 订阅的机体并集（`detail`，
按最近订阅时刻降序，≤ 64 架，超出时截断并发 `status{id: "interest.truncated"}`）与录制标记集（`marks`，≤ 16 架：各连接
≥ 60 Hz 订阅的 `uav/{id}/state` 即选中机与 FPV 焦点机，ext），`topics` 取自 state_ext、safety、sensor、env；订阅变化后
去抖 250 ms 以全量 Interest `{v: 1, seq, detail: u16[], marks: u16[], topics: str[]}` 发布到 `ctl/sim-core/interest`，
另每 1 s 重发一次以自愈。`record_scope`（M12 F-17）待 17 §9.4 与 `bus/interest.schema.json` 登记后再发送。

D2 按需生成的数据流（M11-FR-212；17 §17.7.2 规则 2；`bus/interest.schema.json` 的 `streams`）：`uav/{id}/perception`、
`uav/{id}/sensor/nav`、`uav/{id}/sensor/radar/tracks`、`uav/{id}/sensor/acoustic/bearings`、`uav/{id}/sensor/lidar/scan` 只为
被订阅的机体生成。订阅对应的机体（含 channel 尚未出现的精确 topic 订阅：首帧生成之前 channel 不存在）按流列入
`streams{perception, nav, radar, acoustic, lidar}`（agent_no，最近订阅在前；`lidar` 每会话至多 1 个，取最近的订阅，第二个订阅
即切换生成对象），这些机体同时并入 `detail`、流名并入 `topics`（只读 detail 与 topics 的生产者同样可用）。

GcsBeacon：5 Hz（墙钟）向 `ctl/sim-core/gcs` 发布 `{v: 1, seq, principal_id, seat_state, ping_age_ms}`；`ping_age_ms` 取席位
持有者（按 principal，不按连接）最近一次 ping 至今的时长，持有者全部连接关闭后继续增长；登记后尚未 ping 时从登记时刻起算；
席位 FREE 时 `principal_id = null`、`ping_age_ms = 0`；超过 u32 上限时钳制。判据与冻结规则在 sim-core（12 §5.9）。
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

from awr.contracts import bus_keys
from awr.runtime.bus import pack

from .protocol import status_msg

if TYPE_CHECKING:
    from .gateway import Gateway

__all__ = ["GcsBeacon", "InterestAggregator"]

DETAIL_MAX = 64
MARKS_MAX = 16
DEBOUNCE_NS = 250_000_000
RESEND_NS = 1_000_000_000
GCS_PERIOD_NS = 200_000_000
DETAIL_SUFFIX = {"state_ext": "state_ext", "safety": "safety", "env": "env"}
STREAM_SUFFIX = {"perception": "perception", "sensor/nav": "nav", "sensor/radar/tracks": "radar",
                 "sensor/acoustic/bearings": "acoustic", "sensor/lidar/scan": "lidar"}
STREAM_ORDER = ("perception", "nav", "radar", "acoustic", "lidar")
# D2.1 场视图（17 §17.20.6；d2-WP-19b-to-M11 第 1 条）：field/<kind> → FieldInterestCode；target/{id}/quant → target_no
FIELD_CODES = {"acoustic": 1, "visual": 2, "thermal": 3, "radar": 4, "volumes": 5}
QUANT_MAX = 30


def field_code(topic: str) -> list[int]:
    """`field/acoustic` → [1]；`field/*`、`field/**` → 全部五个；其余 []。"""
    parts = topic.split("/")
    if len(parts) != 2 or parts[0] != "field":
        return []
    if parts[1] in ("*", "**"):
        return sorted(FIELD_CODES.values())
    c = FIELD_CODES.get(parts[1])
    return [c] if c is not None else []


def quant_target(topic: str) -> str | None:
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "target" and parts[2] == "quant" and "*" not in parts[1]:
        return parts[1]
    return None


def stream_topic_kind(topic: str) -> tuple[str, str] | None:
    """`uav/{id}/perception` → (id, perception)；`uav/{id}/sensor/lidar/scan` → (id, lidar)；其余 None（id 可为通配）。"""
    parts = topic.split("/")
    if len(parts) >= 3 and parts[0] == "uav":
        k = STREAM_SUFFIX.get("/".join(parts[2:]))
        if k is not None:
            return parts[1], k
    return None


def detail_topic_kind(topic: str) -> str | None:
    """`uav/{id}/state_ext` → state_ext；`uav/{id}/safety` → safety；`uav/{id}/env` → env；`uav/{id}/sensor/*/pose` → sensor。"""
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "uav":
        return DETAIL_SUFFIX.get(parts[2])
    if len(parts) == 5 and parts[0] == "uav" and parts[2] == "sensor" and parts[4] == "pose":
        return "sensor"
    return None


class InterestAggregator:
    def __init__(self, gw: Gateway) -> None:
        self.gw = gw
        self.seq = 0
        self.dirty_since = 0
        self.last_pub = 0
        self.detail: list[int] = []
        self.marks: list[int] = []
        self.topics: list[str] = []
        self.streams: dict[str, list[int]] = {}
        self.fsub = 0  # field/* 与 target/*/quant 的订阅操作计数（sim-core 见到变化即发关键帧）
        self.truncated = False
        self._pub: Any = None
        self.stats = {"published": 0}

    def on_subs_changed(self) -> None:
        if not self.dirty_since:
            self.dirty_since = time.monotonic_ns()

    def compute(self) -> tuple[list[int], list[int], list[str], bool]:
        gw = self.gw
        latest: dict[int, int] = {}
        mark_t: dict[int, int] = {}
        kinds: set[str] = set()
        for s in gw.sessions:
            if s.closing:
                continue
            for sc in s.order:
                ch = sc.channel
                ent = ch.entity
                if not ent or ent.get("kind") != "uav":
                    continue
                no = gw.agent_no_of(ent["id"])
                if no is None:
                    continue
                k = detail_topic_kind(ch.topic)
                if k is not None:
                    kinds.add(k)
                    if latest.get(no, -1) < sc.t_sub_ns:
                        latest[no] = sc.t_sub_ns
                elif ch.is_full64 and sc.rate >= 60 and mark_t.get(no, -1) < sc.t_sub_ns:
                    mark_t[no] = sc.t_sub_ns
        streams = self.compute_streams()
        for k, nos in streams.items():
            kinds.add(k)
            for i, no in enumerate(nos):
                latest.setdefault(no, -1 - i)
        detail = [no for no, _ in sorted(latest.items(), key=lambda kv: (-kv[1], kv[0]))]
        truncated = len(detail) > DETAIL_MAX
        marks = [no for no, _ in sorted(mark_t.items(), key=lambda kv: (-kv[1], kv[0]))][:MARKS_MAX]
        order = ("state_ext", "safety", "sensor", "env", *STREAM_ORDER)
        self.streams = streams
        return detail[:DETAIL_MAX], marks, [t for t in order if t in kinds], truncated

    def compute_streams(self) -> dict[str, list[int]]:
        """按需生成的数据流（FR-212）：各连接订阅（含尚无 channel 的精确 topic）→ {流: [agent_no]}，最近订阅在前。"""
        gw = self.gw
        when: dict[str, dict[int, int]] = {}
        for s in gw.sessions:
            if s.closing:
                continue
            for sc in s.order:
                ent = sc.channel.entity
                hit = stream_topic_kind(sc.channel.topic)
                if hit is None or not ent:
                    continue
                no = gw.agent_no_of(ent["id"])
                if no is not None:
                    d = when.setdefault(hit[1], {})
                    d[no] = max(d.get(no, -1), sc.t_sub_ns)
            for sub in s.subs.values():
                hit = stream_topic_kind(str(sub.get("topic", "")))
                if hit is None or "*" in hit[0]:
                    continue
                no = gw.agent_no_of(hit[0])
                if no is not None:
                    d = when.setdefault(hit[1], {})
                    d.setdefault(no, int(sub.get("t_ns", 0)))
        out: dict[str, list[int]] = {}
        for k in STREAM_ORDER:
            d = when.get(k)
            if not d:
                continue
            nos = [no for no, _ in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]
            out[k] = nos[:1] if k == "lidar" else nos[:DETAIL_MAX]
        # D2.1：场视图与读数（订阅时生成；只有有订阅时才出现这些键）
        codes: set[int] = set()
        quant: list[int] = []
        rev = {tid: no for no, tid in gw.target_by_no.items()}
        for s in gw.sessions:
            if s.closing:
                continue
            for sub in s.subs.values():
                topic = str(sub.get("topic", ""))
                codes.update(field_code(topic))
                tid = quant_target(topic)
                if tid is not None and tid in rev and rev[tid] not in quant:
                    quant.append(rev[tid])
        if codes or quant:
            if codes:
                out["field"] = sorted(codes)
            if quant:
                out["quant"] = quant[:QUANT_MAX]
            out["fsub"] = [self.fsub % 65536]
        return out

    def note_subscribe(self, topics: list[str]) -> None:
        """一次 subscribe 操作：含 field/* 或 target/*/quant 时订阅代数 + 1（新订阅者无需等待关键帧周期）。"""
        if any(field_code(t) or quant_target(t) for t in topics):
            self.fsub += 1

    def maybe_push(self, t_mono: int) -> None:
        if self.dirty_since and t_mono - self.dirty_since >= DEBOUNCE_NS:
            self.dirty_since = 0
            self.publish(t_mono)
        elif t_mono - self.last_pub >= RESEND_NS:
            self.publish(t_mono)

    def publish(self, t_mono: int | None = None) -> dict:
        gw = self.gw
        self.detail, self.marks, self.topics, trunc = self.compute()
        if trunc != self.truncated:
            self.truncated = trunc
            if trunc:
                gw.set_status(status_msg("interest.truncated", "warning", "逐机详情订阅超过 64 架，已按最近订阅截断",
                                         source="api"))
            else:
                gw.clear_status("interest.truncated")
        msg = {"v": 1, "seq": self.seq, "detail": list(self.detail), "marks": list(self.marks), "topics": list(self.topics)}
        if self.streams:
            msg["streams"] = {k: list(v) for k, v in self.streams.items()}
        if self._pub is None:
            self._pub = gw.bus.publisher(bus_keys.CTL_INTEREST)
        with contextlib.suppress(Exception):  # 总线关闭（停机中）
            self._pub.put(pack(msg))
            self.stats["published"] += 1
        self.seq += 1
        self.last_pub = time.monotonic_ns() if t_mono is None else t_mono
        return msg

    def close(self) -> None:
        if self._pub is not None:
            self._pub.close()
            self._pub = None


class GcsBeacon:
    def __init__(self, gw: Gateway) -> None:
        self.gw = gw
        self.seq = 0
        self.last = 0
        self._pub: Any = None
        self.last_msg: dict[str, Any] = {}

    def maybe_publish(self, t_mono: int) -> None:
        if t_mono - self.last >= GCS_PERIOD_NS:
            self.publish(t_mono)

    def build(self, t_mono: int) -> dict[str, Any]:
        gw = self.gw
        holder = gw.seat.get("holder")
        state = gw.seat.get("state", "FREE") if holder else "FREE"
        if holder is None:
            age = 0
        else:
            last = gw.principal_last_ping.get(holder, gw.seat_claimed_mono or t_mono)
            age = min(0xFFFFFFFF, max(0, (t_mono - last) // 1_000_000))
        return {"v": 1, "seq": self.seq, "principal_id": holder, "seat_state": state, "ping_age_ms": int(age)}

    def publish(self, t_mono: int) -> None:
        msg = self.last_msg = self.build(t_mono)
        if self._pub is None:
            self._pub = self.gw.bus.publisher(bus_keys.CTL_GCS)
        with contextlib.suppress(Exception):
            self._pub.put(pack(msg))
        self.seq += 1
        self.last = t_mono

    def close(self) -> None:
        if self._pub is not None:
            self._pub.close()
            self._pub = None
