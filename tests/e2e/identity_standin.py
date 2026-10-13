"""R79、R80 的测试替身（WP-13b，M16；17 §17.20.5；AWR-04 §15.9.2、§15.9.6）。

show-api 的 `POST /api/auth/identity`（R79）与 `DELETE /api/auth/identity`（R80）属 M11 的 WP-03b；在其交付之前，D2.1 的浏览器用例
与本机演练需要一个端点把口令交给身份库比对。本替身只是这两条路由的薄壳，比对、限流、三级锁定、固定时延、令牌签发、撤销表与
审计全部是产品库 `awr.sandbox.identity.IdentityService`（M17）本身，状态目录与池相同（`AWR_IDENTITY_STATE_DIR`），因此池对内部档
令牌的验签与撤销降档是真实路径。用例在 show-api 已提供 R79 时一律改用真实端点（`real_r79(show_base)`），替身只在缺失时启用并在
报告中注明。

- `IdentityStandin(env).start()`：在空闲端口启动（uvicorn，线程内），`url` 为根地址；`stop()` 停止。
- 审计写 `<state_dir>/standin-audit.jsonl`（与 D1 审计同形的一行一条：`{t_unix_ns, kind, detail}`），便于扫描口令与令牌。
- 请求体与头不进入任何日志（uvicorn 访问日志关闭）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

__all__ = ["IdentityStandin", "real_r79"]


def real_r79(show_base: str, timeout_s: float = 5.0) -> bool:
    """show-api 是否已提供 R79/R80：不带身份头的 DELETE（不触发比对、不计失败次数）缺失时为 404。"""
    import httpx

    try:
        r = httpx.delete(show_base.rstrip("/") + "/api/auth/identity", timeout=timeout_s)
    except httpx.HTTPError:
        return False
    return r.status_code != 404


class IdentityStandin:
    def __init__(self, env: dict[str, str], *, host: str = "127.0.0.1", port: int = 0) -> None:
        self.env = dict(env)
        self.host = host
        self.port = port
        self.url = ""
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self.svc: Any = None
        self.audit_path: Path | None = None

    def _app(self) -> FastAPI:
        from awr.sandbox.identity import IdentityInvalid, IdentityService, default_state_dir

        state = default_state_dir(self.env)
        self.audit_path = state / "standin-audit.jsonl"

        def audit(kind: str, detail: dict) -> None:
            assert self.audit_path is not None
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"t_unix_ns": str(time.time_ns()), "kind": kind, "detail": detail}) + "\n")

        self.svc = svc = IdentityService.from_env(self.env, state_dir=state, audit=audit)
        app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

        def problem(status: int, code: int, detail: dict | None, headers: dict[str, str] | None) -> JSONResponse:
            body: dict[str, Any] = {"type": f"urn:awr:reason:{code}", "status": status, "code": code}
            if detail:
                body["detail"] = detail
            return JSONResponse(body, status_code=status, headers=headers, media_type="application/problem+json")

        @app.post("/api/auth/identity")
        async def r79(request: Request) -> Response:
            try:
                body = await request.json()
            except ValueError:
                body = None
            if not isinstance(body, dict) or not isinstance(body.get("secret"), str):
                return problem(422, 110, {"field": "secret"}, None)
            # principal：真实 R79 按 D1 的 principal 规则由 principal_hint 推出；替身不复刻该规则，由身份库生成随机 principal
            addr = request.client.host if request.client else None
            v = await svc.acheck(body["secret"], addr, principal_id=None)
            body = None                                  # 口令不再被引用
            status, code, detail, headers = v.problem()
            if v.ok and v.token and v.payload:
                return JSONResponse({"identity_token": v.token, "exp_unix_ns": str(int(v.payload["exp"]) * 1_000_000_000), "tier": "internal"})
            return problem(status, code, detail, headers)

        @app.delete("/api/auth/identity")
        async def r80(request: Request) -> Response:
            tok = request.headers.get("x-awr-identity")
            if not tok:
                return problem(401, 301, None, None)
            try:
                svc.verify_token(tok)
            except IdentityInvalid as e:
                return problem(401, 302, {"why": f"identity_{e.why}"}, None)
            svc.revoke(tok)
            return Response(status_code=204)

        return app

    def start(self) -> IdentityStandin:
        import socket

        import uvicorn

        if not self.port:
            with socket.socket() as s:
                s.bind((self.host, 0))
                self.port = int(s.getsockname()[1])
        cfg = uvicorn.Config(self._app(), host=self.host, port=self.port, log_level="warning", access_log=False, lifespan="off")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, name="identity-standin", daemon=True)
        self._thread.start()
        end = time.monotonic() + 15.0
        while not self._server.started and time.monotonic() < end:
            time.sleep(0.05)
        if not self._server.started:
            raise RuntimeError("identity stand-in did not start")
        self.url = f"http://{self.host}:{self.port}"
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(10.0)
        if self.svc is not None:
            self.svc.flush(force=True)
