"""`coverage_scan`（M26 §14.3）：任务区外接框按机数沿 x 切分为条带，割草机航线，航带间距按相机地面足迹 70% 重叠
（俯视足迹宽 2·h·tan(HFOV/2)）；云台近正下视（look-at 前方 10 m），分区固定、不通信。"""

from __future__ import annotations

import math

from drone4d.baselines.common import BaselineAgent

__all__ = ["CoverageScan"]


class CoverageScan(BaselineAgent):
    name = "coverage_scan"
    ARRIVE_M = 6.0

    def reset(self, config: dict) -> None:
        super().reset(config)
        cam = next(s for s in config["sensors"] if s["kind"] == "camera")
        hfov = 2 * math.atan((cam["width"] / 2) / cam["K"][0])
        foot = 2 * self.alt * math.tan(hfov / 2)
        spacing = max(10.0, foot * 0.3)
        xs, ys = zip(*self.area, strict=True)
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        fleet = sorted([self.uav, *self.peers])
        i, n = fleet.index(self.uav), len(fleet)
        w = (x1 - x0) / n
        sx0, sx1 = x0 + i * w, x0 + (i + 1) * w
        lanes = []
        x = sx0 + spacing / 2
        while x < sx1:
            lanes.append(x)
            x += spacing
        self.wps = [w_ for j, x in enumerate(lanes) for w_ in (((x, y0), (x, y1)) if j % 2 == 0 else ((x, y1), (x, y0)))]
        self.wi = 0

    def mission(self, obs, inbox):
        me = self.pos[:2]
        if math.dist(self.wps[self.wi], me) < self.ARRIVE_M:
            self.wi = (self.wi + 1) % len(self.wps)
        w = self.wps[self.wi]
        d = math.dist(w, me)
        ux, uy = ((w[0] - me[0]) / d, (w[1] - me[1]) / d) if d > 1e-6 else (0.0, 1.0)
        return w, (me[0] + 10.0 * ux, me[1] + 10.0 * uy)
