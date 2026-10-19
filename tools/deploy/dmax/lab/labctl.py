#!/usr/bin/env python3
"""内部工作区（dmax `d3` profile）的部署核查（AWR-05 §7.11、ADR-166；AWR-17 §18.3、§18.10、§18.11；D3-AC-51、D3-AC-25）。

子命令（结果写 JSON，不输出、不记录任何令牌与 Cookie 值）：

- `scan --host <地址> --label <标签> [--ports …] [--out F]`：TCP 连接扫描（只用标准库，Python ≥ 3.10）。判定"只经 ZeroTier 可达、
  公网端口扫描不可达"（D3-AC-51）的两个视角：本机（ZeroTier 成员）对 dmax 的 ZeroTier 地址与公网地址；dmax 宿主对自身每个非回环
  地址（公网网卡、另一 ZeroTier 网络、docker 网桥），以 22 端口（0.0.0.0 监听）为阳性对照，证明 lab 端口只在 ZeroTier 地址上监听。
- `smoke --base https://<ZeroTier 地址>:<端口> [--cafile F] [--issue --data-root D] [--rerun-run RID] [--out F]`：经鉴权反向代理逐项
  核查。无令牌部分（本机经 ZeroTier 执行）：页面未登录 302 到登录页、登录页 200、bench-api 401、D1/D2 接口与探针 404、
  rerun-proxy 无 Cookie 401（grpc-status 16）、lab-gateway 无令牌 4401、TLS 与安全响应头。`--issue`（在 d3-bench 容器内执行，
  需 httpx、websockets 与 awr）另以进程内临时签发的评测员 bench token（1 h，结束时撤销）核查：会话 Cookie 属性（HttpOnly、Secure、
  SameSite=Strict、Max-Age=43200、Path=/）与 `exp - iat = 43200`、登录后页面 200、短期令牌经 `/api/lab/rt` 握手与订阅、`--rerun-run`
  给出时 BN-57 下发两条路径限定的 `d3_rr` 并经 rerun-proxy 读取分发 catalog、注销后 401。
- `cookie-probe --base … --data-root D --out F [--margin-s 60]`：12 h 会话 Cookie 实测（d3-test-f05-cookie 容器内长时运行）：
  签发临时研究者 token（13 h 后自动到期，结束时撤销）→ 登录并记录 Cookie 属性与 `iat`、`exp` → 到期前 `margin` 秒 GET
  /api/bench/v1/session 应为 200 → 到期后 `margin` 秒应为 401；每一步追加写入结果文件。

退出码：0 全部通过；1 有未通过项；2 参数或环境错误。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROTO = "awr.rt.v1"
PREFIX = "/api/bench/v1"
GRPC = "/rerun.cloud.v1alpha1.RerunCloudService"
SECURITY_HEADERS = ("strict-transport-security", "x-content-type-options", "x-frame-options", "referrer-policy")
SESSION_TTL_S = 43_200


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: str | None, obj: Any) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, p)
    else:
        print(text)


# ── 端口扫描 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def parse_ports(spec: str) -> list[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    if not out or min(out) < 1 or max(out) > 65535:
        raise ValueError(f"bad port list {spec!r}")
    return sorted(out)


def probe_port(host: str, port: int, timeout: float) -> str:
    """open（连接建立）、closed（RST）、filtered（超时或不可达）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "open"
    except ConnectionRefusedError:
        return "closed"
    except (TimeoutError, OSError):
        return "filtered"


def cmd_scan(a: argparse.Namespace) -> int:
    ports = parse_ports(a.ports)
    t0 = time.monotonic()
    with cf.ThreadPoolExecutor(max_workers=min(64, len(ports))) as ex:
        res = dict(zip(ports, ex.map(lambda p: probe_port(a.host, p, a.timeout), ports), strict=True))
    lab = parse_ports(a.lab_ports)
    out = {"schema": "awr.d3.lab_portscan.v1", "t_utc": utc(), "label": a.label, "host": a.host, "method": "tcp-connect",
           "timeout_s": a.timeout, "elapsed_s": round(time.monotonic() - t0, 2), "ports": {str(p): s for p, s in res.items()},
           "open": [p for p, s in res.items() if s == "open"], "lab_ports": lab, "lab_open": [p for p in lab if res.get(p) == "open"]}
    write_json(a.out, out)
    print(f"scan {a.label} {a.host}: {len(ports)} ports, open={out['open']}, lab_open={out['lab_open']}", file=sys.stderr)
    return 0


# ── 经反向代理的核查 ──────────────────────────────────────────────────────────────────────────────────────────────────

class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, ok: bool, **detail: Any) -> bool:
        self.items.append({"check": name, "ok": bool(ok), **detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {json.dumps(detail, ensure_ascii=False)[:300]}", file=sys.stderr)
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(i["ok"] for i in self.items)


def ssl_ctx(cafile: str | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    if cafile:
        ctx.check_hostname = True
    return ctx


def cookie_attrs(set_cookies: list[str], name: str) -> list[dict]:
    """Set-Cookie 头 → 属性表（值只记录是否非空，不记录内容）。"""
    out = []
    for raw in set_cookies:
        parts = [p.strip() for p in raw.split(";")]
        k, _, v = parts[0].partition("=")
        if k != name:
            continue
        attrs: dict[str, Any] = {"has_value": bool(v)}
        for p in parts[1:]:
            ak, _, av = p.partition("=")
            attrs[ak.lower()] = av if av else True
        out.append(attrs)
    return out


def grpc_web(client: Any, base: str, rpc: str, cookies: dict | None = None) -> Any:
    hdr = {"content-type": "application/grpc-web+proto", "x-grpc-web": "1", "accept": "application/grpc-web+proto", "origin": base}
    return client.post(f"{base}{GRPC}/{rpc}", content=b"\x00\x00\x00\x00\x00", headers=hdr, cookies=cookies)


def ws_open(base: str, cafile: str | None, protos: list[str]):
    """WS 连接用独立的 SSLContext：httpx 会改写传入上下文的 ALPN（装有 h2 时含 h2），nginx 开启 http2 时 WS 升级会被协商到 h2。"""
    from websockets.sync.client import connect

    u = urlsplit(base)
    ctx = ssl_ctx(cafile) if u.scheme == "https" else None
    url = ("wss" if u.scheme == "https" else "ws") + f"://{u.netloc}/api/lab/rt"
    return connect(url, subprotocols=protos, additional_headers={"Origin": base}, ssl=ctx,
                   open_timeout=10, close_timeout=3)


def unauth_checks(c: Checks, client: Any, base: str, cafile: str | None) -> None:
    r = client.get(f"{base}/")
    c.add("root redirects to /lab/runs", r.status_code == 302 and r.headers.get("location", "").endswith("/lab/runs"), status=r.status_code)
    r = client.get(f"{base}/lab/runs")
    c.add("page without session redirects to login", r.status_code == 302 and "/lab/login?next=/lab/runs" in r.headers.get("location", ""),
          status=r.status_code, location=r.headers.get("location"))
    r = client.get(f"{base}/lab/login")
    c.add("login page 200 (html)", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""), status=r.status_code)
    c.add("security headers", all(h in r.headers for h in SECURITY_HEADERS), present=[h for h in SECURITY_HEADERS if h in r.headers])
    r = client.get(f"{base}{PREFIX}/session")
    c.add("bench-api without session 401", r.status_code == 401 and r.json().get("code") in (301, 302), status=r.status_code)
    for path in ("/api/rt", "/api/sandbox/v1/sessions", "/rerun/healthz", "/api/lab/rt/healthz", "/api/bench/v1/../../api/rt"):
        r = client.get(f"{base}{path}")
        c.add(f"not exposed: {path}", r.status_code in (404, 400), status=r.status_code)
    r = grpc_web(client, base, "Version")
    c.add("rerun-proxy without d3_rr 401", r.status_code == 401 and r.headers.get("grpc-status") == "16", status=r.status_code)
    # 网关在发出 error{301} 后立即以 4401 关闭；同机回环时延下 websockets 同步客户端偶发"等待握手响应超时"（响应与 EOF 同时到达，
    # curl 30/30 次均为 101），故最多尝试 3 次并记录尝试次数
    last: dict[str, Any] = {}
    for attempt in range(1, 4):
        try:
            with ws_open(base, cafile, [PROTO]) as ws:
                m = json.loads(ws.recv(timeout=10))
                code = None
                try:
                    ws.recv(timeout=5)
                except Exception as e:
                    code = getattr(getattr(e, "rcvd", None), "code", None)
                last = {"code": m.get("code"), "close": code, "attempts": attempt}
                break
        except Exception as e:
            last = {"error": type(e).__name__, "attempts": attempt}
    c.add("lab-gateway without token: error 301 + 4401", last.get("code") == 301 and last.get("close") == 4401, **last)


def issue_token(data_root: str, role: str, principal: str, ttl_s: int) -> tuple[Any, str, str]:
    from awr.bench.auth import BenchAuth
    from awr.bench.settings import BenchSettings

    s = BenchSettings(data_root=Path(data_root))
    auth = BenchAuth(s.keys_dir, s.db_path)
    tid, tok = auth.issue_token(principal, role, (), ttl_s=ttl_s)
    return auth, tid, tok


def auth_checks(c: Checks, client: Any, base: str, cafile: str | None, token: str, rerun_run: str | None) -> None:
    hdr = {"origin": base}
    r = client.post(f"{base}{PREFIX}/session", json={"token": token}, headers=hdr)
    sc = cookie_attrs(r.headers.get_list("set-cookie"), "d3_session")
    a = sc[0] if sc else {}
    c.add("login 204 sets d3_session (HttpOnly, Secure, SameSite=Strict, Max-Age=43200, Path=/)",
          r.status_code == 204 and len(sc) == 1 and a.get("httponly") is True and a.get("secure") is True
          and str(a.get("samesite", "")).lower() == "strict" and a.get("max-age") == str(SESSION_TTL_S) and a.get("path") == "/",
          status=r.status_code, cookie={k: v for k, v in a.items() if k != "has_value"})
    jar = {"d3_session": r.cookies.get("d3_session")} if r.cookies.get("d3_session") else {}
    r = client.get(f"{base}{PREFIX}/session", cookies=jar)
    body = r.json() if r.status_code == 200 else {}
    ttl = (int(body["exp_unix_ns"]) - int(body["iat_unix_ns"])) // 1_000_000_000 if body.get("exp_unix_ns") else None
    c.add("session exp - iat = 43200 s (12 h)", r.status_code == 200 and ttl == SESSION_TTL_S, ttl_s=ttl, role=body.get("role"))
    r = client.get(f"{base}/lab/runs", cookies=jar)
    c.add("page with session 200", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""), status=r.status_code)
    r = client.get(f"{base}{PREFIX}/runs", cookies=jar)
    runs = r.json().get("items", r.json().get("runs", [])) if r.status_code == 200 else []
    c.add("runs list 200", r.status_code == 200, n=len(runs))
    r = client.post(f"{base}{PREFIX}/session/short-token", json={"aud": "lab-rt"}, cookies=jar, headers={**hdr, "x-d3-csrf": "1"})
    st = r.json().get("token") if r.status_code == 200 else None
    c.add("short token (aud=lab-rt) 200", bool(st), status=r.status_code)
    if st:
        try:
            with ws_open(base, cafile, [PROTO, f"bearer.{st}"]) as ws:
                info = json.loads(ws.recv(timeout=10))
                ws.recv(timeout=10)
                ws.recv(timeout=10)
                from awr.contracts import CONTRACTS_VERSION
                ws.send(json.dumps({"op": "hello", "contracts": CONTRACTS_VERSION, "client": "labctl"}))
                ok = info.get("op") == "serverInfo" and "lab" in info.get("capabilities", []) and info.get("lab", {}).get("role") == "evaluator"
                sub = None
                rid = rerun_run or (runs[0]["run_id"] if runs else None)
                if rid:
                    ws.send(json.dumps({"op": "subscribe", "subs": [{"id": 1, "topic": f"bench/run/{rid}/status"}]}))
                    end = time.monotonic() + 10
                    got_batch = False
                    while time.monotonic() < end and not (sub and got_batch):
                        m = ws.recv(timeout=max(0.1, end - time.monotonic()))
                        if isinstance(m, str) and json.loads(m).get("op") in ("subscribed", "error"):
                            sub = json.loads(m)
                        elif isinstance(m, bytes) and m[:1] == b"\x10":
                            got_batch = True
                    ok = ok and sub is not None and sub.get("op") == "subscribed" and got_batch
                c.add("lab-gateway handshake + status subscription via proxy", ok, run_id=rid, sub=sub and sub.get("op"))
        except Exception as e:
            c.add("lab-gateway handshake + status subscription via proxy", False, error=f"{type(e).__name__}: {e}"[:200])
    if rerun_run:
        r = client.get(f"{base}{PREFIX}/runs/{rerun_run}/viewer_url", params={"catalog": "dist"}, cookies=jar)
        rr = cookie_attrs(r.headers.get_list("set-cookie"), "d3_rr")
        body = r.json() if r.status_code == 200 else {}
        url = str(body.get("url", ""))
        c.add("BN-57 viewer_url: d3_rr on /rerun/ and /rerun.cloud… (HttpOnly, Secure, SameSite=Strict, Max-Age=600), URL without token",
              r.status_code == 200 and sorted(x.get("path") for x in rr) == ["/rerun.cloud.v1alpha1.RerunCloudService", "/rerun/"]
              and all(x.get("httponly") is True and x.get("secure") is True and x.get("max-age") == "600" for x in rr)
              and "d3st_" not in url and "token" not in url.lower() and url.startswith("rerun+https://"),
              status=r.status_code, url_origin=url.split("/dataset/")[0], paths=[x.get("path") for x in rr])
        rv = next((ck.value for ck in r.cookies.jar if ck.name == "d3_rr"), None)   # 同值两条路径
        if rv:
            r2 = grpc_web(client, base, "FindEntries", cookies={"d3_rr": rv})
            c.add("rerun-proxy with d3_rr: FindEntries 200 grpc-status 0", r2.status_code == 200 and
                  (r2.headers.get("grpc-status") in ("0", None)) and b"grpc-status:0" in r2.content.replace(b" ", b""),
                  status=r2.status_code, bytes=len(r2.content))
    r = client.delete(f"{base}{PREFIX}/session", cookies=jar, headers={**hdr, "x-d3-csrf": "1"})
    r2 = client.get(f"{base}{PREFIX}/session", cookies=jar)
    c.add("logout 204, session then 401", r.status_code == 204 and r2.status_code == 401, status=r.status_code, after=r2.status_code)


def cmd_smoke(a: argparse.Namespace) -> int:
    import httpx

    ctx = ssl_ctx(a.cafile) if a.base.startswith("https") else None
    c = Checks()
    auth = tid = None
    with httpx.Client(verify=ctx if ctx is not None else True, follow_redirects=False, timeout=20.0) as client:
        unauth_checks(c, client, a.base, a.cafile)
        if a.issue:
            auth, tid, tok = issue_token(a.data_root, "evaluator", "f05-smoke", 3600)
            try:
                auth_checks(c, client, a.base, a.cafile, tok, a.rerun_run)
            finally:
                auth.revoke_token(tid)
    write_json(a.out, {"schema": "awr.d3.lab_smoke.v1", "t_utc": utc(), "base": a.base, "authenticated": bool(a.issue),
                       "rerun_run": a.rerun_run, "ok": c.ok, "checks": c.items})
    return 0 if c.ok else 1


def cmd_cookie_probe(a: argparse.Namespace) -> int:
    """12 h 会话 Cookie 实测（见模块说明）。"""
    import httpx

    ctx = ssl_ctx(a.cafile) if a.base.startswith("https") else None
    rec: dict[str, Any] = {"schema": "awr.d3.lab_cookie_probe.v1", "base": a.base, "t_start_utc": utc(), "margin_s": a.margin_s, "steps": [],
                           "ok": None}
    auth, tid, tok = issue_token(a.data_root, "researcher", "f05-cookie-probe", SESSION_TTL_S + 3600)
    try:
        with httpx.Client(verify=ctx if ctx is not None else True, follow_redirects=False, timeout=30.0) as client:
            r = client.post(f"{a.base}{PREFIX}/session", json={"token": tok}, headers={"origin": a.base})
            sc = cookie_attrs(r.headers.get_list("set-cookie"), "d3_session")
            jar = {"d3_session": r.cookies.get("d3_session") or ""}
            s = client.get(f"{a.base}{PREFIX}/session", cookies=jar).json()
            iat, exp = int(s["iat_unix_ns"]) // 10**9, int(s["exp_unix_ns"]) // 10**9
            rec.update(login_status=r.status_code, cookie={k: v for k, v in (sc[0] if sc else {}).items() if k != "has_value"},
                       iat_unix=iat, exp_unix=exp, ttl_s=exp - iat, login_utc=utc())
            write_json(a.out, rec)
            for label, at, want in (("before_expiry", exp - a.margin_s, 200), ("after_expiry", exp + a.margin_s, 401)):
                while (left := at - time.time()) > 0:
                    time.sleep(min(left, 600))
                r = client.get(f"{a.base}{PREFIX}/session", cookies=jar)
                rec["steps"].append({"step": label, "t_utc": utc(), "t_unix": int(time.time()), "rel_to_exp_s": int(time.time()) - exp,
                                     "status": r.status_code, "code": (r.json() or {}).get("code") if r.status_code != 200 else None,
                                     "ok": r.status_code == want})
                write_json(a.out, rec)
    finally:
        auth.revoke_token(tid)
    rec["ok"] = rec.get("ttl_s") == SESSION_TTL_S and len(rec["steps"]) == 2 and all(x["ok"] for x in rec["steps"])
    rec["t_end_utc"] = utc()
    write_json(a.out, rec)
    return 0 if rec["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="labctl.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--host", required=True)
    s.add_argument("--label", required=True, help="vantage and address class, e.g. public, zerotier, dmax-host:ens108f0")
    s.add_argument("--ports", default="22,80,443,8080,8443,18640-18670")
    s.add_argument("--lab-ports", default="18650-18654,18660")
    s.add_argument("--timeout", type=float, default=3.0)
    s.add_argument("--out")
    m = sub.add_parser("smoke")
    m.add_argument("--base", required=True)
    m.add_argument("--cafile")
    m.add_argument("--issue", action="store_true", help="issue a temporary evaluator token in-process (needs --data-root)")
    m.add_argument("--data-root", default=os.environ.get("AWR_BENCH_DATA_ROOT"))
    m.add_argument("--rerun-run")
    m.add_argument("--out")
    k = sub.add_parser("cookie-probe")
    k.add_argument("--base", required=True)
    k.add_argument("--cafile")
    k.add_argument("--data-root", default=os.environ.get("AWR_BENCH_DATA_ROOT"))
    k.add_argument("--margin-s", type=int, default=60)
    k.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if (a.cmd == "cookie-probe" or (a.cmd == "smoke" and a.issue)) and not a.data_root:
        ap.error("--data-root (or AWR_BENCH_DATA_ROOT) is required")
    return {"scan": cmd_scan, "smoke": cmd_smoke, "cookie-probe": cmd_cookie_probe}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
