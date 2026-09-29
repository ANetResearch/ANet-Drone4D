"""MockBackend：包装 FleetSim 的 Mock 后端（M08-FR-066、FR-067；M08 §6.11.3；能力声明 `packages/contracts/rt/caps/mock.json`）。

实现 10 §3.4 骨架：`attach`、`spawn`（分配 slot 与 agent_no，Mock 生命周期由 roster 推进）、`despawn`、`dispatch_batch`（准入 ④–⑧
之后的向量化分发；参数为 ENU、已校验的 StagedCmd 或等价 dict，在 FleetSim 上直接进入运动模式，同 tick 原生确认
`native_ack = true`）、`step`（lockstep 推进 n 个主时钟 tick）、`snapshot`/`restore`（checkpoint 数组与小对象，msgpack）。
`MockDroneView` 是 SoA 行视图，不持有逐机对象状态；`derive_state()` 取 FSM（M09 safety 块），绝不从显示值反推。
sim-core 进程内的命令路径是 CommandEngine（staged → ingest），本类用于一致性套件与独立装配。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import msgpack
import numpy as np

from awr.contracts.enums import LIFECYCLE_NAMES
from awr.contracts.reasons import Reason
from awr.world.georef.frames import enu_to_ned, yaw_ned_from_enu

from ..fleet import actions as ACT
from ..fleet import kernels_l1 as K
from ..fleet.fleet import FleetSim
from ..fleet.pipeline import StageCtx
from .base import BackendCaps, DispatchResult, EntitySpec, Kind, Pose, load_caps

__all__ = ["MockBackend", "MockDroneView"]


class MockDroneView:
    """DroneAdapter 的 Mock 实现：FleetState 的行视图。"""

    kind = Kind.UAV

    def __init__(self, fleet: FleetSim, slot: int, entity_id: str, agent_no: int) -> None:
        self._f = fleet
        self.slot = slot
        self.id = entity_id
        self.agent_no = agent_no

    def lifecycle(self) -> str:
        return LIFECYCLE_NAMES[int(self._f.S.lifecycle[self.slot])]

    def pose(self) -> Pose:
        S = self._f.S
        e = S.enu
        return Pose(S.t_ns, e.pos[self.slot].copy(), e.q_xyzw[self.slot].copy(), e.vel[self.slot].copy(),
                    e.omega_flu[self.slot].copy())

    def capabilities(self) -> list[str]:
        return []

    def derive_state(self) -> dict[str, Any]:
        S = self._f.S
        sb = S.blocks["safety"]
        return {"fs": int(sb["fs"][self.slot]), "sub": int(sb["sub"][self.slot]), "pose_src": "TRUTH", "native": None}

    def frames(self) -> Any:
        return None


class MockBackend:
    name = "mock"

    def __init__(self, fleet: FleetSim, *, ctx: StageCtx | None = None) -> None:
        self.fleet = fleet
        self.caps: BackendCaps = load_caps("mock")
        self.ctx = ctx or StageCtx(profiles=fleet.T, paths=fleet.PB, events=fleet.events)
        self._next = 0
        self.views: dict[str, MockDroneView] = {}
        self.world = self.clock = self.bus = self.ring = None

    def attach(self, world: Any, clock: Any, bus: Any, ring: Any) -> None:
        self.world, self.clock, self.bus, self.ring = world, clock, bus, ring

    def spawn(self, spec: EntitySpec, *, slot: int | None = None, agent_no: int | None = None) -> MockDroneView:
        S = self.fleet.S
        s = int(np.flatnonzero(~S.active)[0]) if slot is None else slot
        no = self._next if agent_no is None else agent_no
        self._next = max(self._next, no + 1)
        eid = spec.entity_id or f"mock-{no:02d}"
        self.fleet.add(spec, slot=s, agent_no=no, entity_id=eid, t_s=self.ctx.t_ns * 1e-9)
        S.lifecycle[s] = 4  # 独立装配时直接 READY（sim-core 内由 roster 推进 PENDING → READY）
        v = MockDroneView(self.fleet, s, eid, no)
        self.views[eid] = v
        return v

    def despawn(self, entity_id: str) -> None:
        v = self.views.pop(entity_id, None)
        if v is not None:
            self.fleet.remove(v.slot)

    def dispatch_batch(self, cmds: Sequence[Any]) -> list[DispatchResult]:
        """已准入命令的向量化分发（ENU 参数）；Mock 同 tick 原生确认。"""
        out = []
        S, t = self.fleet.S, self.ctx.t_ns * 1e-9
        for c in cmds:
            op = c.op if hasattr(c, "op") else c["op"]
            args = c.args if hasattr(c, "args") else c.get("args", {})
            slots = np.asarray(c.slots if hasattr(c, "slots") else c["slots"], np.int64)
            ids = [S.ids[int(s)] or "" for s in slots]
            code = self._exec(op, args, slots, t)
            out += [DispatchResult(i, code == 0, code, code == 0) for i in ids]
        return out

    def _exec(self, op: str, args: dict, s: np.ndarray, t: float) -> int:
        S, T = self.fleet.S, self.fleet.T
        if self.caps.cmd_impl(op) == "none" and op not in ("arm", "disarm", "cancel", "resume", "velocity_stop"):
            return int(Reason.BACKEND_UNSUPPORTED)
        if op == "takeoff":
            ACT.begin_takeoff(S, s, float(args.get("alt_m", 2.5)), t)
        elif op == "goto":
            y = args.get("yaw_rad")
            ACT.begin_goto(S, s, enu_to_ned(np.asarray(args["pos"], np.float64)), float(args.get("speed_mps") or np.nan),
                           None if y is None else float(yaw_ned_from_enu(float(y))), t)
        elif op in ("hover", "safety_stop", "pause"):
            ACT.begin_hold(S, s, t)
        elif op == "land":
            ACT.begin_land(S, s, None, t)
        elif op == "rtl":
            z = np.maximum(S.enu.pos[s, 2], S.enu.home[s, 2] + 30.0)
            ACT.begin_rtl(S, s, z, np.full(s.size, np.nan), t)
        elif op == "orbit":
            ACT.begin_orbit(S, s, enu_to_ned(np.asarray(args["center"], np.float64)), float(args["radius_m"]),
                            float(args.get("speed_mps") or T.LT[S.limits_id[s[0]], K.L_CRUISE]), bool(args.get("cw", True)),
                            float(args.get("turns", 0) or 0), args.get("yaw_behavior", "center"), None, t)
        elif op == "follow_path":
            w = enu_to_ned(np.asarray(args["waypoints"], np.float64))
            for i in s:
                if ACT.begin_path(S, self.fleet.PB, T, int(i), w, float(args.get("speed_mps") or np.nan), None, t) is None:
                    return int(Reason.PARAM_OUT_OF_RANGE)
        elif op == "velocity":
            ACT.begin_velocity(S, s, args.get("frame") == "body", float(args.get("vmax_mps") or np.inf),
                               bool(args.get("hold_alt", True)), t)
        elif op == "kill":
            ACT.begin_kill(S, s, t)
        elif op == "arm":
            ACT.arm(S, s, t)
        elif op == "disarm":
            ACT.disarm(S, s, t)
        else:
            return int(Reason.BACKEND_UNSUPPORTED)
        return 0

    def step(self, tick: int) -> None:
        """lockstep：推进到主时钟 tick（含）。"""
        n = int(tick) - int(self.ctx.tick)
        if n > 0:
            self.fleet.step(self.ctx, n)

    def snapshot(self) -> bytes:
        arrays = self.fleet.checkpoint_arrays()
        meta = self.fleet.checkpoint_meta()
        meta["ctx_tick"] = int(self.ctx.tick)
        return msgpack.packb({"meta": meta, "arrays": {k: [v.dtype.str, list(v.shape), v.tobytes()] for k, v in arrays.items()}},
                             use_bin_type=True)

    def restore(self, blob: bytes) -> None:
        d = msgpack.unpackb(blob, raw=False, strict_map_key=False)
        arrays = {k: np.frombuffer(b, np.dtype(dt)).reshape(shape).copy() for k, (dt, shape, b) in d["arrays"].items()}
        self.fleet.restore(arrays, d["meta"])
        self.ctx.tick = int(d["meta"]["ctx_tick"])
        self.ctx.t_ns = self.ctx.tick * 4_000_000
