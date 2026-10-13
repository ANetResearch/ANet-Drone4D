"""池参数：由 `sandbox:` 段（`awr.runtime.config.SandboxCfg`，M11 拥有文件与校验，键与缺省值按 M17 PRD §6.7）与环境推出
（M17-FR-019、FR-061；AWR-04 §4.4.1、§4.4.3）。

- `quota_profile: local`（本机与局域网）：不做地址前缀配额与工作量证明，批处理可用；`public`：§6.7 全部生效；
- N_max：`max_sessions` 为整数时取之；`auto` 时按 AWR-04 §4.4.1 公式，Q 取单元根 `cpu.max`（读不到时 `cpu_quota_cores`），
  cgroup 不可委派时减 1（M17-FR-019）；运维 `ctl limits --max-sessions` 的覆盖值优先（落盘，重启保留）；
- `limits_obj()`：线上 `awr.sandbox.limits.v1` 完整对象（会话对象、serverInfo 与 `session.json` 共用）。

D2.1（AWR-04 §15.6.2、§15.7.2；M17 PRD §14.4）：`ParkSettings`（`sandbox.park`）、`InternalSettings`（`sandbox.internal`）与
`PowerSettings`（顶层 `power:`）三组参数。它们的键属于 M11 的 `configs/runtime.yaml` 与 `awr.runtime.config`（WP-03b）；本模块
按"`SandboxCfg` 上已有该段则取之，否则读生效配置文件中的原始段，再没有取缺省值"的顺序解析，缺省值与 AWR-04 一致，因此在 M11
登记这些键之前与之后行为相同。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from awr.runtime import cgroup as cg
from awr.runtime.config import ROOT, SandboxCfg

__all__ = ["PLAN_LIMITS", "InternalSettings", "ParkSettings", "PoolSettings", "PowerSettings", "limits_obj",
           "power_from_env", "raw_section", "read_unit_quota"]


def _dump(obj: Any) -> dict[str, Any]:
    """pydantic 对象或 dict → dict（None 与其他类型为空）。"""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return dict(obj) if isinstance(obj, dict) else {}


def _from_obj(cls: type, obj: Any) -> Any:
    """dict 或 pydantic 对象 → dataclass（未知键忽略，类型按缺省值转换）；obj 为空时取缺省。"""
    if obj is None:
        return cls()
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    if not isinstance(obj, dict):
        return cls()
    kw: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in obj or obj[f.name] is None:
            continue
        v = obj[f.name]
        try:
            kw[f.name] = int(v) if isinstance(f.default, int) and not isinstance(f.default, bool) else float(v)
        except (TypeError, ValueError):
            continue
    return cls(**kw)


@dataclass
class ParkSettings:
    """`sandbox.park`（AWR-04 §15.6.2、§15.7.2；M17 PRD §14.4）。"""

    idle_disconnected_s: float = 120.0       # 无连接且无调用 → PARKING（替代 D2 的 idle.ttl_s 结束）
    idle_disconnected_busy_s: float = 60.0   # 队列非空时
    retention_s: float = 3600.0              # PARKED 保留，到期 parked_expired
    per_prefix: int = 2                      # 每地址前缀驻留会话上限
    total_max: int = 64                      # 全站驻留上限
    disk_mb: float = 512.0                   # 驻留目录合计
    session_mb: float = 8.0                  # 每会话驻留目录上限（超出时丢弃 checkpoint，退回配置快照）
    checkpoint_timeout_s: float = 5.0        # checkpoint 落盘超时

    @classmethod
    def from_obj(cls, obj: Any) -> ParkSettings:
        return _from_obj(cls, obj)


@dataclass
class InternalSettings:
    """`sandbox.internal`（AWR-04 §15.9.3；ADR-120、ADR-123）。"""

    reserved_slots: int = 1                  # 不计入 N_max 的内部预留槽位
    lifetime_max_s: float = 28800.0          # 内部档寿命上限 8 h
    max_per_identity: int = 2                # 每个身份同时持有的会话数

    @classmethod
    def from_obj(cls, obj: Any) -> InternalSettings:
        return _from_obj(cls, obj)


@dataclass
class PowerSettings:
    """顶层 `power:`（AWR-04 §15.7.2）；前端以容量接口与 `serverInfo.power` 下发的值为准。"""

    viewer_idle_s: float = 300.0
    viewer_idle_busy_s: float = 120.0
    sandbox_idle_s: float = 600.0
    sandbox_idle_busy_s: float = 300.0
    hidden_s: float = 60.0
    warn_s: float = 30.0
    quiet_s: float = 60.0
    grace_s: float = 90.0
    presence_min_s: float = 15.0
    internal_idle_s: float = 3600.0

    @classmethod
    def from_obj(cls, obj: Any) -> PowerSettings:
        return _from_obj(cls, obj)

    def public_view(self) -> dict[str, float]:
        """容量接口 `power` 字段（`{viewer_idle_s, sandbox_idle_s, hidden_s, warn_s}`，17 §17.20.2）。"""
        return {"viewer_idle_s": self.viewer_idle_s, "sandbox_idle_s": self.sandbox_idle_s, "hidden_s": self.hidden_s,
                "warn_s": self.warn_s}


def raw_section(name: str, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """生效配置中的顶层段 `name`（原始 dict）：`AWR_PERSIST_DIR/effective-config.yaml`（supervisor 写出，含 profile 与覆盖），
    其次 `AWR_CONFIG` 指向的文件（只取顶层，不叠加 profile）；都没有返回 None。环境变量 `AWR_<NAME>_JSON` 最后覆盖（测试与
    运维临时调整）。"""
    import yaml

    e = os.environ if env is None else env
    out: dict[str, Any] | None = None
    for p in ([Path(e["AWR_PERSIST_DIR"]) / "effective-config.yaml"] if e.get("AWR_PERSIST_DIR") else []) + \
             ([Path(e["AWR_CONFIG"])] if e.get("AWR_CONFIG") else []):
        with contextlib.suppress(OSError, ValueError, yaml.YAMLError):
            d = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get(name), dict):
                out = dict(d[name])
                break
    js = e.get(f"AWR_{name.upper()}_JSON")
    if js:
        with contextlib.suppress(ValueError):
            d = json.loads(js)
            if isinstance(d, dict):
                out = {**(out or {}), **d}
    return out


def power_from_env(env: dict[str, str] | None = None) -> PowerSettings:
    return PowerSettings.from_obj(raw_section("power", env))

PLAN_LIMITS = {"area_max_m2": 1_000_000, "vertices_max": 64, "strips_max": 200, "work_items_max": 50,
               "station_candidates_max": 2400, "coverage_cells_max": 250_000}  # 17 §17.3.6 规划输入规模
SPAWN_EST_S = 6.0          # ETA 中的拉起时长（17 §17.4.3）
API_SHARE_FIXED = 0.05     # B_sim 公式的常数项（AWR-04 §4.4.5 第 2 条）


def limits_obj(sb: SandboxCfg) -> dict[str, Any]:
    """`awr.sandbox.limits.v1`（17 §17.4.1）。"""
    lim, r = sb.limits, sb.rest
    return {"vehicles_max": lim.vehicles, "targets_max": lim.targets, "nests_max": lim.nests,
            "tasks_active_max": lim.active_tasks, "cloned_models_max": lim.cloned_models, "rest_rps": float(r.rps),
            "rest_burst": int(r.burst), "call_rps": float(r.call_rps), "call_burst": int(r.call_burst),
            "clock_rps": float(r.clock_rps), "entity_rps": float(r.entity_rps), "task_rps": float(r.task_rps),
            "ws_max": int(sb.ws.per_session), "setpoint_hz_max": float(r.setpoint_hz),
            "snapshot_kib": float(lim.snapshot_kib), "plan": dict(PLAN_LIMITS)}


def read_unit_quota(env: dict[str, str] | None = None) -> float | None:
    """单元根 `cpu.max`（`"<quota> <period>"`）换算的核数；`max` 或读不到时 None。"""
    root = cg.unit_root(env)
    if root is None:
        return None
    try:
        q, _, period = (root / "cpu.max").read_text(encoding="ascii").strip().partition(" ")
    except OSError:
        return None
    if q == "max":
        return None
    try:
        return int(q) / max(1, int(period or cg.CPU_PERIOD_US))
    except ValueError:
        return None


def _sb_delegated(env: dict[str, str] | None = None) -> bool:
    """沙盒叶子组的父组 `sandbox/sb` 存在且可写（`cgroup-exec.sh` 已建立布局并委派）。"""
    p = cg.leaf_path("sandbox/sb", env)
    return p is not None and p.is_dir() and os.access(p, os.W_OK)


@dataclass
class PoolSettings:
    sb: SandboxCfg = field(default_factory=SandboxCfg)
    state_dir: Path = ROOT / "var" / "sandbox"
    worlds_dir: Path = ROOT / "worlds"
    shm_root: Path = Path("/dev/shm/awr")
    access_mode: str = "loopback"
    allow_derive: bool = True
    q_cores: float | None = None        # None：读单元根，再读不到取 sb.cpu_quota_cores
    delegated: bool | None = None       # None：探测 sandbox/sb
    worlds_allow: list[str] | None = None
    plugins: tuple[str, ...] = ()
    simcore_argv: tuple[str, ...] = ()
    profile: str = "dev"
    rendezvous: str | None = None
    lease_ms: int = 10_000
    max_sessions_override: int | None = None
    show_ring: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    park: ParkSettings = field(default_factory=ParkSettings)
    internal: InternalSettings = field(default_factory=InternalSettings)
    power: PowerSettings = field(default_factory=PowerSettings)
    identity_dir: Path | None = None    # 身份库状态目录（撤销表）；None 为 `var/identity`

    def __post_init__(self) -> None:
        # `sandbox:` 段已有 park、internal（M11 登记后）时以之为准；构造时显式给出的对象（测试）不被覆盖
        for name, cls in (("park", ParkSettings), ("internal", InternalSettings)):
            sec = getattr(self.sb, name, None)
            if sec is not None and getattr(self, name) == cls():
                setattr(self, name, cls.from_obj(sec))

    @property
    def public(self) -> bool:
        return self.sb.quota_profile == "public"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def snapshots_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @property
    def parked_dir(self) -> Path:
        """驻留目录根 `var/sandbox/parked/`（M17 PRD §14.2）。"""
        return self.state_dir / "parked"

    def quota_cores(self) -> float:
        if self.q_cores is not None:
            return float(self.q_cores)
        q = read_unit_quota(self.env or None)
        return float(q) if q is not None else float(self.sb.cpu_quota_cores)

    def is_delegated(self) -> bool:
        if self.delegated is not None:
            return bool(self.delegated)
        return _sb_delegated(self.env or None)

    def n_max(self) -> int:
        if self.max_sessions_override is not None:
            return max(0, min(self.sb.max_sessions_hard, int(self.max_sessions_override)))
        return self.sb.n_max(self.quota_cores(), delegated=self.is_delegated())

    def cpu_max_sandbox(self) -> float:
        """`sandbox/cpu.max = 0.9·Q − C_core`（AWR-04 §4.4.4）。"""
        return max(0.0, 0.9 * self.quota_cores() - self.sb.costs.c_core)

    def b_sim(self, n_live: int, ws: list[int] | None = None) -> float:
        """治理器可分配的核数 `B_sim = cpu.max(sandbox) − C_ctl − 0.08·N − 0.05`（AWR-04 §4.4.5 第 2 条）。"""
        c = self.sb.costs
        return max(0.0, self.cpu_max_sandbox() - c.c_ctl - c.api_per_session * max(0, n_live) - API_SHARE_FIXED)

    def per_prefix_cap(self, n_max: int) -> int:
        a = self.sb.prefix.active
        return int(a) if isinstance(a, int) else min(2, math.ceil(n_max / 2))

    def per_agg_cap(self, n_max: int) -> int:
        a = self.sb.prefix.agg_active
        return int(a) if isinstance(a, int) else math.ceil(n_max / 2)

    def limits_obj(self) -> dict[str, Any]:
        return limits_obj(self.sb)

    def world_allowed(self, world: str) -> bool:
        return self.worlds_allow is None or world in self.worlds_allow

    @classmethod
    def from_env(cls, sb: SandboxCfg, env: dict[str, str] | None = None) -> PoolSettings:
        """池进程的设置：状态目录、tmpfs 根、世界目录、白名单与汇合点取 supervisor 注入的环境变量（`procutil.sandbox_env`）。"""
        e = dict(os.environ if env is None else env)
        sd = Path(e.get("AWR_SANDBOX_STATE_DIR") or sb.state_path(ROOT))
        allow = [w.strip() for w in (e.get("AWR_SANDBOX_WORLDS_ALLOW") or "").split(",") if w.strip()]
        if not allow:
            allow = list(sb.worlds_allow) or [w.strip() for w in (e.get("AWR_WORLDS_ALLOW") or "").split(",") if w.strip()]
        plugins = tuple(p.strip() for p in (e.get("AWR_SANDBOX_PLUGINS") or "").split(",") if p.strip())
        show_ring = Path(e["AWR_RUN_DIR"]) / "state.sim-core" if e.get("AWR_RUN_DIR") else None
        sb_raw = raw_section("sandbox", e) or {}
        # `sandbox:` 段的 park、internal（M11 已登记，D2-INT）为底，原始段（生效配置与 AWR_SANDBOX_JSON 覆盖）逐键覆盖
        park = ParkSettings.from_obj({**_dump(getattr(sb, "park", None)), **_dump(sb_raw.get("park"))})
        internal = InternalSettings.from_obj({**_dump(getattr(sb, "internal", None)), **_dump(sb_raw.get("internal"))})
        return cls(sb=sb, state_dir=sd, worlds_dir=Path(e.get("AWR_WORLDS_DIR") or ROOT / "worlds"),
                   shm_root=Path(e.get("AWR_SHM_ROOT") or "/dev/shm/awr"), access_mode=e.get("AWR_ACCESS_MODE") or "loopback",
                   allow_derive=e.get("AWR_GEO_ALLOW_DERIVE", "1" if sb.quota_profile != "public" else "0") != "0",
                   worlds_allow=allow or None, plugins=plugins, profile=e.get("AWR_PROFILE") or "dev",
                   rendezvous=e.get("AWR_RENDEZVOUS"), lease_ms=int(e.get("AWR_BUS_LEASE_MS") or 10_000),
                   show_ring=show_ring, env=e, park=park, internal=internal, power=power_from_env(e),
                   identity_dir=None)          # None：identity.default_state_dir(env)（AWR_IDENTITY_STATE_DIR、identity.state_dir）
