"""尾帧、编码一次与发送时装帧（M11-AC-013；M11-FR-033、FR-037 至 FR-039、FR-042、FR-043；AWR-17 §6.8 性质 1–4）。

以 gwstub 按 tick 手动驱动 ClientSession.mark_due / assemble / on_ack（不经网络），逐项验证：
- 源发布到 seq 148 后停止：10、30、60 Hz 订阅者最后都收到 148；
- "30 Hz channel 在到期 tick 被 credit 挡住、随后源停止"仍收到最后值（原型逻辑在此场景失败）；
- sender 每 3 tick 才取一次：只发布一次（seq 1）后停止的 channel 最终必收到 seq 1，`slot_overwrites` > 0，frame_seq 连续；
- 订阅尚无值的 channel 不产生记录，首值到达后的帧带 SNAPSHOT；
- 10 个同 rate 客户端的编码次数等于发布次数；每条记录 payload 起点 % 8 == 0；帧内 roster 在 swarm 之前。
"""

from __future__ import annotations

from gwstub import StubGw, chan, make_session, records_of

from awr.contracts import frame as F


def _sub(s, sid: int, topic: str, rate: int) -> None:
    s.subscribe([{"id": sid, "topic": topic, "rate": rate, "mode": "latest"}])


def _send(s) -> bytes | None:
    if not s.can_assemble():
        return None
    return s.assemble()


def test_tail_frame_all_rates() -> None:
    gw = StubGw()
    ch = chan(gw, "uav/a1/state", {"kind": "uav", "id": "a1"})
    sess = {r: make_session(gw) for r in (10, 30, 60)}
    for r, s in sess.items():
        _sub(s, 1, "uav/a1/state", r)
    last: dict[int, int] = {}
    for k in range(1, 200):
        if ch.seq < 148:
            ch.publish(b"\1" * 64, k * 1000)
            if ch.seq < 148:
                ch.publish(b"\2" * 64, k * 1000 + 500)  # 125 Hz 源：约每 tick 2 次
        for r, s in sess.items():
            s.mark_due(k)
            f = _send(s)
            if f is not None:
                for rec in records_of(f):
                    last[r] = rec.seq
                s.on_ack(F.decode_batch_header(f).frame_seq)
    assert ch.seq == 148
    assert last == {10: 148, 30: 148, 60: 148}


def test_credit_blocked_then_source_stops_keeps_tail() -> None:
    gw = StubGw()
    ch = chan(gw, "uav/a1/state", {"kind": "uav", "id": "a1"})
    s = make_session(gw, window=3)
    _sub(s, 1, "uav/a1/state", 30)
    got: list[int] = []
    unacked: list[int] = []
    for k in range(1, 40):
        if k <= 20:
            ch.publish(bytes([k]) * 64, k * 1000)
        s.window = 3
        s.mark_due(k)
        f = _send(s)
        if f is not None:
            got += [r.seq for r in records_of(f)]
            unacked.append(F.decode_batch_header(f).frame_seq)
        # 12..24 tick 不 ack：窗口在源的最后一次发布（k = 20，偶数 tick 到期）时已满
        if not 12 <= k <= 24 and unacked:
            s.on_ack(unacked[-1])
            unacked.clear()
    assert s.stats["credit_skips"] > 0
    assert got[-1] == 20 == ch.seq


def test_slow_sender_overwrites_keep_single_publish() -> None:
    gw = StubGw()
    once = chan(gw, "uav/a1/state", {"kind": "uav", "id": "a1"})
    busy = chan(gw, "uav/a2/state", {"kind": "uav", "id": "a2"})
    s = make_session(gw)
    _sub(s, 1, "uav/a1/state", 60)
    _sub(s, 2, "uav/a2/state", 60)
    for sc in s.order:  # 订阅快照已发出的稳态（只看单槽覆盖）
        sc.snapshot = False
    seen_once: list[int] = []
    seqs: list[int] = []
    for k in range(1, 30):
        if k == 1:
            once.publish(b"\7" * 64, 1)
        busy.publish(bytes([k]) * 64, k)
        s.mark_due(k)
        if k % 3 == 0:  # 人为让 sender 每 3 tick 才取一次
            f = _send(s)
            if f is not None:
                h = F.decode_batch_header(f)
                seqs.append(h.frame_seq)
                seen_once += [r.seq for r in records_of(f) if r.channel_id == once.id]
                s.on_ack(h.frame_seq)
    assert seen_once == [1]
    assert s.stats["slot_overwrites"] > 0
    assert seqs == list(range(1, len(seqs) + 1))


def test_no_value_then_snapshot_and_priority_order() -> None:
    gw = StubGw()
    roster = gw.registry.add_fixed(1, "fleet/roster")
    swarm = gw.registry.add_fixed(2, "swarm/uav/state")
    s = make_session(gw)
    _sub(s, 1, "swarm/uav/state", 10)
    _sub(s, 2, "fleet/roster", 10)
    for k in range(1, 20):
        s.mark_due(k)
        assert _send(s) is None  # 尚无值：不产生记录，不发帧
    swarm.publish(b"\0" * 32 * 4, 5)
    roster.publish(b"\x80", 5)
    s.mark_due(20)
    f = _send(s)
    assert f is not None
    h = F.decode_batch_header(f)
    assert h.flags & F.BATCH_SNAPSHOT and h.frame_seq == 1
    recs = records_of(f)
    assert [r.channel_id for r in recs] == [roster.id, swarm.id]  # roster 恒在 swarm 之前
    assert all(r.payload_off % 8 == 0 for r in recs)


def test_encode_once_across_clients() -> None:
    gw = StubGw()
    ch = chan(gw, "uav/a1/state", {"kind": "uav", "id": "a1"})
    clients = [make_session(gw) for _ in range(10)]
    for s in clients:
        _sub(s, 1, "uav/a1/state", 60)
    pubs = 0
    for k in range(1, 61):
        gw.frame_t_sim_ns = k * 16_000_000
        ch.publish(bytes([k % 256]) * 64, gw.frame_t_sim_ns)
        pubs += 1
        for s in clients:
            s.mark_due(k)
            f = _send(s)
            assert f is not None
            s.on_ack(F.decode_batch_header(f).frame_seq)
    assert ch.encodes == pubs
    assert ch.hits == pubs * 9
