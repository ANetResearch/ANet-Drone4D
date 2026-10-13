"""部署套件：install.sh 的本机模拟、内部身份口令文件、世界包、沙盒冒烟与发布扫描 REL-04（M00，D2 WP-16；M17-FR-064、
M17-AC-020、D2-AC-24、D2-AC-40、D2-AC-51 的部署部分；AWR-04 §4.4.7、§15.4、§15.9.5；ADR-109、ADR-118、ADR-123）。

- install.sh `--dry-run`：不需要 root、不改系统；校验部署包（MANIFEST、演示构建、build-info 与 RELEASE 一致、单元文件委派）、
  口令文件（单行、可打印 ASCII、无空白、≥ 8；口令从不出现在任何输出中）、世界包（解到临时目录后按发布的代码 deep 校验）；
- worldpack.py：以合成小世界（`awr.world.geometry.fake`）打包、校验、安装、幂等、旧派生缓存条目清理、篡改与越界成员拒绝；
- smoke.py：对一个桩 sandbox-api 跑通过、满员排队、需要工作量证明与失败四种结局；
- check-release.mjs REL-04：植入随机口令的临时文件被报告，口令不出现在输出中；
- build-local.sh：真实演示构建（不打世界包）+ install.sh --dry-run；有 synthcity 时另跑带世界包的完整模拟（needs_data）。
测试口令一律在运行时随机生成（AWR-04 §15.9.5 第 2 条）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EMAX = ROOT / "tools" / "deploy" / "emax"
INSTALL = EMAX / "install.sh"
SCRIPTS = sorted(EMAX.glob("*.sh"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, EMAX / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


worldpack = _load("worldpack")
smoke = _load("smoke")


def _env(**kw: str) -> dict[str, str]:
    e = {k: v for k, v in os.environ.items() if not k.startswith(("AWR_", "VITE_AWR_"))}
    e.update(kw)
    return e


def _manifest(b: Path) -> None:
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  ./{p.relative_to(b).as_posix()}"
             for p in sorted(b.rglob("*")) if p.is_file() and p.name != "MANIFEST.sha256"]
    (b / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")


def fake_bundle(tmp: Path, *, release: str = "20261005-120000-nogit", bi_release: str | None = None, landing: bool = True,
                bench: bool = False, delegate: bool = True, with_code: bool = False) -> Path:
    b = tmp / f"anet-drone4d-{release}"
    (b / "apps" / "web" / "dist").mkdir(parents=True)
    shutil.copytree(EMAX, b / "tools" / "deploy" / "emax", ignore=shutil.ignore_patterns("__pycache__"))
    for s in (b / "tools" / "deploy" / "emax").glob("*.sh"):
        s.chmod(0o755)
    if not delegate:
        unit = b / "tools" / "deploy" / "emax" / "anet-drone4d.service"
        unit.write_text(unit.read_text().replace("Delegate=yes", "#Delegate"))
    (b / "apps" / "web" / "dist" / "index.html").write_text("<html data-landing>\n" if landing else "<html>\n")
    (b / "apps" / "web" / "dist" / "build-info.json").write_text(json.dumps({"release": bi_release or release}, indent=1) + "\n")
    if bench:
        (b / "apps" / "web" / "dist" / "bench").mkdir()
    (b / "requirements.lock").write_text("numpy==2.5.3\n")
    (b / "RELEASE").write_text(f"release={release}\nversion=0.2.0\nchannel=demo\ncommit=nogit\ndirty=false\n")
    if with_code:  # 世界的 deep 校验按"发布的代码"执行：python、契约、配置与剧本（zones）
        for d in ("python", "configs", "scenarios"):
            shutil.copytree(ROOT / d, b / d, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "packages" / "contracts", b / "packages" / "contracts",
                        ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    _manifest(b)
    return b


def dry_run(bundle: Path, *args: str, stdin: str | None = None, tmp: Path | None = None) -> subprocess.CompletedProcess:
    env = _env(AWR_DRY_PYTHON=sys.executable, **({"TMPDIR": str(tmp)} if tmp else {}))
    return subprocess.run(["bash", str(INSTALL), "--dry-run", "--bundle", str(bundle), *args], input=stdin, env=env,
                          capture_output=True, text=True, timeout=600)


# ---------------------------------------------------------------- 脚本与包形态


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_are_executable_bash(script):
    assert script.stat().st_mode & stat.S_IXUSR, f"{script.name} 缺少可执行位"
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_help():
    p = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True)
    assert p.returncode == 0 and "--admin-secret-file" in p.stdout and "--dry-run" in p.stdout


def test_install_without_dry_run_requires_root(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("以 root 运行时不检查")
    p = subprocess.run(["bash", str(INSTALL), "--bundle", str(fake_bundle(tmp_path))], capture_output=True, text=True, env=_env())
    assert p.returncode == 1 and "root" in p.stderr


def test_dry_run_valid_bundle(tmp_path):
    p = dry_run(fake_bundle(tmp_path))
    assert p.returncode == 0, p.stderr
    assert "dry-run 通过" in p.stdout and "Delegate=yes" in p.stdout


@pytest.mark.parametrize(("kw", "why"), [
    ({"landing": False}, "演示构建"),
    ({"bench": True}, "dist/bench"),
    ({"bi_release": "20261005-130000-nogit"}, "build-info.json"),
    ({"release": "latest"}, "格式"),
    ({"delegate": False}, "Delegate=yes"),
])
def test_dry_run_rejects_bad_bundles(tmp_path, kw, why):
    p = dry_run(fake_bundle(tmp_path, **kw))
    assert p.returncode == 1 and why in p.stderr, p.stderr


def test_dry_run_detects_tampering(tmp_path):
    b = fake_bundle(tmp_path)
    (b / "requirements.lock").write_text("numpy==9.9.9\n")
    p = dry_run(b)
    assert p.returncode == 1 and "MANIFEST.sha256" in p.stderr


def test_limits_validation(tmp_path):
    b = fake_bundle(tmp_path)
    assert dry_run(b, "--cpu-quota", "2000%").returncode == 1
    assert dry_run(b, "--memory-high", "lots").returncode == 1
    assert dry_run(b, "--reset-limits", "--cpu-quota", "200%").returncode == 1
    p = dry_run(b, "--cpu-quota", "200%", "--memory-high", "1.6G")
    assert p.returncode == 0 and "CPUQuota=200% MemoryHigh=1.6G" in p.stdout


# ---------------------------------------------------------------- 内部身份口令文件（ADR-123）


def _secret() -> str:
    return "Ts" + secrets.token_urlsafe(18)


def test_admin_secret_file_accepted_and_never_printed(tmp_path):
    b = fake_bundle(tmp_path)
    s = _secret()
    f = tmp_path / "secret.txt"
    f.write_text(s + "\n")
    f.chmod(0o600)
    for args, stdin in ((["--admin-secret-file", str(f)], None), (["--admin-secret-file", "-"], s + "\n")):
        p = dry_run(b, *args, stdin=stdin)
        assert p.returncode == 0, p.stderr
        assert "口令校验通过" in p.stdout
        assert s not in p.stdout and s not in p.stderr


@pytest.mark.parametrize(("content", "why"), [
    ("Ab1-xyz", "长度"),                             # 7 个字符
    ("has space in it", "可打印 ASCII"),
    ("tab\there12345", "可打印 ASCII"),
    ("line1-abcdef\nline2-abcdef\n", "单行"),
    ("café-secret-1", "可打印 ASCII"),
    ("", "长度"),
])
def test_admin_secret_file_rejected_without_echo(tmp_path, content, why):
    b = fake_bundle(tmp_path)
    f = tmp_path / "secret.txt"
    f.write_text(content, encoding="utf-8")
    p = dry_run(b, "--admin-secret-file", str(f))
    assert p.returncode == 1 and why in p.stderr, p.stderr
    for part in [x for x in content.split() if len(x) >= 4]:
        assert part not in p.stdout and part not in p.stderr


def test_secret_only_requires_a_secret(tmp_path):
    p = subprocess.run(["bash", str(INSTALL), "--secret-only"], capture_output=True, text=True, env=_env())
    assert p.returncode == 1


def test_admin_secret_options_are_exclusive(tmp_path):
    f = tmp_path / "s"
    f.write_text(_secret())
    p = dry_run(fake_bundle(tmp_path), "--admin-secret", "--admin-secret-file", str(f))
    assert p.returncode == 1 and "只能取其一" in p.stderr


def test_secret_never_reaches_a_command_line():
    """口令只在 shell 变量与管道里流动：install.sh 用内建 printf 写文件，不把 $SECRET 作为外部命令的参数。"""
    text = INSTALL.read_text(encoding="utf-8")
    uses = [ln.strip() for ln in text.splitlines() if re.search(r"\$\{?SECRET\b", ln) or re.match(r"\s*SECRET=", ln)]
    assert len(uses) >= 5
    for ln in uses:
        assert ln.startswith(("printf '%s\\n' \"$SECRET\"", "[ -z \"$SECRET\" ]", "[ -n \"$SECRET\" ]", "if [ -n \"$SECRET\" ]", "SECRET=\"\"",
                              "SECRET=$raw")), ln
    assert "set -x" not in text


# ---------------------------------------------------------------- 世界包（合成小世界）


@pytest.fixture
def tiny_worlds(tmp_path):
    from awr.world.geometry import open_world_query
    from awr.world.geometry.fake import tiny_world

    w = tmp_path / "worlds"
    tiny_world(w)
    wj = w / "tiny" / "world.json"
    doc = json.loads(wj.read_text())
    doc["dataset"] = {"name": "ANet Synthetic City", "redistribution": True}
    wj.write_text(json.dumps(doc, indent=1) + "\n")
    wq = open_world_query(w / "tiny")                               # 派生缓存 <worlds>/.geo-cache/tiny/<cv>-<key>
    (w / ".status").mkdir()
    (w / ".status" / "tiny.json").write_text(json.dumps({"status": "ready", "content_version": doc["contentVersion"]}))
    (w / "_shared" / "env" / "turb").mkdir(parents=True)
    (w / "_shared" / "env" / "turb" / "box.awrv").write_bytes(os.urandom(256))
    return w, doc["contentVersion"], wq.derive_sha8


def test_pack_verify_install_idempotent(tmp_path, tiny_worlds):
    w, cv, key = tiny_worlds
    out = tmp_path / "packs"
    idx = worldpack.pack(w, out, ["tiny"], log=lambda *_: None)
    (e,) = idx["worlds"]
    assert (e["world_id"], e["content_version"], e["derive_sha8"]) == ("tiny", cv, key)
    wj8 = hashlib.sha256((w / "tiny" / "world.json").read_bytes()).hexdigest()[:8]
    assert e["world_pack"]["file"] == f"world-tiny-{cv}-{wj8}.tar.gz" and e["cache_pack"]["file"] == f"geo-tiny-{cv}-{key}.tar.gz"
    assert idx["shared"]["file"].startswith("shared-env-")
    worldpack.verify(out)
    # 第二次打包复用同名包（内容寻址）
    mtime = (out / e["world_pack"]["file"]).stat().st_mtime_ns
    worldpack.pack(w, out, ["tiny"], log=lambda *_: None)
    assert (out / e["world_pack"]["file"]).stat().st_mtime_ns == mtime
    dest = tmp_path / "server-worlds"
    rep = worldpack.install(out, dest, log=lambda *_: None)
    assert rep["worlds"][0]["world"] == "installed" and rep["worlds"][0]["cache"] == "installed" and rep["shared"] == "installed"
    assert (dest / "tiny" / "world.json").is_file() and (dest / ".status" / "tiny.json").is_file()
    assert (dest / ".geo-cache" / "tiny" / f"{cv}-{key}" / "manifest.json").is_file()
    from awr.world.geometry import cache_status

    assert cache_status(dest / "tiny", deep=True).available                    # 键与内容在新位置一致
    rep = worldpack.install(out, dest, log=lambda *_: None)
    assert rep["worlds"][0]["world"] == "skip" and rep["worlds"][0]["cache"] == "skip" and rep["shared"] == "skip"
    # 只改 world.json 的元数据（contentVersion 不变）：换包并重新安装世界，派生缓存条目不变
    doc = json.loads((w / "tiny" / "world.json").read_text())
    doc["dataset"]["notice"] = "metadata only"
    (w / "tiny" / "world.json").write_text(json.dumps(doc, indent=1) + "\n")
    idx2 = worldpack.pack(w, out, ["tiny"], log=lambda *_: None)
    assert idx2["worlds"][0]["world_pack"]["file"] != e["world_pack"]["file"]
    assert not (out / e["world_pack"]["file"]).exists()                         # 旧包随索引更新删除
    rep = worldpack.install(out, dest, log=lambda *_: None)
    assert rep["worlds"][0]["world"] == "installed" and rep["worlds"][0]["cache"] == "skip"
    assert json.loads((dest / "tiny" / "world.json").read_text())["dataset"]["notice"] == "metadata only"


def test_show_recording_change_repacks_and_reinstalls(tmp_path, tiny_worlds):
    """城市演示录制 show/show.awrrt 不计入 contentVersion：只换录制时包名与索引摘要随之变化，安装时重装该世界（D2.2-W2）。"""
    w, cv, _key = tiny_worlds
    out, dest = tmp_path / "packs", tmp_path / "server-worlds"
    idx = worldpack.pack(w, out, ["tiny"], log=lambda *_: None)
    assert idx["worlds"][0]["show_sha256"] is None and "-s" not in idx["worlds"][0]["world_pack"]["file"][len("world-tiny-"):]
    worldpack.install(out, dest, log=lambda *_: None)
    show = w / "tiny" / "show" / "show.awrrt"
    show.parent.mkdir()
    show.write_bytes(b"recording-1")
    idx1 = worldpack.pack(w, out, ["tiny"], log=lambda *_: None)
    e1 = idx1["worlds"][0]
    assert e1["content_version"] == cv and e1["show_sha256"] == hashlib.sha256(b"recording-1").hexdigest()
    assert e1["world_pack"]["file"].endswith(f"-s{e1['show_sha256'][:8]}.tar.gz")
    rep = worldpack.install(out, dest, log=lambda *_: None)
    assert rep["worlds"][0]["world"] == "installed" and rep["worlds"][0]["cache"] == "skip"
    assert (dest / "tiny" / "show" / "show.awrrt").read_bytes() == b"recording-1"
    assert worldpack.install(out, dest, log=lambda *_: None)["worlds"][0]["world"] == "skip"
    show.write_bytes(b"recording-2")
    idx2 = worldpack.pack(w, out, ["tiny"], log=lambda *_: None)
    assert idx2["worlds"][0]["world_pack"]["file"] != e1["world_pack"]["file"]
    assert worldpack.install(out, dest, log=lambda *_: None)["worlds"][0]["world"] == "installed"
    assert (dest / "tiny" / "show" / "show.awrrt").read_bytes() == b"recording-2"


def test_install_prunes_old_cache_entries(tmp_path, tiny_worlds):
    w, cv, key = tiny_worlds
    out = tmp_path / "packs"
    worldpack.pack(w, out, ["tiny"], log=lambda *_: None)
    dest = tmp_path / "server-worlds"
    for old in ("aaaaaaaaaaaa-11111111", "bbbbbbbbbbbb-22222222", "cccccccccccc-33333333"):
        (dest / ".geo-cache" / "tiny" / old).mkdir(parents=True)
    rep = worldpack.install(out, dest, keep=2, log=lambda *_: None)
    left = sorted(p.name for p in (dest / ".geo-cache" / "tiny").iterdir())
    assert f"{cv}-{key}" in left and len(left) == 2 and len(rep["worlds"][0]["pruned"]) == 2


def test_verify_rejects_tampered_pack(tmp_path, tiny_worlds):
    w, _, _ = tiny_worlds
    out = tmp_path / "packs"
    idx = worldpack.pack(w, out, ["tiny"], log=lambda *_: None)
    p = out / idx["worlds"][0]["world_pack"]["file"]
    data = bytearray(p.read_bytes())
    data[-10] ^= 0xFF
    p.write_bytes(bytes(data))
    with pytest.raises(worldpack.PackError, match=r"大小|sha256"):
        worldpack.verify(out)


@pytest.mark.parametrize("bad", ["../escape.txt", "/abs.txt", "other/world.json", "tiny/../../x", ".geo-cache/tiny/x"])
def test_check_tar_rejects_paths_outside_prefix(tmp_path, bad):
    t = tmp_path / "bad.tar.gz"
    with tarfile.open(t, "w:gz") as tar:
        data = b"x"
        ti = tarfile.TarInfo(bad)
        ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
    with pytest.raises(worldpack.PackError):
        worldpack.check_tar(t, ("tiny",), (".status/tiny.json",))


def test_check_tar_rejects_links(tmp_path):
    t = tmp_path / "link.tar.gz"
    with tarfile.open(t, "w:gz") as tar:
        ti = tarfile.TarInfo("tiny/link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tar.addfile(ti)
    with pytest.raises(worldpack.PackError, match="不是普通文件"):
        worldpack.check_tar(t, ("tiny",))


def test_pack_refuses_unready_world(tmp_path, tiny_worlds):
    w, _, _ = tiny_worlds
    (w / ".status" / "tiny.json").write_text(json.dumps({"status": "failed", "content_version": "x"}))
    with pytest.raises(worldpack.PackError, match="ready"):
        worldpack.pack(w, tmp_path / "packs", ["tiny"], log=lambda *_: None)


def test_world_terms():
    from awr.world.ingest.urbanscene3d import DATASET

    assert worldpack.world_terms({"dataset": {"redistribution": True}}) == worldpack.TERMS_SYNTH
    # ADR-109 第 3 条：六城的条件取自 world.json 的 dataset.terms（适配器的当前文字）
    us3d = {"id": "shenzhen", "dataset": dict(DATASET)}
    assert worldpack.world_terms(us3d) == DATASET["terms"]
    assert "经数据集权利方授权使用" in worldpack.world_terms(us3d) and "ADR-109" in worldpack.world_terms(us3d)
    with pytest.raises(worldpack.PackError):
        worldpack.world_terms({"id": "x", "dataset": {"name": "Other", "redistribution": False}})


def test_world_terms_refuses_pre_adr109_dataset_text():
    """D2-AC-24：ADR-109 之前构建的六城（无 terms、许可写科研用途）不能进入部署包，提示只重写元数据。"""
    d1 = {"name": "UrbanScene3D", "redistribution": False, "license": "UrbanScene3D terms of use: non-commercial research only; see url",
          "notice": "Recorded for provenance only; distribution follows ADR-034."}
    with pytest.raises(worldpack.PackError, match="make worlds-dataset"):
        worldpack.world_terms({"id": "shenzhen", "dataset": d1})
    with pytest.raises(worldpack.PackError, match="make worlds-dataset"):  # 有 terms 但许可仍是旧文字
        worldpack.world_terms({"id": "shenzhen", "dataset": d1 | {"terms": "used under authorization"}})


def test_public_worlds_are_the_seven():
    assert worldpack.public_worlds() == ["synthcity", "shenzhen", "suzhou", "newyork", "chicago", "shanghai", "sanfrancisco"]


# ---------------------------------------------------------------- 沙盒冒烟（桩 sandbox-api）


class _Stub:
    def __init__(self, create_status: int, create_body: dict):
        self.calls: list[tuple[str, str]] = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code: int, doc: dict) -> None:
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                stub.calls.append(("GET", self.path))
                if self.path.endswith("/health"):
                    self._send(200, {"ok": True, "pool": "up"})
                else:
                    self._send(200, {"sid": "sb-abcdefghij", "state": "READY"})

            def do_POST(self):
                stub.calls.append(("POST", self.path))
                self.rfile.read(int(self.headers.get("content-length") or 0))
                self._send(create_status, create_body)

            def do_DELETE(self):
                stub.calls.append(("DELETE", self.path))
                self._send(202, {"state": "DRAINING"})

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"


@pytest.mark.parametrize(("status", "body", "rc"), [
    (201, {"session": {"sid": "sb-abcdefghij", "state": "SPAWNING"}, "token": "v1.x.y"}, 0),
    (202, {"queue": {"ticket_id": "q-abcdefghij", "ticket_token": "q1.z", "position": 2}}, 3),
    (409, {"code": 509, "name": "SANDBOX_POW_REQUIRED"}, 3),
    (409, {"code": 504, "name": "SANDBOX_QUOTA"}, 3),
    (503, {"code": 502, "name": "SANDBOX_SPAWN_FAILED"}, 1),
])
def test_smoke_outcomes(status, body, rc):
    s = _Stub(status, body)
    out: list[str] = []
    assert smoke.run(s.base, "drone4d.agentnetwork.org.cn", "synthcity", 5, out=out.append) == rc, out
    if rc == 0:
        assert ("DELETE", "/api/sandbox/v1/sessions/sb-abcdefghij") in s.calls
        assert any(p.startswith("/api/sandbox/v1/sessions/sb-abcdefghij?wait_state=READY") for _, p in s.calls)
    if status == 202:
        assert ("DELETE", "/api/sandbox/v1/queue/q-abcdefghij") in s.calls      # 冒烟不占排队名额
    assert all("v1.x.y" not in line for line in out)                            # 不打印 token
    s.srv.shutdown()


# ---------------------------------------------------------------- REL-04（发布扫描中的部署口令）


def test_rel04_reports_planted_secret_without_printing_it(tmp_path):
    s = _secret()
    secret_file = tmp_path / "admin.secret"
    secret_file.write_text(s + "\n")
    leak = tmp_path / "leak.ts"
    leak.write_text(f"export const k = '{s}'\n")
    clean = tmp_path / "clean.ts"
    clean.write_text("export const k = 'nothing here'\n")
    lst = tmp_path / "files.txt"
    lst.write_text(f"{leak}\n{clean}\n")
    p = subprocess.run(["node", str(ROOT / "tools" / "lint" / "check-release.mjs"), "--files", str(lst), "--secret-file", str(secret_file)],
                       capture_output=True, text=True, env=_env(AWR_RUNS_DIR=str(tmp_path / "runs")))
    assert p.returncode == 1
    assert f"{leak}:1:19 REL-04" in p.stdout
    assert str(clean) not in p.stdout
    assert s not in p.stdout and s not in p.stderr
    for f in (tmp_path / "runs" / "lint").glob("*.json"):
        assert s not in f.read_text()
    lst.write_text(f"{clean}\n")
    p = subprocess.run(["node", str(ROOT / "tools" / "lint" / "check-release.mjs"), "--files", str(lst), "--secret-file", str(secret_file)],
                       capture_output=True, text=True, env=_env(AWR_RUNS_DIR=str(tmp_path / "runs")))
    assert p.returncode == 0, p.stdout


def test_rel04_runs_without_git(tmp_path):
    """没有 git（发布集合未知）时 REL-04 仍按 lint 范围扫描；随机口令在仓库中 0 处。"""
    if shutil.which("git") and subprocess.run(["git", "-C", str(ROOT), "rev-parse"], capture_output=True).returncode == 0:
        pytest.skip("仓库在 git 中：REL-04 按发布集合扫描")
    secret_file = tmp_path / "admin.secret"
    secret_file.write_text(_secret() + "\n")
    p = subprocess.run(["node", str(ROOT / "tools" / "lint" / "check-release.mjs"), "--secret-file", str(secret_file)],
                       capture_output=True, text=True, env=_env(AWR_RUNS_DIR=str(tmp_path / "runs")))
    assert p.returncode == 0, p.stdout
    assert "REL-04 only: scanned" in p.stderr


# ---------------------------------------------------------------- build-local.sh + install.sh --dry-run（真实演示构建）


def _build_local(out: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(EMAX / "build-local.sh"), "--out", str(out), *args], capture_output=True, text=True,
                          env=_env(AWR_PERF_LOCK_HELD="test", AWR_RUNS_DIR=str(out / "runs")), timeout=900)


def test_build_local_rejects_bad_options(tmp_path):
    p = _build_local(tmp_path, "--worlds", "Bad,ids")
    assert p.returncode == 1 and "--worlds" in p.stderr


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    if shutil.which("node") is None or not (ROOT / "node_modules").is_dir():
        pytest.skip("需要 Node 与 node_modules（make setup）")
    out = tmp_path_factory.mktemp("deploy")
    p = _build_local(out, "--worlds", "none")
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-3000:]
    (stage,) = (out / "stage").iterdir()
    return out, stage


def test_build_local_bundle(built):
    out, stage = built
    rel = dict(line.split("=", 1) for line in (stage / "RELEASE").read_text().splitlines())
    assert re.fullmatch(r"\d{8}-\d{6}-(?:[0-9a-f]{10}|nogit)(?:-dirty)?", rel["release"])
    assert rel["channel"] == "demo" and rel["version"] == "0.2.0" and rel["dirty"] in ("true", "false")
    bi = json.loads((stage / "apps" / "web" / "dist" / "build-info.json").read_text())
    assert bi == {"version": rel["version"], "channel": "demo", "commit": rel["commit"], "dirty": rel["dirty"] == "true",
                  "built_at": rel["built_at"], "release": rel["release"]}
    html = (stage / "apps" / "web" / "dist" / "index.html").read_text()
    assert '<meta name="awr-build" content="0.2.0-demo+' in html
    assert '<div class="boot-version">V0.2.0-demo · ' in html and "window.__AWR_BOOT_MANIFEST=" in html
    assert (stage / "tools" / "sdk" / "awr_sandbox.py").is_file()
    for s in (stage / "tools" / "deploy" / "emax").glob("*.sh"):
        assert s.stat().st_mode & stat.S_IXUSR, s
    assert not (stage / "worlds").exists() and not (out / "worlds").exists()
    assert (out / f"{stage.name}.tar.gz").is_file()


def _chunk_closure(assets: Path, start: str) -> set[str]:
    """静态 import 闭包（构建产物的 `import … from "./x.js"`），返回去掉哈希的分块名。"""
    seen: set[str] = set()
    stack = [start]
    while stack:
        f = stack.pop()
        if f in seen:
            continue
        seen.add(f)
        text = (assets / f).read_text(encoding="utf-8")
        stack += re.findall(r"(?:^|[;}\n])\s*import\s*(?:[^'\"()]*?from\s*)?[\"']\./([^\"']+\.js)[\"']", text)
    return {re.sub(r"-[A-Za-z0-9_-]{8}\.js$", "", f) for f in seen}


def test_demo_chunks_landing_and_api_docs(built):
    """ADR-083 与 M15-FR-221（WP-12 请求第 1 条）：落地页只加载入口、landing、react 与运行时 4 个脚本，另可有早期 boot 小分块
    （容量文案与复数格式化，D2-FX-R2-web 为 D2-AC-41 在 vite.config.ts 增加）；/sandbox/api 的独立分块不加载 three、r3f、ui 与 engine。"""
    _, stage = built
    assets = stage / "apps" / "web" / "dist" / "assets"
    html = (stage / "apps" / "web" / "dist" / "index.html").read_text()
    entry = re.search(r'src="/assets/([^"]+\.js)"', html).group(1)
    landing = _chunk_closure(assets, entry)
    assert landing - {"boot"} == {"index", "landing", "react", "rolldown-runtime"}, landing
    for f in assets.glob("boot-*.js"):          # 早期分块保持很小（只含文案与格式化，不含 react 以外的依赖）
        assert f.stat().st_size < 64 * 1024, (f.name, f.stat().st_size)
    (apidocs,) = [f.name for f in assets.glob("apidocs-*.js")]
    got = _chunk_closure(assets, apidocs)
    assert not got & {"three", "r3f", "ui", "engine", "main"}, got
    assert "apidocs" in got


def test_build_local_then_dry_run(built, tmp_path):
    _, stage = built
    p = dry_run(stage, tmp=tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr


@pytest.mark.needs_data
def test_dry_run_with_synthcity_world_pack(built, tmp_path):
    """M17-AC-020 的本机模拟：synthcity 世界包与派生缓存解到临时目录，按发布的代码 deep 校验并确认世界状态 ready。"""
    worlds = ROOT / "worlds"
    if not (worlds / "synthcity" / "world.json").is_file() or not (worlds / ".status" / "synthcity.json").is_file():
        pytest.skip("需要已构建的 synthcity（make demo-world）")
    from awr.world.geometry import cache_status

    if not cache_status(worlds / "synthcity").available:
        pytest.skip("synthcity 派生缓存不可用（make geo-warm）")
    _, stage = built
    packs = tmp_path / "packs"
    worldpack.pack(worlds, packs, ["synthcity"], log=lambda *_: None)
    p = dry_run(stage, "--worlds", str(packs), tmp=tmp_path)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-3000:]
    assert "[synthcity] 可用" in p.stdout or "[synthcity] 可用" in p.stderr
    assert "[synthcity] 状态 ready，需重建 False" in p.stdout
