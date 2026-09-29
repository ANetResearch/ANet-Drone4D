"""生成器安全（M10-FR-027；M10-AC-013，缩减样本）：六城中心区 lawnmower（盒长 = 城市短边 40%，fly_over）
经 plan-pool 生成作业（圆角、TOPP-lite、B-spline、`path_valid` 细校验）必须成功；轨迹按 0.5 s 采样没有低于 Height_map
的点。缺少已构建世界时 skip。"""

from __future__ import annotations

import numpy as np
import pytest

from awr.sim.planning import bspline as BS
from awr.sim.planning.jobs import PlanRequest
from awr.sim.planning.worker import set_world, worker_main

pytestmark = [pytest.mark.slow, pytest.mark.needs_data]

CITIES = ("shenzhen", "shanghai", "newyork", "sanfrancisco", "suzhou", "chicago")


def _hm_at(w, xy: np.ndarray) -> np.ndarray:
    g = w.hm
    c = np.clip(np.floor((xy[:, 0] - g.x0) / g.cell).astype(np.int64), 0, g.a.shape[1] - 1)
    r = np.clip(np.floor((xy[:, 1] - g.y0) / g.cell).astype(np.int64), 0, g.a.shape[0] - 1)
    return np.asarray(g.a, np.float64)[r, c]


@pytest.mark.parametrize("city", CITIES)
def test_center_lawnmower_is_safe(city: str) -> None:
    from geo_city import open_city

    w = open_city(city)
    key = (w.world_id, w.content_version, w.coordinate_sha256)
    set_world(key, w)
    g = w.hm
    W = g.a.shape[1] * g.cell
    H = g.a.shape[0] * g.cell
    cx, cy = g.x0 + W / 2, g.y0 + H / 2
    half = 0.4 * min(W, H) / 2
    box = [[cx - half, cy - half], [cx + half, cy - half], [cx + half, cy + half], [cx - half, cy + half]]
    home = [cx - half - 20.0, cy - half - 20.0, float(w.ground_dtm(np.array([[cx - half - 20.0, cy - half - 20.0]]))[0])]
    payload = {"generator": "lawnmower", "vehicles": [{"vehicle_id": "p600-01", "home_enu_m": home, "pos_enu_m": home}],
               "params": {"polygon_enu_m": box, "altitude": {"mode": "fly_over", "agl_m": 80, "clearance_m": 10},
                          "speed_mps": 8.0},
               "constraints": {}, "zones": ["border"], "mission_id": f"safety-{city}"}
    req = PlanRequest(f"gen:{city}", "generator", key, ("p600-01",), payload, {"v_max_mps": 12.0}, 1, 0, 20000, city)
    res = worker_main(req)
    assert res.ok, (city, res.status, res.detail, res.stats)
    for tr in res.trajectories:
        Q = np.asarray(tr["ctrl_pts"], np.float64)
        T = BS.duration(Q, tr["ts_s"])
        S = BS.eval_bspline(Q, tr["ts_s"], np.arange(0.0, T, 0.5))
        top = _hm_at(w, S[:, :2])
        assert float((S[:, 2] - top).min()) >= -0.05, city            # 航线上没有低于 Height_map 的点
    assert res.items and len(res.items) >= 1
