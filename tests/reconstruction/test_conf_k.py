"""Confidence mapping and intrinsics (M01-AC-003, AC-004; M01-FR-005, FR-006)."""

from __future__ import annotations

import numpy as np
import pytest

from awr.reconstruction.conventions import (
    conf_to_u8,
    k_model_to_orig,
    k_orig_to_model,
    k_scale,
    preproc_identity,
    vggt_preproc,
)


def test_conf():
    c = conf_to_u8(np.array([1.0, 1.5, 5.0, np.inf, 0.5]), "expp1")
    assert c.tolist() == [0, 85, 204, 255, 0]
    assert c.dtype == np.uint8
    assert conf_to_u8(np.array([0.0]), "sigmoid_logit").tolist() == [128]
    assert conf_to_u8(np.array([0.0, 2.0, 4.0, 10.0]), "reproj_err").tolist() == [255, 128, 0, 0]
    assert conf_to_u8(np.array([0.0, 0.5, 1.0, 1.7]), "synthetic").tolist() == [0, 128, 255, 255]
    assert conf_to_u8(None, "expp1") is None and conf_to_u8(np.ones(3), "none") is None
    with pytest.raises(ValueError):
        conf_to_u8(np.ones(1), "softplus")
    # identity with the sigmoid of the underlying logit: 1 - 1/(1 + exp(x)) = sigmoid(x) (r02 §0 item 9)
    x = np.linspace(-6, 6, 101)
    assert np.array_equal(conf_to_u8(1.0 + np.exp(x), "expp1"), conf_to_u8(x, "sigmoid_logit"))
    # histogram of a map sums to its point count
    rng = np.random.default_rng(1)
    u8 = conf_to_u8(1.0 + np.exp(rng.normal(size=5000)), "expp1")
    assert np.bincount(u8, minlength=256).sum() == 5000


def _project(K, X):
    return np.c_[K[0, 0] * X[:, 0] / X[:, 2] + K[0, 2], K[1, 1] * X[:, 1] / X[:, 2] + K[1, 2]]


def test_k_mapping_vggt_518():
    p = vggt_preproc(1920, 1080)
    assert (p.W_model, p.H_model) == (518, 294)
    assert p.sx == pytest.approx(0.26979, abs=1e-5) and p.sy == pytest.approx(0.27222, abs=1e-5)
    K_orig = np.array([[1400.0, 0, 961.3], [0, 1395.0, 538.9], [0, 0, 1]])
    K_model = k_orig_to_model(K_orig, p, "integer")
    np.testing.assert_allclose(k_model_to_orig(K_model, p, "integer"), K_orig, rtol=0, atol=1e-9)
    rng = np.random.default_rng(4)
    X = np.c_[rng.uniform(-20, 20, (1000, 2)), rng.uniform(5, 200, 1000)]
    uv_o = _project(K_orig, X)                        # original image, COLMAP continuous coordinates
    uv_m = _project(K_model, X)                       # model image, integer-centre convention
    # orig -> model: continuous * s, then integer-centre = continuous - 0.5; back again must round-trip
    uv_m2 = np.c_[uv_o[:, 0] * p.sx - 0.5, uv_o[:, 1] * p.sy - 0.5]
    assert np.abs(uv_m - uv_m2).max() <= 0.01
    back = np.c_[(uv_m[:, 0] + 0.5) / p.sx, (uv_m[:, 1] + 0.5) / p.sy]
    assert np.abs(back - uv_o).max() <= 0.01


def test_k_mapping_crop_and_half():
    p = vggt_preproc(1080, 1920)                      # portrait: resized to 518 x 924, centre-cropped to 518 x 518
    assert (p.W_model, p.H_model) == (518, 518) and p.crop_y == (924 - 518) // 2
    K = np.array([[900.0, 0, 540.0], [0, 900.0, 960.0], [0, 0, 1]])
    for pc in ("integer", "half"):
        np.testing.assert_allclose(k_model_to_orig(k_orig_to_model(K, p, pc), p, pc), K, rtol=0, atol=1e-9)
    q = preproc_identity(1920, 1080, 128, 72)
    Km = k_orig_to_model(np.array([[1662.77, 0, 960.0], [0, 1662.77, 540.0], [0, 0, 1]]), q, "half")
    assert Km[0, 2] == pytest.approx(64.0) and Km[1, 2] == pytest.approx(36.0) and Km[0, 0] == pytest.approx(110.851, abs=1e-3)
    np.testing.assert_allclose(k_scale(k_model_to_orig(Km, q, "half"), (1920, 1080), (128, 72)), Km, rtol=0, atol=1e-9)
