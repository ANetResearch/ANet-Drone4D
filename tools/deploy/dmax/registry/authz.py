#!/usr/bin/env python3
"""d3-registry 鉴权子请求服务（AWR-17 §18.23.2、§18.23.3 第 1、2、4 条；AWR-05 §5.6.7、ADR-183；D3-AC-59）。

`d3-registry-front`（nginx，TLS，只监听 ZeroTier 地址）对 `/v2/**` 的每个请求以 `auth_request` 子请求调用本服务，本服务以
`BenchAuth.authorize_registry`（`awr.bench.auth`，B13）逐请求判定：

- 凭据：`Authorization: Basic base64(<principal>:<推送令牌>)`（`docker login <registry> -u <principal> --password-stdin`，
  推送令牌由 BN-82 `POST /api/bench/v1/registry/token` 签发，10 min）；脚本客户端也可用 `Authorization: Bearer <推送令牌>`；
- 方法与路径取自 nginx 设置的 `X-Original-Method` 与 `X-Original-URI`，客户端地址取自 `X-Real-IP`（只用于审计）；
- 结果：放行 204（`X-D3-Principal` 只含主体名与角色，供访问日志）；缺少凭据 401（不写审计：Docker 客户端首个 `/v2/` 握手
  不带凭据）；令牌无效、过期或来源 bench token 已撤销或到期 401（302，`authorize_registry` 写 `deny` 审计）；越界 403
  （115 `invited_scope | project_scope`，写 `deny` 审计）；其他异常 500（nginx 按拒绝处理，失败关闭）。401 带
  `WWW-Authenticate: Basic realm="d3-registry"`。
- 不缓存：每个子请求都重读令牌表（`authorize_registry` → `verify_short` → `_token_live`），nginx 侧也不缓存子请求结果，
  因此 bench token 撤销后推送与拉取在下一个请求即失效（D3-AC-59 撤销 ≤ 2 s；17 §18.23.3 第 4 条"鉴权缓存须 ≤ 1 s"）。
- 不记录令牌：访问日志只写方法、路径（去掉查询串，Blob 上传的 `_state` 参数不入日志）、状态与耗时。

密钥目录与审计目录与 bench-api 相同（`$AWR_BENCH_DATA_ROOT/keys`、`$AWR_BENCH_DATA_ROOT/audit`）；本服务不建立会话，会话库单独
存放（`registry-authz.sqlite`，与 rerun-proxy 的做法相同），不与 bench-api 争用 `bench.sqlite`。

用法（d3-registry-authz 容器，镜像 d3-lab-py）：`python /opt/awr/registry/authz.py --host 127.0.0.1 --port 18672`；
`GET /healthz` → 200。只依赖标准库与 `awr.bench`。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REALM = 'Basic realm="d3-registry"'
AUTHZ_PATH = "/authz"


def credential(header: str) -> str | None:
    """`Authorization` 头 → 推送令牌；Basic 取密码部分，Bearer 取令牌本身；缺失或格式不符为 None。"""
    h = (header or "").strip()
    scheme, _, rest = h.partition(" ")
    rest = rest.strip()
    if scheme.lower() == "bearer" and rest:
        return rest
    if scheme.lower() != "basic" or not rest:
        return None
    try:
        raw = base64.b64decode(rest, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    _, sep, pw = raw.partition(":")
    return pw if sep and pw else None


def decide(auth, headers) -> tuple[int, dict[str, str], dict]:
    """一次鉴权子请求 → (HTTP 状态, 响应头, 日志字段)。`auth` 为 `BenchAuth`（或测试替身），`headers` 为请求头映射。"""
    from awr.bench.auth import AuthError

    method = (headers.get("X-Original-Method") or "").upper()
    uri = headers.get("X-Original-URI") or ""
    client = headers.get("X-Real-IP") or None
    path = uri.split("?", 1)[0]
    log = {"method": method, "path": path}
    if not method or not uri.startswith("/v2"):
        return 403, {}, {**log, "why": "bad_subrequest"}
    tok = credential(headers.get("Authorization") or "")
    if tok is None:
        return 401, {"WWW-Authenticate": REALM}, {**log, "why": "no_credentials"}
    try:
        p = auth.authorize_registry(tok, method, uri, client=client)
    except AuthError as e:
        if e.code == 115:
            return 403, {}, {**log, "code": 115, "why": e.detail}
        return 401, {"WWW-Authenticate": REALM}, {**log, "code": e.code}
    return 204, {"X-D3-Principal": f"{p.name} {p.role}"}, {**log, "principal": p.name, "role": p.role}


class Handler(BaseHTTPRequestHandler):
    server_version = "d3-registry-authz/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    auth = None                                   # 由 make_server 绑定

    def _reply(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        t0 = time.monotonic()
        route = self.path.split("?", 1)[0]
        if route == "/healthz":
            self._reply(200, {"Content-Type": "text/plain"}, b"ok\n")
            return
        if route != AUTHZ_PATH:
            self._reply(404, {})
            return
        try:
            status, headers, log = decide(self.auth, self.headers)
        except Exception as e:                    # 失败关闭：nginx 把 500 当作拒绝
            status, headers, log = 500, {}, {"error": type(e).__name__}
        self._reply(status, headers)
        line = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": status, "ms": round((time.monotonic() - t0) * 1e3, 2), **log}
        sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    do_HEAD = do_GET

    def log_message(self, fmt: str, *args) -> None:   # 关闭 http.server 的缺省访问日志（含原始请求行）
        return


def make_server(auth, host: str = "127.0.0.1", port: int = 18672) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"auth": auth})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def build_auth(data_root: Path):
    from awr.bench.auth import BenchAuth
    from awr.bench.settings import BenchSettings

    s = BenchSettings(data_root=Path(data_root))
    return BenchAuth(s.keys_dir, s.data_root / "registry-authz.sqlite")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="authz.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18672)
    ap.add_argument("--data-root", default=os.environ.get("AWR_BENCH_DATA_ROOT", "/data/d3"))
    a = ap.parse_args(argv)
    if a.host not in ("127.0.0.1", "::1", "localhost"):
        print("authz.py binds loopback only (the front proxy is on the same host)", file=sys.stderr)
        return 2
    srv = make_server(build_auth(Path(a.data_root)), a.host, a.port)
    print(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "listen": f"{a.host}:{a.port}", "data_root": a.data_root}), flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
