"""dmax `d3` profile 内部工作区部署件（`tools/deploy/dmax/lab/`；AWR-05 §7.11、ADR-166；AWR-17 §18.10.3、§18.11、§19；D3-AC-51、
D3-AC-25）的静态核查：

1. `profile.sh up` 在记录桩下渲染（`audit_manifests.render_shell`，不连接 dmax）：六个容器、CM-01 至 CM-08 无错误级发现、
   服务只绑定回环 18650 至 18654、只有 d3-labweb 带 ZeroTier 地址、只读根文件系统、`d3.slice`、不挂 GPU。
2. nginx 站点模板：只有一个 `listen ${LAB_BIND}:${LAB_PORT} ssl`；`/api/bench/`、`/api/lab/rt`、`/rerun/` 与
   `/rerun.cloud.v1alpha1.RerunCloudService/` 的上游端口；页面经 `auth_request` 鉴权；其余 `/api/` 与探针 404。
3. 镜像依赖锁定与 requirements.lock、requirements-d3.lock 一致；runtime.yaml 的 d3 进程入口模块存在。
4. labctl.py 的端口表解析、Set-Cookie 属性解析与 TCP 扫描（回环上的监听与未监听端口）。
5. 登录跳转参数（K12）：`@login` 以 njs 计算的 `$lab_next`（encodeURIComponent($request_uri)）拼接，查询串随 next 保留，与前端
   useBenchSession 同一口径；nginx.conf 加载 njs 模块，镜像复制 lab-next.js；函数本身以 node 执行核对。
6. `labctl.sh snapshot`（build 的快照一步，K12）：源码取自已提交的快照（`git archive`），不读工作树与未入库文件；补丁只改其列出的
   文件、标签加补丁摘要；快照外路径的补丁、非空目标目录与取不到提交时拒绝；node_modules 的 @awr/* 指回快照。需要 git（外置 git
   目录时由环境变量 GIT_DIR、GIT_WORK_TREE 给出），否则跳过。
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
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


def test_login_redirect_next_keeps_query() -> None:
    t = (LAB / "nginx/lab.conf.template").read_text(encoding="utf-8")
    assert re.findall(r"location @login \{([^}]*)\}", t) == [" return 302 /lab/login?next=$lab_next; "]
    conf = "\n".join(ln.split("#", 1)[0] for ln in (LAB / "nginx/nginx.conf").read_text(encoding="utf-8").splitlines())
    assert re.search(r"^load_module modules/ngx_http_js_module\.so;", conf, re.M)
    assert "js_import lab from /etc/nginx/lab-next.js;" in conf and "js_set $lab_next lab.next;" in conf
    assert "lab-next.js" in (LAB / "images/web/Dockerfile").read_text(encoding="utf-8")
    assert '"$L/nginx/lab-next.js"' in (LAB / "labctl.sh").read_text(encoding="utf-8")
    node = shutil.which("node")
    if node is None:
        pytest.skip("[dep] node not installed")
    js = (f"const m = await import({json.dumps((LAB / 'nginx/lab-next.js').as_uri())});"
          "const out = ['/lab/runs/run-x?view=eval', '/lab/replay?a=r1&b=r2&t=1.5', '/lab/worlds/%E4%B8%96', ''].map("
          "(u) => m.default.next({ variables: { request_uri: u } })); console.log(JSON.stringify(out))")
    r = subprocess.run([node, "--input-type=module", "-e", js], capture_output=True, text=True, check=True)
    assert json.loads(r.stdout) == ["%2Flab%2Fruns%2Frun-x%3Fview%3Deval", "%2Flab%2Freplay%3Fa%3Dr1%26b%3Dr2%26t%3D1.5",
                                    "%2Flab%2Fworlds%2F%25E4%25B8%2596", "%2Flab%2Fruns"]


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


def _git(*args: str) -> str | None:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False)
    return r.stdout if r.returncode == 0 else None


def _snapshot(dst: Path, patch: Path | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "LAB_REF": "HEAD", "LAB_PATCH": str(patch) if patch else ""}
    return subprocess.run(["bash", str(LAB / "labctl.sh"), "snapshot", str(dst)], capture_output=True, text=True, env=env, check=False)


def _patch_for(rel: str, head_text: str, extra: str) -> str:
    return "".join(difflib.unified_diff(head_text.splitlines(keepends=True), (head_text + extra).splitlines(keepends=True),
                                        fromfile=f"a/{rel}", tofile=f"b/{rel}"))


def test_labctl_snapshot_uses_committed_tree_and_patch(tmp_path: Path) -> None:
    commit = (_git("rev-parse", "--verify", "-q", "HEAD^{commit}") or "").strip()
    if not commit:
        pytest.skip("[dep] git commit not reachable (export GIT_DIR and GIT_WORK_TREE for the external git dir)")
    if not (ROOT / "node_modules").is_dir():
        pytest.skip("[dep] node_modules not installed")
    # 无补丁：标签为提交号前 10 位，源码与 HEAD 一致，未入库的忽略文件不进入快照
    r = _snapshot(tmp_path / "a")
    assert r.returncode == 0, r.stderr
    build = json.loads((tmp_path / "a" / "BUILD.json").read_text(encoding="utf-8"))
    assert (build["schema"], build["commit"], build["tag"], build["patch_sha256"], build["patch_files"], build["web_dirty"]) == \
        ("drone4d.lab.build.v1", commit, commit[:10], None, [], False)
    for rel in ("python/awr/bench/runs_store.py", "apps/web/package.json", "tools/deploy/dmax/lab/labctl.py", "tests/e2e/lab-roles.spec.ts"):
        assert (tmp_path / "a" / rel).read_text(encoding="utf-8") == _git("show", f"HEAD:{rel}"), rel
    assert not (tmp_path / "a" / "apps/web/public/bench").exists()            # .gitignore 下的夹具（禁止再分发）
    assert not (tmp_path / "a" / "docs").exists()
    assert (tmp_path / "a" / "node_modules/@awr/contracts").resolve() == (tmp_path / "a" / "packages/contracts").resolve()
    assert (tmp_path / "a" / "node_modules/.bin").is_dir()
    # 目标目录非空时拒绝
    assert _snapshot(tmp_path / "a").returncode == 2
    # 补丁：只改列出的文件，标签加 sha256 前 8 位；apps/web 内的改动置 web_dirty
    rel = "apps/web/src/main.tsx"
    head = _git("show", f"HEAD:{rel}")
    assert head is not None
    patch = tmp_path / "k.patch"
    patch.write_text(_patch_for(rel, head, "// k12 snapshot test\n"), encoding="utf-8")
    r = _snapshot(tmp_path / "b", patch)
    assert r.returncode == 0, r.stderr
    build = json.loads((tmp_path / "b" / "BUILD.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    assert (build["tag"], build["patch_sha256"], build["patch_files"], build["web_dirty"]) == (f"{commit[:10]}-p{digest[:8]}", digest, [rel], True)
    assert (tmp_path / "b" / rel).read_text(encoding="utf-8") == head + "// k12 snapshot test\n"
    # 快照外路径的补丁失败
    bad = tmp_path / "bad.patch"
    bad.write_text(_patch_for("docs/README.md", _git("show", "HEAD:docs/README.md") or "x\n", "y\n"), encoding="utf-8")
    assert _snapshot(tmp_path / "c", bad).returncode != 0


def test_labctl_snapshot_refuses_without_commit(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    env.update(LAB_REF="refs/heads/k12-no-such-branch", GIT_CEILING_DIRECTORIES=str(ROOT.parent))
    r = subprocess.run(["bash", str(LAB / "labctl.sh"), "snapshot", str(tmp_path / "x")], capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 2 and "not a commit" in r.stderr
    assert not (tmp_path / "x").exists()
