"""nginx `/worlds/` 静态直出与 show-api 静态路由的对拍（M11-FR-218、M11-AC-211、M17-AC-020；d2-WP-03-to-M00 第 4 条；ADR-092）。

需要本机有 nginx 可执行文件（`nginx` 在 PATH 中或 /usr/sbin/nginx）；没有时跳过（本仓库原则上不装新依赖，emax 上有 nginx 1.24，
部署后在服务器上执行同一用例：`.venv/bin/python -m pytest tests/ops/test_nginx_static_parity.py`）。

做法：在临时目录建一个最小世界（world.json 与 4 KiB 的二进制文件）与共享环境资产；从站点文件取出 http 层的 map 与限流区
（去掉 proxy_cache_path）与 443 server 中 `/worlds/` 的 4 个 location，把 alias 根换成临时目录，以非 root、回环高端口启动
nginx；对同一组请求比较 nginx 与 show-api 静态路由（`awr.api.static.build_static_router` 加 `middleware.SECURITY_HEADERS`）的
语义：状态码（200、206、304、416）、Content-Range、Content-Length、Accept-Ranges、Cache-Control、ETag 存在且各自的条件请求
得 304、COOP、COEP、CORP、nosniff、Referrer-Policy。
已知且登记的差异（不比较）：ETag 的取值（show-api `"<cv>-<size>[-<mtime_ns>]"`，nginx `"<mtime>-<size>"`）；206 上的
Accept-Ranges（nginx 只在 200 上声明，206 自带 Content-Range，前端 `engine/pointcloud/io/fetchJob.ts` 只认后者）；416 的 Cache-Control
（show-api no-store，nginx 沿用 no-cache，二者都不缓存错误）、正文与安全头的重复（nginx 1.24 对 416 再走一遍头过滤器，
add_header always 的值出现两次，同源请求下无影响）；`?v=` 与当前 contentVersion 不一致时 show-api 返回 409 310，
nginx 照常 200（世界包只随发布更新，world.json 恒为 no-cache）。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nginx_conf import SITE

NGINX = shutil.which("nginx") or ("/usr/sbin/nginx" if os.path.exists("/usr/sbin/nginx") else None)
pytestmark = pytest.mark.skipif(
    NGINX is None, reason="本机没有 nginx：用 make deploy-nginx-check（一次性 Ubuntu 24.04 容器，nginx 1.24）执行；emax 上直接执行")

CV = "abc123def456"
SIZE = 4096
WORLDS_ROOT = "/opt/anet-drone4d/var/worlds"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _blocks(text: str, head_re: str) -> list[str]:
    """按大括号配对取出以 head_re 开头的块的完整文本（块内没有引号中的大括号）。"""
    out = []
    for m in re.finditer(head_re, text, re.M):
        i = text.index("{", m.end() - 1)
        depth, j = 0, i
        while True:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(text[m.start():j + 1])
    return out


def _nginx_conf(tmp: Path, worlds: Path, port: int) -> Path:
    site = SITE.read_text(encoding="utf-8")
    http = site[:site.index("# ---- 80")]
    http = "\n".join(line for line in http.splitlines() if not line.lstrip().startswith("proxy_cache_path"))
    locs = _blocks(site, r"^    location ~ \^/worlds/")
    assert len(locs) == 4, len(locs)
    body = "\n".join(locs).replace(WORLDS_ROOT, str(worlds))
    mime = "include /etc/nginx/mime.types;" if os.path.exists("/etc/nginx/mime.types") else "types { application/json json; }"
    conf = f"""
daemon off;
master_process off;
worker_processes 1;
pid {tmp}/nginx.pid;
error_log {tmp}/error.log;
events {{ worker_connections 64; }}
http {{
    {mime}
    default_type application/octet-stream;
    access_log off;
    client_body_temp_path {tmp}/body;
    proxy_temp_path {tmp}/proxy;
    fastcgi_temp_path {tmp}/fastcgi;
    uwsgi_temp_path {tmp}/uwsgi;
    scgi_temp_path {tmp}/scgi;
    limit_req_status 429;
{http}
    server {{
        listen 127.0.0.1:{port};
{body}
    }}
}}
"""
    p = tmp / "nginx.conf"
    p.write_text(conf, encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("nginx")
    worlds = tmp / "worlds"
    (worlds / "synthcity" / "visual" / "pointcloud").mkdir(parents=True)
    (worlds / "synthcity" / "world.json").write_text(f'{{"id": "synthcity", "contentVersion": "{CV}"}}\n', encoding="utf-8")
    (worlds / "synthcity" / "visual" / "pointcloud" / "octree.bin").write_bytes(os.urandom(SIZE))
    (worlds / "_shared" / "env" / "turb").mkdir(parents=True)
    (worlds / "_shared" / "env" / "turb" / "box.awrv").write_bytes(os.urandom(1000))
    port = _free_port()
    conf = _nginx_conf(tmp, worlds, port)
    proc = subprocess.Popen([NGINX, "-p", str(tmp), "-c", str(conf)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError as e:
            if proc.poll() is not None:
                log = (tmp / "error.log").read_text(errors="replace") if (tmp / "error.log").exists() else ""
                raise AssertionError(proc.stdout.read().decode() + log) from e
            time.sleep(0.05)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from awr.api.middleware import SECURITY_HEADERS
    from awr.api.static import build_static_router

    app = FastAPI()
    router, _ = build_static_router(worlds, None)
    app.include_router(router)

    @app.middleware("http")
    async def sec(request, call_next):
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS:
            resp.headers.setdefault(k.decode(), v.decode())
        return resp

    import httpx

    yield httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5), TestClient(app)
    proc.terminate()
    proc.wait(timeout=5)


PATHS = ["/worlds/synthcity/world.json", "/worlds/synthcity/visual/pointcloud/octree.bin",
         f"/worlds/synthcity/visual/pointcloud/octree.bin?v={CV}", "/worlds/_shared/env/turb/box.awrv"]
RANGES = [None, "bytes=0-99", "bytes=-100", "bytes=999999999-", "bytes=0-1,5-6"]
SEC = ["cross-origin-opener-policy", "cross-origin-embedder-policy", "cross-origin-resource-policy", "x-content-type-options",
       "referrer-policy"]


CASES = [("GET", r) for r in RANGES] + [("HEAD", None)]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(("method", "rng"), CASES)
def test_semantics_match(setup, path, method, rng):
    ng, api = setup
    h = {"Range": rng} if rng else {}
    a = api.request(method, path, headers=h)
    n = ng.request(method, path, headers=h)
    assert n.status_code == a.status_code, (path, rng, method, n.status_code, a.status_code)
    if a.status_code == 416:
        assert n.headers.get("content-range") == a.headers.get("content-range")
        for k in SEC:  # nginx 1.24 对 416 再走一遍头过滤器，add_header always 的值会重复一次（同源请求下无影响）
            assert {x.strip() for x in n.headers.get(k, "").split(",")} == {a.headers.get(k)}, (path, k)
        return
    for k in SEC:
        assert n.headers.get(k) == a.headers.get(k), (path, k)
    for k in ("content-range", "content-length", "cache-control"):
        assert n.headers.get(k) == a.headers.get(k), (path, rng, method, k, n.headers.get(k), a.headers.get(k))
    if a.status_code == 200:  # nginx 只在 200 上声明 Accept-Ranges（206 自带 Content-Range，前端只认它，fetchJob.ts）
        assert n.headers.get("accept-ranges") == a.headers.get("accept-ranges") == "bytes"
    assert n.headers.get("etag") and a.headers.get("etag")
    assert n.headers.get("content-type", "").split(";")[0] == a.headers.get("content-type", "").split(";")[0]
    if method == "GET":
        assert n.content == a.content


@pytest.mark.parametrize("path", [p for p in PATHS if "?v=" not in p])
def test_conditional_304(setup, path):
    ng, api = setup
    for c in (ng, api):
        etag = c.get(path).headers["etag"]
        r = c.get(path, headers={"If-None-Match": etag})
        assert r.status_code == 304, (path, c)


def test_whitelist_and_hidden(setup):
    ng, _ = setup
    assert ng.get("/worlds/synthcity/.status/x").status_code == 404
    assert ng.get("/worlds/.geo-cache/synthcity/x").status_code == 404


def test_known_deviation_stale_v(setup):
    """登记的差异：?v= 与当前 contentVersion 不一致时 show-api 409 310，nginx 200 immutable。"""
    ng, api = setup
    p = "/worlds/synthcity/visual/pointcloud/octree.bin?v=000000000000"
    assert api.get(p).status_code == 409
    r = ng.get(p)
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
