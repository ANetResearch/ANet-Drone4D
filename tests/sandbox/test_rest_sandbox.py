"""沙盒 REST v1 的 M17 端点（SB-00 至 SB-19）经真实 sandbox-api 与真实池（M17-AC-004、005、007 的端点部分、009、010、011、012、
016、018 的 REST 部分、024 的公开站 404；17 §17.3 至 §17.5）。httpx 客户端 + uvicorn 线程 + PoolThread（FakeLauncher）。"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx
import pytest
import rtc
from sbharness import SbxStack, make_sb

from awr.sandbox.pow import solve

P = "/api/sandbox/v1"


def H(i: int) -> str:
    return f"{i:032x}"


def _client(st: SbxStack, **headers: str) -> httpx.Client:
    return httpx.Client(base_url=st.base, headers={"origin": st.origin, **headers}, timeout=15)


def _create(c: httpx.Client, i: int, **body: Any) -> httpx.Response:
    return c.post(f"{P}/sessions", json={"world": "synthcity", "principal_hint": H(i), **body})


def _ready(c: httpx.Client, sid: str, tok: str) -> dict:
    r = c.get(f"{P}/sessions/{sid}", params={"wait_state": "READY", "wait_s": 10}, headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] in ("READY", "ACTIVE"), r.json()
    return r.json()


@pytest.fixture(scope="module")
def st():
    s = SbxStack(sb=make_sb(max_sessions=2, spawn={"timeout_s": 4}),
                 sb_api={"rest": {"rps": 200, "burst": 400, "clock_rps": 100, "entity_rps": 100}})
    yield s
    s.close()


def _end_all(st: SbxStack) -> None:
    def go() -> None:
        for r in st.broker.live():
            st.broker.stop(r.sid)
        for t in st.broker.queue.open():
            st.broker.queue.close(t, "LEFT", st.broker.now())

    st.pt.call(go)
    st.pt.wait(lambda: not st.broker.live(), 15, "all ended")


def test_public_catalog_and_cache(st: SbxStack) -> None:
    with _client(st) as c:
        m = c.get(f"{P}/meta")
        assert m.status_code == 200 and m.headers["cache-control"].startswith("public") and m.headers.get("etag")
        assert rtc.schema_errors("sandbox/session.schema.json", m.json(), "#/$defs/meta") == []
        assert m.json()["max_sessions"] == 2 and "batch" in m.json()["features"]
        assert c.get(f"{P}/meta", headers={"if-none-match": m.headers["etag"]}).status_code == 304
        w = c.get(f"{P}/worlds").json()
        assert rtc.schema_errors("sandbox/session.schema.json", w, "#/$defs/worlds") == []
        assert [x["world_id"] for x in w["items"]] == ["synthcity", "shenzhen", "suzhou", "newyork", "chicago", "shanghai",
                                                       "sanfrancisco"]
        assert all(x["available"] for x in w["items"]) and w["items"][1]["dataset"]["citation"].endswith("ECCV 2022")
        t = c.get(f"{P}/templates", params={"world": "synthcity"})
        assert t.status_code == 200 and t.headers["cache-control"] == "public, max-age=300"
        assert rtc.schema_errors("sandbox/session.schema.json", t.json(), "#/$defs/templates") == []
        assert {x["template_id"] for x in t.json()["items"]} == {"t1_point_watch", "t5_area_guard"}
        ch = c.get(f"{P}/challenge").json()
        assert rtc.schema_errors("sandbox/session.schema.json", ch, "#/$defs/challenge") == []


def test_lifecycle_endpoints(st: SbxStack) -> None:
    _end_all(st)
    with _client(st) as c:
        r = _create(c, 1, template="t5_area_guard", calendar={"start_utc": "2026-10-05T02:00:00Z", "tz": "Asia/Shanghai"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert rtc.schema_errors("sandbox/session.schema.json", body, "#/$defs/created") == []
        sid, tok = body["session"]["sid"], body["token"]
        assert r.headers["location"] == f"{P}/sessions/{sid}"
        auth = {"authorization": f"Bearer {tok}"}
        s = _ready(c, sid, tok)
        assert rtc.schema_errors("sandbox/session.schema.json", s) == []
        assert s["calendar"] == {"start_utc": "2026-10-05T02:00:00Z", "tz": "Asia/Shanghai"} and s["clock"]["tick_hz"] == 100
        again = _create(c, 1)
        assert again.status_code == 200 and again.json()["token"] is None and again.json()["session"]["sid"] == sid
        k = c.post(f"{P}/sessions/{sid}/keepalive", headers=auth)
        assert k.status_code == 200 and rtc.schema_errors("sandbox/session.schema.json", k.json(), "#/$defs/keepalive_reply") == []
        assert k.json()["renewed"] is True and k.json()["token"] != tok
        st.pt.wait(lambda: st.broker.sessions[sid].state == "ACTIVE", 5, "ACTIVE")
        clk = c.post(f"{P}/sessions/{sid}/clock", headers=auth, json={"op": "speed", "rate": 10})
        assert clk.status_code == 200, clk.text
        d = clk.json()["data"]
        assert rtc.schema_errors("sandbox/session.schema.json", d, "#/$defs/clock_data") == []
        assert d["requested_rate"] == 10 and d["granted_rate"] <= d["rate_cap"]
        if d["granted_rate"] < 10:
            assert d["limit_code"] == 501 and "SANDBOX_RATE_LIMITED" in clk.json()["warnings"]
        assert c.post(f"{P}/sessions/{sid}/clock", headers=auth, json={"op": "speed", "rate": 3}).status_code == 400
        assert c.post(f"{P}/sessions/{sid}/clock", headers=auth, json={"op": "pause"}).json()["status"] == "succeeded"
        snap = c.get(f"{P}/sessions/{sid}/snapshot", headers=auth)
        assert snap.status_code == 200 and snap.json()["world_id"] == "synthcity"
        rs = c.post(f"{P}/sessions/{sid}/restore", headers=auth, json={"snapshot": dict(snap.json(), world_id="shenzhen")})
        assert rs.status_code == 422 and rs.json()["code"] == 512 and rs.json()["detail"]["why"] == "world_mismatch"
        rs = c.post(f"{P}/sessions/{sid}/restore", headers=auth, json={"snapshot": snap.json()})
        assert rs.status_code == 202 and rtc.schema_errors("sandbox/session.schema.json", rs.json()) == []
        tp = c.post(f"{P}/sessions/{sid}/template", headers=auth, json={"template": "t1_point_watch"})
        assert tp.status_code == 202 and tp.json()["template_id"] == "t1_point_watch"
        assert c.post(f"{P}/sessions/{sid}/template", headers=auth, json={"template": "t7_nope"}).status_code == 422
        ev = c.get(f"{P}/sessions/{sid}/events", headers=auth, params={"since_seq": 0}).json()
        kinds = [i.get("type") or i.get("kind") for i in ev["items"]]
        assert "sandbox.rate" in kinds and ev["next_since_seq"] >= 1
        bt = c.post(f"{P}/sessions/{sid}/batch", headers=auth, json={"until_s": 0.5})
        assert bt.status_code == 202, bt.text
        assert rtc.schema_errors("sandbox/session.schema.json", bt.json(), "#/$defs/batch_status") == []
        busy = c.post(f"{P}/sessions/{sid}/batch", headers=auth, json={"until_s": 0.5})
        assert busy.status_code == 409 and busy.json()["code"] == 514
        bg = c.get(f"{P}/sessions/{sid}/batch/{bt.json()['batch_id']}", headers=auth)
        assert bg.status_code == 200 and bg.json()["state"] in ("running", "done", "failed")
        sw = c.post(f"{P}/sessions/{sid}/world", headers=auth, json={"world": "shenzhen"})
        assert sw.status_code == 202 and sw.json()["state"] == "SPAWNING" and sw.json()["world_id"] == "shenzhen"
        s2 = _ready(c, sid, k.json()["token"])
        assert s2["world_id"] == "shenzhen" and s2["template_id"] == "t1_point_watch"
        tok2 = k.json()["token"]
        auth2 = {"authorization": f"Bearer {tok2}"}
        assert c.delete(f"{P}/sessions/{sid}", headers=auth2).json() == {"state": "DRAINING"}
        st.pt.wait(lambda: st.broker.sessions[sid].state == "ENDED", 10, "ENDED")
        st.api.wait(lambda: st.api.hub.ended_reason(sid) is not None, 6, "hub saw end")
        g = c.get(f"{P}/sessions/{sid}", headers=auth2)
        assert g.status_code == 410 and g.json()["code"] == 506 and g.json()["detail"]["snapshot_available"] is True
        sn = c.get(f"{P}/sessions/{sid}/snapshot", headers=auth2)
        assert sn.status_code == 200 and sn.json()["world_id"] == "shenzhen"


def test_token_scope_and_auth(st: SbxStack) -> None:
    _end_all(st)
    with _client(st) as c:
        a = _create(c, 11).json()
        b = _create(c, 12).json()
        sa, ta, sb_ = a["session"]["sid"], a["token"], b["session"]["sid"]
        _ready(c, sa, ta)
        r = c.get(f"{P}/sessions/{sb_}", headers={"authorization": f"Bearer {ta}"})
        assert r.status_code == 403 and r.json()["code"] == 503
        assert c.get(f"{P}/sessions/{sa}").status_code == 401
        assert c.get(f"{P}/sessions/{sa}", headers={"authorization": "Bearer v1.x.y"}).status_code == 401
        bt = c.post(f"{P}/sessions/{sa}/end-beacon", json={"token": b["token"]})
        assert bt.status_code == 403
        assert c.post(f"{P}/sessions/{sa}/end-beacon", json={"token": ta}).status_code == 204
        st.pt.wait(lambda: st.broker.sessions[sa].state == "ENDED", 10, "beacon ended")


def test_queue_long_poll_slot_ready_and_claim(st: SbxStack) -> None:
    _end_all(st)
    with _client(st) as c:
        s1 = _create(c, 21).json()
        _create(c, 22)
        q = _create(c, 23)
        assert q.status_code == 202 and q.headers["location"].startswith(f"{P}/queue/q-")
        assert rtc.schema_errors("sandbox/session.schema.json", q.json(), "#/$defs/queued") == []
        t = q.json()["queue"]
        qa = {"authorization": f"Bearer {t['ticket_token']}"}
        v = c.get(f"{P}/queue/{t['ticket_id']}", headers=qa)
        assert v.status_code == 200 and rtc.schema_errors("sandbox/ticket.schema.json", v.json()) == []
        assert v.json()["state"] == "waiting" and v.json()["position"] == 1
        assert c.get(f"{P}/queue/{t['ticket_id']}").status_code == 401
        assert c.get(f"{P}/queue/{t['ticket_id']}", headers={"authorization": f"Bearer {s1['token']}"}).status_code == 401
        assert c.get(f"{P}/queue/q-aaaaaaaaaa", headers=qa).json()["code"] == 511
        assert c.post(f"{P}/queue/{t['ticket_id']}/claim", headers=qa).json()["code"] == 105
        sid1 = s1["session"]["sid"]
        _ready(c, sid1, s1["token"])
        t0 = time.monotonic()
        c.delete(f"{P}/sessions/{sid1}", headers={"authorization": f"Bearer {s1['token']}"})
        lp = c.get(f"{P}/queue/{t['ticket_id']}", headers=qa, params={"wait_s": 20})
        assert lp.json()["state"] == "slot_ready" and time.monotonic() - t0 <= 5.0
        assert lp.json()["claim_deadline_unix_ns"] is not None
        cl = c.post(f"{P}/queue/{t['ticket_id']}/claim", headers=qa)
        assert cl.status_code == 201 and cl.json()["token"] and cl.json()["session"]["world_id"] == "synthcity"
        t1 = time.monotonic()
        _ready(c, cl.json()["session"]["sid"], cl.json()["token"])
        assert time.monotonic() - t1 <= 6.0
        q2 = _create(c, 24).json()["queue"]
        qa2 = {"authorization": f"Bearer {q2['ticket_token']}"}
        assert c.delete(f"{P}/queue/{q2['ticket_id']}", headers=qa2).status_code == 204
        assert c.get(f"{P}/queue/{q2['ticket_id']}", headers=qa2).json()["state"] == "expired"


def test_world_rules_and_validation(st: SbxStack) -> None:
    _end_all(st)
    with _client(st) as c:
        assert _create(c, 31, world="atlantis").json()["code"] == 507
        assert _create(c, 31, principal_hint="nothex-0123456789").status_code == 422
        assert _create(c, 31, bogus=1).status_code == 400
        r = c.post(f"{P}/sessions", json={"world": "synthcity", "principal_hint": H(31), "template": "t1_point_watch",
                                          "config_snapshot": {"schema": "awr.sandbox.snapshot.v1"}})
        assert r.status_code == 422


@pytest.fixture(scope="module")
def pub():
    s = SbxStack(sb=make_sb(max_sessions=4, quota_profile="public", batch={"enabled": False}, pow={"bits": 6, "bits_hot": 8},
                            spawn={"timeout_s": 4}),
                 sb_api={"quota_profile": "public"}, access_mode="public")
    yield s
    s.close()


def test_public_quota_pow_and_batch_404(pub: SbxStack) -> None:
    with _client(pub, **{"x-forwarded-for": "203.0.113.50"}) as c:
        a = _create(c, 41)
        b = _create(c, 42)
        assert a.status_code == 201 and b.status_code == 201
        r = _create(c, 43)                                      # 伪造 XFF 不改变前缀：同一回环出口的第 3 个
        assert r.status_code == 409 and r.json()["code"] == 504 and r.json()["detail"]["why"] == "active_per_prefix"
        auth = {"authorization": f"Bearer {a.json()['token']}"}
        sid = a.json()["session"]["sid"]
        _ready(c, sid, a.json()["token"])
        assert c.post(f"{P}/sessions/{sid}/batch", headers=auth, json={"until_s": 60}).status_code == 404
        meta = c.get(f"{P}/meta").json()
        assert "batch" not in meta["features"] and "pow" in meta["features"] and meta["access_mode"] == "public"
        for x in (a, b):
            c.delete(f"{P}/sessions/{x.json()['session']['sid']}", headers={"authorization": f"Bearer {x.json()['token']}"})
        pub.pt.wait(lambda: not pub.broker.live(), 10, "ended")
        r = _create(c, 44)
        assert r.status_code == 429 and r.json()["code"] == 504 and int(r.headers["retry-after"]) >= 1

        def refill() -> None:
            pub.broker.buckets.clear()
            for k in list(pub.broker.creates):
                pub.broker.creates[k] = pub.broker.creates[k] + [time.time()]

        pub.pt.call(refill)                                     # 1 h 内第 3 次：要求工作量证明
        r = _create(c, 44)
        assert r.status_code == 409 and r.json()["code"] == 509
        ch = r.json()["detail"]["challenge"]
        nonce = solve(base64.b64decode(ch["prefix_b64"]), ch["bits"])
        ok = _create(c, 44, pow={"challenge_id": ch["challenge_id"], "nonce": nonce})
        assert ok.status_code == 201, ok.text
        rp = _create(c, 45, pow={"challenge_id": ch["challenge_id"], "nonce": nonce})
        assert rp.status_code == 409 and rp.json()["detail"]["why"] == "invalid"
