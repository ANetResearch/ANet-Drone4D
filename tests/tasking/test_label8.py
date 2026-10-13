"""GSD 扫描补拍聚类的 8 邻接连通域标记（`awr.tasking.scan._label8`，D2-FX-R2）：不导入 scipy，结果与
`scipy.ndimage.label(mask, structure=np.ones((3, 3)))` 逐元素相同（标号按光栅扫描中首个像素的先后连续编号）。"""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from awr.tasking.scan import _label8


def test_matches_scipy_on_random_masks() -> None:
    ndimage = pytest.importorskip("scipy.ndimage")
    rng = np.random.default_rng(20261007)
    for _ in range(200):
        h, w = (int(x) for x in rng.integers(1, 48, 2))
        m = rng.random((h, w)) < float(rng.choice([0.03, 0.2, 0.45, 0.7, 0.95]))
        a, na = _label8(m)
        b, nb = ndimage.label(m, structure=np.ones((3, 3), int))
        assert na == nb and np.array_equal(a, b)


def test_edge_cases() -> None:
    assert _label8(np.zeros((0, 0), bool))[1] == 0
    assert _label8(np.zeros((4, 5), bool))[1] == 0
    lab, n = _label8(np.eye(5, dtype=bool))                      # 对角相邻 = 同一域
    assert n == 1 and (lab[np.eye(5, dtype=bool)] == 1).all()
    m = np.array([[1, 0, 1], [0, 0, 0], [1, 0, 1]], bool)
    lab, n = _label8(m)
    assert n == 4 and lab[0, 0] == 1 and lab[0, 2] == 2 and lab[2, 0] == 3 and lab[2, 2] == 4


def test_does_not_import_scipy() -> None:
    code = ("import sys, numpy as np; from awr.tasking.scan import _label8; _label8(np.ones((3, 3), bool)); "
            "print(int('scipy.ndimage' in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
    assert out.stdout.strip().splitlines()[-1] == "0"
