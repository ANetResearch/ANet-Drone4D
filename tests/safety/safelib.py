"""M09 测试台：在隔离登记表中装配 M09 的 SimCore，以注入的假墙钟单步驱动（不睡眠、确定性；参照 tests/sim/test_skeleton.py）。"""

from __future__ import annotations

import contextlib
import secrets
from typing import Any

import numpy as np

import awr.sim.safety as M09
from awr.contracts import LAYOUT_ID
from awr.contracts.enums import FLIGHTSTATE_NAMES, FlightState, Lifecycle, sub_name
from awr.runtime.bus import LocalBus
from awr.runtime.principal import Principal, derive_key, sign_principal
from awr.runtime.statering import LocalRing
from awr.sim.core import metrics as MET
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import registry as R
from awr.sim.runtime.config import SimConfig
from awr.sim.runtime.main import SimCore

FS = FlightState
UAV = "p600-01"


class Harness:
    """SimCore + M09（隔离登记表）。`world` 为 M04 WorldQuery（None 时为平坦地面，围栏检查跳过）。"""

    def __init__(self, *, n: int = 1, world: Any = None, params: Any = None, spawn: tuple[float, float] | None = None,
                 kernel: str | None = None, profile: str = "p600_mid360", spacing: float = 6.0,
                 fleet_kernel: str | None = None, limits: str | None = None) -> None:
        self._stack = contextlib.ExitStack()
        self.reg = self._stack.enter_context(R.isolated_registry())
        self._stack.enter_context(MET.isolated_metrics())
        self.svc = M09.install(params, sync_rows=True, kernel=kernel, warm=False)
        self.events: list[dict] = []
        self.svc.event_log = self.events
        self.W = [1_000_000_000]
        self.secret = secrets.token_bytes(32)
        self.run_id = "rm09" + secrets.token_hex(3)
        self.path = f"/m09/{self.run_id}/state.sim-core"
        self.ring, _ = LocalRing.open_or_create(self.path, capacity=1024, slots=32, layout_id=LAYOUT_ID, id_base=0,
                                                id_count=1024)
        self.bus = LocalBus.open("sim-core", namespace=f"awr/test/{self.run_id}")
        from awr.sim.fleet.fleet import FleetConfig

        fc = FleetConfig() if fleet_kernel is None else FleetConfig(kernel=fleet_kernel)
        cfg = SimConfig(run_id=self.run_id, n_vehicles=n, load_world=False, autoplay=True, spawn_xy=spawn,
                        profile_id=profile, spawn_spacing_m=spacing, fleet=fc, limits_profile=limits)
        self.core = SimCore(cfg, self.bus, self.ring, secret=self.secret, wall_ns=lambda: self.W[0], reg=self.reg,
                            world=world)
        self.core.start()
        self.k_entry = derive_key(self.secret, self.run_id, "entry")
        self.n = 0
        self.gcs_age_ms: int | None = None  # 非 None 时每次推进按 5 Hz 投递席位信标（ping 年龄 = 该值）
        self._last_gcs = 0

    # ---------------------------------------------------------------- 基础
    @property
    def rt(self):
        return self.svc.rt

    @property
    def S(self):
        return self.core.fleet.S

    def principal(self, cid: str, pid: str = "p-op", role: str = "operator", seat: bool = True) -> dict:
        p = Principal(pid, role, "api", "c-1", seat)
        d = p.fields()
        d["sig"] = sign_principal(p, cid, self.k_entry)
        return d

    def step(self, n_iter: int = 1, dt_ticks: int = 1) -> None:
        for _ in range(n_iter):
            self.W[0] += dt_ticks * TICK_NS
            self._beacon()
            self.core.iterate()

    def advance(self, seconds: float) -> None:
        end = self.core.clock.t_ns + int(seconds * 1e9)
        guard = 0
        while self.core.clock.t_ns < end and guard < 200_000:
            self.W[0] += 5 * TICK_NS
            self._beacon()
            self.core.iterate()
            guard += 1

    def _beacon(self) -> None:
        if self.gcs_age_ms is None:
            return
        if self.W[0] - self._last_gcs >= 200_000_000:
            self._last_gcs = self.W[0]
            clk = self.core.clock
            self.svc.on_gcs_beacon("p-op", "HELD", int(self.gcs_age_ms), clk.wall_mono_ns(), clk.paused_total_ns())

    def until(self, pred, timeout_s: float, step_s: float = 0.02) -> bool:
        t_end = self.core.clock.t_ns + int(timeout_s * 1e9)
        while self.core.clock.t_ns < t_end:
            if pred():
                return True
            self.advance(step_s)
        return bool(pred())

    def cmd(self, op: str, args: dict | None = None, *, cid: str | None = None, uav: Any = UAV, **pk) -> dict:
        self.n += 1
        cid = cid or f"c-t{self.n:04d}"
        return self.core.engine.handle({"v": 1, "cid": cid, "op": op, "uav": uav, "args": args or {},
                                        "principal": self.principal(cid, **pk), "lease": None, "t_wall_ns": 0,
                                        "epoch_seen": 1, "batch_id": None})

    def call(self, cid: str):
        ent = self.core.engine.idem.get(cid)
        return ent.call if ent is not None else None

    def seat(self, pid: str = "p-op") -> dict:
        return self.core._lease_op({"v": 1, "cid": "seat-1", "op": "seat_claim", "principal": self.principal("seat-1", pid)})

    def slot(self, uav: str = UAV) -> int:
        return self.core.roster.resolve(uav).slot

    def fs(self, uav: str = UAV) -> tuple[str, str | None]:
        sb = self.S.blocks["safety"]
        s = self.slot(uav)
        f, sub = int(sb["fs"][s]), int(sb["sub"][s])
        return FLIGHTSTATE_NAMES[f], sub_name(f, sub)

    def state(self, uav: str = UAV) -> str:
        a, b = self.fs(uav)
        return f"{a}/{b}"

    def pos(self, uav: str = UAV) -> np.ndarray:
        return self.S.enu.pos[self.slot(uav)].copy()

    def ready(self, timeout_s: float = 10.0) -> None:
        self.seat()
        ids = [e.id for e in self.core.roster.by_slot.values()]
        assert self.until(lambda: all(self.core.roster.resolve(u).lifecycle == int(Lifecycle.READY) for u in ids), timeout_s)
        self.advance(0.1)

    def takeoff(self, alt: float = 10.0, uav: str = UAV, timeout_s: float = 40.0) -> None:
        cid = f"to-{uav}-{self.n}"
        rep = self.cmd("takeoff", {"alt_m": alt}, cid=cid, uav=uav)
        assert rep["status"] == "accepted", rep
        c = self.call(cid)
        assert self.until(lambda: c.final, timeout_s), self.state(uav)
        assert c.status == "succeeded", (c.status, c.code, self.state(uav))

    def takeoff_all(self, alt: float = 10.0, timeout_s: float = 40.0) -> list[str]:
        ids = [e.id for e in self.core.roster.by_slot.values()]
        cids = []
        for u in ids:
            cid = f"to-{u}-{self.n}"
            assert self.cmd("takeoff", {"alt_m": alt}, cid=cid, uav=u)["status"] == "accepted"
            cids.append(self.call(cid))
        assert self.until(lambda: all(c.final for c in cids), timeout_s)
        assert all(c.status == "succeeded" for c in cids), [self.state(u) for u in ids]
        return ids

    def goto(self, pos, uav: str = UAV, wait: bool = True, timeout_s: float = 120.0, **kw):
        cid = f"go-{uav}-{self.n}"
        rep = self.cmd("goto", {"pos": list(map(float, pos)), "route": "direct", **kw}, cid=cid, uav=uav)
        if wait and rep["status"] == "accepted":
            c = self.call(cid)  # 幂等表只保留 60 s【墙钟】，持有调用对象
            self.until(lambda: c.final, timeout_s)
        return rep, cid

    def codes(self, uav: str | None = UAV) -> list[str]:
        return [e.get("code") for e in self.events if e.get("kind", "").startswith("safety.") and
                (uav is None or e.get("uav") == uav)]

    def states(self, uav: str | None = UAV) -> list[str]:
        return [e["to"] for e in self.events if e.get("kind") == "uav.state" and (uav is None or e.get("uav") == uav)]

    def close(self) -> None:
        try:
            if self.svc.rt is not None:
                self.svc.rt.close()
            self.core.stop()
            self.bus.close()
            LocalRing.remove(self.path)
        finally:
            self._stack.close()


# ====================================================================== 单元测试台（无 SimCore）
class FakeClock:
    def __init__(self) -> None:
        self.wall = 1_000_000_000
        self.paused = 0
        self.rate = 1.0

    def wall_mono_ns(self) -> int:
        return self.wall

    def paused_total_ns(self) -> int:
        return self.paused


class FakeEvents:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def emit(self, kind: str, *, t_sim_ns: int, severity: int = 0, uav=None, cid=None, fields=None, **data) -> int:
        self.items.append({"kind": kind, "t_sim_ns": t_sim_ns, "severity": severity, "uav": uav, **(fields or {}), **data})
        return len(self.items)


class FakeCalls:
    def __init__(self) -> None:
        self.resolved: list[tuple[list[int], str, int]] = []
        self.inputlog = None

    def resolve_calls(self, slots, status: str, code: int, reason: str) -> None:
        self.resolved.append(([int(s) for s in np.atleast_1d(slots)], status, int(code)))


class UnitRig:
    """FleetState（M09 状态块）+ SafetyRuntime，直接驱动 M09 stage；不运行 M08 物理。"""

    def __init__(self, n: int = 4, *, world: Any = None, params: Any = None, capacity: int = 64,
                 profile: str = "p600_mid360") -> None:
        from awr.contracts.enums import Lifecycle as LC
        from awr.sim.core.supervisor_queue import SupervisorQueue
        from awr.sim.fleet.pipeline import StageCtx
        from awr.sim.fleet.profiles import ProfileTable
        from awr.sim.fleet.stages.registry import StateBlockSpec
        from awr.sim.fleet.state import FleetState
        from awr.sim.safety.service import SafetyService
        from awr.sim.safety.state import BATTERY_FIELDS, SAFETY_FIELDS

        def spec(name, fields):
            return StateBlockSpec(name, "M09", {k: (np.dtype(v[0]), tuple(v[1])) for k, v in fields.items()})

        self.T = ProfileTable()
        self.S = S = FleetState(capacity, {"safety": spec("safety", SAFETY_FIELDS), "battery": spec("battery", BATTERY_FIELDS)})
        pid = self.T.index(profile)
        for i in range(n):
            S.active[i] = True
            S.lifecycle[i] = int(LC.READY)
            S.ids[i] = f"u{i:02d}"
            S.agent_no[i] = i + 1
            S.profile_id[i] = pid
            S.limits_id[i] = self.T.limits_index(None, profile)
            S.blocks["battery"]["soc"][i] = 1.0
            S.landed[i] = True
        self.ctx = StageCtx()
        self.ctx.profiles = self.T
        self.ctx.supervisor = SupervisorQueue()
        self.ctx.events = FakeEvents()
        self.ctx.clock = FakeClock()
        self.ctx.calls = FakeCalls()
        self.ctx.world = world
        self.svc = SafetyService(params, sync_rows=True)
        self.rt = self.svc.bind(S, self.ctx)
        self.rt.begin(self.ctx)
        self.sb = S.blocks["safety"]
        self.bb = S.blocks["battery"]

    def set(self, slot: int, fs: int, sub: int = 0, *, auto: bool = False, air: bool = True, t_enter_s: float = -10.0) -> None:
        sb = self.sb
        sb["fs"][slot] = fs
        sb["sub"][slot] = sub
        sb["fs_auto"][slot] = auto
        sb["latch"][slot] = 1 if fs == FS.ELAND else 2 if fs == FS.FAILSAFE else 0
        sb["t_enter_ns"][slot] = int(t_enter_s * 1e9)
        self.S.in_air[slot] = air
        self.S.landed[slot] = not air
        sb["landed_prev"][slot] = not air
        sb["last_mode_t"][slot] = self.S.mode_t[slot]

    def tick(self, n: int = 1, stages: tuple[str, ...] = ("fsm",)) -> None:
        from awr.sim.fleet.pipeline import TICK_NS as TN

        for _ in range(n):
            self.ctx.tick += 1
            self.ctx.t_ns = self.ctx.tick * TN
            self.ctx.clock.wall += TN
            for st in stages:
                self.rt.run(st, self.ctx)

    def fs(self, slot: int) -> tuple[int, int]:
        return int(self.sb["fs"][slot]), int(self.sb["sub"][slot])

    def events(self, kind_prefix: str = "safety.") -> list[dict]:
        return [e for e in self.ctx.events.items if e["kind"].startswith(kind_prefix)]
