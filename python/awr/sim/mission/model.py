"""任务数据模型（M10-FR-001；M10 §6.3.1；AWR-12 §3.3.9；AWR-16 §12.2）。

pydantic 2 定义；字段名 snake_case 并带单位后缀（AWR-03 §5.4）；msgpack 与 JSON 往返无损（`to_msgpack`、`from_msgpack`、
`to_json`、`from_json`）。运行期的任务引擎用这些模型做校验与线上表示，热路径使用普通数据结构。
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Literal

import msgpack
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["GENERATORS", "ActionSpec", "ItemEstimate", "MissionConstraints", "MissionItemSpec", "MissionSpec",
           "StartSpec", "TrajectorySpec", "YawSpec", "from_json", "from_msgpack", "new_mission_id", "to_json",
           "to_msgpack"]

GENERATORS = ("lawnmower", "helix_scan", "orbit", "expanding_square", "corridor", "terrain_follow", "formation",
              "follow_path")
ItemKind = Literal["transit", "leg", "dwell", "orbit", "land"]
Primitive = Literal["goto", "follow_path", "orbit", "hover"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MissionConstraints(_M):
    alt_min_agl_m: float = Field(20.0, ge=5, le=300)
    alt_max_m: float | None = None
    clearance_m: float = Field(5.0, ge=2, le=50)
    speed_profile: str | None = None
    energy_reserve: float = Field(0.20, ge=0.1, le=0.5)
    min_sep_m: float = Field(10.0, ge=4.2, le=50)
    transit_planner: Literal["safe_transit", "astar25", "direct"] = "safe_transit"
    prefer_low: bool = False
    layer_dz_m: float = Field(4.0, ge=2, le=20)
    time_window: dict[str, float] | None = None


class StartSpec(_M):
    at_s: float | None = Field(None, ge=0)
    after: str | None = None
    on: Literal["ready"] | None = None
    now: bool | None = None

    @model_validator(mode="after")
    def _one(self) -> StartSpec:
        n = sum(x is not None for x in (self.at_s, self.after, self.on, self.now))
        if n != 1:
            raise ValueError("start must have exactly one of at_s, after, on, now")
        return self


class YawSpec(_M):
    mode: Literal["lookahead", "path", "center", "axis", "fixed", "none"] = "lookahead"
    t_fwd_s: float = Field(1.0, gt=0, le=10)
    center_enu_m: list[float] | None = None
    fixed_rad: float | None = None


class ActionSpec(_M):
    kind: Literal["dwell", "yaw", "gimbal", "camera.trigger", "mark", "sensor", "wait_sync"]
    at: Literal["start", "during", "end"] = "during"
    args: dict[str, Any] = Field(default_factory=dict)


class ItemEstimate(_M):
    len_m: float = 0.0
    duration_s: float = 0.0
    energy_wh: float | None = None
    photos: int = 0


class MissionItemSpec(_M):
    seq: int = Field(ge=0, le=65534)
    kind: ItemKind = "leg"
    primitive: Primitive = "follow_path"
    geometry: dict[str, Any] = Field(default_factory=dict)
    speed_mps: float | None = Field(None, gt=0, le=12)
    acceptance_radius_m: float | None = Field(None, ge=0.5, le=20)
    yaw: YawSpec = Field(default_factory=YawSpec)
    gimbal: dict[str, Any] | None = None
    actions: list[ActionSpec] = Field(default_factory=list, max_length=32)
    traj_key: str | None = None
    est: ItemEstimate | None = None

    def acceptance_m(self, v_mps: float) -> float:
        return self.acceptance_radius_m if self.acceptance_radius_m is not None else max(1.0, 0.25 * float(v_mps))


class TrajectorySpec(_M):
    schema_: Literal["awr.traj.bspline.v1"] = Field("awr.traj.bspline.v1", alias="schema")
    traj_id: int = Field(ge=0)
    vehicle_id: str
    frame: Literal["world"] = "world"
    order: Literal[3] = 3
    ts_s: float = Field(0.5, ge=0.05, le=5.0)
    t0_ns: int = 0
    ctrl_pts: list[list[float]]
    yaw: YawSpec = Field(default_factory=YawSpec)
    limits: dict[str, float] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)
    time_policy: str = "stretch"
    revision: int = Field(0, ge=0)

    @field_validator("ctrl_pts")
    @classmethod
    def _n(cls, v: list[list[float]]) -> list[list[float]]:
        if not 4 <= len(v) <= 4096 or any(len(p) != 3 for p in v):
            raise ValueError("ctrl_pts must be 4..4096 points of 3 numbers")
        return v


class MissionSpec(_M):
    mission_id: str = Field(min_length=1, max_length=96)
    origin: Literal["scenario", "operator", "agent"] = "scenario"
    generator: Literal["lawnmower", "helix_scan", "orbit", "expanding_square", "corridor", "terrain_follow", "formation",
                       "follow_path"]
    params: dict[str, Any]
    vehicle_ids: list[str] = Field(min_length=1, max_length=1000)
    constraints: MissionConstraints = Field(default_factory=MissionConstraints)
    sync_policy: Literal["free", "barrier", "timed"] = "free"
    priority: int = Field(0, ge=0, le=9)
    on_done: Literal["rtl", "hover", "land"] = "rtl"
    on_abort: Literal["rtl", "hover", "land"] = "hover"
    resume_on_lease_return: bool = True
    start: StartSpec | None = None
    revision: int = Field(0, ge=0)


def new_mission_id() -> str:
    return "m-" + secrets.token_hex(4)


def to_msgpack(m: BaseModel) -> bytes:
    return msgpack.packb(m.model_dump(mode="json", by_alias=True, exclude_none=False), use_bin_type=True)


def from_msgpack(cls: type[BaseModel], b: bytes) -> BaseModel:
    return cls.model_validate(msgpack.unpackb(b, raw=False))


def to_json(m: BaseModel) -> str:
    return json.dumps(m.model_dump(mode="json", by_alias=True, exclude_none=False), ensure_ascii=False, sort_keys=True)


def from_json(cls: type[BaseModel], s: str) -> BaseModel:
    return cls.model_validate(json.loads(s))
