"""纯函数用例的小工具：单识别物视图与按斜距、俯视角放置的传感器位置。"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from awr.sim.targets import TargetConfig
from awr.sim.targets.model import TargetView


def view(cls: str = "person", *, vel: Any = (0.0, 0.0, 0.0), occlusion: float = 0.0, **over: Any) -> TargetView:
    over.setdefault("motion", {"mode": "static"})
    c = TargetConfig.from_dict({"class": cls, "pos": [0.0, 0.0, 0.0], **over}, f"{cls}-01")
    s = c.size_lwh
    return TargetView(1, np.array([0]), np.array([1]), np.zeros((1, 3)), np.zeros(1), np.array([vel], float), np.array([s]), np.array([occlusion]),
                      (c,), 0)


def at(r: float, eps_deg: float = 0.0, az_deg: float = 0.0) -> tuple[float, float, float]:
    e, a = math.radians(eps_deg), math.radians(az_deg)
    return (r * math.cos(e) * math.cos(a), r * math.cos(e) * math.sin(a), r * math.sin(e))
