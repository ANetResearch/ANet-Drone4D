"""`track_share`（M26 §14.3）：从观察位出发，检测到与简报外观匹配的目标后转跟踪（后方站位，水平距 50 m、俯角约 55°），
丢失后按最后已知速度外推搜索 10 s 再回观察位；每秒广播一次目击（目标轨迹）与跟踪声明，他机已声明且更近的目标不抢；
声明方丢失且本机空闲且最近时接力（"最近空闲机"）；本机发出 report 后广播告知，他机 30 s 内不重复报同类型。"""

from __future__ import annotations

import math

from drone4d.agent import decode_msg, out_msg
from drone4d.baselines.static_guard import StaticGuard

__all__ = ["TrackShare"]

STANDOFF_M = 50.0


class TrackShare(StaticGuard):
    name = "track_share"
    CRUISE_MPS = 10.0

    def reset(self, config: dict) -> None:
        super().reset(config)
        self.ALT_AGL_M = None
        self.shared: dict[int, tuple[int, tuple[float, float], tuple[float, float], str]] = {}   # 目标号 → (t, xy, vel, 来源)
        self.claims: dict[int, tuple[int, str, float]] = {}                                     # 目标号 → (t, 机, 距离)
        self.track: int | None = None
        self.lost_at: tuple[int, tuple[float, float], tuple[float, float]] | None = None

    def _target_no(self, rgb) -> int | None:
        for i, t in enumerate(self.targets):
            if t is not None and rgb is not None and math.dist(rgb, t) < 45.0:
                return i
        return None

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
            elif p.get("kind") == "reported":                  # 他机已报：同类型 30 s 内不再重复
                self.suppress[str(p["type"])] = max(self.suppress.get(str(p["type"]), -1), int(p["t"]) + 30_000_000_000)
        return []

    def mission(self, obs, inbox):
        t = obs["t_ns"]
        me = self.pos[:2]
        mine = {}
        for tr in self.target_tracks(t, 2.0):
            i = self._target_no(tr.rgb)
            if i is not None:
                mine[i] = tr
                self.shared[i] = (t, tr.xy, tr.vel, self.uav)
        cand = []
        for i, (ts, xy, _v, _src) in self.shared.items():
            if (t - ts) / 1e9 > 10.0:
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
