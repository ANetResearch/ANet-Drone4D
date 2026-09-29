"""SIH 黄金数据回归（M08-AC-005；D1-AC-12；M08-FR-016 至 FR-021、FR-030、FR-075、FR-076；g08 §9.2–§9.3）。

参数化配置矩阵（M08 §6.13.3）：A linear（x500_sih）l1_every 1、B linear 2、C composite（x500）1、D composite 2（产品默认形态）、
E composite 2 numpy oracle —— 17 项指标全部落在容差内且测试用 FastGuard 事件 0；反例 F（composite，`time_stretch` 与
`stop_motion` 关闭）必须**不通过**（GoTo RMSE_x ≈ 0.633 m > 0.50 m，证明测试有判别力）。
黄金数据：`tests/golden/sih_x500_px4-1.18rc1/`（inst0 offboard 阶跃、inst1 DO_REPOSITION、inst2 8 m/s 风）；Mock 48 ms 采样、右移 0.12 s。
"""

from __future__ import annotations

import pytest
from simlib import TOL, evaluate

CONFIGS = [
    pytest.param("x500_sih", 1, "numba", id="A-linear-l1every1"),
    pytest.param("x500_sih", 2, "numba", id="B-linear-l1every2"),
    pytest.param("x500", 1, "numba", id="C-composite-l1every1"),
    pytest.param("x500", 2, "numba", id="D-composite-l1every2"),
    pytest.param("x500", 2, "numpy", id="E-composite-oracle"),
]


def test_tolerance_table_has_17_metrics() -> None:
    assert sum(len(v) for v in TOL.values()) == 17


@pytest.mark.parametrize(("profile", "l1_every", "kernel"), CONFIGS)
def test_sih_parity(profile: str, l1_every: int, kernel: str) -> None:
    _ref, row, fails = evaluate(profile, l1_every=l1_every, kernel=kernel)
    assert not fails, (fails, row)


def test_counterexample_f_must_fail() -> None:
    _ref, row, fails = evaluate("x500", l1_every=2, time_stretch=False, stop_motion=False)
    assert any(f.startswith("goto.rmse_x") for f in fails), (fails, row)
    assert row["goto"]["rmse_x"] > 0.5
