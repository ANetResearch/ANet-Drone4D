"""Rerun catalog 访问：BN-57 `viewer_url` 与 rerun-proxy（AWR-17 §18.10.2、§18.10.3；AWR-05 §6.1.4、§7.11、ADR-166、ADR-173；
M27-FR-084、FR-155；API-AC-091、M27-AC-022；D3-AC-25"Rerun 数据经路径限定 Cookie `d3_rr` 访问，URL 中不含令牌"、D3-AC-51
"Rerun catalog 只能经 `rerun-proxy` 以短期令牌访问"）。

catalog 服务（`awr.bench.catalog.service`）只监听 dmax 内网、本身不鉴权；浏览器与原生 Viewer 只经 rerun-proxy 访问：

- **BN-57**（bench-api，`router`）：`GET /runs/{rid}/viewer_url?t_from=&t_to=&catalog=dist|eval` 以 `D3Catalog.viewer_url(strict=True)`
  生成 catalog 深链接（服务地址改写为 rerun-proxy 的 origin，链接不含任何凭据），签发 `aud = rerun` 的短期令牌（10 min，
  `scope{catalog, dataset, run_id}`），以 HttpOnly、Secure、SameSite=Strict、Max-Age=600 的路径限定 Cookie `d3_rr` 下发；
  评测 catalog 只对评测员及以上（403 `115`），未注册（含 test 划分的运行在分发 catalog 中不存在）为 404 `305`，`rerun_version`
  不一致为 409 `694`（`detail.migrate_hint`），catalog 服务不可达为 503 `213`。
- **rerun-proxy**（独立进程，`create_proxy_app`，`python -m awr.bench.api.rerun_proxy`，缺省 127.0.0.1:18652）：校验短期令牌
  （浏览器为 Cookie `d3_rr`，本机代理为 `Authorization: Bearer d3st_…`）的签名、`exp`、来源 bench token 未撤销、角色与 catalog
  范围，失败为 401 `302` 或 403 `115`；只转发只读 RPC（`READ_RPCS`，注册、写入与维护类一律 403 `115`，`why = read_only`）；
  去掉 Cookie 与 `Authorization` 后以 gRPC-web 转发到对应 catalog，响应流式回传、不缓存（`Cache-Control: no-store`）；
  每个请求与每次会话（同一短期令牌 `jti`）写审计：catalog、dataset、segment、RPC 与上下行字节量。

Cookie 路径（ADR-173 的落地，见 doc_notes）：Rerun 0.38 的 gRPC 客户端（Web Viewer 的 gRPC-web 与原生 tonic）只取 URL 的
origin，忽略路径前缀，RPC 一律发往 `/<package.Service>/<Method>`。因此 `d3_rr` 以同名、同值、两个路径下发：

| Cookie Path | 覆盖的请求 | 使用者 |
|---|---|---|
| `/rerun/` | `/rerun/catalog/{dist,eval}/<package.Service>/<Method>`（路径中显式给出 catalog） | 本机代理与支持前缀的客户端 |
| `/rerun.cloud.v1alpha1.RerunCloudService` | `/rerun.cloud.v1alpha1.RerunCloudService/<Method>`（catalog 取令牌 `scope.catalog`） | Web Viewer（经 `viewer.open(url)`） |

两者均不覆盖 `/api/**`、`/lab/**` 等其余路径（RFC 6265 §5.1.4 路径匹配，`cookie_path_matches`）。反向代理须把这两类路径都
转发到 rerun-proxy（F05）。原生 Viewer 使用 HTTP/2 gRPC，rerun-proxy 只服务 gRPC-web（HTTP/1.1）；`awr.bench.rerun.open` 的本机
代理负责注入令牌并以 gRPC-web 访问 `/rerun/catalog/<catalog>/…`。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from awr.api.problem import ApiProblem, problem, uuid7
from awr.bench.api.deps import Researcher
from awr.bench.auth import AuthError, BenchAuth, Principal
from awr.bench.settings import BenchSettings

__all__ = ["CATALOGS", "GRPC_SERVICE", "PROXY_PORT", "READ_RPCS", "RR_COOKIE", "RR_COOKIE_PATHS", "RR_TTL_S", "ProxySessions",
           "cookie_path_matches", "create_proxy_app", "main", "router"]

CATALOGS = ("dist", "eval")
RR_COOKIE = "d3_rr"
GRPC_SERVICE = "rerun.cloud.v1alpha1.RerunCloudService"          # Rerun 0.38 catalog（redap）服务
RR_COOKIE_PATHS = ("/rerun/", f"/{GRPC_SERVICE}")                 # 见模块说明"Cookie 路径"
RR_TTL_S = 600                                                     # d3_rr 与 aud = rerun 短期令牌 10 min（AWR-17 §18.10.2、§19）
PROXY_PORT = 18652                                                 # 127.0.0.1:18652（AWR-17 §19，经鉴权反向代理暴露在 ZeroTier 内网）
MAX_REQUEST_BYTES = 16 * 2 ** 20                                   # gRPC-web 请求只有一元与服务端流（请求体为单条消息）
# 只读 RPC（Web Viewer 与 D3Catalog 读取所需）；注册、写入、删除、维护与任务控制不经 rerun-proxy（注册作业在 dmax 内网直连服务）
READ_RPCS = frozenset({
    "Version", "WhoAmI", "FindEntries", "ReadDatasetEntry", "ReadTableEntry", "GetDatasetSchema", "GetDatasetManifestSchema",
    "GetSegmentTableSchema", "GetTableSchema", "ScanSegmentTable", "ScanDatasetManifest", "ScanTable", "QueryDataset", "FetchChunks",
    "GetRrdManifest", "GetAssetsForSegment", "WatchEvents",
})
_FWD_REQ = ("content-type", "accept", "x-grpc-web", "grpc-timeout", "grpc-encoding", "grpc-accept-encoding", "user-agent", "x-user-agent", "te")
_FWD_RESP = ("content-type", "grpc-status", "grpc-message", "grpc-encoding", "grpc-accept-encoding", "x-rerun-server-version")
_FWD_PREFIX = "x-rerun-"   # Rerun 的 gRPC 元数据（按条目寻址的 RPC 必需 x-rerun-entry-id、x-rerun-entry-name-bin；F05 部署实测）：双向转发
_GRPC_STATUS = {401: "16", 403: "7", 404: "5", 405: "12", 413: "8", 503: "14"}       # UNAUTHENTICATED、PERMISSION_DENIED……


def cookie_path_matches(cookie_path: str, request_path: str) -> bool:
    """RFC 6265 §5.1.4 路径匹配：浏览器只在请求路径与 Cookie Path 匹配时携带该 Cookie。"""
    if request_path == cookie_path:
        return True
    return request_path.startswith(cookie_path) and (cookie_path.endswith("/") or request_path[len(cookie_path)] == "/")


def _claims(token: str) -> dict:
    """已验签短期令牌的载荷（`jti`、`iat`、`exp`）；只在 `verify_short` 通过后调用。"""
    pb = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(pb + "=" * (-len(pb) % 4)))


def _origin(base: str) -> str:
    """`rerun+https://lab.example/any/path` → `rerun+https://lab.example`（Rerun 数据集 URI 只认 origin，路径前缀会使解析失败）。"""
    u = urlsplit(base)
    if not u.scheme or not u.netloc:
        raise ValueError(f"rerun proxy base must be an absolute URL: {base!r}")
    scheme = u.scheme if u.scheme.startswith("rerun") else f"rerun+{u.scheme}"
    return f"{scheme}://{u.netloc}"


def _http_url(url: str) -> str:
    """catalog 服务地址 `rerun+http://h:p`、`rerun+https://h:p`、`rerun://h:p` → gRPC-web 的 HTTP 基址。"""
    if url.startswith("rerun+"):
        return url[len("rerun+"):].rstrip("/")
    if url.startswith("rerun://"):
        return "https://" + url[len("rerun://"):].rstrip("/")
    return url.rstrip("/")


# ── BN-57 viewer_url（bench-api） ────────────────────────────────────────────────────────────────────────────────────

router = APIRouter()


def _proxy_base(request: Request) -> str:
    """rerun-proxy 的外部 origin：设置 `extra.rerun_proxy_base`、环境变量 `AWR_RERUN_PROXY_BASE`，缺省取请求的 Host
    （bench-api 与 rerun-proxy 经同一反向代理同源暴露，Cookie 因此随 Web Viewer 的同源请求发送）。"""
    s: BenchSettings = request.app.state.settings
    base = s.extra.get("rerun_proxy_base") or os.environ.get("AWR_RERUN_PROXY_BASE")
    if not base:
        host = request.headers.get("host") or request.url.netloc
        base = f"rerun+{'https' if s.secure_cookie else 'http'}://{host}"
    return _origin(base)


def _catalog(request: Request, catalog: str):
    """`D3Catalog`：测试与嵌入方可在 `app.state.rerun_catalogs[catalog]` 注入（D3Catalog 或 CatalogClient）；缺省按
    `<data_root>/rerun/<catalog>/service.json` 连接在线 catalog（按进程缓存）。服务未启动或依赖缺失为 503 `213`。"""
    st = request.app.state
    cache = getattr(st, "rerun_catalogs", None)
    if cache is None:
        cache = st.rerun_catalogs = {}
    obj = cache.get(catalog)
    try:
        from awr.bench.catalog import D3Catalog, live_client
    except ImportError:                                           # rerun-sdk / datafusion 只在 .venv-d3 与 d3-bench 镜像
        raise ApiProblem(213, detail={"service": f"catalog-{catalog}", "why": "rerun_sdk_missing"}) from None
    if obj is None:
        try:
            obj = live_client(st.settings.data_root / "rerun", catalog)
        except Exception:
            obj = None
        if obj is None:
            raise ApiProblem(213, detail={"service": f"catalog-{catalog}"})
    if not isinstance(obj, D3Catalog):
        obj = D3Catalog(client=obj, target=catalog)
    cache[catalog] = obj
    return obj


def _segment_range(cat, event: str, rid: str) -> tuple[int, int] | None:
    t = cat.runs(event)
    if "t_start_ns" not in t.column_names:
        return None
    for s, a, b in zip(t.column("segment").to_pylist(), t.column("t_start_ns").to_pylist(), t.column("t_end_ns").to_pylist(), strict=True):
        if s == rid and a is not None and b is not None:
            return int(a), int(b)
    return None


@router.get("/runs/{rid}/viewer_url")
def viewer_url(rid: str, request: Request, p: Researcher, t_from: float | None = Query(None, ge=0), t_to: float | None = Query(None, ge=0),
               catalog: Literal["dist", "eval"] = "dist") -> Response:
    """BN-57：catalog 深链接（不含凭据）与路径限定 Cookie `d3_rr`（AWR-17 §18.10.2）。"""
    if catalog == "eval" and not p.at_least("evaluator"):
        raise ApiProblem(115, detail={"required": "evaluator", "role": p.role, "why": "catalog", "catalog": catalog})
    if t_from is not None and t_to is not None and t_to < t_from:
        raise ApiProblem(300, status=400, detail={"errors": [{"loc": ["query", "t_to"], "msg": "t_to must be >= t_from"}]})
    cat = _catalog(request, catalog)                              # 依赖缺失或服务不可达：503 213
    from awr.bench.catalog import CatalogError, CatalogVersionError

    try:
        event = cat.event_of([rid])
        seg = _segment_range(cat, event, rid)
        window = None
        if t_from is not None or t_to is not None:
            lo = t_from if t_from is not None else (seg[0] / 1e9 if seg else 0.0)
            hi = t_to if t_to is not None else (seg[1] / 1e9 if seg else lo)
            if hi < lo:
                raise ApiProblem(300, status=400, detail={"errors": [{"loc": ["query", "t_from"], "msg": "t_from is after the end of the run"}]})
            window = (lo, hi)
        link = cat.viewer_url(rid, window, event=event, proxy_base=_proxy_base(request), strict=True)
    except CatalogVersionError as e:
        d = e.detail or {}
        raise ApiProblem(694, detail={"run": rid, "catalog": catalog, "found": d.get("found"), "expected": d.get("expected"),
                                      "migrate_hint": d.get("hint")}) from None
    except CatalogError:
        raise ApiProblem(305, detail={"run": rid, "catalog": catalog}) from None
    except ApiProblem:
        raise
    except Exception:                                             # catalog 服务中途不可达
        request.app.state.rerun_catalogs.pop(catalog, None)
        raise ApiProblem(213, detail={"service": f"catalog-{catalog}"}) from None
    auth: BenchAuth = request.app.state.auth
    tok, exp = auth.sign_short(p, "rerun", run_id=rid, scope_extra={"catalog": catalog, "dataset": link.dataset}, ttl_s=RR_TTL_S)
    rng = [round(window[0] * 1e9), round(window[1] * 1e9)] if window else ([seg[0], seg[1]] if seg else None)
    auth.audit.write("rerun_link", principal=p.name, role=p.role, via=p.via, token_id=p.token_id, catalog=catalog, dataset=link.dataset,
                     segment=rid, session=_claims(tok)["jti"], exp_unix=exp, request_id=getattr(request.state, "request_id", None))
    body = {"url": link.url, "recording_id": rid, "dataset": link.dataset, "timeline": "sim_time", "time_range_ns": rng, "catalog": catalog,
            "rerun_version": link.rerun_version, "cookie_exp_unix_ns": str(exp * 1_000_000_000), "migrate_hint": link.migrate_hint,
            "viewer": link.viewer}
    r = JSONResponse(body)
    for path in RR_COOKIE_PATHS:
        r.set_cookie(RR_COOKIE, tok, max_age=RR_TTL_S, path=path, secure=request.app.state.settings.secure_cookie, httponly=True,
                     samesite="strict")
    return r


# ── rerun-proxy（独立进程） ──────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class _Session:
    principal: str
    role: str
    catalog: str
    dataset: str | None
    segment: str | None
    exp: int
    requests: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    rpcs: dict[str, int] = field(default_factory=dict)
    first_unix_ns: int = 0
    last_unix_ns: int = 0


class ProxySessions:
    """按短期令牌 `jti` 汇总的访问会话（AWR-17 §18.10.3"审计：每次会话的 catalog、dataset、segment 与字节量"）。令牌到期后
    的下一次请求或进程停止时，每个会话写一条 `rerun_session` 审计。"""

    def __init__(self, auth: BenchAuth) -> None:
        self.auth = auth
        self._s: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def add(self, jti: str, p: Principal, catalog: str, exp: int, rpc: str, bytes_in: int, bytes_out: int) -> None:
        now = int(self.auth.clock() * 1e9)
        with self._lock:
            s = self._s.get(jti)
            if s is None:
                s = self._s[jti] = _Session(p.name, p.role, catalog, p.scope.get("dataset"), p.scope.get("run_id"), exp, first_unix_ns=now)
            s.requests += 1
            s.bytes_in += bytes_in
            s.bytes_out += bytes_out
            s.rpcs[rpc] = s.rpcs.get(rpc, 0) + 1
            s.last_unix_ns = now

    def get(self, jti: str) -> dict | None:
        with self._lock:
            s = self._s.get(jti)
            return None if s is None else {**s.__dict__, "rpcs": dict(s.rpcs)}

    def flush(self, *, expired_only: bool = True) -> int:
        t = self.auth.clock()
        with self._lock:
            done = [k for k, s in self._s.items() if not expired_only or s.exp <= t]
            out = [(k, self._s.pop(k)) for k in done]
        for k, s in out:
            self.auth.audit.write("rerun_session", session=k, principal=s.principal, role=s.role, catalog=s.catalog, dataset=s.dataset,
                                  segment=s.segment, requests=s.requests, bytes_in=s.bytes_in, bytes_out=s.bytes_out, rpcs=s.rpcs,
                                  first_unix_ns=str(s.first_unix_ns), last_unix_ns=str(s.last_unix_ns))
        return len(out)


class _Deny(Exception):
    def __init__(self, code: int, status: int, why: str, p: Principal | None = None, **detail) -> None:
        super().__init__(f"{code} {why}")
        self.code, self.status, self.why, self.p, self.detail = code, status, why, p, {"why": why, **detail}


def _upstreams(settings: BenchSettings, explicit: dict[str, str] | None) -> dict[str, str | None]:
    """catalog → gRPC-web 基址：显式参数、环境变量 `AWR_RERUN_CATALOG_{DIST,EVAL}_URL`、`<data_root>/rerun/<catalog>/service.json`。"""
    out: dict[str, str | None] = {}
    for c in CATALOGS:
        u = (explicit or {}).get(c) or os.environ.get(f"AWR_RERUN_CATALOG_{c.upper()}_URL")
        if not u:
            p = settings.data_root / "rerun" / c / "service.json"
            try:
                u = json.loads(p.read_text(encoding="utf-8")).get("url") if p.is_file() else None
            except (OSError, ValueError):
                u = None
        out[c] = _http_url(u) if u else None
    return out


def create_proxy_app(settings: BenchSettings | None = None, *, upstreams: dict[str, str] | None = None, auth: BenchAuth | None = None,
                     transport=None) -> FastAPI:
    """rerun-proxy 应用。`upstreams` 缺省按 `_upstreams` 解析（每次请求重读，catalog 服务重启换端口后无需重启代理）；
    `auth` 缺省与 bench-api 共用密钥目录与审计目录（会话库单独存放，代理不建立会话）；`transport` 供测试注入 httpx 传输。"""
    import httpx

    s = settings or BenchSettings.from_env()
    a = auth or BenchAuth(s.keys_dir, s.data_root / "rerun-proxy.sqlite", session_ttl_s=s.session_ttl_s, short_ttl_s=RR_TTL_S)
    clients: dict[int, httpx.AsyncClient] = {}

    def http() -> httpx.AsyncClient:
        k = id(asyncio.get_running_loop())
        c = clients.get(k)
        if c is None:
            c = clients[k] = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(connect=5.0, read=300.0, write=60.0, pool=10.0))
        return c

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            app.state.sessions.flush(expired_only=False)
            for c in list(clients.values()):
                await c.aclose()
            clients.clear()

    app = FastAPI(title="ANet4D rerun-proxy", version="1", openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = s
    app.state.auth = a
    app.state.sessions = ProxySessions(a)
    app.state.upstreams = upstreams

    def audit(request: Request, action: str, p: Principal | None, **fields) -> None:
        a.audit.write(action, principal=p.name if p else None, role=p.role if p else None, token_id=p.token_id if p else None,
                      method=request.method, route="rerun-proxy", path=request.url.path, client=request.client.host if request.client else None,
                      request_id=request.state.request_id, **fields)

    def deny(request: Request, e: _Deny, **fields) -> Response:
        audit(request, "deny", e.p, code=e.code, status=e.status, why=e.why, **fields)
        return problem(e.code, status=e.status, detail=e.detail, request_id=request.state.request_id,
                       headers={"grpc-status": _GRPC_STATUS.get(e.status, "2"), "grpc-message": e.why})

    def credential(request: Request) -> tuple[str, str]:
        h = request.headers.get("authorization", "")
        if h[:7].lower() == "bearer ":
            return h[7:].strip(), "bearer"
        c = request.cookies.get(RR_COOKIE)
        if c:
            return c, "cookie"
        raise _Deny(302, 401, "missing")

    def check(request: Request, catalog: str | None, rpc: str) -> tuple[Principal, dict, str, str]:
        tok, via = credential(request)
        if not tok.startswith("d3st_v1."):
            raise _Deny(302, 401, "not_short_token")
        try:
            p = a.verify_short(tok, "rerun")
        except AuthError as e:
            raise _Deny(e.code, 403 if e.code == 115 else 401, "invited_scope" if e.code == 115 else "token") from None
        if via == "cookie":                                        # 浏览器请求必带 Origin；跨站一律拒绝（SameSite=Strict 之外的第二道）
            o = request.headers.get("origin")
            if o is not None and o not in s.allowed_origins:
                raise _Deny(303, 403, "origin", p)
        scoped = p.scope.get("catalog")
        c = catalog or scoped or "dist"
        if c not in CATALOGS:
            raise _Deny(305, 404, "catalog_unknown", p, catalog=c)
        if scoped and scoped != c:
            raise _Deny(115, 403, "catalog_scope", p, catalog=c, scope=scoped)
        if c == "eval" and not p.at_least("evaluator"):
            raise _Deny(115, 403, "catalog", p, catalog=c, required="evaluator", role=p.role)
        if rpc not in READ_RPCS:
            raise _Deny(115, 403, "read_only", p, catalog=c, rpc=rpc)
        return p, _claims(tok), c, via

    async def forward(request: Request, catalog: str | None, service: str, rpc: str) -> Response:
        request.state.request_id = request.headers.get("x-request-id") or uuid7()
        app.state.sessions.flush()
        if request.method != "POST":
            return deny(request, _Deny(306, 405, "method"))
        if service != GRPC_SERVICE:
            return deny(request, _Deny(305, 404, "service", service=service))
        try:
            p, claims, c, via = check(request, catalog, rpc)
        except _Deny as e:
            return deny(request, e, rpc=rpc, catalog=catalog)
        base = _upstreams(s, app.state.upstreams).get(c)
        if not base:
            return deny(request, _Deny(213, 503, "upstream", p, catalog=c))
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            return deny(request, _Deny(307, 413, "request_too_large", p, catalog=c))
        hdrs = {k: v for k in _FWD_REQ if (v := request.headers.get(k)) is not None}     # 不转发 Cookie、Authorization、Origin、Host
        hdrs.update((k, v) for k, v in request.headers.items() if k.startswith(_FWD_PREFIX))
        cli = http()
        try:
            up = await cli.send(cli.build_request("POST", f"{base}/{service}/{rpc}", content=body, headers=hdrs), stream=True)
        except httpx.HTTPError:
            return deny(request, _Deny(213, 503, "upstream", p, catalog=c))
        out_h = {k: v for k in _FWD_RESP if (v := up.headers.get(k)) is not None}
        out_h.update((k, v) for k, v in up.headers.items() if k.startswith(_FWD_PREFIX))
        out_h["Cache-Control"] = "no-store"
        common = {"via": via, "session": claims.get("jti"), "catalog": c, "dataset": p.scope.get("dataset"), "segment": p.scope.get("run_id"),
                  "rpc": rpc}

        async def stream():
            n = 0
            try:
                async for chunk in up.aiter_bytes():               # gRPC-web 不用 HTTP 内容编码；若上游使用则在此解码、不转发该头
                    n += len(chunk)
                    yield chunk
            finally:
                await up.aclose()
                app.state.sessions.add(claims.get("jti", ""), p, c, int(claims.get("exp", 0)), rpc, len(body), n)
                audit(request, "rerun_proxy", p, status=up.status_code, grpc_status=up.headers.get("grpc-status"), bytes_in=len(body),
                      bytes_out=n, **common)

        return StreamingResponse(stream(), status_code=up.status_code, headers=out_h)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: 305, 405: 306}.get(exc.status_code, 300 if exc.status_code < 500 else 320)
        return problem(code, status=exc.status_code, headers={"grpc-status": _GRPC_STATUS.get(exc.status_code, "2"), "grpc-message": "route"})

    @app.get("/rerun/healthz")
    def healthz() -> dict:
        """部署探针（只回各 catalog 是否已配置上游，不含地址）。"""
        return {"ok": True, "catalogs": {c: bool(u) for c, u in _upstreams(s, app.state.upstreams).items()}}

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

    @app.api_route("/rerun/catalog/{catalog}/{service}/{rpc}", methods=methods)
    async def prefixed(catalog: str, service: str, rpc: str, request: Request) -> Response:
        """`/rerun/catalog/{dist,eval}/<package.Service>/<Method>`（AWR-17 §18.10.3 路径；本机代理）。"""
        return await forward(request, catalog, service, rpc)

    @app.api_route(f"/{GRPC_SERVICE}/{{rpc}}", methods=methods)
    async def rooted(rpc: str, request: Request) -> Response:
        """`/rerun.cloud.v1alpha1.RerunCloudService/<Method>`（Web Viewer；catalog 取令牌 `scope.catalog`）。"""
        return await forward(request, None, GRPC_SERVICE, rpc)

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m awr.bench.api.rerun_proxy")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int, default=PROXY_PORT)
    ns = ap.parse_args(argv)
    s = BenchSettings.from_env()
    uvicorn.run(create_proxy_app(s), host=ns.host or s.bind, port=ns.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
