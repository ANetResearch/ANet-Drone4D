"""驻留与恢复的 broker 纯逻辑（注入时钟、记录副作用；M17-FR-069 至 FR-074、FR-076；AWR-04 §15.6、§15.7.4；M17-AC-026 至 030
的逻辑部分）：驻留请求与字段、槽位口径（PARKED 不计）、寿命冻结与恢复寿命、满员恢复票（FIFO、不插队）、服务端兜底驻留（提前告知、
新交互取消、手动控制不驻留）、无连接驻留、保留到期与驻留上限、恢复失败退回配置快照、结束驻留会话、抢占与回收改为驻留、内部档
（预留槽位、队首、免工作量证明、寿命 8 h、免抢占、每身份上限、身份撤销降档）、落盘往返。"""

from __future__ import annotations

from typing import Any

from brokerkit import create, finish_discards, finish_parks, finish_stops, hint, make_broker, ready

from awr.runtime.sbauth import peek_token_payload
from awr.sandbox.capacity import pool_stats
from awr.sandbox.ladder import LadderInput
from awr.sandbox.model import ACTIVE, DRAINING, ENDED, IDLE, PARKED, PARKING, READY, SPAWNING

IDENT = {"jti": "0123456789abcdef", "sub": "p-" + "ab" * 16, "exp": 2_000_000_000}


def _active(b: Any, i: int = 1, **kw: Any) -> str:
    sid = create(b, i, **kw)["result"]["session"]["sid"]
    ready(b, sid)
    b.activity({sid: {"ws_n": 1, "api_calls": 1}})
    assert b.sessions[sid].state == ACTIVE
    return sid


def _ev(fx: Any, kind: str, sid: str | None = None) -> list[dict]:
    return [d for k, s, d, _v in fx.events if k == kind and (sid is None or s == sid)]


def test_park_request_fields_and_slot_release() -> None:
    b, fx, c = make_broker(n_max=1)
    sid = _active(b)
    exp0 = b.sessions[sid].expires
    c.adv(100)
    r = b.park(sid, "idle")
    assert r["http"] == 202 and r["result"]["state"] == PARKING and fx.parked == [sid]
    assert _ev(fx, "sandbox.parking", sid) == [{"reason": "idle"}]
    assert b.free_slots() == 0                                         # PARKING 仍占槽位
    assert b.park(sid)["http"] == 202
    finish_parks(b, fx, t_sim_ns=42_000_000_000)
    rec = b.sessions[sid]
    assert rec.state == PARKED and b.free_slots() == 1                 # PARKED 不计
    w = b.wire(rec)
    assert w["park_reason"] == "idle" and w["resume_mode"] == "checkpoint" and w["t_sim_ns"] == "42000000000"
    assert abs(w["remaining_s"] - (exp0 - 100 - 1_791_000_000.0)) < 1 and "binding" not in w and w["renewable"] is False
    assert int(w["retention_unix_ns"]) - int(w["parked_unix_ns"]) == 3600 * 10**9
    assert _ev(fx, "sandbox.parked", sid)[0]["resume_mode"] == "checkpoint"
    c.adv(1000)                                                         # 寿命冻结
    assert b.wire(b.sessions[sid])["remaining_s"] == w["remaining_s"]
    assert b.park(sid)["http"] == 200
    for op in (b.keepalive(sid), b.template(sid, "t1_point_watch"), b.rate(sid, 2), b.world_switch(sid, "shenzhen")):
        assert op["code"] == 515 and op["detail"]["resume_url"].endswith(f"/{sid}/resume")
    other = create(b, 2)
    assert other["http"] == 201                                         # 槽位已释放


def test_park_rejected_states() -> None:
    b, fx, _c = make_broker()
    sid = create(b, 1)["result"]["session"]["sid"]
    assert b.park(sid)["code"] == 513                                   # SPAWNING
    ready(b, sid)
    b.stop(sid)
    assert b.park(sid)["code"] == 513                                   # DRAINING
    finish_stops(b, fx)
    assert b.park(sid)["code"] == 506
    assert b.park("sb-aaaaaaaaaa")["code"] == 305


def test_resume_with_room_restores_lifetime_and_epoch() -> None:
    b, fx, c = make_broker(n_max=2)
    sid = _active(b)
    rec = b.sessions[sid]
    c.adv(1700)                                                         # 剩余约 100 s：恢复后至少 300 s
    b.park(sid, "user")
    finish_parks(b, fx)
    held = rec.held_total_s
    assert 1699 <= held <= 1701
    c.adv(900)
    e0 = rec.epoch
    r = b.resume(sid)
    assert r["http"] == 201 and r["result"]["token"].startswith("v1.") and fx.resumed == [sid]
    assert rec.state == SPAWNING and rec.epoch == e0 + 1 and rec.resuming
    assert abs(rec.expires - (c() + 300)) < 1e-6
    assert abs(rec.max_expires - (c() + 7200 - held)) < 1e-6           # 寿命上限只计持有时间
    p = peek_token_payload(r["result"]["token"])
    assert p is not None and p["sid"] == sid and abs(p["exp"] - rec.expires) <= 1
    b.on_spawned(sid, 4243, 1)
    b.on_ready(sid)
    assert rec.state == READY and _ev(fx, "sandbox.state", sid)[-1]["reason"] == "resume"
    b.on_resumed(sid, "checkpoint")
    assert _ev(fx, "sandbox.resumed", sid) == [{"resume_mode": "checkpoint", "epoch": e0 + 1}]
    assert not _ev(fx, "session.switched", sid)
    assert b.resume(sid)["http"] == 200                                 # 已活动


def test_resume_full_queues_behind_new_visitors() -> None:
    b, fx, _c = make_broker(n_max=1)
    s1 = _active(b, 1)
    b.park(s1)
    finish_parks(b, fx)
    s2 = _active(b, 2)
    q3 = create(b, 3)["result"]["queue"]                               # 新访客先排队
    r = b.resume(s1)
    assert r["http"] == 202 and r["result"]["queue"]["kind"] == "resume" and r["result"]["queue"]["position"] == 2
    qr = r["result"]["queue"]
    again = b.resume(s1)                                               # 幂等：同一张恢复票
    assert again["http"] == 202 and again["result"]["queue"]["ticket_id"] == qr["ticket_id"]
    tok = again["result"]["queue"]["ticket_token"]
    assert pool_stats(b)["queue"] == 2
    b.stop(s2)
    finish_stops(b, fx)
    v3 = b.queue_op({"op": "get", "ticket_id": q3["ticket_id"], "ticket_token": q3["ticket_token"]})["result"]
    assert v3["state"] == "slot_ready"                                 # FIFO：新访客先得到空位
    s3 = b.queue_op({"op": "claim", "ticket_id": q3["ticket_id"], "ticket_token": q3["ticket_token"],
                     "principal_id": "p-" + hint(3)})["result"]["session"]["sid"]
    ready(b, s3)
    b.stop(s3)
    finish_stops(b, fx)
    vr = b.queue_op({"op": "get", "ticket_id": qr["ticket_id"], "ticket_token": tok})["result"]
    assert vr["state"] == "slot_ready"
    cl = b.queue_op({"op": "claim", "ticket_id": qr["ticket_id"], "ticket_token": tok})
    assert cl["http"] == 201 and cl["result"]["session"]["sid"] == s1 and b.sessions[s1].state == SPAWNING
    assert fx.resumed == [s1]


def test_server_fallback_idle_park_warning_cancel_and_manual() -> None:
    b, fx, c = make_broker(power={"sandbox_idle_s": 30, "grace_s": 6, "warn_s": 5})
    sid = _active(b)
    t0 = c()
    rec = b.sessions[sid]
    for _ in range(40):                                                # 连接保持（ws 1）但无交互
        c.adv(1)
        b.activity({sid: {"ws_n": 1, "api_calls": 0}})
        b.tick()
        if _ev(fx, "sandbox.expiring", sid):
            break
    warn = _ev(fx, "sandbox.expiring", sid)
    assert warn and warn[0]["action"] == "park" and warn[0]["reason"] == "idle" and 4 <= warn[0]["in_s"] <= 5
    assert 31 <= c() - t0 <= 32 and b.summary(rec)["expiring"]["action"] == "park"
    b.activity({sid: {"ws_n": 1, "api_calls": 0, "last_input_unix_ns": str(int(c() * 1e9))}})   # presence：新的交互
    b.tick()
    assert rec.park_at is None and rec.state == ACTIVE
    t1 = c()
    while rec.state == ACTIVE and c() - t1 < 60:
        c.adv(1)
        b.activity({sid: {"ws_n": 1, "api_calls": 0}})
        b.tick()
    assert rec.state == PARKING and rec.park_reason == "idle" and abs((c() - t1) - 36) <= 2   # 阈值 + 余量 ± 2 s
    finish_parks(b, fx)
    s2 = _active(b, 2)
    b.sessions[s2].manual = True                                       # 手动控制租约：不兜底驻留
    c.adv(200)
    b.activity({s2: {"ws_n": 1, "api_calls": 0}})
    b.tick()
    assert b.sessions[s2].state == ACTIVE and b.sessions[s2].park_at is None


def test_idle_disconnected_park_and_busy_threshold() -> None:
    b, _fx, c = make_broker(n_max=1)
    sid = _active(b)
    b.activity({sid: {"ws_n": 0, "api_calls": 0}})
    c.adv(61)
    b.tick()
    assert b.sessions[sid].state == IDLE
    create(b, 2)                                                        # 队列非空：60 s
    c.adv(59)
    b.tick()
    assert b.sessions[sid].state == PARKING and b.sessions[sid].park_reason == "idle_disconnected"


def test_retention_expiry_keeps_snapshot_and_park_limits() -> None:
    b, fx, c = make_broker(n_max=4, park={"retention_s": 100, "per_prefix": 2, "total_max": 3})
    sids = []
    for i in range(1, 4):                                              # 同一前缀 3 个驻留：最早者被结束
        s = _active(b, i)
        c.adv(1)
        b.park(s)
        finish_parks(b, fx)
        sids.append(s)
    assert fx.discarded == [sids[0]]
    finish_discards(b, fx)
    w = b.wire(b.sessions[sids[0]])
    assert w["state"] == ENDED and w["end_reason"] == "parked_expired" and w["snapshot_available"]
    s4 = _active(b, 4, addr="203.0.113.9")
    b.park(s4)
    finish_parks(b, fx)
    s5 = _active(b, 5, addr="192.0.2.77")
    b.park(s5)
    finish_parks(b, fx)                                                 # 全站上限 3：再结束最早者
    assert fx.discarded == [sids[1]]
    finish_discards(b, fx)
    c.adv(101)
    b.tick()
    assert set(fx.discarded) == {sids[2], s4, s5}                        # 保留到期
    finish_discards(b, fx)
    assert b.resume(s5)["code"] == 506


def test_park_disk_limit() -> None:
    b, fx, _c = make_broker(n_max=3, park={"disk_mb": 0.007})
    a = _active(b, 1)
    b.park(a)
    finish_parks(b, fx)                                                 # 4096 B
    s = _active(b, 2, addr="203.0.113.9")
    b.park(s)
    finish_parks(b, fx)                                                 # 合计 8192 B > 7 KiB：最早者结束
    assert fx.discarded == [a]


def test_resume_failed_retries_snapshot_then_ends() -> None:
    b, fx, _c = make_broker()
    sid = _active(b)
    b.park(sid)
    finish_parks(b, fx)
    b.resume(sid)
    b.on_spawn_failed(sid, "crash")
    rec = b.sessions[sid]
    assert fx.resumed == [sid, sid] and rec.resume_mode == "snapshot" and rec.state == SPAWNING
    assert _ev(fx, "session.switched", sid)[0]["reason"] == "resume_snapshot"
    b.on_spawn_failed(sid, "crash")
    assert rec.state == DRAINING and rec.end_reason == "resume_failed"


def test_stop_parked_and_parking_sessions() -> None:
    b, fx, _c = make_broker(n_max=3)
    s1 = _active(b, 1)
    b.park(s1)
    finish_parks(b, fx)
    assert b.stop(s1)["http"] == 202 and fx.discarded == [s1]
    finish_discards(b, fx)
    assert b.sessions[s1].state == ENDED and b.wire(b.sessions[s1])["end_reason"] == "user"
    s2 = _active(b, 2)
    b.park(s2)
    assert b.stop(s2)["http"] == 202 and b.sessions[s2].end_after_park
    finish_parks(b, fx)
    assert fx.discarded == [s2]


def test_preempt_and_reclaim_park_instead_of_end() -> None:
    b, fx, c = make_broker(n_max=2, queue={"preempt_after_s": 100})
    s1 = _active(b, 1)
    s2 = _active(b, 2)
    q = create(b, 3)["result"]["queue"]
    for _ in range(4):
        c.adv(40)
        b.queue_op({"op": "get", "ticket_id": q["ticket_id"], "ticket_token": q["ticket_token"]})
        b.activity({s1: {"ws_n": 1, "api_calls": 1}, s2: {"ws_n": 1, "api_calls": 1}})
        b.tick()
    pre = _ev(fx, "sandbox.expiring")
    assert pre and pre[0]["reason"] == "preempted" and pre[0]["action"] == "park" and pre[0]["code"] == 510
    b2, fx2, c2 = make_broker(n_max=3)
    a = _active(b2, 1)
    _active(b2, 2, identity=IDENT)
    b2.ladder.tick = lambda inp: 3                                      # type: ignore[method-assign]
    b2.tick(LadderInput(now=c2()))
    rc = _ev(fx2, "sandbox.expiring")
    assert rc and rc[0]["reason"] == "reclaimed" and rc[0]["action"] == "park"
    assert b2.sessions[a].park_pending == "reclaimed_cpu"              # 公开会话先于内部档


def test_internal_tier_reserved_slot_queue_head_and_limits() -> None:
    b, fx, c = make_broker(n_max=1, public=True)
    s1 = create(b, 1, addr="198.51.100.7")["result"]["session"]["sid"]
    ready(b, s1)
    pub_q = create(b, 2, addr="203.0.113.5")
    assert pub_q["http"] == 202 and "kind" not in pub_q["result"]["queue"]      # 公开访客排队
    r = create(b, 3, addr="192.0.2.1", identity=IDENT)
    assert r["http"] == 201                                             # 预留槽位：N_max + 1，队列非空也免工作量证明
    sid = r["result"]["session"]["sid"]
    rec = b.sessions[sid]
    assert rec.tier == "internal" and peek_token_payload(r["result"]["token"])["tier"] == "internal"
    assert abs(rec.max_expires - (c() + 28800)) < 1e-6
    st = pool_stats(b)
    assert st["active"] == 1 and st["internal_active"] == 1 and st["max"] == 1 and b.n_live() == 2
    q2 = create(b, 4, addr="192.0.2.1", identity=IDENT)                 # 预留已用、公开已满：内部票
    assert q2["http"] == 202 and q2["result"]["queue"]["kind"] == "internal"
    third = create(b, 5, addr="192.0.2.2", identity=IDENT)
    assert third["code"] == 504 and third["detail"]["why"] == "per_identity"
    ready(b, sid)
    b.activity({sid: {"ws_n": 1, "api_calls": 1}})
    c.adv(1700)
    assert b.keepalive(sid)["result"]["renewed"] is True               # 队列非空也可续期（不被抢占）
    t = b.queue.get(q2["result"]["queue"]["ticket_id"])
    assert b.queue.waiting()[0] is t                                    # 内部票在公开票之前
    assert b.queue_op({"op": "get", "ticket_id": pub_q["result"]["queue"]["ticket_id"],
                       "ticket_token": pub_q["result"]["queue"]["ticket_token"]})["result"]["position"] == 2
    b.stop(s1)
    finish_stops(b, fx)
    assert t.state == "OFFERED"


def test_internal_memory_admission_queues_without_preempting() -> None:
    b, fx, _c = make_broker(n_max=2, mem_sandbox_mb=300)
    s1 = _active(b, 1)
    r = create(b, 2, identity=IDENT, world="sanfrancisco")              # L 级世界内存不足
    assert r["http"] == 202 and r["result"]["queue"]["kind"] == "internal"
    assert b.sessions[s1].state == ACTIVE and not _ev(fx, "sandbox.expiring")


def test_identity_revoked_demotes_to_public() -> None:
    b, fx, c = make_broker(n_max=1)
    sid = create(b, 1, identity=IDENT)["result"]["session"]["sid"]
    ready(b, sid)
    b.activity({sid: {"ws_n": 1, "api_calls": 1}})
    for _ in range(5):                                                  # 内部档续期到公开寿命上限以外
        c.adv(1500)
        b.activity({sid: {"ws_n": 1, "api_calls": 1}})
        assert b.keepalive(sid)["result"]["renewed"] is True
        b.tick()
    b.is_revoked = lambda jti: jti == IDENT["jti"]
    b.tick()
    rec = b.sessions[sid]
    assert rec.tier == "public" and rec.identity_jti is None
    assert rec.park_pending == "preempted" and _ev(fx, "sandbox.expiring", sid)[-1]["action"] == "park"
    assert any(k == "sandbox.internal_session" and kv.get("action") == "demoted" for k, kv in fx.audits)


def test_dump_load_roundtrip_keeps_parked() -> None:
    b, fx, _c = make_broker(n_max=2)
    sid = _active(b)
    b.park(sid)
    finish_parks(b, fx, t_sim_ns=7)
    q = b.resume(sid)
    assert q["http"] == 201
    b2, _fx2, _c2 = make_broker(n_max=2)
    b2.load(b.dump())
    r2 = b2.sessions[sid]
    assert r2.state == SPAWNING and r2.resuming and r2.resume_mode == "checkpoint" and r2.held_total_s >= 0
    b3, fx3, _c3 = make_broker(n_max=2)
    s3 = _active(b3)
    b3.park(s3)
    finish_parks(b3, fx3)
    b4, _fx4, _c4 = make_broker(n_max=2)
    b4.load(b3.dump())
    assert b4.sessions[s3].state == PARKED and b4.sessions[s3].park_dir.endswith(s3)


def test_pair_code_single_use_ttl_and_rate() -> None:
    b, _fx, c = make_broker()
    sid = _active(b)
    r = b.pair({"op": "code", "sid": sid})
    assert r["http"] == 201 and len(r["result"]["code"]) == 8 and r["result"]["code"].isupper()
    code = r["result"]["code"]
    assert code not in str(b.dump())                                   # 只存散列
    assert b.pair({"op": "redeem", "sid": sid, "code": "AAAAAAAA"})["code"] == 302
    ok_ = b.pair({"op": "redeem", "sid": sid, "code": code.lower()})
    assert ok_["http"] == 201 and peek_token_payload(ok_["result"]["token"])["sub"] == b.sessions[sid].principal_id
    assert b.pair({"op": "redeem", "sid": sid, "code": code})["code"] == 511          # 单次
    code2 = b.pair({"op": "code", "sid": sid})["result"]["code"]
    c.adv(121)
    assert b.pair({"op": "redeem", "sid": sid, "code": code2})["code"] == 511          # 120 s 过期
    for _ in range(3):
        assert b.pair({"op": "code", "sid": sid})["http"] == 201
    rl = b.pair({"op": "code", "sid": sid})
    assert rl["http"] == 429 and rl["detail"]["why"] == "pair_rate"                   # 每会话 10 min 内 5 次
    code3 = None
    c.adv(600)
    code3 = b.pair({"op": "code", "sid": sid})["result"]["code"]
    for _ in range(5):
        b.pair({"op": "redeem", "sid": sid, "code": "BBBBBBBB"})
    assert b.pair({"op": "redeem", "sid": sid, "code": code3})["code"] == 511          # 错 5 次后作废


def test_reserve_memory_quota() -> None:
    b, _fx, c = make_broker(n_max=4, mem_sandbox_mb=700)
    _active(b, 1)
    r = b.reserve(250, 600)
    assert r["result"]["ok"] is True and r["result"]["mem_sb_mb"] == 450
    assert not b.admit("sanfrancisco") and create(b, 2, world="sanfrancisco", queue_ok=True)["http"] == 202
    b2, _fx2, _c2 = make_broker(n_max=4, mem_sandbox_mb=400)
    _active(b2, 1)
    _active(b2, 2, addr="203.0.113.5")
    no = b2.reserve(250, 600)
    assert no["result"]["ok"] is False and no["result"]["why"] == "insufficient"
    c.adv(601)
    assert b.m_sb() == 700                                              # 到期恢复
    b.reserve(100, 60)
    assert b.reserve(0, 0)["result"]["mem_sb_mb"] == 700                # 撤销


def test_load_d2_state_without_d21_fields() -> None:
    b, _fx, _c = make_broker()
    sid = _active(b)
    doc = b.dump()
    for x in doc["sessions"]:                                          # D2 的 state.json 没有这些字段
        for k in ("held_since", "last_input", "tier", "park_at", "park_pending"):
            x.pop(k, None)
    b2, fx2, _c2 = make_broker()
    b2.load(doc)
    r = b2.sessions[sid]
    assert r.tier == "public" and r.held_since > 0 and r.last_input > 0
    b2.tick()
    assert r.park_at is None and not [e for e in fx2.events if e[0] == "sandbox.expiring"]
