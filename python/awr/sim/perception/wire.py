"""数据产品打包与发布、检测环（M19-FR-016 至 FR-018；AWR-04 §11.7；17 §17.6.4、§17.7.2、§17.7.4；d2-WP-03-to-M19 第 1、2 条）。

- 总线载荷 `state/sim-core/{perception,radar,acoustic}` = msgpack `{v: 1, t_sim_ns, items: {<uav id>: 载荷}}`，网关按键拆为
  `uav/{id}/perception`、`uav/{id}/sensor/radar/tracks`、`uav/{id}/sensor/acoustic/bearings`；只为兴趣下推的机体编码（`ctx.streams`）。
- 检测环：会话内 8192 条（17 §17.6.4），`seq` 单调；`page(since_seq, ...)` 为 SB-39 的回复，`since_seq` 早于环 → 码 319（HTTP 410）。
- 数值紧凑化：浮点保留 3–4 位有效小数（帧 ≤ 2 KB，M19-NFR-005）。
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

__all__ = ["DetectionRing", "Publisher", "rnd"]


def rnd(x: float | None, nd: int = 3) -> float | None:
    if x is None:
        return None
    v = float(x)
    if not math.isfinite(v):
        return None
    return round(v, nd)


class DetectionRing:
    """会话检测环（awr.sensor.detection.v1）。"""

    def __init__(self, cap: int) -> None:
        self.cap = int(cap)
        self.items: deque[dict] = deque(maxlen=self.cap)
        self.seq = 0

    def clear(self) -> None:
        self.items.clear()

    def push(self, rec: dict) -> dict:
        self.seq += 1
        rec = {"seq": self.seq, **rec}
        self.items.append(rec)
        return rec

    def push_rec(self, rec: dict) -> dict:
        """同 `push`，但 rec 已以 "seq" 为首键（原地写入序号，不再复制）。"""
        self.seq += 1
        rec["seq"] = self.seq
        self.items.append(rec)
        return rec

    @property
    def oldest_seq(self) -> int:
        return int(self.items[0]["seq"]) if self.items else self.seq + 1

    def page(self, since_seq: int = 0, *, uav: str | None = None, target: str | None = None, sensor: str | None = None,
             level_min: int = 0, limit: int = 500) -> tuple[int, dict]:
        """(码, 回复)：码 0，或 319（显式的 since_seq > 0 早于环中最早一条，中间记录已丢弃）。since_seq = 0 从最早一条开始。"""
        from .params import LEVEL_INDEX

        since = max(int(since_seq or 0), 0)
        limit = max(1, min(int(limit or 500), 500))
        if since > 0 and self.items and since + 1 < self.oldest_seq:
            return 319, {"oldest_seq": self.oldest_seq, "since_seq": since}
        out: list[dict] = []
        last = since
        for r in self.items:
            if r["seq"] <= since:
                continue
            last = r["seq"]
            if uav and r.get("uav") != uav:
                continue
            if target and r.get("target") != target:
                continue
            if sensor and r.get("sensor") != sensor:
                continue
            if level_min and LEVEL_INDEX.get(str(r.get("level_to")), 0) < level_min:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return 0, {"items": out, "next_since_seq": int(last), "oldest_seq": self.oldest_seq}


class Publisher:
    """按总线键缓存 publisher（与 M13 `nav_pub._bus_put` 同一做法）；`sink(key, bytes)` 为测试替代去向。"""

    def __init__(self) -> None:
        self._pubs: dict[str, Any] = {}
        self._bus: Any = None
        self.sink: Any = None
        self.puts = 0

    def put(self, ctx: Any, key: str, t_ns: int, items: dict[str, dict]) -> bool:
        if not items:
            return False
        import msgpack

        raw = msgpack.packb({"v": 1, "t_sim_ns": int(t_ns), "items": items}, use_bin_type=True)
        if self.sink is not None:
            self.sink(key, raw)
            self.puts += 1
            return True
        ev = getattr(ctx, "events", None)
        bus = getattr(ev, "bus", None)
        if bus is None:
            return False
        try:
            if bus is not self._bus:
                self._bus, self._pubs = bus, {}
            pub = self._pubs.get(key)
            if pub is None:
                pub = self._pubs[key] = bus.publisher(key)
            pub.put(raw)
            self.puts += 1
            return True
        except Exception:
            self._pubs.pop(key, None)
            return False
