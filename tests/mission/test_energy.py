"""能量预检（M10-FR-004；M10-AC-014；AWR-12 §5.8.4、§5.8.5）。

以 S1 定稿参数在深圳世界上计算落地 SOC（兜底能量模型与 12 §5.8.5 复核脚本同一公式；M09 登记 EnergyModel 后改用其
`path_wh`）：p600-01 0.36 ± 0.03、p600-02 0.32 ± 0.03；x01 原参数（峰值点为心、半径 45 m、Δz 9.24 m、4 m/s）两机都不可行，
detail 列出缺口 Wh。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from awr.sim.mission import energy as EN

ROOT = Path(__file__).resolve().parents[2]


def test_fallback_model_hover_and_climb() -> None:
    ep = EN.EnergyParams(p_hover_w=515.0, omega_sum=4008.0)
    hover = np.c_[np.arange(0, 61.0), np.zeros((61, 6))]
    assert EN.fallback_path_wh(ep, hover, EN.WindProfile()) == pytest.approx(515.0 * 60 / 3600, rel=1e-9)
    climb = EN.vertical_samples(np.zeros(3), 30.0, 3.0)
    wh = EN.fallback_path_wh(ep, climb, EN.WindProfile())
    assert wh == pytest.approx((515.0 + 3.5 * 9.80665 * 3.0 / 0.5) * 10 / 3600, rel=1e-6)
    windy = EN.fallback_path_wh(ep, hover, EN.WindProfile(6.0, 135.0), np.full(61, -200.0))
    assert windy > EN.fallback_path_wh(ep, hover, EN.WindProfile())


def test_rtl_profile_rule() -> None:
    smp, z = EN.rtl_samples(np.array([100.0, 0, 50]), np.array([0.0, 0, 0]), 120.0, EN.WindProfile(), 0.0)
    assert z == pytest.approx(125.0)                          # H_top + 5 m
    assert smp[0, 3] == pytest.approx(50) and smp[-1, 3] == pytest.approx(0.0) and np.allclose(smp[-1, 1:3], 0)


@pytest.mark.needs_data
@pytest.mark.slow
def test_s1_precheck_soc() -> None:
    from harness import Sim

    from awr.world.geometry.query import open_world_query

    if not (ROOT / "worlds/shenzhen/world.json").exists():
        pytest.skip("shenzhen not built")
    w = open_world_query(ROOT / "worlds/shenzhen", allow_derive=False)
    doc = json.loads((ROOT / "packages/contracts/fixtures/scenario/s1-shenzhen-facade.json").read_text(encoding="utf-8"))
    doc["zones"] = {"active": ["border"]}
    for m in doc["missions"]:
        m["start"] = {"at_s": 1000}          # 只做预检，不启动
    sim = Sim(w, n=1, profile_id="p600_mid360", spawn_xy=(87.1, 12.5))
    try:
        sim.advance(0.2)
        assert sim.rt.load_scenario("s1-shenzhen-facade", doc=doc)["code"] == 0
        assert sim.until(lambda: len(sim.rt.missions.missions) == 2 and all(
            m.gen == "ok" for m in sim.rt.missions.missions.values()), 20.0)
        soc = {}
        for m in sim.rt.missions.missions.values():
            per, ok = sim.rt.missions.energy_precheck(m)
            assert ok, per
            soc[per[0]["id"]] = per[0]["soc_after"]
        assert soc["p600-01"] == pytest.approx(0.36, abs=0.03)
        assert soc["p600-02"] == pytest.approx(0.32, abs=0.03)
        # x01 原参数：两机都不可行，detail 列出缺口 Wh
        eng = sim.rt.missions
        for vid, z0, z1 in (("p600-01", 10, 195), ("p600-02", 190, 391)):
            eng.create({"mission_id": f"x01-{vid}", "generator": "helix_scan", "vehicle_ids": [vid],
                        "params": {"center_enu_m": [-162.0, 98.5], "radius_m": 45, "standoff_m": 30,
                                   "z_range_m": [z0, z1], "dz_per_rev_m": 9.24, "speed_mps": 4.0}}, "scenario")
        assert sim.until(lambda: all(eng.missions[f"x01-{v}"].gen in ("ok", "failed") for v in ("p600-01", "p600-02")),
                         20.0)
        for v in ("p600-01", "p600-02"):
            m = eng.missions[f"x01-{v}"]
            if m.gen != "ok":          # 峰值点为心、半径 45 m 的螺旋穿入塔体：生成即被细校验拒绝（几何不成立）
                assert m.gen_error["code"] in (102, 125)
                continue
            r = eng.start(m.mid)
            assert r["code"] == 119 and r["detail"]["vehicles"][0]["deficit_wh"] > 0
    finally:
        sim.close()
