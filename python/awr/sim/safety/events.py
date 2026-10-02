"""SafetyEventSink（事件组装与合批）与 safety 行快照发布（M09 §6.3.4、§6.3.5、§6.13；FR-014、FR-100 至 FR-104）。

- 事件：守卫在条件边沿登记待发事件，仲裁器登记转移；fsm stage 每 tick 末尾统一经 M11 `EventPublisher.emit()` 追加，
  EventPublisher 在主循环每轮迭代对每个 category 至多一次 put（ADR-018），因此 1000 架同 tick 转移只 put 一次
  `evt/sim-core/safety`（`uav.state` 属 `sim` category，同轮另一次 put；见实现报告偏差表）。事件只带代码与参数，
  文案由前端按文案键生成（FR-104）。
- safety 行：慢任务 10 Hz【仿真】在主循环内只做兴趣集行的 numpy 拷贝（`snapshot()`），后台线程组装 `awr.uav.safety.v1`
  字典、按量化步长判断变化（margin 0.5 m、时间 1 s、soc 1%）并 msgpack 编码，载荷 `[[agent_no, row], …]` 发布到
  `state/sim-core/safety`（api DetailDemux 原样切片转发，M11-FR-072）；1 Hz【墙钟】发布全量。
"""

from __future__ import annotations

import contextlib
import logging
import math
import queue
import threading
from dataclasses import dataclass
from typing import Any

import msgpack
import numpy as np

from awr.contracts import bus_keys
from awr.contracts.enums import FLIGHTSTATE_NAMES, GCSLOSSPOLICY_NAMES, FlightState, sub_name
from awr.contracts.safety_codes import SAFETY_CODES

from . import codes as C
from .state import COND_BITS, COND_CODE, COND_LEVEL, N_COND_SINCE

__all__ = ["RowPublisher", "SafetyEventSink", "fs_label", "uav_state_level"]

log = logging.getLogger("awr.sim.safety.events")
FS = FlightState
LINK_SRC_NAMES = ("self", "seat", "agent")
LINK_STATE_NAMES = ("OK", "DEGRADED", "LOST_HOLD", "LOST_RTL")
_KILLED = 2  # DISARMED 子模式 KILLED


_LABEL: dict[tuple[int, int], str] = {}
_OBJ: dict[tuple[int, int], dict] = {}
_N_PSEUDO = len(C.PSEUDO)


def fs_label(fs: int, sub: int) -> str:
    key = (int(fs), int(sub))
    lab = _LABEL.get(key)
    if lab is None:
        lab = _LABEL[key] = f"{FLIGHTSTATE_NAMES[key[0]]}/{sub_name(key[0], key[1]) or key[1]}"
    return lab


def fs_obj(fs: int, sub: int) -> dict:
    """事件 data 的 `{state, sub}`（缓存；只读共享）。"""
    key = (int(fs), int(sub))
    o = _OBJ.get(key)
    if o is None:
        o = _OBJ[key] = {"state": FLIGHTSTATE_NAMES[key[0]], "sub": sub_name(key[0], key[1])}
    return o


def uav_state_level(fs: int, sub: int, failsafe: bool) -> int:
    """`uav.state` 的 level（M09 §6.13）。"""
    if fs in (FS.ELAND, FS.FAILSAFE, FS.CRASHED) or (fs == FS.DISARMED and sub == _KILLED):
        return 3
    if fs in (FS.CORRECTING, FS.HOLD, FS.RTL) or (fs == FS.LANDING and failsafe):
        return 2
    return 0


@dataclass(slots=True)
class _Ev:
    slot: int
    code: int
    t_ns: int
    value: float | None = None
    threshold: float | None = None
    detail: str | None = None
    frm: tuple[int, int] | None = None
    to: tuple[int, int] | None = None
    origin: int = 1
    rank: int = 0


class SafetyEventSink:
    """事件缓冲：守卫与仲裁器只追加，fsm stage 末尾 `flush(ctx)` 一次性 emit。"""

    ORIGIN = ("SYSTEM", "AUTO", "OPERATOR")

    def __init__(self) -> None:
        self.pending: list[_Ev] = []
        self.states: list[tuple[int, int, int, int, int, int, int, bool]] = []
        self.counts: dict[str, int] = {"info": 0, "warn": 0, "action": 0, "critical": 0}
        self.by_code: dict[str, int] = {}
        self.per_slot_guard: dict[int, int] = {}
        self.emitted = 0
        self.flushes = 0
        self.log: list[dict] | None = None  # 测试：置为列表即记录全部已发事件（确定性对拍）

    def reset(self) -> None:
        self.pending.clear()
        self.states.clear()
        for k in self.counts:
            self.counts[k] = 0
        self.by_code.clear()
        self.per_slot_guard.clear()

    def add(self, slot: int, code: int, t_ns: int, *, value: float | None = None, threshold: float | None = None,
            detail: str | None = None, frm: tuple[int, int] | None = None, to: tuple[int, int] | None = None,
            origin: int = 1, rank: int = 0) -> None:
        if not C.is_event(code):
            return
        self.pending.append(_Ev(int(slot), int(code), int(t_ns), value, threshold, detail, frm, to, int(origin), int(rank)))

    def add_many(self, slots: np.ndarray, code: int, t_ns: int, *, values: np.ndarray | None = None,
                 threshold: float | None = None, detail: str | None = None, rank: int = 0) -> None:
        for k, s in enumerate(np.asarray(slots).reshape(-1)):
            v = None if values is None else float(values[k])
            self.add(int(s), code, t_ns, value=v, threshold=threshold, detail=detail, rank=rank)

    def add_transitions(self, slots: list, codes: list, t_ns: int, values: list, thrs: list, details: list, fs0: list,
                        sub0: list, fs1: list, sub1: list, origins: list, ranks: list) -> None:
        """批量登记带转移的事件（调用方已向量取数）。"""
        ev = self.pending
        for k, c in enumerate(codes):
            if c < _N_PSEUDO:
                continue
            ev.append(_Ev(slots[k], c, t_ns, values[k], thrs[k], details[k], (fs0[k], sub0[k]), (fs1[k], sub1[k]),
                          origins[k], ranks[k]))

    def state_change(self, slot: int, fs0: int, sub0: int, fs1: int, sub1: int, reason: int, t_ns: int,
                     failsafe: bool) -> None:
        self.states.append((int(slot), int(fs0), int(sub0), int(fs1), int(sub1), int(reason), int(t_ns), bool(failsafe)))

    # ------------------------------------------------------------------ emit
    def flush(self, ctx: Any, ids: list[str | None]) -> int:
        """把本 tick 的 safety 事件与 uav.state 追加到 EventPublisher（ctx.events）。返回条数。"""
        if not self.pending and not self.states:
            return 0
        ev_pub = getattr(ctx, "events", None)
        n = 0
        codes = C.CODES
        counts, by_code = self.counts, self.by_code
        for e in self.pending:
            code = codes[e.code]
            sc = SAFETY_CODES[code]
            counts[sc.cls] = counts.get(sc.cls, 0) + 1
            by_code[code] = by_code.get(code, 0) + 1
            data = {"code": code, "cls": sc.cls,
                    "from": None if e.frm is None else fs_obj(*e.frm),
                    "to": None if e.to is None else fs_obj(*e.to),
                    "value": _num(e.value), "threshold": _num(e.threshold), "detail": e.detail,
                    "origin": self.ORIGIN[e.origin], "rank": int(e.rank)}
            uav = ids[e.slot] if 0 <= e.slot < len(ids) else None
            if ev_pub is not None:
                ev_pub.emit(sc.type, t_sim_ns=e.t_ns, severity=sc.level, uav=uav, fields=data)
            if self.log is not None:
                self.log.append({"kind": sc.type, "uav": uav, "t_sim_ns": e.t_ns, **data})
            n += 1
        for slot, fs0, sub0, fs1, sub1, reason, t_ns, fsafe in self.states:
            uav = ids[slot] if 0 <= slot < len(ids) else None
            frm, to = fs_label(fs0, sub0), fs_label(fs1, sub1)
            lvl = uav_state_level(fs1, sub1, fsafe)
            if ev_pub is not None:
                ev_pub.emit("uav.state", t_sim_ns=t_ns, severity=lvl, uav=uav,
                            fields={"uav": uav, "from": frm, "to": to, "reason": codes[reason]})
            if self.log is not None:
                self.log.append({"kind": "uav.state", "uav": uav, "t_sim_ns": t_ns, "from": frm, "to": to,
                                 "reason": codes[reason], "level": lvl})
            n += 1
        self.pending.clear()
        self.states.clear()
        self.emitted += n
        self.flushes += 1
        return n


def _num(x: float | None) -> float | None:
    if x is None:
        return None
    x = float(x)
    return round(x, 3) if math.isfinite(x) else None


# ====================================================================== safety 行
_QUANT_M = 0.5
_QUANT_S = 1.0
_QUANT_SOC = 0.01
BIG_M = 99999.0


class RowPublisher:
    """兴趣集 safety 行：主线程 `snapshot()`（numpy 拷贝）→ 后台线程组装、量化比较、编码与发布（`sync = True` 时同步，测试用）。"""

    def __init__(self, full_period_ns: int = 1_000_000_000, sync: bool = False) -> None:
        self.full_period_ns = full_period_ns
        self.sync = sync
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._pub: Any = None
        self._bus: Any = None
        self._last_key: dict[int, tuple] = {}
        self._last_full_ns = -(10 ** 18)
        self.last_rows: dict[int, dict] = {}
        self.last_payload: bytes | None = None
        self.last_pub_rows: dict[int, dict] = {}
        self.published = 0
        self.bytes_out = 0
        self.stopped = False

    # ---- 主线程
    def snapshot(self, rt: Any, ctx: Any) -> dict | None:
        S = rt.S
        interest = np.asarray(getattr(ctx, "interest", np.zeros(0)), np.int64)
        if interest.size == 0:
            return None
        act = np.flatnonzero(S.active)
        if act.size == 0:
            return None
        no = S.agent_no[act]
        sel = act[np.isin(no, interest)]
        if sel.size == 0:
            return None
        sb, bb = S.blocks["safety"], S.blocks["battery"]
        snap = {"slots": sel.copy(), "agent_no": S.agent_no[sel].copy(),
                "ids": [S.ids[int(s)] for s in sel], "t_sim_ns": int(S.t_ns),
                "wall_ns": int(ctx.clock.wall_mono_ns()) if getattr(ctx, "clock", None) is not None else 0}
        for k in ("fs", "sub", "fs_auto", "latch", "t_enter_ns", "reason", "cond", "geo_margin_m", "clearance_m",
                  "zone_hit", "sep_m", "sep_mate", "link_src", "policy", "link_state", "locked", "correct_target",
                  "fault_mask", "yield_state"):
            snap[k] = sb[k][sel].copy()
        snap["cond_since_ns"] = sb["cond_since_ns"][sel].copy()
        snap["mate_ids"] = [S.ids[int(m)] if 0 <= int(m) < S.capacity else None for m in snap["sep_mate"]]
        for k in ("soc", "has_bat", "t_rem_s", "t_rtl_s", "z_rtl_m", "p_avg_w", "e_use_wh"):
            snap[k] = bb[k][sel].copy()
        snap["link_age_ms"] = rt.link.age_ms_of(sel) if rt.link is not None else np.full(sel.size, -1, np.int64)
        snap["zone_names"] = rt.geo.zone_names if rt.geo is not None else []
        snap["resume"] = [rt.admission.resume_status(int(s)) for s in sel] if rt.admission is not None else \
            [(True, None)] * sel.size
        snap["faults"] = [rt.faults.row_faults(int(s)) if rt.faults is not None else [] for s in sel]
        snap["rtl_margin"] = rt.params.battery.rtl_margin
        snap["emerg"] = rt.params.battery.emerg
        return snap

    def submit(self, snap: dict | None, bus: Any) -> None:
        if snap is None:
            return
        self._bus = bus
        if self.sync:
            self._process(snap)
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="m09-safety-rows", daemon=True)
            self._thread.start()
        self._q.put(snap)

    def close(self) -> None:
        self.stopped = True
        if self._thread is not None:
            self._q.put(None)
            self._thread.join(timeout=1.0)
        if self._pub is not None:
            with contextlib.suppress(Exception):
                self._pub.close()
            self._pub = None

    # ---- 后台线程
    def _run(self) -> None:
        while not self.stopped:
            snap = self._q.get()
            if snap is None:
                return
            try:
                self._process(snap)
            except Exception:  # 编码失败只记日志，不影响主循环
                log.exception("safety row encode failed")

    def _process(self, snap: dict) -> None:
        full = snap["wall_ns"] - self._last_full_ns >= self.full_period_ns
        if full:
            self._last_full_ns = snap["wall_ns"]
        pk = msgpack.Packer(use_bin_type=True, use_single_float=True)  # 显示用数值：float32 足够，行更小
        items: list[bytes] = []
        pub_rows: dict[int, dict] = {}
        for k in range(len(snap["slots"])):
            row = build_row(snap, k)
            no = int(snap["agent_no"][k])
            key = quant_key(row)
            self.last_rows[no] = row
            if not full and self._last_key.get(no) == key:
                continue
            self._last_key[no] = key
            items.append(pk.pack([no, row]))
            pub_rows[no] = row
        if not items:
            return
        payload = pk.pack_array_header(len(items)) + b"".join(items)
        self.last_payload = payload
        self.last_pub_rows = pub_rows  # 与 last_payload 同一次发布的行（last_rows 含未发布的最新快照）
        self.published += 1
        self.bytes_out += len(payload)
        bus = self._bus
        if bus is None:
            return
        try:
            if self._pub is None:
                self._pub = bus.publisher(bus_keys.state_safety("sim-core"))
            self._pub.put(payload)
        except Exception:
            log.debug("safety row publish failed", exc_info=True)


def build_row(snap: dict, k: int) -> dict:
    """snapshot 第 k 行 -> `awr.uav.safety.v1`（17 §6.5；M09 §6.3.5）。"""
    fs, sub = int(snap["fs"][k]), int(snap["sub"][k])
    cond = int(snap["cond"][k])
    active = []
    for b, (name, _lv) in enumerate(COND_BITS):
        if cond >> b & 1:
            since = int(snap["cond_since_ns"][k][b]) if b < N_COND_SINCE else -1
            active.append({"code": COND_CODE[name], "level": COND_LEVEL[b], "since_t_ns": since})
    latch = int(snap["latch"][k])
    latched = [n for bit, n in ((1, "ELAND"), (2, "FAILSAFE")) if latch & bit]
    margin = float(snap["geo_margin_m"][k])
    sep = float(snap["sep_m"][k])
    has_bat = bool(snap["has_bat"][k])
    soc = float(snap["soc"][k])
    p_avg = max(float(snap["p_avg_w"][k]), 1e-6)
    e_use = float(snap["e_use_wh"][k])
    t_rtl = float(snap["t_rtl_s"][k])
    soc_rtl = snap["emerg"] + snap["rtl_margin"] * t_rtl * p_avg / (e_use * 3600.0) if has_bat and e_use > 0 else 0.0
    zone = int(snap["zone_hit"][k])
    names = snap["zone_names"]
    mate = int(snap["sep_mate"][k])
    age = int(snap["link_age_ms"][k])
    src = int(snap["link_src"][k])
    ok, blocked = snap["resume"][k]
    tgt = snap["correct_target"][k]
    row = {
        "active": active,
        "fsm": {"state": FLIGHTSTATE_NAMES[fs], "sub": sub_name(fs, sub) or str(sub), "latched": latched,
                "auto": bool(snap["fs_auto"][k]), "since_t_ns": int(snap["t_enter_ns"][k]),
                "reason": C.code_of(int(snap["reason"][k]))},
        "geofence_margin_m": round(min(margin, BIG_M), 3) if math.isfinite(margin) else BIG_M,
        "separation_m": round(sep, 3) if math.isfinite(sep) and sep < 1e6 else None,
        "battery_rtl": {"t_rem_s": round(min(float(snap["t_rem_s"][k]), 1e7), 1), "t_rtl_s": round(t_rtl, 1)} if has_bat else None,
        "energy": {"soc_pct": round(100.0 * soc, 1), "soc_rtl_pct": round(100.0 * min(soc_rtl, 1.0), 1),
                   "z_rtl_m": round(float(snap["z_rtl_m"][k]), 2)},
        "link": {"src": LINK_SRC_NAMES[src] if src < 3 else "self",
                 "policy": GCSLOSSPOLICY_NAMES.get(int(snap["policy"][k]), "hold_rtl"),
                 "state": LINK_STATE_NAMES[min(int(snap["link_state"][k]), 3)], "age_ms": None if src == 0 or age < 0 else age},
        "zone_id": names[zone] if 0 <= zone < len(names) else None,
        "clearance_m": round(min(float(snap["clearance_m"][k]), BIG_M), 3) if math.isfinite(float(snap["clearance_m"][k])) else BIG_M,
        "sep_mate": snap["mate_ids"][k] if mate >= 0 else None,
        "resume": {"ok": bool(ok), "blocked_by": blocked},
        "correct_target_enu_m": [round(float(x), 3) for x in tgt] if fs == FS.CORRECTING else None,
        "faults": snap["faults"][k],
    }
    # 可选字段取缺省值时省略（带宽：兴趣集 80 架 × 10 Hz，M09-NFR-009）；前端按缺省解释（resume 缺省为可恢复）
    for key, default in (("zone_id", None), ("sep_mate", None), ("correct_target_enu_m", None), ("faults", [])):
        if row[key] == default:
            del row[key]
    if row["resume"]["ok"]:
        del row["resume"]
    return row


def quant_key(row: dict) -> tuple:
    """量化后的"变化"判据（margin 0.5 m、时间 1 s、soc 1%，M09-FR-101）。"""
    act = tuple((a["code"], a["level"]) for a in row["active"])
    f = row["fsm"]
    q = lambda x, s: None if x is None else round(x / s)  # noqa: E731
    br = row["battery_rtl"]
    return (act, f["state"], f["sub"], tuple(f["latched"]), f["auto"], q(row["geofence_margin_m"], _QUANT_M),
            q(row["separation_m"], _QUANT_M), None if br is None else (q(br["t_rem_s"], _QUANT_S), q(br["t_rtl_s"], _QUANT_S)),
            q(row["energy"]["soc_pct"] / 100.0, _QUANT_SOC), row["link"]["state"], row["link"]["src"],
            q(None if row["link"]["age_ms"] is None else row["link"]["age_ms"] / 1000.0, _QUANT_S), row.get("zone_id"),
            "resume" not in row, row.get("sep_mate"), len(row.get("faults", ())))
