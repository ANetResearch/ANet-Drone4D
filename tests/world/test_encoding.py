"""M03-AC-017：ANET_Q16 编码精度与 16 §4.3 示例点 12 字节 golden；baked-height 五档字节（15 `--pc-ramp-0…4`）。

（M03 PRD 把 golden 用例写在 tests/contracts/test_anet_q16.py，该目录属 M00；本文件是 M03 自测副本。）
"""

from __future__ import annotations

import numpy as np

from awr.world.pointcloud.encode import BAKED_HEIGHT_RAMP, baked_height, oct16_decode, oct16_encode, quantize_node


def test_oct16_accuracy_1m_random():
    rng = np.random.default_rng(1)
    v = rng.normal(size=(1_000_000, 3))
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    w = oct16_encode(v)
    assert not np.any(w == 0)
    d = oct16_decode(w)
    ang = np.degrees(np.arccos(np.clip((d * v).sum(1), -1, 1)))
    assert ang.mean() <= 0.35 and np.percentile(ang, 99) <= 0.8 and ang.max() <= 1.0


def test_oct16_zero_reserved():
    assert oct16_decode(np.array([0], np.uint16)).tolist() == [[0.0, 0.0, 0.0]]
    w = oct16_encode(np.array([[0.0, 0.0, -1.0]]))
    assert w[0] != 0 and np.allclose(oct16_decode(w), [[0, 0, -1]], atol=1e-6)


def test_golden_shenzhen_example_bytes():
    """16 §4.3：深圳根节点，p = (−162.0, 98.5, 374.0)，法线 +Z，屋顶类别 5，zP1/zP99 = −7.3/124.71。"""
    nmin = np.array([-924.0189514160156, -999.5227966308594, -16.924943923950195])
    ns = 1999.0455962607646
    p = np.array([[-162.0, 98.5, 374.0]])
    pos = np.empty((1, 4), "<u2")
    pos[:, :3] = quantize_node(p, nmin, ns)
    pos[:, 3] = oct16_encode(np.array([[0.0, 0.0, 1.0]]))
    col = np.empty((1, 4), np.uint8)
    col[:, :3] = baked_height(p[:, 2], -7.3, 124.71)
    col[:, 3] = 5
    assert (pos.tobytes() + col.tobytes()).hex(" ") == "95 61 9d 8c 10 32 80 80 f2 f3 f5 05"
    dec = nmin + pos[0, :3].astype(float) / 65535 * ns
    assert np.all(np.abs(dec - p[0]) <= ns / 65535 / 2)


def test_baked_height_ramp_endpoints():
    z = np.array([-10.0, 0.0, 50.0, 100.0, 1000.0])
    rgb = baked_height(z, 0.0, 100.0)
    assert rgb[0].tolist() == rgb[1].tolist() == BAKED_HEIGHT_RAMP[0].astype(int).tolist() == [29, 31, 35]
    assert rgb[3].tolist() == rgb[4].tolist() == [242, 243, 245]
    ts = np.array([0.0, 0.25, 0.5, 0.75, 1.0]) ** (1 / 0.6) * 100.0
    assert baked_height(ts, 0.0, 100.0).tolist() == BAKED_HEIGHT_RAMP.astype(int).tolist()


def test_node_quantisation_error_bound():
    rng = np.random.default_rng(2)
    nmin = np.array([10.0, -20.0, 3.0])
    ns = 312.5
    p = nmin + rng.random((100_000, 3)) * ns
    q = quantize_node(p, nmin, ns)
    dec = nmin + q.astype(float) / 65535 * ns
    assert np.abs(dec - p).max() <= ns / 65535 / 2 + 1e-12
