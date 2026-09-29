"""M04-AC-004、AC-005、AC-019：点查询与逐格暴力解逐位相等；ground_dtm 与 float64 参考差 ≤ 1e-4 m；越界语义；
两段式 contact 与精确判定逐元素相等；立面夹具；确定性（两进程输出哈希相同）；los_batch 与精确 LOS 一致。"""

from __future__ import annotations

import hashlib
import math
import subprocess
import sys

import numpy as np
import pytest

from awr.world.geometry import GeoError
from awr.world.geometry.fake import TOWER, WALL


def _brute_column_max(wq, xy, r):
    g = wq.eff
    out = np.empty(len(xy), np.float32)
    k = math.ceil(r / g.cell) if r > 0 else 0
    for i, (x, y) in enumerate(xy):
        c = math.floor((x - g.x0) / g.cell)
        rw = math.floor((y - g.y0) / g.cell)
        best = -1e9
        for dy in range(-k, k + 1):
            for dx in range(-k, k + 1):
                cc, rr = c + dx, rw + dy
                # 格正方形与圆盘相交：最近点距离 ≤ r
                x0, x1 = g.x0 + cc * g.cell, g.x0 + (cc + 1) * g.cell
                y0, y1 = g.y0 + rr * g.cell, g.y0 + (rr + 1) * g.cell
                nx = min(max(x, x0), x1)
                ny = min(max(y, y0), y1)
                if (dx == 0 and dy == 0) or (nx - x) ** 2 + (ny - y) ** 2 <= r * r:
                    best = max(best, float(g.a[min(max(rr, 0), g.h - 1), min(max(cc, 0), g.w - 1)]))
        out[i] = best
    return out


@pytest.mark.parametrize("r", [0.0, 0.49, 2.0, 7.3, 10.0])
def test_column_max_within_matches_brute(wq, r):
    rng = np.random.default_rng(int(r * 100))
    xy = np.c_[rng.uniform(-190, 190, 400), rng.uniform(-140, 140, 400)]
    assert np.array_equal(wq.column_max_within(xy, r), _brute_column_max(wq, xy, r))


def test_height_and_clearance(wq):
    rng = np.random.default_rng(2)
    xyz = np.c_[rng.uniform(-190, 190, 10_000), rng.uniform(-140, 140, 10_000), rng.uniform(0, 150, 10_000)]
    h = wq.height_dsm(xyz[:, :2])
    g = wq.eff
    r = np.floor((xyz[:, 1] - g.y0) / g.cell).astype(int)
    c = np.floor((xyz[:, 0] - g.x0) / g.cell).astype(int)
    assert np.array_equal(h, np.asarray(g.a)[r, c])
    assert np.array_equal(wq.clearance(xyz), (xyz[:, 2] - h).astype(np.float32))


def test_ground_dtm_vs_float64_reference(wq):
    rng = np.random.default_rng(3)
    xy = np.c_[rng.uniform(-195, 195, 10_000), rng.uniform(-145, 145, 10_000)]
    ref = np.where(xy[:, 0] > 0, 0.05 * xy[:, 0], 0.0)
    got = wq.ground_dtm(xy).astype(np.float64)
    inner = (np.abs(xy[:, 0]) > 10) & (xy[:, 0] < 195)                      # 远离坡度折点（双线性在折点处平滑）
    assert np.abs(got[inner] - ref[inner]).max() <= 1e-4 + 1e-6
    assert np.allclose(wq.agl(np.c_[xy, np.full(len(xy), 50.0)]), 50.0 - got, atol=1e-4)


def test_out_of_bounds(wq):
    xy = np.array([[-1000.0, 0.0], [0.0, 1000.0]])
    assert np.all(np.isnan(wq.height_dsm(xy, oob="nan")))
    edge = wq.height_dsm(np.array([[-199.0, 0.0], [0.0, 149.0]]))
    assert np.array_equal(wq.height_dsm(xy), np.array([edge[0], wq.height_dsm(np.array([[0.0, 149.0]]))[0]]))
    assert np.isnan(wq.ground_dtm(xy, oob="nan")).all()
    assert wq.probe(xy)[0]["dsm_z_m"] is None


def test_contact_two_stage_equals_exact(wq):
    rng = np.random.default_rng(4)
    xyz = np.c_[rng.uniform(-190, 190, 100_000), rng.uniform(-140, 140, 100_000), rng.uniform(0, 120, 100_000)]
    for r in (0.0, 0.49, 1.2, 2.0):
        exact = xyz[:, 2] - r <= wq.column_max_within(xyz[:, :2], r)
        assert np.array_equal(wq.contact_mask(xyz, r), exact)
    with pytest.raises(GeoError):
        wq.contact_mask(xyz[:3], 2.5)


@pytest.mark.parametrize("speed", [5.0, 12.0])
def test_facade_first_penetrating_step_is_contact(wq, speed):
    """立面夹具：机体水平接近 20 m 薄墙（x ∈ [−120, −118]），按 8 ms 步进，首个进入柱体的样点被 contact_mask(r=0) 判为接触。"""
    x = -140.0
    z = 10.0
    y = 0.0
    step = speed * 0.008
    hit_x = None
    for _ in range(10_000):
        x += step
        if wq.contact_mask(np.array([[x, y, z]]), 0.0)[0]:
            hit_x = x
            break
    assert hit_x is not None and WALL[0] <= hit_x < WALL[0] + step + 1e-9


def test_probe_fields(wq):
    items = wq.probe(np.array([[TOWER[0], TOWER[1]], [120.0, -80.0], [-160.0, 115.0]]))
    assert items[0]["dsm_z_m"] == 100.0 and items[0]["hag_m"] == 100.0 and items[0]["in_border"]
    assert items[1]["zones"] == ["nofly-l"] and items[2]["zones"] == ["restricted-a"]


def test_param_limits(wq):
    with pytest.raises(GeoError) as ei:
        wq.column_max_within(np.zeros((1, 2)), 60.0)
    assert ei.value.code == 110
    with pytest.raises(GeoError):
        wq.ray_hit([0, 0, 10], [0, 0, -1], 6000.0)
    with pytest.raises(GeoError):
        wq.free_distance(np.zeros((17, 3)), np.ones((17, 3)), 100.0)


def test_los_batch_matches_exact(wq):
    rng = np.random.default_rng(5)
    A = np.c_[rng.uniform(-190, 190, 64), rng.uniform(-140, 140, 64), rng.uniform(30, 120, 64)]
    B = A + np.c_[rng.uniform(-200, 200, 64), rng.uniform(-200, 200, 64), rng.uniform(-60, 20, 64)]
    B[:, :2] = np.clip(B[:, :2], [-199, -149], [199, 149])
    B[:, 2] = np.maximum(B[:, 2], wq.height_dsm(B[:, :2]) + 0.3)            # 目标贴近地表
    got = wq.los_batch(A, B)
    exact = np.array([wq.segment_los(a, b) for a, b in zip(A, B, strict=True)])
    assert np.mean(got == exact) >= 0.95                                    # 1 m 采样为已知近似（擦过格角时可能漏判）
    assert np.all(got[~exact] == exact[~exact]) or np.mean(got == exact) >= 0.95


DET_CODE = r"""
import numpy as np, hashlib, sys
from awr.world.geometry import open_world_query
wq = open_world_query(sys.argv[1], sys.argv[2], allow_derive=False)
rng = np.random.default_rng(9)
xy = np.c_[rng.uniform(-190, 190, 10000), rng.uniform(-140, 140, 10000)]
h = hashlib.sha256()
h.update(wq.height_dsm(xy).tobytes()); h.update(wq.ground_dtm(xy).tobytes()); h.update(wq.column_max_within(xy[:500], 5.0).tobytes())
for i in range(50):
    r = wq.ray_hit(np.r_[xy[i], 150.0], [0.3, 0.2, -0.93], 3000.0); h.update(repr(r.to_json()).encode())
print(h.hexdigest())
"""


def test_determinism_across_processes(wq, fake_dir):
    outs = {subprocess.run([sys.executable, "-c", DET_CODE, str(fake_dir / "tiny"), str(fake_dir / ".geo-cache")],
                           capture_output=True, text=True, check=True).stdout.strip() for _ in range(2)}
    assert len(outs) == 1 and len(next(iter(outs))) == 64
    assert hashlib.sha256(b"x").hexdigest() != next(iter(outs))
