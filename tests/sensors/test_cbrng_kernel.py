"""计数器 RNG 外积键核与 numpy 实现逐位相同（M13-FR-030；D2-FX-R2-sim 第 2 次，`sensors/kernels_cbrng.py`）。

`cbrng.normal` / `uniform` 在 `kernels_cbrng` 已导入（插件安装时的 warmup）且 agent、ch 为一维、tick 为标量时走 numba 外积键；
导入 `cbrng` 本身不引入 numba（M13-NFR-010，见 test_imports.py）。本用例在子进程中先以 numpy 路径求参考值，再导入核比较。
"""

from __future__ import annotations

import os
import subprocess
import sys

CODE = """
import sys
import numpy as np
from awr.sim.sensors import cbrng as C
assert 'numba' not in sys.modules
rng = np.random.default_rng(7)
cases = []
for _ in range(300):
    n = int(rng.integers(1, 7)); m = int(rng.integers(1, 9))
    seed = int(rng.integers(0, 2 ** 63)); st = int(rng.integers(0, 20)); tick = int(rng.integers(0, 10 ** 12))
    ag = rng.integers(-5, 1 << 20, n)
    ch = rng.integers(0, 1 << 25, m)
    if rng.random() < 0.2:
        ag = ag.astype(np.uint64)
    cases.append((seed, st, ag, tick, ch))
cases.append((2 ** 64 - 1, 3, np.array([0, 1023]), 0, np.array([0, (1 << 24) + 32767])))
ref = [(C.normal(*c).tobytes(), C.uniform(*c).tobytes(), C.normal(*c).shape) for c in cases]
C.warmup()
if not sys.modules['awr.sim.sensors.kernels_cbrng'].HAVE_NUMBA:
    print('skip'); raise SystemExit(0)
assert C._outer(1, 3, np.arange(2), 5, np.arange(3)) is not None
bad = sum((C.normal(*c).tobytes(), C.uniform(*c).tobytes(), C.normal(*c).shape) != r for c, r in zip(cases, ref))
print('mismatch', bad)
"""


def test_outer_kernel_matches_numpy() -> None:
    env = {k: v for k, v in os.environ.items() if k != "AWR_PLUGINS"}
    out = subprocess.run([sys.executable, "-c", CODE], capture_output=True, text=True, env=env, timeout=300, check=True).stdout
    assert out.strip().splitlines()[-1] in ("mismatch 0", "skip"), out
