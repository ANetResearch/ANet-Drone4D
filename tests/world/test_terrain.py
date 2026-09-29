"""M03-AC-009（栅格部分）：DTM 开运算与填补、DSM 最大值与 DTM 填补、观测数、可分离双线性与逐点公式逐位相同。"""

from __future__ import annotations

import warnings

import numpy as np
from m03_common import load

from awr.world.terrain.dsm import dsm_index, dsm_max, raw_top
from awr.world.terrain.dtm import dtm_opening, fill_nan_4mean
from awr.world.terrain.grids import CellIndex, Grid, bilinear_on_centres, grid_reduce, read_grid


def _fill_ref(dtm: np.ndarray, it: int = 300) -> np.ndarray:
    """g03 `dtm_opening` 的填补循环（参考实现）。"""
    for _ in range(it):
        nanm = np.isnan(dtm)
        if not nanm.any():
            break
        p = np.pad(dtm, 1, constant_values=np.nan)
        nb = np.stack([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]])
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            fill = np.nanmean(nb, axis=0)
        dtm = np.where(nanm & ~np.isnan(fill), fill, dtm)
    return dtm


def test_fill_matches_g03_bitwise():
    rng = np.random.default_rng(1)
    a = rng.normal(0, 5, (120, 90))
    a[rng.random(a.shape) < 0.6] = np.nan
    a[40:80, 20:70] = np.nan
    assert np.array_equal(fill_nan_4mean(a.copy()), _fill_ref(a.copy()), equal_nan=True)


def test_grid_reduce_min_max():
    iy = np.array([0, 0, 1, 1, 1])
    ix = np.array([0, 0, 2, 2, 0])
    v = np.array([3.0, 1.0, 5.0, 7.0, 2.0])
    mn = grid_reduce(iy, ix, v, "min", np.inf, 2, 3)
    mx = grid_reduce(iy, ix, v, "max", -np.inf, 2, 3)
    assert mn[0, 0] == 1.0 and mn[1, 2] == 5.0 and mn[1, 0] == 2.0 and np.isinf(mn[0, 1])
    assert mx[1, 2] == 7.0


def test_dtm_opening_removes_buildings():
    rng = np.random.default_rng(2)
    x = rng.uniform(0, 500, 50_000)
    y = rng.uniform(0, 500, 50_000)
    z = 0.01 * x
    b = (x > 200) & (x < 260) & (y > 200) & (y < 260)
    z[b] += 40                                          # 60 m 楼（开运算 9×9 格 = 90 m 窗口可去除）
    dtm, _gi = dtm_opening(np.c_[x, y, z])
    assert dtm.shape == (int(np.floor(500 / 10)), int(np.floor(500 / 10))) or dtm.shape[0] >= 49
    assert np.nanmax(np.abs(dtm - 0.01 * (np.arange(dtm.shape[1]) * 10)[None, :])) < 1.0
    assert not np.isnan(dtm).any()


def test_dsm_max_fill_and_counts():
    rng = np.random.default_rng(3)
    E = np.c_[rng.uniform(0, 100, 5000), rng.uniform(0, 60, 5000), rng.uniform(0, 30, 5000)]
    E[:, 2][(E[:, 0] > 40) & (E[:, 0] < 60)] = 50.0
    ci = dsm_index(E, 2.0)
    top = raw_top(ci, E[:, 2])
    dtm = Grid(np.zeros((7, 11), np.float32), (0.0, 0.0), 10.0)
    dsm, cnt = dsm_max(top, ci, (float(E[:, 0].min()), float(E[:, 1].min())), 2.0, dtm)
    assert dsm.a.shape == top.shape == cnt.shape
    assert np.all(dsm.a >= 0.0)
    obs = np.isfinite(top)
    assert np.array_equal(dsm.a[obs], np.maximum(top[obs], 0.0))
    assert np.all(dsm.a[~obs] == 0.0) and np.all(cnt[~obs] == 0)
    assert int(cnt.sum()) == min(5000, int(cnt.sum())) and cnt.max() <= 255


def test_bilinear_on_centres_matches_pointwise():
    rng = np.random.default_rng(4)
    src = Grid(rng.normal(0, 3, (23, 31)).astype(np.float32), (-7.0, 3.0), 10.0)
    org = (-7.0, 3.0)
    H, W = 115, 155
    t = bilinear_on_centres(src, org, 2.0, H, W)
    cx = org[0] + (np.arange(W) + 0.5) * 2.0
    cy = org[1] + (np.arange(H) + 0.5) * 2.0
    X, Y = np.meshgrid(cx, cy)
    assert np.array_equal(t, src.bilinear(X, Y))
    assert np.array_equal(bilinear_on_centres(src, org, 2.0, H, W, rows=(10, 30)), t[10:30])


def test_cell_index_counts():
    x = np.array([0.1, 0.2, 2.5, 3.9, 5.0])
    y = np.array([0.1, 0.3, 0.2, 2.1, 5.0])
    ci = CellIndex.build(x, y, (0.0, 0.0), 2.0)
    c = ci.counts()
    assert c[0, 0] == 2 and c[0, 1] == 1 and c[1, 1] == 1 and c.sum() == 5


def test_tiny_world_grids(tiny_built):
    d = tiny_built["dir"]
    c = load(d / "coordinate.json")
    tsc, dtm = read_grid(d / "geometry/terrain/dtm_10m.json")
    dsc, dsm = read_grid(d / "geometry/terrain/dsm_2m.json")
    nsc, n = read_grid(d / "geometry/terrain/dsm_2m_n.json")
    hi = c["extent"]["max"]
    assert tsc["originXY"] == dsc["originXY"] == nsc["originXY"]
    assert dsm.shape == n.shape == (int((hi[1] - dsc["originXY"][1]) // 2) + 1, int((hi[0] - dsc["originXY"][0]) // 2) + 1)
    assert abs(float(dsm.max()) - hi[2]) <= 1e-3
    assert nsc["kind"] == "occupancy" and nsc["dtype"] == "uint8" and nsc["scale"] == 1.0 and nsc["offset"] == 0.0
    t = bilinear_on_centres(Grid(dtm, tuple(tsc["originXY"]), 10.0), tuple(dsc["originXY"]), 2.0, *dsm.shape)
    assert np.all(dsm >= t - 0.01)
    assert np.all(np.abs(dsm[n == 0] - t[n == 0]) <= 0.01)
    assert int(n.astype(np.int64).sum()) <= 200_000
