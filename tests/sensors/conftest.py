"""tests/sensors 公共夹具（M13）：隔离登记表中装配 M13 插件，用 M08 的 FleetState 与 ProfileTable 单步驱动 sensors stage。

`Bench` 不起 sim-core 进程与总线：自己推进 tick（250 Hz），在 `(tick − 2) % 5 == 0` 时调用登记的 sensors stage，
按需调用两个慢任务；事件由 `Events` 记录（接口与 EventPublisher 的 emit/replay/last_seq 相同）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from awr.contracts.rng_streams import Stream, rng  # noqa: E402
from awr.sim.fleet.pipeline import StageCtx  # noqa: E402
from awr.sim.fleet.stages import registry as R  # noqa: E402
from awr.sim.fleet.state import FleetState  # noqa: E402
from awr.world.georef.frames import enu_to_ned, q_nedfrd_from_enuflu  # noqa: E402

TICK_NS = 4_000_000


class Events:
    """EventPublisher 的替身（emit、last_seq、replay、epoch、bus）。"""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.epoch = 1
        self.bus = None

    @property
    def last_seq(self) -> int:
        return len(self.items)

    def emit(self, kind: str, *, t_sim_ns: int, severity: int = 0, uav: str | None = None, cid: str | None = None,
             batch_id: str | None = None, fields: dict | None = None, **data: Any) -> int:
        d = {**(fields or {}), **data}
        self.items.append({"seq": len(self.items) + 1, "kind": kind, "severity": severity, "t_sim_ns": int(t_sim_ns), "uav": uav,
                           "data": d})
        return len(self.items)

    def replay(self, since: int, epoch: int) -> dict:
        return {"v": 1, "events": [e for e in self.items if e["seq"] > since], "truncated": False}

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.items if e["kind"] == kind]


@pytest.fixture(scope="session")
def profiles():
    from awr.sim.fleet.profiles import ProfileTable

    return ProfileTable()


class Bench:
    def __init__(self, reg: R.Registry, rt: Any, T: Any, *, capacity: int = 64, seed: int = 7, world: Any = None,
                 env: Any = None) -> None:
        self.reg = reg
        self.rt = rt
        rt.strict = True
        self.T = T
        self.S = FleetState(capacity, blocks={k: v for k, v in reg.blocks.items()})
        self.events = Events()
        self.ctx = StageCtx()
        self.ctx.events = self.events
        self.ctx.profiles = T
        self.ctx.world = world
        self.ctx.env = env
        self.ctx.rng = {st.name.lower(): rng(seed, int(st)) for st in Stream}
        self.stage = next(s for s in reg.stages if s.name == "sensors")
        self.tick = 0
        self.slow_pose = True
        self.slow_obs = True

    # ---------------------------------------------------------------- 机体
    def spawn(self, slot: int, agent_no: int, profile: str = "p600_mid360", pos=(0.0, 0.0, 60.0), yaw_deg: float = 0.0,
              *, vid: str | None = None, in_air: bool = True) -> int:
        S = self.S
        S.active[slot] = True
        S.agent_no[slot] = agent_no
        S.profile_id[slot] = self.T.ids.index(profile)
        S.ids[slot] = vid or f"v{agent_no:02d}"
        S.in_air[slot] = in_air
        self.set_pose(slot, pos, yaw_deg=yaw_deg)
        return slot

    def remove(self, slot: int) -> None:
        self.S.active[slot] = False
        self.S.ids[slot] = None
        self.S.touch()

    def set_pose(self, slot: int, pos, *, yaw_deg: float = 0.0, q_xyzw=None) -> None:
        S = self.S
        S.p[slot] = enu_to_ned(np.asarray(pos, np.float64))
        if q_xyzw is None:
            h = np.radians(yaw_deg) / 2.0
            q_xyzw = (0.0, 0.0, np.sin(h), np.cos(h))
        S.q[slot] = q_nedfrd_from_enuflu(np.asarray(q_xyzw, np.float64))
        S.touch()

    def interest(self, agent_nos) -> None:
        self.ctx.interest = np.asarray(sorted(agent_nos), np.uint16)

    # ---------------------------------------------------------------- 时钟
    def run(self, seconds: float, *, on_tick=None) -> None:
        n = round(seconds * 250)
        for _ in range(n):
            self.step(on_tick)

    def step(self, on_tick=None) -> None:
        t = self.tick
        c = self.ctx
        c.tick = t
        c.t_ns = t * TICK_NS
        if on_tick is not None:
            on_tick(self, t)
        if (t - self.stage.phase) % self.stage.every == 0:
            self.stage.fn(self.S, c)
        if self.slow_pose:
            while self.rt.packer.run_slow(c) is not False:
                pass
        if self.slow_obs and (t % 125 == 0):
            self.rt.obs.run_slow(c)
        self.tick += 1

    def block_bytes(self) -> bytes:
        b = self.S.blocks["sensors"]
        return b"".join(np.ascontiguousarray(b[k]).tobytes() for k in sorted(b))


@pytest.fixture
def bench_factory(profiles):
    """bench_factory(capacity=64, seed=7, world=None, env=None) -> Bench（每次在新的隔离登记表中装配 M13）。"""
    made: list[Any] = []

    def make(**kw) -> Bench:
        from awr.sim.sensors import plugin

        cm = R.isolated_registry()
        reg = cm.__enter__()
        rt = plugin.install()
        made.append((cm, plugin))
        return Bench(reg, rt, profiles, **kw)

    yield make
    for cm, plugin in reversed(made):
        plugin.uninstall()
        cm.__exit__(None, None, None)


@pytest.fixture
def bench(bench_factory) -> Bench:
    return bench_factory()
