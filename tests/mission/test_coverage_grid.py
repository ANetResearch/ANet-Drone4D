"""覆盖栅格与立面网格（M10-FR-052、FR-067、FR-068；M10-AC-022 的功能部分；戳记耗时属性能用例，不在此断言）。"""

from __future__ import annotations

import math
import struct

import numpy as np
import pytest

from awr.sim.mission.coverage import CoverageGrid, FacadeGrid, blob_grid_u8


def _lawn(g: CoverageGrid, W: float, L: float, n_veh: int, x0: float, x1: float, y0: float, y1: float) -> None:
    ys = np.r_[np.arange(y0 + W / 2, y1 - W / 2, W * 0.9), y1 - W / 2]
    for k, y in enumerate(ys):
        for x in np.arange(x0, x1 + 1e-9, L * 0.5):
            g.stamp(np.array([x, y, 100.0]), 0.0, W, L, k % n_veh)


def test_grid_sizes_and_resolution() -> None:
    box = np.array([[0, 0], [1000, 0], [1000, 1000], [0, 1000]], float)
    g = CoverageGrid.for_polygon(box)
    assert g.w * g.h <= 65536 and g.res >= math.sqrt(1e6 / 65536)          # 内部栅格 ≤ 65536 格
    small = CoverageGrid.for_polygon(np.array([[0, 0], [100, 0], [100, 60], [0, 60]], float))
    assert small.res == 2.0


def test_stamp_owner_count_and_ratio() -> None:
    g = CoverageGrid.for_polygon(np.array([[0, 0], [400, 0], [400, 300], [0, 300]], float))
    assert g.ratio == 0.0
    n = g.stamp(np.array([200.0, 150.0, 80.0]), 0.0, 60.0, 40.0, 3)
    assert n > 0 and g.dirty
    j, i = int((150 - g.y0) / g.res), int((200 - g.x0) / g.res)
    assert g.owner[j, i] == 4 and g.count[j, i] == 1                     # owner = 序号 + 1
    g.stamp(np.array([200.0, 150.0, 80.0]), 0.0, 60.0, 40.0, 5)
    assert g.owner[j, i] == 4 and g.count[j, i] == 2                     # 首扫者保留，计数累加
    _lawn(g, 60.0, 40.0, 4, 0.0, 400.0, 0.0, 300.0)
    assert g.ratio >= 0.99


def test_snapshot_bounds_and_ratio_match() -> None:
    box = np.array([[0, 0], [1000, 0], [1000, 1000], [0, 1000]], float)
    g = CoverageGrid.for_polygon(box)
    _lawn(g, 80.0, 60.0, 8, 0.0, 600.0, 0.0, 1000.0)                      # 覆盖约 60%
    snap, k = g.snapshot()
    assert snap.size <= 16384 and snap.dtype == np.uint8 and k >= 2
    geo = g.snapshot_geom()
    assert (geo["w"], geo["h"], geo["k"]) == (snap.shape[1], snap.shape[0], k)
    assert geo["res_m"] == pytest.approx(g.res * k)
    # 抽取快照与内部栅格的覆盖率差 ≤ 1%（快照按 AOI 内格统计）
    ins = g.inside[::k, ::k][: snap.shape[0], : snap.shape[1]]
    r_snap = float(((snap != 0) & ins).sum()) / float(ins.sum())
    assert abs(r_snap - g.ratio) <= 0.01
    blob = blob_grid_u8(snap)
    assert len(blob) == 16 + snap.size <= 16 * 1024 + 16
    magic, (ver, dtype, count, comp) = blob[:4], struct.unpack("<HHII", blob[4:16])
    assert magic == b"AWRB" and (ver, dtype, count, comp) == (1, 3, snap.size, 1)


def test_facade_grid_stamp_geometry() -> None:
    fg = FacadeGrid(np.array([0.0, 0.0]), 27.0, 45.0, 120.0, 30.0)
    assert fg.n_ang == math.ceil(2 * math.pi * 27.0 / 2.0) and fg.n_z == math.ceil(75.0 / 2.0)
    cam = np.array([57.0, 0.0, 80.0])                                      # 立面外 30 m，看向中轴
    n = fg.stamp(cam, None)
    assert n > 0
    ang = (np.flatnonzero(fg.seen.any(0)) + 0.5) * fg.d_ang
    ang = np.where(ang > math.pi, ang - 2 * math.pi, ang)
    assert np.all(np.abs(ang) <= math.radians(60.0) + fg.d_ang)            # 入射角 ≤ 60°
    zs = 45.0 + (np.flatnonzero(fg.seen.any(1)) + 0.5) * 2.0
    assert zs.min() >= 80.0 - 30.0 * math.tan(fg.vfov / 2) - 2.0 and zs.max() <= 80.0 + 30.0 * math.tan(fg.vfov / 2) + 2.0
    assert fg.stamp(cam, None) == 0                                        # 已置位的格不重复计数
    for th in np.linspace(0, 2 * math.pi, 90, endpoint=False):
        for z in np.arange(50.0, 120.0, 10.0):
            fg.stamp(np.array([57.0 * math.cos(th), 57.0 * math.sin(th), z]), None)
    assert fg.ratio >= 0.95
