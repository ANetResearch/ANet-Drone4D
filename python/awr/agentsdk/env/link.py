"""链路层（M26 §6.6.6；AWR-05 §5.5.5；P0a 基础版）。

- 路损：2.4 GHz 自由空间 `20 lg d + 20 lg f − 27.55`，非视距（世界网格遮挡）+20 dB；余量 = 发射 20 dBm − 路损 + 灵敏度 90 dB；
- 丢包：`logistic((10 − margin) / 2)`，与事件通信区的额外丢包合成 `1 − (1 − p)(1 − p_zone)`；
- 速率：`1 Mbps · clip(margin / 30, 0.05, 1) · bandwidth_scale`，时延 `10 ms + 8B / rate + U(0, 20) ms + 区内附加时延`；
- 链路状态按 `(min, max, ⌊t / 1 s⌋)` 对称缓存（1 s 内逐消息不重复求视线）；
- 上行令牌桶：每机 `bucket_Bps` 补充、容量 `burst_B`，预算不足的消息丢弃（`dropped_budget`）；
- 随机数键 `(seed, 20, src_no·256 + dst_no, msg_seq, ch)`，与处理次序无关；逐条记录写入 `/net/records`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from awr.agentsdk.env.geom import los as los_batch
from awr.agentsdk.rng import CH, STREAM_LINK, u01

__all__ = ["CommCfg", "LinkModel"]


@dataclass(frozen=True)
class CommCfg:
    enabled: bool = True
    bucket_Bps: int = 125_000
    burst_B: int = 125_000
    msg_max_B: int = 4096
    model: str = "fspl-nlos@1"


def _in_poly(poly: np.ndarray, x: float, y: float) -> bool:
    c = False
    n = len(poly)
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[i - 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            c = not c
    return c


@dataclass
class LinkModel:
    seed: int
    uavs: tuple[str, ...]
    world: object
    tape: object | None
    cfg: CommCfg = field(default_factory=CommCfg)
    f_mhz: float = 2400.0

    def __post_init__(self) -> None:
        self.no = {u: i for i, u in enumerate(self.uavs)}
        self.bucket = {u: float(self.cfg.burst_B) for u in self.uavs}
        self.stats = {u: {"sent": 0, "dropped_budget": 0, "rejected": 0, "lost": 0, "delivered": 0, "bytes": 0} for u in self.uavs}
        self.seq: dict[tuple[str, str], int] = {}
        self.out_seq = dict.fromkeys(self.uavs, 0)
        self.pending: list[tuple[int, int, str, dict]] = []     # (t_recv_ns, 序号, dst, Message)
        self.records: list[dict] = []
        self._cache: dict[tuple, tuple[float, float, bool, float]] = {}
        self._n = 0

    def refill(self, dt_s: float) -> None:
        for u in self.uavs:
            self.bucket[u] = min(float(self.cfg.burst_B), self.bucket[u] + self.cfg.bucket_Bps * dt_s)

    def _state(self, a: str, b: str, pa: np.ndarray, pb: np.ndarray, t_ns: int) -> tuple[float, float, bool, float]:
        key = (min(a, b), max(a, b), t_ns // 1_000_000_000)
        if key in self._cache:
            return self._cache[key]
        d = max(float(np.linalg.norm(pa - pb)), 1.0)
        los = bool(los_batch(self.world, pa[None], pb[None])[0])
        pl = 20 * math.log10(d) + 20 * math.log10(self.f_mhz) - 27.55 + (0.0 if los else 20.0)
        margin = 20.0 - pl + 90.0
        per = 1.0 / (1.0 + math.exp((margin - 10.0) / 2.0))
        extra, lat_ms, scale = 0.0, 0.0, 1.0
        if self.tape is not None:
            for z in self.tape.comm_zones_at(t_ns):
                if _in_poly(z["polygon_m"], *pa[:2]) or _in_poly(z["polygon_m"], *pb[:2]):
                    extra = max(extra, float(z["extra_loss"]))
                    lat_ms = max(lat_ms, float(z["extra_latency_ms"]))
                    scale = min(scale, float(z["bandwidth_scale"]))
        rate = 1e6 * float(np.clip(margin / 30.0, 0.05, 1.0)) * scale
        v = (1.0 - (1.0 - per) * (1.0 - extra), rate, los, lat_ms)
        self._cache[key] = v
        if len(self._cache) > 4096:
            self._cache.clear()
        return v

    def transmit(self, src: str, msgs: list[dict], t_ns: int, pos: dict[str, np.ndarray]) -> None:
        """msgs 已经守卫校验（dst 为 "*" 或 uav id，payload 为 bytes）；pos 为真值位置。"""
        for m in msgs:
            nb = len(m["payload"]) + len(m["topic"]) + 16
            if self.bucket[src] < nb:
                self.stats[src]["dropped_budget"] += 1
                continue
            self.bucket[src] -= nb
            self.stats[src]["sent"] += 1
            self.stats[src]["bytes"] += nb
            self.out_seq[src] += 1
            dsts = [d for d in self.uavs if d != src] if m["dst"] == "*" else [m["dst"]]
            for dst in dsts:
                s = self.seq[(src, dst)] = self.seq.get((src, dst), 0) + 1
                ag = self.no[src] * 256 + self.no[dst]
                loss, rate, los, zlat = self._state(src, dst, pos[src], pos[dst], t_ns)
                if u01(self.seed, STREAM_LINK, ag, s, CH["link_loss"]) < loss:
                    self.stats[src]["lost"] += 1
                    self.records.append({"t_ns": t_ns, "src": src, "dst": dst, "ok": False, "lat_ms": None, "los": los, "bytes": nb, "why": "loss"})
                    continue
                lat_s = 0.010 + nb * 8 / rate + 0.020 * u01(self.seed, STREAM_LINK, ag, s, CH["link_lat"]) + zlat / 1000.0
                t_recv = t_ns + round(lat_s * 1e9)
                self._n += 1
                self.pending.append((t_recv, self._n, dst, {"src": src, "seq": self.out_seq[src], "topic": m["topic"], "t_sent_ns": t_ns,
                                                            "t_recv_ns": t_recv, "payload": m["payload"]}))
                self.stats[src]["delivered"] += 1
                self.records.append({"t_ns": t_ns, "src": src, "dst": dst, "ok": True, "lat_ms": round(lat_s * 1000, 2), "los": los, "bytes": nb,
                                     "why": None})

    def deliver_due(self, t_ns: int) -> dict[str, list[dict]]:
        due = sorted(p for p in self.pending if p[0] <= t_ns)
        self.pending = [p for p in self.pending if p[0] > t_ns]
        out: dict[str, list[dict]] = {}
        for _t, _n, dst, msg in due:
            out.setdefault(dst, []).append(msg)
        return out

    def status(self, uav: str) -> dict:
        s = self.stats[uav]
        return {"enabled": self.cfg.enabled, "bucket_B": int(self.bucket[uav]), "sent": s["sent"], "dropped_budget": s["dropped_budget"],
                "rejected": s["rejected"]}
