"""带 /lab/ 的站点文件在真实 nginx 上的端到端检查（tools/deploy/emax/d3-public；D3-AC-21；AWR-05 §7.9 实现约束 1–4、17 §18.8.5）。

需要 nginx：开发机用 `tools/deploy/emax/d3-public/nginx-check.sh`（一次性 Ubuntu 24.04 容器，nginx 1.24，与 emax 相同）；本机没有
nginx 时跳过。站点文件经 `labsite.localize` 改为回环明文端口与临时目录，location、限速、限流与头原样；发布目录由 labctl.py 的
push 与 activate 建立（两个版本，含一个 40 MiB 的制品）；show-api 与 sandbox-api 为桩（D2 行为：`/api/bench*` 404、容量接口
N_max = 4）。用例：

1. verify.py 全部站点检查通过：bench404、lab（SPA 回退、assets immutable、405、点路径 404）、static（no-cache + ETag 304、immutable、
   长度与摘要、404 规则、405）、rate（整段下载前 8 MiB 不限、之后 ≤ 2 MiB/s；Range 从首字节起 ≤ 2 MiB/s）、conn_ip（第 5 条 429）、
   conn_all（11 个回环源地址，第 33 条 429）、nmax；
2. 回滚演练：labctl rollback 切回上一版本（index.json 版本、/lab/ 构建随之切换）；withdraw 恢复 D2 站点文件并 reload 后
   verify --expect d2 通过；再安装"D2 + /lab/"站点文件后 /lab/ 恢复。各步耗时写入证据（≤ 10 min 的判据见 drill.py）。

`AWR_NGINX_EVIDENCE_DIR` 有值时把 verify 报告与回滚耗时写到 `<dir>/d3-public-*.json`。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from d3public_fixtures import load, published  # noqa: E402

NGINX = shutil.which("nginx") or ("/usr/sbin/nginx" if os.path.exists("/usr/sbin/nginx") else None)
pytestmark = pytest.mark.skipif(NGINX is None, reason="本机没有 nginx：用 tools/deploy/emax/d3-public/nginx-check.sh（Ubuntu 24.04 容器）执行")

labctl = load("labctl")
labsite = load("labsite")
verify = load("verify")
HOST = "drone4d.agentnetwork.org.cn"
EVIDENCE = os.environ.get("AWR_NGINX_EVIDENCE_DIR")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stub:
    """D2 上游桩：`/api/bench*` 404（D2 标签构建没有这些路由）、容量接口、其余回显 JSON（含 / 与 SPA 回退）。"""

    def __init__(self, name: str) -> None:
        stub = self
        self.name = name

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self) -> None:
                n = int(self.headers.get("content-length") or 0)
                if n:
                    self.rfile.read(n)
                status = 200
                if self.path.startswith("/api/bench"):
                    status, body = 404, {"status": 404, "title": "Not Found"}
                elif self.path.startswith("/api/sandbox/v1/capacity"):
                    body = {"schema": "awr.capacity.v1", "accepting": True, "sandbox": {"max": 4, "active": 0}}
                else:
                    body = {"upstream": stub.name, "path": self.path}
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(raw)

            do_GET = do_POST = do_HEAD = _reply

            def log_message(self, *a) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Nginx:
    def __init__(self, tmp: Path, show: int, sandbox: int) -> None:
        self.tmp, self.show, self.sandbox = tmp, show, sandbox
        self.port = _free_port()
        self.conf = tmp / "nginx.conf"
        self.site_file = tmp / "site.conf"
        (tmp / "logs").mkdir(exist_ok=True)
        mime = "include /etc/nginx/mime.types;" if os.path.exists("/etc/nginx/mime.types") else ""
        user = "user root;" if os.geteuid() == 0 else ""
        self.conf.write_text(f"""
{user}
daemon off;
worker_processes 2;
pid {tmp}/nginx.pid;
error_log {tmp}/error.log;
events {{ worker_connections 1024; }}
http {{
    {mime}
    default_type application/octet-stream;
    access_log off;
    sendfile on;
    client_body_temp_path {tmp}/body;
    proxy_temp_path {tmp}/proxy;
    fastcgi_temp_path {tmp}/fastcgi;
    uwsgi_temp_path {tmp}/uwsgi;
    scgi_temp_path {tmp}/scgi;
    include {self.site_file};
}}
""", encoding="utf-8")
        self.proc: subprocess.Popen | None = None

    def localize(self, site: str) -> str:
        return labsite.localize(site, root=self.tmp / "srv", show_port=self.show, sandbox_port=self.sandbox,
                                listen=f"127.0.0.1:{self.port}", opt_root=self.tmp / "opt", micro_dir=self.tmp / "micro")

    @property
    def test_cmd(self) -> str:
        return f"{NGINX} -t -q -p {self.tmp} -c {self.conf}"

    @property
    def reload_cmd(self) -> str:
        return f"{NGINX} -s reload -p {self.tmp} -c {self.conf}"

    def start(self) -> None:
        self.proc = subprocess.Popen([NGINX, "-p", str(self.tmp), "-c", str(self.conf)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.1).close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    raise AssertionError((self.tmp / "error.log").read_text(errors="replace")) from None
                time.sleep(0.05)
        raise AssertionError("nginx 未监听")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("d3pub")
    root = tmp / "srv"
    for ver, rel, seed in (("2026.10.19-1", "20261019-060000-aaaaaaaaaa", "s1"), ("2026.10.20-1", "20261020-060000-bbbbbbbbbb", "s2")):
        v, dist = published(labctl, tmp / "src", ver, release=rel, slice_seed=seed, big_mib=40)
        labctl.push(v, dist, str(root))
        labctl.activate(labctl.Layout(root), ver)
    show, sandbox = Stub("show-api"), Stub("sandbox-api")
    ng = Nginx(tmp, show.port, sandbox.port)
    (tmp / "d2-site.conf").write_text(ng.localize(labsite.D2_SITE.read_text(encoding="utf-8")), encoding="utf-8")
    ng.site_file.write_text(ng.localize(labsite.render(labsite.D2_SITE.read_text(encoding="utf-8"))), encoding="utf-8")
    ng.start()
    site = verify.Site(f"http://{HOST}:{ng.port}", connect="127.0.0.1")
    yield ng, site, labctl.Layout(root)
    ng.stop()
    show.close()
    sandbox.close()


def _evidence(name: str, doc: dict) -> None:
    if EVIDENCE:
        Path(EVIDENCE).mkdir(parents=True, exist_ok=True)
        (Path(EVIDENCE) / f"d3-public-{name}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def test_verify_all_site_checks_pass(stack):
    _ng, site, _lay = stack
    rep = verify.run(site, source_ips=verify.parse_ips("127.0.0.2-127.0.0.12"), expect_nmax=4)
    _evidence("verify-lab", rep)
    bad = [c for c in rep["checks"] if not c["ok"]]
    assert not bad, json.dumps(bad, ensure_ascii=False, indent=1)
    names = {c["name"] for c in rep["checks"]}
    assert names >= {"bench404", "lab", "static", "rate", "conn_ip", "conn_all", "nmax"}
    rate = next(c for c in rep["checks"] if c["name"] == "rate")["detail"]
    assert rate["after_8mib_mib_s"] <= 2.0 * 1.05 and rate["range_mib_s"] <= 2.0 * 1.05 and rate["burst_mib_s"] > 3.0
    assert next(c for c in rep["checks"] if c["name"] == "static")["detail"]["version"] == "2026.10.20-1"


def test_nginx_specifics(stack):
    _, site, _lay = stack
    # 分段请求中从 0 开始的整段下载享受前 8 MiB；HEAD 不计速率；index.json 的 ETag 随版本变化
    st, h, _ = site.request("HEAD", "/lab-static/v/2026.10.20-1/runs/run-abcdefghij/video/uav-02_cam0.mp4")
    assert st == 200 and h["content-type"] == "video/mp4" and h["accept-ranges"] == "bytes"
    st, h, _body = site.request("GET", "/lab-static/v/2026.10.20-1/SHA256SUMS")
    assert st == 200 and h["content-type"].startswith("text/plain")
    st, h, _ = site.request("GET", "/lab-static/v/2026.10.20-1/runs/run-abcdefghij/slices/encounter_ab.rrd")
    assert st == 200 and h["content-type"] == "application/octet-stream" and h["cross-origin-resource-policy"] == "same-origin"
    # 上一版本目录在保留期内仍可读（旧页面不中断）
    st, _, _ = site.request("HEAD", "/lab-static/v/2026.10.19-1/SHA256SUMS")
    assert st == 200
    # /lab/ 的安全头与 D2 一致
    st, h, _ = site.request("GET", "/lab/")
    assert h["cross-origin-embedder-policy"] == "require-corp" and h["x-content-type-options"] == "nosniff"


def test_rollback_withdraw_and_reenable(stack):
    ng, site, lay = stack
    t: dict[str, float] = {}
    t0 = time.monotonic()
    labctl.rollback(lay)
    idx, _, _ = verify.fetch_index(site)
    assert idx and idx["version"] == "2026.10.19-1"
    _st, _, body = site.request("GET", "/lab/")
    assert b'data-release="20261019-060000-aaaaaaaaaa"' in body
    t["rollback_version_s"] = round(time.monotonic() - t0, 3)
    # 回滚到 D2：恢复 D2 站点文件（撤下 /lab/），nginx -t 后 reload
    t0 = time.monotonic()
    labctl.withdraw(lay, ng.site_file, d2_site=ng.tmp / "d2-site.conf", nginx_test=ng.test_cmd, nginx_reload=ng.reload_cmd)
    deadline = time.monotonic() + 15
    rep = verify.run(site, expect="d2")
    while not rep["ok"] and time.monotonic() < deadline:          # reload 后旧 worker 处理完已有连接才退出
        time.sleep(0.2)
        rep = verify.run(site, expect="d2")
    t["withdraw_to_d2_s"] = round(time.monotonic() - t0, 3)
    _evidence("verify-d2", rep)
    assert rep["ok"], json.dumps(rep["checks"], ensure_ascii=False)
    assert ng.site_file.read_text(encoding="utf-8") == (ng.tmp / "d2-site.conf").read_text(encoding="utf-8")
    # 重新上线 /lab/
    t0 = time.monotonic()
    labctl.install_site(ng.site_file, ng.localize(labsite.render(labsite.D2_SITE.read_text(encoding="utf-8"))),
                        nginx_test=ng.test_cmd, nginx_reload=ng.reload_cmd)
    ok = False
    while time.monotonic() < t0 + 15 and not ok:
        with contextlib.suppress(Exception):
            ok = verify.check_lab(site)["ok"]
        time.sleep(0.1)
    t["reenable_s"] = round(time.monotonic() - t0, 3)
    _evidence("rollback-timing", t)
    assert ok and all(v < 60 for v in t.values()), t
    # 坏站点文件：nginx -t 失败时不安装
    good = ng.site_file.read_text(encoding="utf-8")
    with pytest.raises(labctl.LabError, match="nginx -t"):
        labctl.install_site(ng.site_file, good.replace("limit_rate 2m;", "limit_rate 2m"), nginx_test=ng.test_cmd,
                            nginx_reload=ng.reload_cmd)
    assert ng.site_file.read_text(encoding="utf-8") == good
