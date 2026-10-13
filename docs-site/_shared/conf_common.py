"""ANet Drone4D 文档站的公共 Sphinx 配置（en 与 zh_CN 两套源共用）。

每种语言的 conf.py 只做一件事：

    from conf_common import configure
    globals().update(configure("zh_CN"))

URL 布局与 Read the Docs 一致：/drone4d/<lang>/latest/。语言目录名为 en 与 zh-cn（RTD 的语言代码），
源目录名为 en 与 zh_CN（Sphinx 的 language 代码）。两套源的文档名（docname）必须一一对应，
语言切换浮层据此链接到另一语言的同一页面（drone4d_site 扩展在构建时校验）。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

SHARED = Path(__file__).resolve().parent
SITE = SHARED.parent
REPO = SITE.parent

sys.path.insert(0, str(SHARED / "_ext"))

# 语言：Sphinx 代码 -> (URL 段, 显示名)
LANGUAGES = {
    "zh_CN": ("zh-cn", "简体中文"),
    "en": ("en", "English"),
}
VERSIONS = ["latest"]
CURRENT_VERSION = "latest"
SITE_ROOT_URL = "https://docs.agentnetwork.org.cn/drone4d/"
GITHUB_URL = "https://github.com/ANetResearch/ANet-Drone4D"
DEMO_URL = "https://drone4d.agentnetwork.org.cn"


def _release() -> str:
    """产品版本单一来源：pyproject.toml（与 apps/web/package.json 由 CI 校验相等）。"""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "0.0.0"


def _quiet_jieba() -> None:
    """jieba 在首次分词时向 stderr 打印加载信息；构建日志只保留 Sphinx 自身的输出。"""
    try:
        import logging

        import jieba

        jieba.setLogLevel(logging.WARNING)
    except ImportError:
        pass


def configure(language: str) -> dict:
    if language not in LANGUAGES:
        raise ValueError(f"unknown language {language!r}")
    lang_slug, _ = LANGUAGES[language]
    release = _release()
    zh = language == "zh_CN"
    if zh:
        _quiet_jieba()
    on_rtd = os.environ.get("READTHEDOCS") == "True"

    cfg: dict = {}
    cfg["project"] = "ANet Drone4D"
    cfg["author"] = "Agent Network Research"
    cfg["copyright"] = "2026, Agent Network Research"
    cfg["release"] = release
    cfg["version"] = ".".join(release.split(".")[:2])
    cfg["language"] = language
    cfg["root_doc"] = "index"

    cfg["extensions"] = [
        "myst_parser",
        "sphinx_copybutton",
        "drone4d_api",
        "drone4d_site",
    ]
    cfg["source_suffix"] = {".md": "markdown"}
    cfg["exclude_patterns"] = ["_build", "Thumbs.db", ".DS_Store"]
    cfg["templates_path"] = [str(SHARED / "_templates")]

    # MyST
    cfg["myst_enable_extensions"] = ["colon_fence", "deflist", "fieldlist", "attrs_inline", "attrs_block"]
    cfg["myst_heading_anchors"] = 3

    # 代码块复制按钮：只复制命令，不复制提示符与输出
    cfg["copybutton_prompt_text"] = r"\$ "
    cfg["copybutton_prompt_is_regexp"] = True
    cfg["copybutton_only_copy_prompt_lines"] = False
    cfg["copybutton_exclude"] = ".linenos, .gp, .go"
    cfg["copybutton_image_svg"] = (SHARED / "_static" / "img" / "copy.svg").read_text(encoding="utf-8")
    cfg["copybutton_selector"] = (
        "div.highlight-sh pre, div.highlight-bash pre, div.highlight-python pre, div.highlight-shell pre"
    )

    # HTML 与主题
    cfg["html_theme"] = "sphinx_rtd_theme"
    cfg["html_theme_options"] = {
        "logo_only": True,
        "navigation_depth": 3,
        "collapse_navigation": False,
        "sticky_navigation": True,
        "includehidden": True,
        "titles_only": False,
        "prev_next_buttons_location": "bottom",
        "style_external_links": False,
        "version_selector": False,
        "language_selector": False,
    }
    cfg["html_title"] = "ANet Drone4D " + ("文档" if zh else "Documentation")
    cfg["html_short_title"] = "ANet Drone4D"
    cfg["html_logo"] = str(SHARED / "_static" / "img" / "anet-logo.svg")
    cfg["html_favicon"] = str(SHARED / "_static" / "img" / "favicon-32.png")
    cfg["html_static_path"] = [str(SHARED / "_static")]
    cfg["html_css_files"] = ["css/drone4d.css", "css/drone4d-icons.css"]
    cfg["html_js_files"] = ["js/drone4d.js"]
    cfg["html_show_sourcelink"] = False
    cfg["html_show_sphinx"] = True
    cfg["html_copy_source"] = False
    cfg["html_baseurl"] = f"{SITE_ROOT_URL}{lang_slug}/{CURRENT_VERSION}/"
    cfg["html_last_updated_fmt"] = "%Y-%m-%d"
    cfg["html_last_updated_use_utc"] = True
    cfg["html_search_language"] = "zh" if zh else "en"
    cfg["html_context"] = {
        "display_github": True,
        "github_user": "ANetResearch",
        "github_repo": "ANet-Drone4D",
        "github_version": "main",
        "conf_py_path": f"/docs-site/{language}/",
        "drone4d_languages": [(code, slug, name) for code, (slug, name) in LANGUAGES.items()],
        "drone4d_current_language": language,
        "drone4d_versions": VERSIONS,
        "drone4d_current_version": CURRENT_VERSION,
        "drone4d_github_url": GITHUB_URL,
        "drone4d_demo_url": DEMO_URL,
        "drone4d_on_rtd": on_rtd,
    }

    # 本站自有扩展的配置
    cfg["drone4d_repo_root"] = str(REPO)
    cfg["drone4d_site_root"] = str(SITE)

    # 链接检查（make docs-site-linkcheck）：对外链接允许超时，结果写入 _build/linkcheck/<lang>/output.txt
    cfg["linkcheck_timeout"] = 20
    cfg["linkcheck_retries"] = 2
    cfg["linkcheck_workers"] = 4
    cfg["linkcheck_anchors"] = False
    cfg["linkcheck_report_timeouts_as_broken"] = False
    cfg["linkcheck_ignore"] = [
        r"^http://127\.0\.0\.1",
        r"^http://localhost",
        r"^https?://<",
    ]
    cfg["linkcheck_allowed_redirects"] = {
        r"https://github\.com/.*": r"https://github\.com/.*",
        r"https://doi\.org/.*": r".*",
        r"https://arxiv\.org/abs/.*": r"https://arxiv\.org/abs/.*",
    }

    cfg["today"] = datetime.now(UTC).strftime("%Y-%m-%d")
    cfg["pygments_style"] = "bw"
    cfg["smartquotes"] = False
    cfg["numfig"] = False
    return cfg
