"""credit 窗口、ack、令牌桶与 L4 拥塞（M11-AC-014、AC-016、AC-017；M11-FR-040、FR-041、FR-043、FR-044；AWR-17 §6.9）。

- §6.4.5 表中四种情形的 W（`ping.srttMs` 与"发出到被 ack 覆盖"回退两种来源；`srttMs` 超过 5 s 未更新改用回退）；
- 窗口满时数据帧 0 而控制面照常；ack 恢复后只发最新值、不补积压；
- `maxKbps = 2048` 时 priority 0 通道实际频率不下降，priority 3 通道出现顺延且最终送达，每帧首条记录不受预算限制；
- 单次 send > 200 ms（或连续 3 次 > 50 ms）→ `status net.congested`，期间控制面照常、不触发 1013；恢复后 `removeStatus`。
"""

from __future__ import annotations

import asyncio
import json
import time

from gwstub import StubGw, chan, make_session, records_of

from awr.api.rt.scheduler import TokenBucket, credit_window
from awr.contracts import frame as F


def _sub(s, sid: int, topic: str, rate: int) -> None:
    s.subscribe([{"id": sid, "topic": topic, "rate": rate, "mode": "latest"}])


def test_window_formula_table() -> None:
    assert credit_window(60, 0.001) == 6
    assert credit_window(60, 0.033) == 7
    assert credit_window(60, 0.040) == 8
    assert credit_window(60, 0.200) == 8  # 上限
    assert credit_window(10, 0.050) == 3  # 下限
    assert credit_window(10, 0.005) == 3


def test_window_sources_srtt_report_and_fallback() -> None:
    gw = StubGw()
    chan(gw, "uav/a1/state", {"kind": "uav", "id": "a1"})
    s = make_session(gw)
    _sub(s, 1, "uav/a1/state", 60)
    s.on_ping(1.0, 1.0)  # 客户端上报 srtt 1 ms
    s.recompute_window()
    assert s.window == 6
    s.on_ping(2.0, 40.0)
    s.recompute_window()
    assert s.window == 8
    # 超过 5 s 未更新：改用"发出到被 ack 覆盖"的最小时延（此处 33 ms）
    s.srtt_client_mono = time.monotonic_ns() - 6_000_000_000
    s.ack_delay.extend([33_000_000, 40_000_000])
    s.recompute_window()
    assert s.window == 7
    s.ack_delay.clear()
    s.recompute_window()
    assert s.window == 6  # 两者都没有：5 ms
    s2 = make_session(gw)
    gw.registry.add_fixed(2, "swarm/uav/state")
    _sub(s2, 1, "swarm/uav/state", 10)
    s2.recompute_window()
    assert s2.window == 3


def test_window_full_blocks_data_not_control_and_no_backlog() -> None:
    gw = StubGw()
    ch = chan(gw, "uav/a1/state", {"kind": "uav", "id": "a1"})
    s = make_session(gw, window=3)
    _sub(s, 1, "uav/a1/state", 60)
    sent = 0
    for k in range(1, 30):
        ch.publish(bytes([k]) * 64, k)
        s.mark_due(k)
        if s.can_assemble():
            assert s.assemble() is not None
            sent += 1
    assert sent == 3 and s.stats["credit_skips"] > 0
    s.send_ctrl({"op": "pong", "t": 1, "server_ns": 0, "sim_ns": 0, "epoch": 1})
    assert len(s.ctrl) >= 1  # 控制面照常入队
    s.on_ack(3)
    ch.publish(b"\xff" * 64, 99)
    s.mark_due(30)
    f = s.assemble()
    recs = records_of(f)
    assert len(recs) == 1 and recs[0].seq == ch.seq  # 只发最新值，不补积压
    assert not s.can_assemble() or not s.has_due


def test_bucket_priority_and_first_record() -> None:
    gw = StubGw()
    t = [0.0]
    hi = chan(gw, "uav/a1/state", {"kind": "uav", "id": "a1"})  # priority 0，64 B
    lo = gw.registry.add_fixed(9, "mission/m1/status")  # priority 2
    lo.priority = 3
    s = make_session(gw)
    s.bucket = TokenBucket(clock=lambda: t[0])
    s.max_kbps = 2048
    s.bucket.retune(None, 2048)
    assert s.bucket.rate == 256000
    _sub(s, 1, "uav/a1/state", 60)
    _sub(s, 2, "mission/m1/status", 30)
    hi_n = lo_n = 0
    for k in range(1, 121):
        t[0] = k / 60
        hi.publish(bytes([k % 256]) * 64, k)
        lo.publish(bytes([k % 256]) * 32768, k)
        s.mark_due(k)
        if s.can_assemble():
            f = s.assemble()
            if f is not None:
                ids = [r.channel_id for r in records_of(f)]
                hi_n += ids.count(hi.id)
                lo_n += ids.count(lo.id)
                s.on_ack(F.decode_batch_header(f).frame_seq)
    assert hi_n >= 115  # 最高优先级不降频（每帧首条记录不受预算限制）
    assert s.stats["bucket_defers"] > 0
    assert 0 < lo_n < 60  # 低优先级被顺延但最终送达
    # 首条记录超预算也放行
    s2 = make_session(gw)
    s2.bucket = TokenBucket(rate_bps=1.0, depth_b=10, clock=lambda: t[0])
    _sub(s2, 1, "mission/m1/status", 30)
    s2.mark_due(1000)
    f = s2.assemble()
    assert f is not None and len(records_of(f)) == 1


def test_congestion_status_and_recovery() -> None:
    async def run() -> None:
        gw = StubGw()
        ch = chan(gw, "uav/a1/state", {"kind": "uav", "id": "a1"})
        s = make_session(gw)
        ws = s.ws
        _sub(s, 1, "uav/a1/state", 60)
        task = asyncio.ensure_future(s.sender())
        ws.delays = {3: 0.25}  # 第 4 次发送卡住 250 ms（> 200 ms）
        for k in range(1, 20):
            ch.publish(bytes([k]) * 64, k)
            s.mark_due(k)
            s.on_ack(s.frame_seq)
            await asyncio.sleep(1 / 60)
        texts = [json.loads(m) for m in ws.sent if isinstance(m, str)]
        assert any(m.get("op") == "status" and m["id"] == "net.congested" for m in texts)
        # 拥塞期间数据面暂停，控制面照常（TIME 等控制消息 < 20 ms 连续 3 次后恢复）
        for _ in range(4):
            s.send_ctrl(b"\x02" + b"\0" * 23)
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        texts = [json.loads(m) for m in ws.sent if isinstance(m, str)]
        assert any(m.get("op") == "removeStatus" and m["ids"] == ["net.congested"] for m in texts)
        assert not s.congested and ws.closed is None and s.stats["congestions"] == 1
        n0 = s.stats["frames"]
        for k in range(20, 30):
            ch.publish(bytes([k]) * 64, k)
            s.mark_due(k)
            s.on_ack(s.frame_seq)
            await asyncio.sleep(1 / 60)
        assert s.stats["frames"] > n0
        s.closing = True
        s.wake.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(run())


def test_ctrl_backlog_closes_1013() -> None:
    async def run() -> None:
        gw = StubGw()
        s = make_session(gw)
        for i in range(1024):
            s.send_ctrl({"op": "status", "id": f"x{i}", "level": "info", "message": "m"})
        assert not s.closing and s.stats["ctrl_hwm"] == 1024
        s.send_ctrl({"op": "status", "id": "overflow", "level": "info", "message": "m"})
        assert s.closing
        await asyncio.sleep(0.05)
        assert s.ws.closed == 1013
        st = json.loads(s.ws.sent[-1])
        assert st["op"] == "status" and st["code"] == 318 and st["level"] == "error"

    asyncio.run(run())
