"""后端与适配器协议（M08 §7.1.2、§7.1.5；AWR-10 §3.4 骨架；P-07；M08-FR-065，签名在 D1-MS1 冻结）。

`SimBackend`、`EntityAdapter`、`DroneAdapter` 三层接口让 Mock（D1）、Replay（L0）、PX4 SIH 与 Prometheus（V0.2 起）
共用同一上层（CommandEngine、Gateway、UI）。`EnvironmentService` 由 M07 实现（M07 §7.1），M08 在 env stage 之外不依赖其实现。
后端能力声明从 `packages/contracts/rt/caps/<backend>.json` 加载（`load_caps`）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import numpy as np

from awr.contracts._paths import schema_path

if TYPE_CHECKING:
    from awr.runtime.bus import Bus
    from awr.runtime.statering import StateRing

__all__ = [
    "BackendCaps",
    "ClockCaps",
    "DispatchResult",
    "DroneAdapter",
    "EntityAdapter",
    "EntitySpec",
    "EnvironmentService",
    "Kind",
    "Pose",
    "SimBackend",
    "load_caps",
]


class Kind(StrEnum):
    UAV = "uav"
    UGV = "ugv"
    ROBOT = "robot"
    VEHICLE = "vehicle"
    SENSOR = "sensor"
    HUMAN = "human"


@dataclass(frozen=True)
class ClockCaps:
    mode: Literal["lockstep", "slaved_realtime", "free_running", "live"]
    pausable: bool
    max_speed: float
    steppable: bool

    def to_json(self) -> dict[str, Any]:
        return {"mode": self.mode, "pausable": self.pausable, "max_speed": self.max_speed, "steppable": self.steppable}


@dataclass(frozen=True)
class BackendCaps:
    backend: str
    version: int
    kinds: tuple[Kind, ...]
    max_vehicles: int
    clock: ClockCaps
    cmd: Mapping[str, object]
    ack: Mapping[str, object]
    trust_ceiling: int
    truth: bool | Literal["optional"]
    raw: Mapping[str, Any]

    def cmd_impl(self, op: str) -> str:
        """命令实现类别：native、emulated、gateway、none；caps 未列出的命令视为 none（准入第 ⑦ 步，109）。"""
        v = self.cmd.get(op)
        if v is None:
            return "none"
        if isinstance(v, str):
            return v
        if isinstance(v, Mapping):
            return str(v.get("impl", "native"))
        return "native"


@cache
def load_caps(backend: str) -> BackendCaps:
    d = json.loads(schema_path(f"rt/caps/{backend}.json").read_text(encoding="utf-8"))
    c = d["clock"]
    return BackendCaps(backend=d["backend"], version=int(d["version"]), kinds=tuple(Kind(k) for k in d["kinds"]),
                       max_vehicles=int(d["max_vehicles"]),
                       clock=ClockCaps(c["mode"], bool(c["pausable"]), float(c["max_speed"]), bool(c["steppable"])),
                       cmd=dict(d.get("cmd", {})), ack=dict(d.get("ack", {})), trust_ceiling=int(d.get("trust_ceiling", 0)),
                       truth=d.get("truth", False), raw=d)


@dataclass(frozen=True)
class EntitySpec:
    entity_id: str | None
    kind: Kind
    profile_id: str
    limits_profile: str | None
    home_enu_m: tuple[float, float, float | None]
    yaw_rad: float
    initial_soc: float = 1.0
    capabilities: tuple[str, ...] = ()
    source: dict | None = None


@dataclass(frozen=True)
class DispatchResult:
    entity_id: str
    ok: bool
    code: int
    native_ack: bool
    detail: str | None = None


@dataclass(frozen=True)
class Pose:
    t_sim_ns: int
    pos: np.ndarray  # ENU m
    q_xyzw: np.ndarray  # WORLD<-FLU
    vel_mps: np.ndarray
    omega_rad_s: np.ndarray  # FLU


@runtime_checkable
class EnvironmentService(Protocol):
    """M07 实现（M07 §7.1；AWR-10 §3.4）。`pos` 为 World ENU（N×3，float64），返回的 wind 为 ENU 去向矢量。"""

    def query(self, pos: np.ndarray, t_sim_ns: int, *, fields: int, frame: Any = ..., vel: np.ndarray | None = None,
              quat_xyzw: np.ndarray | None = None, agent_idx: np.ndarray | None = None, out: Any = None) -> Any: ...

    def keyframe(self) -> Any: ...

    def apply(self, op: Any, apply_tick: int) -> Any: ...

    def checkpoint(self) -> bytes: ...

    def restore(self, blob: bytes) -> None: ...


@runtime_checkable
class EntityAdapter(Protocol):
    kind: Kind
    id: str
    agent_no: int

    def lifecycle(self) -> Any: ...

    def pose(self) -> Pose: ...

    def capabilities(self) -> list[str]: ...


@runtime_checkable
class DroneAdapter(EntityAdapter, Protocol):
    def derive_state(self) -> Any: ...

    def frames(self) -> Any: ...


@runtime_checkable
class SimBackend(Protocol):
    name: str
    caps: BackendCaps

    def attach(self, world: Any, clock: Any, bus: Bus, ring: StateRing | None) -> None: ...

    def spawn(self, spec: EntitySpec) -> EntityAdapter: ...

    def despawn(self, entity_id: str) -> None: ...

    def dispatch_batch(self, cmds: Sequence[Any]) -> list[DispatchResult]: ...

    def step(self, tick: int) -> None: ...

    def snapshot(self) -> bytes: ...

    def restore(self, blob: bytes) -> None: ...
