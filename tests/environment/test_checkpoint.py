"""checkpoint 与 env.warning（D1-ext；M07-FR-027、FR-029；M07-AC-032 功能部分）。

- 恢复后：关键帧、锚点、Dryden 状态、阵风调度器与 RNG 与不中断运行逐位一致（生产者纪元 + 1，K08），后续事件序列相同；
- env.warning：mor_low（总 MOR < 1000 m）、wind_high（10 m 参考风速 > 12 m/s）、precip_heavy（有效雨强 > 7.6 mm/h）进入与离开各一次。
"""

from __future__ import annotations

import numpy as np
import pytest

from awr.environment.field import EnvironmentServiceImpl
from awr.environment.keyframe import EnvOp

BOUNDS = ((-900.0, -1000.0, 0.0), (900.0, 1000.0, 300.0))


def svc(seed: int = 3) -> EnvironmentServiceImpl:
    return EnvironmentServiceImpl(world_id="t", world_seed=seed, bounds=BOUNDS, initial_preset="thunderstorm", load_assets=False, capacity=16)


def drive(env: EnvironmentServiceImpl, t0: int, t1: int, slots: np.ndarray, pos: np.ndarray) -> list[bytes]:
    out = []
    for tick in range(t0, t1, 5):
        for kf in env.on_env_tick_all(tick):
            w = kf.to_wire()
            w["epoch"] = 0
            out.append(repr(w).encode())
        env.step_dryden(slots, pos, np.zeros_like(pos), 0.02)
    return out


@pytest.mark.ext
def test_restore_continues_identically():
    slots = np.array([1, 4, 9])
    pos = np.array([[0.0, 0.0, 30.0], [50.0, 10.0, 60.0], [-20.0, 30.0, 90.0]])
    a = svc()
    a.apply(EnvOp("set", patch={"config": {"wind": {"turbulence": {"model": "dryden"}}}}), 1)
    drive(a, 0, 250 * 60, slots, pos)
    a.apply(EnvOp("preset", name="rain"), a.last_tick + 1)
    blob = a.checkpoint()
    b = svc()
    b.restore(blob)
    assert b.epoch == a.epoch + 1 and b.kf.epoch == b.epoch
    fa = drive(a, a.last_tick + 5, a.last_tick + 250 * 120, slots, pos)
    fb = drive(b, b.last_tick + 5, b.last_tick + 250 * 120, slots, pos)
    assert fa == fb and len(fa) >= 2
    assert a.anchors.A.to_json() == b.anchors.A.to_json()
    assert np.array_equal(a.dryden.out, b.dryden.out) and np.array_equal(a.dryden.zv, b.dryden.zv)


@pytest.mark.ext
def test_env_warning_enter_and_leave_once():
    env = EnvironmentServiceImpl(world_id="t", world_seed=1, bounds=BOUNDS, initial_preset="clear", load_assets=False)
    env.on_env_tick_all(0)
    env.outbox.clear()
    env.apply(EnvOp("preset", name="thunderstorm", duration_s=0), 1)
    for tick in range(5, 250, 5):
        env.on_env_tick_all(tick)
    got = [(d["code"], d["state"]) for k, d in env.outbox if k == "env.warning"]
    assert set(got) == {("mor_low", "enter"), ("wind_high", "enter"), ("precip_heavy", "enter")} and len(got) == 3
    env.outbox.clear()
    env.apply(EnvOp("preset", name="clear", duration_s=0), env.last_tick + 1)
    for tick in range(env.last_tick + 5, env.last_tick + 250, 5):
        env.on_env_tick_all(tick)
    got = [(d["code"], d["state"]) for k, d in env.outbox if k == "env.warning"]
    assert set(got) == {("mor_low", "leave"), ("wind_high", "leave"), ("precip_heavy", "leave")} and len(got) == 3
