"""tests/perception 公共工具（M19）：在隔离登记表中装配 M18 与 M19 插件，用合成平台（替代 M13 读取）单步驱动感知 stage。

- `PBench`：EntityTables + 最小机群（`FakeS`，带 agent_no 与 ids）+ `StageCtx`；`tick` 为 sandbox100 的 100 Hz：每 10 tick 调一次
  targets stage（10 Hz），每 tick 调一次 perception stage（20 片轮转，每机 5 Hz）。平台由 `PBench.platforms[slot]` 给出（测试按需
  设置云台指向、焦距、模式与非成像传感器），经 `PerceptionRuntime.platforms_fn` 注入。
- `FakeEnv`：EnvPerceptionView 的替身（照度档、MOR 均匀消光、热背景、24 带声学背景）。
- 需要真实 M13 的用例见 `test_m13_integration.py`（FleetState + ProfileTable）。
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT / "tests" / "targets"), str(ROOT / "tests" / "sim"), str(ROOT / "tests" / "contracts"), str(ROOT / "tools" / "fake")):
    if p not in sys.path:
        sys.path.insert(0, p)

from tlib import Events, FakeS, FakeWorld  # noqa: E402

__all__ = ["FakeEnv", "FakeS2", "FakeWorld", "PBench", "look_R", "make_imaging", "make_nonimg"]


def look_R(p: Any, at: Any) -> np.ndarray:
    """WORLD ← SENSOR（FLU，x 为视轴）使 x 指向 `at`。"""
    x = np.asarray(at, float) - np.asarray(p, float)
    x /= np.linalg.norm(x)
    up = np.array([0.0, 0.0, 1.0])
    y = np.cross(up, x)
    if np.linalg.norm(y) < 1e-6:
        y = np.array([0.0, 1.0, 0.0])
    y /= np.linalg.norm(y)
    z = np.cross(x, y)
    return np.stack([x, y, z], axis=1)


def make_imaging(spec: Any, p: Any, at: Any, *, f_mm: float | None = None, mode: str = "auto", src: str = "tasking", name: str | None = None,
                 sensor_no: int = 0, illum_on: tuple[bool, ...] = (), resolution: str | None = None, mode_eff: str | None = None) -> Any:
    from awr.sim.perception.optics import hfov_rad, vfov_rad
    from awr.sim.perception.runtime import Imaging
    from awr.sim.perception.specs import SensorState

    f = spec.f_max_mm if f_mm is None else float(f_mm)
    st = SensorState(spec, f, mode, resolution, illum_on)  # type: ignore[arg-type]
    ill = {}
    for j, il in enumerate(spec.illuminators):
        on = (not illum_on) or (j < len(illum_on) and illum_on[j])
        ill["builtin" if il.kind == "builtin" else "ext"] = "on" if on and spec.kind == "nir" else "off"
    return Imaging(name or spec.name, sensor_no, spec, st, np.asarray(p, float), look_R(p, at), hfov_rad(spec, f, resolution),
                   vfov_rad(spec, f, resolution), src, mode_eff or ("nir" if spec.kind == "nir" else "day"), ill, 0)


def make_nonimg(kind: str, spec: Any, p: Any, *, R: Any = None, name: str | None = None, sensor_no: int = 5) -> Any:
    from awr.sim.perception.runtime import NonImaging

    return NonImaging(kind, name or kind, sensor_no, spec, np.asarray(p, float), np.eye(3) if R is None else np.asarray(R, float))


class FakeS2(FakeS):
    """FakeS 加 agent_no、ids、vel 与 frame_kind（M19 读取）。"""

    def __init__(self, n: int) -> None:
        super().__init__(n)
        self.agent_no = np.arange(self.capacity, dtype=np.int32) + 1
        self.ids = [f"uav-{i + 1:02d}" for i in range(self.capacity)]
        self.vel = np.zeros((self.capacity, 3))
        self.frame_kind = np.zeros(self.capacity, np.uint8)


@dataclass
class _Sky:
    illum_lx: float
    band: str
    band_code: int
    mor_m: float
    rho_v_gm3: float
    k_cross: float
    t_bg_c: float
    eps_bg: float
    t_sky_c: float
    rain_mmph: float
    l_bg_bands_dba: np.ndarray
    l_bg_dba: float
    acoustic_profile: str = "rural"


@dataclass
class FakeEnv:
    """EnvPerceptionView 替身：均匀消光（与 `optics.sigma_per_m` 同式）、固定照度档与 24 带背景（A 计权、各带等能量）。"""

    illum_lx: float = 1.0e5
    band: str = "day"
    mor_m: float = 20000.0
    rho_v_gm3: float = 7.5
    k_cross: float = 1.0
    t_bg_c: float = 20.0
    eps_bg: float = 0.95
    t_sky_c: float = -20.0
    rain_mmph: float = 0.0
    bg_dba: float = 30.0
    calls: dict = field(default_factory=dict)

    def sky_sample(self, t_ns: int) -> _Sky:
        bands = np.full(24, self.bg_dba - 10.0 * math.log10(24.0))
        return _Sky(self.illum_lx, self.band, {"day": 0, "low": 1, "night": 2}[self.band], self.mor_m, self.rho_v_gm3, self.k_cross, self.t_bg_c,
                    self.eps_bg, self.t_sky_c, self.rain_mmph, bands, self.bg_dba)

    def optical_depth(self, p0: Any, p1: Any, t_ns: int, *, wavelength_nm: float = 550.0) -> np.ndarray:
        from awr.sim.perception.optics import sigma_per_m

        self.calls[wavelength_nm] = self.calls.get(wavelength_nm, 0) + 1
        L = np.linalg.norm(np.asarray(p1, float).reshape(-1, 3) - np.asarray(p0, float).reshape(-1, 3), axis=1)
        return sigma_per_m(self.mor_m, wavelength_nm, self.rho_v_gm3) * L

    @classmethod
    def night(cls, **kw: Any) -> FakeEnv:
        return cls(illum_lx=0.001, band="night", **kw)


@dataclass
class _RE:
    id: str
    agent_no: int
    profile_id: str = "p600_mid360"


class _Roster:
    def __init__(self, n: int) -> None:
        self.by_slot = {i: _RE(f"uav-{i + 1:02d}", i + 1) for i in range(n)}


class PBench:
    """隔离登记表中的 M18 + M19 台架（sandbox100，100 Hz tick）。"""

    def __init__(self, n_uav: int = 1, world: Any = None, *, env: Any = None, seed: int = 7) -> None:
        from awr.sim.fleet.stages import registry as R

        self.R = R
        self._cm = R.isolated_registry()
        self.reg = self._cm.__enter__()
        from awr.sim.perception import plugin as pp
        from awr.sim.targets import plugin as tp

        self.tp, self.pp = tp, pp
        self.trt = tp.install()
        self.prt = pp.install()
        from awr.sim.core.entities import EntityTables
        from awr.sim.fleet.pipeline import StageCtx

        self.ents = EntityTables(self.reg.entity_blocks, limits={"target": 30})
        self.S = FakeS2(n_uav)
        self.events = Events()
        self.world = world
        from awr.contracts.rng_streams import Stream, rng

        self.ctx = StageCtx(tick=0, t_ns=0, dt_tick=0.01, env=env, world=world, events=self.events, entities=self.ents, stage_dt=0.01,
                            roster=_Roster(n_uav), rng={st.name.lower(): rng(seed, int(st)) for st in Stream})
        self.ctx.streams = {}
        self.platforms: dict[int, Any] = {}
        self.prt.platforms_fn = self._platforms
        self.cues: list[tuple[int, Any]] = []
        self.payloads: list[tuple[str, dict]] = []

        def sink(key: str, raw: bytes) -> None:
            import msgpack

            self.payloads.append((key, msgpack.unpackb(raw, raw=False)))

        self.prt.pub.sink = sink
        self.prt._seed = seed

    # ------------------------------------------------------------ 生命周期
    def __enter__(self) -> PBench:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.pp.uninstall()
        self.tp.uninstall()
        self._cm.__exit__(*exc)

    def close(self) -> None:
        self.__exit__(None, None, None)

    # ------------------------------------------------------------ 识别物与平台
    def add(self, target: dict) -> str:
        r = self.reg.commands["target/add"].fn({"op": "target/add", "args": {"target": target}}, self.ctx.tick + 1, self.ctx)
        assert r["status"] == "accepted", r
        return r["result"]["id"]

    def remove(self, tid: str) -> None:
        r = self.reg.commands["target/remove"].fn({"op": "target/remove", "args": {"id": tid}}, self.ctx.tick + 1, self.ctx)
        assert r["status"] == "accepted", r

    def platform(self, slot: int, *, pos: Any = (0.0, 0.0, 60.0), vel: Any = (0.0, 0.0, 0.0), imaging: list | None = None, nonimg: list | None = None,
                 model_id: str = "p600_mid360", frame: str = "multirotor", cue: int = 0, hold_sigma: float = 0.02, lw_bands: Any = None) -> Any:
        from awr.sim.perception.runtime import Platform

        self.S.fly(slot, pos)
        self.S.vel[slot] = vel
        P = Platform(slot, self.S.ids[slot], int(self.S.agent_no[slot]), model_id, frame, np.asarray(pos, float), np.asarray(vel, float), hold_sigma,
                     None if lw_bands is None else np.asarray(lw_bands, float), cue, list(imaging or []), list(nonimg or []))
        self.platforms[slot] = P
        return P

    def _platforms(self, S: Any, ctx: Any, slots: Any, imaging: bool, kinds: tuple[str, ...]) -> list:
        out = []
        for s in np.asarray(slots).tolist():
            P = self.platforms.get(int(s))
            if P is None:
                continue
            Q = replace(P, imaging=list(P.imaging) if imaging else [], nonimg=[N for N in P.nonimg if N.kind in kinds])
            out.append(Q)
        return out

    # ------------------------------------------------------------ 推进
    def step(self, n: int = 1) -> None:
        for _ in range(n):
            self.ctx.tick += 1
            self.ctx.t_ns += 10_000_000
            if self.ctx.tick % 10 == 1:
                self.ctx.stage_dt = 0.1
                self.trt.stage(self.S, self.ctx)
            self.ctx.stage_dt = 0.01
            self.prt.stage(self.S, self.ctx)

    def run(self, seconds: float) -> None:
        self.step(round(seconds * 100))

    @property
    def t(self) -> float:
        return self.ctx.t_ns * 1e-9

    def ev(self, kind: str) -> list[dict]:
        return self.events.of(kind)
