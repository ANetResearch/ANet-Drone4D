"""M20 数据模型（M20 PRD §6.2、§7.3；契约 `packages/contracts/tasking/*.schema.json`）。

纯数据类：机型摘要 `ModelInfo`（含扫描与站位用的成像传感器 `Camera`）、机体 `VehicleView`、机巢 `NestView`、禁入圆 `Keepout`、
工作项 `WorkItem`、快照 `Snapshot`、分配 `Assignment` 与计划 `Plan`；`to_payload`/`from_payload` 只用 dict、list、str、int、float、
bool、None 与 numpy 数组（共享规划服务的线上编码 `jobs.packb`，d2-WP-15-to-M20 第 3 条）。坐标一律 world ENU（m），时间为仿真秒。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

__all__ = [
    "FRAMES", "LEVELS", "LEVEL_RANK", "PRIORITY_OF", "RELAY_PRIORITY", "Assignment", "Camera", "Keepout", "ModelInfo", "NestView",
    "Plan", "Snapshot", "TaskingError", "VehicleView", "WorkItem", "canonical_sha256", "level_at_least", "rnd",
]

LEVELS = ("none", "D", "R", "I")
LEVEL_RANK = {"none": 0, "D": 1, "R": 2, "I": 3}
PRIORITY_OF = {"low": 3, "normal": 5, "high": 7}
RELAY_PRIORITY = 8                     # M20-FR-061：接力缺省优先级 8，高于巡查（5）
FRAMES = ("multirotor", "fixed_wing", "vtol")
PERSISTENT_KINDS = ("station", "relay_station", "subregion", "perimeter_phase", "guard_cycle", "segment_loop")


class TaskingError(Exception):
    """带原因码的编排错误（580–586、105、110、305、505、525、583）；`detail` 进入 REST problem+json 与命令拒绝。"""

    def __init__(self, code: int, why: str, **detail: Any) -> None:
        super().__init__(f"{code} {why}")
        self.code = int(code)
        self.why = str(why)
        self.detail = {"why": self.why, **{k: v for k, v in detail.items() if v is not None}}

    def to_dict(self) -> dict:
        return {"code": self.code, "why": self.why, "detail": self.detail}


def level_at_least(level: str | None, req: str | None) -> bool:
    return LEVEL_RANK.get(str(level or "none"), 0) >= LEVEL_RANK.get(str(req or "R"), 2)


def rnd(x: Any, nd: int = 3) -> Any:
    """确定性取整（结果哈希与线上形状）：浮点按 nd 位小数，-0.0 归 0，非有限值为 None。"""
    if isinstance(x, (float, np.floating)):
        f = float(x)
        if not math.isfinite(f):
            return None
        v = round(f, nd)
        return 0.0 if v == 0 else v
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, np.ndarray):
        return [rnd(v, nd) for v in x.tolist()]
    if isinstance(x, (list, tuple)):
        return [rnd(v, nd) for v in x]
    if isinstance(x, dict):
        return {k: rnd(v, nd) for k, v in x.items()}
    return x


def canonical_sha256(obj: Any) -> str:
    """键排序、紧凑分隔的 JSON 的 sha256（同一快照两次分配逐字节一致的判据，M20-AC-012）。"""
    return hashlib.sha256(json.dumps(rnd(obj, 6), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ 传感器与机型
@dataclass(frozen=True)
class Camera:
    """扫描与站位使用的成像传感器摘要（M19 SensorSpecP 的子集：输出档的有效像元与焦距范围）。"""

    name: str
    kind: str = "eo"                    # eo | nir | lwir
    w_out_px: int = 1920
    h_out_px: int = 1080
    p_um: float = 2.9                   # 输出档的有效像元 p'（µm）
    f_min_mm: float = 4.8
    f_max_mm: float = 48.0
    fixed_f_mm: tuple[float, ...] = ()  # 定焦镜头组（空为连续变焦）
    t_exp_s: float = 1.0 / 500.0        # 日间曝光（扫描速度上限 0.5·g/t_exp）
    fps: float = 2.0                    # 触发频率上限（帧间航向重叠约束）

    def to_payload(self) -> dict:
        return {"name": self.name, "kind": self.kind, "w_out_px": int(self.w_out_px), "h_out_px": int(self.h_out_px), "p_um": float(self.p_um),
                "f_min_mm": float(self.f_min_mm), "f_max_mm": float(self.f_max_mm), "fixed_f_mm": [float(x) for x in self.fixed_f_mm],
                "t_exp_s": float(self.t_exp_s), "fps": float(self.fps)}

    @staticmethod
    def from_payload(d: dict) -> Camera:
        return Camera(str(d["name"]), str(d.get("kind", "eo")), int(d.get("w_out_px", 1920)), int(d.get("h_out_px", 1080)),
                      float(d.get("p_um", 2.9)), float(d.get("f_min_mm", 4.8)), float(d.get("f_max_mm", 48.0)),
                      tuple(float(x) for x in d.get("fixed_f_mm") or ()), float(d.get("t_exp_s", 0.002)), float(d.get("fps", 2.0)))


@dataclass(frozen=True)
class ModelInfo:
    """机型摘要（M21 目录与 M08 ProfileTable 派生，sim-core 插件构造）：功率按角色（悬停、巡航、盘旋），能量为可用能量 E_use。"""

    id: str
    frame: str = "multirotor"           # multirotor | fixed_wing | vtol
    v_cruise_mps: float = 8.0
    v_max_mps: float = 12.0
    climb_mps: float = 3.0
    mass_kg: float = 3.8
    p_hover_w: float = 401.0
    p_cruise_w: float = 420.0
    p_loiter_w: float = 401.0
    e_use_wh: float = 193.8
    link_range_m: float = 3500.0
    wind_max_mps: float = 12.0
    r_loiter_min_m: float = 0.0
    capabilities: tuple[str, ...] = ()
    cams: tuple[Camera, ...] = ()
    station_sensors: tuple[str, ...] = ()   # 接力可用的成像传感器名（M19 model_sensor_spec 的 sensor 参数）
    pack: dict = field(default_factory=dict)  # {pack_id, cells, capacity_ah, c_max, packs_per_swap}
    r_loiter_w: tuple = ()                    # 固定翼与垂起机：((风速, 盘旋半径下限), …)（M21 `r_loiter_min(w)`，风下作业高度选择）

    def r_loiter_at(self, w: float) -> float:
        """风速 w 下的盘旋半径下限（按 `r_loiter_w` 线性插值；没有表时取 r_loiter_min_m）。"""
        if not self.r_loiter_w:
            return float(self.r_loiter_min_m)
        ws = [float(a) for a, _r in self.r_loiter_w]
        rs = [float(r) for _a, r in self.r_loiter_w]
        return float(np.interp(float(w), ws, rs))

    @property
    def hovers(self) -> bool:
        return self.frame in ("multirotor", "vtol")

    def p_role(self, role: str) -> float:
        if role == "hover":
            return self.p_loiter_w if self.frame == "fixed_wing" else self.p_hover_w
        if role == "loiter":
            return self.p_loiter_w if self.frame != "multirotor" else self.p_hover_w
        return self.p_cruise_w

    def station_role(self) -> str:
        return "hover" if self.frame == "multirotor" else "loiter"

    def endurance_s(self, role: str = "cruise") -> float:
        p = max(self.p_role(role), 1e-6)
        return self.e_use_wh * 3600.0 / p

    def to_payload(self) -> dict:
        return {"id": self.id, "frame": self.frame, "v_cruise_mps": self.v_cruise_mps, "v_max_mps": self.v_max_mps, "climb_mps": self.climb_mps,
                "mass_kg": self.mass_kg, "p_hover_w": self.p_hover_w, "p_cruise_w": self.p_cruise_w, "p_loiter_w": self.p_loiter_w,
                "e_use_wh": self.e_use_wh, "link_range_m": self.link_range_m, "wind_max_mps": self.wind_max_mps,
                "r_loiter_min_m": self.r_loiter_min_m, "capabilities": list(self.capabilities), "cams": [c.to_payload() for c in self.cams],
                "station_sensors": list(self.station_sensors), "pack": dict(self.pack),
                **({"r_loiter_w": [[float(a), float(b)] for a, b in self.r_loiter_w]} if self.r_loiter_w else {})}

    @staticmethod
    def from_payload(d: dict) -> ModelInfo:
        return ModelInfo(str(d["id"]), str(d.get("frame", "multirotor")), float(d.get("v_cruise_mps", 8.0)), float(d.get("v_max_mps", 12.0)),
                         float(d.get("climb_mps", 3.0)), float(d.get("mass_kg", 3.8)), float(d.get("p_hover_w", 401.0)),
                         float(d.get("p_cruise_w", 420.0)), float(d.get("p_loiter_w", 401.0)), float(d.get("e_use_wh", 193.8)),
                         float(d.get("link_range_m", 3500.0)), float(d.get("wind_max_mps", 12.0)), float(d.get("r_loiter_min_m", 0.0)),
                         tuple(str(x) for x in d.get("capabilities") or ()), tuple(Camera.from_payload(c) for c in d.get("cams") or ()),
                         tuple(str(x) for x in d.get("station_sensors") or ()), dict(d.get("pack") or {}),
                         tuple((float(a), float(b)) for a, b in d.get("r_loiter_w") or ()))


# ------------------------------------------------------------------------------------------------ 机体与机巢
@dataclass(frozen=True)
class VehicleView:
    """分配候选：`state` ∈ {ready（在巢可起飞或充电中，t_ready 给出可起飞时刻）, standby（空中待命）, busy, unavailable}。"""

    id: str
    model_id: str
    nest_id: str | None
    pos: tuple[float, float, float]
    soc: float
    e_avail_wh: float
    t_ready_s: float
    state: str = "ready"
    airborne: bool = False
    holds: tuple[str, ...] = ()          # 已持有的工作项（同任务硬约束）
    sensors: tuple[str, ...] = ()

    def to_payload(self) -> dict:
        return {"id": self.id, "model_id": self.model_id, "nest_id": self.nest_id, "pos": [float(x) for x in self.pos], "soc": float(self.soc),
                "e_avail_wh": float(self.e_avail_wh), "t_ready_s": float(self.t_ready_s), "state": self.state, "airborne": bool(self.airborne),
                "holds": list(self.holds), "sensors": list(self.sensors)}

    @staticmethod
    def from_payload(d: dict) -> VehicleView:
        return VehicleView(str(d["id"]), str(d["model_id"]), d.get("nest_id"), tuple(float(x) for x in d["pos"]), float(d.get("soc", 1.0)),  # type: ignore[arg-type]
                           float(d.get("e_avail_wh", 0.0)), float(d.get("t_ready_s", 0.0)), str(d.get("state", "ready")),
                           bool(d.get("airborne", False)), tuple(d.get("holds") or ()), tuple(d.get("sensors") or ()))


@dataclass(frozen=True)
class NestView:
    id: str
    pos: tuple[float, float, float]
    kind: str = "box"
    crewed: bool = False
    launch_interval_s: float = 30.0
    turnaround_s: float = 60.0
    swap_s: float = 180.0
    link_range_m: float | None = None
    wind_max_mps: float = 12.0
    rtk_base: bool = False

    def to_payload(self) -> dict:
        return {"id": self.id, "pos": [float(x) for x in self.pos], "kind": self.kind, "crewed": bool(self.crewed),
                "launch_interval_s": float(self.launch_interval_s), "turnaround_s": float(self.turnaround_s), "swap_s": float(self.swap_s),
                "link_range_m": self.link_range_m, "wind_max_mps": float(self.wind_max_mps), "rtk_base": bool(self.rtk_base)}

    @staticmethod
    def from_payload(d: dict) -> NestView:
        return NestView(str(d["id"]), tuple(float(x) for x in d["pos"]), str(d.get("kind", "box")), bool(d.get("crewed", False)),  # type: ignore[arg-type]
                        float(d.get("launch_interval_s", 30.0)), float(d.get("turnaround_s", 60.0)), float(d.get("swap_s", 180.0)),
                        None if d.get("link_range_m") is None else float(d["link_range_m"]), float(d.get("wind_max_mps", 12.0)),
                        bool(d.get("rtk_base", False)))


@dataclass(frozen=True)
class Keepout:
    """2.5D 禁入圆（M20 §6.4 第 3 条）：识别物禁入体的水平投影（半径已含外扩 m 与运动外扩），全高度禁入，只能水平绕行。"""

    id: str
    x: float
    y: float
    r: float
    z_min: float = -1e9
    z_max: float = 1e9
    target: str | None = None
    moving: bool = False

    def to_payload(self) -> dict:
        return {"id": self.id, "x": float(self.x), "y": float(self.y), "r": float(self.r), "z_min": float(self.z_min), "z_max": float(self.z_max),
                "target": self.target, "moving": bool(self.moving)}

    @staticmethod
    def from_payload(d: dict) -> Keepout:
        return Keepout(str(d["id"]), float(d["x"]), float(d["y"]), float(d["r"]), float(d.get("z_min", -1e9)), float(d.get("z_max", 1e9)),
                       d.get("target"), bool(d.get("moving", False)))

    def grown(self, extra_m: float) -> Keepout:
        return replace(self, r=self.r + float(extra_m))


# ------------------------------------------------------------------------------------------------ 工作项
@dataclass
class WorkItem:
    """分配的最小单位（M20 §6.2.3）。几何：站位 `pos`（含高度）；航段 `path`（N×3）与 `closed`（循环）、`dwell`（航点序号 → 停留秒）。

    `needs`：`caps` 必需能力、`role`（hover、loiter、cruise）、`level`、`gsd_m`、`fw_ok`（固定翼可执行）、`mr_ok`（多旋翼可执行）。"""

    wid: str
    tid: str
    kind: str
    priority: int = 5
    persistent: bool = False
    workload_s: float = 0.0
    deadline_s: float | None = None
    divisible: bool = False
    min_grain_s: float = 60.0
    group: str | None = None            # 同组工作项必须分给不同机体（同一扫描或巡查任务的条带组、子区）
    pos: tuple[float, float, float] | None = None
    path: np.ndarray | None = None
    closed: bool = False
    dwell: dict[int, float] = field(default_factory=dict)
    needs: dict = field(default_factory=dict)
    allowed_nests: tuple[str, ...] = ()
    allowed_models: tuple[str, ...] = ()
    multi_sortie: bool = False
    sortie_order: int = 0
    ref: str = ""
    loiter_radius_m: float | None = None
    look_at: tuple[float, float, float] | None = None
    target: str | None = None

    @property
    def start(self) -> np.ndarray:
        if self.path is not None and len(self.path):
            return np.asarray(self.path[0], float)
        return np.asarray(self.pos if self.pos is not None else (0.0, 0.0, 0.0), float)

    @property
    def end(self) -> np.ndarray:
        if self.path is not None and len(self.path):
            return np.asarray(self.path[0 if self.closed else -1], float)
        return self.start

    @property
    def length_m(self) -> float:
        if self.path is None or len(self.path) < 2:
            return 0.0
        P = np.asarray(self.path, float)
        L = float(np.linalg.norm(np.diff(P[:, :2], axis=0), axis=1).sum())
        if self.closed:
            L += float(np.linalg.norm(P[0, :2] - P[-1, :2]))
        return L

    @property
    def role(self) -> str:
        return str(self.needs.get("role") or ("cruise" if self.path is not None else "hover"))

    def to_payload(self) -> dict:
        return {"wid": self.wid, "tid": self.tid, "kind": self.kind, "priority": int(self.priority), "persistent": bool(self.persistent),
                "workload_s": float(self.workload_s), "deadline_s": self.deadline_s, "divisible": bool(self.divisible),
                "min_grain_s": float(self.min_grain_s), "group": self.group,
                "pos": None if self.pos is None else [float(x) for x in self.pos],
                "path": None if self.path is None else np.asarray(self.path, np.float64), "closed": bool(self.closed),
                "dwell": [[int(k), float(v)] for k, v in sorted(self.dwell.items())], "needs": dict(self.needs),
                "allowed_nests": list(self.allowed_nests), "allowed_models": list(self.allowed_models), "multi_sortie": bool(self.multi_sortie),
                "sortie_order": int(self.sortie_order), "ref": self.ref, "loiter_radius_m": self.loiter_radius_m,
                "look_at": None if self.look_at is None else [float(x) for x in self.look_at], "target": self.target}

    @staticmethod
    def from_payload(d: dict) -> WorkItem:
        path = d.get("path")
        return WorkItem(str(d["wid"]), str(d["tid"]), str(d["kind"]), int(d.get("priority", 5)), bool(d.get("persistent", False)),
                        float(d.get("workload_s", 0.0)), None if d.get("deadline_s") is None else float(d["deadline_s"]),
                        bool(d.get("divisible", False)), float(d.get("min_grain_s", 60.0)), d.get("group"),
                        None if d.get("pos") is None else tuple(float(x) for x in d["pos"]),  # type: ignore[arg-type]
                        None if path is None else np.asarray(path, np.float64).reshape(-1, 3), bool(d.get("closed", False)),
                        {int(k): float(v) for k, v in d.get("dwell") or []}, dict(d.get("needs") or {}),
                        tuple(d.get("allowed_nests") or ()), tuple(d.get("allowed_models") or ()), bool(d.get("multi_sortie", False)),
                        int(d.get("sortie_order", 0)), str(d.get("ref") or ""), d.get("loiter_radius_m"),
                        None if d.get("look_at") is None else tuple(float(x) for x in d["look_at"]), d.get("target"))  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ 快照与计划
@dataclass
class Snapshot:
    revision: int
    t_s: float
    vehicles: list[VehicleView]
    models: dict[str, ModelInfo]
    nests: dict[str, NestView]
    items: list[WorkItem]
    keepouts: list[Keepout] = field(default_factory=list)
    wind_mps: float = 0.0
    h_transit_agl_m: float = 60.0
    reserve: float = 0.2
    wind_hi_mps: float | None = None      # 机巢上方作业高度（120 m AGL）的平均风，固定翼规划转弯半径用；None 时取 wind_mps

    def to_payload(self) -> dict:
        d = {"revision": int(self.revision), "t_s": float(self.t_s), "vehicles": [v.to_payload() for v in self.vehicles],
             "models": {k: m.to_payload() for k, m in sorted(self.models.items())},
             "nests": {k: n.to_payload() for k, n in sorted(self.nests.items())}, "items": [w.to_payload() for w in self.items],
             "keepouts": [k.to_payload() for k in self.keepouts], "wind_mps": float(self.wind_mps),
             "h_transit_agl_m": float(self.h_transit_agl_m), "reserve": float(self.reserve)}
        if self.wind_hi_mps is not None:
            d["wind_hi_mps"] = float(self.wind_hi_mps)
        return d

    @staticmethod
    def from_payload(d: dict) -> Snapshot:
        return Snapshot(int(d.get("revision", 0)), float(d.get("t_s", 0.0)), [VehicleView.from_payload(v) for v in d.get("vehicles") or []],
                        {k: ModelInfo.from_payload(m) for k, m in (d.get("models") or {}).items()},
                        {k: NestView.from_payload(n) for k, n in (d.get("nests") or {}).items()},
                        [WorkItem.from_payload(w) for w in d.get("items") or []], [Keepout.from_payload(k) for k in d.get("keepouts") or []],
                        float(d.get("wind_mps", 0.0)), float(d.get("h_transit_agl_m", 60.0)), float(d.get("reserve", 0.2)),
                        None if d.get("wind_hi_mps") is None else float(d["wind_hi_mps"]))

    def vehicle(self, vid: str) -> VehicleView | None:
        return next((v for v in self.vehicles if v.id == vid), None)

    def item(self, wid: str) -> WorkItem | None:
        return next((w for w in self.items if w.wid == wid), None)


@dataclass
class Assignment:
    wid: str
    uav: str
    nest: str | None
    t_launch_s: float
    eta_s: float
    t_done_s: float
    energy_wh: float
    margin: float
    cost: float
    terms: dict = field(default_factory=dict)
    order: int = 0
    reversed_: bool = False

    def to_payload(self) -> dict:
        return rnd({"wid": self.wid, "uav": self.uav, "nest": self.nest, "t_launch_s": self.t_launch_s, "eta_s": self.eta_s,
                    "t_done_s": self.t_done_s, "energy_wh": self.energy_wh, "margin": self.margin, "cost": self.cost, "terms": self.terms,
                    "order": int(self.order), "reversed": bool(self.reversed_)})

    @staticmethod
    def from_payload(d: dict) -> Assignment:
        return Assignment(str(d["wid"]), str(d["uav"]), d.get("nest"), float(d.get("t_launch_s") or 0.0), float(d.get("eta_s") or 0.0),
                          float(d.get("t_done_s") or 0.0), float(d.get("energy_wh") or 0.0), float(d.get("margin") or 0.0),
                          float(d.get("cost") or 0.0), dict(d.get("terms") or {}), int(d.get("order", 0)), bool(d.get("reversed", False)))


@dataclass
class Plan:
    plan_id: str
    allocator: str
    revision: int
    assignments: list[Assignment] = field(default_factory=list)
    infeasible: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {"plan_id": self.plan_id, "allocator": self.allocator, "revision": int(self.revision),
                "assignments": [a.to_payload() for a in sorted(self.assignments, key=lambda a: a.wid)],
                "infeasible": sorted(self.infeasible, key=lambda x: str(x.get("wid"))), "warnings": list(self.warnings),
                "stats": rnd(dict(self.stats))}

    @staticmethod
    def from_payload(d: dict) -> Plan:
        return Plan(str(d.get("plan_id", "")), str(d.get("allocator", "greedy")), int(d.get("revision", 0)),
                    [Assignment.from_payload(a) for a in d.get("assignments") or []], [dict(x) for x in d.get("infeasible") or []],
                    [str(w) for w in d.get("warnings") or []], dict(d.get("stats") or {}))

    def digest(self) -> str:
        p = self.to_payload()
        p.pop("stats", None)
        return canonical_sha256(p)

    def of(self, wid: str) -> Assignment | None:
        return next((a for a in self.assignments if a.wid == wid), None)
