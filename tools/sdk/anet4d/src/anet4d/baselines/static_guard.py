"""`static_guard`（M26 §14.3）：以公开锚点与任务区中心为候选观察位，按贪心最近分配（每机一个），高度取任务包络中值；
到位后云台以候选点为中心按 3 × 3 网格（±20 m）每 2 s 切换一次视点扫视。广播与通信不使用。

P0b 目标搜索：简报给出搜索先验（`brief.search`）时，候选观察位改为先验中心（gmm 分量按权重降序、grid 为质量最高且相距 ≥ 40 m
的格心，`anet4d.baselines.prior`），第 i 架机（按 uav id 排序）取第 i mod n 个。

注视方式（构造参数 `gaze`，ADR-217 决策 (1)）：`sweep`（缺省）为上述 3 × 3 扫视；`fixed` 为到位后云台固定注视候选点（r6 §12.3
hover 行的设置，不扫视）。"""

from __future__ import annotations

import math

from anet4d.baselines.common import BaselineAgent

__all__ = ["StaticGuard"]

SWEEP = [(0, 0), (20, 0), (20, 20), (0, 20), (-20, 20), (-20, 0), (-20, -20), (0, -20), (20, -20)]
GAZES = ("sweep", "fixed")


class StaticGuard(BaselineAgent):
    name = "static_guard"

    def __init__(self, *, gaze: str = "sweep", **kw) -> None:
        if gaze not in GAZES:
            raise ValueError(f"gaze must be one of {GAZES}, got {gaze!r}")
        super().__init__(**kw)
        self.gaze = gaze

    def reset(self, config: dict) -> None:
        super().reset(config)
        cands = [tuple(a["pos_enu_m"][:2]) for a in self.anchors] + [self.area_center()]
        fleet = sorted([self.uav, *self.peers])
        start = config["start"]["nav_pos_enu_m"][:2]
        i = fleet.index(self.uav)
        order = sorted(range(len(cands)), key=lambda j: (math.dist(cands[j], start), j))
        self.post = cands[order[i % len(cands)]] if len(fleet) <= len(cands) else cands[i % len(cands)]
        centers = self.prior.centers(len(fleet)) if self.prior is not None else []
        if centers:
            self.post = centers[i % len(centers)]
        self.stand = (self.post[0] - 30.0, self.post[1] - 30.0)          # 斜视站位：俯角约 60°

    def mission(self, obs, inbox):
        if self.gaze == "fixed":
            return self.stand, self.post
        j = (obs["k"] // 20) % len(SWEEP)
        dx, dy = SWEEP[j]
        return self.stand, (self.post[0] + dx, self.post[1] + dy)
