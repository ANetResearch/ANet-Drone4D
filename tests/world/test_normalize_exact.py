"""M03-AC-010：90° 整数倍旋转矩阵元素恰为 0、±1；规范化结果与"逐元素乘加"参考逐位相同；上轴表 det = +1。"""

from __future__ import annotations

import os
import subprocess
import sys

import numpy as np
import pytest

from awr.world.ingest.normalize import UP_ROT, apply_linear, compose3, exact_rz, rodrigues, scale3


@pytest.mark.parametrize("deg", [0, 90, 180, 270, -90, 360, 450])
def test_exact_rz_integer_multiples(deg):
    R = np.asarray(exact_rz(deg))
    assert set(np.unique(R).tolist()) <= {-1.0, 0.0, 1.0}
    th = np.radians(deg)
    assert np.allclose(R, [[np.cos(th), -np.sin(th), 0], [np.sin(th), np.cos(th), 0], [0, 0, 1]], atol=1e-15)


def test_up_rot_table():
    for k, R in UP_ROT.items():
        M = np.asarray(R)
        assert abs(np.linalg.det(M) - 1.0) < 1e-15
        ax = {"x": 0, "y": 1, "z": 2}[k[1]]
        v = np.zeros(3)
        v[ax] = 1.0 if k[0] == "+" else -1.0
        assert np.allclose(M @ v, [0, 0, 1])
    assert UP_ROT["+y"] == ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0))


def test_elementwise_matches_reference_bitwise():
    rng = np.random.default_rng(3)
    P = rng.normal(0, 1000, (100_000, 3))
    A = scale3(10.15, compose3(exact_rz(90.0), rodrigues([0.03, 0.01, 1.0]), UP_ROT["+y"]))
    out = apply_linear(A, P)
    ref = np.empty_like(P)
    for i in range(3):
        ref[:, i] = (A[i][0] * P[:, 0] + A[i][1] * P[:, 1]) + A[i][2] * P[:, 2]
    assert np.array_equal(out, ref)


def test_exact_matrix_identity_preserves_bits():
    rng = np.random.default_rng(4)
    P = rng.normal(0, 1000, (10_000, 3))
    Q = apply_linear(scale3(1.0, exact_rz(90.0)), P)
    assert np.array_equal(Q[:, 0], -P[:, 1]) and np.array_equal(Q[:, 1], P[:, 0]) and np.array_equal(Q[:, 2], P[:, 2])


def test_rodrigues_rotates_a_to_b():
    a = np.array([0.0321, 0.0146, 0.9974])
    R = np.asarray(rodrigues(a))
    assert np.allclose(R @ (a / np.linalg.norm(a)), [0, 0, 1], atol=1e-12)
    assert abs(np.linalg.det(R) - 1) < 1e-12


def test_blas_core_type_does_not_change_result():
    """两种 OPENBLAS_CORETYPE 下同一规范化的字节哈希相同（规范化不经 BLAS）。"""
    code = ("import numpy as np, hashlib\nfrom awr.world.ingest.normalize import *\n"
            "P = np.random.default_rng(5).normal(0, 1000, (50000, 3))\n"
            "A = scale3(1000.0, compose3(exact_rz(0.0), rodrigues([0.03, 0.01, 1.0]), UP_ROT['+z']))\n"
            "print(hashlib.sha256(apply_linear(A, P).tobytes()).hexdigest())")
    outs = set()
    for core in ("Haswell", "Prescott"):
        env = dict(os.environ, OPENBLAS_CORETYPE=core)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
        outs.add(r.stdout.strip())
    assert len(outs) == 1
