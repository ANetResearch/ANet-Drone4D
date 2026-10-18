"""`hover_plaza`（r6 §12.3 `hover1_plaza`，M26-FR-081）：全部机体守同一个观察位——简报中第一个区域型（`kind = zone`）公开锚点
（缺省取任务区中心），高度取任务包络中值并按机分层；到位后与 `static_guard` 相同，斜视站位（候选点西南 30 m）并以候选点为中心
按 3 × 3 网格（±20 m）每 2 s 切换一次视点扫视。不跟踪、不通信。"""

from __future__ import annotations

from drone4d.baselines.static_guard import StaticGuard

__all__ = ["HoverPlaza"]


class HoverPlaza(StaticGuard):
    name = "hover_plaza"

    def reset(self, config: dict) -> None:
        super().reset(config)
        zones = [a for a in self.anchors if a["kind"] == "zone"]
        self.post = tuple(zones[0]["pos_enu_m"][:2]) if zones else self.area_center()
        self.stand = (self.post[0] - 30.0, self.post[1] - 30.0)
