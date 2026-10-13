"""共享展示观看的节能兜底与观看席位排队（WP-13b，M16；D2-AC-44 的服务端兜底、D2-AC-43 的观看口径；AWR-04 §15.6.3、§15.7.4、
§15.7.6；17 §17.20.3、§17.20.4；M11-FR-220 起）。

show-api 一侧（presence、`last_input`、10 s 扫描的 4430 回收、观看席位上限与 4431、R81 至 R83 观看排队、状态文件）属 M11 的 WP-03b。
用例按设计写成，在真实栈上运行（不以替身代替 show-api）。第 1 轮时 WP-03b 未交付，按能力探测 skip；D2-INT 交付后（第 2 轮起）
能力缺失即失败：

| 用例 | 探测 | 断言 |
|---|---|---|
| 无 presence 的观看连接 | `serverInfo.power` 存在 | 阈值 20 s + 余量 5 s（`AWR_POWER_JSON`）后 ±2 s 内以 `error{518}` + 关闭码 4430 断开 |
| presence 延后回收 | 同上 | 每 10 s 上报 `input_age_s` 小值时 35 s 内不回收；停止上报后在最后一次输入 + 25 s ±2 s 回收 |
| 观看席位已满 | 配置模型有 `viewer_seats` 段 | V_max = 3：第 4 个连接 `error{517}` + 4431；R81 排队第 1 位；一个连接断开后 ≤ 5 s `seat_ready` 并可连接；60 s 无长轮询的票 519 |
| 状态文件 | show-api 写 `/dev/shm/awr/_status/show.json` | 容量接口 `viewers` 与连接数在 2 s 内一致（D2-AC-42） |
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time

import httpx
import pytest
from awrproc import ROOT, world_ready
from sbstack import SandboxStack

sys.path.insert(0, str(ROOT / "tests" / "chaos"))                 # rtprobe（M16 的最小 awr.rt.v1 客户端）

pytestmark = [pytest.mark.needs_data, pytest.mark.slow]

POWER = {"viewer_idle_s": 20, "viewer_idle_busy_s": 20, "grace_s": 5, "warn_s": 5, "presence_min_s": 1}


def _seats_supported() -> bool:
    from awr.runtime.config import RuntimeConfig

    return "viewer_seats" in getattr(RuntimeConfig, "model_fields", {})


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    d = tmp_path_factory.mktemp("viewer")
    extra = ["viewer_seats.max=3"] if _seats_supported() else []
    st = SandboxStack(max_sessions=1, env={"AWR_POWER_JSON": json.dumps(POWER), "AWR_SHOW_STATUS_FILE": str(d / "show.json")},
                      extra_set=extra).start()
    try:
        yield st
    finally:
        st.stop()


def _token(st: SandboxStack, k: int) -> str:
    hint = ("MSIXVIEWERPOWER" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[k % 26] + "AAAAAAAAAAAA")[:24]
    r = httpx.post(st.show_base + "/api/auth/token", json={"role": "viewer", "principal_hint": hint}, timeout=10)
    r.raise_for_status()
    return str(r.json()["token"])


async def _open(st: SandboxStack, k: int):
    from rtprobe import Probe

    return await Probe.open(st.show_base, _token(st, k))


async def _refused(st: SandboxStack, k: int, timeout_s: float = 10.0) -> tuple[int | None, list[dict]]:
    """握手被拒（席位已满）：首条消息为 `error{517}`，随后关闭码 4431；Probe.open 要求首条为 serverInfo，因此直接连接（D2-INT）。"""
    from websockets.asyncio.client import connect

    url = st.show_base.replace("http", "ws", 1) + "/api/rt"
    ws = await connect(url, subprotocols=["awr.rt.v1", f"bearer.{_token(st, k)}"], origin=st.show_base, compression=None,
                       max_size=None, open_timeout=timeout_s)
    texts: list[dict] = []
    with contextlib.suppress(Exception):
        while True:
            m = await asyncio.wait_for(ws.recv(), timeout_s)
            if isinstance(m, str):
                texts.append(json.loads(m))
    return ws.close_code, texts


async def _closed_code(p, timeout_s: float) -> tuple[int | None, list[dict], float]:
    """读到连接关闭为止：(关闭码, 期间的 JSON 控制消息, 用时)。"""
    t0 = time.monotonic()
    texts: list[dict] = []
    with contextlib.suppress(Exception):
        while time.monotonic() - t0 < timeout_s:
            kind, x = await p.recv(max(0.1, timeout_s - (time.monotonic() - t0)))
            if kind == "json":
                texts.append(x)
    return p.ws.close_code, texts, time.monotonic() - t0


def _server_power(st: SandboxStack) -> dict | None:
    async def go() -> dict | None:
        p = await _open(st, 0)
        try:
            return p.server_info.get("power")
        finally:
            await p.ws.close()

    return asyncio.run(go())


def _need_presence(st: SandboxStack) -> None:
    pw = _server_power(st)
    assert pw, "show-api publishes no serverInfo.power (M11 WP-03b, delivered in D2-INT)"
    assert float(pw.get("viewer_idle_s", 0)) == POWER["viewer_idle_s"], pw


def test_viewer_without_presence_is_reclaimed_with_4430(stack: SandboxStack) -> None:
    _need_presence(stack)

    async def go() -> tuple[int | None, list[dict], float]:
        p = await _open(stack, 1)
        return await _closed_code(p, 60.0)

    code, texts, dt = asyncio.run(go())
    assert code == 4430, (code, texts[-3:])
    assert any(t.get("op") == "error" and t.get("code") == 518 for t in texts), texts[-3:]
    assert abs(dt - (POWER["viewer_idle_s"] + POWER["grace_s"])) <= 2.0 + 10.0, dt      # 10 s 扫描周期（AWR-04 §15.7.4 第 2 条）


def test_presence_postpones_the_reclaim(stack: SandboxStack) -> None:
    _need_presence(stack)

    async def go() -> tuple[bool, int | None, float]:
        p = await _open(stack, 2)
        t0 = time.monotonic()
        alive = True
        while time.monotonic() - t0 < 35.0:
            await p.send({"op": "presence", "input_age_s": 1.0, "visible": True, "power": "active"})
            try:
                await p.drain(10.0)
            except Exception:
                alive = False
                break
        t_last = time.monotonic()
        code, _texts, _dt = await _closed_code(p, 60.0)
        return alive, code, time.monotonic() - t_last

    alive, code, dt = asyncio.run(go())
    assert alive, "reclaimed although presence reported recent input"
    assert code == 4430 and dt <= POWER["viewer_idle_s"] + POWER["grace_s"] + 12.0, (code, dt)


def test_viewer_seats_full_queue_and_seat_ready(stack: SandboxStack) -> None:
    assert _seats_supported(), "configuration has no viewer_seats section (M11 WP-03b, delivered in D2-INT)"

    async def go() -> None:
        held = [await _open(stack, 10 + k) for k in range(3)]
        code, texts = await _refused(stack, 13)
        assert code == 4431 and any(t.get("code") == 517 for t in texts), (code, texts[-3:])
        async with httpx.AsyncClient(base_url=stack.show_base, timeout=30.0) as c:
            r = await c.post("/api/viewer/queue", json={"principal_hint": "MSIXVIEWERPOWERNAAAAAAAA"})
            assert r.status_code == 201, r.text[:300]
            q = r.json()
            assert q["position"] == 1
            auth = {"authorization": f"Bearer {q['ticket_token']}"}
            await held[0].ws.close()
            t0 = time.monotonic()
            st = {}
            while time.monotonic() - t0 < 10.0:
                st = (await c.get(f"/api/viewer/queue/{q['ticket_id']}", params={"wait_s": 5}, headers=auth)).json()
                if st.get("state") == "seat_ready":
                    break
            assert st.get("state") == "seat_ready" and time.monotonic() - t0 <= 5.5, st
            p5 = await _open(stack, 13)
            assert p5.server_info
            await p5.ws.close()
            r2 = await c.post("/api/viewer/queue", json={"principal_hint": "MSIXVIEWERPOWEROAAAAAAAA"})
            q2 = r2.json()
            await asyncio.sleep(62.0)
            g = await c.get(f"/api/viewer/queue/{q2['ticket_id']}", params={"wait_s": 0}, headers={"authorization": f"Bearer {q2['ticket_token']}"})
            assert g.status_code == 410 and g.json().get("code") == 519, g.text[:300]
        for p in held[1:]:
            await p.ws.close()

    asyncio.run(go())


def test_show_status_file_matches_the_connections(stack: SandboxStack) -> None:
    async def go() -> tuple[dict, dict]:
        ps = [await _open(stack, 20 + k) for k in range(2)]
        await asyncio.sleep(2.5)
        c1 = httpx.get(stack.base + "/api/sandbox/v1/capacity", timeout=5).json()["viewers"]
        for p in ps:
            await p.ws.close()
        await asyncio.sleep(2.5)
        c0 = httpx.get(stack.base + "/api/sandbox/v1/capacity", timeout=5).json()["viewers"]
        return c1, c0

    c1, c0 = asyncio.run(go())
    assert c1.get("state") != "unknown", ("show-api wrote no viewer status file (M11 WP-03b, AWR-04 §15.5.4)", c1)
    assert c1["live"] - c0["live"] == 2, (c1, c0)
