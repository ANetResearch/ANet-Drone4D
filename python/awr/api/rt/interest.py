"""兴趣集下推与 GCS 链路心跳（M11-FR-024、FR-071；M11 §6.4.13；AWR-17 §9.3、§9.4；10 AD-10；ADR-026）。

InterestAggregator：取全部连接对 `uav/{id}/{state_ext,safety,env}` 与 `uav/{id}/sensor/*/pose` 订阅的机体并集（`detail`，
按最近订阅时刻降序，≤ 64 架，超出时截断并发 `status{id: "interest.truncated"}`）与录制标记集（`marks`，≤ 16 架：各连接
≥ 60 Hz 订阅的 `uav/{id}/state` 即选中机与 FPV 焦点机，ext），`topics` 取自 state_ext、safety、sensor、env；订阅变化后
去抖 250 ms 以全量 Interest `{v: 1, seq, detail: u16[], marks: u16[], topics: str[]}` 发布到 `ctl/sim-core/interest`，
另每 1 s 重发一次以自愈。`record_scope`（M12 F-17）待 17 §9.4 与 `bus/interest.schema.json` 登记后再发送。

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
        detail = [no for no, _ in sorted(latest.items(), key=lambda kv: (-kv[1], kv[0]))]
        truncated = len(detail) > DETAIL_MAX
        marks = [no for no, _ in sorted(mark_t.items(), key=lambda kv: (-kv[1], kv[0]))][:MARKS_MAX]
        order = ("state_ext", "safety", "sensor", "env")
        return detail[:DETAIL_MAX], marks, [t for t in order if t in kinds], truncated

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
