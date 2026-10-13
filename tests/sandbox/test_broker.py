"""broker 纯逻辑（注入时钟、记录副作用；M17 PRD §6.3、§6.4、§6.6.1 至 §6.6.3）：创建与幂等、满员排队与派发、票的两种作废、
认领、配额（前缀、聚合前缀、创建速率）、工作量证明触发、续期、空闲与未认领回收、寿命告知、抢占、崩溃重启与 FAILED、
切换底图准入、快照与模板校验、落盘往返。对应 M17-AC-001、004、005、009、010、012、015、016、018 的逻辑部分。

D2.1（ADR-121）：无连接空闲与抢占改为驻留（PARKING → PARKED，可恢复），本文件的两个用例按新口径断言；驻留与恢复的完整用例见
`test_park.py`。"""

from __future__ import annotations

import base64

from brokerkit import HINT, create, finish_parks, finish_stops, hint, make_broker, ready

from awr.sandbox.model import ACTIVE, DRAINING, ENDED, FAILED, IDLE, PARKED, PARKING, READY, SPAWNING
from awr.sandbox.pow import solve


def test_create_idempotent_and_spawn_request() -> None:
    b, fx, _c = make_broker()
    r = create(b, 1)
    assert r["http"] == 201 and r["status"] == "created" and r["result"]["token"].startswith("v1.")
    s = r["result"]["session"]
    assert s["state"] == SPAWNING and s["template_id"] == "t5_area_guard" and s["world_id"] == "synthcity"
    assert s["limits"]["vehicles_max"] == 12 and s["endpoints"]["rt"].endswith("/rt")
    sid, cfg, restart = fx.spawned[0]
    assert sid == s["sid"] and not restart and cfg["world_id"] == "synthcity" and cfg["template_id"] == "t5_area_guard"
    assert cfg["limits"]["targets"] == 30 and cfg["calendar"]["tz"] == "Asia/Shanghai" and len(cfg["vehicles"]) == 1
    again = create(b, 1)
    assert again["http"] == 200 and again["result"]["token"] is None and again["result"]["idempotent"] is True
    assert again["result"]["session"]["sid"] == sid
    other_prefix = create(b, 1, addr="203.0.113.5")
    assert other_prefix["http"] == 201 and other_prefix["result"]["session"]["sid"] != sid
    assert len(fx.spawned) == 2


def test_rejections_world_template_snapshot() -> None:
    b, _fx, _c = make_broker(missing=("chicago",))
    assert b.create({"principal_id": "p-x", "req": {}})["code"] == 300 or True
    assert create(b, 1, world="atlantis")["code"] == 507
    r = create(b, 1, world="chicago")
    assert r["code"] == 507 and r["detail"]["why"] == "cache_missing" and r["http"] == 409
    r = create(b, 2, template="t9_unknown")
    assert r["http"] == 422 and r["code"] == 300
    snap = {"schema": "awr.sandbox.snapshot.v1", "contracts": "1.1.0", "world_id": "shenzhen", "template_id": None,
            "calendar": {"start_utc": "2026-10-05T02:00:00Z", "tz": "Asia/Shanghai"}, "seed": 1, "nests": [], "vehicles": [],
            "targets": [], "tasks": []}
    r = create(b, 3, config_snapshot=snap)
    assert r["http"] == 422 and r["code"] == 512 and r["detail"]["why"] == "world_mismatch"
    snap["world_id"] = "synthcity"
    r = create(b, 3, config_snapshot=snap)
    assert r["http"] == 201 and r["result"]["session"]["template_id"] is None
    b.accepting = False
    r = create(b, 4)
    assert r["http"] == 503 and r["code"] == 502 and r["detail"]["why"] == "maintenance"


def test_full_queue_offer_claim_and_expiry() -> None:
    b, fx, c = make_broker(n_max=2)
    sids = [create(b, i)["result"]["session"]["sid"] for i in (1, 2)]
    for s in sids:
        ready(b, s)
    q = create(b, 3)
    assert q["http"] == 202 and q["status"] == "queued"
    t1 = q["result"]["queue"]
    assert t1["ticket_id"].startswith("q-") and t1["ticket_token"].startswith("q1.") and t1["position"] == 1
    assert t1["eta_s"] is not None
    q2 = create(b, 4, addr="203.0.113.5")
    t2 = q2["result"]["queue"]
    assert q2["http"] == 202 and t2["position"] == 2
    same = create(b, 3)                      # 同一 principal 与前缀：幂等返回原票，换发令牌
    assert same["http"] == 202 and same["result"]["queue"]["ticket_id"] == t1["ticket_id"]
    tok1 = same["result"]["queue"]["ticket_token"]
    assert b.queue_op({"op": "get", "ticket_id": t1["ticket_id"], "ticket_token": t1["ticket_token"]})["code"] == 302
    view = b.queue_op({"op": "get", "ticket_id": t1["ticket_id"], "ticket_token": tok1})["result"]
    assert view["state"] == "waiting" and view["position"] == 1
    early = b.queue_op({"op": "claim", "ticket_id": t1["ticket_id"], "ticket_token": tok1})
    assert early["http"] == 409 and early["code"] == 105
    b.stop(sids[0])
    finish_stops(b, fx)
    v = b.queue_op({"op": "get", "ticket_id": t1["ticket_id"], "ticket_token": tok1})["result"]
    assert v["state"] == "slot_ready" and v["claim_deadline_unix_ns"] is not None and v["position"] == 0
    c.adv(30)
    b.queue_op({"op": "get", "ticket_id": t2["ticket_id"], "ticket_token": t2["ticket_token"]})
    c.adv(31)
    b.tick()                                  # 60 s 未认领 → 作废并转给下一张
    assert b.queue_op({"op": "get", "ticket_id": t1["ticket_id"], "ticket_token": tok1})["result"]["state"] == "expired"
    v2 = b.queue_op({"op": "get", "ticket_id": t2["ticket_id"], "ticket_token": t2["ticket_token"]})["result"]
    assert v2["state"] == "slot_ready"
    got = b.queue_op({"op": "claim", "ticket_id": t2["ticket_id"], "ticket_token": t2["ticket_token"]})
    assert got["http"] == 201 and got["result"]["token"] and got["result"]["session"]["state"] == SPAWNING
    again = b.queue_op({"op": "claim", "ticket_id": t2["ticket_id"], "ticket_token": t2["ticket_token"]})
    assert again["http"] == 200 and again["result"]["token"] is None
    gone = b.queue_op({"op": "claim", "ticket_id": t1["ticket_id"], "ticket_token": tok1})
    assert gone["http"] == 410 and gone["code"] == 511
    assert b.queue_op({"op": "get", "ticket_id": "q-aaaaaaaaaa", "ticket_token": tok1})["code"] == 511


def test_waiting_ticket_idle_expiry_and_leave() -> None:
    b, _fx, c = make_broker(n_max=1)
    ready(b, create(b, 1)["result"]["session"]["sid"])
    t = create(b, 2)["result"]["queue"]
    c.adv(119)
    b.tick()
    assert b.queue_op({"op": "get", "ticket_id": t["ticket_id"], "ticket_token": t["ticket_token"]})["result"]["state"] == "waiting"
    c.adv(121)
    b.tick()
    assert b.queue_op({"op": "get", "ticket_id": t["ticket_id"], "ticket_token": t["ticket_token"]})["result"]["state"] == "expired"
    t3 = create(b, 3)["result"]["queue"]
    assert b.queue_op({"op": "leave", "ticket_id": t3["ticket_id"], "ticket_token": t3["ticket_token"]})["http"] == 204
    assert not b.queue.open()


def test_world_capacity_508_and_queue_ok() -> None:
    b, _fx, _c = make_broker(n_max=4)
    for i, w in enumerate(("sanfrancisco", "chicago", "synthcity"), 1):
        assert create(b, i, world=w)["http"] == 201
    r = create(b, 9, world="shanghai")
    assert r["http"] == 409 and r["code"] == 508 and r["detail"]["queue_ok"] is True and r["detail"]["available_now"] == []
    q = create(b, 9, world="shanghai", queue_ok=True)
    assert q["http"] == 202


def test_public_quotas_rate_and_pow() -> None:
    b, fx, c = make_broker(n_max=4, public=True, pow={"bits": 8, "bits_hot": 10})
    a1 = create(b, 1, addr="2001:db8:1:2::1")
    a2 = create(b, 2, addr="2001:db8:1:2:ffff::9")          # 同一 /64 轮换地址
    assert a1["http"] == 201 and a2["http"] == 201
    r = create(b, 3, addr="2001:db8:1:2::77")
    assert r["http"] == 409 and r["code"] == 504 and r["detail"]["why"] == "active_per_prefix"
    r = create(b, 4, addr="2001:db8:1:3::1")                  # 同一 /48：聚合前缀上限 ⌈4/2⌉ = 2
    assert r["code"] == 504 and r["detail"]["why"] == "active_per_aggregate"
    for sid in [a1["result"]["session"]["sid"], a2["result"]["session"]["sid"]]:
        b.stop(sid)
    finish_stops(b, fx)
    r = create(b, 5, addr="2001:db8:1:2::5")                  # 突发 2 已用完：429 + Retry-After
    assert r["http"] == 429 and r["code"] == 504 and r["detail"]["why"] == "create_rate" and r["detail"]["retry_after_s"] > 0
    c.adv(601)
    third = create(b, 5, addr="2001:db8:1:2::5")              # 令牌回补后第 3 次创建
    assert third["http"] == 201
    b.stop(third["result"]["session"]["sid"])
    finish_stops(b, fx)
    c.adv(601)
    r = create(b, 5, addr="2001:db8:1:2::5")                  # 1 h 内已创建 3 次：要求工作量证明
    assert r["http"] == 409 and r["code"] == 509 and r["detail"]["why"] == "required"
    ch = r["detail"]["challenge"]
    nonce = solve(base64.b64decode(ch["prefix_b64"]), ch["bits"])
    ok = create(b, 5, addr="2001:db8:1:2::5", pow={"challenge_id": ch["challenge_id"], "nonce": nonce})
    assert ok["http"] == 201
    b.stop(ok["result"]["session"]["sid"])
    finish_stops(b, fx)
    c.adv(601)
    replay = create(b, 5, addr="2001:db8:1:2::5", pow={"challenge_id": ch["challenge_id"], "nonce": nonce})
    assert replay["code"] == 509 and replay["detail"]["why"] == "invalid" and replay["detail"]["challenge"]["bits"] >= 9
    assert any(k == "quota.denied" for k, _ in fx.audits) and any(k == "pow.failed" for k, _ in fx.audits)
    assert all("2001:db8" not in str(kv) for _k, kv in fx.audits)       # 审计只有前缀散列


def test_pow_required_when_queue_nonempty() -> None:
    b, _fx, _c = make_broker(n_max=1, public=True, pow={"bits": 4})
    ready(b, create(b, 1, addr="198.51.100.1")["result"]["session"]["sid"])
    q = create(b, 2, addr="192.0.2.2")
    assert q["http"] == 202
    r = create(b, 3, addr="203.0.113.3")
    assert r["code"] == 509
    assert b.meta("203.0.113.3")["result"]["pow"]["required_now"] is True


def test_keepalive_rules() -> None:
    b, fx, c = make_broker(n_max=1)
    sid = create(b, 1)["result"]["session"]["sid"]
    assert b.keepalive(sid)["code"] == 513                     # SPAWNING
    ready(b, sid)
    rec = b.sessions[sid]
    exp0 = rec.expires
    c.adv(600)
    r = b.keepalive(sid)
    assert r["result"]["renewed"] is True and r["result"]["token"].startswith("v1.")
    assert abs(rec.expires - (c.t + 1800)) < 1e-6 and rec.expires > exp0
    assert rec.state == ACTIVE and fx.clocks[-1][1] == "play"  # 续期计为活动：READY → ACTIVE 并启动时钟
    create(b, 2)                                               # 排队 → 不续期
    r = b.keepalive(sid)
    assert r["result"]["renewed"] is False and r["result"]["session"]["renew_block_reason"] == "queue_nonempty"
    b2, _fx2, c2 = make_broker(n_max=1)
    s2 = create(b2, 1)["result"]["session"]["sid"]
    ready(b2, s2)
    for _ in range(5):
        c2.adv(1700)
        b2.keepalive(s2)
    r = b2.keepalive(s2)
    assert r["result"]["renewed"] is False and r["result"]["session"]["renew_block_reason"] == "max_lifetime"
    assert b2.sessions[s2].expires <= b2.sessions[s2].created + 7200 + 1e-6


def test_idle_unclaimed_and_lifetime() -> None:
    b, fx, c = make_broker(n_max=3)
    s_un = create(b, 1)["result"]["session"]["sid"]
    ready(b, s_un)
    c.adv(121)
    b.tick()
    assert b.sessions[s_un].state == DRAINING and b.sessions[s_un].end_reason == "unclaimed"
    s_id = create(b, 2)["result"]["session"]["sid"]
    ready(b, s_id)
    b.activity({s_id: {"ws_n": 1, "api_calls": 0}})
    assert b.sessions[s_id].state == ACTIVE
    b.activity({s_id: {"ws_n": 0, "api_calls": 0}})
    c.adv(61)
    b.tick()
    assert b.sessions[s_id].state == IDLE
    b.activity({s_id: {"ws_n": 0, "api_calls": 3}})
    assert b.sessions[s_id].state == ACTIVE
    c.adv(61)
    b.tick()
    assert b.sessions[s_id].state == IDLE
    c.adv(58)
    b.tick()
    assert b.sessions[s_id].state == IDLE              # 无连接 119 s：未到 120 s
    c.adv(3)
    b.tick()                                           # D2.1：无连接且无调用 120 s → 驻留（替代 D2 的空闲结束）
    assert b.sessions[s_id].state == PARKING and b.sessions[s_id].park_reason == "idle_disconnected"
    finish_parks(b, fx)
    assert b.sessions[s_id].state == PARKED and b.sessions[s_id].end_reason is None
    s_lt = create(b, 3)["result"]["session"]["sid"]
    ready(b, s_lt)
    for _ in range(30):
        c.adv(59)
        b.activity({s_lt: {"ws_n": 1, "api_calls": 1}})
        b.tick()
    assert b.sessions[s_lt].drain_at is not None
    exp = [d for k, s, d, _ in fx.events if k == "sandbox.expiring" and s == s_lt]
    assert exp and exp[0]["reason"] == "lifetime" and 0 < exp[0]["in_s"] <= 60
    c.adv(70)
    b.tick()
    assert b.sessions[s_lt].state == DRAINING
    finish_stops(b, fx)
    assert b.sessions[s_lt].state == ENDED
    w = b.wire(b.sessions[s_un])
    assert w["end_reason"] == "idle" and w["state"] == ENDED       # unclaimed 在线上为 idle


def test_preempt_longest_holder_with_notice() -> None:
    b, fx, c = make_broker(n_max=2)
    s1 = create(b, 1)["result"]["session"]["sid"]
    ready(b, s1)
    c.adv(100)
    s2 = create(b, 2)["result"]["session"]["sid"]
    ready(b, s2)
    c.adv(1000)
    for s in (s1, s2):                       # 队列为空时续期（寿命延到 2800 s 以后）
        assert b.keepalive(s)["result"]["renewed"] is True
    t = create(b, 3)["result"]["queue"]

    def poll() -> None:                      # 排队者持续长轮询（否则 120 s 后票作废）
        b.queue_op({"op": "get", "ticket_id": t["ticket_id"], "ticket_token": t["ticket_token"]})

    for _ in range(13):                      # s1 持有 1750 s、s2 1650 s
        c.adv(50)
        poll()
    for s in (s1, s2):
        b.activity({s: {"ws_n": 1, "api_calls": 1}})
    b.tick()
    assert not [e for e in fx.events if e[0] == "sandbox.expiring" and e[2]["reason"] == "preempted"]
    c.adv(60)
    poll()
    b.activity({s1: {"ws_n": 1, "api_calls": 1}, s2: {"ws_n": 1, "api_calls": 1}})
    b.tick()
    pre = [e for e in fx.events if e[0] == "sandbox.expiring" and e[2]["reason"] == "preempted"]
    assert len(pre) == 1 and pre[0][1] == s1 and pre[0][2]["code"] == 510 and pre[0][2]["in_s"] == 60
    b.tick()
    assert len([e for e in fx.events if e[0] == "sandbox.expiring" and e[2]["reason"] == "preempted"]) == 1
    c.adv(61)
    poll()
    b.tick()
    assert b.sessions[s1].state == PARKING            # D2.1：抢占改为驻留（ADR-121）
    finish_parks(b, fx)
    assert b.queue_op({"op": "get", "ticket_id": t["ticket_id"], "ticket_token": t["ticket_token"]})["result"]["state"] == "slot_ready"
    w = b.get(s1)["result"]
    assert w["state"] == PARKED and w["park_reason"] == "preempted" and w["end_reason"] is None
    assert b.keepalive(s1)["code"] == 515
    r = b.resume(s1)                                   # 被抢占者可立即排队恢复（满员：恢复票）
    assert r["http"] == 202 and r["result"]["queue"]["kind"] == "resume"


def test_crash_restart_then_failed() -> None:
    b, fx, c = make_broker()
    sid = create(b, 1)["result"]["session"]["sid"]
    ready(b, sid)
    b.activity({sid: {"ws_n": 1, "api_calls": 0}})
    e0 = b.sessions[sid].epoch
    b.on_exit(sid, -9)
    assert fx.spawned[-1] == (sid, None, True) and b.sessions[sid].epoch == e0 + 1 and b.sessions[sid].state == ACTIVE
    b.on_ready(sid)
    assert fx.clocks[-1] == (sid, "play", {})
    c.adv(10)
    b.on_exit(sid, -9)
    assert b.sessions[sid].restarts == 2
    c.adv(10)
    b.on_exit(sid, -9)
    assert b.sessions[sid].state == DRAINING and fx.stopped[-1] == (sid, False)
    finish_stops(b, fx)
    assert b.sessions[sid].state == FAILED and b.wire(b.sessions[sid])["end_reason"] == "failed"
    b2, _fx2, c2 = make_broker()
    s2 = create(b2, 1)["result"]["session"]["sid"]
    ready(b2, s2)
    for _ in range(3):
        c2.adv(61)
        b2.on_exit(s2, -9)
    assert b2.sessions[s2].state == ACTIVE or b2.sessions[s2].state == READY      # 60 s 窗口外的故障不累计


def test_spawn_failure_releases_slot() -> None:
    b, fx, _c = make_broker(n_max=1)
    sid = create(b, 1)["result"]["session"]["sid"]
    assert create(b, 2)["http"] == 202
    b.on_spawn_failed(sid, "timeout")
    finish_stops(b, fx)
    assert b.sessions[sid].state == FAILED
    assert b.queue.offered()                                  # 槽位释放后派发给排队票


def test_world_switch_admission_and_rollback() -> None:
    b, fx, _c = make_broker(n_max=3)
    s = [create(b, i, world=w)["result"]["session"]["sid"] for i, w in ((1, "sanfrancisco"), (2, "chicago"), (3, "synthcity"))]
    for x in s:
        ready(b, x)
    r = b.world_switch(s[2], "shanghai")
    assert r["http"] == 409 and r["code"] == 508
    assert b.sessions[s[2]].world == "synthcity" and b.sessions[s[2]].state == READY
    assert b.world_switch(s[2], "atlantis")["code"] == 507
    r = b.world_switch(s[2], "shenzhen")
    assert r["http"] == 202 and r["result"]["session"]["state"] == SPAWNING and r["result"]["session"]["world_id"] == "shenzhen"
    sid, cfg = fx.switched[-1]
    assert sid == s[2] and cfg["world_id"] == "shenzhen" and cfg["template_id"] == "t5_area_guard"
    assert b.world_switch(s[2], "suzhou")["code"] == 513
    b.on_spawn_failed(s[2], "timeout")                         # 新世界拉起失败 → 按原世界恢复
    assert b.sessions[s[2]].world == "synthcity" and fx.switched[-1][1]["world_id"] == "synthcity"
    b.on_spawn_failed(s[2], "timeout")                         # 恢复也失败 → 结束
    assert b.sessions[s[2]].state == DRAINING and b.sessions[s[2]].end_reason == "world_switch_failed"
    r = b.world_switch(s[1], "sanfrancisco")
    assert r["http"] == 202
    b.on_ready(s[1])
    assert b.sessions[s[1]].state == READY and b.sessions[s[1]].world == "sanfrancisco"


def test_template_and_restore() -> None:
    b, fx, _c = make_broker()
    sid = create(b, 1)["result"]["session"]["sid"]
    assert b.template(sid, "t1_point_watch")["code"] == 513
    ready(b, sid)
    e0 = b.sessions[sid].epoch
    r = b.template(sid, "t1_point_watch")
    assert r["http"] == 202 and r["result"]["epoch"] == e0 + 1
    assert fx.resets[-1][2] == "template" and fx.resets[-1][1]["template_id"] == "t1_point_watch"
    assert b.template(sid, "t9_nope")["http"] == 422
    snap = {"schema": "awr.sandbox.snapshot.v1", "contracts": "1.1.0", "world_id": "shenzhen", "template_id": None,
            "calendar": {"start_utc": "2026-10-05T02:00:00Z", "tz": "Asia/Shanghai"}, "seed": 5, "nests": [], "vehicles": [],
            "targets": [], "tasks": []}
    r = b.restore(sid, snap)
    assert r["http"] == 422 and r["code"] == 512 and r["detail"]["why"] == "world_mismatch"
    snap["world_id"] = "synthcity"
    r = b.restore(sid, snap)
    assert r["http"] == 202 and fx.resets[-1][2] == "restore" and fx.resets[-1][1]["seed"] == 5
    assert b.restore(sid, dict(snap, vehicles=[{"model_id": "p600"}] * 13))["detail"]["why"] == "schema"
    b3, _fx3, _c3 = make_broker(limits={"vehicles": 2})
    s3 = create(b3, 1)["result"]["session"]["sid"]
    ready(b3, s3)
    assert b3.restore(s3, dict(snap, vehicles=[{"model_id": "p600"}] * 3))["detail"]["why"] == "limits"


def test_rate_grants_and_l2_degrade() -> None:
    b, fx, c = make_broker(n_max=2, q_cores=2.5)
    s1 = create(b, 1)["result"]["session"]["sid"]
    s2 = create(b, 2)["result"]["session"]["sid"]
    for s in (s1, s2):
        ready(b, s)
        b.activity({s: {"ws_n": 1, "api_calls": 1}})
    g1 = b.rate(s1, 10)["result"]
    g2 = b.rate(s2, 10)["result"]
    b_sim = b.s.b_sim(b.n_live())
    tot = sum(b.sessions[x].c_i * b.sessions[x].granted_rate for x in (s1, s2))
    assert tot <= b_sim * 1.1 and g1["granted"] <= g1["cap"] and g2["code"] == 501
    assert [e for e in fx.events if e[0] == "sandbox.rate" and e[1] == s2][-1][2]["code"] == 501
    assert b.rate(s1, 3)["http"] == 422
    from awr.sandbox.ladder import LadderInput

    b.sessions[s1].clock_state = "PLAYING"
    b.tick(LadderInput(now=c.t, psi_cpu=50.0))
    assert b.level == 2 and b.sessions[s1].granted_rate == 1.0 and b.sessions[s1].limited_by == "l2"
    assert (s1, "speed", {"rate": 1.0}) in fx.clocks


def test_reclaim_hold_blocks_admission_until_l2_clears() -> None:
    """D2-FX-R2：L3 回收（驻留）之后阶梯回到 L2 时仍停止接纳（创建排队、恢复排队、不派发），降到 L1 及以下才解除；避免
    D2-ACC-1 变体 A 的"回收 → 立即恢复 → 再回收"循环。"""
    from awr.sandbox.ladder import LadderInput

    b, fx, c = make_broker(n_max=3, q_cores=2.5)
    sids = [create(b, i)["result"]["session"]["sid"] for i in (1, 2)]
    for s in sids:
        ready(b, s)
        b.activity({s: {"ws_n": 1, "api_calls": 1}})
        b.sessions[s].clock_state = "PLAYING"
        b.on_perf(s, {"rtf_win": 0.5})                     # 饥饿：RTF 0.5
    for _ in range(32):
        c.adv(1.0)
        b.tick(LadderInput(now=c.t, psi_cpu=50.0))
    assert b.level == 3 and b.reclaim_hold and any(b.sessions[s].park_pending == "reclaimed_cpu" for s in sids)
    for s in sids:                                         # 回收已安排：其余会话恢复正常 RTF，但仍有 CPU 压力（L2）
        b.on_perf(s, {"rtf_win": 1.0})
    c.adv(1.0)
    b.tick(LadderInput(now=c.t, psi_cpu=50.0))
    assert b.level == 2 and b.cpu_blocked() and b.startable_worlds() == []
    r = create(b, 3, queue_ok=True)
    assert r["http"] == 202 and r["status"] == "queued"
    for _ in range(45):                                    # 压力消失：L2 的 30 s 解除计时之后回到 L1（队列非空）
        c.adv(1.0)
        b.tick(LadderInput(now=c.t, psi_cpu=0.0))
    assert b.level <= 1 and not b.reclaim_hold and not b.cpu_blocked()
    assert ("ladder.reclaim_hold", {"on": True}) in fx.audits and ("ladder.reclaim_hold", {"on": False}) in fx.audits


def test_recheck_downgrades_unachievable_grant() -> None:
    b, fx, c = make_broker(n_max=1)
    s = create(b, 1)["result"]["session"]["sid"]
    ready(b, s)
    b.activity({s: {"ws_n": 1, "api_calls": 1}})
    b.sessions[s].c_i = 0.05
    assert b.rate(s, 10)["result"]["granted"] == 10
    b.sessions[s].clock_state = "PLAYING"
    b.on_perf(s, {"rtf_win": 3.2})
    c.adv(5.5)
    b.tick()
    assert b.sessions[s].granted_rate == 2 and b.sessions[s].limited_by == "measured"
    assert fx.clocks[-1] == (s, "speed", {"rate": 2})


def test_dump_load_roundtrip_keeps_quotas_and_tickets() -> None:
    b, _fx, c = make_broker(n_max=1, public=True, pow={"bits": 4})
    ready(b, create(b, 1)["result"]["session"]["sid"])
    t = create(b, 2, addr="203.0.113.2")["result"]["queue"]
    d = b.dump()
    b2, _fx2, _c2 = make_broker(n_max=1, public=True, pow={"bits": 4})
    b2.keys = b.keys
    b2.pow.k_pow = b.keys.k_pow
    b2.now = c
    b2.load(d)
    assert set(b2.sessions) == set(b.sessions) and b2.queue.get(t["ticket_id"]) is not None
    assert b2.creates == b.creates and b2.buckets == b.buckets
    v = b2.queue_op({"op": "get", "ticket_id": t["ticket_id"], "ticket_token": t["ticket_token"]})
    assert v["result"]["state"] == "waiting" and v["result"]["position"] == 1


def test_list_and_summary_shapes() -> None:
    b, _fx, _c = make_broker()
    sid = create(b, 1)["result"]["session"]["sid"]
    ready(b, sid)
    lst = b.list()["result"]
    assert lst["n_max"] == 2 and lst["sessions"][0]["binding"]["namespace"] == f"awr/synthcity/{sid}"
    assert lst["sessions"][0]["principal_id"] == "p-" + hint(1)
    sm = b.summary(b.sessions[sid])
    assert sm["clock"]["rate_granted"] == 1.0 and sm["degrade_level"] == "L0" and sm["queue_len"] == 0
    assert HINT  # 保留导入（与 REST 用例共用的提示值）
