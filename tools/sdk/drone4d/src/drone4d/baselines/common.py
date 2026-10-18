"""基线策略的公共部分（M26 §14.3）：起飞与到位、估计系 goto、云台 look-at、检测级多目标关联与 report 规则。

检测级观测的 `det_id` 每帧匿名，基线以地面位置做最近邻关联（门限 4 m，类别相同），形成本机轨迹，位置与速度取近 3 s 原始位置的
直线拟合；外观可识别时以上衣颜色
与简报目标比对（RGB 欧氏距离 < 45）。report 规则只读本机轨迹与已收到的消息：
- `meet`：两条已持续 ≥ 5 s 的人员轨迹地面距离 ≤ 3 m 并保持 ≥ 5 s（> 5 m 才清零；检测地面位置带噪，对应简报 2 m 判据），
  其间至少 20 帧两人同帧被检测（排除同一人的分裂轨迹）；
- `board`：人员轨迹在静止车辆（轨迹拟合 ≥ 2 s、速度 < 1 m/s）4 m 内消失（≥ 2 s 未再关联），该车随后 60 s 内启动（> 1.5 m/s）时确认；
- `enter_building`：人员轨迹在公开门锚点 4 m 内消失 ≥ 5 s，其后门旁未再出现人员，附近无车辆。
每条 report 附各参与者最近 3 个检测作为证据（uav、sensor、frame_seq、det_id；L2 为 bbox_xywh）；每辆车、每个门只报一次，同类型 30 s
内只报一次。

L2、L2-depth：检测由参考检测器 `ref_det_v1` 与感知适配层（`drone4d.perception.ref_det.L2Perception`）从相机图像得到，形同 L1 检测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from drone4d.agent import DroneAgent, act, gimbal_look_at, nav_goto, nav_takeoff, report

__all__ = ["BaselineAgent", "Track", "Tracks"]

GATE_M = 4.0
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

    def span_s(self) -> float:
        return (self.hist[-1][0] - self.hist[0][0]) / 1e9 if self.hist else 0.0

    def fit(self) -> None:
        """近 FIT_S 秒原始地面位置的最小二乘直线：位置取末端拟合值，速度取斜率（检测地面位置逐帧独立带噪，差分速度不可用）。"""
        h = self.hist
        if len(h) < 5 or h[-1][0] - h[0][0] < 1_000_000_000:
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
        by_frame: dict[int, list[dict]] = {}
        for d in dets:
            by_frame.setdefault(d["seq"], []).append(d)
        touched: list[Track] = []
        for seq in sorted(by_frame):
            used: set[str] = set()
            for d in sorted(by_frame[seq], key=lambda d: d["det_id"]):
                x, y = d["ground_xy_noisy"]
                best, bd = None, GATE_M
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
                    best = Track(f"{self.uav}-t{self.n}", d["class"], (x, y), t, t)
                    self.items.append(best)
                best.hist = [p for p in best.hist if t - p[0] <= FIT_NS] + [(t, x, y)]
                best.t_last = t
                best.fit()
                if "appearance" in d:
                    best.rgb = tuple(d["appearance"]["top_rgb"])
                ev = {"bbox_xywh": d["bbox_xywh"]} if d.get("l2") else {"det_id": d["det_id"]}
                best.ev = ([*best.ev, {"uav": self.uav, "sensor": d["sensor_id"], "frame_seq": d["seq"], **ev}])[-3:]
                used.add(best.tid)
                touched.append(best)
        for tr in self.items:
            if not tr.closed and (t_ns - tr.t_last) / 1e9 > 15.0:
                tr.closed = True
        return touched

    def live(self, t_ns: int, max_age_s: float = LOST_S) -> list[Track]:
        return [tr for tr in self.items if not tr.closed and (t_ns - tr.t_last) / 1e9 <= max_age_s]


def rgb_match(rgb, target) -> bool:
    return rgb is not None and target is not None and math.dist(rgb, target) < TARGET_RGB_TOL


class BaselineAgent(DroneAgent):
    """起飞到巡航高度后进入 `mission(obs, inbox)`；派生类返回 (goto 目标 xy, look_at 地面点 xy 或 None)。"""

    name = "baseline"
    ALT_AGL_M: float | None = None
    CRUISE_MPS = 8.0

    def reset(self, config: dict) -> None:
        super().reset(config)
        self.uav = config["uav_id"]
        self.peers = config["peers"]
        b = config["brief"]
        self.area = [tuple(p) for p in b["area"]["polygon_enu_m"]]
        lo, hi = b["area"]["alt_agl_range_m"]
        fleet = sorted([self.uav, *self.peers])
        layer = LAYER_M * (fleet.index(self.uav) - 0.5 * (len(fleet) - 1))   # 高度分层：同高度交叉航线会机间碰撞
        self.alt = min(max((self.ALT_AGL_M or 0.5 * (lo + hi)) + layer, lo + 2.0), hi - 2.0)
        self.anchors = b["anchors"]
        self.targets = [t["appearance"].get("top_rgb") for t in b["targets"]]
        self.vocab = set(b["report_vocab"])
        self.cam = next(s["sensor_id"] for s in config["sensors"] if s["kind"] == "camera")
        self.tracks = Tracks(self.uav)
        self.perception = None
        if config.get("obs_level") in ("L2", "L2-depth"):
            from drone4d.perception.ref_det import L2Perception

            self.perception = L2Perception(config)
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
        extra = self.messages(obs)
        z = self.ground_ref + self.alt
        g = gimbal_look_at(self.cam, (look[0], look[1], self.ground_ref)) if look is not None else None
        nav_cmd = nav_goto((goal[0], goal[1], z), self.CRUISE_MPS) if goal is not None else None
        return act(obs["k"], nav=nav_cmd, gimbal=g, msgs=(msgs + extra)[:8], report=reports)

    def mission(self, obs: dict, inbox: list[dict]):
        raise NotImplementedError

    def on_inbox(self, inbox: list[dict]) -> list[dict]:
        return []

    def messages(self, obs: dict) -> list[dict]:
        self.just_reported = []
        return []

    # --------------------------------------------------------------- report 规则
    def suppressed(self, type_: str, t_ns: int) -> bool:
        return self.suppress.get(type_, -1) >= t_ns

    def _emit(self, out: list[dict], type_: str, parts: list[Track], t0: int, t1: int, xy, conf: float) -> None:
        out.append(report(type_, [{"track_id": tr.tid, "evidence": tr.ev} for tr in parts], t0, t1, (xy[0], xy[1], self.ground_ref or 0.0), conf))
        self.suppress[type_] = t1 + SUPPRESS_NS
        self.just_reported.append((type_, t1))

    def reports(self, t_ns: int) -> list[dict]:
        out: list[dict] = []
        if "meet" in self.vocab and not self.suppressed("meet", t_ns):
            persons = [tr for tr in self.tracks.live(t_ns) if tr.cls == "person" and (t_ns - tr.t_first) / 1e9 >= MIN_TRACK_S]
            for i, a in enumerate(persons):
                for b in persons[i + 1:]:
                    key = tuple(sorted((a.tid, b.tid)))
                    d = math.dist(a.xy, b.xy)
                    if a.t_last == b.t_last and key in self.tracks.near_since:
                        self.tracks.near_co[key] = self.tracks.near_co.get(key, 0) + 1
                    if d <= MEET_M:
                        t0 = self.tracks.near_since.setdefault(key, t_ns)
                        if (t_ns - t0 >= MEET_HOLD_NS and self.tracks.near_co.get(key, 0) >= MEET_CO_MIN and ("meet", *key) not in self.reported
                                and not self.suppressed("meet", t_ns)):
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
        return out[:4]

    # --------------------------------------------------------------- 几何工具
    def area_center(self) -> tuple[float, float]:
        xs, ys = zip(*self.area, strict=True)
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def target_tracks(self, t_ns: int, max_age_s: float = 3.0) -> list[Track]:
        return [tr for tr in self.tracks.live(t_ns, max_age_s) if any(rgb_match(tr.rgb, t) for t in self.targets)]
