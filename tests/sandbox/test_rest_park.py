"""SB-69、SB-70、SB-72 与内部档创建经真实 sandbox-api 与真实池（M17-AC-027、029、030 的 REST 部分；17 §17.20.4、§17.20.5；
API-AC-066）：驻留后会话对象字段、需要 sim-core 的端点 409 `515`、驻留期间过期 token 的恢复与换发、信标驻留、满员恢复票与认领、
`X-AWR-Identity` 内部档（预留槽位、token 档位）、无效身份令牌 401。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest
from sbharness import SbxStack, make_sb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
from identity import make_secret_file  # 正式夹具（WP-13b，d2-WP-13b-to-M17 第 1 条）

from awr.runtime.sbauth import peek_token_payload
from awr.sandbox.identity import IdentityService

HERE = Path(__file__).resolve().parent
P = "/api/sandbox/v1"
PARK_SCHEMA = json.loads((HERE / "fixtures" / "contracts_draft" / "park.schema.json").read_text(encoding="utf-8"))


def _errs(doc: Any, ref: str) -> list[str]:
    v = jsonschema.Draft202012Validator({**PARK_SCHEMA, "$ref": f"#/$defs/{ref}"})
    return [e.message for e in v.iter_errors(doc)]


def H(i: int) -> str:
    return f"{i:032x}"


def _client(st: SbxStack, **headers: str) -> httpx.Client:
    return httpx.Client(base_url=st.base, headers={"origin": st.origin, **headers}, timeout=15)


@pytest.fixture(scope="module")
def st(tmp_path_factory: pytest.TempPathFactory):
    s = SbxStack(sb=make_sb(max_sessions=1, spawn={"timeout_s": 4}),
                 sb_api={"rest": {"rps": 200, "burst": 400, "clock_rps": 100, "entity_rps": 100}})
    d = tmp_path_factory.mktemp("ident")
    path, secret = make_secret_file(d)
    svc = IdentityService(secret=path.read_bytes().strip(), state_dir=d / "state")

    def inject() -> None:                                              # 池是内部档令牌的验签方（awr.api 不依赖 awr.sandbox）
        s.pt.pool.identity = svc
        s.broker.is_revoked = svc.is_revoked

    s.pt.call(inject)
    s.secret = secret                                                  # type: ignore[attr-defined]
    s.identity = svc                                                   # type: ignore[attr-defined]
    yield s
    s.close()


def _end_all(st: SbxStack) -> None:
    def go() -> None:
        for r in list(st.broker.sessions.values()):
            if r.state not in ("ENDED", "FAILED"):
                st.broker.stop(r.sid)
        for t in st.broker.queue.open():
            st.broker.queue.close(t, "LEFT", st.broker.now())

    st.pt.call(go)
    st.pt.wait(lambda: all(r.state in ("ENDED", "FAILED") for r in st.broker.sessions.values()), 20, "all ended")


def _start(c: httpx.Client, st: SbxStack, i: int) -> tuple[str, str]:
    r = c.post(f"{P}/sessions", json={"world": "synthcity", "principal_hint": H(i)})
    assert r.status_code == 201, r.text
    sid, tok = r.json()["session"]["sid"], r.json()["token"]
    g = c.get(f"{P}/sessions/{sid}", params={"wait_state": "READY", "wait_s": 10}, headers={"authorization": f"Bearer {tok}"})
    assert g.json()["state"] in ("READY", "ACTIVE")
    return sid, tok


def test_park_resume_with_expired_token(st: SbxStack) -> None:
    _end_all(st)
    with _client(st) as c:
        sid, tok = _start(c, st, 1)
        auth = {"authorization": f"Bearer {tok}"}
        r = c.post(f"{P}/sessions/{sid}/park", json={"reason": "idle"}, headers=auth)
        assert r.status_code == 202 and r.json()["state"] == "PARKING" and not _errs(r.json(), "park_reply")
        st.pt.wait(lambda: st.broker.sessions[sid].state == "PARKED", 8, "PARKED")
        s = c.get(f"{P}/sessions/{sid}", headers=auth).json()
        assert s["state"] == "PARKED" and s["park_reason"] == "idle" and s["resume_mode"] == "checkpoint"
        assert not _errs(s, "session_d21") and "retention_unix_ns" in s and s["renewable"] is False
        for method, path, body in (("POST", "/clock", {"op": "play"}), ("POST", "/keepalive", {}),
                                   ("GET", "/events", None), ("POST", "/template", {"template": "t1_point_watch"})):
            rr = c.request(method, f"{P}/sessions/{sid}{path}", json=body, headers=auth)
            assert rr.status_code == 409 and rr.json()["code"] == 515, (path, rr.text)
            assert rr.json()["detail"]["resume_url"].endswith(f"/sessions/{sid}/resume")
        snap = c.get(f"{P}/sessions/{sid}/snapshot", headers=auth)
        assert snap.status_code == 200 and snap.json()["world_id"] == "synthcity"
        pid = st.broker.sessions[sid].principal_id
        expired, _ = st.api.ctx.tokens.issue(pid, sid, exp_unix_s=int(time.time()) - 120)
        assert c.get(f"{P}/sessions/{sid}", headers={"authorization": f"Bearer {expired}"}).status_code == 401
        assert c.post(f"{P}/sessions/{sid}/resume").status_code == 401
        res = c.post(f"{P}/sessions/{sid}/resume", headers={"authorization": f"Bearer {expired}"})
        assert res.status_code == 201, res.text
        body = res.json()
        assert not _errs(body, "resume_created") and body["token"] and body["session"]["sid"] == sid
        new = {"authorization": f"Bearer {body['token']}"}
        g = c.get(f"{P}/sessions/{sid}", params={"wait_state": "READY", "wait_s": 10}, headers=new)
        assert g.status_code == 200 and g.json()["state"] in ("READY", "ACTIVE")
        assert c.post(f"{P}/sessions/{sid}/resume", headers=new).status_code == 200      # 已活动
    _end_all(st)


def test_park_beacon_and_resume_queue(st: SbxStack) -> None:
    _end_all(st)
    with _client(st) as c:
        sid, tok = _start(c, st, 2)
        bad = c.post(f"{P}/sessions/{sid}/park-beacon", json={"token": "v1.bad.bad", "reason": "hidden"})
        assert bad.status_code == 401
        b = c.post(f"{P}/sessions/{sid}/park-beacon", json={"token": tok, "reason": "hidden"})   # Blob 类型 application/json
        assert b.status_code == 204
        st.pt.wait(lambda: st.broker.sessions[sid].state == "PARKED", 8, "PARKED")
        assert st.broker.sessions[sid].park_reason == "hidden"
        other, _ = _start(c, st, 3)                                       # 槽位被他人占用
        q = c.post(f"{P}/sessions/{sid}/resume", headers={"authorization": f"Bearer {tok}"})
        assert q.status_code == 202 and not _errs(q.json(), "resume_queued"), q.text
        qq = q.json()["queue"]
        assert qq["kind"] == "resume" and q.headers["location"].endswith(qq["ticket_id"])
        qa = {"authorization": f"Bearer {qq['ticket_token']}"}
        st.pt.call(lambda: st.broker.stop(other))
        v = c.get(f"{P}/queue/{qq['ticket_id']}", params={"wait_s": 10}, headers=qa).json()
        if v["state"] != "slot_ready":
            v = c.get(f"{P}/queue/{qq['ticket_id']}", params={"wait_s": 10}, headers=qa).json()
        assert v["state"] == "slot_ready"
        cl = c.post(f"{P}/queue/{qq['ticket_id']}/claim", headers=qa, json={})
        assert cl.status_code == 201 and cl.json()["session"]["sid"] == sid and cl.json()["token"]
        st.pt.wait(lambda: st.broker.sessions[sid].state in ("READY", "ACTIVE"), 8, "READY")
    _end_all(st)


def test_internal_identity_create(st: SbxStack) -> None:
    _end_all(st)
    svc: IdentityService = st.identity                                   # type: ignore[attr-defined]
    v = svc.decide(st.secret, "198.51.100.77", principal_id="p-" + H(7))  # type: ignore[attr-defined]
    assert v.ok and v.token
    with _client(st) as c:
        _sid, _tok = _start(c, st, 4)                                     # 公开槽位已满
        r = c.post(f"{P}/sessions", json={"world": "synthcity", "principal_hint": H(5)}, headers={"x-awr-identity": v.token})
        assert r.status_code == 201, r.text                               # 预留槽位：N_max + 1
        p = peek_token_payload(r.json()["token"])
        assert p is not None and p["tier"] == "internal" and r.json()["session"]["tier"] == "internal"
        bad = c.post(f"{P}/sessions", json={"world": "synthcity", "principal_hint": H(6)},
                     headers={"x-awr-identity": v.token[:-3] + "AAA"})
        assert bad.status_code == 401 and bad.json()["detail"]["why"].startswith("identity_")
        svc.revoke(v.token)
        time.sleep(1.1)
        rv = c.post(f"{P}/sessions", json={"world": "synthcity", "principal_hint": H(8)}, headers={"x-awr-identity": v.token})
        assert rv.status_code == 401 and rv.json()["detail"]["why"] == "identity_revoked"
    _end_all(st)


def test_commander_pairing(st: SbxStack) -> None:
    _end_all(st)
    with _client(st) as c:
        sid, tok = _start(c, st, 9)
        r = c.post(f"{P}/sessions/{sid}/pair-code", headers={"authorization": f"Bearer {tok}"})
        assert r.status_code == 201 and len(r.json()["code"]) == 8
        assert c.post(f"{P}/sessions/{sid}/pair-code").status_code == 401           # 需要会话 token
        code = r.json()["code"]
        bad = c.post(f"{P}/sessions/{sid}/pair", json={"code": "AAAAAAAA"})
        assert bad.status_code == 401 and bad.json()["code"] == 302
        ok_ = c.post(f"{P}/sessions/{sid}/pair", json={"code": code})
        assert ok_.status_code == 201, ok_.text
        p_new, p_old = peek_token_payload(ok_.json()["token"]), peek_token_payload(tok)
        assert p_new is not None and p_old is not None and p_new["sub"] == p_old["sub"] and p_new["sid"] == sid
        g = c.get(f"{P}/sessions/{sid}", headers={"authorization": f"Bearer {ok_.json()['token']}"})
        assert g.status_code == 200
        assert c.post(f"{P}/sessions/{sid}/pair", json={"code": code}).status_code == 410   # 单次
    _end_all(st)


def test_reserve_only_for_supervisor_principal(st: SbxStack) -> None:
    import asyncio

    from awr.contracts import bus_keys
    from awr.runtime.bus import LocalBus
    from awr.runtime.sbauth import POOL_PRINCIPAL, POOL_SECRET_NAME, load_or_create_key, pool_key, sign_pool_request

    k = pool_key(load_or_create_key(st.settings.state_dir / POOL_SECRET_NAME))

    async def go(principal: str) -> dict:
        b = LocalBus.open("t-reserve", namespace=st.pt.namespace, announce=False, loop=asyncio.get_running_loop())
        try:
            return await b.call(bus_keys.sys_session("reserve"),
                                sign_pool_request(k, "reserve", {"mem_mb": 50, "ttl_s": 5}, principal=principal), timeout=2.0)
        finally:
            b.close()

    try:
        assert asyncio.run(go(POOL_PRINCIPAL))["code"] == 115                         # sandbox-api 不可调用
        rep = asyncio.run(go("svc:supervisor"))
        assert rep["status"] == "ok" and rep["result"]["ok"] is True
    finally:
        st.pt.call(lambda: st.broker.reserve(0, 0))


def test_sdk_create_with_identity_token(st: SbxStack) -> None:
    import sys

    sdk_dir = str(Path(__file__).resolve().parents[2] / "tools" / "sdk")
    if sdk_dir not in sys.path:
        sys.path.insert(0, sdk_dir)
    import awr_sandbox as A

    _end_all(st)
    svc: IdentityService = st.identity                                   # type: ignore[attr-defined]
    v = svc.decide(st.secret, "198.51.100.78", principal_id="p-" + H(11))  # type: ignore[attr-defined]
    sb = A.Sandbox(st.base, principal_hint="f" * 32, auto_keepalive=False, origin=st.origin)
    s = sb.create("synthcity", None, identity_token=v.token)             # M17-FR-080：内部档
    assert s.info.get("tier") == "internal" and peek_token_payload(s.token)["tier"] == "internal"
    s.close()
    sb.close()
    _end_all(st)
