"""公开构建产物与公开静态目录扫描（tools/bench/d3/scan_public.py；D3-AC-21、D3-AC-01；扩展点 15、16）。

纯函数用例只用临时目录；两个集成用例实际构建（需要本检出的 node_modules 与 git，缺失时跳过）：
- `/lab/` 独立静态构建（VITE_AWR_LAB_ONLY=1）的产物按 lab 档扫描为 0，且不含 public/ 的 bench 夹具与模型；
- 扩展点 15 的构建对拍：D2 标签 v0.2.1 与当前工作树的演示构建（开关缺省关闭）逐字节一致（UX-AC-082）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("scan_public", ROOT / "tools" / "bench" / "d3" / "scan_public.py")
assert _spec and _spec.loader
sp = importlib.util.module_from_spec(_spec)
sys.modules["scan_public"] = sp
_spec.loader.exec_module(sp)

VITE = ROOT / "node_modules" / ".bin" / "vite"


def _w(root: Path, rel: str, data: str | bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------------------------------------------------
# build


def test_profiles_carry_the_ac21_strings() -> None:
    for prof in ("lab", "demo"):
        for s in ("/leaderboard", "/submissions", "/board", "/registry", "/api/bench/v1/", "/api/bench"):
            assert s in sp.PROFILES[prof]
    for s in ("/api/bench/v1/runs", "/api/bench/v1/events", "/api/bench/v1/session", "/api/", "new WebSocket"):
        assert s in sp.PROFILES["lab"]
        assert s not in sp.PROFILES["demo"]


def test_build_scan_clean_and_hits(tmp_path: Path) -> None:
    d = tmp_path / "dist"
    _w(d, "index.html", '<script type="module" src="/lab/assets/index-a.js"></script>')
    _w(d, "assets/index-a.js", "const t=`/lab-static/index.json`;fetch(t)")
    _w(d, "assets/font.woff2", bytes(range(256)) * 4)
    rep = sp.scan_build(d, "lab")
    assert rep["ok"] and rep["files"] == 3 and all(v == 0 for v in rep["strings"].values())
    # the bench base without the trailing slash (BENCH + '/runs' concatenation) and a full AC-21 path
    _w(d, "assets/ui-b.js", "const B=`/api/bench/v1`;nav(`/lab/leaderboard`);nav(`/lab/leaderboard?x`)")
    rep = sp.scan_build(d, "lab")
    assert not rep["ok"]
    assert rep["strings"]["/leaderboard"] == 2 and rep["strings"]["/api/bench"] == 1 and rep["strings"]["/api/"] == 1
    hit = next(f for f in rep["findings"] if f["string"] == "/leaderboard")
    assert hit["file"] == "assets/ui-b.js" and "/lab/leaderboard" in hit["hits"][0]["context"]


def test_build_scan_demo_profile_allows_d2_api_paths(tmp_path: Path) -> None:
    d = tmp_path / "dist"
    _w(d, "assets/main.js", "fetch(`/api/sandbox/v1/capacity`);new WebSocket(u);`/api/runs/${id}/events`")
    assert sp.scan_build(d, "demo")["ok"]
    rep = sp.scan_build(d, "lab")
    assert not rep["ok"] and rep["strings"]["new WebSocket"] == 1


def test_build_scan_counts_test_files_in_the_build(tmp_path: Path) -> None:
    d = tmp_path / "dist"
    _w(d, "index.html", "<html></html>")
    _w(d, "samples/test/run.json", "{}")
    rep = sp.scan_build(d, "lab")
    assert rep["test_files"] == 1 and not rep["ok"]


# ---------------------------------------------------------------------------------------------------------------------
# static


def test_static_clean(tmp_path: Path) -> None:
    v = tmp_path / "lab-static" / "v" / "2026.11.03-1"
    _w(v, "runs/run-abcdefghij/run_manifest.json", json.dumps({"split": "dev", "ranked": False}))
    _w(v, "runs/run-abcdefghij/video/uav-01_cam0.mp4", b"\0" * 1000)
    _w(tmp_path / "lab-static", "index.json", json.dumps({"schema": "drone4d.lab_static_index.v1", "events": [{"split": "dev"}]}))
    rep = sp.scan_static(tmp_path / "lab-static")
    assert rep["ok"] and rep["test_files"] == 0 and rep["truth_files"] == 0 and rep["files"] == 3


def test_static_test_split_rules(tmp_path: Path) -> None:
    r = tmp_path / "s"
    _w(r, "v/1/hidden/abc/truth.bin", b"x")  # segment hidden (and truth)
    _w(r, "v/1/test/readme.txt", "x")  # segment test
    _w(r, "v/1/runs/run-aaaaaaaaaa/run_manifest.json", json.dumps({"run": {"split": "test"}}))  # nested split = test
    _w(r, "v/1/runs/run-aaaaaaaaaa/video/uav-01_cam0.mp4", b"\0")  # same subtree
    _w(r, "v/1/runs/run-bbbbbbbbbb/video/uav-01_cam0.mp4", b"\0")  # dev run, clean
    _w(r, "v/1/events/abc-secret-001/tape.bin", b"\0")  # listed in --test-ids
    ids = tmp_path / "ids.txt"
    ids.write_text("# test split\nabc-secret-001\n\n", encoding="utf-8")
    rep = sp.scan_static(r, test_ids=sp.load_test_ids(ids))
    got = {x["file"]: x["reason"] for x in rep["test_detail"]}
    assert got == {
        "v/1/hidden/abc/truth.bin": "segment",
        "v/1/test/readme.txt": "segment",
        "v/1/runs/run-aaaaaaaaaa/run_manifest.json": "split:v/1/runs/run-aaaaaaaaaa",
        "v/1/runs/run-aaaaaaaaaa/video/uav-01_cam0.mp4": "split:v/1/runs/run-aaaaaaaaaa",
        "v/1/events/abc-secret-001/tape.bin": "test-id",
    }
    assert rep["test_files"] == 5 and not rep["ok"]
    # "contest" or "latest" are not the test segment
    r2 = tmp_path / "s2"
    _w(r2, "v/1/latest/contest.json", "{}")
    assert sp.scan_static(r2)["ok"]


def test_static_truth_marker_across_read_chunks(tmp_path: Path) -> None:
    r = tmp_path / "s"
    blob = b"\0" * ((1 << 20) - 5) + sp.TRUTH_MARKER + b"camera/uav-01/cam0" + b"\0" * 100
    _w(r, "v/1/runs/run-abcdefghij/slices/a.rrd", blob)
    rep = sp.scan_static(r)
    assert rep["truth_files"] == 1 and rep["truth_detail"][0]["reason"] == "eval-hidden-channel" and not rep["ok"]


def test_static_size_limits_and_hardlink_dedupe(tmp_path: Path) -> None:
    r = tmp_path / "s"
    a = _w(r, "v/2/big.mp4", b"\0" * 3000)
    (r / "v" / "1").mkdir(parents=True)
    os.link(a, r / "v" / "1" / "big.mp4")  # rsync --link-dest keeps one copy on disk
    rep = sp.scan_static(r, max_file_mb=1 / 1024, budget_gb=10)
    assert rep["bytes_total"] == 3000 and rep["oversize_files"] == 2 and not rep["ok"]
    rep = sp.scan_static(r, max_file_mb=1, budget_gb=2000 / 1024 ** 3)
    assert rep["oversize_files"] == 0 and rep["bytes_total"] > rep["budget_bytes"] and not rep["ok"]
    assert sp.scan_static(r, max_file_mb=1, budget_gb=1)["ok"]


# ---------------------------------------------------------------------------------------------------------------------
# parity and CLI


def test_parity(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        _w(d, "index.html", "x")
        _w(d, "assets/a.js", "1")
    assert sp.parity(a, b)["ok"]
    _w(b, "assets/a.js", "2")
    _w(b, "assets/c.js", "3")
    _w(a, "assets/d.js", "4")
    rep = sp.parity(a, b)
    assert not rep["ok"] and rep["differ"] == ["assets/a.js"] and rep["only_b"] == ["assets/c.js"] and rep["only_a"] == ["assets/d.js"]


def test_parity_env_clears_the_switches() -> None:
    env = sp.parity_env("928c57bcc3", "2026-10-17T10:53:51Z", {"PATH": "/bin", "VITE_AWR_LAB_PUBLIC": "1", "VITE_AWR_TEST_SWITCHES": "1",
                                                               "AWR_BUILD_DIRTY": "true", "SOURCE_DATE_EPOCH": "1"})
    assert env == {"PATH": "/bin", "VITE_AWR_DEMO": "public", "AWR_BUILD_CHANNEL": "demo", "AWR_BUILD_SHA": "928c57bcc3",
                   "AWR_BUILD_DIRTY": "false", "AWR_BUILD_TIME": "2026-10-17T10:53:51Z", "AWR_RELEASE": "local-928c57bcc3"}


def test_cli_exit_codes(tmp_path: Path) -> None:
    d = tmp_path / "dist"
    _w(d, "a.js", "ok")
    out = tmp_path / "r.json"
    assert sp.main(["build", str(d), "--json", str(out)]) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["schema"] == sp.SCHEMA and rep["ok"] and rep["profile"] == "lab"
    _w(d, "b.js", "`/registry`")
    assert sp.main(["build", str(d)]) == 1
    assert sp.main(["build", str(tmp_path / "missing")]) == 2
    assert sp.main(["static", str(tmp_path / "missing")]) == 2
    assert sp.main(["parity", str(d), str(d)]) == 0


# ---------------------------------------------------------------------------------------------------------------------
# integration: real builds


def _need_node() -> None:
    if not VITE.exists() or shutil.which("node") is None:
        pytest.skip("[platform] node_modules/.bin/vite or node not available")


def _git_ok() -> bool:
    try:
        subprocess.run(["git", "rev-parse", "v0.2.1^{commit}"], cwd=ROOT, check=True, capture_output=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def test_lab_only_build_is_clean(tmp_path: Path) -> None:
    """`/lab/` 独立静态构建：AC-21 字符串、bench 路径、接口路径与 WS 为 0；不复制 public/（bench 夹具、模型、演示媒体）"""
    _need_node()
    out = tmp_path / "lab-dist"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VITE_AWR_", "AWR_BUILD_"))}
    env.update({"VITE_AWR_LAB_ONLY": "1", "AWR_BUILD_CHANNEL": "demo", "AWR_BUILD_SHA": "nogit", "AWR_BUILD_DIRTY": "false",
                "AWR_BUILD_TIME": "2026-10-18T00:00:00Z", "AWR_RELEASE": "local-nogit"})
    r = subprocess.run([str(VITE), "build", "--outDir", str(out), "--emptyOutDir", "--logLevel", "error"], cwd=ROOT / "apps" / "web", env=env,
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    rep = sp.scan_build(out, "lab")
    assert rep["ok"], rep["findings"][:5]
    assert rep["test_files"] == 0 and rep["truth_files"] == 0
    names = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert not any(n.startswith(("bench/", "models/", "demo/")) for n in names)
    assert {"index.html", "build-info.json", "brand/avatar-96.png", "brand/favicon-32.png"} <= names
    html = (out / "index.html").read_text(encoding="utf-8")
    assert 'src="/lab/assets/' in html and "/src/main.tsx" not in html and "data-awr-early" not in html and "manifest.webmanifest" not in html
    k9 = subprocess.run(["node", "apps/web/tests/m06/lint/prod-bundle-scan.mjs", str(out)], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert k9.returncode == 0, k9.stdout + k9.stderr


def test_d2_demo_build_parity(tmp_path: Path) -> None:
    """扩展点 15：开关缺省关闭时 d3-dev 的演示构建与 D2 标签 v0.2.1 的演示构建逐字节一致（D3-AC-01，UX-AC-082）"""
    _need_node()
    if not _git_ok():
        pytest.skip("[platform] git with tag v0.2.1 not reachable (export GIT_DIR for the external git directory)")
    rep = sp.d2_parity("v0.2.1", tmp_path / "parity")
    assert all(b["returncode"] == 0 for b in rep["builds"].values()), rep["builds"]
    assert rep["parity"]["ok"], {k: rep["parity"][k] for k in ("only_a", "only_b", "differ")}
    assert rep["parity"]["files_a"] > 20
    assert rep["scan"]["ok"]
