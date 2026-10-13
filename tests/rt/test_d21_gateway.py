"""D2.1 网关侧（WP-03b，D2-INT 集成实现；M11-FR-220 至 FR-231；M11-AC-213、215、216、217；17 §17.20.3 至 §17.20.5）。

FakeSim + 真实 Gateway（create_app）：
- presence：限频（`presence_min_s`）、`last_input` 取 `now − input_age_s`、可见性；兜底回收的阈值选择（纯函数）；
- 空闲兜底：无 presence 的 viewer 在 `viewer_idle_s + grace_s`（测试 2 + 1 s，扫描 1 s）后收到 `error{518}` 再以 4430 断开；
  operator 按 `internal_idle_s` 不回收；
- 观看席位与排队：`viewer_seats.max = 2` 时第 3 个 viewer `error{517}` + 4431；R81 发票（幂等、第 1 位）、R82 长轮询到 `seat_ready`、
  轮到的 principal 占用预留席位；R83 离队；无效票 410 `519`；operator 与带身份令牌的连接不计席位；
- 状态文件 `AWR_SHOW_STATUS_FILE`：`{t_unix_ns, live, max, queue, eta_s, releases_per_s}` 随连接变化；
- serverInfo：`power`（与 `AWR_POWER_JSON` 一致）与 `identity{tier}`；`GET /api/health` 的 `build`；
- 内部身份（运行时随机口令，`tests/fixtures/identity.py`）：R79 正确 200、错误 401 `304`、未配置 403 `115`；R80 撤销后令牌失效；
  公开模式带身份令牌的 GET 可读隐藏功能族、写入仍 403；R01 以 `identity_token` 换取 admin；R02 的 `identity` 字段；
  审计不含口令与令牌。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path

import fakesim
import httpx
import pytest
import rtc
from websockets.asyncio.client import connect

from awr.api.rt import presence as P
from awr.api.viewer_queue import SeatsConfig, TicketError, ViewerSeats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
import identity as idfx  # noqa: E402

PROTO = "awr.rt.v1"
POWER = {"viewer_idle_s": 2, "viewer_idle_busy_s": 2, "grace_s": 1, "warn_s": 1, "presence_min_s": 1, "scan_s": 1,
         "internal_idle_s": 600}


@contextlib.contextmanager
def _env(**kv: str):
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update(kv)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------- 纯函数
class _S:
    def __init__(self, role: str = "viewer") -> None:
        self.role = role
        self.closing = False
        self.hello = True
        self.last_input_ns = time.time_ns()
        self.visible = True
        self.presence_seen = False
        self.presence_last_ns = 0
        self.presence_dropped = 0
        self.power_state = "active"
        self.identity_tier = "public"


def test_presence_rate_limit_and_last_input() -> None:
    p = P.PowerView(presence_min_s=15.0)
    s = _S()
    now = time.time_ns()
    s.last_input_ns = now - 100 * 10**9
    assert P.on_presence(s, {"op": "presence", "input_age_s": 4.0, "visible": False, "power": "quiet"}, p, now)
    assert abs(s.last_input_ns - (now - 4 * 10**9)) < 10**6 and s.visible is False and s.power_state == "quiet"
    assert not P.on_presence(s, {"op": "presence", "input_age_s": 0.0}, p, now + 5 * 10**9)   # 15 s 内的第二条忽略
    assert s.presence_dropped == 1 and abs(s.last_input_ns - (now - 4 * 10**9)) < 10**6
    assert P.on_presence(s, {"op": "presence", "input_age_s": 1e9}, p, now + 16 * 10**9)       # 钳制在 1 天内、只取较新
    assert abs(s.last_input_ns - (now - 4 * 10**9)) < 10**6


def test_idle_limits_by_role_visibility_and_busy() -> None:
    p = P.PowerView()
    v, op, hid, internal = _S(), _S("operator"), _S(), _S()
    hid.visible = False
    internal.identity_tier = "internal"
    assert P.idle_limit_s(v, p, busy=False) == 300 and P.idle_limit_s(v, p, busy=True) == 120
    assert P.idle_limit_s(op, p, busy=True) == 3600 and P.idle_limit_s(internal, p, busy=False) == 3600
    assert P.idle_limit_s(hid, p, busy=False) == 60
    now = time.time_ns()
    for s in (v, op, hid, internal):
        s.last_input_ns = now - 400 * 10**9
    due = dict((id(s), r) for s, r in P.reap_due([v, op, hid, internal], p, busy=False, now_ns=now))
    assert due == {id(v): "idle", id(hid): "hidden"}
    p.reap = False
    assert P.reap_due([v, hid], p, busy=False, now_ns=now) == []


def test_viewer_seats_fifo_reservation_and_expiry() -> None:
    t = [0.0]
    seats = ViewerSeats(SeatsConfig(max=2, reserve_s=30, ticket_idle_s=60, per_prefix=2, queue_max=3), clock=lambda: t[0])
    assert seats.admit("p-a") and seats.admit("p-b") and not seats.admit("p-c")
    tc, tok_c, created = seats.issue("p-c", "10.0.0.1/32")
    assert created and seats.position(tc) == 1 and seats.ticket_view(tc)["state"] == "waiting"
    _t2, tok_c2, again = seats.issue("p-c", "10.0.0.1/32")
    assert not again and seats.get(tc.ticket_id, tok_c2) is tc
    with pytest.raises(TicketError):
        seats.get(tc.ticket_id, tok_c)                          # 幂等发票换发了令牌
    td, _tok_d, _ = seats.issue("p-d", "10.0.0.1/32")
    with pytest.raises(TicketError) as e:
        seats.issue("p-e", "10.0.0.1/32")                       # 每前缀 2 张
    assert e.value.code == 504 and e.value.status == 409
    seats.release()                                             # 一个席位释放 → 队首 seat_ready 并预留
    assert tc.state == "seat_ready" and not seats.admit("p-x") and seats.admit("p-c")
    assert tc.ticket_id not in seats.tickets and seats.live == 2
    seats.release()
    assert td.state == "seat_ready"
    t[0] += 31.0                                                # 预留超时：作废，派发下一张（没有了）
    seats.gc()
    assert td.ticket_id not in seats.tickets and seats.free() == 1
    te, tok_e, _ = seats.issue("p-e", "10.0.0.2/32")
    assert te.state == "seat_ready"                             # 有空位时新票立即轮到
    t[0] += 61.0
    seats.gc()                                                  # seat_ready 的预留同样到期
    with pytest.raises(TicketError) as e2:
        seats.get(te.ticket_id, tok_e)
    assert e2.value.code == 519 and e2.value.status == 410
    snap = seats.snapshot()
    assert set(snap) >= {"t_unix_ns", "live", "max", "queue", "eta_s", "releases_per_s"} and snap["max"] == 2


# ---------------------------------------------------------------- 真实 Gateway
@pytest.fixture(scope="module")
def power_stack(tmp_path_factory):
    d = tmp_path_factory.mktemp("d21gw")
    status = d / "show.json"
    with _env(AWR_POWER_JSON=json.dumps(POWER), AWR_VIEWER_SEATS_JSON=json.dumps({"max": 2, "ticket_idle_s": 5}),
              AWR_SHOW_STATUS_FILE=str(status), AWR_IDENTITY_STATE_DIR=str(d / "identity")):
        s = fakesim.GwStack(n=1)
    s.status_file = status
    yield s
    s.close()


async def _open(stack, tok: str, *, ident: str | None = None, hello: bool = True, handshake: bool = True):
    protos = [PROTO, f"bearer.{tok}"] + ([f"ident.{ident}"] if ident else [])
    ws = await connect(stack.ws_url, subprotocols=protos, origin=stack.origin, max_size=None, compression=None, open_timeout=10)
    c = rtc.Client(ws, auto_ack=True)
    c.server_info = {}
    if handshake:
        c.server_info, _adv, _t = await c.handshake()
    if hello:
        await c.hello()
    return c


async def _until_closed(c, timeout: float) -> tuple[int | None, list[dict], float]:
    t0 = time.monotonic()
    with contextlib.suppress(Exception):
        while time.monotonic() - t0 < timeout:
            await c.recv(max(0.1, timeout - (time.monotonic() - t0)))
    return c.ws.close_code, list(c.texts), time.monotonic() - t0


def test_server_info_power_identity_and_health(power_stack) -> None:
    tok = rtc.token(power_stack.base, "viewer", rtc.hint_of("d21-info"))["token"]

    async def go() -> dict:
        c = await _open(power_stack, tok)
        try:
            return c.server_info
        finally:
            await c.ws.close()

    si = asyncio.run(go())
    assert si["power"]["viewer_idle_s"] == 2 and si["power"]["presence_min_s"] == 1 and si["identity"] == {"tier": "public"}
    assert rtc.ops_errors(si) == []
    h = httpx.get(power_stack.base + "/api/health", timeout=5).json()
    assert h["status"] == "ok" and set(h["build"]) == {"version", "channel", "release"} and h["build"]["version"] == "0.2.0"


def test_idle_viewer_is_reclaimed_with_518_and_4430(power_stack) -> None:
    tok = rtc.token(power_stack.base, "viewer", rtc.hint_of("d21-idle"))["token"]

    async def go():
        c = await _open(power_stack, tok)
        return await _until_closed(c, 12.0)

    code, texts, dt = asyncio.run(go())
    assert code == 4430, (code, texts[-3:])
    err = [t for t in texts if t.get("op") == "error" and t.get("code") == 518]
    assert err and err[-1]["detail"]["reason"] == "idle"
    assert 2.5 <= dt <= 6.5, dt                                  # 2 + 1 s 余量，扫描 1 s


def test_presence_postpones_and_operator_uses_internal_threshold(power_stack) -> None:
    vt = rtc.token(power_stack.base, "viewer", rtc.hint_of("d21-pres"))["token"]
    ot = rtc.token(power_stack.base, "operator", rtc.hint_of("d21-op"))["token"]

    async def go():
        v = await _open(power_stack, vt)
        o = await _open(power_stack, ot)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 6.0:
            await v.send({"op": "presence", "input_age_s": 0.2, "visible": True, "power": "active"})
            await v.drain(1.1)
        alive_v = v.ws.close_code is None
        await o.drain(0.5)
        alive_o = o.ws.close_code is None
        code, _t, dt = await _until_closed(v, 10.0)
        await o.ws.close()
        return alive_v, alive_o, code, dt

    alive_v, alive_o, code, dt = asyncio.run(go())
    assert alive_v and alive_o
    assert code == 4430 and dt <= 6.5, (code, dt)


def test_viewer_seats_full_4431_and_viewer_queue(power_stack) -> None:
    base = power_stack.base
    hints = [rtc.hint_of(f"d21-seat-{k}") for k in range(3)]
    toks = [rtc.token(base, "viewer", h)["token"] for h in hints]
    op = rtc.token(base, "operator", rtc.hint_of("d21-op"))["token"]  # 同一席位持有者（幂等）

    async def go():
        held = [await _open(power_stack, toks[0]), await _open(power_stack, toks[1])]
        o = await _open(power_stack, op)                         # operator 不计席位
        c3 = await _open(power_stack, toks[2], hello=False, handshake=False)
        code, texts, _ = await _until_closed(c3, 5.0)
        assert code == 4431 and any(t.get("code") == 517 and t["detail"]["queue_url"] == "/api/viewer/queue" for t in texts), \
            (code, texts[-3:])
        async with httpx.AsyncClient(base_url=base, timeout=30.0) as h:
            r = await h.post("/api/viewer/queue", json={"principal_hint": hints[2]})
            assert r.status_code == 201, r.text
            q = r.json()
            assert q["position"] == 1 and q["state"] == "waiting" and q["ticket_token"].startswith("vq1.")
            assert q["ticket_id"].startswith("vq-")
            auth = {"authorization": f"Bearer {q['ticket_token']}"}
            bad = await h.get(f"/api/viewer/queue/{q['ticket_id']}", params={"wait_s": 0},
                              headers={"authorization": "Bearer vq1.wrong"})
            assert bad.status_code == 410 and bad.json()["code"] == 519
            await asyncio.sleep(1.2)
            st = json.loads(power_stack.status_file.read_text())
            assert st["live"] == 2 and st["max"] == 2 and st["queue"] == 1
            await held[0].ws.close()
            t0 = time.monotonic()
            g = (await h.get(f"/api/viewer/queue/{q['ticket_id']}", params={"wait_s": 5}, headers=auth)).json()
            assert g["state"] == "seat_ready" and time.monotonic() - t0 < 5.0, g
            c3b = await _open(power_stack, toks[2])              # 预留席位
            assert c3b.server_info["identity"]["tier"] == "public"
            r2 = await h.post("/api/viewer/queue", json={"principal_hint": rtc.hint_of("d21-seat-x")})
            assert r2.status_code == 201
            q2 = r2.json()
            d = await h.delete(f"/api/viewer/queue/{q2['ticket_id']}", headers={"authorization": f"Bearer {q2['ticket_token']}"})
            assert d.status_code == 204
            d2 = await h.delete(f"/api/viewer/queue/{q2['ticket_id']}", headers={"authorization": f"Bearer {q2['ticket_token']}"})
            assert d2.status_code == 410 and d2.json()["code"] == 519
        for c in (held[1], o, c3b):
            await c.ws.close()

    asyncio.run(go())
    time.sleep(1.5)
    st = json.loads(power_stack.status_file.read_text())
    assert st["live"] == 0 and st["queue"] == 0, st


# ---------------------------------------------------------------- 内部身份
@pytest.fixture(scope="module")
def ident_stack(tmp_path_factory):
    d = tmp_path_factory.mktemp("d21id")
    sf = idfx.secret_file(d)
    env = {k: str(v) for k, v in sf.env().items()}
    env.setdefault("AWR_IDENTITY_STATE_DIR", str(d / "identity"))
    env["AWR_IDENTITY_JSON"] = json.dumps({"delay_ms": 50, "jitter_ms": 5, "rps": 20, "burst": 20})
    site = "https://drone4d.example.test"
    with _env(**env):
        s = fakesim.GwStack(n=1, access_mode="public", origins=[site], allowed_hosts_extra=["drone4d.example.test"],
                            admin_password=sf.secret, max_conns=64,
                            persist_dir=d / "persist", run_dir=d / "run")
    s.site = site
    s.sf = sf
    s.dir = d
    yield s
    s.close()


def test_identity_unlock_wrong_logout_and_exchange(ident_stack) -> None:
    base, sf = ident_stack.base, ident_stack.sf
    hdr = {"Origin": ident_stack.site}
    w = httpx.post(base + "/api/auth/identity", json={"secret": sf.secret + "-x", "principal_hint": rtc.hint_of("d21-id")},
                   headers=hdr, timeout=10)
    assert w.status_code == 401 and w.json()["code"] == 304 and sf.secret not in w.text
    r = httpx.post(base + "/api/auth/identity", json={"secret": sf.secret, "principal_hint": rtc.hint_of("d21-id")},
                   headers=hdr, timeout=10)
    assert r.status_code == 200, r.text
    j = r.json()
    tok = j["identity_token"]
    assert j["tier"] == "internal" and tok.startswith("v1.") and sf.secret not in r.text
    # 公开模式：隐藏功能族对 viewer 404，带身份令牌的 GET 放行（按 admin）；写入仍 403
    vt = httpx.post(base + "/api/auth/token", json={"role": "viewer"}, headers=hdr, timeout=10).json()["token"]
    assert httpx.get(base + "/api/sys/audit", headers={**hdr, "Authorization": f"Bearer {vt}"}, timeout=10).status_code == 404
    g = httpx.get(base + "/api/sys/audit", headers={**hdr, "Authorization": f"Bearer {vt}", "X-AWR-Identity": tok}, timeout=10)
    assert g.status_code != 404, g.text[:200]
    wr = httpx.post(base + "/api/sys/restart", json={"name": "sim-core"},
                    headers={**hdr, "Authorization": f"Bearer {vt}", "X-AWR-Identity": tok}, timeout=10)
    assert wr.status_code == 403 and wr.json()["code"] == 115
    who = httpx.get(base + "/api/auth/whoami", headers={**hdr, "Authorization": f"Bearer {vt}", "X-AWR-Identity": tok}, timeout=10)
    assert who.json()["identity"]["tier"] == "internal"
    # R01：身份令牌换取 admin（不需要口令）
    a = httpx.post(base + "/api/auth/token", json={"role": "admin", "identity_token": tok, "principal_hint": rtc.hint_of("d21-id")},
                   headers=hdr, timeout=10)
    assert a.status_code == 200 and a.json()["role"] == "admin", a.text
    bad = httpx.post(base + "/api/auth/token", json={"role": "admin", "identity_token": tok[:-2] + "xx"}, headers=hdr, timeout=10)
    assert bad.status_code == 401 and bad.json()["code"] == 302
    # R80：撤销后令牌失效
    d = httpx.delete(base + "/api/auth/identity", headers={**hdr, "X-AWR-Identity": tok}, timeout=10)
    assert d.status_code == 204
    g2 = httpx.get(base + "/api/sys/audit", headers={**hdr, "Authorization": f"Bearer {vt}", "X-AWR-Identity": tok}, timeout=10)
    assert g2.status_code == 404
    again = httpx.post(base + "/api/auth/token", json={"role": "admin", "identity_token": tok}, headers=hdr, timeout=10)
    assert again.status_code == 401 and again.json()["detail"]["why"] == "identity_revoked"
    assert httpx.delete(base + "/api/auth/identity", headers=hdr, timeout=10).status_code == 401
    # 审计四类（unlock、denied、logout、exchange）且不含口令与令牌
    ident_stack.ctx.audit.close()
    lines = [json.loads(x) for p in ident_stack.dir.rglob("audit.jsonl") for x in p.read_text().splitlines() if x.strip()]
    kinds = {x["kind"] for x in lines}
    assert {"auth.identity_unlock", "auth.identity_denied", "auth.identity_logout", "auth.identity_exchange"} <= kinds, kinds
    blob = json.dumps(lines)
    assert sf.secret not in blob and tok not in blob


def test_identity_ws_not_counted_and_tier_internal(ident_stack) -> None:
    base, sf = ident_stack.base, ident_stack.sf
    hdr = {"Origin": ident_stack.site}
    tok = httpx.post(base + "/api/auth/identity", json={"secret": sf.secret}, headers=hdr, timeout=10).json()["identity_token"]
    vt = httpx.post(base + "/api/auth/token", json={"role": "viewer"}, headers=hdr, timeout=10).json()["token"]

    async def go() -> tuple[dict, int]:
        protos = [PROTO, f"bearer.{vt}", f"ident.{tok}"]
        ws = await connect(ident_stack.ws_url, subprotocols=protos, origin=ident_stack.site, max_size=None, compression=None,
                           open_timeout=10)
        c = rtc.Client(ws, auto_ack=True)
        info, _adv, _t = await c.handshake()
        live = ident_stack.ctx.seats.live
        await ws.close()
        return info, live

    si, live = asyncio.run(go())
    assert si["identity"]["tier"] == "internal" and int(si["identity"]["exp_unix_ns"]) > time.time_ns()
    assert live == 0


def test_identity_disabled_without_secret_file(tmp_path) -> None:
    with _env(AWR_IDENTITY_STATE_DIR=str(tmp_path / "identity")):
        os.environ.pop("AWR_ADMIN_SECRET_FILE", None)
        s = fakesim.GwStack(n=1)
    try:
        r = httpx.post(s.base + "/api/auth/identity", json={"secret": "anything-at-all"}, timeout=10)
        assert r.status_code == 403 and r.json()["code"] == 115 and r.json()["detail"]["why"] == "PUBLIC_READONLY"
    finally:
        s.close()
