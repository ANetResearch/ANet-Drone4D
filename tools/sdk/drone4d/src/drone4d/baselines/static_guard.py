"""`static_guard`（M26 §14.3）：以公开锚点与任务区中心为候选观察位，按贪心最近分配（每机一个），高度取任务包络中值；
到位后云台以候选点为中心按 3 × 3 网格（±20 m）每 2 s 切换一次视点扫视。广播与通信不使用。"""

from __future__ import annotations

import math

from drone4d.baselines.common import BaselineAgent

__all__ = ["StaticGuard"]

SWEEP = [(0, 0), (20, 0), (20, 20), (0, 20), (-20, 20), (-20, 0), (-20, -20), (0, -20), (20, -20)]


class StaticGuard(BaselineAgent):
    name = "static_guard"

    def reset(self, config: dict) -> None:
        super().reset(config)
        cands = [tuple(a["pos_enu_m"][:2]) for a in self.anchors] + [self.area_center()]
        fleet = sorted([self.uav, *self.peers])
        start = config["start"]["nav_pos_enu_m"][:2]
        i = fleet.index(self.uav)
        order = sorted(range(len(cands)), key=lambda j: (math.dist(cands[j], start), j))
        self.post = cands[order[i % len(cands)]] if len(fleet) <= len(cands) else cands[i % len(cands)]
        self.stand = (self.post[0] - 30.0, self.post[1] - 30.0)          # 斜视站位：俯角约 60°

    def mission(self, obs, inbox):
        j = (obs["k"] // 20) % len(SWEEP)
        dx, dy = SWEEP[j]
        return self.stand, (self.post[0] + dx, self.post[1] + dy)
