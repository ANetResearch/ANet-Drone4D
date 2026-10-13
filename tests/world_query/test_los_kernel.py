"""M04-AC-202（D2）：`los_batch` 的 numba 核（AWR-04 §4.2 第 4 条、§6.3；D2-MS2 必做项）。

- 与 numpy 参考实现 `los_batch_np` 逐位一致：随机批（0–64 对）、多种步长与 eps、退化对（A = B、2·eps ≥ L）、出界射线、
  贴地目标、非有限输入（退回 numpy 路径，行为不变）；七个世界（needs_data）各 10⁴ 对感知几何；
- 签名恒定：不同输入布局（列表、float32、非连续视图）只产生一个 numba 签名（预热签名覆盖运行期，D1 验收第 1 轮 4.1）；
- 未导入 numba 的进程不因 `los_batch` 导入 numba（走 numpy 路径，结果相同）；`AWR_KERNEL=numpy` 关闭核；
- 上限与参数校验不变（> 64 对、step ≤ 0 为 110）。
"""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest
from geo_m04_common import WORLDS

from awr.world.geometry import GeoError, open_world_query, warm_kernels
from awr.world.geometry import kernels as K

SEVEN = ["synthcity", "shenzhen", "suzhou", "newyork", "chicago", "shanghai", "sanfrancisco"]


@pytest.fixture(scope="module")
def kwq(wq):
    assert warm_kernels() and K.is_ready()
    wq.kernel_ready = True
    return wq


def _rand_batch(rng, wq, n, ground=False):
    A = np.c_[rng.uniform(-260, 260, n), rng.uniform(-200, 200, n), rng.uniform(-5, 140, n)]
    B = A + np.c_[rng.uniform(-300, 300, n), rng.uniform(-300, 300, n), rng.uniform(-100, 50, n)]
    if ground:
        B[:, 2] = np.maximum(B[:, 2], wq.height_dsm(B[:, :2]) + rng.uniform(0, 1, n))
    return A, B


def test_matches_numpy_reference(kwq):
    rng = np.random.default_rng(20261005)
    total = 0
    for it in range(400):
        A, B = _rand_batch(rng, kwq, int(rng.integers(0, 65)), ground=it % 3 == 0)
        step = float(rng.choice([1.0, 2.0, 0.5, 3.7, 0.25]))
        eps = float(rng.choice([0.5, 0.0, 2.0, 0.1]))
        got = kwq.los_batch(A, B, step, eps)
        ref = kwq.los_batch_np(A, B, step, eps)
        assert got.dtype == np.bool_ and got.shape == ref.shape
        assert np.array_equal(got, ref), (it, step, eps)
        total += len(A)
    assert total > 10_000


def test_edge_cases(kwq):
    A = np.array([[0.0, 0.0, 50.0], [-40.0, -20.0, 50.0], [-150.0, 20.0, 60.0], [-500.0, 0.0, 10.0], [-130.0, 0.0, 10.0],
                  [-40.0, -20.0, 300.0]])
    B = np.array([[0.0, 0.0, 50.0], [-40.0, -20.0, 50.5], [-150.0, 100.0, 0.05], [500.0, 0.0, 10.0], [-110.0, 0.0, 10.0],
                  [-40.0, -20.0, 1.0]])
    got = kwq.los_batch(A, B)
    assert np.array_equal(got, kwq.los_batch_np(A, B))
    assert got[0] and got[1] and not got[4] and not got[5]            # 退化对、2·eps ≥ L 可见；薄墙与塔顶遮挡
    for eps in (0.0, 5.0, 1000.0):
        assert np.array_equal(kwq.los_batch(A, B, 1.0, eps), kwq.los_batch_np(A, B, 1.0, eps))
    empty = kwq.los_batch(np.zeros((0, 3)), np.zeros((0, 3)))
    assert empty.shape == (0,) and empty.dtype == np.bool_


def test_non_finite_inputs_fall_back(kwq):
    A = np.array([[0.0, 0.0, 50.0], [np.nan, 0.0, 50.0], [0.0, np.inf, 10.0]])
    B = np.array([[10.0, 0.0, 50.0], [10.0, 0.0, 50.0], [10.0, 0.0, 10.0]])
    with np.errstate(all="ignore"):
        assert np.array_equal(kwq.los_batch(A, B), kwq.los_batch_np(A, B))
        assert np.array_equal(kwq.los_batch(A[:1], B[:1], float("nan")), kwq.los_batch_np(A[:1], B[:1], float("nan")))


def test_limits_unchanged(kwq):
    with pytest.raises(GeoError) as ei:
        kwq.los_batch(np.zeros((65, 3)), np.ones((65, 3)))
    assert ei.value.code == 110
    with pytest.raises(GeoError):
        kwq.los_batch(np.zeros((2, 3)), np.ones((2, 3)), step_m=0.0)
    with pytest.raises(GeoError):
        kwq.los_batch(np.zeros((2, 3)), np.ones((3, 3)))


def test_single_signature_for_input_layouts(kwq):
    rng = np.random.default_rng(1)
    A, B = _rand_batch(rng, kwq, 32)
    wide = np.c_[A, np.zeros(32)]
    variants = [(A, B), (A.tolist(), B.tolist()), (A.astype(np.float32), B.astype(np.float32)), (wide[:, :3], B),
                (np.asfortranarray(A), B[::1])]
    for a, b in variants:
        assert np.array_equal(kwq.los_batch(a, b), kwq.los_batch_np(a, b))
    assert len(K._los_kernel.signatures) == 1, K._los_kernel.signatures


def test_writeable_dsm_grid_uses_same_signature(kwq):
    """DSM 以可写数组构造的世界（测试替身）也以只读视图进入核，签名不变。"""
    ro = K.readonly(np.ones((4, 4), np.float32))
    assert not ro.flags.writeable
    out = K.los_batch(np.array([[1.0, 1.0, 5.0]]), np.array([[7.0, 7.0, 0.5]]), ro, 0.0, 0.0, 2.0, 1.0, 0.5)
    assert out is not None and not out[0] and len(K._los_kernel.signatures) == 1


_NO_NUMBA = r"""
import sys
from pathlib import Path
import numpy as np
from awr.world.geometry import open_world_query
wq = open_world_query(Path(sys.argv[1]), Path(sys.argv[2]), allow_derive=False)
A = np.array([[-130.0, 0.0, 10.0], [0.0, 0.0, 50.0]]); B = np.array([[-110.0, 0.0, 10.0], [10.0, 0.0, 50.0]])
r = wq.los_batch(A, B)
print(int("numba" in sys.modules), int(wq.kernel_ready), r.tolist())
"""


def test_no_numba_import_without_need(fake_dir):
    out = subprocess.run([sys.executable, "-c", _NO_NUMBA, str(fake_dir / "tiny"), str(fake_dir / ".geo-cache")],
                         capture_output=True, text=True, check=True).stdout.split(maxsplit=2)
    assert out[0] == "0" and out[1] == "0" and out[2].strip() == "[False, True]"


_FORCE_NUMPY = r"""
import sys
import numba  # noqa: F401  进程已导入 numba，但 AWR_KERNEL=numpy 关闭核
from pathlib import Path
from awr.world.geometry import open_world_query
from awr.world.geometry import kernels as K
wq = open_world_query(Path(sys.argv[1]), Path(sys.argv[2]), allow_derive=False)
print(int(K.HAVE_NUMBA), int(wq.kernel_ready))
"""


def test_awr_kernel_numpy_disables(fake_dir):
    import os

    env = dict(os.environ, AWR_KERNEL="numpy")
    out = subprocess.run([sys.executable, "-c", _FORCE_NUMPY, str(fake_dir / "tiny"), str(fake_dir / ".geo-cache")],
                         capture_output=True, text=True, check=True, env=env).stdout.split()
    assert out == ["0", "0"]


@pytest.mark.needs_data
@pytest.mark.skipif(not all((WORLDS / c / "world.json").exists() for c in SEVEN), reason="七个世界未构建（make worlds）")
@pytest.mark.parametrize("city", SEVEN)
def test_seven_worlds_bitwise(city):
    """感知几何（斜距 100–500 m、对地目标顶部、中心、底部）与随机方向射线各 5000 对，结果逐位一致。"""
    warm_kernels()
    wq = open_world_query(WORLDS / city, allow_derive=False)
    assert wq.kernel_ready
    rng = np.random.default_rng(7)
    b = wq.bounds_m
    for it in range(160):
        n = 64
        if it % 2 == 0:
            T = np.c_[rng.uniform(b[0, 0], b[1, 0], n), rng.uniform(b[0, 1], b[1, 1], n)]
            T = np.c_[T, wq.ground_dtm(T) + 1.8 * rng.choice([0.95, 0.5, 0.05], n)]
            az = rng.uniform(0, 2 * np.pi, n)
            r = rng.uniform(100, 500, n)
            A = np.c_[T[:, 0] + r * np.cos(az), T[:, 1] + r * np.sin(az), T[:, 2] + rng.uniform(40, 150, n)]
            B = T
        else:
            A = np.c_[rng.uniform(b[0, 0], b[1, 0], n), rng.uniform(b[0, 1], b[1, 1], n), rng.uniform(5, 250, n)]
            d = rng.normal(size=(n, 3))
            B = A + d / np.linalg.norm(d, axis=1)[:, None] * rng.uniform(1, 800, (n, 1))
        step = 1.0 if it % 4 < 2 else 2.0
        assert np.array_equal(wq.los_batch(A, B, step), wq.los_batch_np(A, B, step)), (city, it)
