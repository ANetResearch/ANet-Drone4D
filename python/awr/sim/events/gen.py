"""门 2 与磁带生成（M23 §6.5、§6.6、§6.11）：吸附、几何预检与排时建议、运动学脚本演员、背景行走者、贴地、定点化与规范头。

P0a 过渡实现（Python）：路径来自 `nav.NavGrid`（栅格 A*，同一路径函数供预检与生成，`len_m` 逐位相同）；背景演员按 §6.6.4 的
目标点抽样、速度与停留抽样（流 19）沿路径匀速行走，暂不做 DetourCrowd 互避（最小间距写入报告供 M27 评测）。脚本演员的轨迹
只由 plan 决定，与背景人数无关。全部时刻为整数毫秒，帧时刻 t_k = 100·k ms。

到达（§6.9.4 `S-ARRIVAL`）：`arrivals` 由实现的时间线逐路点给出实际到达时刻与定点化后的到达位置误差，清单 `report.arrivals`
取自同一结果（不再照抄计划时刻）。真值骨架（§6.10.1）：`dsl_sha256` 覆盖规范化后的完整 DSL，`build_truth` 组装
`event_truth.json`，`truth_sha256` 为去掉 `perf` 后的 JCS 哈希；门 3 的检测器、声明核对与关系核对接入前，`events`、
`annotation_checks`、`relation_checks` 由调用方给出（缺省为空）。

车道图车辆（M23 §15.2、§15.4，D3-AC-33，E09）：给出 `drive` 的车辆演员与世界有车道图时的背景车辆按 `vehicle.simulate` 在 M22
车道图上同一次推进（IDM 跟驰、弯道限速、路口让行），不再按运动学排时；规则见 `lane_traffic`。其轨迹、到达与停靠窗口取自
生成结果，门 2 的上车检查、门 3 的 `S-OFFMESH`（车道口径）与 `S-ARRIVAL`（车道口径）读取同一结果；没有车道图车辆的事件
磁带与此前逐字节相同。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from awr.sim.events.anchors import anchor_xy, point_in_polygon
from awr.sim.events.dsl import CARRY_PICKUP, Issue, carry_schedule
from awr.sim.events.jcs import jcs
from awr.sim.events.nav import NavGrid, Snap, rounded_length
from awr.sim.events.tape import NO_CONTAINER, RECORD, fold_yaw, record_spec
from awr.sim.sensors import cbrng

__all__ = ["CARRY_DIST_M", "GRAPH_SCHEMA", "LANE_SNAP_MAX_M", "LANE_SNAP_WARN_M", "STREAM", "TRUTH_SCHEMA", "LaneTraffic", "Stage", "arrivals",
           "build_graph", "build_header", "build_truth", "compile_env_track", "dsl_sha256", "generate_frames", "lane_driven", "lane_traffic",
           "load_lane_graph", "navgraph_ref", "preflight", "t_min", "truth_sha256", "v_peak"]

STREAM = 19
TICK_MS = 100
BOARD_DIST_M = 8.0
CARRY_DIST_M = 2.0                              # 拾取点到物体位置（交接时到交出者放下点）的距离上限（M23 §6.9.1 object_handover）
ZONE_SLOT_R = 0.8
INF_MS = 1 << 62
TRUTH_SCHEMA = "drone4d.event_truth.v1"
GRAPH_SCHEMA = "drone4d.event_graph.v1"
for _n, _r in (("bg_goal", (0, 1)), ("bg_speed", (2, 2)), ("bg_pause", (3, 3)), ("bg_look", (4, 7)), ("bg_phase", (8, 8)),
               ("interactive", (16, 19)), ("local_dev", (20, 23))):
    cbrng.CHANNELS.setdefault((STREAM, _n), _r)


def t_min(L: float, a: float, v: float) -> float:
    return L / v + v / a if v * v / a <= L else 2.0 * math.sqrt(L / a)


def v_peak(L: float, T: float, a: float) -> float:
    if L <= 0:
        return 0.0
    if T <= 0 or a * T * T < 4.0 * L:
        return math.inf
    return (a * T - math.sqrt(a * a * T * T - 4.0 * a * L)) / 2.0


def _ceil_tick_s(t: float) -> float:
    return math.ceil(round(t * 10.0, 9)) / 10.0


def _issue(code: str, reason: int | None, path: str, zh: str, en: str, **d) -> Issue:
    return Issue(code, reason, path, zh, en, d)


@dataclass
class Stage:
    """事件舞台：各 profile 的可走面、贴地索引、解析后的锚点与（惰性装载的）M22 车道图。"""
    nav: dict[str, NavGrid]
    ground: object                              # GroundIndex
    anchors: dict[str, dict]
    snaps: dict[tuple[str, str], Snap] = field(default_factory=dict)
    world_dir: Path | None = None               # 车道图 nav.graph 的来源（`lanes` 未给出时按需装载）
    lanes: Any = None                           # LaneGraph；None 且无 world_dir 时车辆只能按运动学排时
    cache: dict = field(default_factory=dict)   # 车道图网络与车流结果（`lane_traffic`），同一舞台内复用

    def snap(self, anchor_id: str, profile: str, xy: tuple[float, float] | None = None, key: str | None = None) -> Snap:
        k = (key or anchor_id, profile)
        if k not in self.snaps:
            p = xy or anchor_xy(self.anchors[anchor_id])
            self.snaps[k] = self.nav[profile].snap(anchor_id, p[0], p[1])
        return self.snaps[k]

    def lane_graph(self):
        """世界包的车道图（`geojson/awr-navgraph@1`，M22 §6.6.11）；世界无该图层时为 None。"""
        if self.lanes is None and self.world_dir is not None and "lanes_loaded" not in self.cache:
            self.cache["lanes_loaded"] = True
            self.lanes = load_lane_graph(self.world_dir)
        return self.lanes

    def lane_net(self):
        """车道图上与车辆无关的预计算（`vehicle.Network`，crop 约 7 s），同一舞台只构建一次。"""
        if "lane_net" not in self.cache:
            from awr.sim.events.vehicle import Network

            self.cache["lane_net"] = Network(self.lane_graph())
        return self.cache["lane_net"]

    def actor_nav(self, plan: dict, actor_id: str, profile: str | None):
        """门 3 `S-OFFMESH` 的可走面：车道图车辆为其车道路线（到车道中心线的距离减车道半宽，M23 §6.9.4 车辆口径），其余为
        profile 的可走面（无 profile 为 None）。"""
        lt = lane_traffic(plan, self) if any(a["id"] == actor_id and lane_driven(a) for a in plan["actors"]) else None
        if lt is not None and lt.sim is not None and actor_id in lt.index:
            return lt.route_nav(self.lane_graph(), actor_id)
        return self.nav.get(profile) if profile else None


# ---------------------------------------------------------------------- 演员次序与站位
def actor_order(plan: dict) -> list[dict]:
    """§6.6.1：车辆在前（按 id），其余按 id。"""
    acts = plan["actors"]
    return sorted([a for a in acts if a["class"] == "vehicle"], key=lambda a: a["id"]) + \
        sorted([a for a in acts if a["class"] != "vehicle"], key=lambda a: a["id"])


def _slots(plan: dict, anchors: dict) -> dict[tuple[str, str], tuple[float, float]]:
    """同一 zone 锚点被 m ≥ 2 名非车辆脚本演员使用时，第 k 名（按 id）取半径 0.8 m 圆周上 2πk/m 处。"""
    users: dict[str, list[str]] = {}
    for a in plan["actors"]:
        if a["class"] == "vehicle":
            continue
        for w in a["route"]:
            an = anchors.get(w["at"])
            if an and an.get("kind") == "zone" and a["id"] not in users.setdefault(w["at"], []):
                users[w["at"]].append(a["id"])
    out = {}
    for zid, ids in users.items():
        if len(ids) < 2:
            continue
        cx, cy = anchor_xy(anchors[zid])
        for k, aid in enumerate(sorted(ids)):
            th = 2.0 * math.pi * k / len(ids)
            out[(aid, zid)] = (cx + ZONE_SLOT_R * math.cos(th), cy + ZONE_SLOT_R * math.sin(th))
    return out


def _gait(a: dict) -> tuple[float, float, float, float, float]:
    """(lo, hi_可行, hi, a, v_med)：车辆可行上限 0.7 × hi、建议中值 0.6 × hi。"""
    lo, hi = a["gait"]["speed_mps"]
    acc = a["gait"]["accel_mps2"]
    if a["class"] == "vehicle":
        return lo, 0.7 * hi, hi, acc, 0.6 * hi
    return lo, hi, hi, acc, a["gait"]["median_mps"]


# ---------------------------------------------------------------------- 门 2
@dataclass
class Leg:
    actor: str
    index: int
    from_anchor: str
    to_anchor: str
    path: np.ndarray | None
    len_m: float
    avail_s: float
    need_mps: float
    peak_mps: float
    feasible: bool
    why: str | None

    def report(self) -> dict:
        def f(x: float) -> float | None:
            return None if not math.isfinite(x) else round(x, 6)

        return {"actor": self.actor, "index": self.index, "from_anchor": self.from_anchor, "to_anchor": self.to_anchor,
                "len_m": f(self.len_m), "avail_s": f(self.avail_s), "need_mps": f(self.need_mps), "peak_mps": f(self.peak_mps),
                "feasible": self.feasible, "why": self.why}


def _no_path_leg(w: dict) -> bool:
    """进入建筑到走出、上车到下车之间不行进。"""
    return w["do"] in ("enter_building", "board")


def _points(stage: Stage, a: dict, slots) -> tuple[list[Snap], list[list[Snap]]]:
    prof = a["profile"] or "person"                               # 静止类别按行人可走面吸附
    pts, vias = [], []
    for w in a["route"]:
        sxy = slots.get((a["id"], w["at"]))
        pts.append(stage.snap(w["at"], prof, sxy, key=f"{w['at']}#{a['id']}" if sxy else None))
        vias.append([stage.snap(v, prof) for v in w["via"]])
    return pts, vias


def preflight(plan: dict, stage: Stage, *, mode: str = "minimal") -> dict:
    """门 2：返回 {errors, warnings, legs (actor → [Leg]), snaps, retime, vehicle_dwell_ext_s, boards, windows, lanes}。车道图车辆
    （`lane_driven`）的路点、路段、排时建议与停靠窗口取自 `lane_traffic`（`lanes`），其余按运动学排时。"""
    errs: list[Issue] = []
    warns: list[Issue] = []
    slots = _slots(plan, stage.anchors)
    order = actor_order(plan)
    idx_of = {a["id"]: i for i, a in enumerate(plan["actors"])}
    legs: dict[str, list[Leg]] = {}
    retime: dict[str, list[float]] = {}
    windows: dict[str, list[tuple[float, float, Snap]]] = {}
    pts_of: dict[str, list[Snap]] = {}
    lt = lane_traffic(plan, stage)
    if lt is not None:
        errs += lt.errors
        warns += lt.warnings
    for a in order:
        ai = idx_of[a["id"]]
        if lane_driven(a):                                        # 车道图车辆（E09）
            pts_of[a["id"]] = lt.wp_snap.get(a["id"], []) if lt is not None else []
            if lt is not None and lt.sim is not None:
                legs[a["id"]], retime[a["id"]], windows[a["id"]] = lt.legs[a["id"]], lt.retime[a["id"]], lt.windows[a["id"]]
            continue
        if a["profile"] is None:                                  # 静止类别：只需吸附出生点
            pts, _ = _points(stage, a, slots) if a["route"] else ([], [])
            pts_of[a["id"]] = pts
            for j, s in enumerate(pts):
                _snap_issue(s, f"/actors/{ai}/route/{j}/at", errs, warns)
            continue
        pts, vias = _points(stage, a, slots)
        pts_of[a["id"]] = pts
        for j, s in enumerate(pts):
            _snap_issue(s, f"/actors/{ai}/route/{j}/at", errs, warns)
            for m, v in enumerate(vias[j]):
                _snap_issue(v, f"/actors/{ai}/route/{j}/via/{m}", errs, warns)
        lo, vhi, _hi, acc, vmed = _gait(a)
        r = a["route"]
        out: list[Leg] = []
        sug = [r[0]["t_ms"] / 1000.0]
        t_arr = r[0]["t_ms"] / 1000.0
        win: list[tuple[float, float, Snap]] = []
        for i in range(len(r) - 1):
            w0, w1 = r[i], r[i + 1]
            t_dep = max(t_arr, w0["t_ms"] / 1000.0) + w0["dwell_ms"] / 1000.0
            if w0["dwell_ms"] > 0 or w0["do"] == "wait":
                win.append((t_arr, t_dep, pts[i]))
            if _no_path_leg(w0):
                t_arr = w1["t_ms"] / 1000.0
                sug.append(t_arr)
                continue
            ok_ends = all(s.status != "error" for s in (pts[i], pts[i + 1], *vias[i + 1]))
            path = stage.nav[a["profile"]].route([pts[i].p, *[v.p for v in vias[i + 1]], pts[i + 1].p]) if ok_ends else None
            T = w1["t_ms"] / 1000.0 - t_dep
            if path is None:
                out.append(Leg(a["id"], i, w0["at"], w1["at"], None, math.inf, T, math.inf, math.inf, False, "disconnected"))
                if ok_ends:
                    errs.append(_issue("I-DISCONNECTED", 612, f"/actors/{ai}/route/{i + 1}", f"{w0['at']} 与 {w1['at']} 之间无可走路径",
                                       f"No walkable path between {w0['at']} and {w1['at']}", actor=a["id"], index=i))
                t_arr = w1["t_ms"] / 1000.0
                sug.append(t_arr)
                continue
            L = rounded_length(path)
            v = v_peak(L, T, acc)
            feas, why = True, None
            if v > vhi:
                feas, why = False, "speed"
                errs.append(_issue("I-SPEED", 612, f"/actors/{ai}/route/{i + 1}/t", f"{a['id']} 第 {i} 段需要峰值 {v:.2f} m/s，超过上限 {vhi:.2f}",
                                   f"{a['id']} leg {i} needs a peak of {v:.2f} m/s, above the limit {vhi:.2f}", actor=a["id"], index=i,
                                   peak_mps=None if math.isinf(v) else v, limit_mps=vhi))
            elif v < lo:
                why = "slow"
                warns.append(_issue("W-SLOW", None, f"/actors/{ai}/route/{i + 1}/t", f"{a['id']} 第 {i} 段所需速度低于步态下限，出发将延后",
                                    f"{a['id']} leg {i} is slower than the gait minimum; departure is delayed", actor=a["id"], index=i))
            out.append(Leg(a["id"], i, w0["at"], w1["at"], path, L, T, L / T if T > 0 else math.inf, v, feas, why))
            if mode == "minimal":
                td2 = max(sug[i], w0["t_ms"] / 1000.0) + w0["dwell_ms"] / 1000.0
                t2 = max(w1["t_ms"] / 1000.0, td2 + t_min(L, acc, vhi))
            else:
                td2 = sug[i] + w0["dwell_ms"] / 1000.0
                t2 = td2 + t_min(L, acc, vmed)
            sug.append(_ceil_tick_s(t2))
            t_arr = w1["t_ms"] / 1000.0 if feas else t_dep + t_min(L, acc, vhi)
        last = r[-1]
        if last["do"] in ("pass", "wait"):
            win.append((t_arr, math.inf, pts[-1]))
        elif last["dwell_ms"] > 0:
            win.append((t_arr, t_arr + last["dwell_ms"] / 1000.0, pts[-1]))
        legs[a["id"]] = out
        retime[a["id"]] = sug
        windows[a["id"]] = win
    # 上下车（§6.5 preflight_event）
    boards, ext = [], {}
    for a in order:
        ai = idx_of[a["id"]]
        for j, w in enumerate(a["route"]):
            if w["do"] not in ("board", "alight"):
                continue
            tgt = w["target"]
            t_p = w["t_ms"] / 1000.0
            p = pts_of[a["id"]][j]
            near = [(t0, t1) for t0, t1, s in windows.get(tgt, []) if math.dist(s.p[:2], p.p[:2]) < BOARD_DIST_M]
            fit = [(t0, t1) for t0, t1 in near if (t1 >= t_p if w["do"] == "board" else t0 <= t_p <= t1)]
            tb, wwin, ext_s = None, None, None
            if fit:
                t0, t1 = fit[0]
                tb, wwin = max(t_p, t0), [t0, None if math.isinf(t1) else t1]
            else:
                before = [(t0, t1) for t0, t1 in near if t1 < t_p]
                if before:                                        # 建议①：车辆停留延长 Δ = t_b − W.end + 5 s
                    t0, t1 = before[-1]
                    wwin, ext_s = [t0, t1], round(t_p - t1 + 5.0, 1)
                    ext[tgt] = max(ext.get(tgt, 0.0), ext_s)
            boards.append({"actor": a["id"], "waypoint": j, "vehicle": tgt, "kind": w["do"], "t_board_s": tb, "window_s": wwin,
                           "feasible": bool(fit), **({"dwell_ext_s": ext_s} if ext_s is not None else {})})
            if not fit:
                errs.append(_issue("I-BOARD", 612, f"/actors/{ai}/route/{j}", f"{a['id']} 在 {w['at']} 的{'上' if w['do'] == 'board' else '下'}车不在 {tgt} 的等待窗口内或距离 ≥ 8 m",
                                   f"{a['id']} {w['do']} at {w['at']} is outside the waiting window of {tgt} or ≥ 8 m away",
                                   actor=a["id"], vehicle=tgt, dwell_ext_s=ext_s))
    errs.extend(_carry_preflight(plan, pts_of, idx_of))
    return {"errors": errs, "warnings": warns, "legs": legs, "retime": {"mode": mode, "actors": retime, "vehicle_dwell_ext_s": ext},
            "boards": boards, "windows": windows, "points": pts_of, "lanes": lt,
            "snaps": [{"anchor": s.anchor, "profile": k[1], "dist_m": round(s.dist_m, 3) if math.isfinite(s.dist_m) else None,
                       "status": s.status} for k, s in sorted(stage.snaps.items())]}


def _snap_issue(s: Snap, path: str, errs: list, warns: list) -> None:
    if s.status == "error":
        errs.append(_issue("E-ANCHOR-UNSNAPPED", 611, path, f"锚点 {s.anchor} 无法吸附到可走面（{s.why}）",
                           f"Anchor {s.anchor} cannot be snapped to the walkable surface ({s.why})", anchor=s.anchor, why=s.why))
    elif s.status == "warn" and not any(w.detail.get("anchor") == s.anchor for w in warns if w.code == "W-SNAP"):
        warns.append(_issue("W-SNAP", None, path, f"锚点 {s.anchor} 吸附距离 {s.dist_m:.1f} m", f"Anchor {s.anchor} snapped {s.dist_m:.1f} m away",
                            anchor=s.anchor, dist_m=s.dist_m))


def _carry_preflight(plan: dict, pts_of: dict[str, list[Snap]], idx_of: dict[str, int]) -> list[Issue]:
    """门 2 携带可行性（M23-FR-058，P0b）：拾取路点的吸附点到物体当前位置（出生点，或上一次放下者的放下路点吸附点）的水平距离
    < `CARRY_DIST_M`，否则 `I-CARRY`（612）。门 1 已保证携带次序；实际磁带上的距离由门 3（`verify.rendezvous`）复核。"""
    errs = []
    for oid, steps in carry_schedule(plan["actors"]).items():
        if not pts_of.get(oid):
            continue
        at = pts_of[oid][0].p
        for s in steps:
            p = pts_of[s.actor][s.waypoint].p
            if s.kind != CARRY_PICKUP:
                at = p
                continue
            d = math.dist(p[:2], at[:2])
            if not d < CARRY_DIST_M:
                errs.append(_issue("I-CARRY", 612, f"/actors/{idx_of[s.actor]}/route/{s.waypoint}",
                                   f"{s.actor} 的拾取点距物体 {oid} {d:.2f} m（须 < {CARRY_DIST_M:g} m）",
                                   f"Pickup point of {s.actor} is {d:.2f} m from object {oid} (must be < {CARRY_DIST_M:g} m)",
                                   actor=s.actor, object=oid, dist_m=round(d, 3)))
    return errs


# ---------------------------------------------------------------------- 时间线
@dataclass
class Phase:
    kind: str                     # absent、wait、move、inside、vehicle、gone
    t0: int                       # ms，含
    t1: int                       # ms，不含
    p: tuple[float, float, float] | None = None
    path: np.ndarray | None = None
    cum: np.ndarray | None = None
    L: float = 0.0
    v: float = 0.0
    a: float | None = None        # None：匀速（背景）
    container: int = -1
    wp: int = -1                  # 移动阶段的目标路点下标（脚本演员）；其余为 −1


def _move(t0_ms: int, T_s: float, path: np.ndarray, v: float, a: float | None) -> Phase:
    d = np.diff(path, axis=0)
    cum = np.concatenate([[0.0], np.cumsum(np.hypot(d[:, 0], d[:, 1]))])
    t1 = t0_ms + math.floor(T_s * 1000.0 + 0.5)
    return Phase("move", t0_ms, t1, path=path, cum=cum, L=float(cum[-1]), v=v, a=a)


def scripted_timeline(a: dict, legs: list[Leg], pts: list[Snap], board_t: dict[int, float], idx_of_vehicle: dict[str, int]) -> list[Phase]:
    """§6.6.2、§6.6.3：返回覆盖 (−∞, +∞) 的阶段序列。"""
    r = a["route"]
    p0 = pts[0].p
    ph = [Phase("absent", -INF_MS, r[0]["t_ms"], p=p0)]
    if a["profile"] is None:                                     # 静止类别
        end = next((w["t_ms"] for w in r[1:] if w["do"] == "despawn"), INF_MS)
        ph.append(Phase("wait", r[0]["t_ms"], end, p=p0))
        if end < INF_MS:
            ph.append(Phase("gone", end, INF_MS, p=p0))
        return ph
    lo, _vhi, _hi, acc, vmed = _gait(a)
    leg_of = {lg.index: lg for lg in legs}
    t = r[0]["t_ms"]
    i = 0
    while i < len(r):
        w, p = r[i], pts[i].p
        if w["do"] == "despawn":
            ph.append(Phase("gone", t, INF_MS, p=p))
            return ph
        if w["do"] == "enter_building":
            t1 = r[i + 1]["t_ms"] if i + 1 < len(r) else INF_MS
            ph.append(Phase("inside", t, t1, p=p))
            if t1 == INF_MS:
                return ph
            t, i = t1, i + 1
            continue
        if w["do"] == "board":
            tb = math.floor(board_t.get(i, t / 1000.0) * 1000.0 + 0.5)
            tb = (tb + TICK_MS - 1) // TICK_MS * TICK_MS if tb % TICK_MS else tb
            if tb > t:
                ph.append(Phase("wait", t, tb, p=p))
            t1 = r[i + 1]["t_ms"] if i + 1 < len(r) else INF_MS
            ph.append(Phase("vehicle", tb, t1, p=p, container=idx_of_vehicle[w["target"]]))
            if t1 == INF_MS:
                return ph
            t, i = t1, i + 1
            continue
        if i == len(r) - 1:
            ph.append(Phase("wait", t, INF_MS, p=p))
            return ph
        lg = leg_of[i]
        td = max(t, w["t_ms"]) + w["dwell_ms"]
        T = (r[i + 1]["t_ms"] - td) / 1000.0
        v = v_peak(lg.len_m, T, acc)
        if v < lo and lg.len_m > 0:
            T = t_min(lg.len_m, acc, vmed)
            td = r[i + 1]["t_ms"] - math.floor(T * 1000.0 + 0.5)
            v = v_peak(lg.len_m, T, acc)
        if td > t:
            ph.append(Phase("wait", t, td, p=p))
        mv = _move(td, (r[i + 1]["t_ms"] - td) / 1000.0, lg.path, v, acc)
        mv.p = pts[i + 1].p
        mv.wp = i + 1
        ph.append(mv)
        t, i = mv.t1, i + 1
    return ph


def _eval_move(ph: Phase, t_ms: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (xy (n, 2), vxy (n, 2))。"""
    T = (ph.t1 - ph.t0) / 1000.0
    tau = (t_ms - ph.t0) / 1000.0
    if ph.a is None or ph.L == 0:
        d = np.minimum(ph.v * tau, ph.L)
        sp = np.where(d < ph.L, ph.v, 0.0)
    else:
        a, v = ph.a, ph.v
        ta = min(v / a, T / 2.0) if a > 0 else 0.0
        d = np.where(tau < ta, 0.5 * a * tau * tau,
                     np.where(tau <= T - ta, 0.5 * a * ta * ta + v * (tau - ta), ph.L - 0.5 * a * (T - tau) ** 2))
        sp = np.where(tau < ta, a * tau, np.where(tau <= T - ta, v, a * (T - tau)))
        d = np.clip(d, 0.0, ph.L)
    j = np.clip(np.searchsorted(ph.cum, d, side="right") - 1, 0, len(ph.path) - 2)
    seg = ph.path[j + 1] - ph.path[j]
    sl = np.hypot(seg[:, 0], seg[:, 1])
    u = np.where(sl > 0, (d - ph.cum[j]) / np.where(sl > 0, sl, 1.0), 0.0)
    xy = ph.path[j] + seg * u[:, None]
    dirn = seg / np.where(sl > 0, sl, 1.0)[:, None]
    return xy, dirn * sp[:, None]


def eval_timeline(ph: list[Phase], n_ticks: int, z_nav) -> dict[str, np.ndarray]:
    """在全部帧时刻求值：xy、z_hint、vxy、state、container（−1 无）。"""
    t = np.arange(n_ticks, dtype=np.int64) * TICK_MS
    xy = np.zeros((n_ticks, 2))
    vxy = np.zeros((n_ticks, 2))
    st = np.zeros(n_ticks, np.uint8)
    ct = np.full(n_ticks, -1, np.int64)
    code = {"absent": 0, "wait": 2, "move": 1, "vehicle": 3, "inside": 4, "gone": 5}
    for p in ph:
        m = (t >= p.t0) & (t < p.t1)
        if not m.any():
            continue
        st[m] = code[p.kind]
        if p.kind == "move":
            xy[m], vxy[m] = _eval_move(p, t[m])
        else:
            xy[m] = p.p[:2]
        if p.kind == "vehicle":
            ct[m] = p.container
    zh = z_nav(xy)                                           # z_hint：高度场（可走面）高度
    return {"xy": xy, "vxy": vxy, "state": st, "container": ct, "z_hint": zh}


def _scripted_tracks(pf: dict, order: list[dict]) -> list[list[Phase]]:
    """脚本演员的运动学时间线；车道图车辆的轨迹取自 `lane_traffic`，此处为空表。"""
    vidx = {a["id"]: i for i, a in enumerate(order) if a["class"] == "vehicle"}
    out = []
    for a in order:
        if lane_driven(a):
            out.append([])
            continue
        bt = {b["waypoint"]: b["t_board_s"] for b in pf["boards"] if b["actor"] == a["id"] and b["kind"] == "board" and b["t_board_s"] is not None}
        out.append(scripted_timeline(a, pf["legs"].get(a["id"], []), pf["points"][a["id"]], bt, vidx))
    return out


def _arrivals(order: list[dict], pf: dict, tracks: list[list[Phase]]) -> list[dict]:
    """逐路点的实际到达：移动段终点取该段在 t1 的运动学求值（毫米定点化后与计划点比较）；首路点、出建筑与下车点为
    出现于吸附点（位置即计划点）。返回 [{actor, waypoint, at, do, t_plan_ms, t_arrive_ms, late_ms, xy_plan_m, xy_mm, err_mm}]。"""
    out = []
    lt = pf.get("lanes")
    for a, ph in zip(order, tracks, strict=True):
        if lt is not None and a["id"] in lt.arrivals:              # 车道图车辆：车道口径的到达行（`lane_traffic` 第 4 条）
            out += lt.arrivals[a["id"]]
            continue
        pts = pf["points"][a["id"]]
        mv = {p.wp: p for p in ph if p.kind == "move" and p.wp >= 0}
        for j, w in enumerate(a["route"]):
            p = pts[j].p
            if j in mv:
                m = mv[j]
                t_arr = m.t1
                xy, _v = _eval_move(m, np.array([m.t1], np.int64))
                xy = xy[0]
            else:
                t_arr, xy = w["t_ms"], np.asarray(p[:2], np.float64)
            q = _fix(xy, 1000.0, np.int64)
            err = math.hypot(float(q[0]) - p[0] * 1000.0, float(q[1]) - p[1] * 1000.0)
            out.append({"actor": a["id"], "waypoint": j, "at": w["at"], "do": w["do"], "t_plan_ms": w["t_ms"], "t_arrive_ms": int(t_arr),
                        "late_ms": int(t_arr) - w["t_ms"], "xy_plan_m": [float(p[0]), float(p[1])], "xy_mm": [int(q[0]), int(q[1])],
                        "err_mm": err})
    return out


def arrivals(plan: dict, pf: dict) -> list[dict]:
    """§6.9.4 `S-ARRIVAL` 的生成侧量：按 `actor_order` 重建脚本演员时间线并逐路点求实际到达（与 `generate_frames` 同一时间线，
    与背景无关）。运动学排时下 `late_ms` 恒为 0、`err_mm` 只含毫米定点化误差（≤ √2/2 mm）。"""
    order = actor_order(plan)
    return _arrivals(order, pf, _scripted_tracks(pf, order))


# ---------------------------------------------------------------------- 背景
def _u(seed: int, idx: int, g: int, ch: int) -> float:
    return float(cbrng.uniform_elem(seed, STREAM, idx, g, ch))


def _k(seed: int, idx: int, g: int, ch: int) -> int:
    return int(cbrng.key(seed, STREAM, idx, g, ch))


def zone_cells(nav: NavGrid, zone, anchors: dict) -> np.ndarray:
    P = np.asarray(anchors[zone]["polygon"] if isinstance(zone, str) else zone, np.float64)
    cells = np.flatnonzero(nav.walk.ravel())
    iy, ix = np.divmod(cells, nav.nx)
    cx, cy = nav.x0 + ix + 0.5, nav.y0 + iy + 0.5
    return cells[point_in_polygon(P, cx, cy)]


def _sample(nav: NavGrid, cells: np.ndarray, seed: int, idx: int, g: int) -> tuple[float, float]:
    c = cells[min(int(_u(seed, idx, g, 0) * len(cells)), len(cells) - 1)]
    k = _k(seed, idx, g, 1)
    fx, fy = (k >> 38) / float(1 << 26), ((k >> 12) & ((1 << 26) - 1)) / float(1 << 26)
    x, y = nav.center(int(c))
    return x + (fx - 0.5) * 0.8, y + (fy - 0.5) * 0.8


def background_timeline(g: dict, idx: int, seed: int, nav: NavGrid, cells: np.ndarray, duration_ms: int) -> list[Phase]:
    """§6.6.4：目标点 g = 0 为出生点；每个目标点抽一次速度（通道 2）与停留（通道 3）；出生时刻相位 [0, 2] s（通道 8）。"""
    t_sp = math.floor(_u(seed, idx, 0, 8) * 20.0) * TICK_MS
    p = _sample(nav, cells, seed, idx, 0)
    zp = (p[0], p[1], nav.z_at(*p))
    ph = [Phase("absent", -INF_MS, t_sp, p=zp)]
    t, n, tries = t_sp, 1, 0
    lo, hi = g["speed_mps"]
    plo, phi = g["pause_ms"]
    while t < duration_ms and tries < 64:
        q = _sample(nav, cells, seed, idx, n)
        path = nav.path(p, q)
        if path is None or rounded_length(path) < 1.0:
            n += 1
            tries += 1
            continue
        v = lo + _u(seed, idx, n, 2) * (hi - lo)
        L = rounded_length(path)
        mv = _move(t, L / v, path, v, None)
        mv.p = (q[0], q[1], nav.z_at(*q))
        ph.append(mv)
        pause = math.floor((plo + _u(seed, idx, n, 3) * (phi - plo)) + 0.5)
        ph.append(Phase("wait", mv.t1, mv.t1 + pause, p=mv.p))
        t, p, n, tries = mv.t1 + pause, q, n + 1, 0
    ph.append(Phase("wait", t, INF_MS, p=(p[0], p[1], nav.z_at(*p))))
    return ph


def background_look(seed: int, idx: int, frac: float, scripted_rgb: list[list[int]], cls: str) -> dict:
    """通道 4–7：资产、着色（同一 64 位键分三段取 RGB）、身高、干扰者。"""
    k = _k(seed, idx, 0, 5)
    rgb = [(k >> 43) & 0xFF, (k >> 22) & 0xFF, (k >> 1) & 0xFF]
    if scripted_rgb and _u(seed, idx, 0, 7) < frac:
        base = scripted_rgb[min(int(_u(seed, idx, 0, 4) * len(scripted_rgb)), len(scripted_rgb) - 1)]
        rgb = [int(min(255, max(0, c + (((k >> (8 * j)) & 0x3F) - 20)))) for j, c in enumerate(base)]
    out = {"asset": f"{cls}-proxy@1", "tint_rgb": [int(c) for c in rgb]}
    if cls == "person":
        out["height_mm"] = math.floor((1.55 + 0.4 * _u(seed, idx, 0, 6)) * 1000.0 + 0.5)
    return out


# ---------------------------------------------------------------------- 车道图车辆（M23 §15.2、§15.4，E09）
LANE_SNAP_WARN_M = 3.0                          # 锚点到最近车道中心线 > 3 m 告警 W-SNAP
LANE_SNAP_MAX_M = 15.0                          # > 15 m 为 I-LANES（612，why = unsnapped）
LANE_WIDTH_DEFAULT_M = 3.2                      # M22 缺省车道宽（LANE_WIDTH_M）


def lane_driven(a: dict) -> bool:
    """脚本车辆是否按车道图驾驶：类别 `vehicle` 且给出 `drive`（M23 §15.2）。"""
    return a["class"] == "vehicle" and a.get("drive") is not None


def load_lane_graph(world_dir: Path):
    """世界包 `world.json` 中 `geojson/awr-navgraph@1` 图层（M22 nav.graph）的 `LaneGraph`；无该图层或文件缺失时为 None。"""
    from awr.world.glb.derive.lanes import FORMAT, LaneGraph

    try:
        w = json.loads((Path(world_dir) / "world.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    lay = next((x for x in w.get("layers", []) if x.get("format") == FORMAT and x.get("href")), None)
    p = None if lay is None else Path(world_dir) / lay["href"]
    return LaneGraph.load(p) if p is not None and p.is_file() else None


def _navgraph_sha256(g) -> str:
    """车道图的内容哈希：文件头 `content_sha256`（M22 写出时给出）；测试中直接构造的车道图取段 id 与几何的 JCS 哈希。"""
    sha = (getattr(g, "head", None) or {}).get("content_sha256")
    if sha:
        return str(sha)
    return hashlib.sha256(jcs({k: g.segs[k].xy.tolist() for k in sorted(g.segs)})).hexdigest()


class _ZoneLanes:
    """背景车流的车道图视图：`routable()` 只给出弧长中点位于 zone 多边形内的车道（起点与终点都取自 zone），其余属性照原图。"""

    def __init__(self, g, keep: set[str]) -> None:
        self._g, self._keep = g, keep

    def routable(self) -> list[str]:
        return [k for k in self._g.routable() if k in self._keep]

    def __getattr__(self, name: str):
        return getattr(self._g, name)


class _RouteNav:
    """门 3 `S-OFFMESH` 的车道口径（M23 §6.9.4 车辆行）：点到车道路线中心线的水平距离减车道半宽（不足 0 记 0）。"""

    def __init__(self, P: np.ndarray, half_m: float) -> None:
        self.P, self.half_m = np.asarray(P, np.float64)[:, :2], float(half_m)

    def offmesh_dist_batch(self, xy: np.ndarray) -> np.ndarray:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        A, AB = self.P[:-1], self.P[1:] - self.P[:-1]
        den = AB[:, 0] * AB[:, 0] + AB[:, 1] * AB[:, 1]
        out = np.empty(len(xy))
        for a in range(0, len(xy), 256):
            p = xy[a:a + 256, None]
            t = np.clip(((p[..., 0] - A[:, 0]) * AB[:, 0] + (p[..., 1] - A[:, 1]) * AB[:, 1]) / np.where(den > 0, den, 1.0), 0.0, 1.0)
            dx, dy = p[..., 0] - (A[:, 0] + AB[:, 0] * t), p[..., 1] - (A[:, 1] + AB[:, 1] * t)
            out[a:a + 256] = np.sqrt(dx * dx + dy * dy).min(axis=1)
        return np.maximum(out - self.half_m, 0.0)


@dataclass
class LaneTraffic:
    """车道图车流（`lane_traffic` 的结果）：一次 `vehicle.simulate` 推进全部车道图车辆（脚本车辆按 `actor_order`，其后背景车辆
    按演员表 idx），附门 2 问题、各路点的路线坐标与吸附点、实际停留、停靠窗口、到达、排时建议与生成自检。"""
    errors: list[Issue]
    warnings: list[Issue]
    sim: Any = None                             # vehicle.VehicleSim；门 2 失败时为 None
    net: Any = None
    graph: Any = None
    index: dict[str, int] = field(default_factory=dict)           # 车辆 id → sim 列
    table_idx: dict[int, int] = field(default_factory=dict)       # 背景车辆的演员表 idx → sim 列
    scripted: list[str] = field(default_factory=list)
    wp_u: dict[str, list[float]] = field(default_factory=dict)    # 脚本车辆各路点的路线坐标（m，路线首点为 0）
    wp_snap: dict[str, list[Snap]] = field(default_factory=dict)  # 各路点在路线上的投影点（anchor、dist_m 为锚点到车道的距离）
    holds: dict[str, list[int]] = field(default_factory=dict)     # 各停车点的实际停留（ms）
    windows: dict[str, list[tuple[float, float, Snap]]] = field(default_factory=dict)
    arrivals: dict[str, list[dict]] = field(default_factory=dict)
    retime: dict[str, list[float]] = field(default_factory=dict)
    legs: dict[str, list[Leg]] = field(default_factory=dict)
    audit: dict | None = None
    navgraph_sha256: str | None = None
    iterations: int = 0
    _nav: dict = field(default_factory=dict)

    def route_nav(self, g, actor_id: str) -> _RouteNav:
        if actor_id not in self._nav:
            r = self.sim.specs[self.index[actor_id]].route
            xyz, _cum = g.route_polyline(r)
            w = [float(g.segs[k].props.get("width_m") or LANE_WIDTH_DEFAULT_M) for k in r.segments if g.segs[k].kind == "lane"]
            self._nav[actor_id] = _RouteNav(xyz, min(w, default=LANE_WIDTH_DEFAULT_M) / 2.0)
        return self._nav[actor_id]

    def report(self) -> dict:
        """磁带清单 `report.lanes`（tape_manifest.v1）：车道图哈希、生成摘要、逐车路线与停留、生成自检与决策日志计数。"""
        sim, lg = self.sim, self.sim.log
        spawn = {e["actor"]: e["t_ms"] for e in lg["spawns"]}
        veh = [{"id": s.id, "scripted": s.id in self.index and s.id in self.scripted, "segments": len(s.route.segments),
                "length_m": round(s.route.length_m, 3), "depart_ms": s.depart_ms, "spawn_ms": spawn.get(s.id), "end": s.end,
                "stops": [[round(u, 3), int(d)] for u, d in s.stops]} for s in sim.specs]
        au = self.audit or {}
        return {"navgraph_sha256": self.navgraph_sha256, "digest": sim.digest(), "vehicles": veh,
                "audit": {"ok": au.get("ok"), "counts": au.get("counts", {}), "stats": au.get("stats", {})},
                "log": {"hold_iterations": self.iterations, **{k: lg[k] for k in ("gap_clamps", "stop_clamps", "safety_stops", "max_decel_mps2",
                                                                                   "max_wait_ms", "denied", "absent_at_end", "pending_at_end")}}}


def _traffic_key(plan: dict) -> tuple[str, str]:
    return "lane_traffic", hashlib.sha256(jcs({k: plan[k] for k in ("seed", "duration_ms", "actors", "background")})).hexdigest()


def _route_point(cum: np.ndarray, xyz: np.ndarray, u: float) -> tuple[float, float, float]:
    return (float(np.interp(u, cum, xyz[:, 0])), float(np.interp(u, cum, xyz[:, 1])),
            float(np.interp(u, cum, np.nan_to_num(xyz[:, 2]))))


def _zone_lanes(g, poly: np.ndarray | None) -> set[str]:
    if poly is None or len(poly) < 3:
        return set()
    keep = set()
    for k in g.lanes:
        s = g.segs[k]
        h = s.length / 2.0
        x, y = float(np.interp(h, s.s, s.xy[:, 0])), float(np.interp(h, s.s, s.xy[:, 1]))
        if point_in_polygon(poly, np.array([x]), np.array([y]))[0]:
            keep.add(k)
    return keep


def lane_traffic(plan: dict, stage: Stage) -> LaneTraffic | None:
    """车道图车流（门 2、生成与门 3 共用，同一舞台按 plan 缓存；M23 §15.4，D3-AC-33）。

    1. 路线：`drive` 车辆的路点 `at`（及其 `via`）锚点依次投影到最近车道（> 3 m 告警 `W-SNAP`，> 15 m 为 `I-LANES` 612，
       why = unsnapped），由 `vehicle.chain_route` 展开为一条连续路线（不可达为 `I-DISCONNECTED` 612）；路点的路线坐标为投影点弧长；
       出发时刻为首路点时刻（向上取整到 100 ms），车身取演员尺寸。
    2. 停车：首路点带 `dwell_s`、中间路点 `wait`（或带 `dwell_s` 的 `pass`）在该点停稳后停留到 max(实际停稳 + dwell, 计划时刻 + dwell)，
       与运动学排时的 t_dep = max(t_arr, t_plan) + dwell 同义（固定点迭代：停稳时刻只取决于更早的停留，至多停车数 + 1 次推进收敛）；
       `stop` 停稳后停留 `dwell_s`（不等计划时刻）；末路点 `despawn` 驶到终点消失（带 `dwell_s` 时先在终点停留），`park` 在终点泊车
       （发动机熄火），其余在终点停住。
    3. 背景：世界有车道图时 `class: vehicle` 的背景组由 `vehicle.background` 生成（流 19 `vehicle` 通道，键的 idx 为演员表 idx；起点与
       终点车道的弧长中点位于 zone 内；出发时刻取全时长；期望速度上限为组 `speed_mps` 上界），与脚本车辆同一次推进；未得到路线的
       背景车辆全程缺席并告警 `W-LANES-BG`。
    4. 门 2 检查：脚本车辆未能出生（`I-LANES`，why = spawn_blocked）；路点实际到达晚于计划时刻 + `tol_s`（`I-LATE` 612，附实际时刻作排时建议）。
       到达行（门 3 `S-ARRIVAL` 车道口径）：到达时刻为车头首次到达路点前 0.5 m（`STOP_TOL_M`）的帧（首路点为出生帧，`despawn` 末路点为
       消失帧），位置误差容差为 0.5 m 加该帧一步行程，时间只检查迟到（≤ `tol_s`）。
    5. 生成自检：`vehicle.audit`（D3-AC-33 判据，含决策日志）写入 `report.lanes`；门 3 `vehicle_track` 验证器在磁带一致时复用。
    事件没有车道图车辆时返回 None。"""
    key = _traffic_key(plan)
    if key in stage.cache:
        return stage.cache[key]
    from awr.sim.events import vehicle as V

    order = actor_order(plan)
    acts = [a for a in order if lane_driven(a)]
    g = stage.lane_graph() if acts or any(x["class"] == "vehicle" for x in plan["background"]) else None
    has_bg = g is not None and any(x["class"] == "vehicle" and x["count"] > 0 for x in plan["background"])
    if not acts and not has_bg:
        stage.cache[key] = None
        return None
    idx_of = {a["id"]: i for i, a in enumerate(plan["actors"])}
    lt = LaneTraffic([], [])
    stage.cache[key] = lt
    if g is None:
        lt.errors += [_issue("I-LANES", 612, f"/actors/{idx_of[a['id']]}/drive", f"世界包没有车道图（nav.graph），{a['id']} 不能按车道图驾驶",
                             f"The world package has no lane graph (nav.graph); {a['id']} cannot drive on lanes", actor=a["id"],
                             why="no_lane_graph") for a in acts]
        return lt
    net = stage.lane_net()
    lt.net, lt.graph, lt.navgraph_sha256 = net, g, _navgraph_sha256(g)
    specs, holds, routes_xyz = [], [], {}
    for a in acts:
        ai, r = idx_of[a["id"]], a["route"]
        pts, where = [], []
        for j, w in enumerate(r):
            for m, v in enumerate(w["via"] if j > 0 else []):
                pts.append(anchor_xy(stage.anchors[v]))
                where.append((f"/actors/{ai}/route/{j}/via/{m}", v, None))
            pts.append(anchor_xy(stage.anchors[w["at"]]))
            where.append((f"/actors/{ai}/route/{j}/at", w["at"], j))
        bad, dist = False, {}
        for (path, aid, j), xy in zip(where, pts, strict=True):
            _lane, _s, d = g.nearest_lane(xy)
            if j is not None:
                dist[j] = d
            if d > LANE_SNAP_MAX_M:
                bad = True
                lt.errors.append(_issue("I-LANES", 612, path, f"锚点 {aid} 距最近车道 {d:.1f} m，超过 {LANE_SNAP_MAX_M:g} m",
                                        f"Anchor {aid} is {d:.1f} m from the nearest lane, above {LANE_SNAP_MAX_M:g} m", actor=a["id"], anchor=aid,
                                        dist_m=round(d, 3), why="unsnapped"))
            elif d > LANE_SNAP_WARN_M and not any(x.code == "W-SNAP" and x.detail.get("anchor") == aid for x in lt.warnings):
                lt.warnings.append(_issue("W-SNAP", None, path, f"锚点 {aid} 到车道中心线 {d:.1f} m", f"Anchor {aid} is {d:.1f} m from the lane centre line",
                                          anchor=aid, dist_m=d, profile="lanes"))
        if bad:
            continue
        rc = V.chain_route(g, pts)
        if rc is None:
            lt.errors.append(_issue("I-DISCONNECTED", 612, f"/actors/{ai}/route", f"{a['id']} 的路点在车道图上不可达",
                                    f"The waypoints of {a['id']} are not connected on the lane graph", actor=a["id"], why="lanes"))
            continue
        route, us = rc
        L, W = a["size_mm"][0] / 1000.0, a["size_mm"][1] / 1000.0
        if max(0.0, L - route.s_start) >= route.length_m:
            lt.errors.append(_issue("I-LANES", 612, f"/actors/{ai}/route", f"{a['id']} 的车道路线 {route.length_m:.1f} m 短于车身",
                                    f"The lane route of {a['id']} ({route.length_m:.1f} m) is shorter than the vehicle", actor=a["id"], why="route_too_short"))
            continue
        wu = [us[n] for n, (_p, _a, j) in enumerate(where) if j is not None]
        xyz, cum = g.route_polyline(route)
        routes_xyz[a["id"]] = (xyz, cum)
        lt.wp_u[a["id"]] = wu
        lt.wp_snap[a["id"]] = [Snap(w["at"], _route_point(cum, xyz, wu[j]), -1, dist[j], "ok" if dist[j] <= LANE_SNAP_WARN_M else "warn")
                               for j, w in enumerate(r)]
        stops: list[tuple[float, int]] = []
        end, last = "wait", len(r) - 1
        for j, w in enumerate(r):
            do, dw = w["do"], int(w["dwell_ms"])
            if j == last:
                end = {"despawn": "despawn", "park": "park"}.get(do, "wait")
                if do == "despawn" and dw > 0:
                    holds.append((len(specs), len(stops), w["t_ms"] + dw, dw))
                    stops.append((wu[j], dw))
            elif do == "stop":
                stops.append((wu[j], dw))
            elif (j == 0 and dw > 0) or (j > 0 and (do == "wait" or dw > 0)):
                holds.append((len(specs), len(stops), w["t_ms"] + dw, dw))
                stops.append((wu[j], dw))
        dep = -(-r[0]["t_ms"] // TICK_MS) * TICK_MS
        specs.append(V.VehicleSpec(a["id"], route, dep, V.drive_params(a["drive"]), tuple(stops), end, length_m=L, width_m=W))
    # 背景车辆（演员表 idx：脚本演员之后按背景组次序）
    base, bg = len(order), []
    for gi, x in enumerate(plan["background"]):
        if x["class"] == "vehicle" and x["count"] > 0:
            zone = stage.anchors.get(x["zone"], {}).get("polygon") if isinstance(x["zone"], str) else x["zone"]
            keep = _zone_lanes(g, None if zone is None else np.asarray(zone, np.float64))
            got = V.background(_ZoneLanes(g, keep), x["count"], plan["seed"], idx0=base, depart_window_ms=plan["duration_ms"],
                               drive=V.DriveParams(v0_mps=float(x["speed_mps"][1])), id_prefix="#", net=net) if keep else []
            bg += [(int(s.id[1:]), replace(s, id=f"bg{gi}_{int(s.id[1:]) - base}")) for s in got]
            if len(got) < x["count"]:
                lt.warnings.append(_issue("W-LANES-BG", None, f"/background/{gi}", f"背景车辆组 {gi} 只得到 {len(got)}/{x['count']} 条车道路线，其余全程缺席",
                                          f"Background vehicle group {gi} got {len(got)}/{x['count']} lane routes; the rest stay absent",
                                          group=gi, count=x["count"], routed=len(got), zone_lanes=len(keep)))
        base += x["count"]
    if lt.errors:
        return lt
    lt.scripted = [s.id for s in specs]
    n_s = len(specs)
    all_specs = specs + [s for _i, s in bg]
    lt.index = {s.id: c for c, s in enumerate(all_specs)}
    lt.table_idx = {i: n_s + n for n, (i, _s) in enumerate(bg)}
    # 停留到计划时刻：固定点迭代（模块说明第 2 条）
    dwell = {(c, si): d for c, si, _tp, d in holds}
    for it in range(len(holds) + 2):
        cur = [replace(s, stops=tuple((u, dwell.get((c, si), d)) for si, (u, d) in enumerate(s.stops))) if c < n_s else s
               for c, s in enumerate(all_specs)]
        sim = V.simulate(g, cur, plan["duration_ms"], net=net)
        t_stop: dict[str, list[int]] = {}
        for e in sim.log["stops"]:
            t_stop.setdefault(e["actor"], []).append(e["t_ms"])
        new = dict(dwell)
        for c, si, tp, d in holds:
            ts = t_stop.get(cur[c].id, [])
            if si < len(ts):
                new[(c, si)] = max(d, tp - ts[si])
        lt.iterations = it + 1
        if new == dwell:
            break
        dwell = new
    else:                                                       # pragma: no cover - 因果性保证收敛
        raise RuntimeError("lane traffic hold iteration did not converge")
    lt.sim = sim
    lt.audit = V.audit(g, sim.routes, sim.tape(), log=sim.log, ids=sim.ids, lengths=[s.length_m for s in cur], widths=[s.width_m for s in cur],
                       net=net)
    if not lt.audit["ok"]:
        lt.warnings.append(_issue("W-LANE-AUDIT", None, "/actors", "车道图车流生成自检未通过（D3-AC-33 判据）",
                                  "The lane traffic self-audit failed (D3-AC-33 criteria)", counts=lt.audit["counts"]))
    for c, a in enumerate(acts):
        _lane_actor(lt, a, c, cur[c], routes_xyz[a["id"]], idx_of[a["id"]], plan["duration_ms"], t_stop.get(a["id"], []))
    return lt


def _lane_actor(lt: LaneTraffic, a: dict, c: int, spec, route_xyz, ai: int, dur_ms: int, t_stops: list[int]) -> None:
    """一辆脚本车道图车辆的到达行、迟到问题、排时建议、停靠窗口与门 2 路段报告（`lane_traffic` 第 4 条）。"""
    from awr.sim.events.vehicle import DT as V_DT
    from awr.sim.events.vehicle import STOP_TOL_M

    sim, r, wu = lt.sim, a["route"], lt.wp_u[a["id"]]
    xyz, cum = route_xyz
    st, uu, vv = sim.state[:, c], sim.u[:, c], sim.v[:, c]
    vis = np.isin(st, (1, 2))
    n_t = len(st)
    if not vis.any():
        lt.errors.append(_issue("I-LANES", 612, f"/actors/{ai}/route/0", f"{a['id']} 在事件时长内未能出生（出生点一直被占用）",
                                f"{a['id']} could not spawn within the event (the spawn point stayed occupied)", actor=a["id"], why="spawn_blocked"))
    u0 = max(0.0, spec.length_m - spec.route.s_start)
    rows, sug, last = [], [], len(r) - 1
    for j, w in enumerate(r):
        uw = max(wu[j], u0) if j == 0 else min(wu[j], spec.route.length_m)
        with np.errstate(invalid="ignore"):
            hit = vis if j == 0 else vis & (uu >= uw - STOP_TOL_M)
        if j == last and spec.end == "despawn":
            hit = hit | (st == 5)
        ks = np.flatnonzero(hit)
        k = int(ks[0]) if len(ks) else n_t - 1
        p = _route_point(cum, xyz, uw)
        xy = sim.xy[k, c]
        q = _fix(xy, 1000.0, np.int64)
        err = math.hypot(float(q[0]) - p[0] * 1000.0, float(q[1]) - p[1] * 1000.0) if len(ks) else 1e9
        t_arr = k * TICK_MS if len(ks) else dur_ms
        vk = max(float(vv[k]), float(vv[k - 1]) if k > 0 else 0.0)
        rows.append({"actor": a["id"], "waypoint": j, "at": w["at"], "do": w["do"], "t_plan_ms": w["t_ms"], "t_arrive_ms": int(t_arr),
                     "late_ms": int(t_arr) - w["t_ms"], "xy_plan_m": [p[0], p[1]], "xy_mm": [int(q[0]), int(q[1])], "err_mm": err,
                     "lane": True, "tol_ms": int(w["tol_ms"]), "tol_mm": 1000.0 * (STOP_TOL_M + vk * V_DT) + 1.0})
        sug.append(_ceil_tick_s(t_arr / 1000.0))
        if vis.any() and (not len(ks) or int(t_arr) - w["t_ms"] > int(w["tol_ms"])):
            lt.errors.append(_issue("I-LATE", 612, f"/actors/{ai}/route/{j}/t", f"{a['id']} 按车道图在 {t_arr / 1000.0:.1f} s 到达 {w['at']}，晚于计划 {w['t_ms'] / 1000.0:g} s 超过容差",
                                    f"{a['id']} reaches {w['at']} on the lane graph at {t_arr / 1000.0:.1f} s, later than planned {w['t_ms'] / 1000.0:g} s beyond tolerance",
                                    actor=a["id"], waypoint=j, t_plan_s=w["t_ms"] / 1000.0, t_arrive_s=None if not len(ks) else t_arr / 1000.0,
                                    tol_s=w["tol_ms"] / 1000.0))
    lt.arrivals[a["id"]], lt.retime[a["id"]] = rows, sug
    # 停靠窗口：途中停车 [停稳, 停稳 + 实际停留)；终点停住或泊车 [停稳, ∞)
    eff = [d for _u, d in spec.stops]
    lt.holds[a["id"]] = eff
    win = []
    for si, ts in enumerate(t_stops):
        k = min(ts // TICK_MS, n_t - 1)
        win.append((ts / 1000.0, (ts + eff[si]) / 1000.0, Snap(a["id"], (float(sim.xy[k, c, 0]), float(sim.xy[k, c, 1]), 0.0), -1, 0.0, "ok")))
    if spec.end in ("wait", "park"):
        with np.errstate(invalid="ignore"):
            ke = np.flatnonzero((st == 2) & (vv == 0) & (uu >= spec.route.length_m - STOP_TOL_M))
        if len(ke):
            k = int(ke[0])
            win.append((k * TICK_MS / 1000.0, math.inf, Snap(a["id"], (float(sim.xy[k, c, 0]), float(sim.xy[k, c, 1]), 0.0), -1, 0.0, "ok")))
    lt.windows[a["id"]] = win
    legs = []
    for i in range(len(r) - 1):
        k0, k1 = rows[i]["t_arrive_ms"] // TICK_MS, rows[i + 1]["t_arrive_ms"] // TICK_MS
        T = (r[i + 1]["t_ms"] - r[i]["t_ms"]) / 1000.0
        Lm = max(0.0, wu[i + 1] - wu[i])
        vp = float(np.nanmax(vv[k0:k1 + 1])) if k1 >= k0 else 0.0
        legs.append(Leg(a["id"], i, r[i]["at"], r[i + 1]["at"], None, Lm, T, Lm / T if T > 0 else math.inf, vp,
                        rows[i + 1]["late_ms"] <= rows[i + 1]["tol_ms"], "lane"))
    lt.legs[a["id"]] = legs


def _lane_z(stage: Stage, xy: np.ndarray, lane_z: np.ndarray) -> np.ndarray:
    """车道图车辆的贴地初值 `z_hint`：车辆导航面高度，缺失处取行人导航面高度，再缺失取车道图逐顶点高度（W13 后续事项：避免取到
    DSM 树冠高度）。Detour 后端用 `nav_z_batch`（只取导航面），栅格后端用高度场 `z_at_batch`。"""
    xy = np.asarray(xy, np.float64)
    out = np.full(len(xy), np.nan)
    for prof in ("vehicle", "person"):
        nav = stage.nav.get(prof)
        bad = ~np.isfinite(out)
        if nav is None or not bad.any():
            continue
        fn = getattr(nav, "nav_z_batch", None) or nav.z_at_batch
        u, inv = np.unique(xy[bad], axis=0, return_inverse=True)
        out[bad] = np.asarray(fn(u), np.float64)[np.asarray(inv).reshape(-1)]
    bad = ~np.isfinite(out)
    out[bad] = np.asarray(lane_z, np.float64)[bad]
    return out


def _lane_track(lt: LaneTraffic, stage: Stage, c: int) -> tuple[dict[str, np.ndarray], float]:
    """sim 第 c 列 → `eval_timeline` 同形的逐帧求值（z_hint 见 `_lane_z`）与首帧航向（度，出生帧的路线切向）。"""
    tr = lt.sim.track(c)
    zh = _lane_z(stage, tr["xy"], tr["z_hint"])
    ks = np.flatnonzero(tr["state"] > 0)
    h = lt.sim.heading[int(ks[0]) if len(ks) else 0, c]
    fd = math.degrees(math.atan2(float(h[1]), float(h[0])))
    return {"xy": tr["xy"], "vxy": tr["vxy"], "state": tr["state"].astype(np.uint8), "container": tr["container"], "z_hint": zh}, fd


def navgraph_ref(plan: dict, stage: Stage) -> dict:
    """磁带规范头 `world.navgraph_sha256`（tape.v1）：事件按车道图驾驶时给出车道图内容哈希，否则为空（磁带头不变）。"""
    lt = lane_traffic(plan, stage)
    return {"navgraph_sha256": lt.navgraph_sha256} if lt is not None and lt.sim is not None else {}


# ---------------------------------------------------------------------- 定点化与组装
def _fix(v: np.ndarray, k: float, dtype) -> np.ndarray:
    info = np.iinfo(dtype)
    return np.clip(np.floor(np.asarray(v, np.float64) * k + 0.5), info.min, info.max).astype(dtype)


def _yaw_cdeg(vxy: np.ndarray, state: np.ndarray, first_dir: float) -> np.ndarray:
    """移动时取速度方向；静止保持上一值；首帧取首段方向。"""
    sp = np.hypot(vxy[:, 0], vxy[:, 1])
    ang = np.degrees(np.arctan2(vxy[:, 1], vxy[:, 0]))
    upd = (sp > 1e-6) & (state == 1)
    last = np.maximum.accumulate(np.where(upd, np.arange(len(sp)), -1))      # 至 k 为止最近一次更新的帧（无则 −1）
    out = np.where(last >= 0, ang[np.maximum(last, 0)], first_dir)
    return fold_yaw(_fix(out, 100.0, np.int64))


def _resolve_carry(plan: dict, order: list[dict], ev: list[dict], navs: list) -> None:
    """物体携带（M23-FR-058、§15.1 第 3 条，P0b）：按 `dsl.carry_schedule` 改写物体的逐帧求值（乘客已复制容器记录之后）。

    - 携带帧 [拾取帧, 放下帧)：携带者状态 1 或 2 时物体状态 3、`container` 为携带者；携带者在车内（状态 3）时物体随之在车内，
      `container` 为该车辆（容器字段只指向可见的顶层容器）；携带者状态 0、4、5 时物体取相同状态、无容器；位置、速度与 z 提示
      复制携带者记录（贴地后 x、y、z 与容器逐位相同，§6.9.4 `S-CONTAINER`）；
    - 放下后：物体静止于放下帧的携带者位置（状态 2），直至下一次拾取或物体自身的 despawn；
    - 同一帧先放下后拾取（直接交接）：容器在相邻两帧由交出者变为接收者。
    没有拾取与放下的事件不经过本函数的任何改写，磁带与 `content_sha256` 不变。"""
    pos = {a["id"]: i for i, a in enumerate(order)}
    n_t = len(ev[0]["state"]) if ev else 0
    for oid, steps in carry_schedule(plan["actors"]).items():
        o = pos.get(oid)
        if o is None or order[o]["class"] != "object":
            continue
        e = ev[o]
        own_gone = e["state"] == 5
        holder = -1
        cuts = [(s.t_ms // TICK_MS, s) for s in steps] + [(n_t, None)]
        k_prev = 0
        for k, s in cuts:
            k = min(max(k, 0), n_t)
            if holder >= 0 and k > k_prev:                          # [k_prev, k) 由 holder 携带
                c = ev[holder]
                sl = slice(k_prev, k)
                e["xy"][sl], e["vxy"][sl], e["z_hint"][sl] = c["xy"][sl], c["vxy"][sl], c["z_hint"][sl]
                cs = c["state"][sl]
                e["state"][sl] = np.where(np.isin(cs, (1, 2, 3)), 3, cs)
                e["container"][sl] = np.where(np.isin(cs, (1, 2)), holder, np.where(cs == 3, c["container"][sl], -1))
            if s is None:
                break
            if s.kind == CARRY_PICKUP:
                holder = pos[s.actor]
            elif holder >= 0 and k < n_t:                           # 放下：静止于放下帧的携带者位置，至下一次拾取或 despawn
                rest = ev[holder]["xy"][k].copy()
                m = np.arange(n_t) >= k
                e["xy"][m], e["vxy"][m] = rest, 0.0
                e["z_hint"][m] = navs[o].z_at_batch(np.tile(rest, (int(m.sum()), 1)))
                e["state"][m] = np.where(own_gone[m], 5, 2)
                e["container"][m] = -1
                holder = -1
            else:
                holder = -1
            k_prev = k


def generate_frames(plan: dict, stage: Stage, pf: dict) -> tuple[np.ndarray, list[dict], dict]:
    """返回 (frames (n_ticks, n) RECORD, 演员表, 报告)。车道图车辆（脚本与背景）的逐帧求值取自 `lane_traffic`。"""
    n_t = plan["duration_ms"] // TICK_MS
    order = actor_order(plan)
    seed = plan["seed"]
    tracks, table, bg_rgb = [], [], []
    scripted = _scripted_tracks(pf, order)
    lt = pf.get("lanes")
    lane_ev: dict[int, tuple[dict, float]] = {}                 # 演员表 idx → (逐帧求值, 首帧航向)
    for i, a in enumerate(order):
        ph = scripted[i]
        nav = stage.nav[a["profile"] or "person"]
        tracks.append((ph, nav))
        if lt is not None and a["id"] in lt.index and lane_driven(a):
            lane_ev[i] = _lane_track(lt, stage, lt.index[a["id"]])
        ap = {k: v for k, v in a["appearance"].items() if k in ("asset", "tint_rgb", "top_rgb", "bottom_rgb", "body_rgb", "height_mm")}
        table.append({"idx": i, "id": a["id"], "class": a["class"], "scripted": True, "profile": a["profile"], "size_mm": a["size_mm"],
                      "appearance": ap})
        if a["class"] == "person" and "top_rgb" in a["appearance"]:
            bg_rgb.append(a["appearance"]["top_rgb"])
    n_s = len(order)
    for gi, g in enumerate(plan["background"]):
        prof = "vehicle" if g["class"] == "vehicle" else "person"
        nav = stage.nav[prof]
        on_lanes = g["class"] == "vehicle" and lt is not None and lt.sim is not None       # 车道图背景车流（`lane_traffic` 第 3 条）
        cells = zone_cells(nav, g["zone"], stage.anchors) if not on_lanes else np.zeros(0, np.int64)
        from awr.sim.events.dsl import CLASS_DEFAULTS

        size = [math.floor(s * 1000.0 + 0.5) for s in CLASS_DEFAULTS[g["class"]]["size"]]
        for k in range(g["count"]):
            idx = len(table)
            if on_lanes and idx in lt.table_idx:
                ph = []
                lane_ev[idx] = _lane_track(lt, stage, lt.table_idx[idx])
            elif len(cells):
                ph = background_timeline(g, idx, seed, nav, cells, plan["duration_ms"])
            else:
                ph = [Phase("absent", -INF_MS, INF_MS, p=(0.0, 0.0, 0.0))]
            tracks.append((ph, nav))
            look = background_look(seed, idx, g["distractor_frac"], bg_rgb, g["class"])
            sz = list(size)
            if "height_mm" in look:
                sz[2] = look["height_mm"]
            table.append({"idx": idx, "id": f"bg{gi}_{k}", "class": g["class"], "scripted": False, "profile": prof, "size_mm": sz,
                          "appearance": look})
    n = len(table)
    fr = np.zeros((n_t, n), RECORD)
    ev = [lane_ev[i][0] if i in lane_ev else eval_timeline(ph, n_t, nav.z_at_batch) for i, (ph, nav) in enumerate(tracks)]
    for e in ev:                                                # 乘客：复制容器记录
        m = e["container"] >= 0
        if m.any():
            c = ev[int(e["container"][m][0])]
            src = np.nonzero(m)[0]
            gone = c["state"][src] == 5
            e["xy"][src], e["vxy"][src], e["z_hint"][src] = c["xy"][src], c["vxy"][src], c["z_hint"][src]
            e["state"][src] = np.where(gone, 5, 3)
            e["container"][src[gone]] = -1
    _resolve_carry(plan, order, ev, [nav for _ph, nav in tracks[:n_s]])
    xy = np.stack([e["xy"] for e in ev], 1)
    zh = np.stack([e["z_hint"] for e in ev], 1)
    zh_f = np.where(np.isfinite(zh), zh, 0.0)
    z, found = stage.ground.ground_z_batch(xy.reshape(-1, 2), zh_f.ravel())
    z = z.reshape(n_t, n)
    found = found.reshape(n_t, n)
    for i, e in enumerate(ev):
        first = next((p for p in tracks[i][0] if p.kind == "move"), None)
        fd = 0.0
        if first is not None and len(first.path) > 1:
            d = first.path[1] - first.path[0]
            fd = math.degrees(math.atan2(d[1], d[0]))
        if i in lane_ev:
            fd = lane_ev[i][1]
        fr["x_mm"][:, i] = _fix(xy[:, i, 0], 1000.0, np.int32)
        fr["y_mm"][:, i] = _fix(xy[:, i, 1], 1000.0, np.int32)
        fr["z_mm"][:, i] = _fix(z[:, i], 1000.0, np.int32)
        fr["yaw_cdeg"][:, i] = _yaw_cdeg(e["vxy"], e["state"], fd)
        fr["vx_cmps"][:, i] = _fix(e["vxy"][:, 0], 100.0, np.int16)
        fr["vy_cmps"][:, i] = _fix(e["vxy"][:, 1], 100.0, np.int16)
        fr["state"][:, i] = e["state"]
        fr["container"][:, i] = np.where(e["container"] >= 0, e["container"], NO_CONTAINER).astype(np.uint8)
    for i, e in enumerate(ev):                                  # 乘客与被携带物体的航向逐帧与容器一致
        ks = np.flatnonzero(e["state"] == 3)
        if len(ks):
            fr["yaw_cdeg"][ks, i] = fr["yaw_cdeg"][ks, fr["container"][ks, i].astype(np.int64)]
    _finalize_vz(fr)
    vis = np.isin(fr["state"], (1, 2))
    report = _report(order, pf, fr, vis, n_s, found, _arrivals(order, pf, scripted))
    return fr, table, report


def _finalize_vz(fr: np.ndarray) -> None:
    """§6.6.6：vz = floor((z_{k+1} − z_{k−1}) / 2 + 0.5)（cm/s），端点单侧差分，非可见为 0。"""
    z = fr["z_mm"].astype(np.int64)
    vz = np.zeros_like(z)
    if len(z) >= 3:
        vz[1:-1] = (z[2:] - z[:-2] + 1) // 2
    if len(z) >= 2:
        vz[0] = z[1] - z[0]
        vz[-1] = z[-1] - z[-2]
    vis = np.isin(fr["state"], (1, 2))
    fr["vz_cmps"] = np.where(vis, np.clip(vz, -32768, 32767), 0).astype(np.int16)


def _report(order, pf, fr, vis, n_s, found, arr_rows) -> dict:
    # 到达取自实现的时间线（运动学排时下 late_s 为 0，W-SLOW 亦然）；Interactive 的实现磁带另行计算
    arr = [{"actor": r["actor"], "at": r["at"], "t_plan_s": r["t_plan_ms"] / 1000.0, "t_arrive_s": r["t_arrive_ms"] / 1000.0,
            "late_s": r["late_ms"] / 1000.0} for r in arr_rows]
    P = np.stack([fr["x_mm"], fr["y_mm"], fr["z_mm"]], -1).astype(np.float64) / 1000.0
    ss = bs = math.inf
    off = ~np.eye(n_s, dtype=bool)
    step = max(1, (1 << 21) // max(1, (fr.shape[1] - n_s) * n_s))              # 按 tick 分块，块内 (k, 背景, 脚本) 距离张量约 ≤ 48 MB
    for k0 in range(0, len(fr), step):
        Ps, vs = P[k0:k0 + step, :n_s], vis[k0:k0 + step, :n_s]
        d = np.linalg.norm(Ps[:, :, None] - Ps[:, None], axis=-1)
        m = vs[:, :, None] & vs[:, None] & off
        if m.any():
            ss = min(ss, float(d[m].min()))
        Pb, vb = P[k0:k0 + step, n_s:], vis[k0:k0 + step, n_s:]
        if Pb.shape[1] and vs.any() and vb.any():
            d = np.linalg.norm(Pb[:, :, None] - Ps[:, None], axis=-1)
            m = vb[:, :, None] & vs[:, None]
            if m.any():
                bs = min(bs, float(d[m].min()))
    nz = int((~found & vis).sum())
    warns = sorted({w.code for w in pf["warnings"]}) + (["S-ZGROUND-CANDIDATE"] if nz else [])
    out = {"arrivals": arr, "boards": pf["boards"], "warnings": warns,
           "min_dist_m": {"scripted_scripted": None if math.isinf(ss) else round(ss, 3), "bg_scripted": None if math.isinf(bs) else round(bs, 3)}}
    lt = pf.get("lanes")
    if lt is not None and lt.sim is not None:                   # 车道图车辆（tape_manifest.v1 report.lanes）
        out["lanes"] = lt.report()
    return out


# ---------------------------------------------------------------------- 规范头
def _patch(K: dict) -> dict:
    return {"wind": {"speed_ref_mps": K["wind_mps"], "dir_from_deg": K["wind_dir_deg"], "turb_sigma_u_ref_mps": K["gust_sigma_mps"]},
            "precip": {"rain_mmh": K["rain_mm_h"]}, "atmosphere": {"mor_bg_m": K["visibility_m"], "fog_top_agl_m": K["fog"]},
            "cloud": {"cover": K["cloud"]}}


_ENV_KEYS = ("wind_mps", "wind_dir_deg", "gust_sigma_mps", "rain_mm_h", "visibility_m", "fog", "cloud")


def compile_env_track(env: dict) -> list[dict]:
    """§6.11：首帧 step；smooth 按 ≤ 600 s 子段、linear 按 1 s 子段线性内插终点值。风向按最短弧内插。"""
    kf = env["keyframes"]
    ops = [{"t_ms": 0, "op": {"kind": "set", "patch": _patch(kf[0]), "mode": "step", "duration_s": 0}}]
    mode = env["interpolation"]
    for a, b in itertools.pairwise(kf):
        if mode == "step":
            ops.append({"t_ms": b["t_ms"], "op": {"kind": "set", "patch": _patch(b), "mode": "step", "duration_s": 0}})
            continue
        sub = 600_000 if mode == "smooth" else 1000
        t = a["t_ms"]
        while t < b["t_ms"]:
            t1 = min(t + sub, b["t_ms"])
            u = (t1 - a["t_ms"]) / (b["t_ms"] - a["t_ms"])
            K = {k: a[k] + (b[k] - a[k]) * u for k in _ENV_KEYS}
            dd = ((b["wind_dir_deg"] - a["wind_dir_deg"] + 180.0) % 360.0) - 180.0
            K["wind_dir_deg"] = (a["wind_dir_deg"] + dd * u) % 360.0
            ops.append({"t_ms": t, "op": {"kind": "set", "patch": _patch(K), "mode": "smooth", "duration_s": (t1 - t) / 1000.0}})
            t = t1
    return ops


def build_header(plan: dict, table: list[dict], world: dict, event_sha: str) -> dict:
    return {"schema": "drone4d.tape.v1", "event_id": plan["event_id"], "event_version": plan["version"], "event_sha256": event_sha,
            "mode": plan["mode"], "seed": plan["seed"], "tick_hz": 10, "n_ticks": plan["duration_ms"] // TICK_MS,
            "duration_ms": plan["duration_ms"], "record": record_spec(), "codec": {"name": "col-delta-zstd@1", "block_ticks": 100},
            "actors": table, "world": world, "environment": plan["environment"], "env_track": compile_env_track(plan["environment"]),
            "comms": plan["comms"], "anim": None, "realized": None}


# ---------------------------------------------------------------------- 真值骨架（§6.10.1）
def dsl_sha256(plan: dict) -> str:
    """规范化后完整 DSL（plan，含注释、关系、显示名与简报）的 JCS 哈希；磁带输入子集见 `dsl.event_sha256`。"""
    return hashlib.sha256(jcs(plan)).hexdigest()


def truth_sha256(truth: dict) -> str:
    """§6.10.1：去掉 `perf` 后的 JCS 哈希。"""
    return hashlib.sha256(jcs({k: v for k, v in truth.items() if k != "perf"})).hexdigest()


def build_truth(plan: dict, tape_content_sha256: str, *, strict: bool = True, truth_rev: int = 1, detector_params: dict | None = None,
                events: list[dict] | None = None, annotation_checks: list[dict] | None = None, relation_checks: list[dict] | None = None,
                skyvis_1hz: dict | None = None, consistency: dict | None = None, perf: dict | None = None,
                key_events: list[dict] | None = None, key_relations: list[dict] | None = None, validator_params: dict | None = None,
                report_map: dict | None = None) -> dict:
    """组装 `event_truth.json`（`drone4d.event_truth.v1`）。真值随 `dsl_sha256` 绑定完整 DSL：只改注释、关系、显示名或简报时
    磁带与 `content_sha256` 不变，`dsl_sha256` 与 `truth_sha256` 改变（§6.7.5）。门 3 的结果经关键字参数传入。

    类型验证器的输出（M23 §15.3 规则 1、4，M23-FR-077）：`key_events`、`key_relations`、`validator_params`、`report_map` 只在
    事件类型登记了验证器且其给出结果时写入（None 时不写该字段，未登记验证器的事件真值与此前逐字节相同）。"""
    doc = {"schema": TRUTH_SCHEMA, "event_id": plan["event_id"], "event_version": plan["version"], "truth_rev": int(truth_rev),
           "tape_content_sha256": tape_content_sha256, "dsl_sha256": dsl_sha256(plan), "strict": bool(strict),
           "detector_params": dict(detector_params or {}), "events": list(events or []), "annotation_checks": list(annotation_checks or []),
           "relation_checks": list(relation_checks or []), "skyvis_1hz": dict(skyvis_1hz or {}), "consistency": dict(consistency or {})}
    for k, v in (("key_events", key_events), ("key_relations", key_relations), ("validator_params", validator_params),
                 ("report_map", report_map)):
        if v is not None:
            doc[k] = list(v) if isinstance(v, list) else dict(v)
    if perf is not None:
        doc["perf"] = dict(perf)
    return doc


def build_graph(truth: dict, actors: list[dict]) -> dict:
    """事件图 `event_graph.json`（`drone4d.event_graph.v1`，M23 §6.10.2、M23-FR-074）：由真值与磁带头演员表组装。

    节点：脚本演员实体 `{id, kind: entity, class, scripted}`（含 `object` 类物体）；真值事件与类型验证器关键事件
    `{id, kind: event, type, interval_ms, declared}`（关键事件的 `declared` 取其引用的真值事件或匹配的声明）。边：参与者
    `participates_in` 事件；通过验证的作者关系（`relation_checks`，按注释 id 连接其首个命中的真值事件）；关键事件之间的验证器关系
    （`key_relations`）。实体按 id、事件按 (起始时刻, id)、边按 (rel, src, dst) 排序，同一真值两次组装逐字节相同。"""
    ents = [{"id": a["id"], "kind": "entity", "class": a["class"], "scripted": True} for a in actors if a.get("scripted")]
    names = {a["id"] for a in actors if a.get("scripted")}
    evs = [{"id": e["id"], "kind": "event", "type": e["type"], "interval_ms": list(e["interval_ms"]), "declared": bool(e["declared"])}
           for e in truth.get("events", [])]
    keys = [{"id": k["id"], "kind": "event", "type": k["type"], "interval_ms": list(k["interval_ms"]), "declared": bool(k.get("declared", False))}
            for k in truth.get("key_events", [])]
    edges = {(p, "participates_in", e["id"]) for e in [*truth.get("events", []), *truth.get("key_events", [])] for p in e["participants"]
             if p in names}
    ann = {e["annotation"]: e["id"] for e in truth.get("events", []) if e.get("annotation") and e["id"] == e["annotation"]}
    edges |= {(ann[r["src"]], r["rel"], ann[r["dst"]]) for r in truth.get("relation_checks", []) if r["verified"] and r["src"] in ann
              and r["dst"] in ann}
    edges |= {(r["src"], r["rel"], r["dst"]) for r in truth.get("key_relations", [])}
    return {"schema": GRAPH_SCHEMA, "event_id": truth["event_id"], "event_version": truth["event_version"], "truth_rev": truth["truth_rev"],
            "nodes": sorted(ents, key=lambda n: n["id"]) + sorted(evs + keys, key=lambda n: (n["interval_ms"][0], n["id"])),
            "edges": [{"src": a, "rel": r, "dst": b} for a, r, b in sorted(edges, key=lambda x: (x[1], x[0], x[2]))]}
