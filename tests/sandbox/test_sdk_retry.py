"""SDK 的限流重试按 problem+json 的 `retry_after_ms` 等待（D2-AC-09 第 1 轮：实体限流只差一个令牌时服务端给出几十毫秒，
SDK 此前按 `Retry-After: 1` 头等 1.0–1.2 s，实例增删改的调用因此超过 1 s）。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "tools" / "sdk"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

import awr_sandbox as A  # noqa: E402


def _http(handler) -> A._Http:
    h = A._Http("http://sbx.test", timeout_s=5.0, verify_tls=False)
    h.client.close()
    h.client = httpx.Client(base_url="http://sbx.test", transport=httpx.MockTransport(handler))
    return h


def _limited(ms: int | None) -> httpx.Response:
    body = {"type": "urn:awr:reason:111", "status": 429, "code": 111, "name": "RATE_LIMITED", "message": "限流",
            "detail": {"category": "sbx_entity"}, "remedy": "", "request_id": "r"}
    if ms is not None:
        body["retry_after_ms"] = ms
    return httpx.Response(429, json=body, headers={"Retry-After": "1"})


def test_retry_uses_problem_retry_after_ms() -> None:
    calls: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(time.monotonic())
        return _limited(40) if len(calls) == 1 else httpx.Response(200, json={"ok": True})

    h = _http(handler)
    t0 = time.monotonic()
    assert h.get("/api/sandbox/v1/sessions/sb-aaaaaaaaaa/vehicles") == {"ok": True}
    dt = time.monotonic() - t0
    assert len(calls) == 2 and 0.035 <= calls[1] - calls[0] < 0.5 and dt < 0.5


def test_error_retry_after_prefers_body_then_header() -> None:
    e = A._Http._error(_limited(55))
    assert e.code == 111 and abs(e.retry_after_s - 0.055) < 1e-9
    e2 = A._Http._error(_limited(None))
    assert e2.retry_after_s == 1.0
