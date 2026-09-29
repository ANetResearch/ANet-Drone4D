"""剧本能量回归（M16-FR-025；M16-AC-016；M16-NFR-005；SCN-E001）。

S2–S6 以 1-D 悬停功率模型（可用口径 188.7 Wh）复算 M16 §6.4.4–§6.4.7 的架次：落地 SOC ≥ 0.25、最小余量比 ≥ 1.4，
与 `.cache/research/m16/scenario_final.json` 的预言值一致（±0.01）。运行时能量预检（M10 `energy_precheck`，119）由
`test_scenarios.py` 的真实运行覆盖（`energy_precheck = reject` 的剧本若预检失败会以 119 拒绝任务）。ladder 为 `warn`。
"""

from __future__ import annotations

import pytest

from awr.datasets.scenarios.energy import SX_LEGS, EnergyModel, simulate_sortie

pytestmark = pytest.mark.ext


@pytest.mark.parametrize("key", sorted(SX_LEGS))
def test_sx_sortie_energy(key: str) -> None:
    spec = SX_LEGS[key]
    r = simulate_sortie(spec["legs"], EnergyModel(wind_drag=False), key)
    assert r.soc_land == pytest.approx(spec["soc_land"], abs=0.01)
    assert r.soc_land >= 0.25, "SCN-E001 ENERGY_MARGIN_LOW"
    assert r.energy_rtl_fired is None
    if "margin" in spec:
        assert r.min_margin >= 1.4 and r.min_margin == pytest.approx(spec["margin"], abs=0.05)


def test_rejected_variants_stay_rejected() -> None:
    """M16 §1.4：12 §7.3 草稿的 S5（300 × 400 m）在可用口径下落地 SOC 0.183 < 0.25（因此改为 300 × 300 m）。"""
    r = simulate_sortie([("up", 150), ("h", 300, 5, 300), ("h", 3900, 5, 300)], EnergyModel(wind_drag=False))
    assert r.soc_land < 0.25


def test_vehicle_package_constants() -> None:
    m = EnergyModel.from_vehicle()
    assert m.mass_kg == pytest.approx(3.5) and m.p_hover_w == pytest.approx(515.0) and m.e_use_wh == pytest.approx(188.7)
