"""放置（M18-AC-004；M18-FR-005；17 §17.6.5）：贴地误差 ≤ 0.5 m（DSM 对拍）；屋顶放置成立；非法放置各返回 540 与 detail.why；
uav 类高度越界 540；random_walk 缺省区域（以起点为中心 100 m 见方）；更新位置重新校验。"""

from __future__ import annotations

import numpy as np
import pytest
from tlib import Bench, FakeWorld

W = FakeWorld(ground=12.0, boxes=[(50.0, 70.0, 50.0, 70.0, 25.0)], half=500.0)


def test_ground_and_roof() -> None:
    with Bench(0, W) as b:
        rng = np.random.default_rng(1)
        for _ in range(10):
            x, y = rng.uniform(-400, 400, 2)
            tid = b.add({"class": "person", "pos": [float(x), float(y)], "motion": {"mode": "static"}})
            assert abs(b.T(tid).ms.pos[2] - float(W.height_dsm([[x, y]])[0])) <= 0.5
        roof = b.add({"class": "object", "pos": [60.0, 60.0, None]})
        assert b.T(roof).ms.pos[2] == pytest.approx(37.0)
        ok = b.add({"class": "object", "pos": [0.0, 0.0, 12.3]})   # 给出的 z 在 DSM ± 0.5 m 内：贴 DSM
        assert b.T(ok).ms.pos[2] == pytest.approx(12.0)


@pytest.mark.parametrize(("target", "why"), [
    ({"class": "person", "pos": [900.0, 0.0]}, "outside_world"),
    ({"class": "object", "pos": [60.0, 60.0, 15.0]}, "inside_obstacle"),
    ({"class": "object", "pos": [0.0, 0.0, 14.0]}, "step_too_high"),
    ({"class": "person", "pos": [0.0, 0.0], "motion": {"mode": "random_walk", "region": [[10, 10], [40, 10], [40, 40], [10, 40]]}},
     "region_excludes_start"),
    ({"class": "person", "pos": [0.0, 0.0], "motion": {"mode": "random_walk", "region": [[-3, -3], [3, -3], [3, 3], [-3, 3]]}}, "region_area"),
    ({"class": "dog", "pos": [0.0, 0.0], "motion": {"mode": "random_walk", "region": [[-600, -600], [600, -600], [600, 600], [-600, 600]]}},
     "outside_world"),
    ({"class": "vehicle", "pos": [0.0, 0.0], "motion": {"mode": "path", "path": [[0, 0, None], [800, 0, None]]}}, "outside_world"),
])
def test_invalid_placement_540(target, why) -> None:
    with Bench(0, W) as b:
        r = b.cmd("target/add", {"target": target})
        assert r["status"] == "rejected" and r["code"] == 540 and r["detail"]["why"] == why, r


def test_uav_altitude_out_of_range() -> None:
    with Bench(0, W) as b:
        r = b.cmd("target/add", {"target": {"class": "uav", "pos": [0.0, 0.0], "alt_agl_m": 400}})
        assert r["status"] == "rejected" and r["code"] == 540 and r["detail"]["field"] == "alt_agl_m"
        tid = b.add({"class": "uav", "pos": [0.0, 0.0], "alt_agl_m": 40})
        assert b.T(tid).ms.pos[2] == pytest.approx(52.0)


def test_default_region_and_update_revalidates() -> None:
    with Bench(0, W) as b:
        tid = b.add({"class": "person", "pos": [100.0, -100.0]})
        reg = b.rt.cfg_dict(b.T(tid))["motion"]["region"]
        assert reg == [[50.0, -150.0], [150.0, -150.0], [150.0, -50.0], [50.0, -50.0]]
        r = b.cmd("target/update", {"id": tid, "patch": {"pos": [1000.0, 0.0]}})
        assert r["status"] == "rejected" and r["code"] == 540
        r = b.cmd("target/update", {"id": tid, "patch": {"pos": [110.0, -90.0], "motion": {"mode": "static"}}})
        assert r["status"] == "accepted" and np.allclose(b.T(tid).ms.pos, [110.0, -90.0, 12.0])
        assert r["result"]["cfg"]["cfg_rev"] == 2 and b.col("tgt_cfg_rev", tid) == 2
