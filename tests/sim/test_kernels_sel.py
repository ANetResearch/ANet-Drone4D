"""kernels_sel 与原 numpy 表达式逐位相同（D2-AC-08 小机群固定开销；M08-NFR-201）。"""

from __future__ import annotations

import numpy as np
import pytest

from awr.sim.fleet import kernels_sel as KS

pytestmark = pytest.mark.skipif(not KS.HAVE_NUMBA, reason="numba unavailable")


@pytest.mark.parametrize("n", [1, 4, 64, 1024])
def test_selection_kernels_match_numpy(n: int) -> None:
    rng = np.random.default_rng(n)
    for _ in range(20):
        active = rng.random(n) < 0.4
        fid = rng.integers(0, 32, n).astype(np.uint8)
        out = np.empty(n, np.int32)
        k = KS.sel_bits(active, fid, 3, 16, out)
        ref = np.flatnonzero(active & ((fid & 3) != 0) & ((fid & 16) == 0)).astype(np.int32)
        assert np.array_equal(out[:k], ref) and out[:k].dtype == ref.dtype

        fs = rng.integers(0, 12, n).astype(np.uint8)
        sub = rng.integers(0, 6, n).astype(np.uint8)
        o8 = np.empty(n, np.uint8)
        KS.rtl_phase(fs, sub, 7, o8)
        assert np.array_equal(o8, np.where(fs == 7, sub, 0).astype(np.uint8))

        assert KS.any_bits(fid, 16) == bool((fid & 16).any())
        assert KS.any_bits(np.zeros(n, np.uint8), 16) is False

        kind = (rng.random(n) < 0.3).astype(np.uint8) * rng.integers(1, 5, n).astype(np.uint8)
        mode = rng.integers(13, 17, n).astype(np.uint8)
        k = KS.sel_eq_nonzero(kind, mode, 15, out)
        idx = np.flatnonzero(kind != 0)
        assert np.array_equal(out[:k], idx[mode[idx] == 15].astype(np.int32))

        at = np.flatnonzero(active).astype(np.int64)
        lc, sh = rng.integers(0, 3, n).astype(np.uint8), rng.integers(0, 3, n).astype(np.uint8)
        assert KS.any_ne_at(at, lc, sh) == bool((lc[at] != sh[at]).any())
        assert KS.any_nonzero_at(at, done0 := (rng.random(n) < 0.1).astype(np.uint8)) == bool(done0[at].any())
        assert KS.any_eq_at(at, fs, 7) == bool((fs[at] == 7).any())
        assert KS.any_bits_at(at, fid, 16) == bool(((fid[at] & 16) != 0).any())

        done = (rng.random(n) < 0.2).astype(np.uint8)
        sel = out[:k].copy()
        o2 = np.empty(sel.size, np.int32)
        m = KS.take_nonzero(sel, done, o2)
        assert np.array_equal(o2[:m], sel[done[sel] != 0])

        g = rng.integers(0, 3, (n, 2)).astype(np.uint8)
        bi, bk = np.empty(g.size, np.int64), np.empty(g.size, np.int64)
        m = KS.nonzero2(g, bi, bk)
        ri, rk = np.nonzero(g)
        assert np.array_equal(bi[:m], ri) and np.array_equal(bk[:m], rk)
        f = rng.choice([np.nan, 1.0, 2.0, np.inf], (n, 2))
        fc = rng.choice([np.nan, 1.0, 2.0], (n, 2))
        m = KS.unequal_finite2(f, fc, bi, bk)
        ri, rk = np.nonzero(np.isfinite(f) & np.isfinite(fc) & (f != fc))
        assert np.array_equal(bi[:m], ri) and np.array_equal(bk[:m], rk)
        band = rng.choice([255, 0, 1, 2], n).astype(np.uint8)
        ob = np.empty(n, np.int64)
        m = KS.sel_ne2_active(band, 255, 1, active, ob)
        assert np.array_equal(ob[:m], np.flatnonzero((band != 255) & (band != 1) & active))


def test_l1_helpers_use_kernels_and_match() -> None:
    from awr.contracts.enums import FlightState
    from awr.sim.fleet.stages import l1 as L1
    from awr.sim.fleet.state import FleetState

    S = FleetState(64)
    rng = np.random.default_rng(3)
    S.active[:] = rng.random(64) < 0.5
    S.fidelity[:] = rng.integers(0, 32, 64)
    S.blocks["safety"] = {"fs": rng.integers(0, 12, 64).astype(np.uint8), "sub": rng.integers(0, 5, 64).astype(np.uint8)}
    f = S.fidelity
    ref = np.flatnonzero(S.active & ((f & 3) != 0) & ((f & 16) == 0)).astype(np.int32)
    assert np.array_equal(L1.l1_indices(S), ref)
    sb = S.blocks["safety"]
    assert np.array_equal(L1.rtl_phase_of(S), np.where(sb["fs"] == int(FlightState.RTL), sb["sub"], 0).astype(np.uint8))
