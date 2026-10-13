"""tests/targets 公共工具（M18）：合成世界、最小机群状态、事件记录与单步驱动的识别物台架。

`Bench` 不起 sim-core：在隔离登记表中装配 M18 插件，用 M08 的 EntityTables 与 StageCtx 直接调用 targets stage（10 Hz，
sandbox100 的 tick 编号每次 + 10）；参与者由 `FakeS` 给出（位置、armed、in_air）。命令走插件登记的处理函数（与 sim-core 的
CommandEngine 调用同一函数）。需要真实 sim-core 的用例（准入、roster、tap_targets、REST、快照）用 tests/sim 的 CoreHarness。
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT / "tests" / "sim"), str(ROOT / "tests" / "contracts"), str(ROOT / "tools" / "fake")):
    if p not in sys.path:
        sys.path.insert(0, p)


# ------------------------------------------------------------------------------------------------ 合成世界
@dataclass
class FakeWorld:
    """平地（z = ground）加若干长方体建筑 `(xmin, xmax, ymin, ymax, height)`；DSM 为建筑顶面，DTM 为地面。"""

    ground: float = 0.0
    boxes: list[tuple[float, float, float, float, float]] = field(default_factory=list)
    half: float = 2000.0
    world_id: str = "fake"
    los_calls: int = 0
    rays: int = 0

    @property
    def bounds_m(self) -> np.ndarray:
        return np.array([[-self.half, -self.half, self.ground - 10.0], [self.half, self.half, self.ground + 300.0]])

    def height_dsm(self, xy: Any, **_: Any) -> np.ndarray:
        p = np.asarray(xy, float).reshape(-1, 2)
        z = np.full(len(p), self.ground)
        for x0, x1, y0, y1, h in self.boxes:
            m = (p[:, 0] >= x0) & (p[:, 0] <= x1) & (p[:, 1] >= y0) & (p[:, 1] <= y1)
            z[m] = np.maximum(z[m], self.ground + h)
        return z

    def ground_dtm(self, xy: Any, **_: Any) -> np.ndarray:
        return np.full(len(np.asarray(xy, float).reshape(-1, 2)), self.ground)

    def los_batch(self, A: Any, B: Any, step_m: float = 1.0, eps_m: float = 0.5) -> np.ndarray:
        A = np.asarray(A, float).reshape(-1, 3)
        B = np.asarray(B, float).reshape(-1, 3)
        self.los_calls += 1
        self.rays += len(A)
        out = np.ones(len(A), bool)
        for i in range(len(A)):
            L = float(np.linalg.norm(B[i] - A[i]))
            n = max(2, math.ceil(max(L - 2 * eps_m, 0) / step_m) + 1)
            t = np.linspace(eps_m / max(L, 1e-9), 1 - eps_m / max(L, 1e-9), n) if 2 * eps_m < L else np.array([0.5])
            P = A[i][None, :] + (B[i] - A[i])[None, :] * t[:, None]
            out[i] = bool(np.all(P[:, 2] >= self.height_dsm(P[:, :2]) - 1e-9))
        return out


# ------------------------------------------------------------------------------------------------ 机群与上下文
class _Enu:
    def __init__(self, S: FakeS) -> None:
        self._S = S

    def refresh(self, force: bool = False) -> None:
        return None

    @property
    def pos(self) -> np.ndarray:
        return self._S.pos

    @property
    def q_xyzw(self) -> np.ndarray:
        q = np.zeros((len(self._S.pos), 4))
        q[:, 2] = np.sin(self._S.yaw / 2)
        q[:, 3] = np.cos(self._S.yaw / 2)
        return q


class FakeS:
    """FleetState 的最小替身：n 架机（slot 0..n−1），位置 ENU，armed/in_air，velocity setpoint（NED）。"""

    def __init__(self, n: int) -> None:
        self.capacity = max(1, n)
        self.n = n
        self.pos = np.zeros((self.capacity, 3))
        self.yaw = np.zeros(self.capacity)
        self.active = np.zeros(self.capacity, bool)
        self.active[:n] = True
        self.in_air = np.zeros(self.capacity, bool)
        self.blocks = {"safety": {"armed": np.zeros(self.capacity, bool)}}
        self.profile_id = np.zeros(self.capacity, np.uint16)
        self.vel_cmd = np.zeros((self.capacity, 3))
        self.vel_frame = np.zeros(self.capacity, np.uint8)
        self.enu = _Enu(self)

    def active_idx(self) -> np.ndarray:
        return np.flatnonzero(self.active)

    def fly(self, i: int, p: Any) -> None:
        self.pos[i] = p
        self.in_air[i] = True
        self.blocks["safety"]["armed"][i] = True

    def land(self, i: int) -> None:
        self.in_air[i] = False
        self.blocks["safety"]["armed"][i] = False


class Events:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def emit(self, kind: str, *, t_sim_ns: int, severity: int = 0, uav: str | None = None, fields: dict | None = None, **data: Any) -> int:
        d = {**(fields or {}), **data}
        self.items.append({"kind": kind, "t_sim_ns": int(t_sim_ns), "severity": severity, "uav": uav, "data": d})
        return len(self.items)

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.items if e["kind"] == kind]


@dataclass
class _RE:
    id: str
    agent_no: int
    profile_id: str = "p600_mid360"


class FakeRoster:
    def __init__(self, n: int) -> None:
        self.by_slot = {i: _RE(f"uav-{i + 1:02d}", i) for i in range(n)}


class Bench:
    """隔离登记表中的 M18 台架。用法：`with Bench(n_uav=1, world=FakeWorld()) as b: b.add({...}); b.step(10)`。"""

    def __init__(self, n_uav: int = 1, world: Any = None, *, seed: int = 7, env: Any = None, limit: int = 30) -> None:
        from awr.sim.fleet.stages import registry as R

        self.R = R
        self._cm = R.isolated_registry()
        self.reg = self._cm.__enter__()
        from awr.sim.targets import plugin

        self.plugin = plugin
        self.rt = plugin.install()
        from awr.sim.core.entities import EntityTables
        from awr.sim.fleet.pipeline import StageCtx

        self.ents = EntityTables(self.reg.entity_blocks, limits={"target": limit})
        self.S = FakeS(n_uav)
        self.events = Events()
        self.world = world
        rngs = {"targets_motion": np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, 17])))}
        self.ctx = StageCtx(tick=0, t_ns=0, dt_tick=0.01, env=env, world=world, events=self.events, entities=self.ents, stage_dt=0.1,
                            roster=FakeRoster(n_uav), rng=rngs)

    def __enter__(self) -> Bench:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.plugin.uninstall()
        self._cm.__exit__(*exc)

    def close(self) -> None:
        self.__exit__(None, None, None)

    # ------------------------------------------------------------ 命令
    def cmd(self, op: str, args: dict) -> dict:
        return self.reg.commands[op].fn({"op": op, "args": args}, self.ctx.tick + 1, self.ctx)

    def add(self, target: dict) -> str:
        r = self.cmd("target/add", {"target": target})
        assert r["status"] == "accepted", r
        return r["result"]["id"]

    def query(self, op: str, args: dict | None = None) -> dict:
        return self.reg.queries[op].fn({"op": op, "args": args or {}}, self.ctx)

    # ------------------------------------------------------------ 推进
    def step(self, n: int = 1) -> None:
        for _ in range(n):
            self.ctx.tick += 10
            self.ctx.t_ns += 100_000_000
            self.rt.stage(self.S, self.ctx)

    def run(self, seconds: float) -> None:
        self.step(round(seconds * 10))

    @property
    def t(self) -> float:
        return self.ctx.t_ns * 1e-9

    def T(self, tid: str) -> Any:
        return self.rt._by_id(tid)

    def col(self, name: str, tid: str) -> Any:
        tab = self.ents["target"]
        return tab.cols[name][tab.by_id[tid].row]


def static_person(**kw: Any) -> dict:
    """静止的人（对测试最常用：运动关闭、行为缺省）。"""
    d = {"class": "person", "pos": [0.0, 0.0, None], "motion": {"mode": "static"}}
    d.update(kw)
    return d
