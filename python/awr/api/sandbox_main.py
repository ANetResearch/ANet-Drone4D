"""sandbox-api 进程入口：沙盒 REST v1 与会话 WS 的 FastAPI 应用（M11-FR-203、FR-204；AWR-04 §4.7、ADR-092；17 §17.5、§17.7；
M17 PRD §4.6、§8.1）。

启动方式（`configs/runtime.yaml` 的 `sandbox-api` 条目，由 supervisor 按 D1 语义监管）：
`uvicorn awr.api.sandbox_main:app --host 127.0.0.1 --port ${sandbox.port_effective} --workers 1 …`；开发：
`python -m awr.api.sandbox_main [--port 8002]`。

- 路由挂载（FR-203）：`awr.api.rest` 各模块导出的 `sandbox_public_router` 挂在 `/api/sandbox/v1`，`sandbox_router` 挂在
  `/api/sandbox/v1/sessions/{sid}`（路由器级依赖 `deps.require_sandbox_session`：沙盒 token、sid 作用域、会话状态与活动计数）；
  按文件名排序发现（D1 `discover_routers`），导入失败记错误日志后跳过；拒绝清单 runs、jobs、recon、agents、sys、scenarios、auth、
  sessions 中的模块即使导出 `sandbox_router` 也不挂载。WS `/api/sandbox/v1/sessions/{sid}/rt`（`rt/ws.sandbox_rt_endpoint`）；
  `GET /api/sandbox/v1/openapi.json`（SB-20，由已挂载路由生成，OpenAPI 3.1，`bearerAuth`，`x-awr-realtime`）；
  `GET /api/sandbox/v1/health`（不计限流，不入 OpenAPI）。D1 的静态、SPA 与全站 OpenAPI 不在本进程。
- 中间件：D1 `AwrMiddleware`（Host、Origin、请求 id、安全头、请求体上限、Idempotency-Key），鉴权为沙盒 token
  （`SandboxTokenService`，主密钥 `<state_dir>/token.key`），限流为 `ratelimit.sandbox_rate_checks`（公开路由按地址前缀、会话
  路由按 sid）。
- lifespan：受监管时 `init_child("sandbox-api")`（保存并恢复 uvicorn 的信号处理器）→ 按 `AWR_CGROUP` 加入 `sandbox/ctl/`
  （无委派时留在原组，FR-216）→ 读 `<state_dir>/pool.secret` 派生 `K_pool` → 打开池命名空间 `awr/_sandbox/pool` 的总线会话
  → GatewayHub 启动（`sys/session/list` 重建全部上下文，FR-215）→ 10 Hz 心跳 `hb.sandbox-api`。

`live_openapi()`：以固定设置构造应用（无副作用：总线与 hub 在 lifespan 中才启动）并返回其 OpenAPI 文档。全部领域路由挂齐之前
沙盒快照仍取 WP-01 的设计表（`tools/contracts/gen_openapi.py`）；路由挂齐后由 M00 切换为调用本函数（见实现报告与请求）。
`missing_design_routes()` 列出设计表中尚未挂载的路由。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from awr.contracts import CONTRACTS_VERSION, bus_keys
from awr.contracts import commands as C
from awr.contracts import topics as T
from awr.runtime import cgroup
from awr.runtime.binding import RunBinding
from awr.runtime.bus import LocalBus, open_bus
from awr.runtime.child import init_child
from awr.runtime.config import ROOT, SandboxCfg, sandbox_from_env
from awr.runtime.sbauth import POOL_SECRET_NAME, TOKEN_KEY_NAME, load_or_create_key, pool_key
from awr.world.package.catalog import Catalog

from .audit import AuditWriter
from .deps import require_sandbox_session
from .main import _install_handlers, discover_routers
from .middleware import AwrMiddleware
from .ratelimit import CATEGORIES, RateLimiter, sandbox_categories, sandbox_rate_checks
from .rt.hub import GatewayHub, PoolClient
from .rt.ws import sandbox_rt_endpoint
from .security import SandboxTokenService
from .settings import ApiSettings

__all__ = ["API_VERSION", "DENY_MODULES", "PREFIX", "SandboxApiContext", "app", "build_openapi", "create_sandbox_app",
           "live_openapi", "missing_design_routes"]

log = logging.getLogger("awr.api.sandbox")

PREFIX = "/api/sandbox/v1"
CAPACITY_PATH = PREFIX + "/capacity"
API_VERSION = "1.0.0"
DENY_MODULES = ("runs", "jobs", "recon", "agents", "sys", "scenarios", "auth", "sessions")
HB_PERIOD_S = 0.1
TITLE = "ANet Drone4D Sandbox API"


@dataclass
class SandboxApiContext:
    """sandbox-api 的应用级上下文（`app.state.awr`）；会话级上下文在 `hub.contexts`。"""

    kind: ClassVar[str] = "sandbox"
    settings: ApiSettings
    sb: SandboxCfg
    tokens: SandboxTokenService
    catalog: Catalog
    state_dir: Path
    audit: AuditWriter = field(default_factory=lambda: AuditWriter(None))
    limiter: RateLimiter = field(default_factory=RateLimiter)
    bus_kind: str = "zenoh"
    ring_cls: type | None = None
    pool_bus: Any = None
    session_bus_factory: Callable[[RunBinding], Any] | None = None
    hub: GatewayHub | None = None
    run_ctx: Any = None
    world_files: Any = None
    gateway: Any = None  # 中间件兼容：应用级没有 Gateway
    stopping: bool = False
    started_mono: float = field(default_factory=time.monotonic)
    power_info: dict | None = None  # D2.1 serverInfo.power（`power:` 段，M11-FR-229）
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _own_pool_bus: bool = False

    def rate_checks(self, method: str, path: str, prefix: str) -> list[tuple[str, str]]:
        return sandbox_rate_checks(method, path, prefix)

    def fast_get(self, path: str) -> tuple[int, tuple, bytes] | None:
        """中间件快速路径（D2-FX-R2）：`GET /api/sandbox/v1/capacity` 的缓存可用时返回 (状态, 头, 响应体)，否则 None。"""
        if path != CAPACITY_PATH or self.hub is None:
            return None
        from .rest.sandbox import CAPACITY_HEADERS, capacity_cache

        body = capacity_cache(self).fast(self.hub)
        return None if body is None else (200, CAPACITY_HEADERS, body)

    # ------------------------------------------------------------ 生命周期
    async def start(self, app: FastAPI) -> None:
        s = self.settings
        loop = asyncio.get_running_loop()
        if s.supervised:
            saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            self.run_ctx = init_child("sandbox-api")
            for sig, h in saved.items():
                with contextlib.suppress(TypeError, ValueError):
                    signal.signal(sig, h)
        cgroup.join_from_env()  # AWR_CGROUP=sandbox/ctl（无委派时只告警一次，FR-216）
        k_pool = pool_key(await asyncio.to_thread(load_or_create_key, self.state_dir / POOL_SECRET_NAME))
        if self.pool_bus is None:
            self.pool_bus = self._open_bus(bus_keys.POOL_NAMESPACE, loop)
            self._own_pool_bus = True
        self.hub = GatewayHub(self, bus_factory=self._session_bus, pool=PoolClient(self.pool_bus, k_pool),
                              pool_bus=self.pool_bus, ring_cls=self.ring_cls)
        await self.hub.start()
        self._install_stop_hook(loop)
        if self.run_ctx is not None:
            self._tasks.append(asyncio.ensure_future(self._heartbeat()))
        log.info("sandbox-api ready", extra={"kv": {"state_dir": str(self.state_dir), "n_max": self.hub.effective_n_max,
                                                     "bus": self.bus_kind}})

    def _open_bus(self, namespace: str, loop: asyncio.AbstractEventLoop | None) -> Any:
        kw: dict[str, Any] = {"namespace": namespace, "announce": False}
        ctx = self.run_ctx
        if self.bus_kind == "zenoh":
            rv = os.environ.get("AWR_RENDEZVOUS")
            if (ctx is None or ctx.zenoh_config is None) and rv:
                kw["connect"] = [rv]
        try:
            return open_bus("sandbox-api", ctx, kind=self.bus_kind, loop=loop, **kw)
        except Exception:
            log.exception("bus open failed; sandbox-api continues isolated", extra={"kv": {"namespace": namespace}})
            return LocalBus.open("sandbox-api", namespace=f"{namespace}/isolated", loop=loop, announce=False)

    def _session_bus(self, b: RunBinding) -> Any:
        """每会话一个总线会话（namespace `awr/<world>/<sid>`，AWR-04 §4.7 第 2 条）。"""
        if self.session_bus_factory is not None:
            return self.session_bus_factory(b)
        return self._open_bus(b.namespace, asyncio.get_running_loop())

    def _install_stop_hook(self, loop: asyncio.AbstractEventLoop) -> None:
        """SIGTERM/SIGINT：置 stopping（写类请求 503 `213`），交还 uvicorn 的优雅关闭（lifespan 关闭全部上下文）。"""
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            prev = signal.getsignal(sig)
            if not callable(prev):
                continue

            def handler(signum: int, frame: Any, prev: Any = prev) -> None:
                self.stopping = True
                prev(signum, frame)

            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)

    async def _heartbeat(self) -> None:
        hb = self.run_ctx.heartbeat_writer()
        while True:
            with contextlib.suppress(OSError):
                hb.beat()
            await asyncio.sleep(HB_PERIOD_S)

    async def stop(self) -> None:
        self.stopping = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        if self.hub is not None:
            await self.hub.stop()
        if self.pool_bus is not None and self._own_pool_bus:
            with contextlib.suppress(Exception):
                self.pool_bus.close()
        await asyncio.to_thread(self.audit.close)


# ---------------------------------------------------------------- OpenAPI
def _realtime_summary() -> dict[str, Any]:
    """`x-awr-realtime`（M17-FR-054）：沙盒可用的 topic、命令（由契约生成物给出）与端点。"""
    topics = [{"topic": t.pattern, "encoding": t.encoding, "schemaName": t.schema_name, "native_hz": t.native_hz,
               "priority": t.priority, "d1": t.d1} for t in T.TOPICS if t.d1 in ("core", "V0.2-demo") and t.kind != "client"]
    deny = ("sys/", "rec/", "seat/", "sim/reset", "confirm/")
    services = [{"service": s.name, "category": s.category, "lease": bool(s.lease), "d1": s.d1} for s in C.SERVICES
                if s.d1 in ("core", "V0.2-demo") and not s.name.startswith(deny) and s.op not in ("kill", "escalate")]
    return {"endpoint": f"{PREFIX}/sessions/{{sid}}/rt", "subprotocol": "awr.rt.v1", "auth": "bearer.<sandbox token>",
            "topics": topics, "services": services}


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """沙盒 OpenAPI 3.1（SB-20）：只含 `/api/sandbox/v1` 下的已挂载路由，加 WS 端点（SB-64）与本文档（SB-20）两条手写项。"""
    # FastAPI ≥ 0.141 以惰性对象挂载 include_router 的路由：交给 get_openapi 展开，再按前缀过滤路径
    spec = get_openapi(title=TITLE, version=API_VERSION, openapi_version="3.1.0", routes=app.routes,
                       description="沙盒 REST v1（AWR-17 §17.5）。会话路由使用 Authorization: Bearer <沙盒 token>。")
    spec["paths"] = {p: ops for p, ops in (spec.get("paths") or {}).items() if p.startswith(PREFIX)}
    comps = spec.setdefault("components", {})
    comps.setdefault("securitySchemes", {})["bearerAuth"] = {"type": "http", "scheme": "bearer", "bearerFormat": "awr-v1"}
    paths = spec.setdefault("paths", {})
    for path, ops in paths.items():
        if "/sessions/{sid}" in path:
            for op in ops.values():
                if isinstance(op, dict):
                    op.setdefault("security", [{"bearerAuth": []}])
    paths[f"{PREFIX}/openapi.json"] = {"get": {"summary": "沙盒 OpenAPI 3.1", "operationId": "sandbox_openapi_get",
                                               "tags": ["sandbox-api"],
                                               "responses": {"200": {"description": "OpenAPI 文档"}}}}
    paths[f"{PREFIX}/sessions/{{sid}}/rt"] = {"get": {
        "summary": "会话实时通道（WebSocket，子协议 awr.rt.v1 与 bearer.<token>）", "operationId": "sandbox_rt_get",
        "tags": ["sandbox-api"], "security": [{"bearerAuth": []}],
        "parameters": [{"name": "sid", "in": "path", "required": True,
                        "schema": {"type": "string", "pattern": "^sb-[a-z2-7]{10}$"}}],
        "responses": {"101": {"description": "Switching Protocols"}}}}
    spec["x-awr-realtime"] = _realtime_summary()
    spec.setdefault("info", {})["x-awr-contracts"] = CONTRACTS_VERSION
    return spec


# ---------------------------------------------------------------- 应用
def _default_settings() -> ApiSettings:
    s = ApiSettings.from_env()
    s.kind = "sandbox"
    s.serve_web = False
    s.web_dist = None
    return s


def create_sandbox_app(settings: ApiSettings | None = None, *, sb: SandboxCfg | None = None,
                       state_dir: Path | str | None = None, token_key: bytes | None = None, bus_kind: str | None = None,
                       pool_bus: Any = None, session_bus_factory: Callable[[RunBinding], Any] | None = None,
                       ring_cls: type | None = None) -> FastAPI:
    s = settings if settings is not None else _default_settings()
    s.kind = "sandbox"
    sbc = sb if sb is not None else sandbox_from_env()
    sd = Path(state_dir) if state_dir is not None else Path(os.environ.get("AWR_SANDBOX_STATE_DIR") or sbc.state_path(ROOT))
    tokens = SandboxTokenService(sd / TOKEN_KEY_NAME if token_key is None else None, token_key=token_key, mode=s.access_mode)
    limiter = RateLimiter(CATEGORIES | sandbox_categories(sbc))
    ctx = SandboxApiContext(settings=s, sb=sbc, tokens=tokens, catalog=Catalog(s.worlds_dir), state_dir=sd,
                            audit=AuditWriter(sd / "audit-api.jsonl" if state_dir is not None or s.supervised else None),
                            limiter=limiter, bus_kind=bus_kind or s.bus_kind, ring_cls=ring_cls, pool_bus=pool_bus,
                            session_bus_factory=session_bus_factory)
    from awr.runtime.config import section_from_env

    from .rt.presence import PowerView, power_info

    ctx.power_info = power_info(PowerView.from_cfg(section_from_env("power")))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await ctx.start(app)
        try:
            yield
        finally:
            await ctx.stop()

    app = FastAPI(title=TITLE, version=API_VERSION, lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)
    app.state.awr = ctx
    _install_handlers(app)
    for _name, r in discover_routers("sandbox_public_router", deny=DENY_MODULES):
        app.include_router(r, prefix=PREFIX)
    for _name, r in discover_routers("sandbox_router", deny=DENY_MODULES):
        app.include_router(r, prefix=PREFIX + "/sessions/{sid}", dependencies=[Depends(require_sandbox_session)])
    app.add_api_websocket_route(PREFIX + "/sessions/{sid}/rt", sandbox_rt_endpoint)
    cache: dict[str, Any] = {}

    async def openapi_route(request: Request) -> JSONResponse:
        if "doc" not in cache:
            cache["doc"] = build_openapi(request.app)
        return JSONResponse(cache["doc"], headers={"Cache-Control": "public, max-age=300"})

    async def health(request: Request) -> JSONResponse:
        hub = ctx.hub
        from awr.runtime.buildinfo import public_build

        body = {"ok": hub is not None and not ctx.stopping, "pool": "up" if hub is not None and hub.pool_up else "down",
                "sessions": len(hub.views) if hub else 0, "contexts": len(hub.contexts) if hub else 0,
                "n_max": hub.effective_n_max if hub else None, "uptime_s": round(time.monotonic() - ctx.started_mono, 1),
                "build": public_build()}
        return JSONResponse(body, status_code=200 if body["ok"] else 503, headers={"Cache-Control": "no-store"})

    async def inspect(request: Request) -> JSONResponse:
        if s.profile not in ("dev", "ci", "perf", "sandbox") or s.is_public:
            return JSONResponse({"code": 305}, status_code=404)
        return JSONResponse(ctx.hub.inspect() if ctx.hub else {}, headers={"Cache-Control": "no-store"})

    app.add_api_route(PREFIX + "/openapi.json", openapi_route, methods=["GET"], include_in_schema=False)
    app.add_api_route(PREFIX + "/health", health, methods=["GET"], include_in_schema=False)
    app.add_api_route(PREFIX + "/_inspect", inspect, methods=["GET"], include_in_schema=False)
    app.add_middleware(AwrMiddleware, ctx=ctx)
    return app


def _fixed_app() -> FastAPI:
    s = ApiSettings(run_id="r20260101-000000-0000", world_id="synthcity", run_dir=Path("/nonexistent/awr-openapi"),
                    persist_dir=None, worlds_dir=ROOT / "worlds", web_dist=None, serve_web=False, profile="dev",
                    secret=b"\0" * 32, admin_password="")
    return create_sandbox_app(s, sb=SandboxCfg(), state_dir=Path("/nonexistent/awr-openapi"), token_key=b"\0" * 32,
                              bus_kind="local")


def live_openapi() -> dict[str, Any]:
    """固定设置下的沙盒 OpenAPI（无副作用；快照生成用，见模块说明）。"""
    return build_openapi(_fixed_app())


def missing_design_routes() -> list[tuple[str, str]]:
    """WP-01 设计表（`tools/contracts/sandbox_api.py`）中尚未挂载的 (method, 相对路径)。"""
    import sys

    tools = ROOT / "tools" / "contracts"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import sandbox_api  # type: ignore[import-not-found]

    have: set[tuple[str, str]] = set()
    for path, ops in live_openapi()["paths"].items():
        rel = path[len(PREFIX):] if path.startswith(PREFIX) else path
        have |= {(m, rel) for m in ops}
    return sorted(k for k in sandbox_api.route_keys() if k not in have)


class _LazyApp:
    """模块级 `app`：首次被 ASGI 服务器调用时才读取环境并构造应用（导入本模块没有副作用）。"""

    def __init__(self) -> None:
        self._app: FastAPI | None = None

    def get(self) -> FastAPI:
        if self._app is None:
            self._app = create_sandbox_app()
        return self._app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        await self.get()(scope, receive, send)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.get(), name)


app = _LazyApp()


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m awr.api.sandbox_main", description="sandbox-api（开发入口）")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--log-level", default="warning")
    a = ap.parse_args(argv)
    s = _default_settings()
    sbc = sandbox_from_env()
    uvicorn.run(create_sandbox_app(s, sb=sbc), host=a.host or s.bind, port=a.port or sbc.port, ws="websockets",
                ws_per_message_deflate=False, ws_max_size=262144, log_level=a.log_level, lifespan="on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
