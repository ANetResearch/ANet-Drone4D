"""操作席位宽限（AWR-12 §4.2.2 T01–T05；BIZ-FR-007；SK-E2E）：持有者最后一个 WS 连接关闭 → GRACE（写权限仍在）→ 宽限内同一
principal 重连 → HELD；宽限到期 → FREE，其他 principal 才能取得 operator token。宽限时长在测试中缩短为 0.6 s。
"""

from __future__ import annotations

import asyncio
import time

import httpx
import rtc
from websockets.asyncio.client import connect

PROTO = "awr.rt.v1"
HINT_A = "SEATGRACEPRINCIPALAAAAAA"
HINT_B = "SEATGRACEPRINCIPALBBBBBB"


def _token(base: str, hint: str) -> httpx.Response:
    return httpx.post(f"{base}/api/auth/token", json={"role": "operator", "principal_hint": hint}, timeout=10)


async def _session(stack, token: str) -> str:
    """连接、握手、读 serverInfo.seat，然后关闭。"""
    ws = await connect(stack.ws_url, subprotocols=[PROTO, "bearer." + token], origin=stack.origin, max_size=None,
                       compression=None, open_timeout=10)
    c = rtc.Client(ws)
    info, _, _ = await c.handshake()
    await ws.close()
    return info["seat"]


def _wait_seat(gw, state: str, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if gw.seat.get("state") == state:
            return
        time.sleep(0.05)
    raise AssertionError(f"seat {gw.seat} != {state}")


def test_seat_grace_resume_and_expiry(stack) -> None:
    gw = stack.app.state.awr.gateway
    gw.seat_grace_s = 0.6
    # 前一个用例可能留下持有者：等其宽限到期
    if gw.seat.get("holder") is not None:
        _wait_seat(gw, "FREE", 5.0)
    ra = _token(stack.base, HINT_A)
    assert ra.status_code == 200, ra.text
    tok_a = ra.json()
    assert tok_a["seat"] == "held"
    pid_a = tok_a["principal_id"]

    assert asyncio.run(_session(stack, tok_a["token"])) == "held"
    # T03：最后一个连接关闭 → GRACE；其他 principal 仍被拒绝（116）
    _wait_seat(gw, "GRACE")
    assert gw.seat["holder"] == pid_a
    rb = _token(stack.base, HINT_B)
    assert rb.status_code == 409 and rb.json()["code"] == 116, rb.text
    # T04：宽限内同一 principal 重连 → HELD（重连期间 GRACE 计时器取消）
    ws_task_seat = asyncio.run(_session(stack, tok_a["token"]))
    assert ws_task_seat == "held"
    # 再次关闭后重新进入 GRACE，T05：到期 → FREE
    _wait_seat(gw, "GRACE")
    _wait_seat(gw, "FREE", 5.0)
    assert gw.seat["holder"] is None
    rb = _token(stack.base, HINT_B)
    assert rb.status_code == 200 and rb.json()["seat"] == "held", rb.text
    # sim-core 侧席位权威一致：A 的 token 不再持有席位
    assert gw.seat["holder"] == rb.json()["principal_id"]
    tok_b = rb.json()["token"]
    assert asyncio.run(_session(stack, tok_b)) == "held"
    _wait_seat(gw, "FREE", 5.0)
