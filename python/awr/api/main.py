"""api 进程入口：FastAPI 应用、REST 路由自动发现、WS `/api/rt`、静态 World 与前端服务（M11 §6.1、§6.10；
M11-FR-001、FR-015、FR-020、FR-080；AWR-17 §4、§5、§6；AWR-19 §6.3）。

启动方式：
- supervisor：`uvicorn awr.api.main:app --loop uvloop --ws websockets …`（configs/runtime.yaml），环境变量由 supervisor 注入；
- 开发：`python -m awr.api.main [--host 127.0.0.1] [--port 8000]`（未受监管：进程内随机秘密与管理口令，总线默认 zenoh）；
- 进程内：`python -m awr.api.inproc`（LocalBus + LocalRing，sim-core 主循环在同一进程的线程里）。

路由顺序：`rest/*.py` 按文件名排序自动发现各模块导出的 `router`（包括 M04 的 `world_query`）→ WS `/api/rt` →
静态 `/worlds/**`、`/assets/**`、`/brand/**`、`/bench/**` → SPA 回退（`/`、`/world/:id` 等返回 `index.html`）。
`app.state`：`awr`（ApiContext）、`bus`（总线，R07 依赖）、`session_world_id`（当前运行世界）。
lifespan：受监管时先 `init_child("api")`（保存并恢复 uvicorn 已装的 SIGTERM/SIGINT 处理器，停机仍走 uvicorn 的优雅关闭）
→ 打开总线 → 读取世界摘要 → 启动 Gateway → 10 Hz 心跳 `hb.api` → `bus.ready()` 声明 `proc/api/ready`。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import logging
import pkgutil
import signal
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import anyio
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from awr.runtime.bus import LocalBus, open_bus
from awr.runtime.child import init_child
from awr.world.package.catalog import Catalog

from .audit import AuditWriter
from .middleware import AwrMiddleware
from .problem import ApiProblem, problem
from .ratelimit import RateLimiter
from .rt.gateway import Gateway
from .rt.inspect import router as inspect_router
from .rt.ws import rt_endpoint
from .security import TokenService
from .settings import ApiSettings
from .static import WorldFiles, build_static_router, spa_route

__all__ = ["ApiContext", "app", "create_app", "discover_routers"]

log = logging.getLogger("awr.api.main")

HB_PERIOD_S = 0.1


@dataclass
class ApiContext:
    settings: ApiSettings
    tokens: TokenService
    catalog: Catalog
    audit: AuditWriter = field(default_factory=lambda: AuditWriter(None))
    limiter: RateLimiter = field(default_factory=RateLimiter)
    world_files: WorldFiles | None = None
    bus: Any = None
    gateway: Gateway | None = None
    run_ctx: Any = None
    ring_cls: type | None = None
    external_bus: bool = False
    subscribe_events: bool = True
    started_mono: float = field(default_factory=time.monotonic)
    world_info: dict[str, Any] = field(default_factory=dict)
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _ready: Any = None

    async def start(self, app: FastAPI) -> None:
        s = self.settings
        loop = asyncio.get_running_loop()
        if s.supervised:
            saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            self.run_ctx = init_child("api")
            for sig, h in saved.items():
                with contextlib.suppress(TypeError, ValueError):
                    signal.signal(sig, h)
        if self.bus is None:
            bus_ctx = self.run_ctx
            if bus_ctx is None and s.zenoh_config is not None:  # 未受监管但给了会话配置（基准、调试）
                bus_ctx = SimpleNamespace(zenoh_config=s.zenoh_config, namespace=s.namespace)
            try:
                self.bus = open_bus("api", bus_ctx, kind=s.bus_kind, loop=loop, namespace=s.namespace)
            except Exception:
                log.exception("bus open failed; api continues without sim-core")
                self.bus = LocalBus.open("api", namespace=f"{s.namespace}/isolated", loop=loop)
        self.world_info = await anyio.to_thread.run_sync(self._world_info)
        kw: dict[str, Any] = {"world_info": self.world_info, "subscribe_events": self.subscribe_events,
                              "audit": self.audit, "limiter": self.limiter, "confirm_key": self.tokens.k_confirm}
        if self.ring_cls is not None:
            kw["ring_cls"] = self.ring_cls
        self.gateway = Gateway(s, self.bus, self.tokens, **kw)
        await self.gateway.start()
        self._install_stop_hook(loop)
        app.state.bus = self.bus
        app.state.session_world_id = s.world_id
        if self.run_ctx is not None:
            self._tasks.append(asyncio.ensure_future(self._heartbeat()))
        self._ready = self.bus.ready()
        log.info("api ready", extra={"kv": {"run_id": s.run_id, "world": s.world_id, "bus": s.bus_kind}})

    def _world_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"id": self.settings.world_id, "frame": "world", "contentVersion": None,
                                "coordinateSha256": None, "anchorKind": None, "georeferenced": None}
        try:
            d = self.catalog.get(self.settings.world_id)
        except Exception:
            log.exception("world catalog read failed")
            d = None
        if d is not None:
            info.update(contentVersion=d.content_version, coordinateSha256=d.coordinate_sha256,
                        anchorKind=d.anchor_kind, georeferenced=d.georeferenced)
        return info

    def _install_stop_hook(self, loop: asyncio.AbstractEventLoop) -> None:
        """FR-104：uvicorn 收到 SIGTERM/SIGINT 时会先以 1012 断开 WS 再执行 lifespan 关闭；这里包装已安装的信号处理器，
        先执行 Gateway.begin_stop()（213、`sys.shutting_down`、`status proc.api`、1001 关闭，≤ 1.5 s），再交还 uvicorn。"""
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            prev = signal.getsignal(sig)
            if not callable(prev):
                continue

            def handler(signum: int, frame: Any, prev: Any = prev) -> None:
                gw = self.gateway
                if gw is None or gw.stopping:
                    prev(signum, frame)
                    return

                async def pre_stop() -> None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(gw.begin_stop("sigterm" if signum == signal.SIGTERM else "sigint"), 1.5)
                    prev(signum, frame)

                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(pre_stop()))

            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)

    async def _heartbeat(self) -> None:
        hb = self.run_ctx.heartbeat_writer()
        while True:
            with contextlib.suppress(OSError):
                hb.beat()
            await asyncio.sleep(HB_PERIOD_S)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        if self.gateway is not None:
            await self.gateway.stop()
        if self._ready is not None:
            with contextlib.suppress(Exception):
                self._ready.close()
        if self.bus is not None and not self.external_bus:
            with contextlib.suppress(Exception):
                self.bus.close()
        await anyio.to_thread.run_sync(self.audit.close)


def discover_routers() -> list[tuple[str, APIRouter]]:
    """按文件名排序导入 `awr.api.rest` 下各模块并收集其 `router`（导入失败的模块记错误日志后跳过，不拖垮 api）。"""
    from . import rest as pkg

    out: list[tuple[str, APIRouter]] = []
    for mi in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if mi.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{pkg.__name__}.{mi.name}")
        except Exception:
            log.exception("rest module import failed", extra={"kv": {"module": mi.name}})
            continue
        r = getattr(mod, "router", None)
        if isinstance(r, APIRouter):
            out.append((mi.name, r))
    return out


def _install_handlers(app: FastAPI) -> None:
    def rid(request: Request) -> str | None:
        return getattr(request.state, "request_id", None)

    @app.exception_handler(ApiProblem)
    async def _problem(request: Request, exc: ApiProblem):
        return problem(exc.code, status=exc.status, detail=exc.detail, message=exc.message, request_id=rid(request),
                       headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        errs = [{"loc": [str(x) for x in e.get("loc", ())], "msg": str(e.get("msg", ""))} for e in exc.errors()[:20]]
        return problem(300, status=400, detail={"errors": errs}, request_id=rid(request))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: 305, 405: 306, 413: 307, 415: 308}.get(exc.status_code, 300 if exc.status_code < 500 else 320)
        return problem(code, status=exc.status_code, request_id=rid(request))


def _default_scenarios() -> dict[str, str]:
    """`scenarios/catalog.json` 的 `worlds.<id>.default`（M16-FR-002；16 §12.1；`AWR_SCENARIOS_DIR` 可覆盖）。
    INT-1：M16-to-M11 第 3 条，`GET /api/worlds` 的 `default_scenario_id` 此前恒为 null。"""
    import json
    import os
    from pathlib import Path

    root = Path(os.environ.get("AWR_SCENARIOS_DIR") or Path(__file__).resolve().parents[3] / "scenarios")
    try:
        doc = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        return {wid: str(v["default"]) for wid, v in (doc.get("worlds") or {}).items()
                if isinstance(v, dict) and isinstance(v.get("default"), str)}
    except (OSError, ValueError, TypeError):
        return {}


def create_app(settings: ApiSettings | None = None, *, bus: Any = None, ring_cls: type | None = None,
               subscribe_events: bool = True) -> FastAPI:
    s = settings if settings is not None else ApiSettings.from_env()
    tokens = TokenService(s)
    audit_dir = s.persist_dir if s.persist_dir is not None else s.run_dir
    ctx = ApiContext(settings=s, tokens=tokens, catalog=Catalog(s.worlds_dir, default_scenarios=_default_scenarios()),
                     bus=bus, ring_cls=ring_cls,
                     external_bus=bus is not None, subscribe_events=subscribe_events,
                     audit=AuditWriter(audit_dir / "audit.jsonl" if audit_dir is not None else None))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await ctx.start(app)
        try:
            yield
        finally:
            await ctx.stop()

    expose_openapi = s.profile not in ("demo", "public") and not s.is_public
    app = FastAPI(title="ANet Drone4D World Runtime API", version="1", lifespan=lifespan,
                  openapi_url="/api/openapi.json" if expose_openapi else None, docs_url=None, redoc_url=None)
    app.state.awr = ctx
    app.state.bus = None
    app.state.session_world_id = s.world_id
    _install_handlers(app)
    for _name, r in discover_routers():
        app.include_router(r)
    app.add_api_websocket_route("/api/rt", rt_endpoint)
    app.include_router(inspect_router)
    dist = s.web_dist if s.serve_web else None
    static_router, ctx.world_files = build_static_router(s.worlds_dir, dist)
    app.include_router(static_router)
    app.add_api_route("/{path:path}", spa_route(dist), methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                      include_in_schema=False)
    app.add_middleware(AwrMiddleware, ctx=ctx)
    return app


class _LazyApp:
    """模块级 `app`：首次被 ASGI 服务器调用时才读取环境并构造应用（导入本模块没有副作用，便于测试与 OpenAPI 导出）。"""

    def __init__(self) -> None:
        self._app: FastAPI | None = None

    def get(self) -> FastAPI:
        if self._app is None:
            self._app = create_app()
        return self._app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        await self.get()(scope, receive, send)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.get(), name)


app = _LazyApp()


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m awr.api.main", description="AWR api 进程（开发入口）")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--log-level", default="warning")
    a = ap.parse_args(argv)
    s = ApiSettings.from_env()
    uvicorn.run(create_app(s), host=a.host or s.bind, port=a.port, ws="websockets", ws_per_message_deflate=False,
                ws_max_size=262144, log_level=a.log_level, lifespan="on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
