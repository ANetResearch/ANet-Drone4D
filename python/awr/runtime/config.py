"""`configs/runtime.yaml` 加载、profile 叠加与结构校验（AWR-19 §6.2–§6.4；M11-FR-015；OPS-FR-001、003、004）。

优先级：命令行（`--set key=value`）> `AWR_*` 环境变量 > `profiles.<profile>` > 基础值 > 内置默认。
未知键、类型错误、lan 或 public 模式缺 `origins`、public 模式绑定非回环地址、profile = field、D1 中 `real_ops.enabled: true`
（或 `AWR_REAL_OPS=1`）一律抛 ConfigError（退出码 2），消息给出键路径。生效配置经 `effective_dict()` 输出，秘密键掩码为 `***`。
"""

from __future__ import annotations

import copy
import ipaddress
import itertools
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .logjson import mask

__all__ = ["DEFAULT_CONFIG_PATH", "ConfigError", "ProcCfg", "RuntimeConfig", "SandboxCfg", "apply_world_fallback",
           "load_runtime_config", "resolve_profile", "sandbox_from_env"]

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = ROOT / "configs" / "runtime.yaml"
D1_PROFILES = ("dev", "demo", "ci", "perf", "public")
LAYERS = ("core", "ext", "dev", "sandbox", "d3")   # d3：D3 dmax 进程，只由 d3 profile 启用（AWR-05 §12.3 扩展点 12）
WORLD_ID_RE = r"[a-z0-9][a-z0-9_-]*"


class ConfigError(Exception):
    """配置结构错误（AWR-19 §16.2 退出码 2 CONFIG_INVALID）。"""

    exit_code = 2

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path
        self.message = message


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RunCfg(_M):
    id: str = "auto"
    world: str = "shenzhen"
    scenario: str = "s1-shenzhen-facade"
    fallback_world: str | None = "synthcity"   # 默认世界未构建且该世界已构建时回退（ADR-077）；null 关闭回退
    scenario_profile: str | None = None
    shm_root: str = "/dev/shm/awr"
    persist_root: str = "runs"
    keep_run_dir: bool = False
    resume_after_crash: Literal["play", "pause"] = "play"

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if v != "auto" and not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", v):
            raise ValueError("run.id 必须为 auto 或 zenoh chunk（[a-z0-9][a-z0-9_-]*）")
        return v

    @field_validator("world", "fallback_world")
    @classmethod
    def _world(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", v):
            raise ValueError("world 必须为小写 id（[a-z0-9][a-z0-9_-]*）")
        return v


class NetCfg(_M):
    bind: str = "127.0.0.1"
    port: int = Field(8000, ge=1024, le=65535)
    port_offset: int = Field(0, ge=0, le=31)  # 0–31（ADR-055；原 0–9 不够 14 个并行 worktree，INT-1 §7.9）
    static_port: int = Field(8001, ge=1024, le=65535)
    access_mode: Literal["auto", "loopback", "lan", "public"] = "auto"
    origins: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)
    # 公开演示站（ADR-082；AWR-17 §3.3、AWR-19 §3.7）：可信反向代理（只用于限流与日志的客户端地址）、世界白名单、WS 连接上限
    trusted_proxies: list[str] = Field(default_factory=list)
    worlds_allow: list[str] = Field(default_factory=list)   # 空表示不限制
    ws_max: int = Field(32, ge=1, le=1024)
    ws_max_per_ip: int | None = Field(None, ge=1, le=64)

    @field_validator("trusted_proxies")
    @classmethod
    def _proxies(cls, v: list[str]) -> list[str]:
        for x in v:
            try:
                ipaddress.ip_network(x, strict=False)
            except ValueError:
                raise ValueError(f"trusted_proxies 只能是 IP 或 CIDR：{x!r}") from None
        return v

    @field_validator("worlds_allow")
    @classmethod
    def _worlds_allow(cls, v: list[str]) -> list[str]:
        for w in v:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", w):
                raise ValueError(f"worlds_allow 必须为小写世界 id：{w!r}")
        return v

    @field_validator("origins")
    @classmethod
    def _origins(cls, v: list[str]) -> list[str]:
        for o in v:
            if not re.fullmatch(r"https?://[^/\s:]+(:\d+)?", o):
                raise ValueError(f"origin 形如 http://<host>:<port>：{o!r}")
        return v


class BusCfg(_M):
    rendezvous: str = "tcp/127.0.0.1:7447"
    shm: bool = False
    lease_ms: int = Field(3000, ge=500, le=60000)

    @field_validator("rendezvous")
    @classmethod
    def _rv(cls, v: str) -> str:
        m = re.fullmatch(r"tcp/(127\.0\.0\.1|\[::1\]):(\d{1,5})", v)
        if not m or not 1024 <= int(m.group(2)) <= 65535:
            raise ValueError("汇合点只能是回环地址 tcp/127.0.0.1:<port>（主机部分不可改）")
        return v

    @field_validator("shm")
    @classmethod
    def _shm(cls, v: bool) -> bool:
        if v:
            raise ValueError("zenoh SHM 在 D1 一律关闭（g05 §0 第 9 条）")
        return v


class QuotaCfg(_M):
    runs_gb: float = Field(20, ge=1)
    check_every_s: float = Field(600, gt=0)
    disk_warn_gb: float = Field(10, ge=0)
    disk_min_gb: float = Field(5, ge=0)


class CpuCfg(_M):
    pin: Literal["auto", "on", "off"] = "auto"

    @field_validator("pin", mode="before")
    @classmethod
    def _yaml_bool(cls, v: Any) -> Any:
        if v is True:
            return "on"
        if v is False:
            return "off"
        return v


class RestartMax(_M):
    count: int = Field(5, ge=0)
    window_s: float = Field(60, gt=0)


class RestartCfg(_M):
    policy: Literal["always", "on-failure", "never"] = "always"
    backoff_s: list[float] = Field(default_factory=lambda: [0.1, 1, 2, 4, 8], min_length=1)  # 首档 0.1 s（ADR-061，19 §4.2）
    max: RestartMax = Field(default_factory=RestartMax)


class RestartOverride(_M):
    policy: Literal["always", "on-failure", "never"] | None = None
    backoff_s: list[float] | None = None
    max: RestartMax | None = None


class StopCfg(_M):
    signal: Literal["SIGTERM", "SIGINT"] = "SIGTERM"
    grace_s: float = Field(5, gt=0)


class LogCfg(_M):
    max_mb: float = Field(10, gt=0)
    backups: int = Field(5, ge=0)
    tail_lines: int = Field(200, ge=1)


class DefaultsCfg(_M):
    env: dict[str, str] = Field(default_factory=lambda: {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                                                         "MKL_NUM_THREADS": "1", "PYTHONUNBUFFERED": "1"})
    standby: bool = True   # 热备用总开关：false 时忽略 procs[].standby（公开演示站的小内存主机，ADR-082）
    restart: RestartCfg = Field(default_factory=RestartCfg)
    stop: StopCfg = Field(default_factory=StopCfg)
    log: LogCfg = Field(default_factory=LogCfg)


class LivenessCfg(_M):
    ring: str | None = None
    heartbeat: str | None = None
    stale_s: float | None = Field(None, gt=0)
    startup_grace_s: float = Field(15, gt=0)
    mode: Literal["ring", "heartbeat", "exit_only"] | None = None

    @model_validator(mode="after")
    def _mode(self) -> LivenessCfg:
        if self.mode is None:
            self.mode = "ring" if self.ring else ("heartbeat" if self.heartbeat else "exit_only")
        if self.mode == "ring" and not self.ring:
            raise ValueError("mode = ring 需要 ring 文件名")
        if self.mode == "heartbeat" and not self.heartbeat:
            raise ValueError("mode = heartbeat 需要 heartbeat 文件名")
        if self.mode != "exit_only" and self.stale_s is None:
            raise ValueError("ring 或 heartbeat 存活判据需要 stale_s")
        return self


class ProcCfg(_M):
    name: str
    layer: Literal["core", "ext", "dev", "sandbox", "d3"] = "core"
    cmd: list[str] = Field(min_length=1)
    cwd: str | None = None
    run_scoped: bool = True
    on_demand: bool = False
    plugins: list[str] = Field(default_factory=list)
    cpus: list[int] | None = None
    nice: int = Field(0, ge=-20, le=19)
    start_after: list[str] = Field(default_factory=list)
    liveness: LivenessCfg = Field(default_factory=LivenessCfg)
    id_range: tuple[int, int] | None = None
    restart: RestartOverride | None = None
    stop: StopCfg | None = None
    env: dict[str, str] = Field(default_factory=dict)
    # 热备用进程（AWR-19 §4.2；ADR-070）：主进程就绪后另起一个完成导入与预热、阻塞等待的同命令进程，重启时直接接替
    standby: bool = False
    standby_cpus: list[int] | None = None   # 热备用进程等待期间的 CPU（接替时改为 cpus）；None 时取 cpus 之外的全部 CPU

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", v):
            raise ValueError("进程名必须为 zenoh chunk（[a-z0-9][a-z0-9_-]*）")
        return v



# ---------------------------------------------------------------- 沙盒段（D2；AWR-04 §4.4.3；M17 PRD §6.7、§8.1；M11-FR-217）
# 键与缺省值以 M17 PRD §6.7 为准（M17 是语义所有者，M11 拥有本文件与校验）；时长为墙钟秒（ADR-045）。
class SbCosts(_M):
    c_core: float = Field(0.89, ge=0)          # 共享展示合计（核）
    c_ctl: float = Field(0.22, ge=0)           # 沙盒控制面合计
    c_s: float = Field(0.23, gt=0)             # 每会话合计
    b_ff_min: float = Field(0.20, ge=0)        # 倍速余量下限
    api_per_session: float = Field(0.08, ge=0)


class SbLimits(_M):
    vehicles: int = Field(12, ge=1, le=64)
    targets: int = Field(30, ge=0, le=32)
    sensitivity_per_target: int = Field(4, ge=0, le=8)
    sensitivity_active: int = Field(64, ge=0, le=256)
    nests: int = Field(4, ge=0, le=16)
    active_tasks: int = Field(6, ge=0, le=32)
    cloned_models: int = Field(8, ge=0, le=64)
    snapshot_kib: int = Field(512, ge=16, le=4096)


class SbLifetime(_M):
    initial_s: float = Field(1800, gt=0)
    renew_step_s: float = Field(1800, gt=0)
    max_s: float = Field(7200, gt=0)


class SbIdle(_M):
    enter_s: float = Field(60, gt=0)
    ttl_s: float = Field(600, gt=0)
    ttl_busy_s: float = Field(120, gt=0)
    ready_claim_s: float = Field(120, gt=0)


class SbQueue(_M):
    max: int = Field(20, ge=0, le=1000)
    per_prefix: int = Field(1, ge=0, le=10)
    offer_claim_s: float = Field(60, gt=0)
    ticket_idle_s: float = Field(120, gt=0)
    long_poll_s: float = Field(25, gt=0, le=60)
    preempt_after_s: float = Field(1800, gt=0)


class SbPrefix(_M):
    v4: int = Field(32, ge=8, le=32)
    v6: int = Field(64, ge=16, le=128)
    agg_v4: int = Field(24, ge=8, le=32)
    agg_v6: int = Field(48, ge=16, le=128)
    active: Literal["auto"] | int = "auto"          # auto = min(2, ⌈N_max/2⌉)
    agg_active: Literal["auto"] | int = "auto"      # auto = ⌈N_max/2⌉
    create_per_h: float = Field(6, gt=0)
    create_burst: float = Field(2, ge=1)
    agg_create_per_h: float = Field(12, gt=0)
    agg_create_burst: float = Field(4, ge=1)


class SbPow(_M):
    bits: int = Field(20, ge=0, le=32)              # 17 §17.3.5 缺省 20，每次额外触发 + 1
    bits_hot: int = Field(24, ge=0, le=32)          # 上限
    ttl_s: float = Field(120, gt=0)
    prefix_creates_1h: int = Field(3, ge=0)
    agg_creates_1h: int = Field(8, ge=0)


class SbSpawn(_M):
    timeout_s: float = Field(30, gt=0)
    concurrency: int = Field(1, ge=1, le=4)
    nice: int = Field(5, ge=0, le=19)
    stale_s: float = Field(2.0, gt=0)
    startup_grace_s: float = Field(30, gt=0)
    max_crashes: int = Field(3, ge=1)
    crash_window_s: float = Field(60, gt=0)


class SbLeafMem(_M):
    S: int = Field(230, ge=64)
    L: int = Field(250, ge=64)


class SbCgroup(_M):
    leaf_mem_high_mb: SbLeafMem = Field(default_factory=SbLeafMem)
    leaf_mem_max_mb: int = Field(320, ge=64)
    plan_cpu_max: float = Field(0.5, gt=0)
    plan_mem_max_mb: int = Field(250, ge=64)
    rss_watchdog_mb: int = Field(320, ge=64)


class SbLadder(_M):
    l2_throttle: float = Field(0.05, ge=0, le=1)
    l2_psi: float = Field(20, ge=0, le=100)
    l2_exit_s: float = Field(30, gt=0)
    l3_rtf: float = Field(0.95, gt=0, le=1)
    l3_hold_s: float = Field(30, gt=0)
    l3_show_rtf: float = Field(0.98, gt=0, le=1)
    l3_show_hold_s: float = Field(10, gt=0)
    l4_anon_mb: float = Field(1000, gt=0)
    l4_psi: float = Field(10, ge=0, le=100)
    l4_hold_s: float = Field(10, gt=0)
    reclaim_interval_s: float = Field(15, gt=0)


class SbBudgetMs(_M):
    scan: float = Field(2000, gt=0)
    station: float = Field(500, gt=0)
    alloc: float = Field(200, gt=0)


class SbFreeze(_M):
    timeouts: int = Field(3, ge=1)
    window_s: float = Field(600, gt=0)
    freeze_s: float = Field(300, gt=0)


class SbPlanning(_M):
    workers: int = Field(1, ge=1, le=4)
    nice: int = Field(10, ge=0, le=19)
    budget_ms: SbBudgetMs = Field(default_factory=SbBudgetMs)
    kill_factor: float = Field(3, ge=1)
    freeze: SbFreeze = Field(default_factory=SbFreeze)
    pending_per_session: int = Field(8, ge=1)
    world_lru: int = Field(2, ge=1, le=7)


class SbWs(_M):
    max: Literal["auto"] | int = "auto"              # auto = 4·N_max（M11-FR-210）
    max_hard: int = Field(24, ge=1, le=256)
    per_prefix: int = Field(4, ge=1, le=64)
    per_session: int = Field(4, ge=1, le=64)


class SbRest(_M):
    rps: float = Field(10, gt=0)
    burst: float = Field(30, ge=1)
    call_rps: float = Field(20, gt=0)
    call_burst: float = Field(40, ge=1)
    clock_rps: float = Field(2, gt=0)
    entity_rps: float = Field(5, gt=0)
    task_rps: float = Field(1, gt=0)
    task_burst: float = Field(2, ge=1)
    setpoint_hz: float = Field(50, gt=0, le=60)


class SbDisk(_M):
    per_session_mb: float = Field(50, gt=0)
    total_mb: float = Field(500, gt=0)
    log_mb: float = Field(20, gt=0)


class SbBatch(_M):
    enabled: bool = False
    concurrency: int = Field(1, ge=1, le=4)


# D2.1（AWR-04 §15.6.2、§15.7.2、§15.9.3；M17 PRD §14.4；M11-FR-230）：驻留与内部档。语义所有者 M17（`awr.sandbox.config`
# 的 ParkSettings、InternalSettings 读取同名键），M11 拥有本文件与校验
class SbPark(_M):
    idle_disconnected_s: float = Field(120, gt=0)        # 无连接且无调用 → PARKING（替代 D2 idle.ttl_s 结束）
    idle_disconnected_busy_s: float = Field(60, gt=0)    # 队列非空时
    retention_s: float = Field(3600, gt=0)               # PARKED 保留，到期 parked_expired
    per_prefix: int = Field(2, ge=0, le=64)
    total_max: int = Field(64, ge=0, le=4096)
    disk_mb: float = Field(512, gt=0)
    session_mb: float = Field(8, gt=0)
    checkpoint_timeout_s: float = Field(5, gt=0)


class SbInternal(_M):
    reserved_slots: int = Field(1, ge=0, le=2)           # 不计入 N_max 的内部预留槽位
    lifetime_max_s: float = Field(28800, gt=0)           # 内部档寿命上限 8 h
    max_per_identity: int = Field(2, ge=1, le=8)


class SandboxCfg(_M):
    enabled: bool = False
    quota_profile: Literal["local", "public"] = "local"
    port: int = Field(8002, ge=1024, le=65535)       # 随 net.port_offset 平移 10·k（public 18641）
    state_dir: str = "var/sandbox"                   # 相对仓库根；token.key、pool.secret、state.json、runs/<sid>/
    world_costs: str = "configs/sandbox/world_costs.json"
    worlds_allow: list[str] = Field(default_factory=list)   # 空表示沿用 net.worlds_allow（再空则不限制）
    max_sessions: Literal["auto"] | int = "auto"
    max_sessions_hard: int = Field(6, ge=1, le=6)
    cpu_quota_cores: float = Field(2.0, gt=0, le=64)     # 读不到单元根 cpu.max 时的 Q（AWR-04 §4.4.1）
    costs: SbCosts = Field(default_factory=SbCosts)
    mem_sandbox_mb: float = Field(1100, gt=0)
    limits: SbLimits = Field(default_factory=SbLimits)
    rate_tiers: list[float] = Field(default_factory=lambda: [0.25, 0.5, 1, 2, 5, 10], min_length=1)
    rate_cap_k: float = Field(0.85, gt=0)
    recheck_s: float = Field(5, gt=0)
    lifetime: SbLifetime = Field(default_factory=SbLifetime)
    idle: SbIdle = Field(default_factory=SbIdle)
    notice_s: float = Field(60, ge=0)
    queue: SbQueue = Field(default_factory=SbQueue)
    prefix: SbPrefix = Field(default_factory=SbPrefix)
    pow: SbPow = Field(default_factory=SbPow)
    spawn: SbSpawn = Field(default_factory=SbSpawn)
    cgroup: SbCgroup = Field(default_factory=SbCgroup)
    ladder: SbLadder = Field(default_factory=SbLadder)
    planning: SbPlanning = Field(default_factory=SbPlanning)
    ws: SbWs = Field(default_factory=SbWs)
    rest: SbRest = Field(default_factory=SbRest)
    public_rps: float = Field(20, gt=0)
    public_burst: float = Field(120, ge=1)
    event_ring: int = Field(8192, ge=256, le=65536)
    snapshot_keep_s: float = Field(600, ge=0)
    disk: SbDisk = Field(default_factory=SbDisk)
    batch: SbBatch = Field(default_factory=SbBatch)
    prewarm: int = Field(0, ge=0, le=1)               # P1：预热 sim-core（M17-FR-030）
    park: SbPark = Field(default_factory=SbPark)          # D2.1 驻留（AWR-04 §15.7.4）
    internal: SbInternal = Field(default_factory=SbInternal)  # D2.1 内部档（AWR-04 §15.9.3）

    @field_validator("worlds_allow")
    @classmethod
    def _worlds(cls, v: list[str]) -> list[str]:
        for w in v:
            if not re.fullmatch(WORLD_ID_RE, w):
                raise ValueError(f"worlds_allow 必须为小写世界 id：{w!r}")
        return v

    @field_validator("rate_tiers")
    @classmethod
    def _tiers(cls, v: list[float]) -> list[float]:
        if any(x <= 0 for x in v) or v != sorted(v) or 1 not in v:
            raise ValueError("rate_tiers 必须升序、为正且包含 1")
        return v

    @model_validator(mode="after")
    def _caps(self) -> SandboxCfg:
        if isinstance(self.max_sessions, int) and not 0 <= self.max_sessions <= self.max_sessions_hard:
            raise ValueError(f"max_sessions 必须为 auto 或 0–{self.max_sessions_hard}")
        if isinstance(self.ws.max, int) and not 1 <= self.ws.max <= self.ws.max_hard:
            raise ValueError(f"ws.max 必须为 auto 或 1–{self.ws.max_hard}")
        if self.lifetime.initial_s > self.lifetime.max_s:
            raise ValueError("lifetime.initial_s 不能大于 lifetime.max_s")
        return self

    def state_path(self, root: Path = ROOT) -> Path:
        p = Path(self.state_dir)
        return p if p.is_absolute() else root / p

    def world_costs_path(self, root: Path = ROOT) -> Path:
        p = Path(self.world_costs)
        return p if p.is_absolute() else root / p

    def n_max(self, q_cores: float | None = None, *, delegated: bool = True) -> int:
        """并发沙盒上限（AWR-04 §4.4.1 公式；M17 §6.6.4）：配置为整数时取之；auto 时
        `min(hard, ⌊(0.9·Q − C_core − C_ctl − B_ff,min) / c_s⌋)`，无委派时再减 1，下限 0。"""
        if isinstance(self.max_sessions, int):
            return self.max_sessions
        q = self.cpu_quota_cores if q_cores is None else float(q_cores)
        c = self.costs
        n = int((0.9 * q - c.c_core - c.c_ctl - c.b_ff_min) // c.c_s)
        n = min(self.max_sessions_hard, n)
        if not delegated:
            n -= 1
        return max(0, n)

    def ws_max_for(self, n_max: int) -> int:
        """sandbox-api 全站 WS 上限 `4·N_max`（硬上限 ws.max_hard；M11-FR-210）。"""
        if isinstance(self.ws.max, int):
            return self.ws.max
        return max(1, min(self.ws.max_hard, self.ws.per_session * max(1, n_max)))

    def effective_worlds(self, net_allow: list[str]) -> list[str] | None:
        """沙盒可用世界：本段白名单，空时沿用 net.worlds_allow，再空时不限制（None）。"""
        return list(self.worlds_allow or net_allow) or None


class RealOpsCfg(_M):
    enabled: bool = False


# ---------------------------------------------------------------- D2.1 段（AWR-04 §15.6.2、§15.7.2、§15.9.2；M11-FR-230）
class PowerCfg(_M):
    """节能阈值（墙钟秒）：前端以容量接口与 serverInfo.power 下发的值为准；show-api 兜底回收与池驻留读同一段。"""

    viewer_idle_s: float = Field(300, gt=0)
    viewer_idle_busy_s: float = Field(120, gt=0)
    sandbox_idle_s: float = Field(600, gt=0)
    sandbox_idle_busy_s: float = Field(300, gt=0)
    hidden_s: float = Field(60, gt=0)
    warn_s: float = Field(30, ge=0)
    quiet_s: float = Field(60, gt=0)
    grace_s: float = Field(90, ge=0)
    presence_min_s: float = Field(15, ge=0)
    internal_idle_s: float = Field(3600, gt=0)
    reap: bool = True                       # show-api 兜底回收开关（本机基准与浸泡可关闭，d2-WP-12c-to-M11 第 7 条）
    scan_s: float = Field(10, gt=0, le=60)  # 兜底扫描周期（AWR-04 §15.7.4 第 2 条；测试可缩短）


class ViewerSeatsCfg(_M):
    """共享展示实时观看席位与观看排队（show-api 内存，AWR-04 §15.6.3）。"""

    max: Literal["auto"] | int = "auto"     # auto = net.ws_max（ws_max_show）
    queue_max: int = Field(100, ge=0, le=10000)
    reserve_s: float = Field(30, gt=0)
    per_prefix: int = Field(2, ge=1, le=64)
    ticket_idle_s: float = Field(60, gt=0)
    long_poll_s: float = Field(25, gt=0, le=60)

    @field_validator("max")
    @classmethod
    def _max(cls, v: Any) -> Any:
        if isinstance(v, int) and not 1 <= v <= 1024:
            raise ValueError("viewer_seats.max 必须为 auto 或 1–1024")
        return v


class IdentityLockoutCfg(_M):
    prefix_fail: int = Field(5, ge=1)
    window_s: float = Field(600, gt=0)
    prefix_lock_s: float = Field(1800, gt=0)
    agg_fail: int = Field(10, ge=1)
    agg_lock_s: float = Field(1800, gt=0)
    global_fail_h: int = Field(30, ge=1)
    global_window_s: float = Field(3600, gt=0)
    global_lock_s: float = Field(900, gt=0)


class IdentityCfgM(_M):
    """内部身份（ADR-123）：比对、限流与锁定由 M17 库 `awr.sandbox.identity` 执行；口令只经 AWR_ADMIN_SECRET_FILE。"""

    token_ttl_s: float = Field(43200, gt=0)
    delay_ms: float = Field(300, ge=0)
    jitter_ms: float = Field(25, ge=0)
    rps: float = Field(0.5, gt=0)
    burst: float = Field(3, ge=1)
    revoked_max: int = Field(1000, ge=1)
    state_dir: str = "var/identity"         # 相对仓库根或绝对路径（锁定表、撤销表）
    lockout: IdentityLockoutCfg = Field(default_factory=IdentityLockoutCfg)


class RuntimeConfig(_M):
    version: Literal[1] = 1
    run: RunCfg = Field(default_factory=RunCfg)
    net: NetCfg = Field(default_factory=NetCfg)
    bus: BusCfg = Field(default_factory=BusCfg)
    quota: QuotaCfg = Field(default_factory=QuotaCfg)
    cpu: CpuCfg = Field(default_factory=CpuCfg)
    defaults: DefaultsCfg = Field(default_factory=DefaultsCfg)
    real_ops: RealOpsCfg = Field(default_factory=RealOpsCfg)
    sandbox: SandboxCfg = Field(default_factory=SandboxCfg)
    power: PowerCfg = Field(default_factory=PowerCfg)
    viewer_seats: ViewerSeatsCfg = Field(default_factory=ViewerSeatsCfg)
    identity: IdentityCfgM = Field(default_factory=IdentityCfgM)
    procs: list[ProcCfg] = Field(default_factory=list)

    # 不参与校验的解析结果（load_runtime_config 填写）
    world_fallback_note: str | None = None
    profile: str = "dev"
    procs_enable: list[Literal["core", "ext", "dev", "sandbox", "d3"]] = Field(default_factory=lambda: ["core", "ext"])
    source_path: str | None = None

    @model_validator(mode="after")
    def _cross(self) -> RuntimeConfig:
        names = [p.name for p in self.procs]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"procs 中进程名重复：{sorted(dup)}")
        for p in self.procs:
            for d in p.start_after:
                if d not in names:
                    raise ValueError(f"procs[{p.name}].start_after 引用了不存在的进程 {d!r}")
        ranges = sorted((p.id_range, p.name) for p in self.procs if p.id_range is not None)
        for (a, na), (b, nb) in itertools.pairwise(ranges):
            if a[0] + a[1] > b[0]:
                raise ValueError(f"id_range 相交：{na} {list(a)} 与 {nb} {list(b)}")
        return self

    # ------------------------------------------------------------ 派生值
    @property
    def offset(self) -> int:
        return self.net.port_offset * 10

    @property
    def port_effective(self) -> int:
        return self.net.port + self.offset

    @property
    def vite_port_effective(self) -> int:
        return 5173 + self.offset

    @property
    def static_port_effective(self) -> int:
        return self.net.static_port + self.offset

    @property
    def sandbox_port_effective(self) -> int:
        return self.sandbox.port + self.offset

    @property
    def rendezvous_effective(self) -> str:
        host, port = self.bus.rendezvous.rsplit(":", 1)
        return f"{host}:{int(port) + self.offset}"

    @property
    def rendezvous_port(self) -> int:
        return int(self.rendezvous_effective.rsplit(":", 1)[1])

    @property
    def access_mode(self) -> str:
        if self.net.access_mode != "auto":
            return self.net.access_mode
        return "loopback" if _is_loopback(self.net.bind) else "lan"

    @property
    def allowed_hosts(self) -> list[str]:
        hosts = ["localhost", "127.0.0.1"]
        for h in self.net.allowed_hosts:
            if h not in hosts:
                hosts.append(h)
        if not self.net.allowed_hosts:
            for o in self.net.origins:
                h = re.sub(r"^https?://", "", o).rsplit(":", 1)[0]
                if h not in hosts:
                    hosts.append(h)
        return hosts

    @property
    def cpu_pin_enabled(self) -> bool:
        if self.cpu.pin == "on":
            return True
        if self.cpu.pin == "off":
            return False
        return (os.cpu_count() or 1) >= 8 and self.profile != "ci"

    def proc(self, name: str) -> ProcCfg:
        for p in self.procs:
            if p.name == name:
                return p
        raise KeyError(name)

    def enabled_procs(self) -> list[ProcCfg]:
        return [p for p in self.procs if p.layer in self.procs_enable]

    def restart_for(self, p: ProcCfg) -> RestartCfg:
        base = self.defaults.restart
        o = p.restart
        if o is None:
            return base
        return RestartCfg(policy=o.policy or base.policy, backoff_s=o.backoff_s or list(base.backoff_s),
                          max=o.max or base.max)

    def stop_for(self, p: ProcCfg) -> StopCfg:
        return p.stop or self.defaults.stop

    def substitutions(self) -> dict[str, str]:
        return {"net.bind": self.net.bind, "net.port_effective": str(self.port_effective),
                "net.vite_port_effective": str(self.vite_port_effective),
                "net.static_port_effective": str(self.static_port_effective), "run.world": self.run.world,
                "bus.rendezvous": self.rendezvous_effective, "sandbox.port_effective": str(self.sandbox_port_effective)}

    def expand(self, s: str) -> str:
        subs = self.substitutions()

        def rep(m: re.Match[str]) -> str:
            k = m.group(1)
            if k not in subs:
                raise ConfigError("procs[].cmd", f"未知占位符 ${{{k}}}")
            return subs[k]

        return re.sub(r"\$\{([a-z_.]+)\}", rep, s)

    def effective_dict(self) -> dict[str, Any]:
        """生效配置（含派生值），秘密键掩码为 ***；写入 runs/<run>/effective-config.yaml。"""
        d = self.model_dump(mode="json", exclude={"source_path", "world_fallback_note"})
        d["derived"] = {"port_effective": self.port_effective, "vite_port_effective": self.vite_port_effective,
                        "static_port_effective": self.static_port_effective,
                        "sandbox_port_effective": self.sandbox_port_effective,
                        "rendezvous_effective": self.rendezvous_effective, "access_mode": self.access_mode,
                        "allowed_hosts": self.allowed_hosts, "cpu_pin_enabled": self.cpu_pin_enabled,
                        "enabled_procs": [p.name for p in self.enabled_procs() if not p.on_demand]}
        return mask(d)


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------- 加载
_ENV_MAP: dict[str, tuple[str, str]] = {  # 变量 -> (键路径, 类型)
    "AWR_WORLD": ("run.world", "str"),
    "AWR_SCENARIO": ("run.scenario", "str"),
    "AWR_SCENARIO_PROFILE": ("run.scenario_profile", "str"),
    "AWR_RUNS_DIR": ("run.persist_root", "str"),
    "AWR_KEEP_RUN_DIR": ("run.keep_run_dir", "bool"),
    "AWR_BIND": ("net.bind", "str"),
    "AWR_PORT": ("net.port", "int"),
    "AWR_PORT_OFFSET": ("net.port_offset", "int"),
    "AWR_STATIC_PORT": ("net.static_port", "int"),
    "AWR_ACCESS_MODE": ("net.access_mode", "str"),
    "AWR_ORIGINS": ("net.origins", "csv"),
    "AWR_ALLOWED_HOSTS": ("net.allowed_hosts", "csv"),
    "AWR_TRUSTED_PROXIES": ("net.trusted_proxies", "csv"),
    "AWR_WORLDS_ALLOW": ("net.worlds_allow", "csv"),
    "AWR_WS_MAX": ("net.ws_max", "int"),
    "AWR_WS_MAX_PER_IP": ("net.ws_max_per_ip", "int"),
    "AWR_RUNS_QUOTA_GB": ("quota.runs_gb", "float"),
    "AWR_CPU_PIN": ("cpu.pin", "str"),
    "AWR_REAL_OPS": ("real_ops.enabled", "bool"),
    # D2 沙盒（M17 PRD §8.4、§8.5：部署环境文件可覆盖）
    "AWR_SANDBOX_ENABLED": ("sandbox.enabled", "bool"),
    "AWR_SANDBOX_STATE_DIR": ("sandbox.state_dir", "str"),
    "AWR_SANDBOX_PORT": ("sandbox.port", "int"),
    "AWR_SANDBOX_MAX_SESSIONS": ("sandbox.max_sessions", "auto_int"),
    "AWR_SANDBOX_CPU_QUOTA": ("sandbox.cpu_quota_cores", "float"),
    "AWR_SANDBOX_MEM_MB": ("sandbox.mem_sandbox_mb", "float"),
    # D2.1（tools/deploy/emax/env.example；d2-WP-16-to-M11 第 2 条）
    "AWR_POWER_VIEWER_IDLE_S": ("power.viewer_idle_s", "int"),
    "AWR_POWER_SANDBOX_IDLE_S": ("power.sandbox_idle_s", "int"),
    "AWR_POWER_HIDDEN_S": ("power.hidden_s", "int"),
    "AWR_POWER_WARN_S": ("power.warn_s", "int"),
    "AWR_VIEWER_SEATS_MAX": ("viewer_seats.max", "auto_int"),
    "AWR_VIEWER_QUEUE_MAX": ("viewer_seats.queue_max", "int"),
    "AWR_SANDBOX_PARK_RETENTION_S": ("sandbox.park.retention_s", "int"),
    "AWR_IDENTITY_STATE_DIR": ("identity.state_dir", "str"),
}


def _conv(var: str, raw: str, typ: str) -> Any:
    try:
        if typ == "int":
            return int(raw)
        if typ == "float":
            return float(raw)
        if typ == "bool":
            v = raw.strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off", ""):
                return False
            raise ValueError(raw)
        if typ == "csv":
            return [x.strip() for x in raw.split(",") if x.strip()]
        if typ == "auto_int":
            return "auto" if raw.strip().lower() == "auto" else int(raw)
        return raw
    except ValueError:
        raise ConfigError(_ENV_MAP[var][0], f"环境变量 {var}={raw!r} 不是合法的 {typ}") from None


def _set_path(d: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if nxt is None:
            nxt = cur[p] = {}
        if not isinstance(nxt, dict):
            raise ConfigError(path, "路径中间节点不是映射")
        cur = nxt
    cur[parts[-1]] = value


def _merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _fmt_loc(loc: tuple[Any, ...], data: dict[str, Any]) -> str:
    out: list[str] = []
    cur: Any = data
    for x in loc:
        if isinstance(x, int):
            name = cur[x].get("name") if isinstance(cur, list) and x < len(cur) and isinstance(cur[x], dict) else None
            out[-1] = f"{out[-1]}[{name or x}]" if out else f"[{x}]"
            cur = cur[x] if isinstance(cur, list) and x < len(cur) else None
        else:
            out.append(str(x))
            cur = cur.get(x) if isinstance(cur, dict) else None
    return ".".join(out)


def resolve_profile(profile: str | None, env: Mapping[str, str]) -> str:
    return profile or env.get("AWR_PROFILE") or "dev"


def _catalog_default(world_id: str, env: Mapping[str, str]) -> str | None:
    root = Path(env.get("AWR_SCENARIOS_DIR") or ROOT / "scenarios")
    try:
        import json

        v = ((json.loads((root / "catalog.json").read_text(encoding="utf-8")).get("worlds") or {}).get(world_id) or {}).get("default")
        return v if isinstance(v, str) else None
    except (OSError, ValueError, AttributeError):
        return None


def apply_world_fallback(cfg: RuntimeConfig, *, env: Mapping[str, str], argv: Sequence[str] = ()) -> str | None:
    """默认世界回退（ADR-077；与 `awr.world.package.defaults.resolve_default_world` 同一规则，runtime 不 import awr.world）：

    世界未经 `AWR_WORLD` 或 `--set run.world=` 显式指定、`worlds/<run.world>/world.json` 不存在、而 `run.fallback_world`
    （synthcity）已构建时，改用回退世界；剧本未经 `AWR_SCENARIO` 或 `--set run.scenario=` 指定时改为
    `scenarios/catalog.json` 中该世界的 `default`。返回说明文字（未回退时 None）。"""
    keys = {a.split("=", 1)[0].strip() for a in argv if "=" in a}
    fb = cfg.run.fallback_world
    if "AWR_WORLD" in env or "run.world" in keys or not fb or fb == cfg.run.world:
        return None
    wd = Path(env.get("AWR_WORLDS_DIR") or ROOT / "worlds")
    if (wd / cfg.run.world / "world.json").exists() or not (wd / fb / "world.json").exists():
        return None
    old = cfg.run.world
    cfg.run.world = fb
    if "AWR_SCENARIO" not in env and "run.scenario" not in keys:
        sc = _catalog_default(fb, env)
        if sc:
            cfg.run.scenario = sc
    return f"默认世界 {old} 未构建，回退到合成演示城市 {fb}（剧本 {cfg.run.scenario}；ADR-077）"


def load_runtime_config(path: Path | str | None = None, *, profile: str | None = None, env: Mapping[str, str] | None = None,
                        argv: Sequence[str] = (), world_fallback: bool = False) -> RuntimeConfig:
    """解析 runtime.yaml 并叠加 profile、环境变量与命令行覆盖（argv 为 `key.path=value` 列表，值按 YAML 解析）。

    `world_fallback = True`（supervisor 与 awr CLI）时按 ADR-077 应用默认世界回退（`apply_world_fallback`），说明写入
    `cfg.world_fallback_note`。"""
    env = os.environ if env is None else env
    path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError("", f"配置文件不存在：{path}") from None
    except yaml.YAMLError as e:
        raise ConfigError("", f"YAML 语法错误：{e}") from None
    if not isinstance(raw, dict):
        raise ConfigError("", "顶层必须是映射")
    profiles = raw.pop("profiles", {}) or {}
    if not isinstance(profiles, dict):
        raise ConfigError("profiles", "必须是映射")
    prof = resolve_profile(profile, env)
    if prof == "field":
        raise ConfigError("profiles.field", "field profile 自 V0.5 起定义，D1 中不可选择")
    if prof not in profiles:
        raise ConfigError("profiles", f"未知 profile {prof!r}（可选：{', '.join(sorted(profiles))}）")
    overlay = dict(profiles.get(prof) or {})
    procs_enable = overlay.pop("procs_enable", ["core", "ext"])
    for k in overlay:
        if k not in RuntimeConfig.model_fields or k in ("procs", "profile", "procs_enable", "source_path"):
            raise ConfigError(f"profiles.{prof}.{k}", "profile 中不允许的键")
    if not isinstance(procs_enable, list) or any(x not in LAYERS for x in procs_enable):
        raise ConfigError(f"profiles.{prof}.procs_enable", f"取值只能是 {list(LAYERS)}")
    data = _merge(raw, overlay)
    for var, (kp, typ) in _ENV_MAP.items():
        if var in env:
            _set_path(data, kp, _conv(var, env[var], typ))
    for item in argv:
        if "=" not in item:
            raise ConfigError("", f"命令行覆盖应为 key.path=value：{item!r}")
        kp, val = item.split("=", 1)
        try:
            v = yaml.safe_load(val)
        except yaml.YAMLError:
            v = val
        _set_path(data, kp.strip(), v)
    data["profile"] = prof
    data["procs_enable"] = procs_enable
    data["source_path"] = str(path)
    try:
        cfg = RuntimeConfig.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        loc = _fmt_loc(tuple(err.get("loc", ())), data)
        typ = err.get("type", "")
        msg = "未知键" if typ == "extra_forbidden" else err.get("msg", "非法值")
        raise ConfigError(loc, msg) from None
    if cfg.real_ops.enabled:
        raise ConfigError("real_ops.enabled", "D1 中真机闸门必须关闭（OPS-FR-032；AWR_REAL_OPS 同理）")
    if cfg.access_mode in ("lan", "public") and not cfg.net.origins:
        raise ConfigError("net.origins", f"{cfg.access_mode} 模式必须配置 origins（AWR_ORIGINS）")
    if cfg.access_mode == "public" and not _is_loopback(cfg.net.bind):
        raise ConfigError("net.bind", "public 模式只能监听回环地址（由反向代理对外，AWR-19 §3.7）")
    if not cfg.defaults.standby:  # 热备用总开关关闭：逐进程的 standby 一律视为 false（ADR-082）
        for p in cfg.procs:
            p.standby = False
    if world_fallback:
        cfg.world_fallback_note = apply_world_fallback(cfg, env=env, argv=argv)
    return cfg


def sandbox_from_env(env: Mapping[str, str] | None = None) -> SandboxCfg:
    """sandbox-pool 与 sandbox-api 读取 `sandbox:` 段：优先 supervisor 写出的生效配置（`AWR_PERSIST_DIR/effective-config.yaml`，
    含 profile、环境变量与命令行覆盖）；没有时按 `AWR_CONFIG` + `AWR_PROFILE` 重新加载；再没有时取内置默认。
    `AWR_SANDBOX_*` 环境变量（supervisor 对 sandbox 层进程注入的绝对状态目录与端口）最后覆盖。"""
    e = os.environ if env is None else env
    data: dict[str, Any] | None = None
    pd = e.get("AWR_PERSIST_DIR")
    if pd:
        try:
            eff = yaml.safe_load((Path(pd) / "effective-config.yaml").read_text(encoding="utf-8"))
            if isinstance(eff, dict) and isinstance(eff.get("sandbox"), dict):
                data = dict(eff["sandbox"])
        except (OSError, yaml.YAMLError):
            data = None
    if data is None and e.get("AWR_CONFIG"):
        try:
            data = load_runtime_config(e["AWR_CONFIG"], profile=e.get("AWR_PROFILE"), env=e).sandbox.model_dump(mode="json")
        except ConfigError:
            data = None
    data = data or {}
    over: dict[str, Any] = {}
    for var, (kp, typ) in _ENV_MAP.items():
        if kp.startswith("sandbox.") and var in e:
            _set_path(over, kp.split(".", 1)[1], _conv(var, e[var], typ))
    if "AWR_SANDBOX_WORLDS_ALLOW" in e:
        over["worlds_allow"] = [x.strip() for x in e["AWR_SANDBOX_WORLDS_ALLOW"].split(",") if x.strip()]
    try:
        return SandboxCfg.model_validate(_merge(data, over))
    except ValidationError as ex:
        err = ex.errors()[0]
        raise ConfigError("sandbox." + ".".join(str(x) for x in err.get("loc", ())), err.get("msg", "非法值")) from None


def section_from_env(name: str, env: Mapping[str, str] | None = None) -> Any:
    """D2.1 顶层段（`power`、`viewer_seats`、`identity`）的生效值，供 show-api 与 sandbox-api 读取（M11-FR-230）：
    supervisor 写出的生效配置（`AWR_PERSIST_DIR/effective-config.yaml`），其次 `AWR_CONFIG` + `AWR_PROFILE` 重新加载，再没有取
    内置缺省；环境变量 `AWR_<NAME>_JSON` 最后逐键覆盖（测试栈缩短阈值，与 `awr.sandbox.config.raw_section` 同一约定）。
    返回校验后的 pydantic 段对象；覆盖非法时忽略覆盖。"""
    import json

    e = os.environ if env is None else env
    model = RuntimeConfig.model_fields[name].annotation
    data: dict[str, Any] | None = None
    pd = e.get("AWR_PERSIST_DIR")
    if pd:
        try:
            eff = yaml.safe_load((Path(pd) / "effective-config.yaml").read_text(encoding="utf-8"))
            if isinstance(eff, dict) and isinstance(eff.get(name), dict):
                data = dict(eff[name])
        except (OSError, yaml.YAMLError):
            data = None
    if data is None and e.get("AWR_CONFIG"):
        try:
            data = getattr(load_runtime_config(e["AWR_CONFIG"], profile=e.get("AWR_PROFILE"), env=e), name).model_dump(mode="json")
        except ConfigError:
            data = None
    data = data or {}
    js = e.get(f"AWR_{name.upper()}_JSON")
    if js:
        try:
            over = json.loads(js)
            if isinstance(over, dict):
                merged = _merge(data, over)
                return model.model_validate(merged)  # type: ignore[union-attr]
        except (ValueError, ValidationError):
            pass
    try:
        return model.model_validate(data)  # type: ignore[union-attr]
    except ValidationError:
        return model()  # type: ignore[operator]
