"""解析场 AWSL 流线（D1-ext；M07-AC-028；M07-FR-048；16 §8.6）：文件结构（V-E-05 口径）、8–64 顶点、避开实体、tau_hat 单调
且按 12 m / |M| 递增（风速变化时几何不变、相位连续）、LRU 缓存与场 id；1000 × 64 生成耗时 ≤ 0.5 s 为 perf 用例。"""

from __future__ import annotations

import time

import numpy as np
import pytest

from awr.contracts.layouts import ENV_AWSL_HEADER
from awr.environment.field import EnvironmentServiceImpl
from awr.environment.wind.profile import DEFAULT_PROFILE, profile_arr
from awr.environment.wind.streamlines import MAX_PTS, MIN_PTS, STEP_M, analytic_field_id, decode_awsl, generate

BOUNDS = ((-900.0, -1000.0, 0.0), (900.0, 1000.0, 300.0))


def svc() -> EnvironmentServiceImpl:
    return EnvironmentServiceImpl(world_id="t", world_seed=2, bounds=BOUNDS, initial_preset="rain", load_assets=False)


@pytest.mark.ext
def test_awsl_structure_and_phase():
    env = svc()
    assert env.kf.vis["streamlines"] == f"/api/env/streamlines/{env.field_id}"
    b = env.streamlines(env.field_id, 45)
    h, offs, v = decode_awsl(b)
    hdr = np.frombuffer(b[:48], ENV_AWSL_HEADER)[0]
    assert int(hdr["magic"]) == 0x4C535741 and int(hdr["stride"]) == 20 and int(hdr["flags"]) == 3
    assert h["n_lines"] >= 500 and np.all(np.diff(offs.astype(np.int64)) >= MIN_PTS) and np.all(np.diff(offs.astype(np.int64)) <= MAX_PTS)
    assert np.all(np.isfinite(v))
    ex, ey = -np.sin(np.radians(45.0)), -np.cos(np.radians(45.0))
    for i in range(0, h["n_lines"], 97):
        ln = v[offs[i]:offs[i + 1]]
        d = np.diff(ln[:, :3], axis=0)
        assert np.allclose(np.linalg.norm(d, axis=1), STEP_M, atol=1e-3)
        assert np.allclose(d[:, 0] / STEP_M, ex, atol=1e-4) and np.allclose(d[:, 1] / STEP_M, ey, atol=1e-4)
        f = profile_arr(ln[:, 2].astype(np.float64), DEFAULT_PROFILE)
        assert np.allclose(ln[:, 4], f, rtol=1e-5)
        assert np.all(np.diff(ln[:, 3]) > 0)
        assert np.allclose(np.diff(ln[:, 3]), STEP_M / f[:-1], rtol=1e-4)
    # cache and field id
    assert env.streamlines(env.field_id, 45) is b
    assert analytic_field_id("t", "", env.kf.config["wind"]["profile"]) == env.field_id


@pytest.mark.ext
def test_lines_avoid_solids():
    def solid(xy: np.ndarray) -> np.ndarray:
        return np.where(np.hypot(xy[:, 0], xy[:, 1]) < 200.0, 400.0, 0.0)

    lines = generate(270.0, DEFAULT_PROFILE, BOUNDS, solid_top=solid, n_lines=400, seed=5)
    allv = np.concatenate(lines)
    assert not np.any(np.hypot(allv[:, 0], allv[:, 1]) < 200.0)


@pytest.mark.ext
def test_unknown_field_is_404():
    from awr.environment.weather.presets import EnvError

    with pytest.raises(EnvError):
        svc().streamlines("analytic-00000000", 10)


@pytest.mark.perf
def test_generation_time():
    env = svc()
    t = time.perf_counter()
    env.streamlines(env.field_id, 123)
    assert time.perf_counter() - t <= 0.5
