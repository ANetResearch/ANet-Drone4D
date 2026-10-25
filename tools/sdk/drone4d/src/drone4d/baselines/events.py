"""四类事件的 report 规则（M26-FR-122 P0b，M26 §14.3；M23 §15.3 允许的 report 类型；AWR-05 §5.2.8 规则 5 的 EU 映射）。

每条规则只在其 report 类型属于简报 `report_vocab`（即任务契约 `report_types`）时启用；只读本机轨迹（`common.Tracks`）、
本机云台指向记录与已收到的消息，不读真值。基线三策略共用这些规则（与 `meet`、`board`、`enter_building` 相同），策略差异来自
观察位、航线与跟踪。逐条规则（时刻为检测时刻；"轨迹已持续"指首末检测相隔）：

交汇与交接（`rendezvous_handoff`）：
- `handover`、`disperse`：`meet` 判据成立（两人 ≤ 3 m 保持 ≥ 5 s 且同帧 ≥ 20 次）后记录交汇区间 [t0, t1]（t1 为最后一次 ≤ 3 m）；
  其后 120 s 内两条轨迹均在跟且相距 ≥ 10 m 时报 `disperse`（窗口 [t − 1 s, t]），同时报 `handover`（窗口为交汇区间：检测级观测
  看不到物品，基线把每次交汇视为一次交接，置信度 0.3）；每对轨迹各报一次。`relation` 不报（act.v1 的 report 没有关系端点字段）。
- `alight`：人员轨迹已持续 ≥ 3 s，首个检测位于一辆静止车辆（拟合速度 < 1 m/s）4 m 内，该车轨迹比人员早 ≥ 2 s 出现且在人员
  出现前 1 s 内仍在跟；人员出现前 3 s 内该处 4 m 内没有人员轨迹丢失（排除同一人的分裂轨迹）。窗口 [t_first − 2 s, t_first]。

Re-ID（`reid_occlusion`）与车辆追踪（`vehicle_track`）：
- `exit_building`：人员轨迹已持续 ≥ 3 s，首个检测位于公开门锚点 4 m 内，附近 4 m 内无车辆；人员出现前 6 s 至 1 s 本机云台
  look-at 点曾在门 40 m 内（门在视野内，排除进入视野造成的"出现"）；分裂轨迹排除同上。窗口 [t_first − 1 s, t_first]。
- `reacquire` / `identity_link`：与简报目标外观匹配的轨迹在观测间断（人员 ≥ 5 s、车辆 ≥ 3 s，对应遮挡判据）后恢复，或目标的
  旧轨迹丢失后由新轨迹重新匹配到目标（间隔同上）时报一次，窗口为恢复时刻 [t, t]；人员目标优先报 `reacquire`（参与者为恢复的
  轨迹），车辆目标或词表只含 `identity_link` 时报 `identity_link`（参与者为旧轨迹与新轨迹；同一轨迹恢复时只有该轨迹）。
- `vehicle_event`（`event` 字段为车辆关键事件类型）：简报有车辆目标时只报外观匹配目标的车辆轨迹，否则报全部车辆轨迹。拟合速度
  > 2 m/s 为行驶、< 0.5 m/s 为静止（之间保持原状态）。`depart`：首次观测即静止且已静止 ≥ 3 s 的车辆开始行驶；`stop`：行驶后
  静止 3–30 s 再行驶，窗口为静止区间；`park`：行驶后静止 ≥ 30 s，窗口 [静止起点, t]（报 `park` 后再行驶时报 `depart`）；
  `turn`：行驶中航向与 6 s 前相差 ≥ 45°，窗口 [t − 6 s, t]，同一车辆 8 s 内只报一次。`enter_zone`、`exit_zone` 不报（简报锚点
  只有点位，无区域多边形）。

目标搜索（`target_search`）：
- `found`：目标类别（`brief.search.target_class`，缺省取简报目标的类别）的在跟轨迹已持续 ≥ 2 s 且 ≥ 5 个检测；简报给出该类别
  目标外观时须外观匹配，否则 `motion = static` 时须静止（< 0.5 m/s）；多条候选取先验密度最高者（同密度按轨迹号），全程只报一次，
  位置为轨迹拟合位置，窗口 [t_first, t]。

抑制：同类型 report 在 `common.SUPPRESS_S` 的时长内只报一次（`vehicle_event` 按事件类型分键），`track_share` 经"已报"消息让
他机同样抑制。每次 step 至多 4 条 report（与 P0a 规则共用上限）。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drone4d.baselines.common import BaselineAgent, Track

__all__ = ["init_state", "note_meet", "reports"]

S = 1_000_000_000
REACQ_GAP_NS = {"person": 5 * S, "vehicle": 3 * S}
LIVE_NS = 2 * S                   # 两条轨迹均在跟：末次检测在 2 s 内（同 common.LOST_S）
DISPERSE_M, DISPERSE_MAX_NS = 10.0, 120 * S
ALIGHT_M, SPLIT_NS, EMERGE_MIN_NS = 4.0, 3 * S, 3 * S
DOOR_M, WATCH_M, WATCH_NS = 4.0, 40.0, (6 * S, 1 * S)
V_MOVE, V_STOP, V_STILL = 2.0, 0.5, 1.0
STOP_MIN_NS, PARK_NS = 3 * S, 30 * S
TURN_DEG, TURN_WIN_NS, TURN_HOLD_NS = 45.0, 6 * S, 8 * S
FOUND_MIN_NS, FOUND_MIN_N = 2 * S, 5


def init_state(ag: BaselineAgent) -> None:
    ag.meet_log = {}            # 轨迹对 → [t0, t1, 已报 disperse]
    ag.emerged = set()          # 已做过出现判定（alight、exit_building）的人员轨迹
    ag.tgt_last = {}            # 目标序号 → (末次检测时刻, 轨迹号)
    ag.tgt_seen = set()         # 已匹配过目标的 (目标序号, 轨迹号)
    ag.gap_done = {}            # 轨迹号 → 已处理的观测间断数
    ag.vstate = {}              # 车辆轨迹号 → 行驶状态机
    ag.found_done = False


def note_meet(ag: BaselineAgent, key: tuple[str, str], t0: int, t_ns: int) -> None:
    m = ag.meet_log.setdefault(key, [t0, t_ns, False])
    m[1] = t_ns


def _track(ag: BaselineAgent, tid: str) -> Track | None:
    return next((tr for tr in ag.tracks.items if tr.tid == tid), None)


def _pick(ag: BaselineAgent, type_: str, *alts: str) -> str | None:
    return next((t for t in (type_, *alts) if t in ag.vocab), None)


def reports(ag: BaselineAgent, t_ns: int, out: list[dict]) -> None:
    if {"handover", "disperse"} & ag.vocab:
        _rendezvous(ag, t_ns, out)
    if {"alight", "exit_building"} & ag.vocab:
        _emerge(ag, t_ns, out)
    if {"reacquire", "identity_link"} & ag.vocab:
        _reacquire(ag, t_ns, out)
    if "vehicle_event" in ag.vocab:
        _vehicles(ag, t_ns, out)
    if "found" in ag.vocab and not ag.found_done:
        _found(ag, t_ns, out)


# ------------------------------------------------------------------ 交汇与交接
def _rendezvous(ag: BaselineAgent, t_ns: int, out: list[dict]) -> None:
    for key in sorted(ag.meet_log):
        t0, t1, done = ag.meet_log[key]
        if done:
            continue
        if t_ns - t1 > DISPERSE_MAX_NS:
            ag.meet_log[key][2] = True
            continue
        a, b = _track(ag, key[0]), _track(ag, key[1])
        if a is None or b is None or a.closed or b.closed or max(t_ns - a.t_last, t_ns - b.t_last) > LIVE_NS:
            continue
        if math.dist(a.xy, b.xy) < DISPERSE_M:
            continue
        ag.meet_log[key][2] = True
        mid = ((a.xy[0] + b.xy[0]) / 2, (a.xy[1] + b.xy[1]) / 2)
        if "handover" in ag.vocab and ("handover", *key) not in ag.reported and not ag.suppressed("handover", t_ns):
            ag.reported.add(("handover", *key))
            ag._emit(out, "handover", [a, b], t0, t1, mid, 0.3)
        if "disperse" in ag.vocab and ("disperse", *key) not in ag.reported and not ag.suppressed("disperse", t_ns):
            ag.reported.add(("disperse", *key))
            ag._emit(out, "disperse", [a, b], t_ns - S, t_ns, mid, 0.5)


# ------------------------------------------------------------------ 人员出现：下车、出建筑
def _split_near(ag: BaselineAgent, p: Track, xy, r: float) -> bool:
    """p 出现前 SPLIT_NS 内 r 米内有人员轨迹丢失（视为同一人的分裂轨迹）。"""
    return any(q is not p and q.cls == "person" and p.t_first - SPLIT_NS <= q.t_last < p.t_first and math.dist(q.xy, xy) <= r
               for q in ag.tracks.items)


def _emerge(ag: BaselineAgent, t_ns: int, out: list[dict]) -> None:
    for p in ag.tracks.items:
        if p.cls != "person" or p.closed or p.tid in ag.emerged or p.t_last - p.t_first < EMERGE_MIN_NS:
            continue
        ag.emerged.add(p.tid)
        if "alight" in ag.vocab:
            vs = [v for v in ag.tracks.items if v.cls == "vehicle" and math.dist(v.xy, p.xy0) <= ALIGHT_M and v.t_first <= p.t_first - 2 * S
                  and v.t_last >= p.t_first - S and math.hypot(*v.vel) < V_STILL]
            if vs and not _split_near(ag, p, p.xy0, ALIGHT_M):
                v = min(vs, key=lambda v: (math.dist(v.xy, p.xy0), v.tid))
                if ("alight", p.tid) not in ag.reported and not ag.suppressed("alight", t_ns):
                    ag.reported.add(("alight", p.tid))
                    ag._emit(out, "alight", [p, v], p.t_first - 2 * S, p.t_first, p.xy0, 0.5)
                continue
        if "exit_building" in ag.vocab:
            doors = [a for a in ag.anchors if a["kind"] == "door" and math.dist(a["pos_enu_m"][:2], p.xy0) <= DOOR_M]
            if not doors:
                continue
            door = doors[0]["pos_enu_m"][:2]
            veh = any(v.cls == "vehicle" and math.dist(v.xy, p.xy0) <= ALIGHT_M and v.t_last >= p.t_first - S for v in ag.tracks.items)
            watched = any(p.t_first - WATCH_NS[0] <= t <= p.t_first - WATCH_NS[1] and math.dist(xy, door) <= WATCH_M for t, xy in ag.looks)
            if veh or not watched or _split_near(ag, p, door, DOOR_M):
                continue
            if ("exit_building", p.tid) not in ag.reported and not ag.suppressed("exit_building", t_ns):
                ag.reported.add(("exit_building", p.tid))
                ag._emit(out, "exit_building", [p], p.t_first - S, p.t_first, p.xy0, 0.5)


# ------------------------------------------------------------------ 重新捕获
def _reacquire(ag: BaselineAgent, t_ns: int, out: list[dict]) -> None:
    hits: list[tuple[int, str, int, Track, Track | None]] = []          # (恢复时刻, 轨迹号, 目标序号, 恢复轨迹, 旧轨迹)
    for tr in ag.tracks.items:
        if tr.closed or t_ns - tr.t_last > S:
            continue
        i = ag.target_no(tr)
        if i is None:
            continue
        gap = REACQ_GAP_NS.get(tr.cls, 5 * S)
        k0 = ag.gap_done.get(tr.tid, 0)
        if (i, tr.tid) in ag.tgt_seen:
            for t_prev, t_res in tr.gaps[k0:]:
                if t_res - t_prev >= gap:
                    hits.append((t_res, tr.tid, i, tr, None))
        else:
            ag.tgt_seen.add((i, tr.tid))
            last = ag.tgt_last.get(i)
            if last is not None and last[1] != tr.tid and tr.t_first - last[0] >= gap:
                hits.append((tr.t_first, tr.tid, i, tr, _track(ag, last[1])))
        ag.gap_done[tr.tid] = len(tr.gaps)
        if ag.tgt_last.get(i, (-1, ""))[0] <= tr.t_last:
            ag.tgt_last[i] = (tr.t_last, tr.tid)
    for t_res, _tid, i, tr, old in sorted(hits, key=lambda h: (h[0], h[1])):
        ty = _pick(ag, "identity_link", "reacquire") if tr.cls == "vehicle" else _pick(ag, "reacquire", "identity_link")
        if ty is None or ("reacq", i, t_res) in ag.reported or ag.suppressed(ty, t_ns):
            continue
        ag.reported.add(("reacq", i, t_res))
        parts = [old, tr] if ty == "identity_link" and old is not None else [tr]
        ag._emit(out, ty, parts, t_res, t_res, tr.xy, 0.5)


# ------------------------------------------------------------------ 车辆关键事件
def _wrap_deg(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def _vehicles(ag: BaselineAgent, t_ns: int, out: list[dict]) -> None:
    targeted = any(c == "vehicle" and rgb is not None for c, rgb in zip(ag.target_classes, ag.targets, strict=True))
    for v in ag.tracks.items:
        if v.cls != "vehicle" or v.closed or v.t_last != t_ns or v.span_s() < 2.0:
            continue
        st = ag.vstate.setdefault(v.tid, {"state": None, "since": v.t_last, "moved": False, "parked": False, "heads": [], "turn_ok": 0})
        spd = math.hypot(*v.vel)
        new = "moving" if spd > V_MOVE else "still" if spd < V_STOP else st["state"]
        evs: list[tuple[str, int, int]] = []
        if new == "moving":
            if st["state"] == "still":
                dur = t_ns - st["since"]
                if st["moved"] and not st["parked"] and dur >= STOP_MIN_NS:
                    evs.append(("stop", st["since"], t_ns))
                elif (not st["moved"] or st["parked"]) and dur >= STOP_MIN_NS:
                    evs.append(("depart", t_ns - 2 * S, t_ns))
                st["heads"] = []
            if st["state"] != "moving":
                st["since"], st["moved"], st["parked"] = t_ns, True, False
            h = math.degrees(math.atan2(v.vel[1], v.vel[0]))
            st["heads"] = [x for x in st["heads"] if t_ns - x[0] <= TURN_WIN_NS] + [(t_ns, h)]
            t_old, h_old = st["heads"][0]
            if t_ns - t_old >= TURN_WIN_NS - 100_000_000 and t_ns >= st["turn_ok"] and abs(_wrap_deg(h - h_old)) >= TURN_DEG:
                evs.append(("turn", t_old, t_ns))
                st["turn_ok"], st["heads"] = t_ns + TURN_HOLD_NS, []
        elif new == "still":
            if st["state"] != "still":
                st["since"] = t_ns
            elif st["moved"] and not st["parked"] and t_ns - st["since"] >= PARK_NS:
                evs.append(("park", st["since"], t_ns))
                st["parked"] = True
        st["state"] = new
        if targeted and ag.target_no(v) is None:
            continue
        for ev, w0, w1 in evs:
            key = f"vehicle_event:{ev}"
            if ("vehicle_event", v.tid, ev, w1) in ag.reported or ag.suppressed(key, t_ns):
                continue
            ag.reported.add(("vehicle_event", v.tid, ev, w1))
            ag._emit(out, "vehicle_event", [v], w0, w1, v.xy, 0.5, event=ev)


# ------------------------------------------------------------------ 目标搜索
def _found(ag: BaselineAgent, t_ns: int, out: list[dict]) -> None:
    pri = ag.prior
    cls = pri.target_class if pri is not None else (ag.target_classes[0] if ag.target_classes else "person")
    known = [i for i, c in enumerate(ag.target_classes) if c == cls and ag.targets[i] is not None]
    cands = []
    for tr in ag.tracks.live(t_ns, 1.0):
        if tr.cls != cls or tr.t_last - tr.t_first < FOUND_MIN_NS or tr.n < FOUND_MIN_N:
            continue
        if known:
            if ag.target_no(tr) not in known:
                continue
        elif pri is not None and pri.motion == "static" and math.hypot(*tr.vel) >= V_STOP:
            continue
        dens = pri.density(*tr.xy) if pri is not None else 0.0
        cands.append((-round(dens, 15), tr.tid, tr))
    if not cands or ag.suppressed("found", t_ns):
        return
    tr = min(cands, key=lambda c: (c[0], c[1]))[2]
    ag.found_done = True
    ag.reported.add(("found", tr.tid))
    ag._emit(out, "found", [tr], tr.t_first, t_ns, tr.xy, 0.6 if known else 0.3)
