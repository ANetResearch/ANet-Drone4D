"""OpenAPI 操作摘要的英文文案（AWR-05 §7.10 英文规则 1；D3-AC-23；14 §21.15.7 CL-EN-02）。

FastAPI 对未写 `summary` 的操作以函数名生成 Title Case 摘要（`list_tasks` → "List Tasks"），不符合 sentence case；
`copy-lint`（`tools/lint/copy-lint.mjs`，kind openapi）扫描三份 OpenAPI 快照的 `summary` 与 `description`。本模块在应用
装配完成、首次生成 OpenAPI 文档之前，为没有显式摘要的操作写入 sentence case 摘要（"List tasks"），缩写词按
`ACRONYMS` 大写（"Token URL"）；已写显式摘要的操作不变。路由、operationId 与参数不受影响，只改文档文案。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi import routing as _routing
from fastapi.routing import APIRoute

__all__ = ["ACRONYMS", "apply_sentence_case_summaries", "sentence_case"]

# 函数名中出现的缩写词（小写 → 文案写法）；新增路由引入新的缩写时在此登记
ACRONYMS: dict[str, str] = {
    "api": "API", "url": "URL", "urls": "URLs", "id": "ID", "ids": "IDs", "ws": "WS", "rt": "RT", "sdk": "SDK", "gpu": "GPU",
    "cpu": "CPU", "rrd": "RRD", "mcap": "MCAP", "json": "JSON", "uav": "UAV", "uavs": "UAVs", "sse": "SSE", "jwt": "JWT",
    "pow": "PoW", "glb": "GLB", "csv": "CSV", "lidar": "LiDAR", "osm": "OSM", "ci": "CI", "ttl": "TTL",
}


def sentence_case(name: str) -> str:
    """函数名 → sentence case 摘要：下划线分词，首词首字母大写，其余小写，缩写词按 `ACRONYMS`。"""
    words = [w for w in name.split("_") if w]
    out: list[str] = []
    for i, w in enumerate(words):
        low = w.lower()
        if low in ACRONYMS:
            out.append(ACRONYMS[low])
        else:
            out.append(low[:1].upper() + low[1:] if i == 0 else low)
    return " ".join(out)


def _route_pairs(app: FastAPI) -> Iterator[tuple[APIRoute, Any]]:
    """(原始 APIRoute, 挂载后的有效路由上下文或 None)。FastAPI 0.14x 起 include_router 惰性挂载，OpenAPI 从有效路由上下文
    读取 `summary`（同 awr.bench.api.deps._routes 的做法）；更早的版本 app.routes 直接是复制后的 APIRoute。"""
    it = getattr(_routing, "iter_route_contexts", None)
    if it is None:
        for r in app.routes:
            if isinstance(r, APIRoute):
                yield r, None
        return
    for ctx in it(app.routes):
        orig = ctx.original_route
        if isinstance(orig, APIRoute):
            yield orig, getattr(ctx, "_route_context", None)


def apply_sentence_case_summaries(app: FastAPI) -> int:
    """为没有显式摘要、且出现在 OpenAPI 中的操作写入 sentence case 摘要；返回写入的操作数。在全部路由挂载之后、首次生成
    OpenAPI 文档之前调用（重复调用无副作用）。"""
    n = 0
    for orig, eff in _route_pairs(app):
        if not orig.include_in_schema:
            continue
        current = getattr(eff, "summary", None) if eff is not None else None
        if orig.summary and (eff is None or current):
            continue
        s = orig.summary or sentence_case(orig.name)
        orig.summary = s
        if eff is not None and hasattr(eff, "summary"):
            eff.summary = s
        n += 1
    app.openapi_schema = None
    return n
