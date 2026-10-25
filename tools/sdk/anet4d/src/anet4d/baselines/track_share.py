"""`track_share`（M26 §14.3）：从观察位出发，检测到与简报外观匹配的目标后转跟踪（后方站位，水平距 50 m、俯角约 55°），
丢失后按最后已知速度外推搜索 10 s 再回观察位；每秒广播一次目击（目标轨迹）与跟踪声明，他机已声明且更近的目标不抢；
声明方丢失且本机空闲且最近时接力（"最近空闲机"）；本机发出 report 后广播告知，他机在该类型的抑制时长内（缺省 30 s，
`common.SUPPRESS_S`；`vehicle_event` 按事件类型分键）不重复报同类型。

P0b：目标按类别与外观（上衣与下装颜色；车辆目标为车身颜色）比对；简报词表含 `reacquire` 或 `identity_link`（Re-ID、车辆追踪）时，
丢失的目标在 60 s 内仍作为跟踪对象：按最后已知速度外推至多 10 s，其后守在最后外推位置的后方站位等待目标重新出现。

跟踪高度（构造参数 `track_alt_agl_m`，ADR-217 决策 (1)）：给出时，有跟踪对象期间的目标高度改为该机队中心高度（按机分层），
无跟踪对象时回到巡航高度（`alt_agl_m`）；r6 §12.3 跟踪行为巡航 60 m、跟踪 45 m。"""

from __future__ import annotations

import math

from anet4d.agent import decode_msg, out_msg
from anet4d.baselines.common import suppress_ns
from anet4d.baselines.static_guard import StaticGuard

__all__ = ["TrackShare"]

STANDOFF_M = 50.0
SHARED_S, REACQ_HOLD_S = 10.0, 60.0


class TrackShare(StaticGuard):
    name = "track_share"
    CRUISE_MPS = 10.0

    def __init__(self, *, track_alt_agl_m: float | None = None, **kw) -> None:
        super().__init__(**kw)
        self.track_alt_agl_m = track_alt_agl_m

    def goal_alt(self) -> float:
        if self.track is not None and self.track_alt_agl_m is not None:
            return self.layered_alt(self.track_alt_agl_m)
        return self.alt

    def reset(self, config: dict) -> None:
        super().reset(config)
        self.ALT_AGL_M = None
        self.shared: dict[int, tuple[int, tuple[float, float], tuple[float, float], str]] = {}   # 目标号 → (t, xy, vel, 来源)
        self.claims: dict[int, tuple[int, str, float]] = {}                                     # 目标号 → (t, 机, 距离)
        self.track: int | None = None
        self.lost_at: tuple[int, tuple[float, float], tuple[float, float]] | None = None
        self.hold_s = REACQ_HOLD_S if {"reacquire", "identity_link"} & self.vocab else SHARED_S

    def on_inbox(self, inbox):
        for m in inbox:
            p = decode_msg(m)
            if not isinstance(p, dict):
                continue
            if p.get("kind") == "sight":
                for s in p.get("targets", []):
                    i = int(s["target"])
                    if i not in self.shared or self.shared[i][0] < s["t"]:
                        self.shared[i] = (int(s["t"]), tuple(s["xy"]), tuple(s["vel"]), m["src"])
            elif p.get("kind") == "claim":
                self.claims[int(p["target"])] = (int(p["t"]), m["src"], float(p["dist"]))
            elif p.get("kind") == "reported":                  # 他机已报：同类型在抑制时长内不再重复
                key = str(p["type"])
                self.suppress[key] = max(self.suppress.get(key, -1), int(p["t"]) + suppress_ns(key))
        return []

    def mission(self, obs, inbox):
        t = obs["t_ns"]
        me = self.pos[:2]
        mine = {}
        for tr in self.target_tracks(t, 2.0):
            i = self.target_no(tr)
            if i is not None:
                mine[i] = tr
                self.shared[i] = (t, tr.xy, tr.vel, self.uav)
        cand = []
        for i, (ts, xy, _v, _src) in self.shared.items():
            if (t - ts) / 1e9 > self.hold_s:
                continue
            dist = math.dist(xy, me)
            c = self.claims.get(i)
            if c and c[1] != self.uav and (t - c[0]) / 1e9 < 3.0 and (c[2], c[1]) < (dist, self.uav):
                continue
            cand.append((0 if i in mine else 1, dist, i))
        self.track = min(cand)[2] if cand else None
        if self.track is not None:
            ts, xy, vel, _src = self.shared[self.track]
            dt = (t - ts) / 1e9
            px, py = xy[0] + vel[0] * min(dt, 10.0), xy[1] + vel[1] * min(dt, 10.0)
            d = math.dist((px, py), me)
            ux, uy = ((me[0] - px) / d, (me[1] - py) / d) if d > 1e-6 else (1.0, 0.0)
            return (px + STANDOFF_M * ux, py + STANDOFF_M * uy), (px, py)
        return super().mission(obs, inbox)

    def messages(self, obs):
        out = [out_msg("*", "reported", {"kind": "reported", "type": ty, "t": t1}) for ty, t1 in self.just_reported]
        self.just_reported = []
        if obs["k"] % 10 != 0:
            return out
        t = obs["t_ns"]
        sights = [{"target": i, "t": ts, "xy": [round(xy[0], 1), round(xy[1], 1)], "vel": [round(v[0], 2), round(v[1], 2)]}
                  for i, (ts, xy, v, src) in sorted(self.shared.items()) if src == self.uav and (t - ts) / 1e9 < 2.0]
        if sights:
            out.append(out_msg("*", "sight", {"kind": "sight", "targets": sights}))
        if self.track is not None:
            xy = self.shared[self.track][1]
            out.append(out_msg("*", "claim", {"kind": "claim", "t": t, "target": self.track, "dist": round(math.dist(xy, self.pos[:2]), 1)}))
        return out
