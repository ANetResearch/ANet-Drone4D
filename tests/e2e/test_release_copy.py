"""D2 发布面文案复核（D3-AC-46，P-1；D3-AC-23 的"不回退"条款；AWR-05 §7.10、AWR-14 §21.15.8 与 §21.31.2）。

静态用例，不启动服务：
- copy-lint 中文与英文规则集对缺省发布面（两份 README、两份词典、CITATION.cff）在不使用基线时 0 违规，且基线例外已清零
  （`--strict`）；copy-lint 不存在的分支（`d2` 维护分支）跳过这两例；
- README 结构按 §21.15.8：标题与一句话定位、能力表（能力、交付（D1、D2、D3）、运行前提）、运行环境、快速开始、文档、
  许可与署名、引用；定位与能力表分别说明交互沙盒（服务器无需 GPU）与事件沙盘（传感器生成需要服务器 GPU）的前提；
- 产品名写法与引用元数据一致：CITATION.cff、两份 README 与落地页的 BibTeX 标题与版本逐字相同；
- 称谓：D1 视图在界面字符串中称"三维场景视图"（UX-AC-039 按钮"在三维场景视图中打开"、窗口过窄提示），"沙盘"只出现在
  lab.* 键；全部发布面不出现以主版本号 2 指代下一大版本的称谓（CL-ALL-03）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COPY_LINT = ROOT / "tools" / "lint" / "copy-lint.mjs"
I18N = ROOT / "apps" / "web" / "src" / "app" / "i18n"
SURFACE = ["README.md", "README.zh-CN.md", "CITATION.cff", "apps/web/src/app/i18n/zh-CN.json",
           "apps/web/src/app/i18n/en.json"]
# §21.15.8 的章节顺序（README.md / README.zh-CN.md）
SECTIONS = {
    "README.md": ["Capabilities", "Requirements", "Quick start", "Documentation", "License and attribution", "Citation"],
    "README.zh-CN.md": ["能力", "运行环境", "快速开始", "文档", "许可与署名", "引用"],
}
DELIVERY = {"README.md": {"D1 (V0.1)", "D2 (V0.2)", "D3 (V0.3)"}, "README.zh-CN.md": {"D1（V0.1）", "D2（V0.2）", "D3（V0.3）"}}
NO_GPU = {"README.md": "no GPU", "README.zh-CN.md": "无需 GPU"}
GPU = "NVIDIA GPU"

needs_lint = pytest.mark.skipif(not COPY_LINT.exists() or shutil.which("node") is None,
                                reason="copy-lint.mjs 或 node 不可用（d2 维护分支不含 copy-lint）")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _lint(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["node", str(COPY_LINT), *args], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)


@needs_lint
def test_copy_lint_release_surface_without_baseline():
    """中英文规则集对发布面 0 违规，不依赖任何基线例外（D3-AC-46 的改写结果，D3-AC-23 的"复跑不回退"）。"""
    r = _lint("--no-baseline")
    assert r.returncode == 0, r.stdout + r.stderr


@needs_lint
def test_copy_lint_baseline_cleared():
    """基线例外已清零：`--strict` 通过（P0b 出口条件，D3-AC-23）。"""
    r = _lint("--strict")
    assert r.returncode == 0, r.stdout + r.stderr
    reg = json.loads(_read("tools/lint/copy-lint.baseline.json"))
    assert reg["exceptions"] == []


def _headings(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^## (.+)$", text, re.M)]


def _table(text: str, heading: str) -> list[list[str]]:
    """`## heading` 下第一张 Markdown 表的行（不含分隔行），每行为单元格列表。"""
    body = text.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in body.splitlines() if ln.startswith("|")]
    return [[c.strip() for c in ln.strip("|").split("|")] for ln in rows if not re.fullmatch(r"\|[-| :]+\|", ln)]


@pytest.mark.parametrize("name", list(SECTIONS))
def test_readme_follows_the_section_structure(name):
    text = _read(name)
    heads = _headings(text)
    pos = [heads.index(h) for h in SECTIONS[name]]
    assert pos == sorted(pos), f"{name} 章节顺序不符合 §21.15.8：{heads}"
    # 标题为产品名，不使用口号式标语
    assert "<h3>ANet Drone4D</h3>" in text


@pytest.mark.parametrize("name", list(SECTIONS))
def test_readme_positioning_separates_server_gpu_prerequisites(name):
    """一句话定位分别说明交互沙盒（服务器无需 GPU）与事件沙盘（传感器生成需要服务器 GPU），不以"无需 GPU"概括全部产品。"""
    text = _read(name)
    head = text.split("<h3>ANet Drone4D</h3>", 1)[1].split("[![", 1)[0]
    assert NO_GPU[name] in head and GPU in head
    assert "Interactive Sandbox" in head or "交互沙盒" in head
    assert "Event Lab" in head or "事件沙盘" in head
    assert "No GPU needed on the server" not in text and "without a server GPU" not in text


@pytest.mark.parametrize("name", list(SECTIONS))
def test_readme_capability_table_lists_delivery_and_prerequisites(name):
    rows = _table(_read(name), SECTIONS[name][0])
    header, body = rows[0], rows[1:]
    assert len(header) == 4 and header[1] in ("Delivery", "交付") and header[2] in ("Runtime prerequisites", "运行前提")
    assert body, "能力表为空"
    seen = set()
    for cap, delivery, prereq, _status in body:
        assert delivery in DELIVERY[name], f"交付列只用 D1、D2、D3（D4 不列入）：{delivery}"
        seen.add(delivery)
        if delivery.startswith("D3"):
            assert GPU in prereq, f"D3 能力须注明服务器 GPU 前提：{cap}"
        else:
            assert "GPU" not in prereq or NO_GPU[name] in prereq, f"D1、D2 能力不需要服务器 GPU：{cap}"
    assert seen == DELIVERY[name]


@pytest.mark.parametrize("name", list(SECTIONS))
def test_readme_requirements_state_both_server_profiles(name):
    rows = _table(_read(name), SECTIONS[name][1])
    text = "\n".join(" | ".join(r) for r in rows)
    assert NO_GPU[name] in text and GPU in text and "≥ 535" in text


def _bibtex_fields(text: str) -> tuple[str, str]:
    m = re.search(r"@software\{anet_drone4d_2026,\s*title\s*=\s*\{(.+?)\},.*?version\s*=\s*\{(.+?)\}", text, re.S)
    assert m, "缺少 Drone4D 的 BibTeX 条目"
    return m.group(1), m.group(2)


def test_citation_metadata_is_consistent():
    """CITATION.cff、两份 README 与落地页（LandingContent.ts）的引用标题与版本逐字相同；产品名写 ANet Drone4D。"""
    cff = _read("CITATION.cff")
    title = re.search(r'^title: "(.+)"$', cff, re.M).group(1)
    version = re.search(r'^version: "(.+)"$', cff, re.M).group(1)
    assert title.startswith("ANet Drone4D: ")
    for rel in ("README.md", "README.zh-CN.md", "apps/web/src/app/demo/LandingContent.ts"):
        assert _bibtex_fields(_read(rel)) == (title, version), rel


def _dict(lang: str) -> dict[str, str]:
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


def test_d1_view_is_called_scene_view_in_the_ui():
    """UX-AC-039 与 AWR-14 §21.31.2：D1 视图称"三维场景视图"；"沙盘"只用于 D3（lab.* 键与"事件沙盘"称谓本身）。"""
    zh = _dict("zh-CN")
    assert zh["jobs.openInSandbox"] == "在三维场景视图中打开"
    assert zh["jobs.subtitle"].endswith("三维场景视图加载")
    for k in ("shell.tooSmallHint", "ff.narrowHint"):
        assert zh[k].startswith("本应用"), k
    stray = sorted(k for k, v in zh.items() if "沙盘" in v.replace("事件沙盘", "") and not k.startswith("lab."))
    assert stray == [], f"lab.* 以外的键出现'沙盘'：{stray}"


def test_public_demo_title_is_sentence_case():
    assert _dict("en")["demo.product"] == "ANet Drone4D public demo"


def test_landing_roadmap_states_the_event_lab_gpu_prerequisite():
    """落地页的开发中章节说明事件沙盘的传感器生成需要服务器 GPU，交互沙盒章节说明服务器无需 GPU。"""
    zh, en = _dict("zh-CN"), _dict("en")
    assert "事件沙盘" in zh["landing.story.roadmap.body"] and GPU in zh["landing.story.roadmap.body"]
    assert "Event Lab" in en["landing.story.roadmap.body"] and GPU in en["landing.story.roadmap.body"]
    assert "D3 Event Lab" not in en["landing.story.roadmap.body"]
    assert "无需 GPU" in zh["landing.story.sandbox.p3"] and "No server GPU" in en["landing.story.sandbox.p3"]


@pytest.mark.parametrize("rel", SURFACE)
def test_no_major_version_two_naming(rel):
    """CL-ALL-03：下一大版本称 D4；发布面不出现 Drone4D 2.0、Drone4D v2、2.0 版、二代、新一代 Drone4D、D3.5、D3 Plus。"""
    text = _read(rel)
    bad = re.findall(r"Drone4D\s*(?:2(?:\.0)?|v2)\b|2\.0\s*版|二代|新一代\s*Drone4D|\bD3\.5\b|\bD3 Plus\b", text, re.I)
    assert bad == [], f"{rel}: {bad}"


@pytest.mark.parametrize("rel", ["README.md", "README.zh-CN.md"])
def test_repository_identifier_only_in_urls_and_code(rel):
    """仓库标识 ANet-Drone4D 只出现在 URL、代码块与行内代码中（正文写 ANet Drone4D，§7.10 英文规则 5）。"""
    text = _read(rel)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]+`", "", text)
    text = re.sub(r"https?://\S+", "", text)
    assert "ANet-Drone4D" not in text
