"""门 2 与磁带生成（M23 §6.5、§6.6、§6.11）：吸附、几何预检与排时建议、运动学脚本演员、背景行走者、贴地、定点化与规范头。

P0a 过渡实现（Python）：路径来自 `nav.NavGrid`（栅格 A*，同一路径函数供预检与生成，`len_m` 逐位相同）；背景演员按 §6.6.4 的
目标点抽样、速度与停留抽样（流 19）沿路径匀速行走，暂不做 DetourCrowd 互避（最小间距写入报告供 M27 评测）。脚本演员的轨迹
只由 plan 决定，与背景人数无关。全部时刻为整数毫秒，帧时刻 t_k = 100·k ms。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np

from awr.sim.events.anchors import anchor_xy, point_in_polygon
from awr.sim.events.dsl import Issue
from awr.sim.events.nav import NavGrid, Snap, rounded_length
from awr.sim.events.tape import NO_CONTAINER, RECORD, fold_yaw, record_spec
from awr.sim.sensors import cbrng

__all__ = ["STREAM", "Stage", "build_header", "compile_env_track", "generate_frames", "preflight", "t_min", "v_peak"]

STREAM = 19
TICK_MS = 100
BOARD_DIST_M = 8.0
ZONE_SLOT_R = 0.8
INF_MS = 1 << 62
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
    """事件舞台：各 profile 的可走栅格、贴地索引与解析后的锚点。"""
    nav: dict[str, NavGrid]
    ground: object                              # GroundIndex
    anchors: dict[str, dict]
    snaps: dict[tuple[str, str], Snap] = field(default_factory=dict)

    def snap(self, anchor_id: str, profile: str, xy: tuple[float, float] | None = None, key: str | None = None) -> Snap:
        k = (key or anchor_id, profile)
        if k not in self.snaps:
            p = xy or anchor_xy(self.anchors[anchor_id])
            self.snaps[k] = self.nav[profile].snap(anchor_id, p[0], p[1])
        return self.snaps[k]


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
    """门 2：返回 {errors, warnings, legs (actor → [Leg]), snaps, retime, vehicle_dwell_ext_s, boards, windows}。"""
    errs: list[Issue] = []
    warns: list[Issue] = []
    slots = _slots(plan, stage.anchors)
    order = actor_order(plan)
    idx_of = {a["id"]: i for i, a in enumerate(plan["actors"])}
    legs: dict[str, list[Leg]] = {}
    retime: dict[str, list[float]] = {}
    windows: dict[str, list[tuple[float, float, Snap]]] = {}
    pts_of: dict[str, list[Snap]] = {}
    for a in order:
        ai = idx_of[a["id"]]
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
    return {"errors": errs, "warnings": warns, "legs": legs, "retime": {"mode": mode, "actors": retime, "vehicle_dwell_ext_s": ext},
            "boards": boards, "windows": windows, "points": pts_of,
            "snaps": [{"anchor": s.anchor, "profile": k[1], "dist_m": round(s.dist_m, 3) if math.isfinite(s.dist_m) else None,
                       "status": s.status} for k, s in sorted(stage.snaps.items())]}


def _snap_issue(s: Snap, path: str, errs: list, warns: list) -> None:
    if s.status == "error":
        errs.append(_issue("E-ANCHOR-UNSNAPPED", 611, path, f"锚点 {s.anchor} 无法吸附到可走面（{s.why}）",
                           f"Anchor {s.anchor} cannot be snapped to the walkable surface ({s.why})", anchor=s.anchor, why=s.why))
    elif s.status == "warn" and not any(w.detail.get("anchor") == s.anchor for w in warns if w.code == "W-SNAP"):
        warns.append(_issue("W-SNAP", None, path, f"锚点 {s.anchor} 吸附距离 {s.dist_m:.1f} m", f"Anchor {s.anchor} snapped {s.dist_m:.1f} m away",
                            anchor=s.anchor, dist_m=s.dist_m))


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


# ---------------------------------------------------------------------- 定点化与组装
def _fix(v: np.ndarray, k: float, dtype) -> np.ndarray:
    info = np.iinfo(dtype)
    return np.clip(np.floor(np.asarray(v, np.float64) * k + 0.5), info.min, info.max).astype(dtype)


def _yaw_cdeg(vxy: np.ndarray, state: np.ndarray, first_dir: float) -> np.ndarray:
    """移动时取速度方向；静止保持上一值；首帧取首段方向。"""
    sp = np.hypot(vxy[:, 0], vxy[:, 1])
    ang = np.degrees(np.arctan2(vxy[:, 1], vxy[:, 0]))
    out = np.empty(len(sp))
    cur = first_dir
    for k in range(len(sp)):
        if sp[k] > 1e-6 and state[k] == 1:
            cur = ang[k]
        out[k] = cur
    return fold_yaw(_fix(out, 100.0, np.int64))


def generate_frames(plan: dict, stage: Stage, pf: dict) -> tuple[np.ndarray, list[dict], dict]:
    """返回 (frames (n_ticks, n) RECORD, 演员表, 报告)。"""
    n_t = plan["duration_ms"] // TICK_MS
    order = actor_order(plan)
    vidx = {a["id"]: i for i, a in enumerate(order) if a["class"] == "vehicle"}
    seed = plan["seed"]
    tracks, table, bg_rgb = [], [], []
    for i, a in enumerate(order):
        bt = {b["waypoint"]: b["t_board_s"] for b in pf["boards"] if b["actor"] == a["id"] and b["kind"] == "board" and b["t_board_s"] is not None}
        ph = scripted_timeline(a, pf["legs"].get(a["id"], []), pf["points"][a["id"]], bt, vidx)
        nav = stage.nav[a["profile"] or "person"]
        tracks.append((ph, nav))
        ap = {k: v for k, v in a["appearance"].items() if k in ("asset", "tint_rgb", "top_rgb", "bottom_rgb", "body_rgb", "height_mm")}
        table.append({"idx": i, "id": a["id"], "class": a["class"], "scripted": True, "profile": a["profile"], "size_mm": a["size_mm"],
                      "appearance": ap})
        if a["class"] == "person" and "top_rgb" in a["appearance"]:
            bg_rgb.append(a["appearance"]["top_rgb"])
    n_s = len(order)
    for gi, g in enumerate(plan["background"]):
        prof = "vehicle" if g["class"] == "vehicle" else "person"
        nav = stage.nav[prof]
        cells = zone_cells(nav, g["zone"], stage.anchors)
        from awr.sim.events.dsl import CLASS_DEFAULTS

        size = [math.floor(s * 1000.0 + 0.5) for s in CLASS_DEFAULTS[g["class"]]["size"]]
        for k in range(g["count"]):
            idx = len(table)
            ph = background_timeline(g, idx, seed, nav, cells, plan["duration_ms"]) if len(cells) else \
                [Phase("absent", -INF_MS, INF_MS, p=(0.0, 0.0, 0.0))]
            tracks.append((ph, nav))
            look = background_look(seed, idx, g["distractor_frac"], bg_rgb, g["class"])
            sz = list(size)
            if "height_mm" in look:
                sz[2] = look["height_mm"]
            table.append({"idx": idx, "id": f"bg{gi}_{k}", "class": g["class"], "scripted": False, "profile": prof, "size_mm": sz,
                          "appearance": look})
    n = len(table)
    fr = np.zeros((n_t, n), RECORD)
    ev = [eval_timeline(ph, n_t, nav.z_at_batch) for ph, nav in tracks]
    for e in ev:                                                # 乘客：复制容器记录
        m = e["container"] >= 0
        if m.any():
            c = ev[int(e["container"][m][0])]
            src = np.nonzero(m)[0]
            gone = c["state"][src] == 5
            e["xy"][src], e["vxy"][src], e["z_hint"][src] = c["xy"][src], c["vxy"][src], c["z_hint"][src]
            e["state"][src] = np.where(gone, 5, 3)
            e["container"][src[gone]] = -1
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
        fr["x_mm"][:, i] = _fix(xy[:, i, 0], 1000.0, np.int32)
        fr["y_mm"][:, i] = _fix(xy[:, i, 1], 1000.0, np.int32)
        fr["z_mm"][:, i] = _fix(z[:, i], 1000.0, np.int32)
        fr["yaw_cdeg"][:, i] = _yaw_cdeg(e["vxy"], e["state"], fd)
        fr["vx_cmps"][:, i] = _fix(e["vxy"][:, 0], 100.0, np.int16)
        fr["vy_cmps"][:, i] = _fix(e["vxy"][:, 1], 100.0, np.int16)
        fr["state"][:, i] = e["state"]
        fr["container"][:, i] = np.where(e["container"] >= 0, e["container"], NO_CONTAINER).astype(np.uint8)
    for i, e in enumerate(ev):                                  # 乘客的航向与容器一致
        m = e["state"] == 3
        if m.any():
            c = int(fr["container"][m, i][0])
            fr["yaw_cdeg"][m, i] = fr["yaw_cdeg"][m, c]
    _finalize_vz(fr)
    vis = np.isin(fr["state"], (1, 2))
    report = _report(plan, order, pf, fr, vis, n_s, found)
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


def _report(plan, order, pf, fr, vis, n_s, found) -> dict:
    # 运动学排时按计划时刻到达（W-SLOW 亦然），到达偏差为 0；Interactive 的实现磁带另行计算
    arr = [{"actor": a["id"], "at": w["at"], "t_plan_s": w["t_ms"] / 1000.0, "t_arrive_s": w["t_ms"] / 1000.0, "late_s": 0.0}
           for a in order for w in a["route"]]
    P = np.stack([fr["x_mm"], fr["y_mm"], fr["z_mm"]], -1).astype(np.float64) / 1000.0
    ss = bs = math.inf
    for k in range(len(fr)):
        s = np.nonzero(vis[k, :n_s])[0]
        if len(s) >= 2:
            d = np.linalg.norm(P[k, s][:, None] - P[k, s][None], axis=-1)
            d[np.arange(len(s)), np.arange(len(s))] = np.inf
            ss = min(ss, float(d.min()))
        b = np.nonzero(vis[k, n_s:])[0] + n_s
        if len(s) and len(b):
            d = np.linalg.norm(P[k, b][:, None] - P[k, s][None], axis=-1)
            bs = min(bs, float(d.min()))
    nz = int((~found & vis).sum())
    warns = sorted({w.code for w in pf["warnings"]}) + (["S-ZGROUND-CANDIDATE"] if nz else [])
    return {"arrivals": arr, "boards": pf["boards"], "warnings": warns,
            "min_dist_m": {"scripted_scripted": None if math.isinf(ss) else round(ss, 3), "bg_scripted": None if math.isinf(bs) else round(bs, 3)}}


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
