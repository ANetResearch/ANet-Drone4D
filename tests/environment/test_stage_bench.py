"""env stage 预算（M07-AC-008；M07-NFR-001、NFR-002、NFR-003）：N = 1000，L1 + 湍流盒 + OPTICS + THERMO 单次 p99 ≤ 1.6 ms，
Dryden 同条件 ≤ 2.0 ms（load < 6 时判定），env/query 256 点 ≤ 0.6 ms。性能用例：只在独占锁下经 harness 执行（AWR-18 §3）。"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

from awr.environment.keyframe import EnvOp
from awr.environment.query import Fields
from awr.environment.stage import build_service

pytestmark = pytest.mark.perf
FIELDS = int(Fields.WIND | Fields.WIND_PARTS | Fields.THERMO | Fields.OPTICS)


def _p99(f, n: int = 500) -> float:
    for _ in range(20):
        f()
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        f()
        ts.append(time.perf_counter() - t)
    return float(np.percentile(ts, 99)) * 1e3


@pytest.fixture(scope="module")
def env():
    try:
        from awr.world.geometry import open_world_query

        wq = open_world_query(os.path.join(os.path.dirname(__file__), "..", "..", "worlds", "shenzhen"))
    except Exception:
        wq = None
    e = build_service(wq, world_id="shenzhen")
    e.apply(EnvOp("preset", name="thunderstorm", duration_s=0), 1)
    for t in range(0, 50, 5):
        e.on_env_tick_all(t)
    return e


def test_box_p99(env):
    rng = np.random.default_rng(1)
    pos = rng.uniform([-900, -900, 5], [900, 900, 150], (1000, 3))
    vel = rng.normal(0, 5, (1000, 3))
    slots = np.arange(1000)
    assert env.kf.config["wind"]["turbulence"]["model"] == "box"
    assert _p99(lambda: env.query(pos, env.t_grid_ns, fields=FIELDS, vel=vel, agent_idx=slots, out=env.buf)) <= 1.6


def test_dryden_p99(env):
    if os.getloadavg()[0] >= 6:
        pytest.skip("load >= 6 (M07-AC-008)")
    env.apply(EnvOp("set", patch={"config": {"wind": {"turbulence": {"model": "dryden"}}}}), env.last_tick + 1)
    env.on_env_tick_all(env.last_tick + 5)
    rng = np.random.default_rng(2)
    pos = rng.uniform([-900, -900, 5], [900, 900, 150], (1000, 3))
    vel = rng.normal(0, 5, (1000, 3))
    slots = np.arange(1000)

    def tick():
        env.step_dryden(slots, pos, vel, 0.02)
        env.query(pos, env.t_grid_ns, fields=FIELDS, vel=vel, agent_idx=slots, out=env.buf)

    assert _p99(tick) <= 2.0


def test_query_256_points(env):
    pts = np.random.default_rng(3).uniform([-500, -500, 5], [500, 500, 150], (256, 3)).tolist()
    assert _p99(lambda: env.handle_query({"args": {"points": pts}}), 200) <= 0.6
