"""Env 运行上下文（M26 §6.2 RunContext 的 P0a 子集）与 `bench/run_spec.v1` 的映射。

`run_seed` 只存在于 Env 进程内存与运行真值层（排名运行由 M27 以 CSPRNG 生成），不进入观测、简报与 Agent 可见的日志。
决策时延（`policy.params.decision`，M26 §9.4 的 run_spec 变更请求落地前）缺省 `fixed`、τ = 0（下一 tick 生效）。

运行身份（M27-FR-007、M24-FR-069；D3-AC-17）：
- `config_sha256 = sha256(JCS(规格 − CONFIG_EXCLUDE))`，不含种子，用于同配置多种子聚合；
- `spec_sha256 = sha256(JCS(规格 − SPEC_EXCLUDE，并加入 seed_commit))`；排名运行只用 M27 给出的 HMAC 承诺值，不排名运行缺省以
  `seed` 本身代替（不排名运行的种子本就公开）；
- `run_hash = sha256(spec_sha256 的 32 字节 ‖ JCS(versions))`。

规格先经 `resolve_run_spec` 规范化：`clock_profile` 由档名回填为四元组 `{name, main_hz, physics_hz, l1_hz}`（四元组而非名称进入
哈希，同名不同频率的档位不会混为同一配置），`physics` 取实际生效值。`versions` 为字符串映射：平台镜像摘要（`image.gz`、
`image.sensorsvc`、`image.env`、`image.agent`）与运行环境版本（`env`、`python`、`numpy`）；任一镜像摘要变化即改变运行哈希。
"""

from __future__ import annotations

import hashlib
import math
import platform
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from awr.agentsdk.env.link import CommCfg

__all__ = ["CONFIG_EXCLUDE", "ENV_VERSION", "SPEC_EXCLUDE", "DecisionCfg", "EnvSpec", "FleetEntry", "config_sha256", "platform_versions",
           "resolve_clock_profile", "resolve_run_spec", "run_hash", "spec_sha256"]

ENV_VERSION = "d3-env@0.3.0"
# M27-FR-007：不影响结果的键（spec 另保留 seed_commit）；seeds、seed_count 在展开为单个 Run 后不再属于该 Run 的规格
CONFIG_EXCLUDE = frozenset({"run_id", "experiment_id", "experiment", "seed", "seed_commit", "seeds", "seed_count", "submitted_by",
                            "created_unix_ns", "retention", "priority"})
SPEC_EXCLUDE = frozenset({"run_id", "experiment_id", "experiment", "seed", "seeds", "seed_count", "submitted_by", "created_unix_ns",
                          "retention", "priority"})
IMAGE_KEYS = ("gz", "sensorsvc", "env", "agent")


def resolve_clock_profile(cp: str | Mapping | None) -> dict:
    """档名或四元组 → `{name, main_hz, physics_hz, l1_hz}`（缺省 d1_250；M27 §6.7.1、M24-FR-009）。"""
    from awr.phys.adapter import CLOCK_PROFILES

    if cp is None:
        cp = "d1_250"
    if isinstance(cp, str):
        if cp not in CLOCK_PROFILES:
            raise ValueError(f"clock_profile {cp!r} is not a known profile")
        p = CLOCK_PROFILES[cp]
        return {"name": p.name, "main_hz": int(p.main_hz), "physics_hz": int(p.physics_hz), "l1_hz": int(p.l1_hz)}
    missing = [k for k in ("name", "main_hz", "physics_hz", "l1_hz") if k not in cp]
    if missing:
        raise ValueError(f"clock_profile is missing {missing}")
    return {"name": str(cp["name"]), "main_hz": int(cp["main_hz"]), "physics_hz": int(cp["physics_hz"]), "l1_hz": int(cp["l1_hz"])}


def resolve_run_spec(rs: Mapping, *, physics: str | None = None) -> dict:
    """进入哈希的规范规格：深拷贝，`clock_profile` 回填为四元组，`physics` 取实际生效值。"""
    import copy

    out = copy.deepcopy(dict(rs))
    out["clock_profile"] = resolve_clock_profile(out.get("clock_profile"))
    out["physics"] = physics or out.get("physics", "realistic")
    return out


def _jcs(x) -> bytes:
    from awr.sim.events.jcs import jcs

    return jcs(x)


def _drop(d: Mapping, keys) -> dict:
    return {k: v for k, v in d.items() if k not in keys}


def config_sha256(resolved: Mapping) -> str:
    return hashlib.sha256(_jcs(_drop(resolved, CONFIG_EXCLUDE))).hexdigest()


def spec_sha256(resolved: Mapping, seed_commit: str) -> str:
    return hashlib.sha256(_jcs({**_drop(resolved, SPEC_EXCLUDE), "seed_commit": str(seed_commit)})).hexdigest()


def run_hash(spec_sha256_hex: str, versions: Mapping[str, str]) -> str:
    """M27-FR-007：`sha256(spec_sha256 的 32 字节 ‖ JCS(versions))`。"""
    if any(not isinstance(v, str) for v in versions.values()):
        raise TypeError("versions values must be strings (run_manifest.v1)")
    return hashlib.sha256(bytes.fromhex(spec_sha256_hex) + _jcs(dict(versions))).hexdigest()


def platform_versions(images: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """运行清单 `versions`：`image.<gz|sensorsvc|env|agent>` 为平台镜像摘要（缺省不填），另含 Env、Python 与 numpy 版本。"""
    import numpy as np

    v = {"env": ENV_VERSION, "python": platform.python_version(), "numpy": np.__version__}
    for k, d in (images or {}).items():
        if k not in IMAGE_KEYS:
            raise ValueError(f"image key {k!r} is not one of {IMAGE_KEYS}")
        if d:
            v[f"image.{k}"] = str(d)
    v.update({str(k): str(x) for k, x in (extra or {}).items()})
    return v


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
    clock_profile: str | dict = "d1_250"         # 档名或四元组；以四元组进入运行哈希
    run_spec: dict | None = None                 # 规范规格（resolve_run_spec）；None 时由本对象字段合成
    seed_commit: str | None = None               # 排名运行的种子承诺值（M27 生成）；不排名运行缺省以 seed 代替
    versions: dict = field(default_factory=platform_versions)

    # ------------------------------------------------------------------ 运行身份（M27-FR-007）
    def resolved(self) -> dict:
        """进入哈希的规范规格：有 `run_spec` 时取之（physics 以本对象为准），否则由字段合成同构字典。"""
        if self.run_spec is not None:
            return resolve_run_spec(self.run_spec, physics=self.physics)
        return resolve_run_spec({
            "world": {"id": Path(self.world_dir).name}, "event": {"id": Path(self.event_dir).parent.name, "dir": Path(self.event_dir).name},
            "fleet": [{"uav_id": f.uav, "vehicle_ref": f.vehicle_ref, "start": {"kind": "pose", "pos_enu_m": list(f.pos_enu_m),
                                                                                  "yaw_rad": f.yaw_rad, "airborne": f.airborne}}
                      for f in self.fleet],
            "obs_level": self.obs_level, "noise": self.noise, "duration_s": self.duration_s, "ranked": self.ranked,
            "policy": {**self.policy, "params": {**(self.policy.get("params") or {}), "decision": self.decision.config()}},
            "comm": {"enabled": self.comm.enabled, "bucket_Bps": self.comm.bucket_Bps, "burst_B": self.comm.burst_B,
                     "msg_max_B": self.comm.msg_max_B, "model": self.comm.model},
            "sensors_enabled": dict(sorted(self.l2.items())), "clock_profile": self.clock_profile}, physics=self.physics)

    def identity(self) -> dict:
        """→ {config_sha256, spec_sha256, run_hash, versions, clock_profile}（运行清单字段；不含种子明文）。"""
        if self.ranked and not self.seed_commit:
            raise ValueError("a ranked run needs the seed_commit issued by M27; the seed itself must not enter the manifest")
        r = self.resolved()
        sh = spec_sha256(r, self.seed_commit or f"seed:{self.seed}")
        return {"config_sha256": config_sha256(r), "spec_sha256": sh, "run_hash": run_hash(sh, self.versions),
                "versions": dict(self.versions), "clock_profile": r["clock_profile"]}

    def run_hash(self) -> str:
        return self.identity()["run_hash"]

    @classmethod
    def from_run_spec(cls, rs: dict, *, run_id: str, seed: int, worlds_root: Path, events_root: Path, out_dir: Path | None = None,
                      physics: str | None = None, gz: dict | None = None, images: Mapping[str, str] | None = None,
                      seed_commit: str | None = None) -> EnvSpec:
        """`images`：平台镜像摘要（`gz`、`sensorsvc`、`env`、`agent`，进入 `versions`）；缺省 `gz` 取 `gz["image"]`，`agent` 取
        `policy.image_digest`。"""
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
        img = {"gz": (gz or {}).get("image"), "agent": (rs.get("policy") or {}).get("image_digest"), **(images or {})}
        return cls(run_id=run_id, seed=int(seed), world_dir=Path(worlds_root) / rs["world"]["id"],
                   event_dir=Path(events_root) / ev["id"] / f"v{ev.get('version', 1)}", fleet=tuple(fleet),
                   obs_level=rs["obs_level"], noise=rs.get("noise", "realistic"), physics=physics or rs.get("physics", "realistic"),
                   duration_s=rs.get("duration_s"), decision=DecisionCfg(**dec), comm=comm, ranked=bool(rs.get("ranked", False)),
                   policy=dict(rs.get("policy", {"kind": "sdk"})), out_dir=out_dir, gz=dict(gz or {}), l2=l2,
                   clock_profile=resolve_clock_profile(rs.get("clock_profile")), run_spec=resolve_run_spec(rs), seed_commit=seed_commit,
                   versions=platform_versions(img))
