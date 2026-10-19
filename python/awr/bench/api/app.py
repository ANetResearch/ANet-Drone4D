"""bench-api 应用（AWR-17 §18.4 通用规则）：前缀 `/api/bench/v1`、problem+json、`AWR-API-Version` 与 `AWR-Bench-API` 响应头。

路由：会话（BN-00 至 BN-04）、世界与事件只读（BN-10、11、20、22，`worlds`）、运行列表、状态、取消与重试（BN-42 至 BN-45，`runs`）、
帧与真值（BN-49 至 BN-52，`frames`）、Rerun catalog 深链接（BN-57，`rerun_proxy`）、评测与排行榜（`board`）、内部提交（`submissions`）。Run 状态库（`RunStore`）与鉴权共用
`bench.sqlite`，在首次请求时打开。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from awr.api.problem import ApiProblem, problem, uuid7
from awr.bench.api import board, frames, rerun_proxy, runs, session, submissions, worlds
from awr.bench.api.deps import annotate
from awr.bench.api.frames import StoreCache
from awr.bench.auth import BenchAuth
from awr.bench.settings import BenchSettings

__all__ = ["PREFIX", "create_bench_app"]

PREFIX = "/api/bench/v1"


def create_bench_app(settings: BenchSettings) -> FastAPI:
    app = FastAPI(title="drone4d bench-api", version="1", openapi_url=f"{PREFIX}/openapi.json", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.auth = BenchAuth(settings.keys_dir, settings.db_path, session_ttl_s=settings.session_ttl_s, short_ttl_s=settings.short_ttl_s)
    app.state.frame_stores = StoreCache()
    for r in (session.router, worlds.router, runs.router, frames.router, rerun_proxy.router, board.router, submissions.router):
        app.include_router(r, prefix=PREFIX)
    annotate(app, PREFIX)                                        # OpenAPI x-d3-role、x-d3-stage、x-awr-reasons（D3-AC-51）

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or uuid7()
        resp = await call_next(request)
        resp.headers["AWR-API-Version"] = "1"
        resp.headers["AWR-Bench-API"] = "1"
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def rid(request: Request) -> str | None:
        return getattr(request.state, "request_id", None)

    @app.exception_handler(ApiProblem)
    async def _problem(request: Request, exc: ApiProblem):
        return problem(exc.code, status=exc.status, detail=exc.detail, message=exc.message, request_id=rid(request), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        errs = [{"loc": [str(x) for x in e.get("loc", ())], "msg": str(e.get("msg", ""))} for e in exc.errors()[:20]]
        return problem(300, status=400, detail={"errors": errs}, request_id=rid(request))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: 305, 405: 306, 413: 307, 415: 308}.get(exc.status_code, 300 if exc.status_code < 500 else 320)
        return problem(code, status=exc.status_code, request_id=rid(request))

    return app
