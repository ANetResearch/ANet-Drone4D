"""Paper experiment harness: a full sim-core (all production plugins) built in-process and driven by a fake wall clock.

`PaperSim(world_id, ...)` loads a built world, sets a weather preset, adds vehicles at chosen homes and exposes command,
event and state helpers. The ground truth (dynamics, wind field, contact, battery) is always fully coupled; only what the
decision layer can see is changed, through `awr.sim.core.awareness` (set before the core starts).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from awr.contracts import LAYOUT_ID  # noqa: E402
from awr.contracts.enums import FLIGHTSTATE_NAMES, Lifecycle, sub_name  # noqa: E402
from awr.runtime.bus import LocalBus  # noqa: E402
from awr.runtime.statering import LocalRing  # noqa: E402
from awr.sim.core import awareness as AW  # noqa: E402
from awr.sim.fleet.pipeline import TICK_NS  # noqa: E402
from awr.sim.runtime.config import SimConfig  # noqa: E402
from awr.sim.runtime.main import SimCore  # noqa: E402
from awr.sim.runtime.warm import DEFAULT_PLUGINS  # noqa: E402

OP = {"principal_id": "p-op", "role": "operator", "entry": "api", "seat": True}


class PaperSim:
    def __init__(self, world_id: str, *, awareness: AW.DecisionAwareness | None = None, preset: str | None = None,
                 world_seed: int = 0, profile_id: str = "p600_mid360", plugins: tuple[str, ...] = DEFAULT_PLUGINS,
                 rate: float = 1.0) -> None:
        AW.set_awareness(awareness or AW.DecisionAwareness())
        os.environ.setdefault("AWR_PLAN_POOL", "inline")
        self.profile_id = profile_id
        self.W = [1_000_000_000]
        self._last_gcs = 0
        run = "pp" + secrets.token_hex(4)
        self.path = f"/paper/{run}/state.sim-core"
        self.ring, _ = LocalRing.open_or_create(self.path, capacity=1024, slots=32, layout_id=LAYOUT_ID, id_base=0,
                                                id_count=1024)
        self.bus = LocalBus.open("sim-core", namespace=f"awr/paper/{run}")
        cfg = SimConfig(run_id=run, world_id=world_id, n_vehicles=0, autoplay=True, plugins=plugins, profile_id=profile_id,
                        world_seed=world_seed)
        self.core = SimCore(cfg, self.bus, self.ring, secret=None, wall_ns=lambda: self.W[0])
        self.events: list[tuple[float, str, Any, dict]] = []
        orig = self.core.events.emit

        def emit(kind: str, **kw: Any) -> int:
            data = {k: v for k, v in kw.items() if k not in ("t_sim_ns", "fields", "uav")} | dict(kw.get("fields") or {})
            self.events.append((kw.get("t_sim_ns", 0) * 1e-9, kind, kw.get("uav"), data))
            return orig(kind, **kw)

        self.core.events.emit = emit
        self.core.start()
        self.results: dict[str, tuple[str, int]] = {}
        self.core.engine.subscribe_results("", lambda c: self.results.__setitem__(c.cid, (c.status, int(c.code or 0))))
        self.core._lease_op({"v": 1, "cid": "seat", "op": "seat_claim", "principal": OP})
        self._n = 0
        if rate != 1.0:
            self.core.clock.rate = float(rate)
        if preset:
            # duration 0: the preset applies at the next grid point (a default 30 s transition, possibly routed through
            # intermediate presets, would be superseded by a later env/set and leave the previous weather in place)
            r = self.cmd("env/preset", {"name": preset, "duration_s": 0}, uav=None)
            if r.get("status") not in ("accepted", "succeeded"):
                raise RuntimeError(f"env/preset {preset}: {r}")
        self.advance(1.0)

    def env_set(self, wind: dict[str, float]) -> None:
        self.env_patch({"wind": wind})

    def env_patch(self, patch: dict[str, Any]) -> None:
        r = self.cmd("env/set", {"patch": patch, "mode": "step", "duration_s": 0}, uav=None)
        if r.get("status") not in ("accepted", "succeeded"):
            raise RuntimeError(f"env/set {patch}: {r}")
        self.advance(1.0)

    def wind_at(self, xyz: np.ndarray) -> np.ndarray:
        q = self.env.query(np.asarray(xyz, np.float64).reshape(-1, 3), int(self.core.clock.t_ns), fields=3)
        return np.asarray(q.wind_mean_mps)[: len(np.asarray(xyz).reshape(-1, 3))]

    def mor_at(self, xyz: np.ndarray) -> np.ndarray:
        q = self.env.query(np.asarray(xyz, np.float64).reshape(-1, 3), int(self.core.clock.t_ns), fields=8)
        return np.asarray(q.mor_m)[: len(np.asarray(xyz).reshape(-1, 3))]

    # ------------------------------------------------------------ plumbing
    @property
    def world(self) -> Any:
        return self.core.world

    @property
    def env(self) -> Any:
        import awr.environment.stage as ES

        return ES.service()

    @property
    def S(self) -> Any:
        return self.core.fleet.S

    @property
    def t(self) -> float:
        return self.core.clock.t_ns * 1e-9

    def cmd(self, op: str, args: dict, uav: Any, cid: str | None = None) -> dict:
        self._n += 1
        cid = cid or f"p{self._n:05d}"
        return self.core.engine.handle({"v": 1, "cid": cid, "op": op, "uav": uav, "args": args, "principal": OP,
                                        "lease": None, "t_wall_ns": 0, "epoch_seen": 1, "batch_id": None})

    def _beacon(self) -> None:
        if self.W[0] - self._last_gcs < 200_000_000:
            return
        self._last_gcs = self.W[0]
        import awr.sim.safety as M09

        if M09.SERVICE is not None:
            clk = self.core.clock
            M09.SERVICE.on_gcs_beacon("p-op", "HELD", 50, clk.wall_mono_ns(), clk.paused_total_ns())

    def advance(self, seconds: float) -> None:
        end = self.core.clock.t_ns + int(seconds * 1e9)
        idle = 0
        while self.core.clock.t_ns < end and idle < 50:
            t0 = self.core.clock.t_ns
            self.W[0] += 5 * TICK_NS * max(1, int(self.core.clock.rate))
            self._beacon()
            self.core.iterate()
            idle = idle + 1 if self.core.clock.t_ns == t0 else 0

    def until(self, pred, timeout_s: float, step_s: float = 0.5) -> bool:
        end = self.core.clock.t_ns + int(timeout_s * 1e9)
        while self.core.clock.t_ns < end:
            if pred():
                return True
            self.advance(step_s)
        return bool(pred())

    # ------------------------------------------------------------ fleet
    def flat_slots(self, n: int, spacing: float = 10.0, near: tuple[float, float] | None = None) -> np.ndarray:
        """n pad positions on open flat ground (DSM − DTM < 0.5 m within 4 m) nearest to `near` (default: the core's flat spot)."""
        w = self.world
        c = np.asarray(near if near is not None else self.core._flat_spot(), np.float64)
        g = np.arange(-40, 41) * spacing
        X, Y = np.meshgrid(c[0] + g, c[1] + g)
        P = np.c_[X.ravel(), Y.ravel()]
        off = np.array([[dx, dy] for dx in (-4, 0, 4) for dy in (-4, 0, 4)], np.float64)
        Q = (P[:, None, :] + off[None]).reshape(-1, 2)
        diff = (w.height_dsm(Q) - w.ground_dtm(Q)).reshape(len(P), len(off))
        ok = np.all(np.abs(diff) < 0.5, axis=1)
        P = P[ok]
        P = P[np.argsort(np.linalg.norm(P - c, axis=1), kind="stable")]
        return P[:n]

    def add(self, home_xy: tuple[float, float], *, soc: float = 1.0, vid: str | None = None) -> str:
        x, y = float(home_xy[0]), float(home_xy[1])
        z = float(self.world.height_dsm(np.array([[x, y]]))[0]) if self.world is not None else 0.0
        return self.core.add_vehicle(self.profile_id, (x, y, z), 0.0, vehicle_id=vid, initial_soc=float(soc))

    def slot(self, vid: str) -> int:
        return self.core.roster.resolve(vid).slot

    def ids(self) -> list[str]:
        return [e.id for e in self.core.roster.by_slot.values()]

    def ready(self, timeout_s: float = 20.0) -> bool:
        return self.until(lambda: all(self.core.roster.resolve(u).lifecycle == int(Lifecycle.READY) for u in self.ids()),
                          timeout_s, 0.1)

    def pos(self, vid: str) -> np.ndarray:
        return self.S.enu.pos[self.slot(vid)].copy()

    def soc(self, vid: str) -> float:
        return float(self.S.blocks["battery"]["soc"][self.slot(vid)])

    def energy_wh(self, vid: str) -> float:
        b = self.S.blocks["battery"]
        for k in ("e_used_wh", "used_wh", "wh_used"):
            if k in b:
                return float(b[k][self.slot(vid)])
        return float("nan")

    def fs(self, vid: str) -> tuple[str, str | None]:
        sb = self.S.blocks["safety"]
        s = self.slot(vid)
        f, sub = int(sb["fs"][s]), int(sb["sub"][s])
        return FLIGHTSTATE_NAMES[f], sub_name(f, sub)

    def kinds(self, prefix: str, uav: str | None = None) -> list[tuple[float, str, Any, dict]]:
        return [e for e in self.events if e[1].startswith(prefix) and (uav is None or e[2] == uav)]

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.core.stop()
        with contextlib.suppress(Exception):
            import awr.sim.mission as M10

            rt = M10.installed_runtime()
            if rt is not None:
                rt.close()
        with contextlib.suppress(Exception):
            self.bus.close()
        with contextlib.suppress(Exception):
            LocalRing.remove(self.path)


def wall() -> float:
    return time.perf_counter()
