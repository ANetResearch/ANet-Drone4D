"""nginx 站点文件的端到端检查（M00，D2 WP-16、D2-FX-R2-deploy；M17-AC-020、D2-AC-42 的 nginx 部分、API-AC-063 的"微缓存下
100 并发每秒到达 sandbox-api ≤ 1 次"）。需要 nginx：开发机用 `make deploy-nginx-check`（一次性 Ubuntu 24.04 容器，nginx 1.24，
与 emax 相同，本机不装 nginx）；emax 上与 `test_nginx_static_parity.py` 一起直接执行。本机没有 nginx 时跳过。

把真实站点文件改写成可在普通用户下运行的形式：443 的 `listen … ssl http2` 换成回环高端口的明文 listen、去掉 ssl_* 指令、
两个 upstream 指向本进程内的两个桩服务、alias 根换成临时目录、proxy_cache_path 换到临时目录；其余（location、限流、map、
缓存、头）原样。检查：

- 路径分流到正确的 upstream、静态直出、X-Forwarded-For 与 Host 透传、沙盒创建按前缀限流（突发之后 429）；
- 容量接口（D2-AC-42）：100 个并发客户端（各自的回环源地址，即各自的地址前缀；GET 与 HEAD 混合；一半带查询参数；全部
  自带 X-Request-Id、Cookie、Authorization、Origin 与 X-AWR-Identity）同时起跑并持续 8 s：全部 200、到达 sandbox-api 每个自然秒 ≤ 1 次且相邻两次
  间隔 > 1 s、副本与访客无关（回源只带规范路径、Host 与 X-Forwarded-Proto，回复不带 X-Request-Id 与 Set-Cookie）；
  上游 503、500、冷缓存时上游 2.5 s、上游卡住（> 读超时）与上游不可达五种故障下同样 ≤ 1 次/s，且错误回复按 1 s 缓存、
  不无限期提供过期副本；GET、HEAD 以外的方法 405 且不回源。

`AWR_NGINX_EVIDENCE_DIR` 有值时，容量用例把测量写成 `<dir>/capacity-<用例>.json`（`awr.deploy.nginx-capacity.v1`），供验收引用
（`nginx-check.sh --out`）。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nginx_conf import SITE

NGINX = shutil.which("nginx") or ("/usr/sbin/nginx" if os.path.exists("/usr/sbin/nginx") else None)
pytestmark = pytest.mark.skipif(
    NGINX is None, reason="本机没有 nginx：用 make deploy-nginx-check（一次性 Ubuntu 24.04 容器，nginx 1.24）执行；emax 上直接执行")

HOST = "drone4d.agentnetwork.org.cn"
CAP = "/api/sandbox/v1/capacity"
N_CLIENTS = 100                      # D2-AC-42：100 个并发客户端
CACHE_STATUSES = {"HIT", "UPDATING", "EXPIRED", "MISS", "STALE"}
VISITOR_HEADERS = ("x-request-id", "cookie", "authorization", "origin", "x-awr-identity", "x-forwarded-for", "x-real-ip", "user-agent",
                   "accept-encoding")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stub:
    """记录请求的上游桩：回显 {upstream, path, host, xff}。容量接口另记到达时刻与收到的全部请求头，按 `status` 回复（可加
    `delay` 秒），并带 Cache-Control、X-Request-Id（回显访客的或自造）与 Set-Cookie（模拟最坏的上游：每个回复都带单个请求的
    标识）；正文带序号与生成时刻 `t`。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.hits: list[str] = []
        self.cap: list[tuple[float, str, dict[str, str]]] = []      # (到达时刻, 路径, 请求头)
        self.status = 200
        self.delay = 0.0
        self.lock = threading.Lock()
        self.conns: set[socket.socket] = set()
        self.closed = False
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                with stub.lock:
                    stub.conns.add(self.connection)

            def finish(self) -> None:
                with stub.lock:
                    stub.conns.discard(self.connection)
                super().finish()

            def _reply(self) -> None:
                t = time.time()
                stub.hits.append(self.path)
                if self.command == "POST":
                    n = int(self.headers.get("content-length") or 0)
                    if n:
                        self.rfile.read(n)
                status, extra = 200, []
                if "capacity" in self.path:
                    with stub.lock:
                        stub.cap.append((t, self.path, {k.lower(): v for k, v in self.headers.items()}))
                        seq = len(stub.cap)
                    if stub.delay:
                        time.sleep(stub.delay)
                    status = stub.status
                    body = json.dumps({"upstream": stub.name, "path": self.path, "seq": seq, "t": t}).encode()
                    extra = [("cache-control", "public, max-age=1"),
                             ("x-request-id", self.headers.get("x-request-id") or f"rid-{seq}"),
                             ("set-cookie", f"awr_probe={seq}; Path=/")]
                else:
                    body = json.dumps({"upstream": stub.name, "path": self.path, "host": self.headers.get("host"),
                                       "xff": self.headers.get("x-forwarded-for")}).encode()
                    extra = [("cache-control", "no-store")]
                try:
                    self.send_response(status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    for k, v in extra:
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass                                             # nginx 已因读超时断开（卡住的上游）

            do_GET = do_POST = do_DELETE = do_PUT = _reply

            def log_message(self, *a) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        """停止监听并断开已有连接（nginx 到上游的长连接也随之失效，模拟 sandbox-api 不可达）；可重复调用。"""
        if self.closed:
            return
        self.closed = True
        self.server.shutdown()
        self.server.server_close()
        with self.lock:
            conns = list(self.conns)
        for s in conns:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)

    def arrivals(self, n0: int = 0) -> list[float]:
        with self.lock:
            return [t for t, _, _ in self.cap[n0:]]


def _adapt(site: str, tmp: Path, port: int, backend: int, sandbox: int) -> str:
    s = site.replace("127.0.0.1:18640", f"127.0.0.1:{backend}").replace("127.0.0.1:18641", f"127.0.0.1:{sandbox}")
    s = s.replace("/opt/anet-drone4d", str(tmp / "opt"))
    s = s.replace("/var/lib/nginx/drone4d-micro", str(tmp / "micro"))
    s = re.sub(r"^\s*listen 80;\n\s*listen \[::\]:80;\n", f"    listen 127.0.0.1:{_free_port()};\n", s, flags=re.M)
    s = re.sub(r"^\s*listen 443 ssl http2;\n\s*listen \[::\]:443 ssl http2;\n", f"    listen 127.0.0.1:{port};\n", s, flags=re.M)
    s = "\n".join(line for line in s.splitlines() if not line.lstrip().startswith("ssl_"))
    return s


def _start(tmp: Path, backend: Stub, sandbox: Stub) -> tuple[subprocess.Popen, int]:
    (tmp / "opt" / "var" / "worlds" / "synthcity").mkdir(parents=True)
    (tmp / "opt" / "var" / "worlds" / "synthcity" / "world.json").write_text('{"id": "synthcity", "contentVersion": "abc"}\n')
    (tmp / "opt" / "current" / "tools" / "sdk").mkdir(parents=True)
    (tmp / "opt" / "current" / "tools" / "sdk" / "awr_sandbox.py").write_text("print('sdk')\n")
    (tmp / "opt" / "current" / "apps" / "web" / "dist").mkdir(parents=True)
    (tmp / "opt" / "current" / "apps" / "web" / "dist" / "build-info.json").write_text('{"release": "x"}\n')
    port = _free_port()
    mime = "include /etc/nginx/mime.types;" if os.path.exists("/etc/nginx/mime.types") else ""
    conf = f"""
daemon off;
master_process off;
worker_processes 1;
pid {tmp}/nginx.pid;
error_log {tmp}/error.log;
events {{ worker_connections 1024; }}
http {{
    {mime}
    default_type application/octet-stream;
    access_log off;
    client_body_temp_path {tmp}/body;
    proxy_temp_path {tmp}/proxy;
    fastcgi_temp_path {tmp}/fastcgi;
    uwsgi_temp_path {tmp}/uwsgi;
    scgi_temp_path {tmp}/scgi;
{_adapt(SITE.read_text(encoding="utf-8"), tmp, port, backend.port, sandbox.port)}
}}
"""
    (tmp / "nginx.conf").write_text(conf, encoding="utf-8")
    proc = subprocess.Popen([NGINX, "-p", str(tmp), "-c", str(tmp / "nginx.conf")], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError as e:
            if proc.poll() is not None:
                log = (tmp / "error.log").read_text(errors="replace") if (tmp / "error.log").exists() else ""
                raise AssertionError(proc.stdout.read().decode() + log) from e
            time.sleep(0.05)
    return proc, port


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    proc.wait(timeout=5)


_SSL: list[ssl.SSLContext] = []


def _client(port: int, src: str | None = None):
    """回环明文客户端；`src` 为源地址（各自的地址前缀）。100 个客户端共用一个 SSL 上下文（每个各建一个要数秒）。"""
    import httpx

    if not _SSL:
        _SSL.append(ssl.create_default_context())
    transport = httpx.HTTPTransport(local_address=src, verify=_SSL[0]) if src else None
    return httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15, headers={"host": HOST}, transport=transport)


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("site")
    backend, sandbox = Stub("show-api"), Stub("sandbox-api")
    proc, port = _start(tmp, backend, sandbox)
    with _client(port) as c:
        yield c, backend, sandbox, port
    _stop(proc)
    backend.close()
    sandbox.close()


@pytest.fixture
def fresh(tmp_path):
    """故障用例各用一套新的 nginx（冷缓存）与桩，互不影响。"""
    backend, sandbox = Stub("show-api"), Stub("sandbox-api")
    proc, port = _start(tmp_path, backend, sandbox)
    yield port, sandbox
    _stop(proc)
    backend.close()
    sandbox.close()


# ---------------------------------------------------------------- 路径分流、静态、限流


@pytest.mark.parametrize(("method", "uri", "want"), [
    ("GET", "/api/health", "show-api"),
    ("POST", "/api/auth/identity", "show-api"),
    ("POST", "/api/viewer/queue", "show-api"),
    ("GET", "/api/viewer/queue/vq-abcdefghij?wait_s=0", "show-api"),
    ("GET", "/api/sandbox/v1/meta", "sandbox-api"),
    ("GET", "/api/sandbox/v1/sessions/sb-abcdefghij", "sandbox-api"),
    ("POST", "/api/sandbox/v1/sessions/sb-abcdefghij/park", "sandbox-api"),
    ("GET", "/worlds/unknown/world.json", "show-api"),
    ("GET", "/world/synthcity", "show-api"),
])
def test_routes(site, method, uri, want):
    c, _, _, _ = site
    r = c.request(method, uri, content=b"{}" if method == "POST" else None)
    assert r.status_code == 200, (uri, r.status_code)
    doc = r.json()
    assert doc["upstream"] == want and doc["host"] == HOST
    assert doc["xff"] == "127.0.0.1"


def test_static_paths(site):
    c, backend, sandbox, _ = site
    n0 = len(backend.hits) + len(sandbox.hits)
    assert c.get("/worlds/synthcity/world.json").json()["contentVersion"] == "abc"
    assert c.get("/sdk/awr_sandbox.py").text == "print('sdk')\n"
    r = c.get("/build-info.json")
    assert r.json() == {"release": "x"} and r.headers["cache-control"] == "no-cache"
    assert c.get("/worlds/.geo-cache/x").status_code == 404
    assert len(backend.hits) + len(sandbox.hits) == n0


def test_create_rate_limited_by_prefix(site):
    c, _, _, _ = site
    codes = [c.post("/api/sandbox/v1/sessions", content=b"{}").status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and 429 in codes[5:], codes      # 2r/m、突发 4：第 6 个起 429


# ---------------------------------------------------------------- 容量接口微缓存（D2-AC-42）


def _expire() -> None:
    """nginx 的有效期按整秒：在第 S 秒存入、valid 1s 的副本到第 S+2 秒才过期，因此等 2.1 s 保证已过期。"""
    time.sleep(2.1)


def _swarm(port: int, seconds: float, *, n: int = N_CLIENTS, seed: int = 20261012) -> tuple[list[dict[str, Any]], float, float]:
    """n 个并发客户端（源地址 127.0.<k+1>.1，各自一个地址前缀；每 4 个中 1 个用 HEAD）在同一时刻发出第一轮请求，此后各自间隔
    0.1 至 0.4 s 随机持续请求 `seconds` 秒；一半请求带随机查询参数；全部自带 X-Request-Id、Cookie、Authorization、Origin 与
    X-AWR-Identity（sandbox-api 对外来 Origin 回 403：若转发，一个访客就能让共享副本变成错误回复）。
    返回 (逐请求记录, 起跑时刻, 收尾时刻)。"""
    clients = [_client(port, f"127.0.{k + 1}.1") for k in range(n)]
    barrier = threading.Barrier(n)
    box: dict[str, float] = {}

    def worker(k: int) -> list[dict[str, Any]]:
        rng = random.Random(seed + k)
        method = "HEAD" if k % 4 == 3 else "GET"
        out: list[dict[str, Any]] = []
        if barrier.wait() == 0:
            box["t0"] = time.time()
            box["end"] = time.monotonic() + seconds
        while "end" not in box:
            time.sleep(0.0005)
        i = 0
        while time.monotonic() < box["end"]:
            q = f"?bust={k}-{i}&visitor={k}" if rng.random() < 0.5 else ""
            hdr = {"x-request-id": f"visitor-{k}-{i}", "cookie": f"sid=visitor-{k}", "authorization": f"Bearer visitor-{k}",
                   "origin": f"https://visitor-{k}.example", "x-awr-identity": f"visitor-{k}"}
            r = clients[k].request(method, CAP + q, headers=hdr)
            rec = {"k": k, "method": method, "status": r.status_code, "t_recv": time.time(),
                   "cache": r.headers.get("x-drone4d-cache"), "cc": r.headers.get("cache-control"),
                   "rid": r.headers.get("x-request-id"), "cookie": r.headers.get("set-cookie"), "len": len(r.content)}
            if method == "GET" and r.status_code in (200, 500, 503) and r.content:
                with contextlib.suppress(ValueError, KeyError, TypeError):
                    rec["t_gen"] = float(r.json()["t"])
            out.append(rec)
            i += 1
            time.sleep(rng.uniform(0.1, 0.4))
        return out

    try:
        with ThreadPoolExecutor(max_workers=n) as ex:
            recs = [r for rs in ex.map(worker, range(n)) for r in rs]
    finally:
        for cl in clients:
            cl.close()
    return recs, box["t0"], time.time()


def _measure(name: str, recs: list[dict[str, Any]], arrivals: list[float], t0: float, t1: float, **extra: Any) -> dict[str, Any]:
    ts = sorted(arrivals)
    per_s = Counter(int(t) for t in ts)
    gaps = [b - a for a, b in pairwise(ts)]
    # 副本年龄只看持续请求的稳态（起跑 1 s 之后）：空闲之后的第一次刷新期间，并发的访客按 updating 拿到空闲前的那一份
    ages = [r["t_recv"] - r["t_gen"] for r in recs if "t_gen" in r and r["t_recv"] >= t0 + 1.0]
    doc = {
        "schema": "awr.deploy.nginx-capacity.v1", "case": name, "ac": ["D2-AC-42", "API-AC-063"],
        "clients": len({r["k"] for r in recs}), "duration_s": round(t1 - t0, 3), "requests": len(recs),
        "req_per_s": round(len(recs) / max(t1 - t0, 1e-9), 1),
        "methods": dict(Counter(r["method"] for r in recs)), "status": {str(k): v for k, v in sorted(Counter(r["status"] for r in recs).items())},
        "cache_status": dict(Counter(str(r["cache"]) for r in recs)),
        "upstream_arrivals": len(ts), "arrivals_max_per_second": max(per_s.values(), default=0),
        "arrivals_min_gap_s": round(min(gaps), 4) if gaps else None, "arrival_offsets_s": [round(t - t0, 3) for t in ts],
        "copy_max_age_s": round(max(ages), 3) if ages else None,
        "responses_with_x_request_id": sum(1 for r in recs if r["rid"]), "responses_with_set_cookie": sum(1 for r in recs if r["cookie"]),
        "nginx": _nginx_version(),
    } | extra
    out = os.environ.get("AWR_NGINX_EVIDENCE_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"capacity-{name}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return doc


def _nginx_version() -> str:
    r = subprocess.run([NGINX, "-v"], capture_output=True, text=True, check=False)
    return (r.stderr or r.stdout).strip()


def _assert_rate(doc: dict[str, Any]) -> None:
    """每个自然秒 ≤ 1 次，且相邻两次间隔 > 1 s（任意 1 s 窗口内 ≤ 1 次）。"""
    assert doc["arrivals_max_per_second"] <= 1, doc
    assert doc["arrivals_min_gap_s"] is None or doc["arrivals_min_gap_s"] > 1.0, doc


def test_capacity_microcache_100_clients(site):
    """D2-AC-42：100 个并发客户端在 nginx 微缓存下每秒到达 sandbox-api ≤ 1 次；副本与访客无关。"""
    c, _, sandbox, port = site
    assert c.get(CAP).status_code == 200
    _expire()
    n0 = len(sandbox.cap)
    recs, t0, t1 = _swarm(port, 8.0)
    time.sleep(0.3)
    arrived = sandbox.cap[n0:]
    doc = _measure("100-clients", recs, [t for t, _, _ in arrived], t0, t1)
    assert doc["clients"] == N_CLIENTS and doc["requests"] >= 1000, doc
    assert doc["status"] == {"200": doc["requests"]}, doc                 # 各自的前缀，远低于每前缀 20 次/s
    _assert_rate(doc)
    assert 3 <= doc["upstream_arrivals"] <= math.ceil(t1 - t0) + 1, doc   # 持续有人请求时副本按秒刷新（数据不冻结）
    assert doc["copy_max_age_s"] is not None and doc["copy_max_age_s"] < 3.0, doc     # 稳态 < 2 s + 回源与线程调度
    assert doc["responses_with_x_request_id"] == 0 and doc["responses_with_set_cookie"] == 0, doc
    assert all(r["cc"] == "public, max-age=1" and r["cache"] in CACHE_STATUSES for r in recs), doc
    assert all(r["len"] == 0 for r in recs if r["method"] == "HEAD"), doc
    for _, path, hdr in arrived:                                          # 回源只带规范路径、Host 与 X-Forwarded-Proto
        assert path == CAP, path
        assert hdr.get("host") == HOST and hdr.get("x-forwarded-proto") == "http", hdr
        assert not set(VISITOR_HEADERS) & set(hdr), hdr


def test_capacity_copy_is_visitor_independent(site):
    """访客自带的标识与查询参数不回源；回复不带 X-Request-Id 与 Set-Cookie。"""
    c, _, sandbox, _ = site
    _expire()
    n0 = len(sandbox.cap)
    r = c.get(CAP + "?bust=1", headers={"x-request-id": "visitor-secret", "cookie": "sid=abc", "authorization": "Bearer x",
                                        "origin": "https://evil.example", "x-awr-identity": "x"})
    assert r.status_code == 200 and r.headers.get("x-drone4d-cache") in ("EXPIRED", "MISS"), r.headers
    assert "x-request-id" not in r.headers and "set-cookie" not in r.headers, r.headers
    assert r.headers.get("cache-control") == "public, max-age=1"
    ((_, path, hdr),) = sandbox.cap[n0:]
    assert path == CAP and not set(VISITOR_HEADERS) & set(hdr), (path, hdr)
    assert "visitor-secret" not in r.text
    r2 = c.get(CAP, headers={"x-request-id": "other"})
    assert r2.headers.get("x-drone4d-cache") == "HIT" and r2.content == r.content


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_capacity_other_methods_do_not_reach_upstream(site, method):
    c, _, sandbox, _ = site
    n0 = len(sandbox.cap)
    assert c.request(method, CAP, content=b"{}").status_code == 405
    assert c.head(CAP).status_code == 200
    assert len(sandbox.cap) - n0 <= 1                                    # HEAD 至多刷新一次（与 GET 同一份）


@pytest.mark.parametrize("status", [503, 500])
def test_capacity_upstream_errors_are_cached(fresh, status):
    """池不可达（503 211）或 500：错误回复同样缓存 1 s，到达 ≤ 1 次/s；访客看到错误而不是无限期的旧 200。"""
    port, sandbox = fresh
    with _client(port) as c:
        assert c.get(CAP).status_code == 200
    _expire()
    sandbox.status = status
    n0 = len(sandbox.cap)
    recs, t0, t1 = _swarm(port, 5.0)
    time.sleep(0.3)
    doc = _measure(f"upstream-{status}", recs, sandbox.arrivals(n0), t0, t1, upstream_status=status)
    _assert_rate(doc)
    assert doc["upstream_arrivals"] >= 2, doc
    assert set(doc["status"]) <= {"200", str(status)}, doc
    late = [r["status"] for r in recs if r["t_recv"] >= t0 + 1.0]
    assert late and all(s == status for s in late), doc                    # 首次刷新之后不再给出过期的 200


def test_capacity_cold_cache_slow_upstream(fresh):
    """冷缓存且上游 2.5 s 才回复：其余访客等锁（5 s）而不各自回源。"""
    port, sandbox = fresh
    sandbox.delay = 2.5
    recs, t0, t1 = _swarm(port, 6.0)
    time.sleep(0.3)
    doc = _measure("cold-slow-upstream", recs, sandbox.arrivals(), t0, t1, upstream_delay_s=2.5)
    _assert_rate(doc)
    assert doc["status"] == {"200": doc["requests"]}, doc
    assert doc["upstream_arrivals"] <= 3, doc


def test_capacity_hung_upstream(fresh):
    """上游卡住（> 读超时 3 s）：每次回源至多 3 s 一次；其余访客拿到副本或 504。"""
    port, sandbox = fresh
    with _client(port) as c:
        assert c.get(CAP).status_code == 200
    _expire()
    sandbox.delay = 6.0
    n0 = len(sandbox.cap)
    recs, t0, t1 = _swarm(port, 7.0)
    doc = _measure("hung-upstream", recs, sandbox.arrivals(n0), t0, t1, upstream_delay_s=6.0)
    _assert_rate(doc)
    assert doc["upstream_arrivals"] <= 3, doc
    assert set(doc["status"]) <= {"200", "504"}, doc


def test_capacity_upstream_down(fresh):
    """sandbox-api 不可达：没有到达；访客多数直接拿到 502（不无限期提供旧副本，AWR-04 §15.6.5）。"""
    port, sandbox = fresh
    with _client(port) as c:
        assert c.get(CAP).status_code == 200
    sandbox.close()
    _expire()
    n0 = len(sandbox.cap)
    recs, t0, t1 = _swarm(port, 3.0)
    doc = _measure("upstream-down", recs, sandbox.arrivals(n0), t0, t1)
    assert doc["upstream_arrivals"] == 0, doc
    assert set(doc["status"]) <= {"200", "502"}, doc
    assert doc["status"].get("502", 0) >= 0.5 * doc["requests"], doc
