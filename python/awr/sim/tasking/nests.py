"""机巢运行时（M20-FR-001 至 FR-008；M20 §6.2.1、§6.3、§6.11.4；AWR-04 §8.2；17 §17.6.6）。

- `normalize(d)`：机巢对象的校验与缺省（17 §17.6.6：`kind` box、`slots` 6、充电器 4 × C1-XR、`link_range_m` 3500、`rtk_base` false；
  `launcher` 恒 `crewed = true`、检查 600 s）；越界 110（字段级 detail），`inventory` 只在创建时使用（PATCH 中 110 `INVENTORY_READONLY`）。
- `NestRT`：起降位时间线（相邻事件 ≥ `launch_interval_s`，回收优先）、电池（INSTALLED、QUEUED、CHARGING、READY）、充电通道（FIFO、
  节数兼容）、机体在巢状态（ready、charging、swapping、turnaround、waiting_pack、airborne、standby）与 1 Hz 状态载荷
  `awr.nest.status.v1`。充电时间按 AWR-20 §4.5（`awr.tasking.nestmodel.charge_time_s`），换电经 M09 `set_soc` + `reset_flight` 写入。
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from awr.tasking.model import NestView, TaskingError
from awr.tasking.nestmodel import SOC_READY, PadTimeline, charge_time_s, soc_after

__all__ = ["DEFAULT_CHARGER", "KINDS", "MAX_NESTS", "NestRT", "PackRT", "normalize", "sanitize_name", "slot_offsets"]

KINDS = ("box", "launcher", "vtol_pad")
MAX_NESTS = 4
DEFAULT_CHARGER = {"model": "c1_xr", "count": 4, "channels": 1, "i_max_a": 10.0, "cells_max": 6}
DEFAULT_V_CHARGER = {"model": "chg12s_ref", "count": 2, "channels": 1, "i_max_a": 10.0, "cells_max": 12}
SLOT_SPACING_M = 5.0
MIN_LAUNCH_SOC = 0.30
_FIELDS = {"id", "name", "pos", "kind", "crewed", "slots", "energy", "chargers", "turnaround_s", "launch_interval_s", "wind_max_mps", "rtk_base",
           "link_range_m", "inventory", "vehicle_ids"}


def sanitize_name(s: Any, limit: int = 32) -> str:
    out = []
    for ch in str(s or ""):
        cat = unicodedata.category(ch)
        if cat.startswith("C") or cat == "So" or ord(ch) >= 0x1F000:
            continue
        out.append(ch)
    return "".join(out).strip()[:limit]


def _rng(d: dict, k: str, lo: float, hi: float, default: Any) -> Any:
    v = d.get(k, default)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise TaskingError(110, "param", field=k, value=v) from None
    if not (lo <= f <= hi) or not math.isfinite(f):
        raise TaskingError(110, "param", field=k, value=f, range=[lo, hi])
    return f


def normalize(d: dict, *, nid: str, prev: dict | None = None) -> dict:
    """校验并补缺省（PATCH 时 prev 为当前配置，d 为合并后的对象）。"""
    if not isinstance(d, dict):
        raise TaskingError(110, "param", field="nest")
    bad = sorted(set(d) - _FIELDS)
    if bad:
        raise TaskingError(110, "param", field=bad[0], why_detail="UNKNOWN_FIELD")
    kind = str(d.get("kind") or "box")
    if kind not in KINDS:
        raise TaskingError(110, "param", field="kind", allowed=list(KINDS))
    pos = d.get("pos")
    if not (isinstance(pos, (list, tuple)) and 2 <= len(pos) <= 3 and all(isinstance(x, (int, float)) for x in pos[:2])):
        raise TaskingError(110, "param", field="pos")
    crewed = True if kind == "launcher" else bool(d.get("crewed", False))
    en = dict(d.get("energy") or {})
    mode = str(en.get("mode") or "swap")
    if mode not in ("swap", "charge"):
        raise TaskingError(110, "param", field="energy.mode")
    packs = []
    for k, p in enumerate(en.get("packs") or []):
        if not isinstance(p, dict) or int(p.get("cells", 0)) < 1 or int(p.get("count", -1)) < 0:
            raise TaskingError(110, "param", field=f"energy.packs[{k}]")
        packs.append({kk: p[kk] for kk in ("cells", "count", "capacity_ah", "chemistry", "pack_id") if kk in p})
    if sum(int(p["count"]) for p in packs) > 64:
        raise TaskingError(110, "param", field="energy.packs", why_detail="TOO_MANY_PACKS")
    chargers = d.get("chargers")
    if chargers is None:
        chargers = [dict(DEFAULT_V_CHARGER if kind == "vtol_pad" else DEFAULT_CHARGER)]
    ch_out = []
    for k, c in enumerate(chargers):
        try:
            ch_out.append({"model": str(c["model"]), "count": int(c["count"]), "channels": int(c["channels"]), "i_max_a": float(c["i_max_a"]),
                           "cells_max": int(c["cells_max"])})
        except (KeyError, TypeError, ValueError):
            raise TaskingError(110, "param", field=f"chargers[{k}]") from None
    if sum(c["count"] * c["channels"] for c in ch_out) > 16:
        raise TaskingError(110, "param", field="chargers", why_detail="TOO_MANY_CHANNELS")
    out = {
        "id": nid, "name": sanitize_name(d.get("name") or nid) or nid, "pos": [float(pos[0]), float(pos[1]), None if len(pos) < 3 or pos[2] is None else float(pos[2])],
        "kind": kind, "crewed": crewed, "slots": int(_rng(d, "slots", 1, 12, 6)),
        "energy": {"mode": mode, "swap_s": float(_rng(en, "swap_s", 30, 900, 180.0)), "packs": packs},
        "chargers": ch_out, "turnaround_s": float(_rng(d, "turnaround_s", 0, 3600, 600.0 if kind == "launcher" else 60.0)),
        "launch_interval_s": float(_rng(d, "launch_interval_s", 5, 600, 30.0)), "wind_max_mps": float(_rng(d, "wind_max_mps", 0, 25, 12.0)),
        "rtk_base": bool(d.get("rtk_base", False)), "link_range_m": _rng(d, "link_range_m", 0, 50000, 3500.0),
    }
    return out


def slot_offsets(n: int) -> list[tuple[float, float]]:
    """槽位相对机巢中心的水平偏移（3 列网格，间距 5 m；M08 出生点间距下限 2√2·1.5 m）。"""
    out = []
    for k in range(n):
        r, c = divmod(k, 3)
        out.append(((c - 1) * SLOT_SPACING_M, (r - 0.5 * ((n - 1) // 3)) * SLOT_SPACING_M))
    return out


@dataclass
class PackRT:
    pack_id: str
    cells: int
    capacity_ah: float
    c_max: float
    soc: float
    state: str = "ready"            # installed | queued | charging | ready
    t_ready_s: float | None = None
    ch: int | None = None
    t_start_s: float = 0.0
    soc0: float = 0.0
    vehicle: str | None = None


@dataclass
class ChannelRT:
    ch: int
    model: str
    i_max_a: float
    cells_max: int
    pack_id: str | None = None


@dataclass
class AirRT:
    uav: str
    state: str = "ready"            # ready | charging | swapping | turnaround | waiting_pack | airborne | standby
    t_until: float = 0.0
    pack_id: str | None = None
    slot_k: int = 0
    cells: int | None = None


@dataclass
class NestRT:
    cfg: dict
    pos3: tuple[float, float, float]
    pad: PadTimeline = field(default_factory=PadTimeline)
    packs: dict[str, PackRT] = field(default_factory=dict)
    channels: list[ChannelRT] = field(default_factory=list)
    air: dict[str, AirRT] = field(default_factory=dict)
    queue: list[str] = field(default_factory=list)            # QUEUED 电池（FIFO）
    launch_queue: list[dict] = field(default_factory=list)
    recover_queue: list[dict] = field(default_factory=list)
    wind_ok: bool = True
    min_ready: int | None = None
    seq: int = 0

    @property
    def id(self) -> str:
        return str(self.cfg["id"])

    @property
    def mode(self) -> str:
        return str(self.cfg["energy"]["mode"])

    def view(self) -> NestView:
        c = self.cfg
        return NestView(self.id, self.pos3, c["kind"], bool(c["crewed"]), float(c["launch_interval_s"]), float(c["turnaround_s"]),
                        float(c["energy"]["swap_s"]), c.get("link_range_m"), float(c["wind_max_mps"]), bool(c["rtk_base"]))

    @staticmethod
    def build(cfg: dict, z_ground: float) -> NestRT:
        p = cfg["pos"]
        z = float(p[2]) if p[2] is not None else float(z_ground)
        n = NestRT(cfg, (float(p[0]), float(p[1]), z), PadTimeline(float(cfg["launch_interval_s"])))
        n.rebuild_channels()
        n.add_spares()
        return n

    def rebuild_channels(self) -> None:
        old = {c.ch: c.pack_id for c in self.channels}
        chans: list[ChannelRT] = []
        k = 0
        for c in self.cfg["chargers"]:
            for _ in range(int(c["count"]) * int(c["channels"])):
                chans.append(ChannelRT(k, c["model"], float(c["i_max_a"]), int(c["cells_max"]), old.get(k)))
                k += 1
        self.channels = chans

    def add_spares(self, defaults: dict | None = None) -> None:
        """配置的备用电池（满电 READY）；容量与倍率缺省取机型电池包（首次有机体入巢时补全）。"""
        for spec in self.cfg["energy"]["packs"]:
            for j in range(int(spec["count"])):
                pid = str(spec.get("pack_id") or f"{self.id}-s{int(spec['cells'])}-{j}")
                if j and spec.get("pack_id"):
                    pid = f"{spec['pack_id']}-{j}"
                if pid in self.packs:
                    continue
                cap = float(spec.get("capacity_ah") or (defaults or {}).get("capacity_ah") or 10.0)
                self.packs[pid] = PackRT(pid, int(spec["cells"]), cap, float((defaults or {}).get("c_max", 1.0)), 0.95, "ready", None)

    # ------------------------------------------------------------ 机体
    def attach(self, uav: str, pack: dict, soc: float) -> None:
        if uav in self.air:
            return
        used = {a.slot_k for a in self.air.values()}
        k = next(i for i in range(64) if i not in used)
        pid = f"{uav}-p0"
        self.packs[pid] = PackRT(pid, int(pack.get("cells", 6)), float(pack.get("capacity_ah", 10.0)), float(pack.get("c_max", 1.0)), float(soc),
                                 "installed", None, vehicle=uav)
        self.air[uav] = AirRT(uav, "ready", 0.0, pid, k)
        for p in self.packs.values():
            if p.vehicle is None and p.state == "ready" and p.capacity_ah == 10.0 and pack.get("capacity_ah") and p.cells == int(pack.get("cells", 0)):
                p.capacity_ah, p.c_max = float(pack["capacity_ah"]), float(pack.get("c_max", 1.0))

    def detach(self, uav: str) -> None:
        a = self.air.pop(uav, None)
        if a is not None and a.pack_id:
            p = self.packs.get(a.pack_id)
            if p is not None and p.vehicle == uav and p.state == "installed":
                self.packs.pop(a.pack_id, None)
        self.pad.cancel(uav)

    def occupied(self) -> int:
        return sum(1 for a in self.air.values() if a.state not in ("airborne", "standby"))

    # ------------------------------------------------------------ 充电与换电（1 Hz）
    def tick(self, t: float) -> list[tuple[str, dict]]:
        """推进充电与换电；返回待发事件 [(kind, data)] 与换电完成的机体（data 中 `swap_uav`、`soc`）。"""
        ev: list[tuple[str, dict]] = []
        for ch in self.channels:
            if ch.pack_id is None:
                continue
            p = self.packs.get(ch.pack_id)
            if p is None:
                ch.pack_id = None
                continue
            p.soc = min(SOC_READY, soc_after(p.capacity_ah, p.c_max, ch.i_max_a, p.soc0, t - p.t_start_s))
            if t >= (p.t_ready_s or math.inf) - 1e-9:
                p.soc = SOC_READY
                ch.pack_id = None
                p.ch = None
                if p.vehicle is not None and self.mode == "charge":
                    p.state = "installed"
                    a = self.air.get(p.vehicle)
                    if a is not None and a.state == "charging":
                        a.state = "ready"
                        ev.append(("_charged", {"uav": p.vehicle, "soc": p.soc}))
                else:
                    p.state = "ready"
        # FIFO：排队电池进入空闲且节数兼容的通道
        for pid in list(self.queue):
            p = self.packs.get(pid)
            if p is None or p.state != "queued":
                self.queue.remove(pid)
                continue
            ch = next((c for c in self.channels if c.pack_id is None and p.cells <= c.cells_max), None)
            if ch is None:
                continue
            self.queue.remove(pid)
            ch.pack_id = pid
            p.state, p.ch, p.t_start_s, p.soc0 = "charging", ch.ch, t, p.soc
            p.t_ready_s = t + charge_time_s(p.capacity_ah, p.c_max, ch.i_max_a, p.soc, SOC_READY)
        # 机体：检查 → 换电（或充电）→ 就绪
        for uav in sorted(self.air):
            a = self.air[uav]
            if a.state == "turnaround" and t >= a.t_until - 1e-9:
                if self.mode == "swap":
                    # 卸下的电池进入充电队列（FIFO），机体等待 SOC 最高的 READY 电池
                    old = self.packs.get(a.pack_id or "")
                    if old is not None:
                        a.cells = old.cells
                        old.vehicle = None
                        old.state = "queued"
                        self.queue.append(old.pack_id)
                    a.pack_id = None
                    a.state = "waiting_pack"
                else:
                    p = self.packs.get(a.pack_id or "")
                    if p is not None:
                        p.state = "queued"
                        self.queue.append(p.pack_id)
                    a.state = "charging"
            if a.state == "waiting_pack":
                best = sorted((p for p in self.packs.values() if p.state == "ready" and p.vehicle is None and (a.cells is None or p.cells == a.cells)),
                              key=lambda p: (-round(p.soc, 6), p.pack_id))
                if best:
                    newp = best[0]
                    newp.vehicle = uav
                    newp.state = "installed"
                    a.pack_id = newp.pack_id
                    a.state = "swapping"
                    a.t_until = t + float(self.cfg["energy"]["swap_s"])
            if a.state == "swapping" and t >= a.t_until - 1e-9:
                a.state = "ready"
                p = self.packs.get(a.pack_id or "")
                ev.append(("nest.swap_done", {"nest": self.id, "uav": uav, "pack_id": a.pack_id, "soc_pct": round(100 * (p.soc if p else 0.95), 1),
                                              "swap_uav": uav, "soc": p.soc if p else 0.95}))
        ready = sum(1 for p in self.packs.values() if p.state == "ready" and p.vehicle is None)
        self.min_ready = ready if self.min_ready is None else min(self.min_ready, ready)
        return ev

    def recover(self, uav: str, t: float, soc: float) -> None:
        """机体着陆：检查计时开始，所装电池记下着陆 SOC。"""
        a = self.air.get(uav)
        if a is None:
            return
        p = self.packs.get(a.pack_id or "")
        if p is not None:
            p.soc = float(soc)
        a.state = "turnaround"
        a.t_until = t + float(self.cfg["turnaround_s"])
        self.recover_queue = [r for r in self.recover_queue if r.get("uav") != uav]

    def t_ready(self, uav: str, t: float) -> float:
        """可起飞时刻（在巢机体）：就绪为 now；检查、换电按计时；等电池为最早就绪的电池时刻；充电为充满时刻。"""
        a = self.air.get(uav)
        if a is None:
            return math.inf
        if a.state == "ready":
            return t
        if a.state in ("turnaround",):
            return a.t_until + (float(self.cfg["energy"]["swap_s"]) if self.mode == "swap" else 0.0)
        if a.state == "swapping":
            return a.t_until
        if a.state == "waiting_pack":
            nxt = min((p.t_ready_s or math.inf for p in self.packs.values() if p.state == "charging"), default=math.inf)
            return nxt + float(self.cfg["energy"]["swap_s"])
        if a.state == "charging":
            p = self.packs.get(a.pack_id or "")
            return (p.t_ready_s or math.inf) if p is not None and p.state == "charging" else math.inf
        return math.inf

    # ------------------------------------------------------------ 状态载荷
    def status(self, t_ns: int, inventory: list[dict], socs: dict[str, float]) -> dict:
        t = t_ns * 1e-9
        ch_out = []
        for c in self.channels:
            p = self.packs.get(c.pack_id) if c.pack_id else None
            i_a = None
            if p is not None:
                i_a = round(min(c.i_max_a, p.c_max * p.capacity_ah), 2)
            ch_out.append({"ch": c.ch, "model": c.model, "state": "charging" if p is not None else "idle",
                           "pack_id": c.pack_id, "soc_pct": None if p is None else round(100.0 * p.soc, 1),
                           "eta_s": None if p is None or p.t_ready_s is None else round(max(p.t_ready_s - t, 0.0), 1), "i_a": i_a})
        packs = [{"pack_id": p.pack_id, "cells": p.cells, "soc": round(min(max(p.soc, 0.0), 1.0), 4), "state": p.state,
                  "t_ready_s": None if p.t_ready_s is None else round(max(p.t_ready_s - t, 0.0), 1)} for p in sorted(self.packs.values(), key=lambda q: q.pack_id)]
        air = [{"uav": a.uav, "state": "charging" if a.state == "waiting_pack" else a.state, "soc": round(min(max(socs.get(a.uav, 1.0), 0.0), 1.0), 4)}
               for a in sorted(self.air.values(), key=lambda x: x.uav)]
        return {"id": self.id, "kind": self.cfg["kind"], "t_sim_ns": int(t_ns), "slots": {"total": int(self.cfg["slots"]), "occupied": self.occupied()},
                "inventory": inventory, "aircraft": air, "packs": packs, "chargers": ch_out,
                "launch_queue": [dict(q) for q in self.launch_queue], "recover_queue": [dict(q) for q in self.recover_queue], "wind_ok": bool(self.wind_ok)}

    def slot_pos(self, k: int, z: float) -> list[float]:
        dx, dy = slot_offsets(max(int(self.cfg["slots"]), k + 1))[k]
        return [self.pos3[0] + dx, self.pos3[1] + dy, z]

    def config(self, inventory: list[dict]) -> dict:
        c = {k: v for k, v in self.cfg.items()}
        c["pos"] = [self.pos3[0], self.pos3[1], self.pos3[2]]
        c["inventory"] = inventory
        return c

    def ready_packs(self) -> int:
        return sum(1 for p in self.packs.values() if p.state == "ready" and p.vehicle is None)

    def nearest_free_xy(self) -> np.ndarray:
        return np.asarray(self.pos3[:2], float)


def check_slots(n: NestRT, extra: int) -> None:
    if len(n.air) + extra > int(n.cfg["slots"]):
        raise TaskingError(525, "slots", slots=int(n.cfg["slots"]), occupied=len(n.air), add=extra)
