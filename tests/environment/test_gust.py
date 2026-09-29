"""阵风锋面（M07-AC-007；M07-FR-011）：同 seed 事件序列逐字节一致、提前量 ≥ 2 s、活跃 ≤ 4、两机遭遇时差、剧本注入。"""

from __future__ import annotations

import msgpack
import numpy as np

from awr.environment.anchors import H_NS
from awr.environment.field import EnvironmentServiceImpl
from awr.environment.keyframe import EnvOp
from awr.environment.wind.gust import gust, gust_expired

BOUNDS = ((-900.0, -1000.0, 0.0), (900.0, 1000.0, 300.0))
TICK = 4_000_000


def svc(seed: int = 5) -> EnvironmentServiceImpl:
    return EnvironmentServiceImpl(world_id="t", world_seed=seed, bounds=BOUNDS, initial_preset="thunderstorm", load_assets=False)


def run(env: EnvironmentServiceImpl, seconds: float, t0_tick: int = 0) -> list:
    frames = []
    for tick in range(t0_tick, t0_tick + int(seconds * 250), 5):
        frames += env.on_env_tick_all(tick)
    return frames


def test_same_seed_same_event_bytes():
    a, b = svc(5), svc(5)
    fa, fb = run(a, 600), run(b, 600)
    ea = msgpack.packb([f.to_wire()["events"] for f in fa])
    eb = msgpack.packb([f.to_wire()["events"] for f in fb])
    assert ea == eb and len(fa) >= 5
    c = svc(6)
    assert msgpack.packb([f.to_wire()["events"] for f in run(c, 600)]) != ea


def test_lead_time_and_active_cap():
    env = svc(5)
    frames = run(env, 900)
    seen = set()
    fa = env.f_adv
    for f in frames:
        live = [ev for ev in f.events if not gust_expired(f.anchors.s_m, ev, fa)]
        assert len(f.events) <= 4 and len(live) <= 4
        for ev in f.events:
            if ev.id in seen:
                continue
            seen.add(ev.id)
            margin = fa * f.anchors.s_m - ev.x0_m  # 0 at creation (x0 = f_adv * S)
            assert abs(margin) < 1e-9
            # 以恒定 speed_ref 推进：锋面到达世界边界最近角点所需时间
            s_now = float(env.scalars(ev.t_create_ns)[0])
            ex, ey = -np.sin(np.radians(ev.dir_from_deg)), -np.cos(np.radians(ev.dir_from_deg))
            corners = [(x, y) for x in (BOUNDS[0][0], BOUNDS[1][0]) for y in (BOUNDS[0][1], BOUNDS[1][1])]
            s_min = min(ex * x + ey * y for x, y in corners)
            lead_s = (s_min - ev.s0_m) / (fa * s_now)
            assert lead_s >= 2.0
    assert len(seen) >= 10


def test_encounter_time_difference():
    """两机沿锋面方向相距 deltas：首次遭遇的时差与 deltas/(f_adv*s) 偏差 ≤ 2%。"""
    env = EnvironmentServiceImpl(world_id="t", world_seed=1, bounds=BOUNDS, initial_preset="clear", load_assets=False)
    env.apply(EnvOp("set", patch={"wind": {"speed_ref_mps": 8.0, "dir_from_deg": 270.0}}, duration_s=0), 1)
    run(env, 1.0)
    r = env.apply(EnvOp("gust", amp_mps=6.0, length_m=120.0), 260)
    t_tick = 265
    p1, p2 = (-400.0, 0.0), (300.0, 0.0)
    hit = {}
    for tick in range(t_tick, t_tick + 250 * 200, 5):
        env.on_env_tick_all(tick)
        if not env.kf.events:
            continue
        ev = env.kf.events[-1]
        S = env.anchors.A.s_m
        for name, p in (("a", p1), ("b", p2)):
            if name not in hit and gust(p, S, ev, env.f_adv) > 0:
                hit[name] = tick * TICK
        if len(hit) == 2:
            break
    assert r.version >= 2 and len(hit) == 2
    dt = (hit["b"] - hit["a"]) * 1e-9
    expect = 700.0 / (env.f_adv * 8.0)
    assert abs(dt / expect - 1) <= 0.02


def test_scenario_gust_frozen_direction_and_version():
    env = EnvironmentServiceImpl(world_id="t", world_seed=1, bounds=BOUNDS, initial_preset="rain", load_assets=False)
    run(env, 0.1)
    v0 = env.kf.version
    env.apply(EnvOp("gust", amp_mps=6.0, length_m=120.0, dir_from_deg=200.0, by="scenario"), 30)
    frames = run(env, 0.2, 30)
    ev = next(f for f in frames if f.events).events[-1]
    assert ev.dir_from_deg == 200.0 and ev.amp_mps == 6.0 and ev.lam_m == 120.0
    assert env.kf.version == v0 + 1
    # 风向改变不改变已创建锋面的方向
    env.apply(EnvOp("set", patch={"wind": {"dir_from_deg": 10.0}}, duration_s=3), 100)
    run(env, 1.0, 100)
    assert env.kf.events[-1].dir_from_deg == 200.0


def test_gust_deferred_when_full():
    env = EnvironmentServiceImpl(world_id="t", world_seed=1, bounds=BOUNDS, initial_preset="clear", load_assets=False)
    for _ in range(5):
        env.apply(EnvOp("gust", amp_mps=3.0), 1)
    frames = run(env, 0.1)
    assert len(env.kf.events) == 4 and len(env.pending) == 1
    assert len(frames) == 4
    _ = H_NS
