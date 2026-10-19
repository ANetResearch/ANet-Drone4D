"""dmax `d3` profile 内部工作区部署件（`tools/deploy/dmax/lab/`；AWR-05 §7.11、ADR-166；AWR-17 §18.10.3、§18.11、§19；D3-AC-51、
D3-AC-25）的静态核查：

1. `profile.sh up` 在记录桩下渲染（`audit_manifests.render_shell`，不连接 dmax）：六个容器、CM-01 至 CM-08 无错误级发现、
   服务只绑定回环 18650 至 18654、只有 d3-labweb 带 ZeroTier 地址、只读根文件系统、`d3.slice`、不挂 GPU。
2. nginx 站点模板：只有一个 `listen ${LAB_BIND}:${LAB_PORT} ssl`；`/api/bench/`、`/api/lab/rt`、`/rerun/` 与
   `/rerun.cloud.v1alpha1.RerunCloudService/` 的上游端口；页面经 `auth_request` 鉴权；其余 `/api/` 与探针 404。
3. 镜像依赖锁定与 requirements.lock、requirements-d3.lock 一致；runtime.yaml 的 d3 进程入口模块存在。
4. labctl.py 的端口表解析、Set-Cookie 属性解析与 TCP 扫描（回环上的监听与未监听端口）。
"""

from __future__ import annotations

import importlib.util
import re
import socket
import sys
from pathlib import Path

import pytest
import yaml

from awr.agentsdk.env.containers import check_launches, parse_docker_run

ROOT = Path(__file__).resolve().parents[3]
LAB = ROOT / "tools/deploy/dmax/lab"
SERVICES = {"d3-bench": ("bench", 18650), "d3-lab-gateway": ("lab-gateway", 18651), "d3-rerun-proxy": ("rerun-proxy", 18652),
            "d3-bench-catalog-dist": ("bench", 18653), "d3-bench-catalog-eval": ("bench", 18654)}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit():
    return _load("audit_manifests", ROOT / "tools/deploy/dmax/audit_manifests.py")


@pytest.fixture(scope="module")
def labctl():
    return _load("labctl", LAB / "labctl.py")


@pytest.fixture(scope="module")
def rendered(audit):
    code, calls, err = audit.render_shell(LAB / "profile.sh", ("up",))
    assert code == 0, err
    return [c for c in calls if c[:1] == ["run"]]


def _opt(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


def test_profile_renders_six_containers_without_manifest_errors(rendered) -> None:
    launches = [parse_docker_run(["docker", *c], "sh:lab/profile.sh") for c in rendered]
    assert sorted(lc.name for lc in launches) == sorted([*SERVICES, "d3-labweb"])
    errors = [f for f in check_launches(launches) if f.level == "error"]
    assert errors == []
    for lc in launches:
        assert lc.gpus == [] and lc.devices == [] and lc.runtime is None        # CPU 池
        assert lc.cgroup_parent == "d3.slice" and lc.memory and lc.cpus
        assert lc.labels.get("d3.profile") == "d3"


def test_services_bind_loopback_and_only_labweb_faces_zerotier(rendered) -> None:
    by_name = {_opt(c, "--name")[0]: c for c in rendered}
    for name, (component, port) in SERVICES.items():
        c = by_name[name]
        assert _opt(c, "--network") == ["host"] and "--read-only" in c
        assert _opt(c, "--label").count(f"d3.component={component}") == 1
        i = c.index("--host")
        assert c[i + 1:i + 4] == ["127.0.0.1", "--port", str(port)]
        assert "10.253.20.5" not in " ".join(c[c.index("python"):])                # 服务命令不含 ZeroTier 地址
        assert any(e.startswith("AWR_BENCH_ALLOWED_ORIGINS=https://10.253.20.5:18660") for e in _opt(c, "-e"))
        assert _opt(c, "--user") == ["10001:10001"]
    web = by_name["d3-labweb"]
    assert {"LAB_BIND=10.253.20.5", "LAB_PORT=18660"} <= set(_opt(web, "-e"))
    assert any(v.endswith(":/etc/nginx/tls:ro") for v in _opt(web, "-v"))
    assert any(e == "AWR_RERUN_PROXY_BASE=https://10.253.20.5:18660" for e in _opt(by_name["d3-bench"], "-e"))
    assert by_name["d3-bench"][by_name["d3-bench"].index("python"):][:3] == ["python", "-m", "awr.api.bench_main"]
    assert by_name["d3-lab-gateway"][by_name["d3-lab-gateway"].index("python"):][:3] == ["python", "-m", "awr.bench.api.gateway"]
    assert by_name["d3-rerun-proxy"][by_name["d3-rerun-proxy"].index("python"):][:3] == ["python", "-m", "awr.bench.api.rerun_proxy"]


def test_profile_rejects_unknown_action(audit) -> None:
    code, calls, _err = audit.render_shell(LAB / "profile.sh", ("bogus",))
    assert code == 2 and not [c for c in calls if c[:1] == ["run"]]


def test_nginx_template_routes_and_auth() -> None:
    t = (LAB / "nginx/lab.conf.template").read_text(encoding="utf-8")
    body = "\n".join(ln.split("#", 1)[0] for ln in t.splitlines())                   # 去掉注释
    assert re.findall(r"^\s*listen\s+([^;]+);", body, re.M) == ["${LAB_BIND}:${LAB_PORT} ssl"]

    def block(head: str) -> str:
        i = body.index(head)
        return body[i:body.index("}", i)]

    assert "proxy_pass http://127.0.0.1:18650;" in block("location /api/bench/")
    rt = block("location = /api/lab/rt")
    assert "proxy_pass http://127.0.0.1:18651;" in rt and "Upgrade $http_upgrade" in rt
    for head in ("location /rerun/ {", "location /rerun.cloud.v1alpha1.RerunCloudService/ {"):
        b = block(head)
        assert "proxy_pass http://127.0.0.1:18652;" in b and "proxy_buffering off;" in b
    assert "proxy_pass http://127.0.0.1:18650/api/bench/v1/session;" in block("location = /_auth")
    root = block("location / {")
    assert "auth_request /_auth;" in root and "error_page 401 403 = @login;" in root
    assert "return 404;" in block("location /api/ {") and "return 404;" in block("location = /rerun/healthz")
    assert set(re.findall(r"127\.0\.0\.1:(\d+)", body)) == {"18650", "18651", "18652"}       # catalog 端口不经反向代理
    hdr = (LAB / "nginx/lab-headers.conf").read_text(encoding="utf-8")
    for h in ("Strict-Transport-Security", "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"):
        assert h in hdr
    assert body.count("add_header Cache-Control") == body.count("include /etc/nginx/lab-headers.conf;") - 1   # add_header 不跨层继承


def _pins(path: Path) -> dict[str, str]:
    out = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\;]+)", ln)
        if m:
            out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


def test_image_requirements_match_lock_files() -> None:
    want = _pins(LAB / "requirements-lab.txt")
    locks = {**_pins(ROOT / "requirements.lock"), **_pins(ROOT / "requirements-d3.lock")}
    assert want and {k: v for k, v in want.items() if locks.get(k) != v} == {}


def test_runtime_yaml_d3_entry_modules_exist() -> None:
    doc = yaml.safe_load((ROOT / "configs/runtime.yaml").read_text(encoding="utf-8"))
    procs = {p["name"]: p["cmd"] for p in doc["procs"] if p.get("layer") == "d3"}
    assert procs["lab-gateway"] == ["python", "-m", "awr.bench.api.gateway"]
    assert procs["rerun-proxy"] == ["python", "-m", "awr.bench.api.rerun_proxy"]
    for name in ("lab-gateway", "rerun-proxy", "bench-api"):
        assert importlib.util.find_spec(procs[name][2]) is not None, name


def test_labctl_ports_cookies_and_scan(labctl) -> None:
    assert labctl.parse_ports("22,18650-18652") == [22, 18650, 18651, 18652]
    with pytest.raises(ValueError):
        labctl.parse_ports("0-3")
    sc = ["d3_session=abc; HttpOnly; Max-Age=43200; Path=/; SameSite=strict; Secure", "other=1; Path=/"]
    assert labctl.cookie_attrs(sc, "d3_session") == [{"has_value": True, "httponly": True, "max-age": "43200", "path": "/",
                                                       "samesite": "strict", "secure": True}]
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        open_port = srv.getsockname()[1]
        with socket.socket() as tmp:
            tmp.bind(("127.0.0.1", 0))
            closed_port = tmp.getsockname()[1]
        assert labctl.probe_port("127.0.0.1", open_port, 1.0) == "open"
        assert labctl.probe_port("127.0.0.1", closed_port, 1.0) == "closed"
