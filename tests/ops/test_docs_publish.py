"""文档站发布脚本 tools/deploy/docs/publish.sh（D2.2-D1；产品更名为 ANet4D 后的 /anet4d/ 路径与旧路径 /drone4d/ 跳转页）。

脚本只准备待发布目录并打印命令，不上传；这里以一份最小的构建目录执行它，检查：待发布目录以 /anet4d 结尾、去掉构建缓存；
旧路径目录中每个 HTML 页面在同一相对路径上有跳转页（meta refresh、rel=canonical 指向新路径的同一页面，目录页指向目录 URL），
不含非 HTML 文件；两个目录各有 MANIFEST.sha256；打印的命令只作用于 anet4d/ 与 drone4d/ 两个子目录。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / "tools" / "deploy" / "docs" / "publish.sh"
NEW = "https://docs.agentnetwork.org.cn/anet4d"
PAGES = ("index.html", "en/latest/index.html", "zh-cn/latest/index.html", "en/latest/search.html", "zh-cn/latest/search.html",
         "zh-cn/latest/intro/quickstart.html", "en/latest/api/rest.html")


def _build(tmp: Path) -> Path:
    b = tmp / "html"
    for rel in PAGES:
        f = b / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"<html><body>{rel}</body></html>\n", encoding="utf-8")
    (b / "en" / "latest" / "_static").mkdir(parents=True)
    (b / "en" / "latest" / "_static" / "site.css").write_text("body{}\n")
    (b / "en" / "latest" / "_sources").mkdir()
    (b / "en" / "latest" / "_sources" / "index.md.txt").write_text("x\n")
    (b / "en" / "latest" / ".buildinfo").write_text("x\n")
    return b


def _run(tmp: Path, out: Path) -> subprocess.CompletedProcess:
    # the interpreter's own directory first: the gate container has python3 only in its venv, not in /usr/bin
    env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(tmp / "home")}
    return subprocess.run(["bash", str(PUBLISH), "--build", str(_build(tmp)), "--out", str(out)], capture_output=True, text=True,
                          env=env, timeout=120)


@pytest.fixture(scope="module")
def published(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("docs")
    out = tmp / "deploy" / "anet4d"
    r = _run(tmp, out)
    assert r.returncode == 0, r.stderr
    return out, out.parent / "drone4d", r.stdout


def test_out_must_end_with_anet4d(tmp_path):
    r = _run(tmp_path, tmp_path / "deploy" / "drone4d")
    assert r.returncode == 1 and "/anet4d" in r.stderr


def test_new_tree_is_clean_copy(published):
    out, _, _ = published
    for rel in PAGES:
        assert (out / rel).is_file(), rel
    assert (out / "en" / "latest" / "_static" / "site.css").is_file()
    assert not list(out.rglob(".buildinfo")) and not list(out.rglob("_sources"))


def test_legacy_tree_has_one_redirect_per_page(published):
    _, legacy, _ = published
    got = sorted(p.relative_to(legacy).as_posix() for p in legacy.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256")
    assert got == sorted(PAGES)
    for rel in PAGES:
        page = (legacy / rel).read_text(encoding="utf-8")
        path = rel[: -len("index.html")] if rel.endswith("index.html") else rel
        want = f"{NEW}/{path}"
        assert f'<link rel="canonical" href="{want}">' in page, rel
        assert f'<meta http-equiv="refresh" content="0; url={want}">' in page, rel
        assert "location.search + location.hash" in page
        assert re.search(r"<title>[^<]*ANet4D[^<]*</title>", page)
        assert "drone4d" not in page.lower(), rel


def test_manifests_match_files(published):
    out, legacy, _ = published
    for d in (out, legacy):
        lines = (d / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
        assert lines
        for line in lines:
            digest, rel = line.split("  ", 1)
            assert hashlib.sha256((d / rel).read_bytes()).hexdigest() == digest, rel


def test_printed_commands_touch_only_two_subdirs(published):
    _, _, stdout = published
    targets = set(re.findall(r"/var/www/docs\.agentnetwork/public/[A-Za-z0-9_.-]+", stdout))
    assert targets == {"/var/www/docs.agentnetwork/public/anet4d", "/var/www/docs.agentnetwork/public/drone4d"}, targets
    assert f"{NEW}/en/latest/" in stdout
    assert "--delete" in stdout and "/var/www/docs.agentnetwork/backup/drone4d-" in stdout
