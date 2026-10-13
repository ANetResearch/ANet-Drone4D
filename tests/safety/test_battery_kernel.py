"""battery stage 的能量积分核与 numpy 实现逐位相同（M09-FR-050、FR-051；D2-FX-R2-sim 第 2 次，`safety/kernels.py:bat_step`）。

同一随机输入（推力、外部耗电、离地、初始电量跨 LOW/CRIT/EMERG 档）下，核路径与 numpy 路径的电池状态块逐字节相同，登记的
safety 事件与仲裁候选相同（核只在给出候选时执行判据，结果与每次都执行相同）。
"""

from __future__ import annotations

import numpy as np
import pytest
from safelib import UnitRig

from awr.contracts.enums import FlightState
from awr.sim.safety import battery as B

FS = FlightState
N = 12

pytestmark = pytest.mark.skipif(not B.KN.HAVE_NUMBA, reason="numba 不可用（AWR_KERNEL=numpy）")


def _rig(seed: int) -> UnitRig:
    r = UnitRig(n=N)
    rng = np.random.default_rng(seed)
    bb = r.bb
    for s in range(N):
        r.set(s, int(FS.FLYING), 0, air=bool(s % 5 != 4))
        e = float(bb["e_use_wh"][s])
        bb["wh_used"][s] = e * float(rng.choice([0.0, 0.5, 0.7, 0.78, 0.86, 0.9, 0.95]))
    bb["drain_pct_s"][::3] = 0.8
    return r


def _state(r: UnitRig) -> tuple:
    bb = r.bb
    blk = tuple((k, np.ascontiguousarray(v).tobytes()) for k, v in sorted(bb.items()) if isinstance(v, np.ndarray) and v.dtype != object)
    ev = tuple((e.slot, e.code, e.t_ns, e.value, e.threshold, e.detail) for e in r.rt.sink.pending)
    C = r.rt.fsm.C
    cand = tuple(np.ascontiguousarray(getattr(C, k)).tobytes() for k in ("key", "fs", "sub", "origin", "code", "value", "thr", "target", "t_ns"))
    return blk, ev, (cand, C.pending, sorted(C.detail.items()), list(C.losers))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_bat_step_matches_numpy(seed: int, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = _rig(seed), _rig(seed)
    rng = np.random.default_rng(100 + seed)
    for k in range(400):
        thr = rng.uniform(0.0, 1.2, N).astype(a.S.thrust.dtype)
        thr[rng.random(N) < 0.1] = -0.1
        air = rng.random(N) < 0.9
        inv = rng.random(N) < 0.02
        for r in (a, b):
            r.S.thrust[:N] = thr
            r.S.in_air[:N] = air
            r.bb["rtl_valid"][:N] &= ~inv
            r.ctx.tick += 25
            r.ctx.t_ns = r.ctx.tick * 4_000_000
            r.rt.begin(r.ctx)
        a.rt.bat.step(a.ctx)
        monkeypatch.setattr(B.KN, "HAVE_NUMBA", False)
        b.rt.bat.step(b.ctx)
        monkeypatch.setattr(B.KN, "HAVE_NUMBA", True)
        sa, sb = _state(a), _state(b)
        assert sa[0] == sb[0], (k, [x[0] for x, y in zip(sa[0], sb[0], strict=True) if x != y])
        assert sa[1] == sb[1] and sa[2] == sb[2], k
    assert any(e[1] for e in _state(a)[1])  # 判据确有触发（LOW、CRIT 等）
