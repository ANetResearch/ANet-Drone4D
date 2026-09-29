"""M13-AC-024：MID-360 花样（与 r04 参考逐点 ≤ 1e-6°、俯仰范围、1° 网格覆盖率与 r04 实测差 ≤ 5 个百分点）；M13-FR-050。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from awr.sim.sensors.lidar.pattern import NPF, mid360_pattern, mid360_reference

GOLD = json.loads((Path(__file__).parent / "golden" / "mid360.json").read_text())


def test_matches_reference_and_golden():
    az = np.empty(NPF)
    el = np.empty(NPF)
    for fr in GOLD["frames"]:
        mid360_pattern(fr["frame"], az, el)
        ra, re = mid360_reference(fr["frame"])
        assert np.max(np.abs((az - ra + 180) % 360 - 180)) <= 1e-6 and np.max(np.abs(el - re)) <= 1e-6
        idx = np.asarray(fr["idx"])
        assert np.max(np.abs((az[idx] - np.asarray(fr["az_deg"]) + 180) % 360 - 180)) <= 1e-6
        assert np.max(np.abs(el[idx] - np.asarray(fr["el_deg"]))) <= 1e-6
        assert el.min() >= -7.2 and el.max() <= 52.2


def test_coverage_curve_1deg_grid():
    az = np.empty(NPF)
    el = np.empty(NPF)
    cells = []
    for f in range(40):  # 4 s 并集为分母（r04 §3.1.2）
        mid360_pattern(f, az, el)
        cells.append(np.floor(az).astype(np.int64) * 1000 + np.floor(el + 10).astype(np.int64))
    union = np.unique(np.concatenate(cells)).size

    def cov(frames: int) -> float:
        return np.unique(np.concatenate(cells[:frames])).size / union

    measured = {1: 0.646, 5: 0.991, 10: 0.999}  # 0.1、0.5、1.0 s 的 r04 实测
    for n, m in measured.items():
        assert abs(cov(n) - m) <= 0.05, (n, cov(n), m)
