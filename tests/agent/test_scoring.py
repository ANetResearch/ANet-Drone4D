"""M14-AC-015：打分（d05 组 0.573、−0.420、−∞；S3 组 0.101、0.008、−∞；同分 agent_no 小者优先；风险在 MOR 10 km 时随风速单调）。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from awr.agent.capabilities.manifest import vehicle_data
from awr.agent.runtime.scoring import (
    EnvAtTarget,
    Limits,
    ScoreNorm,
    Weights,
    conf_expected_fallback,
    norm_for,
    rank_key,
    risk_of,
    score,
)

GOLD = json.loads((Path(__file__).parent / "golden" / "score_vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("g", GOLD["groups"], ids=[g["name"] for g in GOLD["groups"]])
def test_golden_groups(g: dict) -> None:
    n = ScoreNorm(**g["norm"])
    for r in g["rows"]:
        u = score(r, n, Weights(), g["risk"])
        if r["expect"] is None:
            assert u == -math.inf
        else:
            assert abs(u - r["expect"]) <= 1e-3


def test_norms_by_profile() -> None:
    p600 = norm_for(vehicle_data("p600_mid360").params)
    assert p600.eta_s == pytest.approx(200.0) and p600.energy_wh == pytest.approx(18.87)
    x500 = norm_for(vehicle_data("x500").params)
    assert x500.eta_s == pytest.approx(120.0) and x500.energy_wh is None
    assert norm_for({"anet": {"score_norm": {"eta_s": 150, "energy_wh": None}}}) == ScoreNorm(150.0, None)


def test_tie_break_agent_no() -> None:
    rows = [(0.2, 5), (0.2, 2), (-math.inf, 1), (0.3, 9)]
    assert sorted(rows, key=lambda r: rank_key(*r)) == [(0.3, 9), (0.2, 2), (0.2, 5), (-math.inf, 1)]


def test_risk_per_term_clamped() -> None:
    lim = Limits(12.0, 10.0, 200.0)
    rs = [risk_of(EnvAtTarget(w, 0.0, 10_000.0), lim) for w in (0, 3, 6, 9, 12, 20)]
    assert rs == sorted(rs) and rs[0] == 0.0 and rs[2] == pytest.approx(0.25) and rs[-1] == pytest.approx(0.5)
    assert risk_of(EnvAtTarget(0.0, 5.0, 200.0), lim) == pytest.approx(0.15 + 0.2)
    assert conf_expected_fallback(0.95, 150.0, 60.0, 20_000.0) == pytest.approx(0.80, abs=0.005)
