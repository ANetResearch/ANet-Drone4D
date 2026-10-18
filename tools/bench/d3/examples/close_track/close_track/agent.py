"""`close_track` Agent：track_share 的观察、跟踪、共享与 report 规则不变，巡航高度取 45 m AGL（任务包络 30–120 m 内）。

容器入口：`python -m drone4d.serve close_track.agent:Agent`。
"""

from __future__ import annotations

from drone4d.baselines import TrackShare

__all__ = ["Agent"]

CRUISE_AGL_M = 45.0


class Agent(TrackShare):
    name = "close_track"

    def reset(self, config: dict) -> None:
        super().reset(config)
        lo, hi = config["brief"]["area"]["alt_agl_range_m"]
        fleet = sorted([self.uav, *self.peers])
        layer = 8.0 * (fleet.index(self.uav) - 0.5 * (len(fleet) - 1))     # 与基线相同的高度分层，避免机间碰撞
        self.alt = min(max(CRUISE_AGL_M + layer, float(lo) + 2.0), float(hi) - 2.0)
