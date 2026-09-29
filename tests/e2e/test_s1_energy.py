"""S1 几何、能量与间距的离线回归（M16-AC-016 的 S1 部分；M16-NFR-005；M16 §6.4.3；AWR-12 §5.8.5 预言值）。

风阻模型（`awr.datasets.scenarios.energy.helix_sortie`，移植 biz12/s1_check.py）在 World Package 地形上复算：
落地 SOC 0.36、0.32（±0.03）；P_hover 上浮 10% 时仍 ≥ 0.20；螺旋全程在 6 m 缓冲之外；两机同时扫描期间垂直间隔
≥ 150 m，全程最小三维间距 ≥ 12 m（预言 15.4 m）。1-D 模型给出最小余量比与落地 SOC 的下界（≥ 0.29、≥ 1.4）。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from awr.datasets.scenarios.authoring import s1_scenario
from awr.datasets.scenarios.energy import S1_PLAN, EnergyModel, WindProfile, WorldTerrain, helix_sortie, s1_segment_1d
from awr.datasets.scenarios.geometry import min_pairwise_separation, s1_tracks

ORACLE_SOC = {"p600-01": 0.36, "p600-02": 0.32}


@pytest.fixture(scope="module")
def terrain(request):
    from e2ehelp import _world_query, needs_world

    needs_world("shenzhen")
    return WorldTerrain(_world_query("shenzhen"))


def _sortie(terrain, vid: str, m: EnergyModel):
    z0, z1 = S1_PLAN[vid]["z"]
    return helix_sortie(terrain, S1_PLAN["center"], S1_PLAN["radius_m"], S1_PLAN[vid]["home"], z0, z1,
                        S1_PLAN["dz_nom_m"], S1_PLAN["speed_mps"], WindProfile.from_scenario(s1_scenario()), m, vid)


def test_plan_matches_scenario_file() -> None:
    d = s1_scenario()
    v = {x["vehicle_id"]: x for x in d["vehicles"]}
    for m in d["missions"]:
        vid = m["vehicle_ids"][0]
        assert tuple(m["params"]["z_range_m"]) == S1_PLAN[vid]["z"]
        assert tuple(v[vid]["home_enu_m"][:2]) == S1_PLAN[vid]["home"]
        assert tuple(m["params"]["center_enu_m"]) == S1_PLAN["center"] and m["params"]["radius_m"] == S1_PLAN["radius_m"]


@pytest.mark.needs_data
@pytest.mark.parametrize("vid", ["p600-01", "p600-02"])
def test_s1_energy_drag_model(terrain, vid: str) -> None:
    m = EnergyModel.from_vehicle()
    assert m.e_use_wh == pytest.approx(188.7, abs=0.05)                                         # ADR-052 可用口径
    r = _sortie(terrain, vid, m)
    assert r.soc_land == pytest.approx(ORACLE_SOC[vid], abs=0.03)
    assert r.soc_land >= 0.29 and r.precheck_ok and r.energy_rtl_fired is None                  # M16-NFR-005
    assert r.extra["revs"] == (11 if vid == "p600-01" else 8)
    assert r.extra["dz_m"] < 2 * 30 * math.tan(math.radians(42.1) / 2)                          # 立面垂直覆盖 23.09 m
    hot = _sortie(terrain, vid, m.scaled(1.10))                                                 # ADR-043 辨识容差
    assert hot.precheck_soc is not None and hot.precheck_soc >= 0.20


@pytest.mark.parametrize("vid", ["p600-01", "p600-02"])
def test_s1_energy_1d_lower_bound(vid: str) -> None:
    z0, z1 = S1_PLAN[vid]["z"]
    r, _ = s1_segment_1d(z0, z1, S1_PLAN["speed_mps"], 0.2, S1_PLAN["radius_m"], m=EnergyModel(wind_drag=False))
    assert r.soc_land >= 0.29 and r.min_margin >= 1.4 and r.energy_rtl_fired is None


@pytest.mark.needs_data
def test_s1_helix_clears_the_tower(terrain) -> None:
    """M16 §6.4.3 几何：以形心 (−162.2, 77.3)、半径 57 m 的螺旋在 z ∈ [50, 391] m 不进入 6 m 缓冲障碍。"""
    c, r = S1_PLAN["center"], S1_PLAN["radius_m"]
    a = np.linspace(0.0, 2 * math.pi, 720, endpoint=False)
    xy = np.stack([c[0] + r * np.cos(a), c[1] + r * np.sin(a)], axis=1)
    tops = terrain.tops(xy)
    for z in (50.0, 100.0, 150.0, 250.0, 350.0, 391.0):
        assert float(np.mean(tops >= z)) == 0.0, z


@pytest.mark.needs_data
def test_s1_separation(terrain) -> None:
    t, tr = s1_tracks(terrain, S1_PLAN)
    d, _pair, k = min_pairwise_separation(tr)
    assert d >= 12.0 and d == pytest.approx(15.4, abs=1.5), (d, t[k])
    both = (t > 150) & (t < 600)
    vs = np.abs(tr["p600-01"][both, 2] - tr["p600-02"][both, 2])
    assert vs.min() >= 150.0 and vs.max() <= 165.0
    assert max(p[:, 2].max() for p in tr.values()) <= 424.05                                   # border max_z_m
