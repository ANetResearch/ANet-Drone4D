"""rerun-proxy 与路径限定 Cookie `d3_rr`（AWR-17 §18.10.2、§18.10.3；AWR-05 §6.1.4、§7.11、ADR-173；M27-FR-084、FR-155；
API-AC-091、M27-AC-022；D3-AC-25"Rerun 数据经路径限定 Cookie `d3_rr` 访问，URL 中不含令牌"、D3-AC-51"Rerun catalog 只能经
`rerun-proxy` 以短期令牌访问"）。

- BN-57 `viewer_url`：链接不含凭据、只含 rerun-proxy 的 origin；`d3_rr` 为 HttpOnly、SameSite=Strict、Max-Age=600（Secure 随部署设置），
  路径限定为 `/rerun/` 与 `/rerun.cloud.v1alpha1.RerunCloudService`，令牌 `aud = rerun`、10 min；研究者请求评测 catalog 403 `115`
  并审计；未注册 404 `305`；版本不一致 409 `694` 与迁移提示；catalog 服务不可达 503 `213`；
- rerun-proxy：无 Cookie、过期、撤销、错误 aud 与 bench token 一律 401 `302`；研究者访问评测 catalog、令牌 catalog 范围不符、
  写类 RPC 403 `115`；跨站 Origin 403 `303`；拒绝不触达上游且写 `deny` 审计；转发时去掉 Cookie、Authorization 与 Origin，
  响应不缓存；每个请求与每次会话写 catalog、dataset、segment 与字节量，审计中无令牌明文；
- 端到端（真实 Rerun 0.38 catalog 服务，gRPC-web）：经 Cookie 的 Web Viewer 路径与经 Bearer 的 `/rerun/catalog/<catalog>/…`
  路径都能读取 catalog，分发与评测按令牌范围路由到各自的服务。
"""

from __future__ import annotations

import http.cookies
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import httpx
from fastapi.testclient import TestClient

from awr.api.bench_main import create_app
from awr.bench.api.rerun_proxy import (
    GRPC_SERVICE,
    READ_RPCS,
    RR_COOKIE,
    RR_COOKIE_PATHS,
    RR_TTL_S,
    cookie_path_matches,
    create_proxy_app,
)
from awr.bench.auth import AuthError, BenchAuth
from awr.bench.settings import BenchSettings

ROOT = Path(__file__).resolve().parents[3]
P = "/api/bench/v1"
ORIGIN = "https://lab.example"
EMPTY = b"\x00\x00\x00\x00\x00"                       # gRPC-web 帧：未压缩、长度 0 的空消息
GRPC_WEB = {"content-type": "application/grpc-web+proto", "x-grpc-web": "1"}
ROLES = {"invited": ("ivan", ("team-x",)), "researcher": ("alice", ("d3",)), "evaluator": ("eve", ()), "admin": ("root", ())}
WRITE_RPCS = ("CreateDatasetEntry", "RegisterWithDataset", "UnregisterFromDataset", "DeleteEntry", "WriteChunks", "WriteTable",
              "UpdateDatasetEntry", "DoMaintenance", "DoGlobalMaintenance", "RegisterTable", "CancelTasks", "GetWriteAccessGrant")


def _rpc(name: str) -> str:
    return f"/{GRPC_SERVICE}/{name}"


def _set_cookies(r) -> list[http.cookies.Morsel]:
    out = []
    for h in r.headers.get_list("set-cookie"):
        c = http.cookies.SimpleCookie()
        c.load(h)
        out.extend(c.values())
    return out


class Env:
    """bench-api 与 rerun-proxy 共用数据根（密钥目录与审计目录）；上游为记录请求的 MockTransport。"""

    def __init__(self, tmp: Path, *, upstream=None, proxy_clock=None, **kw) -> None:
        self.settings = BenchSettings(data_root=tmp / "d3", allowed_origins=(ORIGIN,), secure_cookie=kw.pop("secure_cookie", False), **kw)
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.auth: BenchAuth = self.app.state.auth
        self.tokens, self.ids = {}, {}
        for role, (name, projects) in ROLES.items():
            self.ids[role], self.tokens[role] = self.auth.issue_token(name, role, projects)
        self.seen: list[httpx.Request] = []
        self.upstream = upstream or self._echo
        proxy_auth = None
        if proxy_clock is not None:
            proxy_auth = BenchAuth(self.settings.keys_dir, tmp / "proxy.sqlite", clock=proxy_clock)
        self.proxy = create_proxy_app(self.settings, upstreams={"dist": "rerun+http://catalog-dist:51001", "eval": "rerun+http://catalog-eval:51002"},
                                      auth=proxy_auth, transport=httpx.MockTransport(self._handle))

    def _echo(self, req: httpx.Request) -> httpx.Response:
        payload = json.dumps({"host": req.url.host, "path": req.url.path}).encode()
        return httpx.Response(200, content=b"\x00" + len(payload).to_bytes(4, "big") + payload + b"\x80\x00\x00\x00\x0fgrpc-status:0\r\n",
                              headers={"content-type": "application/grpc-web+proto", "grpc-status": "0", "x-rerun-server-version": "rerun-oss/0.38.1",
                                       "access-control-allow-credentials": "true", "access-control-allow-origin": "*"})

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        return self.upstream(req)

    def principal(self, role: str):
        return self.auth.verify_token(self.tokens[role])

    def rr_token(self, role: str, *, catalog: str | None = "dist", now: int | None = None, aud: str = "rerun", run_id: str = "run-0001") -> str:
        extra = {"dataset": "campus-v2.handoff-01", **({"catalog": catalog} if catalog else {})}
        return self.auth.sign_short(self.principal(role), aud, run_id=run_id, now=now, scope_extra=extra)[0]

    def audit(self) -> list[dict]:
        return self.auth.audit.read()

    def audit_text(self) -> str:
        return "".join(p.read_text(encoding="utf-8") for p in sorted((self.settings.data_root / "audit").glob("*.jsonl")))


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ── Cookie 路径 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_cookie_paths_cover_rerun_rpcs_only() -> None:
    """RFC 6265 路径匹配：`d3_rr` 只随 Rerun RPC 发送，不随 bench-api、lab 页面与网关请求发送。"""
    assert RR_COOKIE == "d3_rr" and RR_TTL_S == 600 and RR_COOKIE_PATHS[0] == "/rerun/"
    hit = [_rpc(m) for m in sorted(READ_RPCS)] + [f"/rerun/catalog/{c}/{GRPC_SERVICE}/FindEntries" for c in ("dist", "eval")]
    miss = ["/", "/api/bench/v1/runs/run-0001/viewer_url", "/api/bench/v1/session", "/lab/inspect", "/rerun", "/rerunx/a", "/ws",
            "/rerun.sdk_comms.v1alpha1.MessageProxyService/ReadMessages", f"/{GRPC_SERVICE}x/FindEntries", "/static/app.js"]
    for path in hit:
        assert any(cookie_path_matches(cp, path) for cp in RR_COOKIE_PATHS), path
    for path in miss:
        assert not any(cookie_path_matches(cp, path) for cp in RR_COOKIE_PATHS), path


# ── rerun-proxy：鉴权与范围（上游为 MockTransport） ─────────────────────────────────────────────────────────────────

def _post(pc: TestClient, path: str, *, cookie: str | None = None, bearer: str | None = None, origin: str | None = ORIGIN, method: str = "POST"):
    h = dict(GRPC_WEB)
    if cookie is not None:
        h["cookie"] = f"{RR_COOKIE}={cookie}"
    if bearer is not None:
        h["authorization"] = f"Bearer {bearer}"
    if origin is not None:
        h["origin"] = origin
    return pc.request(method, path, content=EMPTY if method == "POST" else None, headers=h)


def _denied(r, status: int, code: int, why: str | None = None) -> None:
    assert r.status_code == status, r.text
    b = r.json()
    assert b["code"] == code, b
    if why is not None:
        assert b["detail"]["why"] == why, b
    assert r.headers["grpc-status"] in {"16", "7", "5", "12", "14", "8"}       # gRPC-web 客户端可读的状态


def test_proxy_rejects_missing_expired_revoked_and_foreign_tokens(env: Env) -> None:
    now = int(time.time())
    with TestClient(env.proxy) as pc:
        _denied(_post(pc, _rpc("FindEntries")), 401, 302, "missing")
        _denied(_post(pc, _rpc("FindEntries"), cookie=env.rr_token("researcher", now=now - RR_TTL_S - 1)), 401, 302, "token")   # 过期
        _denied(_post(pc, _rpc("FindEntries"), cookie=env.rr_token("researcher", aud="frames")), 401, 302, "token")             # 其他 aud
        _denied(_post(pc, _rpc("FindEntries"), bearer=env.tokens["admin"]), 401, 302, "not_short_token")                        # bench token
        _denied(_post(pc, _rpc("FindEntries"), cookie="d3st_v1.e30.AAAA"), 401, 302, "token")                                   # 伪造签名
        tok = env.rr_token("evaluator")
        assert _post(pc, _rpc("FindEntries"), cookie=tok).status_code == 200
        env.auth.revoke_token(env.ids["evaluator"])                                                                             # 撤销即时生效
        _denied(_post(pc, _rpc("FindEntries"), cookie=tok), 401, 302, "token")
    assert len(env.seen) == 1                                                                                                   # 拒绝不触达上游
    with pytest.raises(AuthError) as ei:                                                                                        # 受邀成员不签发
        env.auth.sign_short(env.principal("invited"), "rerun")
    assert ei.value.code == 115


def test_proxy_catalog_scope_by_role(env: Env) -> None:
    """评测 catalog 只对评测员及以上；令牌的 catalog 范围与请求路径不一致时拒绝；Web Viewer 路径按令牌范围路由。"""
    with TestClient(env.proxy) as pc:
        r_tok = env.rr_token("researcher", catalog="dist")
        _denied(_post(pc, f"/rerun/catalog/eval/{GRPC_SERVICE}/FindEntries", cookie=r_tok), 403, 115, "catalog_scope")
        _denied(_post(pc, f"/rerun/catalog/eval/{GRPC_SERVICE}/FindEntries", bearer=env.rr_token("researcher", catalog=None)), 403, 115, "catalog")
        forged_scope = env.rr_token("researcher", catalog="eval")                     # 签发端不会给研究者签 eval；校验端仍按角色拒绝
        _denied(_post(pc, _rpc("FindEntries"), cookie=forged_scope), 403, 115, "catalog")
        e_dist = env.rr_token("evaluator", catalog="dist")
        _denied(_post(pc, f"/rerun/catalog/eval/{GRPC_SERVICE}/FindEntries", cookie=e_dist), 403, 115, "catalog_scope")
        _denied(_post(pc, f"/rerun/catalog/other/{GRPC_SERVICE}/FindEntries", bearer=env.rr_token("admin", catalog=None)), 404, 305, "catalog_unknown")
        assert len(env.seen) == 0
        for role, cat, path, host in (("researcher", "dist", _rpc("FindEntries"), "catalog-dist"),
                                      ("evaluator", "eval", _rpc("FindEntries"), "catalog-eval"),
                                      ("admin", "eval", f"/rerun/catalog/eval/{GRPC_SERVICE}/QueryDataset", "catalog-eval"),
                                      ("researcher", None, f"/rerun/catalog/dist/{GRPC_SERVICE}/FetchChunks", "catalog-dist"),
                                      ("researcher", None, _rpc("Version"), "catalog-dist")):
            r = _post(pc, path, bearer=env.rr_token(role, catalog=cat), origin=None)
            assert r.status_code == 200, (role, path, r.text)
            assert env.seen[-1].url.host == host and env.seen[-1].url.path == _rpc(path.rsplit("/", 1)[1]), (role, path)
    deny = [a for a in env.audit() if a["action"] == "deny"]
    assert len(deny) == 5 and {a["why"] for a in deny} == {"catalog_scope", "catalog", "catalog_unknown"}
    assert all(a["route"] == "rerun-proxy" and a["principal"] in ("alice", "eve", "root") for a in deny)


def test_proxy_is_read_only_and_checks_origin(env: Env) -> None:
    with TestClient(env.proxy) as pc:
        tok = env.rr_token("admin", catalog="eval")
        for rpc in WRITE_RPCS:
            assert rpc not in READ_RPCS
            _denied(_post(pc, _rpc(rpc), cookie=tok), 403, 115, "read_only")
        _denied(_post(pc, _rpc("FindEntries"), cookie=tok, origin="https://evil.example"), 403, 303, "origin")
        _denied(_post(pc, "/rerun.sdk_comms.v1alpha1.MessageProxyService/WriteMessages", cookie=tok), 404, 305)
        _denied(_post(pc, "/rerun/catalog/eval/rerun.sdk_comms.v1alpha1.MessageProxyService/ReadMessages", cookie=tok), 404, 305, "service")
        _denied(_post(pc, _rpc("FindEntries"), cookie=tok, method="GET"), 405, 306, "method")
        assert _post(pc, _rpc("FindEntries"), cookie=tok, origin=None).status_code == 200          # 无 Origin（同源导航外的本机客户端）
    assert len(env.seen) == 1
    assert sum(a["action"] == "deny" for a in env.audit()) == len(WRITE_RPCS) + 3                 # 404 未知服务（未登记路由）不经处理器


def test_proxy_forwarding_hygiene_audit_and_sessions(tmp_path: Path) -> None:
    """转发去掉凭据；响应流式、不缓存；逐请求与逐会话审计字节量；审计无令牌明文。"""
    t = [float(int(time.time()))]
    env = Env(tmp_path, proxy_clock=lambda: t[0])
    tok = env.rr_token("researcher", now=int(t[0]))
    with TestClient(env.proxy) as pc:
        r1 = _post(pc, _rpc("FindEntries"), cookie=tok, bearer=None)
        r2 = pc.post(_rpc("QueryDataset"), content=b"\x00\x00\x00\x00\x03abc", headers={**GRPC_WEB, "cookie": f"{RR_COOKIE}={tok}; d3_session=sess-x",
                                                                                       "origin": ORIGIN, "grpc-timeout": "10S"})
        assert r1.status_code == r2.status_code == 200
        for r in (r1, r2):
            assert r.headers["cache-control"] == "no-store" and r.headers["grpc-status"] == "0"
            assert r.headers["content-type"] == "application/grpc-web+proto" and "access-control-allow-origin" not in r.headers
        up = env.seen[-1]
        assert up.url.path == _rpc("QueryDataset") and up.content == b"\x00\x00\x00\x00\x03abc" and up.headers["grpc-timeout"] == "10S"
        for h in ("cookie", "authorization", "origin"):
            assert all(h not in s.headers for s in env.seen), h
        recs = [a for a in env.audit() if a["action"] == "rerun_proxy"]
        assert [(a["rpc"], a["catalog"], a["dataset"], a["segment"], a["principal"], a["via"], a["status"]) for a in recs] == [
            ("FindEntries", "dist", "campus-v2.handoff-01", "run-0001", "alice", "cookie", 200),
            ("QueryDataset", "dist", "campus-v2.handoff-01", "run-0001", "alice", "cookie", 200)]
        assert recs[0]["bytes_out"] == len(r1.content) and recs[1]["bytes_in"] == 8 and recs[0]["session"] == recs[1]["session"]
        sid = recs[0]["session"]
        assert env.proxy.state.sessions.get(sid)["requests"] == 2
        t[0] += RR_TTL_S + 1                                                     # 令牌到期：下一次请求时写会话汇总
        _post(pc, _rpc("Version"), bearer=env.rr_token("admin", catalog="eval", now=int(t[0])), origin=None)
        sess = [a for a in env.audit() if a["action"] == "rerun_session"]
        assert len(sess) == 1 and sess[0]["session"] == sid and sess[0]["requests"] == 2 and sess[0]["catalog"] == "dist"
        assert sess[0]["bytes_out"] == len(r1.content) + len(r2.content) and sess[0]["rpcs"] == {"FindEntries": 1, "QueryDataset": 1}
    sess = [a for a in env.audit() if a["action"] == "rerun_session"]
    assert len(sess) == 2 and sess[1]["principal"] == "root" and sess[1]["catalog"] == "eval"          # 进程停止时写其余会话
    text = env.audit_text()
    assert tok not in text and "d3st_" not in text and "sess-x" not in text and env.tokens["researcher"] not in text


def test_proxy_upstream_unavailable(tmp_path: Path) -> None:
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    env = Env(tmp_path, upstream=down)
    with TestClient(env.proxy) as pc:
        _denied(_post(pc, _rpc("FindEntries"), cookie=env.rr_token("researcher")), 503, 213, "upstream")
    bare = create_proxy_app(env.settings)                                        # 未配置上游（无 service.json、无环境变量）
    with TestClient(bare) as pc:
        assert pc.get("/rerun/healthz").json() == {"ok": True, "catalogs": {"dist": False, "eval": False}}
        _denied(_post(pc, _rpc("FindEntries"), cookie=env.rr_token("researcher")), 503, 213, "upstream")


# ── BN-57 viewer_url 与端到端（真实 Rerun catalog 服务） ────────────────────────────────────────────────────────────

def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def catalogs(tmp_path_factory):
    """评测与分发两个在线 catalog：run-0001（两者都有）、run-old（rerun_version 0.37.2）；评测 catalog 另有 dataset
    `campus-v2.evalonly-01`（只在评测 catalog）。"""
    for m in ("rerun", "datafusion", "mcap_protobuf", "foxglove_schemas_protobuf", "zstandard"):
        pytest.importorskip(m)
    tc = _load("d3_rerun_catalog_tests_for_proxy", ROOT / "tests" / "d3" / "rerun" / "test_catalog.py")
    import rerun as rr

    from awr.bench.catalog.registry import register_run
    from awr.bench.catalog.service import CatalogService
    from awr.bench.recorder import rerun_export as rx

    base = tmp_path_factory.mktemp("rrcat")
    root = base / "rerun"
    svc = {c: CatalogService(root, c).start() for c in ("eval", "dist")}
    for rid, ver in (("run-0001", None), ("run-old", "0.37.2")):
        d = base / rid
        ex = tc._export_json(d, rid, {"obs.uav-01": tc._obs_rrd(d / "obs.uav-01.rrd", rid, rerun_version=ver)})
        for c in ("eval", "dist"):
            register_run(ex, catalog=c, root=root, client=svc[c].client())
    d = base / "run-e001"                                                         # 只注册到评测 catalog 的事件
    app_id = "drone4d-d3-campus-v2-evalonly-01"
    w = rx.LayerWriter(d / "obs.uav-01.rrd", app_id=app_id, recording_id="run-e001", layer="obs",
                       properties={"run_id": "run-e001", "layer": "obs", "uav": "uav-01", "rerun_version": rx.sdk_version()}, timebase=rx.Timebase(250))
    w.static("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP)
    for k in range(10):
        w.transform("/fleet/uav-01", k * 100_000_000, [float(k), 0.0, 10.0], [0, 0, 0, 1])
    w.close()
    ex = tc._export_json(d, "run-e001", {"obs.uav-01": d / "obs.uav-01.rrd"})
    man = json.loads(ex.read_text(encoding="utf-8"))
    ex.write_text(json.dumps({**man, "application_id": app_id, "event": "evalonly.01"}), encoding="utf-8")
    register_run(ex, catalog="eval", root=root, client=svc["eval"].client())
    yield {"svc": svc, "root": root}
    for s in svc.values():
        s.shutdown()


def _wire(env: Env, catalogs: dict) -> None:
    env.app.state.rerun_catalogs = {c: catalogs["svc"][c].client() for c in ("eval", "dist")}
    env.proxy.state.upstreams = {c: catalogs["svc"][c].url for c in ("eval", "dist")}


def test_viewer_url_cookie_and_link(tmp_path: Path, catalogs) -> None:
    """链接只含 rerun-proxy origin 与 segment，不含令牌；Cookie 属性与路径；令牌 aud = rerun、10 min、范围含 catalog 与 dataset。"""
    env = Env(tmp_path, secure_cookie=True, extra={"rerun_proxy_base": "https://lab.example/rerun/"})
    _wire(env, catalogs)
    r = env.client.get(f"{P}/runs/run-0001/viewer_url", params={"t_from": 0.5, "t_to": 1.5}, headers={"Authorization": f"Bearer {env.tokens['researcher']}"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["url"].startswith("rerun+https://lab.example/dataset/") and "segment_id=run-0001" in b["url"]
    assert b["recording_id"] == "run-0001" and b["dataset"] == "campus-v2.handoff-01" and b["catalog"] == "dist" and b["timeline"] == "sim_time"
    assert b["time_range_ns"] == [500_000_000, 1_500_000_000] and b["migrate_hint"] is None and b["viewer"]["memory_limit"] == "1.5GB"
    cookies = _set_cookies(r)
    assert sorted(m["path"] for m in cookies) == sorted(RR_COOKIE_PATHS)
    tok = cookies[0].value
    assert {m.key for m in cookies} == {RR_COOKIE} and {m.value for m in cookies} == {tok}
    for m in cookies:
        assert m["httponly"] and m["secure"] and m["samesite"].lower() == "strict" and m["max-age"] == str(RR_TTL_S)
    for s in (b["url"], json.dumps(b)):
        assert tok not in s and "d3st_" not in s and "token" not in s.lower() and env.tokens["researcher"] not in s
    p = env.auth.verify_short(tok, "rerun")
    assert (p.name, p.role, p.scope["catalog"], p.scope["dataset"], p.scope["run_id"]) == ("alice", "researcher", "dist", "campus-v2.handoff-01", "run-0001")
    exp_ns = int(b["cookie_exp_unix_ns"])
    assert isinstance(b["cookie_exp_unix_ns"], str) and abs(exp_ns / 1e9 - time.time() - RR_TTL_S) < 5
    full = env.client.get(f"{P}/runs/run-0001/viewer_url", headers={"Authorization": f"Bearer {env.tokens['researcher']}"}).json()
    assert full["time_range_ns"] == [0, 1_900_000_000]                           # 省略时间窗：整段（_obs_rrd 为 0–1.9 s，10 Hz）
    link = [a for a in env.audit() if a["action"] == "rerun_link"]
    assert len(link) == 2 and link[0]["catalog"] == "dist" and link[0]["segment"] == "run-0001" and tok not in env.audit_text()


def test_viewer_url_cookie_jar_sends_only_to_rerun_paths(tmp_path: Path, catalogs) -> None:
    """会话 Cookie 通道（工作区网页）：Cookie 罐只在 Rerun RPC 路径上携带 `d3_rr`。"""
    env = Env(tmp_path)
    _wire(env, catalogs)
    c = TestClient(env.app)
    assert c.post(f"{P}/session", json={"token": env.tokens["researcher"]}).status_code == 204
    assert c.get(f"{P}/runs/run-0001/viewer_url").status_code == 200
    def sent(path: str) -> bool:
        return f"{RR_COOKIE}=" in (c.build_request("POST", path).headers.get("cookie") or "")
    assert sent(_rpc("FetchChunks")) and sent(f"/rerun/catalog/dist/{GRPC_SERVICE}/FindEntries")
    assert not sent(f"{P}/runs") and not sent("/lab/inspect") and not sent("/")


def test_viewer_url_roles_and_errors(tmp_path: Path, catalogs) -> None:
    env = Env(tmp_path)
    _wire(env, catalogs)

    def get(role: str | None, rid: str = "run-0001", **params):
        h = {"Authorization": f"Bearer {env.tokens[role]}"} if role else {}
        return env.client.get(f"{P}/runs/{rid}/viewer_url", params=params, headers=h)

    assert get(None).status_code == 401 and get(None).json()["code"] == 301
    r = get("invited")
    assert r.status_code == 403 and r.json()["code"] == 115 and r.json()["detail"] == {"required": "researcher", "role": "invited"}
    before = len(env.audit())
    r = get("researcher", catalog="eval")
    assert r.status_code == 403 and r.json()["code"] == 115 and r.json()["detail"]["why"] == "catalog" and "set-cookie" not in r.headers
    deny = [a for a in env.audit()[before:] if a["action"] == "deny"]
    assert [(a["code"], a["principal"], a["why"]) for a in deny] == [(115, "alice", "catalog")]
    for role in ("evaluator", "admin"):
        r = get(role, catalog="eval")
        assert r.status_code == 200 and r.json()["catalog"] == "eval" and _set_cookies(r)
        assert env.auth.verify_short(_set_cookies(r)[0].value, "rerun").scope["catalog"] == "eval"
    assert get("evaluator", "run-e001", catalog="eval").json()["dataset"] == "campus-v2.evalonly-01"
    r = get("researcher", "run-e001")                                            # 只在评测 catalog（如 test 划分）：分发 catalog 404
    assert (r.status_code, r.json()["code"]) == (404, 305) and "set-cookie" not in r.headers
    r = get("researcher", "run-9999")
    assert (r.status_code, r.json()["code"]) == (404, 305)
    r = get("researcher", "run-old")
    assert (r.status_code, r.json()["code"]) == (409, 694) and "set-cookie" not in r.headers
    assert "rerun rrd migrate" in r.json()["detail"]["migrate_hint"] and r.json()["detail"]["found"] == "0.37.2"
    for bad in ({"t_from": 2.0, "t_to": 1.0}, {"catalog": "truth"}, {"t_from": -1}):
        r = get("researcher", **bad)
        assert (r.status_code, r.json()["code"]) == (400, 300), bad
    bare = Env(tmp_path / "bare")                                                # catalog 服务未启动（无 service.json）
    r = bare.client.get(f"{P}/runs/run-0001/viewer_url", headers={"Authorization": f"Bearer {bare.tokens['researcher']}"})
    assert (r.status_code, r.json()["code"]) == (503, 213) and "set-cookie" not in r.headers


def test_end_to_end_viewer_cookie_through_proxy(tmp_path: Path, catalogs) -> None:
    """viewer_url → Cookie → rerun-proxy → 真实 catalog 服务（gRPC-web）；分发与评测按令牌范围路由；令牌不进入上游。"""
    env = Env(tmp_path)
    _wire(env, catalogs)
    env.proxy = create_proxy_app(env.settings, upstreams={c: catalogs["svc"][c].url for c in ("eval", "dist")})
    hdr = lambda role: {"Authorization": f"Bearer {env.tokens[role]}"}   # noqa: E731
    dist_tok = _set_cookies(env.client.get(f"{P}/runs/run-0001/viewer_url", headers=hdr("researcher")))[0].value
    eval_tok = _set_cookies(env.client.get(f"{P}/runs/run-0001/viewer_url", params={"catalog": "eval"}, headers=hdr("evaluator")))[0].value
    with TestClient(env.proxy) as pc:
        v = _post(pc, _rpc("Version"), cookie=dist_tok)
        assert v.status_code == 200 and v.headers["content-type"].startswith("application/grpc-web") and b"re_server" in v.content
        d = _post(pc, _rpc("FindEntries"), cookie=dist_tok)
        assert d.status_code == 200 and b"campus-v2.handoff-01" in d.content and b"evalonly" not in d.content
        e = _post(pc, _rpc("FindEntries"), cookie=eval_tok)
        assert e.status_code == 200 and b"campus-v2.evalonly-01" in e.content and b"campus-v2.handoff-01" in e.content
        nat = _post(pc, f"/rerun/catalog/eval/{GRPC_SERVICE}/FindEntries", bearer=eval_tok, origin=None)           # 本机代理路径
        assert nat.status_code == 200 and b"evalonly" in nat.content
        _denied(_post(pc, f"/rerun/catalog/eval/{GRPC_SERVICE}/FindEntries", cookie=dist_tok), 403, 115, "catalog_scope")
        _denied(_post(pc, _rpc("CreateDatasetEntry"), cookie=eval_tok), 403, 115, "read_only")
        _denied(_post(pc, _rpc("FindEntries")), 401, 302, "missing")
    recs = [a for a in env.audit() if a["action"] == "rerun_proxy"]
    assert [(a["rpc"], a["catalog"]) for a in recs] == [("Version", "dist"), ("FindEntries", "dist"), ("FindEntries", "eval"), ("FindEntries", "eval")]
    assert all(a["bytes_out"] > 0 and a["grpc_status"] in (None, "0") for a in recs)
    assert len([a for a in env.audit() if a["action"] == "rerun_session"]) == 2
    text = env.audit_text()
    assert dist_tok not in text and eval_tok not in text
