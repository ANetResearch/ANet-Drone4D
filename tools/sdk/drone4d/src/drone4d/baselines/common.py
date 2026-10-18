"""基线策略的公共部分（M26 §14.3）：起飞与到位、估计系 goto、云台 look-at、检测级多目标关联与 report 规则。

检测级观测的 `det_id` 每帧匿名，基线以地面位置做最近邻关联（门限 4 m，类别相同），形成本机轨迹；外观可识别时以上衣颜色
与简报目标比对（RGB 欧氏距离 < 45）。report 规则只读本机轨迹与已收到的消息：
- `meet`：两条人员轨迹在同帧中地面距离 ≤ 2 m 且持续 ≥ 5 s；
- `board`：人员轨迹在静止车辆 3 m 内消失（≥ 2 s 未再关联），车辆仍被观测；
- `enter_building`：人员轨迹在公开门锚点 6 m 内消失（≥ 3 s），附近无车辆。
每条 report 附各参与者最近 3 个检测作为证据（uav、sensor、frame_seq、det_id）；同一参与者组合只报一次。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from drone4d.agent import DroneAgent, act, gimbal_look_at, nav_goto, nav_takeoff, report

__all__ = ["BaselineAgent", "Track", "Tracks"]

GATE_M = 4.0
LOST_S = 2.0
TARGET_RGB_TOL = 45.0


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


class Tracks:
    def __init__(self, uav: str) -> None:
        self.uav = uav
        self.items: list[Track] = []
        self.n = 0
        self.near_since: dict[tuple[str, str], int] = {}

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
                if best is None:
                    self.n += 1
                    best = Track(f"{self.uav}-t{self.n}", d["class"], (x, y), d["t_mid_ns"], d["t_mid_ns"])
                    self.items.append(best)
                else:
                    dt = max((d["t_mid_ns"] - best.t_last) / 1e9, 1e-3)
                    vx, vy = (x - best.xy[0]) / dt, (y - best.xy[1]) / dt
                    best.vel = (0.7 * best.vel[0] + 0.3 * vx, 0.7 * best.vel[1] + 0.3 * vy)
                    best.xy = (0.5 * best.xy[0] + 0.5 * x, 0.5 * best.xy[1] + 0.5 * y)
                    best.t_last = d["t_mid_ns"]
                if "appearance" in d:
                    best.rgb = tuple(d["appearance"]["top_rgb"])
                best.ev = ([*best.ev, {"uav": self.uav, "sensor": d["sensor_id"], "frame_seq": d["seq"], "det_id": d["det_id"]}])[-3:]
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
        self.alt = self.ALT_AGL_M or 0.5 * (lo + hi)
        self.anchors = b["anchors"]
        self.targets = [t["appearance"].get("top_rgb") for t in b["targets"]]
        self.vocab = set(b["report_vocab"])
        self.cam = next(s["sensor_id"] for s in config["sensors"] if s["kind"] == "camera")
        self.tracks = Tracks(self.uav)
        self.reported: set[tuple] = set()
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
        dets = obs["sensors"].get("detections", [])
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
        return []

    # --------------------------------------------------------------- report 规则
    def reports(self, t_ns: int) -> list[dict]:
        out = []
        live = self.tracks.live(t_ns)
        persons = [tr for tr in live if tr.cls == "person" and tr.t_last == t_ns]
        for i, a in enumerate(persons):
            for b in persons[i + 1:]:
                key = tuple(sorted((a.tid, b.tid)))
                if math.dist(a.xy, b.xy) <= 2.0:
                    t0 = self.tracks.near_since.setdefault(key, t_ns)
                    if t_ns - t0 >= 5_000_000_000 and ("meet", *key) not in self.reported and "meet" in self.vocab:
                        self.reported.add(("meet", *key))
                        mid = ((a.xy[0] + b.xy[0]) / 2, (a.xy[1] + b.xy[1]) / 2)
                        out.append(report("meet", [{"track_id": a.tid, "evidence": a.ev}, {"track_id": b.tid, "evidence": b.ev}], t0, t_ns,
                                          (mid[0], mid[1], self.ground_ref or 0.0), 0.6))
                else:
                    self.tracks.near_since.pop(key, None)
        vehicles = [tr for tr in self.tracks.live(t_ns, 1.0) if tr.cls == "vehicle"]
        for p in self.tracks.items:
            if p.cls != "person" or p.closed:
                continue
            age = (t_ns - p.t_last) / 1e9
            if age < LOST_S:
                continue
            near_v = [v for v in vehicles if math.dist(v.xy, p.xy) <= 3.0 + 1.0]
            if near_v and age >= 2.0 and ("board", p.tid) not in self.reported and "board" in self.vocab:
                v = near_v[0]
                self.reported.add(("board", p.tid))
                out.append(report("board", [{"track_id": p.tid, "evidence": p.ev}, {"track_id": v.tid, "evidence": v.ev}], p.t_last - 2_000_000_000,
                                  t_ns, (p.xy[0], p.xy[1], self.ground_ref or 0.0), 0.5))
                p.closed = True
                continue
            doors = [a for a in self.anchors if a["kind"] == "door" and math.dist(a["pos_enu_m"][:2], p.xy) <= 6.0]
            if doors and not near_v and age >= 3.0 and ("enter_building", p.tid) not in self.reported and "enter_building" in self.vocab:
                self.reported.add(("enter_building", p.tid))
                out.append(report("enter_building", [{"track_id": p.tid, "evidence": p.ev}], p.t_last - 3_000_000_000, t_ns,
                                  (p.xy[0], p.xy[1], self.ground_ref or 0.0), 0.5))
                p.closed = True
        return out[:4]

    # --------------------------------------------------------------- 几何工具
    def area_center(self) -> tuple[float, float]:
        xs, ys = zip(*self.area, strict=True)
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def target_tracks(self, t_ns: int, max_age_s: float = 3.0) -> list[Track]:
        return [tr for tr in self.tracks.live(t_ns, max_age_s) if any(rgb_match(tr.rgb, t) for t in self.targets)]
