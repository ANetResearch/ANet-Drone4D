"""Env 运行上下文（M26 §6.2 RunContext 的 P0a 子集）与 `bench/run_spec.v1` 的映射。

`run_seed` 只存在于 Env 进程内存与运行真值层（排名运行由 M27 以 CSPRNG 生成），不进入观测、简报与 Agent 可见的日志。
决策时延（`policy.params.decision`，M26 §9.4 的 run_spec 变更请求落地前）缺省 `fixed`、τ = 0（下一 tick 生效）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from awr.agentsdk.env.link import CommCfg

__all__ = ["DecisionCfg", "EnvSpec", "FleetEntry"]


@dataclass(frozen=True)
class FleetEntry:
    uav: str
    pos_enu_m: tuple[float, float, float]
    yaw_rad: float = 0.0
    vehicle_ref: str = "x500"
    airborne: bool = False


@dataclass(frozen=True)
class DecisionCfg:
    mode: str = "fixed"
    tau_ms: int = 0
    tau_min_ms: int = 0
    step_budget_s: float = 2.0
    wall_ratio: float = 1.0

    def lat_ticks(self, tick_ms: int = 100) -> int:
        return max(1, math.ceil(self.tau_ms / tick_ms))

    def config(self) -> dict:
        return {"mode": self.mode, "tau_ms": self.tau_ms, "tau_min_ms": self.tau_min_ms, "step_budget_s": self.step_budget_s,
                "wall_ratio": self.wall_ratio}


@dataclass(frozen=True)
class EnvSpec:
    run_id: str
    seed: int
    world_dir: Path
    event_dir: Path
    fleet: tuple[FleetEntry, ...]
    obs_level: str = "L1"
    noise: str = "realistic"
    physics: str = "realistic"                   # realistic（gz 锁步）| kinematic（开发档，不排名）
    duration_s: float | None = None              # 缺省为事件时长
    decision: DecisionCfg = field(default_factory=DecisionCfg)
    comm: CommCfg = field(default_factory=CommCfg)
    ranked: bool = False
    policy: dict = field(default_factory=lambda: {"kind": "sdk"})
    out_dir: Path | None = None                  # 运行目录；None 不写盘
    gz: dict = field(default_factory=dict)       # GzInstance 选项（launcher、image、run_dir、export_root）
    l2: dict = field(default_factory=dict)       # L2 传感器开关（lidar、imu、gnss；env.l2.L2Config），取自 fleet[].sensors[].enabled

    @classmethod
    def from_run_spec(cls, rs: dict, *, run_id: str, seed: int, worlds_root: Path, events_root: Path, out_dir: Path | None = None,
                      physics: str | None = None, gz: dict | None = None) -> EnvSpec:
        fleet = []
        for f in rs["fleet"]:
            st = f["start"]
            if st["kind"] != "pose":
                raise ValueError(f"{f['uav_id']}: start kind {st['kind']!r} is not available in P0a (use pose)")
            fleet.append(FleetEntry(f["uav_id"], tuple(float(x) for x in st["pos_enu_m"]), math.radians(float(st["yaw_deg"])),
                                    f.get("vehicle_ref", "x500")))
        dec = (rs.get("policy", {}).get("params") or {}).get("decision") or {}
        c = rs.get("comm", {})
        comm = CommCfg(enabled=c.get("enabled", True), bucket_Bps=c.get("bucket_Bps", 125_000), burst_B=c.get("burst_B", 125_000),
                       msg_max_B=c.get("msg_max_B", 4096), model=c.get("model", "fspl-nlos@1"))
        ev = rs["event"]
        en = {x["sensor_id"]: bool(x.get("enabled", True)) for f in rs["fleet"] for x in f.get("sensors", [])}
        l2 = {k: en[sid] for k, sid in (("lidar", "lidar0"), ("imu", "lidar0_imu"), ("gnss", "gnss0")) if sid in en}
        return cls(run_id=run_id, seed=int(seed), world_dir=Path(worlds_root) / rs["world"]["id"],
                   event_dir=Path(events_root) / ev["id"] / f"v{ev.get('version', 1)}", fleet=tuple(fleet),
                   obs_level=rs["obs_level"], noise=rs.get("noise", "realistic"), physics=physics or rs.get("physics", "realistic"),
                   duration_s=rs.get("duration_s"), decision=DecisionCfg(**dec), comm=comm, ranked=bool(rs.get("ranked", False)),
                   policy=dict(rs.get("policy", {"kind": "sdk"})), out_dir=out_dir, gz=dict(gz or {}), l2=l2)
