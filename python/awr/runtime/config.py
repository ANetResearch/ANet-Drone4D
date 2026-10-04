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

__all__ = ["DEFAULT_CONFIG_PATH", "ConfigError", "ProcCfg", "RuntimeConfig", "apply_world_fallback", "load_runtime_config",
           "resolve_profile"]

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = ROOT / "configs" / "runtime.yaml"
D1_PROFILES = ("dev", "demo", "ci", "perf", "public")
LAYERS = ("core", "ext", "dev")


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
    layer: Literal["core", "ext", "dev"] = "core"
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


class RealOpsCfg(_M):
    enabled: bool = False


class RuntimeConfig(_M):
    version: Literal[1] = 1
    run: RunCfg = Field(default_factory=RunCfg)
    net: NetCfg = Field(default_factory=NetCfg)
    bus: BusCfg = Field(default_factory=BusCfg)
    quota: QuotaCfg = Field(default_factory=QuotaCfg)
    cpu: CpuCfg = Field(default_factory=CpuCfg)
    defaults: DefaultsCfg = Field(default_factory=DefaultsCfg)
    real_ops: RealOpsCfg = Field(default_factory=RealOpsCfg)
    procs: list[ProcCfg] = Field(default_factory=list)

    # 不参与校验的解析结果（load_runtime_config 填写）
    world_fallback_note: str | None = None
    profile: str = "dev"
    procs_enable: list[Literal["core", "ext", "dev"]] = Field(default_factory=lambda: ["core", "ext"])
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
                "bus.rendezvous": self.rendezvous_effective}

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
