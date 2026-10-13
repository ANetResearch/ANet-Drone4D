"""drone4d_api：由 packages/contracts 中的 OpenAPI 快照生成端点表（ANet Drone4D 文档站）。

用法（MyST）::

    ```{drone4d-endpoints} packages/contracts/rest/sandbox.openapi.snapshot.json
    :kind: sandbox
    ```

``kind`` 取 ``sandbox``（沙盒 REST v1，前缀取快照的 servers[0].url）或 ``runtime``（本机运行时接口）。
沙盒快照自带中文 summary 与 ``x-awr`` 元数据（SB 编号、鉴权、优先级）；英文说明与运行时接口的中英文说明取自
同目录的 api_summaries.json。快照中出现而说明文件缺少的 operationId 以告警报告，``sphinx-build -W`` 时构建失败，
从而保证端点表与快照同步。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, ClassVar

from docutils import nodes
from docutils.parsers.rst import directives
from sphinx.application import Sphinx
from sphinx.util import logging
from sphinx.util.docutils import SphinxDirective

logger = logging.getLogger(__name__)

SUMMARIES = Path(__file__).resolve().parent / "api_summaries.json"
METHOD_ORDER = {"get": 0, "post": 1, "put": 2, "patch": 3, "delete": 4}

TEXT = {
    "zh": {
        "snapshot": "快照",
        "contracts": "契约",
        "ops": "个操作",
        "prefix": "路径前缀",
        "cols_sandbox": ("编号", "方法", "路径", "说明", "鉴权", "优先级"),
        "cols_runtime": ("方法", "路径", "说明"),
        "source": "来源",
    },
    "en": {
        "snapshot": "Snapshot",
        "contracts": "contracts",
        "ops": "operations",
        "prefix": "Path prefix",
        "cols_sandbox": ("ID", "Method", "Path", "Description", "Auth", "Priority"),
        "cols_runtime": ("Method", "Path", "Description"),
        "source": "Source",
    },
}


def _lang(env: Any) -> str:
    return "zh" if str(env.config.language or "en").startswith("zh") else "en"


def _sb_key(op_id: str) -> tuple[int, str]:
    m = re.match(r"sb_(\d+)(?:_(\w+))?$", op_id)
    return (int(m.group(1)), m.group(2) or "") if m else (10_000, op_id)


def _table(header: tuple[str, ...], rows: list[list[nodes.Node | str]], widths: list[int]) -> nodes.table:
    table = nodes.table(classes=["drone4d-endpoints"])
    tgroup = nodes.tgroup(cols=len(header))
    table += tgroup
    for w in widths:
        tgroup += nodes.colspec(colwidth=w)
    thead = nodes.thead()
    tgroup += thead
    hrow = nodes.row()
    for h in header:
        entry = nodes.entry()
        entry += nodes.paragraph(text=h)
        hrow += entry
    thead += hrow
    tbody = nodes.tbody()
    tgroup += tbody
    for r in rows:
        row = nodes.row()
        for cell in r:
            entry = nodes.entry()
            para = nodes.paragraph()
            para += cell if isinstance(cell, nodes.Node) else nodes.Text(cell)
            entry += para
            row += entry
        tbody += row
    return table


class EndpointsDirective(SphinxDirective):
    required_arguments = 1
    has_content = False
    option_spec: ClassVar[dict[str, Any]] = {"kind": lambda a: directives.choice(a, ("sandbox", "runtime"))}

    def run(self) -> list[nodes.Node]:
        repo = Path(self.env.config.drone4d_repo_root)
        rel = self.arguments[0]
        spec_path = (repo / rel).resolve()
        kind = self.options.get("kind", "sandbox")
        lang = _lang(self.env)
        t = TEXT[lang]
        if not spec_path.is_file():
            logger.warning("drone4d-endpoints: snapshot not found: %s", spec_path, location=self.get_location())
            return []
        self.env.note_dependency(str(spec_path))
        self.env.note_dependency(str(SUMMARIES))
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        tr = json.loads(SUMMARIES.read_text(encoding="utf-8"))

        ops: list[dict[str, Any]] = []
        for path, item in spec.get("paths", {}).items():
            for method, op in item.items():
                if method not in METHOD_ORDER:
                    continue
                ops.append({"path": path, "method": method, **op})

        out: list[nodes.Node] = []
        info = spec.get("info", {})
        prefix = (spec.get("servers") or [{}])[0].get("url", "")
        meta = nodes.paragraph(classes=["drone4d-endpoints-meta"])
        colon, comma, stop = ("：", "，", "。") if lang == "zh" else (": ", ", ", ". ")
        meta += nodes.strong(text=f"{t['snapshot']}{colon}")
        title, version = str(info.get("title", "")), str(info.get("version", ""))
        meta += nodes.Text(title if title.endswith(f"v{version}") else f"{title} v{version}")
        if info.get("x-awr-contracts"):
            meta += nodes.Text(f"{comma}{t['contracts']} {info['x-awr-contracts']}")
        meta += nodes.Text(f"{comma}{len(ops)} {t['ops']}{stop}{t['source']}{colon}")
        meta += nodes.literal(text=rel)
        if prefix:
            meta += nodes.Text(f"{stop}{t['prefix']}{colon}")
            meta += nodes.literal(text=prefix)
        meta += nodes.Text(stop.strip())
        out.append(meta)

        groups_tr = tr["groups"][kind]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for op in ops:
            tags = op.get("tags") or []
            if kind == "sandbox":
                g = tags[0] if tags else "sandbox-api"
            else:
                g = tags[0] if tags else ("worlds" if op["path"].startswith("/api/world") else "sys")
            grouped.setdefault(g, []).append(op)

        order = list(groups_tr.keys())
        for g in sorted(grouped, key=lambda k: order.index(k) if k in order else len(order)):
            if g not in groups_tr:
                logger.warning(
                    "drone4d-endpoints: no group title for %r in api_summaries.json", g, location=self.get_location()
                )
            title = groups_tr.get(g, {}).get(lang, g)
            out.append(nodes.rubric(text=title, classes=["drone4d-endpoints-group"]))
            items = grouped[g]
            rows: list[list[nodes.Node | str]] = []
            if kind == "sandbox":
                items.sort(key=lambda o: (_sb_key(o.get("operationId", "")), METHOD_ORDER[o["method"]]))
                for o in items:
                    oid = o.get("operationId", "")
                    x = o.get("x-awr") or {}
                    if lang == "zh":
                        desc = o.get("summary") or ""
                    else:
                        desc = tr["sandbox"].get(oid)
                        if desc is None:
                            logger.warning(
                                "drone4d-endpoints: no English summary for %s (%s %s)",
                                oid,
                                o["method"].upper(),
                                o["path"],
                                location=self.get_location(),
                            )
                            desc = o.get("summary") or ""
                    auth = tr["auth"].get(x.get("auth", ""), {}).get(lang, x.get("auth", ""))
                    rows.append(
                        [
                            x.get("sb", oid),
                            nodes.literal(text=o["method"].upper()),
                            nodes.literal(text=o["path"]),
                            desc,
                            auth,
                            x.get("priority", ""),
                        ]
                    )
                out.append(_table(t["cols_sandbox"], rows, [8, 8, 36, 40, 12, 8]))
            else:
                items.sort(key=lambda o: (o["path"], METHOD_ORDER[o["method"]]))
                for o in items:
                    oid = o.get("operationId", "")
                    entry = tr["runtime"].get(oid)
                    if entry is None:
                        logger.warning(
                            "drone4d-endpoints: no summary for %s (%s %s)",
                            oid,
                            o["method"].upper(),
                            o["path"],
                            location=self.get_location(),
                        )
                        desc = o.get("summary") or ""
                    else:
                        desc = entry[lang]
                    rows.append([nodes.literal(text=o["method"].upper()), nodes.literal(text=o["path"]), desc])
                out.append(_table(t["cols_runtime"], rows, [10, 45, 45]))

        known = set(tr["sandbox"]) if kind == "sandbox" else set(tr["runtime"])
        stale = sorted(known - {o.get("operationId", "") for o in ops})
        if stale and lang == "en":
            logger.warning(
                "drone4d-endpoints: api_summaries.json has entries not in %s: %s",
                rel,
                ", ".join(stale),
                location=self.get_location(),
            )
        return out


def setup(app: Sphinx) -> dict[str, Any]:
    app.add_config_value("drone4d_repo_root", "", "env")
    app.add_directive("drone4d-endpoints", EndpointsDirective)
    return {"version": "1.0", "parallel_read_safe": True, "parallel_write_safe": True}
