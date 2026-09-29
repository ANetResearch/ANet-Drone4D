"""CAPT（M10-FR-044、FR-058；M10-AC-018；r26 §3.6）。"""

from __future__ import annotations

import itertools
import json
import math
import time
from pathlib import Path

import numpy as np
import pytest

from awr.swarm.formation import SHAPES, capt_assign, capt_duration, capt_plan, capt_positions, formation_slots
from awr.swarm.formation.capt import min_sep_linear, min_sep_samples, smoothstep5

GOLDEN = Path(__file__).resolve().parents[2] / "apps" / "web" / "tests" / "mission" / "golden" / "formation.json"


def test_assignment_matches_bruteforce() -> None:
    rng = np.random.default_rng(1)
    for _ in range(20):
        P = rng.uniform(-50, 50, (5, 3))
        G = rng.uniform(-50, 50, (5, 3))
        a = capt_assign(P, G)
        best = min(itertools.permutations(range(5)), key=lambda p: ((P - G[list(p)]) ** 2).sum())
        assert np.isclose(((P - G[a]) ** 2).sum(), ((P - G[list(best)]) ** 2).sum())


def test_golden_capt() -> None:
    g = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for c in g["capt"]:
        P = np.asarray(c["P"]).reshape(-1, 3)
        G = np.asarray(c["G"]).reshape(-1, 3)
        a = capt_assign(P, G)
        assert a.tolist() == c["assign"]


def test_duration_rule() -> None:
    P = np.zeros((2, 3))
    G = np.array([[60.0, 0, 0], [0, 1, 0]])
    assert capt_duration(P, G, 10.0) == pytest.approx(60.0 / 6.0)
    assert capt_duration(P, P + 1.0, 10.0) == 4.0
    assert smoothstep5(np.array([0.0, 0.5, 1.0])).tolist() == [0.0, 0.5, 1.0]


def test_nine_six_metre_reshapes_keep_2sqrt2_rsafe() -> None:
    """9 机 6 m 间距，6 种队形两两互变 30 组：虚拟锚点下最小间距 ≥ 4.2 m（2√2·R_safe，R_safe = 1.5 m）。"""
    worst = math.inf
    for a, b in itertools.permutations(SHAPES, 2):
        P = formation_slots(a, 9, 6.0, cols=3)
        G = formation_slots(b, 9, 6.0, cols=3)
        idx = capt_assign(P, G)
        worst = min(worst, min_sep_linear(P, G[idx], 400))
    assert worst >= 4.2 - 1e-6


def test_line_to_v_direct_and_line_to_column_staggered() -> None:
    """5 机 12 m：横队变 V 形预测最小间距 ≥ 10 m 直接执行；横队变纵队（8.49 m）改为错层并复核 ≥ 10 m。"""
    P = formation_slots("line", 5, 12.0)
    plan = capt_plan(P, formation_slots("v", 5, 12.0), 12.0, min_sep_m=10.0)
    assert not plan.staggered and plan.d_min_m >= 10.0 and plan.feasible
    G = formation_slots("column", 5, 12.0)
    plan = capt_plan(P, G, 12.0, min_sep_m=10.0)
    assert plan.d_min_m == pytest.approx(12 / math.sqrt(2), abs=0.05)
    assert plan.staggered and plan.feasible and plan.d_min_final_m >= 10.0
    X = capt_positions(P, G[plan.assign], plan, np.linspace(0, plan.total_s, 600))
    assert min_sep_samples(X) >= 10.0 - 1e-6
    assert np.allclose(X[-1], G[plan.assign], atol=1e-9)


@pytest.mark.perf
def test_capt_n50_under_5ms() -> None:
    rng = np.random.default_rng(3)
    P = rng.uniform(-200, 200, (50, 3))
    G = rng.uniform(-200, 200, (50, 3))
    capt_assign(P, G)
    t0 = time.perf_counter()
    for _ in range(20):
        capt_assign(P, G)
    assert (time.perf_counter() - t0) / 20 <= 0.005
