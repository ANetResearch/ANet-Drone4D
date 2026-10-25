"""基线策略的公共部分（M26 §14.3）：起飞与到位、估计系 goto、云台 look-at、检测级多目标关联与 report 规则。

检测级观测的 `det_id` 每帧匿名，基线以地面位置做最近邻关联（门限 4 m、车辆 6 m，类别相同），形成本机轨迹，
位置与速度取近 3 s 原始位置的直线拟合；外观可识别时以上衣颜色与简报目标比对（RGB 欧氏距离 < 45）。
report 规则只读本机轨迹与已收到的消息：
- `meet`：两条已持续 ≥ 5 s 的人员轨迹地面距离 ≤ 3 m 并保持 ≥ 5 s（> 5 m 才清零；检测地面位置带噪，对应简报 2 m 判据），
  其间至少 20 帧两人同帧被检测（排除同一人的分裂轨迹）；
- `board`：人员轨迹在静止车辆（轨迹拟合 ≥ 2 s、速度 < 1 m/s）4 m 内消失（≥ 2 s 未再关联），该车随后 60 s 内启动（> 1.5 m/s）时确认；
- `enter_building`：人员轨迹在公开门锚点 4 m 内消失 ≥ 5 s，其后门旁未再出现人员，附近无车辆。
每条 report 附各参与者最近 3 个检测作为证据（uav、sensor、frame_seq、det_id；L2 为 bbox_xywh）；每辆车、每个门只报一次，同类型 30 s
内只报一次。

L2、L2-depth：检测由参考检测器 `ref_det_v1` 与感知适配层（`anet4d.perception.ref_det.L2Perception`）从相机图像得到，形同 L1 检测。

P0b（M26-FR-122）：四类事件的其余 report 类型（`handover`、`disperse`、`alight`、`exit_building`、`reacquire`、`identity_link`、
`vehicle_event`、`found`）由 `anet4d.baselines.events` 按简报 `report_vocab` 启用，规则见该模块；目标搜索先验见
`anet4d.baselines.prior`。热成像通道（`ref_thermal_blob_v1`，`anet4d.baselines.thermal`）：构造参数 `thermal=None`（缺省）时，
L2 与 L2-depth 下 agent_config 含 `kind = thermal` 的传感器即启用，`True` 强制启用，`False` 关闭；热成像检测与相机检测进入同一
轨迹关联（按 (时刻, 传感器, 帧序号) 分帧），report 证据优先取相机帧，轨迹在窗口内只有热像仪帧时才引用热像仪帧。
外观比对：上衣颜色距离 < 45，且双方都有下装颜色时下装颜色距离也 < 45；类别须与简报目标一致（车辆目标的 `top_rgb` 即车身颜色）。
每条 report 的证据取窗口终点及以前的最近 3 个检测（同一轨迹保留首 3 个与最近 64 个检测供回溯）。

巡航高度（构造参数 `alt_agl_m`，ADR-217 决策 (1)）：机队中心高度，缺省取任务包络中值；各机按 uav id 排序以 `LAYER_M` 分层，
限制在包络内（距上下界各 2 m）。r6 参考行（`anet4d.baselines.reference.REFERENCE6`）以该参数取 r6 §12.3 的高度。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from anet4d.agent import DroneAgent, act, gimbal_look_at, nav_goto, nav_takeoff, report
from anet4d.baselines import events
from anet4d.baselines.prior import search_prior

__all__ = ["BaselineAgent", "Track", "Tracks", "rgb_match", "suppress_ns"]

GATE_M = 4.0
GATE_V_M = 6.0                    # 车辆关联门限（P0b）：行驶车辆急转弯时直线外推误差可达 5 m
GAP_REC_NS = 3_000_000_000        # 同一轨迹相邻两次关联间隔 ≥ 3 s 记为一次观测间断（Re-ID 重新捕获判定用）
EV_KEEP = 64
LAYER_M = 8.0                     # 机间高度分层间隔（按 uav id 排序居中分配）
LOST_S = 2.0
TARGET_RGB_TOL = 45.0
FIT_NS = 3_000_000_000            # 轨迹位置与速度取近 3 s 原始位置的直线拟合
MIN_TRACK_S = 5.0                 # 参与 report 判定的轨迹至少已持续 5 s
MEET_M, MEET_RESET_M, MEET_HOLD_NS = 3.0, 5.0, 5_000_000_000   # 地面位置带噪（sigma ≈ 0.4 + 0.008 R），≤ 3 m 计近、> 5 m 才清零
MEET_CO_MIN = 20                  # 保持期间至少 20 帧同帧检测到两人
BOARD_M = 4.0
DOOR_M = 4.0
ENTER_LOST_S = 5.0
SUPPRESS_NS = 30_000_000_000      # 同类型 report 30 s 内只报一次（含他机经消息告知的）
# P0b 类型的抑制时长（秒）：键为 report 类型（`vehicle_event` 按 `vehicle_event:<event>` 分键）；未列出的类型取 30 s
SUPPRESS_S = {"alight": 10.0, "exit_building": 10.0, "reacquire": 10.0, "identity_link": 10.0, "vehicle_event": 8.0, "found": 3600.0}


def suppress_ns(key: str) -> int:
    return int(SUPPRESS_S.get(key.split(":", 1)[0], SUPPRESS_NS / 1e9) * 1e9)


@dataclass
class Track:
    tid: str
    cls: str
    xy: tuple[float, float]
    t_first: int
    t_last: int
    rgb: tuple[int, int, int] | None = None
    ev: list[dict] = field(default_factory=list)
    vel: tuple[float, float] = (0.0, 0.0)
    closed: bool = False
    hist: list[tuple[int, float, float]] = field(default_factory=list)
    bottom: tuple[int, int, int] | None = None
    xy0: tuple[float, float] = (0.0, 0.0)                    # 首个检测的地面位置
    n: int = 0                                               # 关联的检测数
    last_frame: tuple[str, int] = ("", -1)                   # 最近一次关联的 (传感器, 帧序号)
    gaps: list[tuple[int, int]] = field(default_factory=list)   # 观测间断 (间断前末次时刻, 恢复时刻)
    ev_first: list[tuple[int, bool, dict]] = field(default_factory=list)   # 首 3 个证据 (时刻, 是否热像仪, 证据)
    evh: list[tuple[int, bool, dict]] = field(default_factory=list)        # 最近 EV_KEEP 个证据

    def span_s(self) -> float:
        return (self.hist[-1][0] - self.hist[0][0]) / 1e9 if self.hist else 0.0

    def evidence(self, t_end: int) -> list[dict]:
        """窗口终点 t_end 及以前的最近 3 个证据，相机帧优先；没有时取最早的证据（至多 3 个）。"""
        seen: set[tuple[str, int]] = set()
        rows = []
        for t, th, e in [*self.ev_first, *self.evh]:
            k = (e["sensor"], e["frame_seq"])
            if k not in seen:
                seen.add(k)
                rows.append((t, th, e))
        rows.sort(key=lambda r: (r[0], r[2]["sensor"], r[2]["frame_seq"]))
        upto = [r for r in rows if r[0] <= t_end] or rows[:1]
        cam = [r for r in upto if not r[1]]
        return [e for _t, _th, e in (cam or upto)[-3:]]

    def fit(self) -> None:
        """近 FIT_S 秒原始地面位置的最小二乘直线：位置取末端拟合值，速度取斜率（检测地面位置逐帧独立带噪，差分速度不可用）。
        人员需 ≥ 5 个点且跨 ≥ 1 s；车辆（P0b）≥ 3 个点且跨 ≥ 0.2 s 即拟合，否则行驶车辆在新轨迹的前 1 s 内以均值位置预测，
        8 m/s 时滞后超过关联门限而分裂。"""
        h = self.hist
        n_min, span_min = (3, 200_000_000) if self.cls == "vehicle" else (5, 1_000_000_000)
        if len(h) < n_min or h[-1][0] - h[0][0] < span_min:
            n = len(h)
            self.xy = (sum(p[1] for p in h) / n, sum(p[2] for p in h) / n)
            return
        t0 = h[-1][0]
        ts = [(p[0] - t0) / 1e9 for p in h]
        tm = sum(ts) / len(ts)
        xm, ym = sum(p[1] for p in h) / len(h), sum(p[2] for p in h) / len(h)
        var = sum((t - tm) ** 2 for t in ts)
        vx = sum((t - tm) * (p[1] - xm) for t, p in zip(ts, h, strict=True)) / var
        vy = sum((t - tm) * (p[2] - ym) for t, p in zip(ts, h, strict=True)) / var
        self.vel = (vx, vy)
        self.xy = (xm - vx * tm, ym - vy * tm)


class Tracks:
    def __init__(self, uav: str) -> None:
        self.uav = uav
        self.items: list[Track] = []
        self.n = 0
        self.near_since: dict[tuple[str, str], int] = {}
        self.near_co: dict[tuple[str, str], int] = {}      # 近距保持期间两轨迹同帧更新的次数（排除同一人的分裂轨迹）

    def update(self, dets: list[dict], t_ns: int) -> list[Track]:
        by_frame: dict[tuple[int, str, int], list[dict]] = {}
        for d in dets:                                           # 分帧键 (时刻, 传感器, 帧序号)：相机与热像仪的帧序号各自计数
            by_frame.setdefault((d["t_mid_ns"], d["sensor_id"], d["seq"]), []).append(d)
        touched: list[Track] = []
        for fk in sorted(by_frame):
            used: set[str] = set()
            for d in sorted(by_frame[fk], key=lambda d: d["det_id"]):
                x, y = d["ground_xy_noisy"]
                best, bd = None, (GATE_V_M if d["class"] == "vehicle" else GATE_M)
                for tr in self.items:
                    if tr.closed or tr.cls != d["class"] or tr.tid in used:
                        continue
                    dt = (d["t_mid_ns"] - tr.t_last) / 1e9
                    px, py = tr.xy[0] + tr.vel[0] * dt, tr.xy[1] + tr.vel[1] * dt
                    dist = math.hypot(x - px, y - py)
                    if dist < bd:
                        best, bd = tr, dist
                t = d["t_mid_ns"]
                if best is None:
                    self.n += 1
                    best = Track(f"{self.uav}-t{self.n}", d["class"], (x, y), t, t, xy0=(x, y))
                    self.items.append(best)
                elif t - best.t_last >= GAP_REC_NS:
                    best.gaps.append((best.t_last, t))
                best.hist = [p for p in best.hist if t - p[0] <= FIT_NS] + [(t, x, y)]
                best.t_last = t
                best.n += 1
                best.last_frame = (d["sensor_id"], d["seq"])
                best.fit()
                if "appearance" in d:
                    best.rgb = tuple(d["appearance"]["top_rgb"])
                    if "bottom_rgb" in d["appearance"]:
                        best.bottom = tuple(d["appearance"]["bottom_rgb"])
                ev = {"bbox_xywh": d["bbox_xywh"]} if d.get("l2") else {"det_id": d["det_id"]}
                e = {"uav": self.uav, "sensor": d["sensor_id"], "frame_seq": d["seq"], **ev}
                th = bool(d.get("thermal"))
                if not th:
                    best.ev = [*best.ev, e][-3:]
                row = (t, th, e)
                if len(best.ev_first) < 3:
                    best.ev_first.append(row)
                best.evh = [*best.evh, row][-EV_KEEP:]
                used.add(best.tid)
                touched.append(best)
        for tr in self.items:
            if not tr.closed and (t_ns - tr.t_last) / 1e9 > 15.0:
                tr.closed = True
        return touched

    def live(self, t_ns: int, max_age_s: float = LOST_S) -> list[Track]:
        return [tr for tr in self.items if not tr.closed and (t_ns - tr.t_last) / 1e9 <= max_age_s]


def rgb_match(rgb, target, bottom=None, target_bottom=None) -> bool:
    if rgb is None or target is None or math.dist(rgb, target) >= TARGET_RGB_TOL:
        return False
    return bottom is None or target_bottom is None or math.dist(bottom, target_bottom) < TARGET_RGB_TOL


class BaselineAgent(DroneAgent):
    """起飞到巡航高度后进入 `mission(obs, inbox)`；派生类返回 (goto 目标 xy, look_at 地面点 xy 或 None)。"""

    name = "baseline"
    ALT_AGL_M: float | None = None
    CRUISE_MPS = 8.0

    def __init__(self, *, thermal: bool | None = None, alt_agl_m: float | None = None) -> None:
        self.use_thermal = thermal
        self.alt_agl_m = alt_agl_m

    def reset(self, config: dict) -> None:
        super().reset(config)
        self.uav = config["uav_id"]
        self.peers = config["peers"]
        b = config["brief"]
        self.area = [tuple(p) for p in b["area"]["polygon_enu_m"]]
        self.alt_range = tuple(float(x) for x in b["area"]["alt_agl_range_m"])
        fleet = sorted([self.uav, *self.peers])
        self.layer = LAYER_M * (fleet.index(self.uav) - 0.5 * (len(fleet) - 1))   # 高度分层：同高度交叉航线会机间碰撞
        self.alt = self.layered_alt(self.alt_agl_m or self.ALT_AGL_M or 0.5 * sum(self.alt_range))
        self.anchors = b["anchors"]
        self.targets = [t["appearance"].get("top_rgb") for t in b["targets"]]
        self.target_bottoms = [t["appearance"].get("bottom_rgb") for t in b["targets"]]
        self.target_classes = [t.get("class", "person") for t in b["targets"]]
        self.vocab = set(b["report_vocab"])
        self.prior = search_prior(b)
        self.cam = next(s["sensor_id"] for s in config["sensors"] if s["kind"] == "camera")
        self.tracks = Tracks(self.uav)
        self.perception = None
        self.thermal = None
        l2 = config.get("obs_level") in ("L2", "L2-depth")
        if l2:
            from anet4d.perception.ref_det import L2Perception

            self.perception = L2Perception(config)
        has_th = any(s["kind"] == "thermal" for s in config["sensors"])
        if has_th and (self.use_thermal or (self.use_thermal is None and l2)):
            from anet4d.baselines.thermal import ThermalPerception

            self.thermal = ThermalPerception(config)
        self.looks: list[tuple[int, tuple[float, float]]] = []       # 近 10 s 的云台 look-at 地面点（出建筑判定的"已观察"条件）
        events.init_state(self)
        self.reported: set[tuple] = set()
        self.board_cand: dict[str, tuple[str, int]] = {}
        self.suppress: dict[str, int] = {}
        self.just_reported: list[tuple[str, int]] = []
        self.ground_ref: float | None = None
        self.phase = "start"
        self.t_ns = 0

    # --------------------------------------------------------------- 公共循环
    def step(self, obs: dict, inbox: list[dict]) -> dict:
        self.t_ns = obs["t_ns"]
        nav = obs["nav"]
        self.pos = nav["pos_enu_m"]
        if self.ground_ref is None:
            self.ground_ref = obs["ground_hint"]["dtm_m"]
        dets = self.perception.detect(obs) if self.perception is not None else obs["sensors"].get("detections", [])
        if self.thermal is not None:
            dets = [*dets, *self.thermal.detect(obs)]
        self.tracks.update(dets, obs["t_ns"])
        msgs, reports = self.on_inbox(inbox), self.reports(obs["t_ns"])
        st = obs["status"]["mode"]
        if self.phase == "start":
            self.phase = "climb"
            return act(obs["k"], nav=nav_takeoff(self.alt))
        if self.phase == "climb":
            if st == "airborne" and self.pos[2] - self.ground_ref >= self.alt - 3.0:
                self.phase = "mission"
            else:
                return act(obs["k"], msgs=msgs, report=reports)
        goal, look = self.mission(obs, inbox)
        if look is not None:
            self.looks = [p for p in self.looks if self.t_ns - p[0] <= 10_000_000_000] + [(self.t_ns, (float(look[0]), float(look[1])))]
        extra = self.messages(obs)
        z = self.ground_ref + self.goal_alt()
        g = gimbal_look_at(self.cam, (look[0], look[1], self.ground_ref)) if look is not None else None
        nav_cmd = nav_goto((goal[0], goal[1], z), self.CRUISE_MPS) if goal is not None else None
        return act(obs["k"], nav=nav_cmd, gimbal=g, msgs=(msgs + extra)[:8], report=reports)

    def mission(self, obs: dict, inbox: list[dict]):
        raise NotImplementedError

    def layered_alt(self, center: float) -> float:
        """机队中心高度 → 本机高度：加本机分层偏移，限制在任务包络内（距上下界各 2 m）。"""
        lo, hi = self.alt_range
        return min(max(center + self.layer, lo + 2.0), hi - 2.0)

    def goal_alt(self) -> float:
        """任务阶段 goto 的目标高度（相对起飞点地面）；缺省为巡航高度，派生类可按状态改变（`track_share` 跟踪高度）。"""
        return self.alt

    def on_inbox(self, inbox: list[dict]) -> list[dict]:
        return []

    def messages(self, obs: dict) -> list[dict]:
        self.just_reported = []
        return []

    # --------------------------------------------------------------- report 规则
    def suppressed(self, type_: str, t_ns: int) -> bool:
        return self.suppress.get(type_, -1) >= t_ns

    def _emit(self, out: list[dict], type_: str, parts: list[Track], t0: int, t1: int, xy, conf: float, event: str | None = None) -> None:
        r = report(type_, [{"track_id": tr.tid, "evidence": tr.evidence(t1)} for tr in parts], t0, t1, (xy[0], xy[1], self.ground_ref or 0.0),
                   conf)
        key = type_
        if event is not None:
            r["event"] = event
            key = f"{type_}:{event}"
        out.append(r)
        self.suppress[key] = max(t1, self.t_ns) + suppress_ns(key) if type_ in SUPPRESS_S else t1 + SUPPRESS_NS
        self.just_reported.append((key, t1))

    def reports(self, t_ns: int) -> list[dict]:
        out: list[dict] = []
        if "meet" in self.vocab and not self.suppressed("meet", t_ns):
            persons = [tr for tr in self.tracks.live(t_ns) if tr.cls == "person" and (t_ns - tr.t_first) / 1e9 >= MIN_TRACK_S]
            for i, a in enumerate(persons):
                for b in persons[i + 1:]:
                    key = tuple(sorted((a.tid, b.tid)))
                    d = math.dist(a.xy, b.xy)
                    if a.last_frame == b.last_frame and a.t_last == b.t_last and key in self.tracks.near_since:
                        self.tracks.near_co[key] = self.tracks.near_co.get(key, 0) + 1
                    if d <= MEET_M:
                        t0 = self.tracks.near_since.setdefault(key, t_ns)
                        held = t_ns - t0 >= MEET_HOLD_NS and self.tracks.near_co.get(key, 0) >= MEET_CO_MIN
                        if held:
                            events.note_meet(self, key, t0, t_ns)
                        if held and ("meet", *key) not in self.reported and not self.suppressed("meet", t_ns):
                            self.reported.add(("meet", *key))
                            self._emit(out, "meet", [a, b], t0, t_ns, ((a.xy[0] + b.xy[0]) / 2, (a.xy[1] + b.xy[1]) / 2), 0.6)
                    elif d > MEET_RESET_M:
                        self.tracks.near_since.pop(key, None)
                        self.tracks.near_co.pop(key, None)
        vehicles = [tr for tr in self.tracks.live(t_ns, 1.0) if tr.cls == "vehicle"]
        for p in self.tracks.items:                                    # 乘车候选：人员在静止车辆旁消失
            if p.cls != "person" or p.closed or (t_ns - p.t_last) / 1e9 < LOST_S or (p.t_last - p.t_first) / 1e9 < MIN_TRACK_S:
                continue
            near_v = [v for v in vehicles if math.dist(v.xy, p.xy) <= BOARD_M and v.span_s() >= 2.0 and math.hypot(*v.vel) < 1.0]
            doors = [a for a in self.anchors if a["kind"] == "door" and math.dist(a["pos_enu_m"][:2], p.xy) <= DOOR_M]
            if near_v and p.tid not in self.board_cand:
                self.board_cand[p.tid] = (near_v[0].tid, p.t_last)
            elif doors and not near_v and (t_ns - p.t_last) / 1e9 >= ENTER_LOST_S and "enter_building" in self.vocab:
                door = doors[0]["pos_enu_m"][:2]
                back = any(q.cls == "person" and q.t_last > p.t_last and math.dist(q.xy, door) <= DOOR_M for q in self.tracks.items)
                if not back and ("enter_building", doors[0]["id"]) not in self.reported and not self.suppressed("enter_building", t_ns):
                    self.reported.add(("enter_building", doors[0]["id"]))
                    self._emit(out, "enter_building", [p], p.t_last - 3_000_000_000, p.t_last, p.xy, 0.5)
                p.closed = True
        for pid, (vid, t_lost) in list(self.board_cand.items()):      # 车辆随后启动（> 1.5 m/s）即确认乘车
            v = next((tr for tr in self.tracks.items if tr.tid == vid), None)
            if v is None or (t_ns - t_lost) / 1e9 > 60.0:
                del self.board_cand[pid]
                continue
            if math.hypot(*v.vel) > 1.5 and v.span_s() >= 2.0 and v.t_last == t_ns and "board" in self.vocab:
                p = next(tr for tr in self.tracks.items if tr.tid == pid)
                if ("board", vid) not in self.reported and not self.suppressed("board", t_ns):
                    self.reported.add(("board", vid))
                    self._emit(out, "board", [p, v], t_lost - 2_000_000_000, t_ns, p.xy, 0.5)
                p.closed = True
                del self.board_cand[pid]
        events.reports(self, t_ns, out)
        return out[:4]

    # --------------------------------------------------------------- 几何工具
    def area_center(self) -> tuple[float, float]:
        xs, ys = zip(*self.area, strict=True)
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def target_no(self, tr: Track) -> int | None:
        """轨迹匹配的简报目标序号（类别一致且外观匹配；先列者优先），无匹配为 None。"""
        for i, (rgb, bot, cls) in enumerate(zip(self.targets, self.target_bottoms, self.target_classes, strict=True)):
            if cls == tr.cls and rgb_match(tr.rgb, rgb, tr.bottom, bot):
                return i
        return None

    def target_tracks(self, t_ns: int, max_age_s: float = 3.0) -> list[Track]:
        return [tr for tr in self.tracks.live(t_ns, max_age_s) if self.target_no(tr) is not None]
