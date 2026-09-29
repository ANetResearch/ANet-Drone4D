"""查询接口（M07-AC-009；M07-FR-016、FR-024）：四种帧、flags、廓线与热力学、`env/query` 校验与回复形状。
256 点耗时（≤ 0.6 ms）为 perf 用例。"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from awr.environment.atmosphere.isa import isa
from awr.environment.field import EnvironmentServiceImpl
from awr.environment.keyframe import EnvOp
from awr.environment.query import EnvFlags, Fields, Frame
from awr.environment.wind.profile import profile

BOUNDS = ((-900.0, -1000.0, 0.0), (900.0, 1000.0, 300.0))
COORD = {"ground": {"zM": 2.0}, "anchor": {"hMslM": 12.2}}


def svc(preset: str = "clear", **kw) -> EnvironmentServiceImpl:
    env = EnvironmentServiceImpl(world_id="t", world_seed=1, bounds=BOUNDS, initial_preset=preset, coordinate=COORD, load_assets=False, **kw)
    env.on_env_tick_all(0)
    return env


def test_profile_and_mean_wind():
    env = svc("clear")
    t = env.t_grid_ns
    q = env.query(np.array([[0, 0, 2.3], [0, 0, 42.0]]), t, fields=int(Fields.WIND | Fields.WIND_PARTS))
    assert np.allclose(q.wind_mean_mps[0], [0, 0, 0])  # z_agl 0.3 <= d + z0
    f40 = profile(40.0)
    assert np.allclose(q.wind_mean_mps[1], [3.0 * f40, 0, 0], atol=1e-12)  # 来向 270（西风）-> 去向 +E
    assert np.allclose(q.wind_mps[1], q.wind_mean_mps[1])  # turbulence off (no assets), no gust


def test_frames():
    env = svc("clear")
    t = env.t_grid_ns
    p = np.array([[10.0, 0.0, 50.0]])
    v = np.array([[1.0, 2.0, 0.5]])
    g = env.query(p, t, fields=int(Fields.WIND)).wind_mps[0].copy()
    a = env.query(p, t, fields=int(Fields.WIND), frame=Frame.ADD_VELOCITY_GLOBAL, vel=v).wind_mps[0].copy()
    assert np.allclose(a, g - v[0])
    yaw90 = np.array([[0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)]])  # 机体 x 指向北
    loc = env.query(p, t, fields=int(Fields.WIND), frame=Frame.LOCAL, quat_xyzw=yaw90).wind_mps[0].copy()
    assert np.allclose(loc, [g[1], -g[0], g[2]], atol=1e-12)
    la = env.query(p, t, fields=int(Fields.WIND), frame=Frame.ADD_VELOCITY_LOCAL, vel=v, quat_xyzw=yaw90).wind_mps[0].copy()
    d = g - v[0]
    assert np.allclose(la, [d[1], -d[0], d[2]], atol=1e-12)


def test_flags_fog_precip_calm():
    fog = svc("fog")
    q = fog.query(np.array([[0, 0, 2.0 + 30.0], [0, 0, 2.0 + 100.0], [0, 0, 2.0]]), fog.t_grid_ns, fields=int(Fields.OPTICS))
    assert q.flags[0] & EnvFlags.IN_FOG_LAYER and not q.flags[1] & EnvFlags.IN_FOG_LAYER
    assert q.flags[0] & EnvFlags.VALID
    assert abs(q.mor_m[2] - 150.0) / 150.0 < 1e-5  # 雾层内贴地 sigma = ln20 / MOR
    hr = svc("heavyRain")
    q = hr.query(np.array([[0, 0, 100.0], [0, 0, 600.0]]), hr.t_grid_ns, fields=int(Fields.OPTICS))
    assert q.flags[0] & EnvFlags.BELOW_CLOUD_PRECIP and not q.flags[1] & EnvFlags.BELOW_CLOUD_PRECIP
    assert q.sigma_precip_per_m[0] > 0 and q.sigma_precip_per_m[1] == 0
    calm = svc("clear")
    calm.apply(EnvOp("set", patch={"wind": {"speed_ref_mps": 0}}, duration_s=0), 1)
    calm.on_env_tick_all(5)
    q = calm.query(np.array([[0, 0, 50.0]]), calm.t_grid_ns)
    assert q.flags[0] & EnvFlags.CALM


def test_thermo_isa():
    env = svc("clear")
    q = env.query(np.array([[0, 0, 100.0]]), env.t_grid_ns, fields=int(Fields.THERMO))
    ref = isa(12.2 + 100.0, 0.0)
    assert abs(q.rho_kgm3[0] - ref["rho_kgm3"]) < 1e-6 and abs(q.temperature_c[0] - ref["temperature_c"]) < 1e-4


def test_handle_query_validation_and_reply():
    env = svc("rain")
    rep = env.handle_query({"op": "env/query", "args": {"points": [[0, 0, 50]], "fields": ["WIND", "OPTICS", "THERMO", "PRECIP"]}})
    assert rep["code"] == 0 and rep["n"] == 1 and len(rep["wind_mps"][0]) == 3 and "rain_eff_mmh" in rep and "rho_kgm3" in rep
    assert env.handle_query({"args": {"points": [[0, 0, 1]] * 257}})["code"] == 110
    assert env.handle_query({"args": {"points": [[0, 0, float("nan")]]}})["code"] == 110
    assert env.handle_query({"args": {"points": [[0, 0, 1]], "t_ns": env.t_grid_ns + 11_000_000_000}})["code"] == 110
    assert env.handle_query({"args": {"points": [[0, 0, 1]], "t_ns": -5}})["code"] == 110
    assert env.handle_query({"args": {"points": [[0, 0, 1]], "frame": "sideways"}})["code"] == 300
    assert env.handle_query({"args": {"points": [[0, 0, 1]], "frame": "local"}})["code"] == 300
    assert env.handle_query({"args": {"points": []}})["code"] == 300
    fut = env.handle_query({"args": {"points": [[0, 0, 50]], "t_ns": env.t_grid_ns + 5_000_000_000}})
    assert fut["code"] == 0 and fut["t_ns"] == env.t_grid_ns + 5_000_000_000


def test_optical_depth_matches_scalar():
    from awr.environment.atmosphere.optics import optical_depth

    env = svc("fog")
    p0 = np.array([[0, 0, 12.0]])
    p1 = np.array([[300, 400, 112.0]])
    od = env.optical_depth(p0, p1, env.t_grid_ns)[0]
    d = env.derived(env.t_grid_ns)
    L = math.sqrt(300**2 + 400**2 + 100**2)
    ref = optical_depth(10.0, 100.0 / L, L, d, 60.0, 300.0)
    assert abs(od - ref) <= 1e-12 * max(1.0, ref)


@pytest.mark.perf
def test_query_256_points_time():
    env = svc("thunderstorm")
    rng = np.random.default_rng(1)
    pts = rng.uniform([-500, -500, 5], [500, 500, 150], (256, 3)).tolist()
    env.handle_query({"args": {"points": pts}})
    t = time.perf_counter()
    for _ in range(50):
        env.handle_query({"args": {"points": pts}})
    assert (time.perf_counter() - t) / 50 <= 0.6e-3
