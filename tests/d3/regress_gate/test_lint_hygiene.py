"""D3 回归门 lint 步骤的发布集合卫生（D3-AC-01 第 4 项；封板任务 K02，G05 遗留 (d)）。

- REL-01：论文数据中超过 5,000,000 字节的文件不入库，登记在 research/ccf-tpci/data/external.json（字节数、SHA-256、
  重新生成命令、ModelScope 链接），由 .gitignore 排除；fetch_external.py 下载后按清单校验，不符时拒绝落盘；
  数据目录中其余文件都在上限以内（清单完整）。
- REL-07：tools/deploy/dmax/images/slam（d3-livo，FAST-LIVO2 作业镜像）标注 awr.image.scope=internal。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "research" / "ccf-tpci" / "data"
MANIFEST = DATA / "external.json"
MAX_BYTES = 5_000_000                                   # tools/lint/check-release.mjs MAX_BYTES（REL-01）
EXTERNAL_GIT_DIR = Path("/data/projs/.anet-drone4d.git")

_spec = importlib.util.spec_from_file_location("paper_fetch_external", DATA / "fetch_external.py")
assert _spec and _spec.loader
fx = importlib.util.module_from_spec(_spec)
sys.modules["paper_fetch_external"] = fx
_spec.loader.exec_module(fx)


def _doc() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _gitignore_lines() -> set[str]:
    return {ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")}


def test_check_release_limit_matches():
    src = (ROOT / "tools" / "lint" / "check-release.mjs").read_text(encoding="utf-8")
    assert re.search(r"export const MAX_BYTES = 5_000_000\b", src)


def test_external_manifest_entries_are_large_ignored_and_reproducible():
    doc = _doc()
    assert doc["schema"] == fx.SCHEMA and doc["channel"] == "modelscope" and doc["files"]
    ignored = _gitignore_lines()
    for e in doc["files"]:
        assert e["bytes"] > MAX_BYTES, f"{e['name']} fits the release limit; commit it instead of listing it"
        assert re.fullmatch(r"[0-9a-f]{64}", e["sha256"])
        assert f"/research/ccf-tpci/data/{e['name']}" in ignored, f"{e['name']} must be in .gitignore"
        assert (DATA / e["meta"]).is_file(), "the run metadata (.meta.json) stays in the repository"
        assert "sweep.py" in e["regenerate"] and "collect.py" in e["regenerate"]
        assert e["url"] is None or e["url"].startswith("https://www.modelscope.cn/") or e["url"].startswith("https://modelscope.cn/")


def test_other_paper_data_files_fit_the_release_limit():
    listed = {e["name"] for e in _doc()["files"]}
    big = sorted(p.name for p in DATA.iterdir() if p.is_file() and p.stat().st_size > MAX_BYTES and p.name not in listed)
    assert not big, f"files above {MAX_BYTES} bytes must be listed in external.json and ignored: {big}"


def test_listed_files_are_ignored_by_git(tmp_path):
    env = dict(os.environ)
    if not (ROOT / ".git").exists():
        if not EXTERNAL_GIT_DIR.is_dir():
            pytest.skip("[platform] git directory not available")
        env.update(GIT_DIR=str(EXTERNAL_GIT_DIR), GIT_WORK_TREE=str(ROOT))
    if not shutil.which("git"):
        pytest.skip("[platform] git not installed")
    for e in _doc()["files"]:
        rel = f"research/ccf-tpci/data/{e['name']}"
        r = subprocess.run(["git", "check-ignore", "--no-index", "-q", rel], cwd=ROOT, env=env, capture_output=True, timeout=60)
        assert r.returncode == 0, f"{rel} is not ignored by .gitignore"


def _stage(tmp_path: Path, payload: bytes, *, url: str | None = None) -> tuple[Path, Path]:
    """临时清单目录（manifest + 待获取文件名）与一个充当下载源的目录"""
    dst, src = tmp_path / "data", tmp_path / "mirror"
    dst.mkdir()
    src.mkdir()
    (src / "x_full.parquet").write_bytes(payload)
    doc = {"schema": fx.SCHEMA, "channel": "modelscope", "files": [
        {"name": "x_full.parquet", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "meta": "x_full.meta.json",
         "used_for": "test", "regenerate": "sweep.py x && collect.py x/full", "url": url}]}
    (dst / "external.json").write_text(json.dumps(doc), encoding="utf-8")
    return dst, src


def test_fetch_downloads_verifies_and_checks(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AWR_PAPER_DATA_URL", raising=False)
    payload = os.urandom(4096)
    dst, src = _stage(tmp_path, payload)
    m = dst / "external.json"
    assert fx.main(["--check", "--manifest", str(m)]) == 1                         # missing
    assert fx.main(["--manifest", str(m)]) == 2                                    # no URL yet: prints the regenerate command
    assert "Regenerate it with: sweep.py x" in capsys.readouterr().out
    assert fx.main(["--manifest", str(m), "--base-url", src.as_uri()]) == 0
    assert (dst / "x_full.parquet").read_bytes() == payload
    assert fx.main(["--check", "--manifest", str(m)]) == 0
    (dst / "x_full.parquet").write_bytes(payload[:-1] + b"\0")                     # a regenerated or modified local file
    assert fx.main(["--manifest", str(m), "--base-url", src.as_uri()]) == 1       # reported, never overwritten
    assert (dst / "x_full.parquet").read_bytes() != payload
    assert not [p for p in dst.iterdir() if p.name.startswith(".x_full")]


def test_fetch_rejects_a_download_that_does_not_match(tmp_path, monkeypatch):
    monkeypatch.delenv("AWR_PAPER_DATA_URL", raising=False)
    dst, src = _stage(tmp_path, b"published bytes")
    (src / "x_full.parquet").write_bytes(b"tampered bytes!")
    monkeypatch.setenv("AWR_PAPER_DATA_URL", src.as_uri())
    with pytest.raises(SystemExit, match="does not match"):
        fx.main(["--manifest", str(dst / "external.json")])
    assert sorted(p.name for p in dst.iterdir()) == ["external.json"]             # nothing left behind


def test_slam_image_is_labelled_internal():
    df = (ROOT / "tools" / "deploy" / "dmax" / "images" / "slam" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r'^\s*LABEL\b[^\n]*\bawr\.image\.scope\s*=\s*"?internal"?', df, re.M)
