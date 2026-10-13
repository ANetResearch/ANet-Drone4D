"""Hungarian 分配不经 `scipy.optimize` 包导入（D2-FX-R2；D2-AC-28/45）：直接装载 `_lsap` 扩展模块，结果（含并列代价时的选择）与
`scipy.optimize.linear_sum_assignment` 逐元素相同；首次调用不使进程装入 `scipy.optimize`。"""

from __future__ import annotations

import subprocess
import sys

import numpy as np

from awr.swarm.allocation.hungarian import hungarian


def test_matches_scipy_including_ties() -> None:
    from scipy.optimize import linear_sum_assignment

    rng = np.random.default_rng(7)
    for _ in range(300):
        n, k = sorted(int(x) for x in rng.integers(1, 14, 2))
        C = rng.integers(0, 4, (n, k)).astype(float) if rng.random() < 0.5 else rng.random((n, k))
        r, c = linear_sum_assignment(C)
        want = np.full(n, -1, np.int64)
        want[r] = c
        assert np.array_equal(hungarian(C), want)
    assert hungarian(np.zeros((0, 3))).shape == (0,)
    inf = np.array([[np.inf, 1.0], [2.0, np.inf]])
    assert hungarian(inf).tolist() == [1, 0]


def test_first_call_does_not_import_scipy_optimize() -> None:
    code = ("import sys, numpy as np; from awr.swarm.allocation.hungarian import hungarian; "
            "hungarian(np.array([[3.0, 1.0], [1.0, 3.0]])); print(int('scipy.optimize' in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
    assert out.stdout.strip().splitlines()[-1] == "0"
