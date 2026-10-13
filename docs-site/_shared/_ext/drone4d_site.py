"""drone4d_site：ANet Drone4D 文档站的站点级行为。

1. 两种语言源的文档名必须一一对应（语言切换浮层链接到另一语言的同一页面）；不一致时告警（-W 下构建失败）。
2. 为模板提供语言与版本切换所需的相对链接（drone4d_alternates、drone4d_versions_links），以及 hreflang 备用链接。

站点 URL 布局与 Read the Docs 相同：<site>/<lang>/<version>/<page>.html，例如 /drone4d/zh-cn/latest/intro/quickstart.html。
本地构建输出 docs-site/_build/html/<lang>/<version>/，与线上目录一一对应，因此相对链接在本地与线上都成立。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from docutils import nodes
from sphinx.application import Sphinx
from sphinx.util import logging

logger = logging.getLogger(__name__)

SOURCE_DIRS = {"zh_CN": "zh_CN", "en": "en"}

# 源文件中的软换行在 HTML 中显示为空格。两侧都是中日韩字符，或任一侧是全角标点时删除该换行；中文与西文之间保留一个空格
# （浏览器不实现 CSS Text 的分段换行转换；规则同 Pandoc 的 east_asian_line_breaks，并对全角标点放宽）
_WIDE = re.compile("[\u2e80-\u2fff\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
_PUNCT = re.compile("[\u3000-\u303f\uff00-\uffef]")
_BREAK = re.compile(r"[ \t]*\n[ \t]*")
_KEEP = (nodes.literal_block, nodes.literal, nodes.raw, nodes.comment, nodes.math, nodes.math_block)


def _docnames(src: Path) -> set[str]:
    out: set[str] = set()
    for p in src.rglob("*.md"):
        rel = p.relative_to(src)
        if rel.parts and rel.parts[0].startswith(("_", ".")):
            continue
        out.add(rel.with_suffix("").as_posix())
    return out


def check_parity(app: Sphinx, env: Any, docnames: list[str]) -> None:
    site = Path(app.config.drone4d_site_root)
    me = _docnames(Path(app.srcdir))
    for code, d in SOURCE_DIRS.items():
        if code == app.config.language:
            continue
        other = _docnames(site / d)
        for name in sorted(me - other):
            logger.warning("drone4d_site: %s has no counterpart in %s/", name, d)
        for name in sorted(other - me):
            logger.warning("drone4d_site: %s/%s.md has no counterpart in this language", d, name)


def _join(left: str, right: str) -> bool:
    if (left and _PUNCT.match(left)) or (right and _PUNCT.match(right)):
        return True
    return bool(left and right and _WIDE.match(left) and _WIDE.match(right))


def _edge(node: nodes.Node | None, last: bool) -> str:
    if node is None:
        return ""
    t = node.astext()
    return (t[-1:] if last else t[:1]) if t else ""


def join_cjk_lines(app: Sphinx, doctree: nodes.document) -> None:
    for text in list(doctree.findall(nodes.Text)):
        parent = text.parent
        src = str(text)
        if parent is None or "\n" not in src or any(isinstance(a, _KEEP) for a in _ancestors(parent)):
            continue
        idx = next(i for i, c in enumerate(parent.children) if c is text)  # Text 按值比较，必须按身份定位
        prev_node = parent[idx - 1] if idx > 0 else None
        next_node = parent[idx + 1] if idx + 1 < len(parent) else None

        def repl(
            m: re.Match,
            src: str = src,
            prev_node: nodes.Node | None = prev_node,
            next_node: nodes.Node | None = next_node,
        ) -> str:
            left = src[m.start() - 1] if m.start() > 0 else _edge(prev_node, last=True)
            right = src[m.end()] if m.end() < len(src) else _edge(next_node, last=False)
            return "" if _join(left, right) else "\n"

        joined = _BREAK.sub(repl, src)
        if joined != src:
            parent[idx] = nodes.Text(joined)  # Element.replace 同样按值定位，这里按下标替换


def _ancestors(node: nodes.Node):
    while node is not None:
        yield node
        node = node.parent


def page_context(app: Sphinx, pagename: str, templatename: str, context: dict, doctree: Any) -> None:
    html_ctx = app.config.html_context
    langs = html_ctx.get("drone4d_languages", [])
    current_lang = html_ctx.get("drone4d_current_language")
    versions = html_ctx.get("drone4d_versions", ["latest"])
    current_version = html_ctx.get("drone4d_current_version", "latest")
    depth = pagename.count("/")
    to_version_root = "../" * depth
    to_site_root = to_version_root + "../../"
    page = f"{pagename}.html"
    current_slug = next((slug for code, slug, _ in langs if code == current_lang), "en")
    context["drone4d_alternates"] = [
        {
            "code": code,
            "slug": slug,
            "name": name,
            "current": code == current_lang,
            "href": f"{to_site_root}{slug}/{current_version}/{page}",
            "abs": f"https://docs.agentnetwork.org.cn/drone4d/{slug}/{current_version}/{page}",
        }
        for code, slug, name in langs
    ]
    context["drone4d_versions_links"] = [
        {"name": v, "current": v == current_version, "href": f"{to_site_root}{current_slug}/{v}/{page}"}
        for v in versions
    ]
    context["drone4d_site_root_rel"] = to_site_root


def setup(app: Sphinx) -> dict[str, Any]:
    app.add_config_value("drone4d_site_root", "", "env")
    app.connect("env-before-read-docs", check_parity)
    app.connect("doctree-read", join_cjk_lines)
    app.connect("html-page-context", page_context)
    return {"version": "1.0", "parallel_read_safe": True, "parallel_write_safe": True}
