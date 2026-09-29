"""api 进程设置（M11-FR-015、FR-022；AWR-03 §3.3；AWR-17 §3.3；AWR-19 §6.3 注入变量）。

supervisor 下由注入的 `AWR_*` 变量与 `RunCtx` 构造（秘密只经 `AWR_SECRET_FILE`，管理口令读 `runs/<run>/admin.token`）；
独立运行（`uvicorn awr.api.main:app` 开发用）时生成进程内随机秘密与管理口令。`--inproc` 与测试直接构造本类。
Host 白名单：{localhost, 127.0.0.1} 并上 `AWR_ALLOWED_HOSTS`（为空时由 `AWR_ORIGINS` 推导）；Origin 白名单：回环正则并上 `AWR_ORIGINS`。
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from awr.contracts import CONTRACTS_VERSION, bus_keys

__all__ = ["LOOPBACK_ORIGIN_RE", "ROOT", "ApiSettings"]

ROOT = Path(__file__).resolve().parents[3]
LOOPBACK_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$")
_RUN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    with contextlib.suppress(ValueError):
        return ipaddress.ip_address(host).is_loopback
    return False


def _csv(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


@dataclass
class ApiSettings:
    run_id: str = "local"
    world_id: str = "shenzhen"
    run_dir: Path = Path("/dev/shm/awr/local")
    persist_dir: Path | None = None
    worlds_dir: Path = ROOT / "worlds"
    web_dist: Path | None = ROOT / "apps" / "web" / "dist"
    bind: str = "127.0.0.1"
    access_mode: str = "loopback"
    origins: list[str] = field(default_factory=list)
    allowed_hosts_extra: list[str] = field(default_factory=list)
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    admin_password: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    profile: str = "dev"
    supervised: bool = False
    bus_kind: str = "zenoh"
    contracts: str = CONTRACTS_VERSION
    hello_timeout_s: float = 10.0
    max_conns: int = 32
    max_conns_per_principal: int = 8
    serve_web: bool = True
    producer: str = "sim-core"
    zenoh_config: Path | None = None
    scenario_id: str | None = None
    procs_query: bool = False  # 未受监管时也经总线查询 sys/procs（测试与 --inproc 挂 supervisor 替身时使用）

    @property
    def has_supervisor(self) -> bool:
        return self.supervised or self.procs_query

    @property
    def namespace(self) -> str:
        return bus_keys.namespace(self.world_id, self.run_id if _RUN_RE.match(self.run_id) else "local")

    @property
    def allowed_hosts(self) -> set[str]:
        hosts = {"localhost", "127.0.0.1", "::1", "[::1]"}
        hosts |= {h.lower() for h in self.allowed_hosts_extra}
        if not self.allowed_hosts_extra:
            for o in self.origins:
                h = urlsplit(o).hostname
                if h:
                    hosts.add(h.lower())
        return hosts

    def origin_allowed(self, origin: str) -> bool:
        return bool(LOOPBACK_ORIGIN_RE.match(origin)) or origin in self.origins

    def host_allowed(self, host_header: str | None) -> bool:
        if not host_header:
            return False
        h = host_header.strip().lower()
        if h.startswith("["):
            h = h[: h.find("]") + 1] if "]" in h else h
        elif h.count(":") == 1:
            h = h.split(":", 1)[0]
        return h in self.allowed_hosts

    @property
    def ring_path(self) -> Path:
        return self.run_dir / f"state.{self.producer}"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ApiSettings:
        """由 supervisor 注入的环境变量构造（supervised 时读取秘密文件与管理口令文件）。"""
        e = os.environ if env is None else env
        run_id = e.get("AWR_RUN", "local")
        runs_root = Path(e.get("AWR_RUNS_DIR", str(ROOT / "runs")))
        persist = Path(e.get("AWR_PERSIST_DIR", str(runs_root / run_id)))
        bind = e.get("AWR_BIND", "127.0.0.1")
        mode = e.get("AWR_ACCESS_MODE", "auto")
        if mode not in ("loopback", "lan"):
            mode = "loopback" if _is_loopback(bind) else "lan"
        s = cls(run_id=run_id, world_id=e.get("AWR_WORLD", "shenzhen"),
                run_dir=Path(e.get("AWR_RUN_DIR", f"/dev/shm/awr/{run_id}")), persist_dir=persist,
                worlds_dir=Path(e.get("AWR_WORLDS_DIR", str(ROOT / "worlds"))),
                web_dist=Path(e["AWR_WEB_DIST"]) if e.get("AWR_WEB_DIST") else ROOT / "apps" / "web" / "dist",
                bind=bind, access_mode=mode, origins=_csv(e.get("AWR_ORIGINS")),
                allowed_hosts_extra=_csv(e.get("AWR_ALLOWED_HOSTS")), profile=e.get("AWR_PROFILE", "dev"),
                supervised=bool(e.get("AWR_SUPERVISOR_PID")), bus_kind=e.get("AWR_API_BUS", "zenoh"),
                serve_web=e.get("AWR_SERVE_WEB", "1") not in ("0", "false", "off"),
                zenoh_config=Path(e["AWR_ZENOH_CONFIG"]) if e.get("AWR_ZENOH_CONFIG") else None,
                scenario_id=e.get("AWR_SCENARIO") or None)
        sf = e.get("AWR_SECRET_FILE")
        if sf and Path(sf).exists():
            s.secret = Path(sf).read_bytes()
        at = persist / "admin.token"
        if at.exists():
            with contextlib.suppress(OSError):
                s.admin_password = at.read_text(encoding="utf-8").strip()
        return s
