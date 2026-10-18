"""bench-api 应用（AWR-17 §18.4 通用规则）：前缀 `/api/bench/v1`、problem+json、`AWR-API-Version` 与 `AWR-Bench-API` 响应头。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from awr.api.problem import ApiProblem, problem, uuid7
from awr.bench.api import board, frames, session, submissions
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
    for r in (session.router, frames.router, board.router, submissions.router):
        app.include_router(r, prefix=PREFIX)

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
