"""V0.2 `GeoClient`（sim-core 侧非阻塞调用 geo-worker 的 `svc/geo/{raycast,los,lidar}`）。D1 为桩（M04 §9.1）。"""

from __future__ import annotations


class GeoClient:
    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError("GeoClient（geo-worker 客户端）在 V0.2 提供")
