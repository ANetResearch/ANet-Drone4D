"""D2 补丁 v0.2.2 的发布物料（D3-AC-46，P-1；ADR-209；AWR-14 §21.15.8）。

静态用例，不访问网络、不改动仓库引用：
- GitHub About 一句话描述 ≤ 120 字符并通过 copy-lint 英文规则集；topics 为小写连字符形式、不重复、≤ 20 个，且由
  2026-10-19 的仓库现状按执行单的移除与新增清单得到发布说明中的清单；
- release notes 正文按执行单的抽取规则得到四节（Added、Changed、Fixed、Known limitations），Fixed 列出 `v0.2.1`
  标签说明中的全部议题编号，正文与 release 标题通过 copy-lint 英文规则集；
- 执行单可复现：在临时索引上自 `v0.2.1` 移植 21490b0 的 7 个文件、再应用附录 A，所得的树等于执行单记录的树
  （需要 git 目录与相关对象；不可用时跳过）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NOTES = ROOT / "docs" / "impl" / "D2-v0.2.2-发布说明.md"
RUNBOOK = ROOT / "docs" / "impl" / "D2-v0.2.2-发布执行单.md"
COPY_LINT = ROOT / "tools" / "lint" / "copy-lint.mjs"
# 2026-10-19（UTC）经 GitHub 公开 API 读取的仓库 topics（执行单第 1 节）
TOPICS_2026_10_19 = {"4d", "anet", "digital-twin", "drone", "drone-swarm", "embodied-ai", "gaussian-splatting", "gazebo", "lidar",
                     "multi-agent", "point-cloud", "px4", "simulation", "swarm", "swarm-robotics", "threejs", "uav", "urbanscene3d",
                     "webgl", "world-model"}
# 提交 ① 移植的文件（21490b0 中除 d3-dev 专属的 copy-lint 基线以外的 7 个文件）
COPY_FILES = ["CITATION.cff", "README.md", "README.zh-CN.md", "apps/web/src/app/demo/LandingContent.ts",
              "apps/web/src/app/i18n/en.json", "apps/web/src/app/i18n/zh-CN.json", "tests/e2e/test_release_copy.py"]
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")

pytestmark = pytest.mark.skipif(not NOTES.exists() or not RUNBOOK.exists(), reason="发布物料只在 d3-dev 的 docs/impl 中")
needs_lint = pytest.mark.skipif(not COPY_LINT.exists() or shutil.which("node") is None, reason="copy-lint.mjs 或 node 不可用")


def _section(text: str, heading: str) -> str:
    """`heading` 行之后、下一个同级或更高级标题之前的正文。"""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    i = lines.index(heading)
    out = []
    for line in lines[i + 1:]:
        m = re.match(r"^(#+) ", line)
        if m and len(m.group(1)) <= level:
            break
        out.append(line)
    return "\n".join(out).strip()


def _notes() -> str:
    return NOTES.read_text(encoding="utf-8")


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _lint_en(text: str, kind: str = "text") -> subprocess.CompletedProcess[str]:
    return subprocess.run(["node", str(COPY_LINT), "--lang", "en", "--kind", kind, "--text", text], cwd=ROOT,
                          capture_output=True, text=True, timeout=120, check=False)


def _about() -> str:
    return _section(_notes(), "### About").splitlines()[0].strip()


def _topics() -> list[str]:
    para = _section(_notes(), "### Topics (V0.2)").split("\n\n")[0]
    return re.findall(r"`([^`]+)`", para)


def _release_body() -> str:
    """执行单第 6 节的抽取规则：`### v0.2.2` 之后到 `## 2.` 之前，`####` 上调为 `##`。"""
    out, on = [], False
    for line in _notes().splitlines():
        if line == "### v0.2.2":
            on = True
            continue
        if line.startswith("## 2."):
            break
        if on:
            out.append("## " + line[5:] if line.startswith("#### ") else line)
    return "\n".join(out).strip()


def _runbook_flag(flag: str) -> str:
    m = re.search(rf"{re.escape(flag)} (\"[^\"]+\"|\S+)", _runbook())
    assert m, flag
    return m.group(1).strip('"')


def test_about_length_and_topics_form():
    """About ≤ 120 字符（§21.15.8）；topics 小写连字符、不重复、≤ 20 个（GitHub 上限），共 12 个（ADR-209 决策 (7)）。"""
    about = _about()
    assert 0 < len(about) <= 120, (len(about), about)
    topics = _topics()
    assert len(topics) == len(set(topics)) == 12, topics
    assert all(TOPIC_RE.match(t) for t in topics), topics
    assert _runbook_flag("--description") == about


def test_topics_transition_matches_runbook():
    """仓库现状按执行单的 --remove-topic 与 --add-topic 调整后，恰为发布说明的清单；V0.3 追加项不提前出现。"""
    remove = set(_runbook_flag("--remove-topic").split(","))
    add = set(_runbook_flag("--add-topic").split(","))
    assert remove <= TOPICS_2026_10_19 and not (add & TOPICS_2026_10_19)
    assert (TOPICS_2026_10_19 - remove) | add == set(_topics())
    later = re.findall(r"`([^`]+)`", _section(_notes(), "### Topics (V0.2)").split("Topics to add with the V0.3 release:")[1])
    assert set(later) == {"benchmark", "gazebo", "lidar", "rerun"} and not (set(later) & set(_topics()))


def test_release_notes_sections_and_fixed_issues():
    """release 正文四节齐全且有序；Fixed 列出 v0.2.1 的五个议题（#14、#7、#9、#8、#6）。"""
    body = _release_body()
    heads = re.findall(r"^## (.+)$", body, flags=re.M)
    assert heads == ["Added", "Changed", "Fixed", "Known limitations"], heads
    fixed = body.split("## Fixed")[1].split("## Known limitations")[0]
    assert set(re.findall(r"\(#(\d+)\)", fixed)) == {"14", "7", "9", "8", "6"}
    assert "REL-03" in fixed
    assert "####" not in body and "v0.2.2" not in heads


def test_release_title_consistent():
    """release 标题在发布说明与执行单中一致，并沿用 v0.2.0 的"V0.x · 说明"体例。"""
    title = _section(_notes(), "### Release title").splitlines()[0].strip()
    assert title == _runbook_flag("--title")
    assert title.startswith("V0.2.2 · ")


@needs_lint
def test_release_copy_passes_copy_lint():
    """About、release 标题与 release 正文通过 copy-lint 英文规则集（D3-AC-46"GitHub About、topics 与 release notes 经评审"）。"""
    for text, kind in ((_about(), "text"), (_runbook_flag("--title"), "text"), (_release_body(), "md")):
        r = _lint_en(text, kind)
        assert r.returncode == 0, r.stdout + r.stderr


# ---- 执行单可复现（git） ------------------------------------------------------------------------------------------------------

def _git(args: list[str], *, env: dict[str, str] | None = None, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=ROOT, env=env, input=stdin, capture_output=True, timeout=120, check=False)


def _have_objects() -> bool:
    if shutil.which("git") is None:
        return False
    return all(_git(["rev-parse", "--verify", "--quiet", f"{r}^{{commit}}"]).returncode == 0 for r in ("v0.2.1", "21490b0"))


def _appendix_a() -> bytes:
    text = _runbook().split("## 附录 A", 1)[1]
    m = re.search(r"^```diff\n(.*?)^```$", text, flags=re.S | re.M)
    assert m
    return m.group(1).encode("utf-8")


@pytest.mark.skipif(not _have_objects(), reason="git 目录或 v0.2.1、21490b0 对象不可用（外置 GIT_DIR 未设置时）")
def test_runbook_reproduces_d2_tree(tmp_path: Path):
    """在临时索引上执行第 1 步：自 v0.2.1 移植 7 个文件、应用附录 A，树等于执行单记录值（不改动任何引用与工作树）。"""
    want = re.search(r"test \"\$\(\$G rev-parse 'HEAD\^\{tree\}'\)\" = ([0-9a-f]{40})", _runbook())
    assert want
    env = {**os.environ, "GIT_INDEX_FILE": str(tmp_path / "index")}
    assert _git(["read-tree", "v0.2.1^{tree}"], env=env).returncode == 0
    diff = _git(["show", "--format=", "21490b0", "--", *COPY_FILES]).stdout
    assert diff
    r = _git(["apply", "--cached", "-"], env=env, stdin=diff)
    assert r.returncode == 0, r.stderr.decode()
    r = _git(["apply", "--cached", "-"], env=env, stdin=_appendix_a())
    assert r.returncode == 0, r.stderr.decode()
    tree = _git(["write-tree"], env=env).stdout.decode().strip()
    assert tree == want.group(1)
