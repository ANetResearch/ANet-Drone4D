"""编队槽位（M10-FR-041；M10-AC-012 formation 部分、AC-018；r26 §3.5）。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from awr.swarm.formation import SHAPES, formation_slots, rotate_z

GOLDEN = Path(__file__).resolve().parents[2] / "apps" / "web" / "tests" / "mission" / "golden" / "formation.json"


def test_shapes_match_r26_formulas() -> None:
    s, al = 12.0, math.radians(35.0)
    line = formation_slots("line", 5, s, virtual=False)
    assert np.allclose(line[:, 1], [0, 12, -12, 24, -24]) and np.allclose(line[:, 0], 0)
    col = formation_slots("column", 4, s, virtual=False)
    assert np.allclose(col[:, 0], [0, -12, -24, -36])
    v = formation_slots("V", 5, s, virtual=False)
    r = np.array([0, 1, 1, 2, 2])
    side = np.array([0, 1, -1, 1, -1])
    assert np.allclose(v[:, 0], -r * s * math.cos(al)) and np.allclose(v[:, 1], side * r * s * math.sin(al))
    ech = formation_slots("echelon", 3, s, virtual=False)
    assert np.allclose(ech[:, 0], [0, -s * math.cos(al), -2 * s * math.cos(al)])
    circ = formation_slots("circle", 5, s, virtual=False)
    R = max(s, s / (2 * math.sin(math.pi / 4)))
    assert np.allclose(np.linalg.norm(circ[1:, :2], axis=1), R) and np.allclose(circ[0], 0)
    grid = formation_slots("grid", 6, s, cols=3, virtual=False)
    assert np.allclose(grid[0], 0) and grid.shape == (6, 3)


@pytest.mark.parametrize("shape", SHAPES)
def test_virtual_anchor_centroid_zero(shape: str) -> None:
    off = formation_slots(shape, 7, 6.0)
    assert np.allclose(off.mean(0), 0.0, atol=1e-12)
    d = np.linalg.norm(off[:, None, :2] - off[None, :, :2], axis=-1)
    d[np.diag_indices(7)] = np.inf
    assert d.min() >= 6.0 * math.sin(math.radians(35.0)) - 1e-9 or shape in ("v", "echelon")


def test_rotate_z() -> None:
    r = rotate_z(np.array([1.0, 0.0, 2.0]), math.pi / 2)
    assert np.allclose(r, [0.0, 1.0, 2.0])


def test_bad_shape() -> None:
    with pytest.raises(ValueError):
        formation_slots("star", 3, 5.0)


def test_ts_golden_is_current() -> None:
    """apps/web/tests/mission/golden/formation.json 与当前 Python 参考实现一致（TS 对拍的基准，FR-042）。"""
    g = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for c in g["slots"]:
        off = formation_slots(c["shape"], c["n"], c["spacing_m"], c["half_angle_deg"], c["cols"])
        assert np.allclose(off.ravel(), c["offsets"], atol=1e-9)
