"""problem+json 错误响应与原因码映射（AWR-17 §4.4、§8；`packages/contracts/rest/problem.schema.json`；M11-FR-080）。

`code` 必须存在于 `reasons.json`；HTTP 状态按原因表映射（未映射的码为 409）；`message`、`remedy` 取中文文案；
429 与 503 附 `Retry-After`（秒）。
"""

from __future__ import annotations

import os
import time
from typing import Any

from starlette.responses import JSONResponse

from awr.contracts.reasons import http_status
from awr.contracts.reasons import info as reason_info

__all__ = ["PROBLEM_MEDIA", "ApiProblem", "problem", "problem_body", "uuid7"]

PROBLEM_MEDIA = "application/problem+json"


def uuid7() -> str:
    """UUIDv7（毫秒时间戳 + 随机；Python 3.12 标准库没有 uuid7）。"""
    ms = time.time_ns() // 1_000_000
    rnd = int.from_bytes(os.urandom(10), "big")
    v = (ms & ((1 << 48) - 1)) << 80 | (0x7 << 76) | ((rnd >> 68) & 0xFFF) << 64 | (0b10 << 62) | (rnd & ((1 << 62) - 1))
    h = f"{v:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def problem_body(code: int, *, status: int | None = None, detail: Any = None, message: str | None = None,
                 remedy: str | None = None, request_id: str | None = None, retry_after_ms: int | None = None) -> dict:
    ri = reason_info(code)
    st = status if status is not None else http_status(code)
    body = {"type": f"urn:awr:reason:{int(code)}", "title": ri.name, "status": st, "code": int(code), "name": ri.name,
            "reason": ri.name, "message": message or ri.message_zh, "detail": detail, "remedy": remedy or ri.remedy_zh,
            "request_id": request_id or uuid7()}
    if retry_after_ms is not None:  # problem.schema.json：retry_after_ms 为整数，无值时省略
        body["retry_after_ms"] = int(retry_after_ms)
    return body


def problem(code: int, *, status: int | None = None, detail: Any = None, message: str | None = None,
            request_id: str | None = None, headers: dict[str, str] | None = None,
            retry_after_ms: int | None = None) -> JSONResponse:
    body = problem_body(code, status=status, detail=detail, message=message, request_id=request_id,
                        retry_after_ms=retry_after_ms)
    h = dict(headers or {})
    if body["status"] in (429, 503) and "Retry-After" not in h:
        h["Retry-After"] = str(max(1, int((retry_after_ms or 1000) / 1000)))
    h.setdefault("Cache-Control", "no-store")
    return JSONResponse(body, status_code=body["status"], headers=h, media_type=PROBLEM_MEDIA)


class ApiProblem(Exception):
    """路由内抛出，由 main 中的异常处理器转为 problem+json。"""

    def __init__(self, code: int, *, status: int | None = None, detail: Any = None, message: str | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(f"{code}")
        self.code = int(code)
        self.status = status
        self.detail = detail
        self.message = message
        self.headers = headers
