"""任务内 4D 冲突检查与消解（M10-FR-056；M10-AC-023，功能部分；耗时断言属性能用例，不在并行阶段运行）。"""

from __future__ import annotations

import numpy as np
import pytest

from awr.sim.planning import bspline as BS
from awr.sim.planning.deconflict import candidates, deconflict_mission, min_ellipsoid_dist, sample_traj

pytestmark = pytest.mark.ext


def _star(n: int = 8, R: float = 300.0, v: float = 5.0, z: float = 60.0, dt: float = 0.1) -> list[np.ndarray]:
    """n 架机沿过同一中心的直线对飞（两两交叉），同速同高，同时到达中心。"""
    out = []
    t = np.arange(0.0, 2 * R / v + 1e-9, dt)
    for k in range(n):
        a = np.pi * k / n
        u = np.array([np.cos(a), np.sin(a), 0.0])
        P0 = -R * u + np.array([0.0, 0.0, z])
        out.append(P0[None] + v * t[:, None] * u[None])
    return out


def _check(samples, res, clearance, dt=0.1, z_scale=0.5):
    n_total = max(len(P) for P in samples) + round(max([*res.delays_s, 0.0]) / dt) + 1
    sh = []
    for P, d, z in zip(samples, res.delays_s, res.layers_m, strict=True):
        dn = round(d / dt)
        T = np.empty((n_total, 3))
        T[:dn] = P[0]
        T[dn:dn + len(P)] = P[:n_total - dn]
        T[dn + len(P):] = P[-1]
        T[:, 2] += z
        sh.append(T)
    return min(min_ellipsoid_dist(sh[i], sh[j], z_scale) for i in range(len(sh)) for j in range(i + 1, len(sh)))


def test_candidates_order() -> None:
    c = candidates()
    assert c[0] == (0.0, 0.0)
    keys = [d + 0.5 * abs(z) for d, z in c]
    assert keys == sorted(keys) and len(c) == 16 * 4


def test_star_crossing_resolved() -> None:
    S = _star()
    before = min(min_ellipsoid_dist(S[i], S[j]) for i in range(8) for j in range(i + 1, 8))
    assert before < 1.0                                                  # 未消解：全部在中心相遇
    res = deconflict_mission(S, prio=list(range(8, 0, -1)), clearance_m=10.0)
    assert res.ok and not res.residual, res.to_json()
    assert max(res.delays_s) <= 30.0                                      # M10-AC-023：最大延迟 ≤ 30 s
    assert res.delays_s[0] == 0.0 and res.layers_m[0] == 0.0             # 最高优先级不动
    assert _check(S, res, 10.0) >= 10.0 - 1e-9


def test_dz_requires_validation_and_partial() -> None:
    S = _star(n=3)
    res = deconflict_mission(S, prio=[3, 2, 1], clearance_m=10.0, delays=(0,), dzs=(0.0, 30.0, 60.0),
                             validate=lambda k, dz: dz != 60.0)
    # 只允许 +30 m（椭球 0.5 缩放后 15 m ≥ 10 m）：第 2 架升 30，第 3 架无可用候选
    assert res.layers_m[1] == 30.0 and res.partial == [2]
    assert res.residual and all(r < 10.0 for _, _, r in res.residual)
    js = res.to_json(["a", "b", "c"])
    assert js["partial"] == ["c"] and js["layers_m"]["b"] == 30.0


def test_bspline_sampling_matches_duration() -> None:
    ts = 0.5
    P = np.c_[np.arange(0, 101.0, 2.5), np.zeros(41), np.full(41, 50.0)]          # 5 m/s 直线的控制点
    Q = np.r_[P[:1], P, P[-1:]]
    S = sample_traj(Q, ts, 0.1)
    assert abs((len(S) - 1) * 0.1 - BS.duration(Q, ts)) <= 0.1 + 1e-9
    assert np.allclose(S[0], BS.eval_bspline(Q, ts, [0.0])[0])


@pytest.mark.parametrize("n", [2, 4])
def test_parallel_lanes_need_no_change(n: int) -> None:
    t = np.arange(0.0, 60.0, 0.1)
    S = [np.c_[5.0 * t, np.full_like(t, 30.0 * k), np.full_like(t, 50.0)] for k in range(n)]
    res = deconflict_mission(S, prio=[1.0] * n, clearance_m=10.0)
    assert res.delays_s == [0.0] * n and res.layers_m == [0.0] * n and res.ok
