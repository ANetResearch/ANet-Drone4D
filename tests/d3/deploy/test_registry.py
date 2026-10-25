"""d3-registry 与受邀访问部署件（`tools/deploy/dmax/registry/`；AWR-05 §5.6.7、ADR-183、Q-D3-15；AWR-17 §18.23；D3-AC-59）的核查：

1. 鉴权子请求服务 `authz.py`：Basic 与 Bearer 凭据解析；以真实 `BenchAuth` 逐项判定（无凭据与无效令牌 401 带 Basic 质询、本团队
   推送与拉取放行、他队命名空间、`_catalog` 与受邀成员 DELETE 403 115 并审计、清单推送审计）；撤销后下一个请求即 401（≤ 2 s，
   无缓存）；HTTP 服务的状态码、质询头与访问日志不含令牌。
2. nginx 前端模板：只监听 ZeroTier 地址、`/v2/` 逐请求 `auth_request` 且无缓存指令、401/403 映射与 distribution 错误体、
   `Authorization` 不转发后端、模板变量均由 `regctl.sh up` 提供；后端配置只绑回环、无 debug 端口、镜像版本锁定。
3. `regctl.sh up` 在记录桩下渲染（不连接 dmax）：三个容器、CM-01 至 CM-08 无错误级发现、后端与鉴权服务只绑回环、只有前端
   带 ZeroTier 地址、只读根文件系统、`--cap-drop ALL`、不挂 GPU。
4. `regctl.py`：命名空间口径与 `awr.bench.auth` 一致；演练镜像结构自洽；`materialize_once` 在桩 docker 与内存仓库上的各结论
   （拉取、清单摘要、他队与未找到、受邀成员的他队镜像、内部成员本机镜像、终态清理）。
5. `zerotier/ztmember.py`：片段的每条规则只涉及受邀成员；可达矩阵（只放行 dmax 的 18660 与 18670、回程只来自这两个端口、
   受邀成员之间与内部成员之间丢弃、非受邀流量与既有规则相同）；合并替换旧片段；`apply` 只在 `--yes` 与令牌齐备时执行，
   对 Central 接口的请求以本地桩验证。
"""

from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from awr.agentsdk.env.containers import check_launches, parse_docker_run
from awr.bench.auth import BenchAuth, registry_namespace

ROOT = Path(__file__).resolve().parents[3]
REG = ROOT / "tools/deploy/dmax/registry"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def authz():
    return _load("registry_authz", REG / "authz.py")


@pytest.fixture(scope="module")
def regctl():
    return _load("registry_regctl", REG / "regctl.py")


@pytest.fixture(scope="module")
def zt():
    return _load("ztmember", REG / "zerotier/ztmember.py")


@pytest.fixture
def bench(tmp_path):
    auth = BenchAuth(tmp_path / "keys", tmp_path / "registry-authz.sqlite")
    tid_a, tok_a = auth.issue_token("alice", "invited", ("team-a",))
    tid_b, tok_b = auth.issue_token("bob", "invited", ("team-b",))
    _, tok_z = auth.issue_token("root", "admin", ("ops",))
    pa = auth.sign_push(auth.verify_token(tok_a))[0]
    pz = auth.sign_push(auth.verify_token(tok_z))[0]
    return {"auth": auth, "tid_a": tid_a, "pa": pa, "pz": pz, "tid_b": tid_b, "tok_b": tok_b}


def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def _h(method: str, uri: str, auth: str | None = None) -> dict:
    h = {"X-Original-Method": method, "X-Original-URI": uri, "X-Real-IP": "10.253.20.200"}
    if auth:
        h["Authorization"] = auth
    return h


# ── 1. authz.py ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_credential_parsing(authz) -> None:
    assert authz.credential(_basic("alice", "d3st_v1.x.y")) == "d3st_v1.x.y"
    assert authz.credential(_basic("alice", "pa:ss")) == "pa:ss"                       # 口令可含冒号
    assert authz.credential("Bearer d3st_v1.a.b") == "d3st_v1.a.b"
    for bad in ("", "Basic", "Basic !!!", _basic("alice", ""), "Basic " + base64.b64encode(b"nocolon").decode(), "Digest x"):
        assert authz.credential(bad) is None


def test_decide_scope_matrix_and_audit(authz, bench) -> None:
    auth, pa, pz = bench["auth"], bench["pa"], bench["pz"]
    a = _basic("alice", pa)
    cases = [
        (_h("GET", "/v2/"), 401),                                                       # 首个握手不带凭据
        (_h("GET", "/v2/", _basic("alice", "wrong")), 401),
        (_h("GET", "/v2/", a), 204),
        (_h("POST", "/v2/team-a/agent/blobs/uploads/", a), 204),
        (_h("PATCH", "/v2/team-a/agent/blobs/uploads/0f3c?_state=abc", a), 204),
        (_h("PUT", "/v2/team-a/agent/manifests/v1", a), 204),
        (_h("HEAD", "/v2/team-a/agent/manifests/v1", a), 204),
        (_h("GET", "/v2/team-a/agent/tags/list", a), 204),
        (_h("POST", "/v2/team-b/agent/blobs/uploads/", a), 403),
        (_h("GET", "/v2/team-b/agent/manifests/v1", a), 403),
        (_h("POST", "/v2/team-a-x/agent/blobs/uploads/", a), 403),                       # 前缀碰撞
        (_h("GET", "/v2/team-a/../team-b/agent/manifests/v1", a), 403),                  # 原样 URI 中的点段不是合法仓库名
        (_h("GET", "/v2/_catalog", a), 403),
        (_h("DELETE", "/v2/team-a/agent/manifests/sha256:" + "0" * 64, a), 403),         # DELETE 仅管理员
        (_h("GET", "/healthz", a), 403),                                                # 非 /v2 子请求
        (_h("DELETE", "/v2/team-b/agent/manifests/sha256:" + "0" * 64, _basic("root", pz)), 204),
        (_h("GET", "/v2/_catalog", "Bearer " + pz), 204),
    ]
    for headers, want in cases:
        status, hdrs, log = authz.decide(auth, headers)
        assert status == want, (headers["X-Original-Method"], headers["X-Original-URI"], status, log)
        assert (hdrs.get("WWW-Authenticate") == 'Basic realm="d3-registry"') == (status == 401)
        assert "d3st_v1." not in json.dumps(log)
    rows = auth.audit.read()
    deny = [r for r in rows if r["action"] == "deny" and r.get("route") == "registry"]
    assert sum(r.get("code") == 115 and r.get("principal") == "alice" for r in deny) == 6
    assert any(r.get("code") == 302 and r.get("why") == "push_token" for r in deny)       # 错误口令也写审计
    assert any(r["action"] == "registry_push" and r.get("repository") == "team-a/agent" and r.get("reference") == "v1" for r in rows)
    text = auth.audit.path_for().read_text(encoding="utf-8")
    assert "d3st_v1." not in text and "d3bt_" not in text


def test_revocation_takes_effect_on_next_subrequest(authz, bench) -> None:
    auth, pa = bench["auth"], bench["pa"]
    h = _h("HEAD", "/v2/team-a/agent/manifests/v1", _basic("alice", pa))
    assert authz.decide(auth, h)[0] == 204
    other = BenchAuth(auth.keys_dir, auth.keys_dir.parent / "bench.sqlite")              # 撤销来自另一进程（bench ctl 或 bench-api）
    t0 = time.monotonic()
    assert other.revoke_token(bench["tid_a"])
    status, hdrs, _ = authz.decide(auth, h)
    dt_s = time.monotonic() - t0
    assert status == 401 and hdrs["WWW-Authenticate"] == 'Basic realm="d3-registry"'
    assert dt_s <= 2.0                                                                  # D3-AC-59；无缓存，实际为毫秒级


def test_expired_invited_token_is_rejected(authz, tmp_path) -> None:
    now = [1_800_000_000.0]
    auth = BenchAuth(tmp_path / "keys", tmp_path / "a.sqlite", clock=lambda: now[0])
    _, tok = auth.issue_token("carol", "invited", ("team-c",), ttl_s=3600)
    push = auth.sign_push(auth.verify_token(tok))[0]
    h = _h("GET", "/v2/team-c/x/tags/list", _basic("carol", push))
    assert authz.decide(auth, h)[0] == 204
    now[0] += 3601                                                                      # bench token 到期（推送令牌本身尚未到期也随之失效）
    assert authz.decide(auth, h)[0] == 401


def test_http_server_status_headers_and_log(authz, bench, capsys) -> None:
    srv = authz.make_server(bench["auth"], "127.0.0.1", 0)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path: str, headers: dict) -> tuple[int, dict]:
        req = urllib.request.Request(base + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)

    try:
        assert call("/healthz", {})[0] == 200
        assert call("/other", {})[0] == 404
        st, h = call("/authz", _h("GET", "/v2/"))
        assert st == 401 and h["WWW-Authenticate"] == 'Basic realm="d3-registry"' and h["Cache-Control"] == "no-store"
        st, h = call("/authz", _h("PUT", "/v2/team-a/agent/manifests/v2?x=1", _basic("alice", bench["pa"])))
        assert st == 204 and h["X-D3-Principal"] == "alice invited"
        assert call("/authz", _h("GET", "/v2/_catalog", _basic("alice", bench["pa"])))[0] == 403
    finally:
        srv.shutdown()
        srv.server_close()
    out = capsys.readouterr().out
    lines = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
    assert [x["status"] for x in lines] == [401, 204, 403]
    assert lines[1]["path"] == "/v2/team-a/agent/manifests/v2"                            # 查询串不入日志
    assert "d3st_v1." not in out and "Basic " not in out


def test_authz_main_refuses_non_loopback(authz) -> None:
    assert authz.main(["--host", "0.0.0.0", "--port", "1"]) == 2


# ── 2. nginx 与后端配置 ─────────────────────────────────────────────────────────────────────────────────────────────

def _strip(text: str) -> str:
    return "\n".join(ln.split("#", 1)[0] for ln in text.splitlines())


def test_front_template_routes_and_no_auth_cache() -> None:
    t = (REG / "nginx/registry.conf.template").read_text(encoding="utf-8")
    body = _strip(t)
    assert re.findall(r"^\s*listen\s+([^;]+);", body, re.M) == ["${REG_BIND}:${REG_PORT} ssl"]
    v2 = body[body.index("location /v2/"):body.index("location @unauthorized")]
    assert "auth_request /_authz;" in v2 and "proxy_pass http://127.0.0.1:${REG_BACKEND_PORT};" in v2
    assert 'proxy_set_header Authorization "";' in v2                                  # 令牌不到后端
    assert "error_page 401 = @unauthorized;" in v2 and "error_page 403 = @denied;" in v2
    assert "proxy_request_buffering off;" in v2
    authz_loc = body[body.index("location = /_authz"):body.index("location /v2/")]
    assert "internal;" in authz_loc and "proxy_pass http://127.0.0.1:${REG_AUTHZ_PORT}/authz;" in authz_loc
    assert "X-Original-URI $request_uri;" in authz_loc and "X-Original-Method $request_method;" in authz_loc
    assert "proxy_pass_request_body off;" in authz_loc
    for word in ("proxy_cache", "proxy_store", "auth_request_cache", "fastcgi_cache", "expires"):
        assert word not in body                                                         # 鉴权结果不缓存（撤销 ≤ 2 s）
    unauth = body[body.index("location @unauthorized"):body.index("location @denied")]
    assert "return 401" in unauth and '"UNAUTHORIZED"' in unauth
    assert "WWW-Authenticate" not in unauth                                             # 质询头只由鉴权服务给出（auth_request 带回），避免重复
    assert '"DENIED"' in body[body.index("location @denied"):] and "client_max_body_size 4g;" in body
    assert re.search(r"location / \{\s*return 404;", body)
    used = set(re.findall(r"\$\{([A-Z_]+)\}", t))
    up = (REG / "regctl.sh").read_text(encoding="utf-8")
    up = up[up.index("  up)"):up.index("  down)")]
    for var in used:                                                                    # 模板变量均由 up 以 -e 传入，且在 envsubst 过滤范围内
        assert f"-e {var}=" in up and re.match(r"^(REG|LAB)_", var)
    assert "NGINX_ENVSUBST_FILTER='^(REG|LAB)_'" in up


def test_nginx_main_log_has_no_credentials() -> None:
    t = _strip((REG / "nginx/nginx.conf").read_text(encoding="utf-8"))
    fmt = re.search(r"log_format registry '([^']+)'", t).group(1)
    assert "$uri" in fmt and "$request_uri" not in fmt and "$args" not in fmt and "authorization" not in fmt.lower()


def test_backend_config_loopback_only() -> None:
    cfg = yaml.safe_load((REG / "config.yml").read_text(encoding="utf-8"))
    assert cfg["http"]["addr"] == "127.0.0.1:18671" and "debug" not in cfg["http"] and "auth" not in cfg
    assert cfg["storage"]["delete"]["enabled"] is True and cfg["storage"]["filesystem"]["rootdirectory"] == "/var/lib/registry"
    env = (REG / "registry.env").read_text(encoding="utf-8")
    img = re.search(r"REG_IMAGE=\$\{REG_IMAGE:-([^}]+)\}", env).group(1)
    assert re.fullmatch(r"registry:\d+\.\d+\.\d+", img)                                  # 版本锁定，不用 latest


# ── 3. regctl.sh up 渲染 ───────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rendered():
    audit = _load("audit_manifests", ROOT / "tools/deploy/dmax/audit_manifests.py")
    code, calls, err = audit.render_shell(REG / "regctl.sh", ("up",))
    assert code == 0, err
    return [c for c in calls if c[:1] == ["run"]]


def _opt(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


def test_up_renders_three_containers_without_manifest_errors(rendered) -> None:
    launches = [parse_docker_run(["docker", *c], "sh:registry/regctl.sh") for c in rendered]
    assert sorted(lc.name for lc in launches) == ["d3-registry", "d3-registry-authz", "d3-registry-front"]
    assert [f for f in check_launches(launches) if f.level == "error"] == []
    for lc in launches:
        assert lc.gpus == [] and lc.devices == [] and lc.runtime is None
        assert lc.cgroup_parent == "d3.slice" and lc.memory and lc.cpus
        assert lc.labels.get("d3.component") == "registry" and lc.labels.get("d3.profile") == "d3"


def test_bindings_and_hardening(rendered) -> None:
    by = {_opt(c, "--name")[0]: c for c in rendered}
    for c in by.values():
        assert _opt(c, "--network") == ["host"] and "--read-only" in c and "ALL" in _opt(c, "--cap-drop")
        assert "no-new-privileges" in _opt(c, "--security-opt")
    reg, az, fr = by["d3-registry"], by["d3-registry-authz"], by["d3-registry-front"]
    assert "REGISTRY_HTTP_ADDR=127.0.0.1:18671" in _opt(reg, "-e") and _opt(reg, "--user") == ["10001:10001"]
    i = az.index("--host")
    assert az[i:i + 4] == ["--host", "127.0.0.1", "--port", "18672"] and _opt(az, "--user") == ["10001:10001"]
    assert "10.253.20.5" not in " ".join(reg + az)                                      # 只有前端带 ZeroTier 地址
    env = _opt(fr, "-e")
    assert "REG_BIND=10.253.20.5" in env and "REG_PORT=18670" in env
    assert set(_opt(fr, "--cap-add")) == {"CHOWN", "SETUID", "SETGID"}
    assert any(v.endswith(":/etc/nginx/tls:ro") for v in _opt(fr, "-v"))


# ── 4. regctl.py ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_namespace_rule_matches_auth(regctl) -> None:
    for team in ("team-a", "Team_A", "ANet Lab 2", "b25-drill-a", "x..y", "--z--"):
        assert regctl.registry_namespace(team) == registry_namespace(team)
    with pytest.raises(ValueError):
        regctl.registry_namespace("___")


def test_drill_image_is_self_consistent(regctl) -> None:
    img = regctl.drill_image("b25-drill-a")
    man, cfg = json.loads(img["manifest"]), json.loads(img["config"])
    assert man["config"]["digest"] == img["config_digest"] == "sha256:" + hashlib.sha256(img["config"]).hexdigest()
    assert man["layers"][0]["digest"] == img["layer_digest"] and man["layers"][0]["size"] == len(img["layer"])
    tar = gzip.decompress(img["layer"])
    assert cfg["rootfs"]["diff_ids"] == ["sha256:" + hashlib.sha256(tar).hexdigest()]
    assert (cfg["os"], cfg["architecture"]) == ("linux", "amd64")
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        assert tf.getnames() == ["d3-registry-drill.txt"]
    assert regctl.drill_image("b25-drill-b")["config_digest"] != img["config_digest"]


class _FakeRegistry(BaseHTTPRequestHandler):
    """内存仓库：`repos = {repo: {tag: manifest_bytes}}`，清单按摘要与标签读取。"""
    repos: ClassVar[dict] = {}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/v2/_catalog":
            return self._send(200, json.dumps({"repositories": sorted(self.repos)}).encode())
        m = re.match(r"^/v2/(.+)/(tags/list|manifests/(.+))$", path)
        if not m or m.group(1) not in self.repos:
            return self._send(404, b"{}")
        tags = self.repos[m.group(1)]
        if m.group(2) == "tags/list":
            return self._send(200, json.dumps({"name": m.group(1), "tags": sorted(tags)}).encode())
        ref = m.group(3)
        body = tags.get(ref) or next((b for b in tags.values() if "sha256:" + hashlib.sha256(b).hexdigest() == ref), None)
        if body is None:
            return self._send(404, b"{}")
        return self._send(200, body, {"Docker-Content-Digest": "sha256:" + hashlib.sha256(body).hexdigest()})

    def _send(self, st: int, body: bytes, extra: dict | None = None) -> None:
        self.send_response(st)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a) -> None:
        return


_DOCKER_STUB = """#!{py}
import json, os, sys
st_path = os.environ["FAKE_DOCKER_STATE"]
st = json.load(open(st_path))
a = sys.argv[1:]
def save():
    json.dump(st, open(st_path, "w"))
if a[:2] == ["image", "inspect"]:
    ref = a[2]
    img = st["images"].get(ref) or next((v for k, v in st["images"].items() if ref in v.get("refs", [])), None)
    if img is None:
        sys.exit(1)
    print(json.dumps({{"Id": img["id"], "Os": "linux", "Architecture": "amd64", "Size": 1234}}))
elif a[0] == "pull":
    ref = a[-1]
    pulls = st["pullable"]
    if ref not in pulls:
        sys.exit(1)
    cfg = pulls[ref]
    st["images"][cfg] = {{"id": cfg, "refs": [ref]}}
    st["log"].append(["pull", ref]); save()
elif a[:2] == ["image", "rm"]:
    st["log"].append(["rm", a[2]]); save()
"""


def _manifest(cfg_digest: str, label: str) -> bytes:
    return json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                       "config": {"mediaType": "application/vnd.docker.container.image.v1+json", "size": 1, "digest": cfg_digest},
                       "layers": [], "x": label}).encode()


def _sub(root: Path, sid: str, team: str, digest: str, state: str = "RECEIVED", role: str = "invited") -> None:
    (root / "submissions").mkdir(parents=True, exist_ok=True)
    (root / "submissions" / f"{sid}.json").write_text(json.dumps({"submission_id": sid, "state": state, "image_digest": digest,
                                                                   "_meta": {"team": team, "role": role}}), encoding="utf-8")


def test_materialize_outcomes(regctl, tmp_path, monkeypatch) -> None:
    cfg_a, cfg_b, cfg_local, cfg_x, cfg_y = ("sha256:" + c * 64 for c in "abcde")
    man_a, man_b = _manifest(cfg_a, "a"), _manifest(cfg_b, "b")
    dig_man_b = "sha256:" + hashlib.sha256(man_b).hexdigest()
    _FakeRegistry.repos = {"team-a/agent": {"v1": man_a}, "team-b/agent": {"v1": man_b}, "team-a-x/agent": {"v1": _manifest(cfg_y, "x")}}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeRegistry)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    host = f"127.0.0.1:{srv.server_address[1]}"
    stub = tmp_path / "docker"
    stub.write_text(_DOCKER_STUB.format(py=sys.executable), encoding="utf-8")
    stub.chmod(0o755)
    state = tmp_path / "docker-state.json"
    state.write_text(json.dumps({"images": {cfg_local: {"id": cfg_local, "refs": []}},
                                 "pullable": {f"{host}/team-a/agent@sha256:{hashlib.sha256(man_a).hexdigest()}": cfg_a,
                                              f"{host}/team-b/agent@{dig_man_b}": cfg_b},
                                 "log": []}), encoding="utf-8")
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    root = tmp_path / "data"
    _sub(root, "sub-1", "team-a", cfg_a)                           # 配置摘要 → 拉取
    _sub(root, "sub-2", "team-b", dig_man_b)                       # 清单摘要 → overlay2 宿主上 inspect 不到
    _sub(root, "sub-3", "team-c", cfg_x)                           # 不存在的镜像
    _sub(root, "sub-4", "team-b", cfg_a)                           # 受邀成员引用已被拉取的他队镜像 → foreign_image
    _sub(root, "sub-5", "ops", cfg_local, role="researcher")       # 内部成员的宿主本机镜像
    _sub(root, "sub-6", "team-a", cfg_y)                           # 只在前缀碰撞仓库 team-a-x 中：不算 team-a
    _sub(root, "sub-7", "team-a", cfg_a, state="DONE")             # 非 RECEIVED 不处理
    _sub(root, "sub-8", "ops", cfg_a, role="researcher")           # 内部成员引用宿主已有的镜像：present（不在其命名空间）
    try:
        rep = regctl.materialize_once(root, regctl.Registry(host), docker=str(stub))
    finally:
        srv.shutdown()
        srv.server_close()
    got = {i["submission_id"]: i["status"] for i in rep["items"]}
    assert got == {"sub-1": "pulled", "sub-2": "digest_is_manifest", "sub-3": "not_found", "sub-4": "foreign_image", "sub-5": "present",
                   "sub-6": "not_found", "sub-8": "present"}
    rec = json.loads((root / "submissions/sub-1/registry.json").read_text(encoding="utf-8"))
    assert rec["repository"] == "team-a/agent" and rec["matched"] == "config" and rec["namespace"] == "team-a" and rec["in_namespace"]
    rec2 = json.loads((root / "submissions/sub-2/registry.json").read_text(encoding="utf-8"))
    assert rec2["matched"] == "manifest" and rec2["config_digest"] == cfg_b and rec2["local_image_id"] == cfg_b
    log = json.loads(state.read_text(encoding="utf-8"))["log"]
    assert [x[0] for x in log] == ["pull", "pull"]
    assert sorted(p.name for p in (root / "submissions").glob("sub-*.json")) == [f"sub-{k}.json" for k in range(1, 9)]   # 不新增 sub-*.json

    # 终态清理：sub-1 被拒绝后 --prune 删除其拉取的镜像，只删一次
    doc = json.loads((root / "submissions/sub-1.json").read_text(encoding="utf-8"))
    doc["state"] = "REJECTED"
    (root / "submissions/sub-1.json").write_text(json.dumps(doc), encoding="utf-8")
    srv2 = ThreadingHTTPServer(("127.0.0.1", int(host.rsplit(":", 1)[1])), _FakeRegistry)
    th2 = threading.Thread(target=srv2.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th2.start()
    try:
        r1 = regctl.materialize_once(root, regctl.Registry(host), docker=str(stub), prune=True)
        r2 = regctl.materialize_once(root, regctl.Registry(host), docker=str(stub), prune=True)
    finally:
        srv2.shutdown()
        srv2.server_close()
    assert next(i for i in r1["items"] if i.get("action") == "pruned")["submission_id"] == "sub-1"
    assert not [i for i in r2["items"] if i.get("action") == "pruned"]
    assert [x[0] for x in json.loads(state.read_text(encoding="utf-8"))["log"]].count("rm") == 1


# ── 5. ZeroTier 受邀成员网络策略 ──────────────────────────────────────────────────────────────────────────────────────

TODAY = dt.date(2026, 10, 19)
MEMBERS = {"members": [{"node": "a1b2c3d4e5", "team": "team-a", "expires": "2027-01-15"},
                       {"node": "0f0f0f0f0f", "team": "team-b", "expires": "2027-01-15"},
                       {"node": "1234567890", "team": "team-old", "expires": "2026-10-01"}]}
EXISTING = "# chatchat-01 existing rules\ndrop\n  not ethertype ipv4\n  and not ethertype arp\n  and not ethertype ipv6\n;\naccept;\n"


@pytest.fixture
def members(zt, tmp_path):
    p = tmp_path / "members.json"
    p.write_text(json.dumps(MEMBERS), encoding="utf-8")
    return zt.load_members(p)


def test_fragment_only_touches_invited_members(zt, members) -> None:
    frag = zt.render(members, today=TODAY)
    assert frag.startswith(zt.BEGIN) and frag.rstrip().endswith(zt.END)
    rules = zt.parse_rules(frag)
    nodes = {m.node for m in members}
    for r in rules:                                                                     # 每条规则都以某个受邀成员为源或目的
        assert any(c.key in ("ztsrc", "ztdest") and c.value in nodes and not c.negate for c in r.conds), r.text
    old = [r for r in rules if any(c.value == "1234567890" for c in r.conds)]
    assert [r.action for r in old] == ["drop", "drop"]                                  # 到期成员不放行
    assert "EXPIRED" in frag
    accepts = [r for r in rules if r.action == "accept" and any(c.key == "dport" for c in r.conds)]
    assert sorted({c.value for r in accepts for c in r.conds if c.key == "dport"}) == [(18660, 18660), (18670, 18670)]


def test_policy_matrix_with_existing_rules(zt, members) -> None:
    src = zt.merge(EXISTING, zt.render(members, today=TODAY))
    open_ports = [22, 8000, 8765, 9993, 18660, 18670, 19000, 28650]
    rep = zt.check(src, members, open_ports=open_ports, today=TODAY)
    assert rep["ok"], [c for c in rep["checks"] if not c["ok"]][:5]
    assert rep["invited_reachable_open_ports"] == {"a1b2c3d4e5": [18660, 18670], "0f0f0f0f0f": [18660, 18670], "1234567890": []}
    unchanged = [c for c in rep["checks"] if c["check"].startswith("non-invited")]
    assert unchanged and all(c["want"] == "accept" or "eth=0x" in c["check"] for c in unchanged)


def test_policy_regression_detected(zt, members) -> None:
    frag = zt.render(members, today=TODAY).replace("dport 18670;", "dport 18670;\naccept ztsrc a1b2c3d4e5 and dport 22;", 1)
    rep = zt.check(frag + "accept;\n", members, open_ports=[22, 18660, 18670], today=TODAY)
    assert not rep["ok"] and rep["invited_reachable_open_ports"]["a1b2c3d4e5"] == [22, 18660, 18670]


def test_evaluate_semantics(zt) -> None:
    rules = zt.parse_rules("tag x\n id 1\n;\naccept ztsrc 00000000aa or ztsrc 00000000bb and dport 80;\ndrop not ethertype arp;\naccept;")
    F = zt.Flow
    assert zt.evaluate(rules, F("00000000aa", "00000000cc", dport=80))[0] == "accept"
    assert zt.evaluate(rules, F("00000000bb", "00000000cc", dport=80))[0] == "accept"
    assert zt.evaluate(rules, F("00000000bb", "00000000cc", dport=81))[0] == "drop"
    assert zt.evaluate(rules, F("00000000cc", "00000000aa", ethertype=0x0806, proto=None)) == ("accept", 2)
    assert zt.evaluate(zt.parse_rules("drop ztsrc 00000000aa;"), F("00000000bb", "00000000aa")) == ("drop", None)   # 末尾隐式丢弃
    for bad in ("break;", "accept chr fragment;", "accept and ztsrc 00000000aa;", "accept ztsrc zz;", "accept dport;"):
        with pytest.raises(ValueError):
            zt.parse_rules(bad)


def test_merge_replaces_and_prepends(zt, members) -> None:
    f1 = zt.render(members[:1], today=TODAY)
    f2 = zt.render(members, today=TODAY)
    m1 = zt.merge(EXISTING, f1)
    assert m1.startswith(zt.BEGIN) and m1.endswith(EXISTING)
    m2 = zt.merge(m1, f2)
    assert m2.count(zt.BEGIN) == 1 and m2.endswith(EXISTING) and "0f0f0f0f0f" in m2
    assert zt.merge(m2, f2) == m2                                                      # 幂等
    assert zt.merge("", f1).endswith("accept;\n")
    with pytest.raises(ValueError):
        zt.merge(zt.BEGIN + "\naccept;\n", f1)


def test_members_file_validation(zt, tmp_path) -> None:
    p = tmp_path / "m.json"
    for doc in ({"members": [{"node": "xyz", "team": "t"}]}, {"members": [{"node": "a1b2c3d4e5", "team": "t", "expires": "2027-13-01"}]},
                {"members": [{"node": "a1b2c3d4e5", "team": "t"}, {"node": "A1B2C3D4E5", "team": "u"}]}):
        p.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(ValueError):
            zt.load_members(p)


class _FakeCentral(BaseHTTPRequestHandler):
    calls: ClassVar[list] = []
    rules = EXISTING

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self) -> None:
        type(self).calls.append(("GET", self.path, self.headers.get("Authorization"), None))
        self._send({"id": "ebe7fbd445ade5dd", "rulesSource": type(self).rules})

    def do_POST(self) -> None:
        b = self._body()
        type(self).calls.append(("POST", self.path, self.headers.get("Authorization"), b))
        if "rulesSource" in b:
            type(self).rules = b["rulesSource"]
        self._send({})

    def _send(self, doc: dict) -> None:
        body = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a) -> None:
        return


def test_plan_and_apply_against_stub_central(zt, members, tmp_path, monkeypatch, capsys) -> None:
    pl = zt.plan("ebe7fbd445ade5dd", members, EXISTING, today=TODAY)
    assert "token" not in json.dumps(pl["requests"]).lower()
    assert [r["body"]["config"]["authorized"] for r in pl["requests"][1:]] == [True, True, False]
    _FakeCentral.calls, _FakeCentral.rules = [], EXISTING
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCentral)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    try:
        rep = zt.apply("ebe7fbd445ade5dd", members, "stub-admin-token", base=f"http://127.0.0.1:{srv.server_address[1]}", today=TODAY)
    finally:
        srv.shutdown()
        srv.server_close()
    assert [c[0] for c in _FakeCentral.calls] == ["GET", "POST", "POST", "POST", "POST"]
    assert all(c[2] == "token stub-admin-token" for c in _FakeCentral.calls)
    assert _FakeCentral.rules.startswith(zt.BEGIN) and _FakeCentral.rules.endswith(EXISTING)
    assert [m["authorized"] for m in rep["members"]] == [True, True, False] and "stub-admin-token" not in json.dumps(rep)
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(MEMBERS), encoding="utf-8")
    monkeypatch.delenv("ZT_CENTRAL_TOKEN", raising=False)
    assert zt.main(["apply", "--members", str(mp)]) == 2                               # 缺 --yes
    assert zt.main(["apply", "--members", str(mp), "--yes"]) == 2                      # 缺令牌
    assert zt.main(["check", "--members", str(mp), "--out", str(tmp_path / "c.json")]) == 0
    assert json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))["schema"] == "awr.d3.zt_policy_check.v1"
    capsys.readouterr()


def test_no_secrets_or_emoji_in_registry_files() -> None:
    for p in REG.rglob("*"):
        if p.is_file() and p.suffix in (".py", ".sh", ".env", ".yml", ".conf", ".template", ".rules"):
            t = p.read_text(encoding="utf-8")
            assert not re.search(r"d3bt_[A-Za-z0-9_-]{20,}|d3st_v1\.[A-Za-z0-9_-]{20,}", t), p
            assert not re.search("[" + chr(0x1F300) + "-" + chr(0x1FAFF) + chr(0x2600) + "-" + chr(0x27BF) + "]", t), p
    assert os.access(REG / "regctl.sh", os.R_OK)
