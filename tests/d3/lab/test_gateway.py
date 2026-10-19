"""lab-gateway（`awr.bench.api.gateway`，AWR-17 §18.11；AWR-05 §7.11；D3-AC-51）：握手与鉴权、订阅作用域、数据下发与令牌撤销。

网关以真实 uvicorn 进程内服务运行（独立线程、回环随机端口），与 bench-api 共用数据根（同一 `keys/` 与 `bench.sqlite`，与 dmax
`d3` profile 中两个容器共享数据根卷相同）；短期令牌经 BN-04 `POST /session/short-token {aud: "lab-rt"}` 签发。客户端为
`websockets` 同步客户端（全部接收带超时）。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import msgpack
import pytest

pytest.importorskip("foxglove_schemas_protobuf")
uvicorn = pytest.importorskip("uvicorn")
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

from awr.bench.api import gateway as G  # noqa: E402
from awr.contracts import CONTRACTS_VERSION  # noqa: E402
from awr.contracts import frame as F  # noqa: E402

PROTO = "awr.rt.v1"


@contextmanager
def serve(lab, **hub_kw) -> Iterator[tuple[str, object]]:
    """在回环随机端口上运行 lab-gateway；产出 (ws 地址, app)。"""
    app = G.create_gateway_app(lab.settings, poll_s=hub_kw.pop("poll_s", 0.05), **hub_kw)
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws_max_size=G.MAX_TEXT_BYTES,
                                        lifespan="on"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline, "lab-gateway did not start"
        time.sleep(0.02)
    try:
        yield f"ws://127.0.0.1:{port}{G.WS_PATH}", app
    finally:
        srv.should_exit = True
        t.join(10)


def short(lab, role: str, aud: str = "lab-rt") -> str:
    r = lab.client.post(f"{lab.P}/session/short-token", json={"aud": aud}, headers=lab.bearer(role))
    assert r.status_code == 200, r.text
    return r.json()["token"]


def open_ws(url: str, token: str | None, *, origin: str | None = "https://lab.example", protos: list[str] | None = None):
    sp = protos if protos is not None else [PROTO, *([f"bearer.{token}"] if token else [])]
    return connect(url, subprotocols=sp, additional_headers={"Origin": origin} if origin else None, open_timeout=5, close_timeout=2)


def recv_until(ws, pred: Callable[[object], bool], timeout: float = 5.0) -> object:
    end = time.monotonic() + timeout
    while True:
        left = end - time.monotonic()
        assert left > 0, "timed out waiting for message"
        m = ws.recv(timeout=left)
        if pred(m):
            return m


def ctrl(op: str) -> Callable[[object], bool]:
    return lambda m: isinstance(m, str) and json.loads(m).get("op") == op


def closed_code(ws, timeout: float = 5.0) -> int:
    end = time.monotonic() + timeout
    with pytest.raises(ConnectionClosed) as ei:
        while time.monotonic() < end:
            ws.recv(timeout=max(0.01, end - time.monotonic()))
    return ei.value.rcvd.code


def handshake(ws) -> tuple[dict, list]:
    msgs = [ws.recv(timeout=5) for _ in range(3)]
    info, adv = json.loads(msgs[0]), json.loads(msgs[1])
    assert isinstance(msgs[2], bytes) and msgs[2][0] == F.OP_TIME
    ws.send(json.dumps({"op": "hello", "contracts": CONTRACTS_VERSION, "client": "test"}))
    return info, adv["channels"]


def records(frame: bytes) -> list[F.Record]:
    assert frame[0] == F.OP_BATCH
    return list(F.iter_records(frame))


class Rec:
    """BATCH 中一条记录：记录头字段与载荷字节。"""

    def __init__(self, frame: bytes, r: F.Record) -> None:
        self.channel_id, self.encoding, self.rflags, self.seq, self.dt_us = r.channel_id, r.encoding, r.rflags, r.seq, r.dt_us
        self.payload = bytes(frame[r.payload_off:r.payload_off + r.length])


def next_record(ws, cid: int, timeout: float = 5.0) -> tuple[F.BatchHeader, Rec]:
    end = time.monotonic() + timeout
    while True:
        m = recv_until(ws, lambda x: isinstance(x, bytes) and x[0] == F.OP_BATCH, max(0.01, end - time.monotonic()))
        hit = [r for r in records(m) if r.channel_id == cid]
        if hit:
            return F.decode_batch_header(m), Rec(m, hit[-1])


def collect(ws, cids: list[int], timeout: float = 5.0) -> dict[int, Rec]:
    """读 BATCH 直到每个 channel 至少一条记录（同一运行的多个 channel 可能在同一帧中）。"""
    end = time.monotonic() + timeout
    out: dict[int, Rec] = {}
    while set(cids) - out.keys():
        m = recv_until(ws, lambda x: isinstance(x, bytes) and x[0] == F.OP_BATCH, max(0.01, end - time.monotonic()))
        out.update({r.channel_id: Rec(m, r) for r in records(m) if r.channel_id in cids})
    return out


def subscribe(ws, *subs: dict) -> dict[int, dict]:
    """发送一次 subscribe，收集每个订阅项的应答（subscribed 或 error）。"""
    ws.send(json.dumps({"op": "subscribe", "subs": list(subs)}))
    want = {s["id"] for s in subs}
    got: dict[int, dict] = {}
    while want - got.keys():
        m = recv_until(ws, lambda x: isinstance(x, str) and json.loads(x).get("op") in ("subscribed", "error"))
        d = json.loads(m)
        key = d["id"] if d["op"] == "subscribed" else d.get("ref", {}).get("id")
        got[key] = d
    return got


# ── 握手与鉴权 ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_handshake_server_info_and_ping(lab) -> None:
    with serve(lab) as (url, _app), open_ws(url, short(lab, "researcher")) as ws:
        assert ws.subprotocol == PROTO
        info, adv = handshake(ws)
        assert info["op"] == "serverInfo" and info["protocol"] == PROTO and "lab" in info["capabilities"]
        assert info["lab"] == {"principal": "alice", "role": "researcher", "projects": ["d3"]}
        assert "run" not in info and "world" not in info and adv == []          # 不含任何 run 级 sim-core 信息
        ws.send(json.dumps({"op": "ping", "t": 12.5}))
        pong = json.loads(recv_until(ws, ctrl("pong")))
        assert pong["t"] == 12.5
        ws.send(json.dumps({"op": "call", "id": "x-0001"}))
        assert json.loads(recv_until(ws, ctrl("error")))["code"] == 313


def test_token_failures_close_4401_and_audit(lab) -> None:
    auth = lab.app.state.auth
    with serve(lab) as (url, _app):
        with open_ws(url, None) as ws:
            assert json.loads(ws.recv(timeout=5))["code"] == 301
            assert closed_code(ws) == 4401
        for bad in (lab.tokens["researcher"], short(lab, "researcher", aud="frames"), "d3st_v1.e30.forged"):
            with open_ws(url, bad) as ws:
                assert json.loads(ws.recv(timeout=5))["code"] == 302
                assert closed_code(ws) == 4401
    with pytest.raises(Exception) as ei:                               # 受邀成员不签发 lab-rt 令牌（§7.11）
        auth.sign_short(auth.verify_token(lab.tokens["invited"]), "lab-rt")
    assert getattr(ei.value, "code", None) == 115
    deny = [r for r in lab.audit() if r["action"] == "deny" and r.get("route") == "lab-rt"]
    assert len(deny) == 4 and {r["code"] for r in deny} == {301, 302}
    text = json.dumps(lab.audit())
    assert lab.tokens["researcher"] not in text and "d3st_v1." not in text


def test_origin_and_subprotocol_rejected_before_upgrade(lab) -> None:
    tok = short(lab, "researcher")
    with serve(lab) as (url, _app):
        with pytest.raises(InvalidStatus) as ei:
            open_ws(url, tok, origin="https://evil.example")
        assert ei.value.response.status_code == 403 and json.loads(ei.value.response.body)["code"] == 303
        with pytest.raises(InvalidStatus) as ei:
            open_ws(url, tok, protos=[f"bearer.{tok}"])
        assert ei.value.response.status_code == 400
        assert ei.value.response.headers["AWR-Supported-Protocols"] == PROTO


def test_hello_timeout_and_contracts_major(lab, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(G, "HELLO_TIMEOUT_S", 0.3)
    with serve(lab) as (url, _app):
        with open_ws(url, short(lab, "researcher")) as ws:
            assert json.loads(recv_until(ws, ctrl("error")))["code"] == 317
            assert closed_code(ws) == 4408
        monkeypatch.setattr(G, "HELLO_TIMEOUT_S", 10.0)
        with open_ws(url, short(lab, "researcher")) as ws:
            for _ in range(3):
                ws.recv(timeout=5)
            ws.send(json.dumps({"op": "hello", "contracts": "99.0.0"}))
            assert json.loads(recv_until(ws, ctrl("error")))["code"] == 311
            assert closed_code(ws) == 4426


def test_connection_limit_per_principal(lab, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(G, "MAX_CONNS_PER_PRINCIPAL", 2)
    tok = short(lab, "researcher")
    with serve(lab) as (url, _app), open_ws(url, tok) as a, open_ws(url, tok) as b:
        handshake(a)
        handshake(b)
        with open_ws(url, tok) as c:
            assert json.loads(c.recv(timeout=5))["code"] == 316
            assert closed_code(c) == 4429


# ── 订阅作用域 ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_subscription_scope_matrix(lab) -> None:
    """研究者：状态限本人与所属项目、预览限本人运行、actor/state 需评测员；评测员不限；语法、参数与运行存在性。"""
    auth = lab.app.state.auth
    carol_tid, carol = auth.issue_token("carol", "researcher", ("d3",))       # 同项目、非提交者
    pv = "uav/uav-01/sensor/cam0/preview"
    with serve(lab) as (url, _app):
        with open_ws(url, short(lab, "researcher")) as ws:
            handshake(ws)
            got = subscribe(ws,
                            {"id": 1, "topic": f"bench/run/{lab.RUN_D3}/status"},
                            {"id": 2, "topic": f"bench/run/{lab.RUN_OTHER}/status"},
                            {"id": 3, "topic": pv, "params": {"run_id": lab.RUN_D3}, "rate": 2},
                            {"id": 4, "topic": pv, "params": {"run_id": lab.RUN_OTHER}},
                            {"id": 5, "topic": "actor/state", "params": {"run_id": lab.RUN_D3}},
                            {"id": 6, "topic": "uav/*/sensor/*/preview", "params": {"run_id": lab.RUN_D3}},
                            {"id": 7, "topic": "event/status"},
                            {"id": 8, "topic": "event/status", "params": {"run_id": "run-zzz0000001"}},
                            {"id": 9, "topic": f"bench/run/{lab.RUN_D3}/status", "params": {"run_id": lab.RUN_OTHER}},
                            {"id": 10, "topic": "uav/uav-01/state", "params": {"run_id": lab.RUN_D3}})
            assert got[1]["op"] == "subscribed" and got[1]["run_id"] == lab.RUN_D3
            assert got[3]["op"] == "subscribed" and got[3]["rate"] == 2.0
            assert (got[2]["code"], got[2]["detail"]["why"]) == (115, "project_scope")
            assert (got[4]["code"], got[4]["detail"]["why"]) == (115, "project_scope")
            assert (got[5]["code"], got[5]["detail"]["why"]) == (115, "role")
            assert got[6]["code"] == 314 and got[10]["code"] == 314
            assert got[7]["code"] == 300 and got[9]["code"] == 300 and got[8]["code"] == 305
        with open_ws(url, auth.sign_short(auth.verify_token(carol), "lab-rt")[0]) as ws:
            handshake(ws)
            got = subscribe(ws, {"id": 1, "topic": f"bench/run/{lab.RUN_D3}/status"}, {"id": 2, "topic": pv, "params": {"run_id": lab.RUN_D3}})
            assert got[1]["op"] == "subscribed" and (got[2]["code"], got[2]["detail"]["why"]) == (115, "owner_scope")
        with open_ws(url, short(lab, "evaluator")) as ws:
            handshake(ws)
            got = subscribe(ws, {"id": 1, "topic": f"bench/run/{lab.RUN_OTHER}/status"}, {"id": 2, "topic": pv, "params": {"run_id": lab.RUN_OTHER}},
                            {"id": 3, "topic": "actor/state", "params": {"run_id": lab.RUN_OTHER}})
            assert all(g["op"] == "subscribed" for g in got.values())
    deny = [r for r in lab.audit() if r["action"] == "deny" and r.get("route") == "lab-rt"]
    assert sorted(r["why"] for r in deny) == ["owner_scope", "project_scope", "project_scope", "role"]
    assert all(r["code"] == 115 and r["status"] == 403 and r["topic"] for r in deny)
    assert carol_tid


def test_preview_subscription_limit_per_run(lab) -> None:
    with serve(lab) as (url, _app), open_ws(url, short(lab, "evaluator")) as ws:
        handshake(ws)
        subs = [{"id": i, "topic": f"uav/uav-{i:02d}/sensor/cam0/preview", "params": {"run_id": lab.RUN_D3}} for i in range(1, 18)]
        got = subscribe(ws, *subs)
        assert sum(g["op"] == "subscribed" for g in got.values()) == G.MAX_PREVIEW_PER_RUN
        assert got[17]["code"] == 315
        got = subscribe(ws, {"id": 17, "topic": "uav/uav-17/sensor/cam0/preview", "params": {"run_id": lab.RUN_OTHER}})
        assert got[17]["op"] == "subscribed"                             # 上限按运行计


# ── 数据下发 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_run_status_snapshot_transition_and_live_counters(lab) -> None:
    st = lab.runs()
    topic = f"bench/run/{lab.RUN_D3}/status"
    with serve(lab) as (url, _app), open_ws(url, short(lab, "researcher")) as ws:
        handshake(ws)
        ws.send(json.dumps({"op": "subscribe", "subs": [{"id": 1, "topic": topic}]}))
        adv = json.loads(recv_until(ws, ctrl("advertise")))["channels"]
        assert len(adv) == 1 and adv[0]["topic"] == topic and adv[0]["entity"] == {"run_id": lab.RUN_D3}
        assert adv[0]["schemaName"] == "awr.bench.run_status.v1" and adv[0]["encoding"] == "msgpack"
        cid = adv[0]["id"]
        _h, r = next_record(ws, cid)
        body = msgpack.unpackb(r.payload)
        assert body["state"] == "QUEUED" and body["attempt"] == 1 and body["frames_delivered"] == 0
        assert r.encoding == F.ENC_MSGPACK and r.rflags & F.RF_KEYFRAME
        t0 = time.monotonic()
        st.transition(lab.RUN_D3, "PREPARING")
        while True:
            _h, r = next_record(ws, cid)
            if msgpack.unpackb(r.payload)["state"] == "PREPARING":
                break
        assert time.monotonic() - t0 < 1.0                             # 状态迁移立即发布（不等 1 Hz）
        G.write_live(lab.settings.data_root, lab.RUN_D3, topic,
                     msgpack.packb({"t_sim_ns": 5_000_000_000, "progress_pct": 12.5, "frames_delivered": 150, "state": "FORGED"}), 0)
        while True:
            h, r = next_record(ws, cid)
            b = msgpack.unpackb(r.payload)
            if b["frames_delivered"] == 150:
                break
        assert b["state"] == "PREPARING" and b["progress_pct"] == 12.5 and h.t_sim_ns == 5_000_000_000   # 状态字段只取状态库


def test_preview_and_event_status_phase_by_role(lab) -> None:
    root = lab.settings.data_root
    pv = "uav/uav-01/sensor/cam0/preview"
    blob = G.preview_blob(7, 2_000_000_000, bytes(_jpeg()))
    seq, t_ns, w, h = G.PREVIEW_HDR.unpack_from(blob)
    assert (seq, t_ns, w, h) == (7, 2_000_000_000, 480, 270) and blob[G.PREVIEW_HDR.size:][:2] == b"\xff\xd8"
    ev = {"event_id": "abc-crossing", "version": 1, "t_ms": 2000, "mode": "strict", "env": {}, "comms_active": [],
          "phase": [{"id": "p1", "type": "handoff", "state": "active"}]}
    with serve(lab) as (url, _app), open_ws(url, short(lab, "researcher")) as res, open_ws(url, short(lab, "evaluator")) as eva:
        handshake(res)
        handshake(eva)
        got_r = subscribe(res, {"id": 1, "topic": pv, "params": {"run_id": lab.RUN_D3}}, {"id": 2, "topic": "event/status", "params": {"run_id": lab.RUN_D3}})
        got_e = subscribe(eva, {"id": 2, "topic": "event/status", "params": {"run_id": lab.RUN_D3}})
        G.write_live(root, lab.RUN_D3, pv, blob, 2_000_000_000)
        G.write_live(root, lab.RUN_D3, "event/status", msgpack.packb(ev), 2_000_000_000)
        c1, c2 = got_r[1]["channels"][0], got_r[2]["channels"][0]
        recs = collect(res, [c1, c2])
        r = recs[c1]
        assert r.payload == blob and r.encoding == F.ENC_BLOB
        r = recs[c2]
        assert "phase" not in msgpack.unpackb(r.payload) and msgpack.unpackb(r.payload)["t_ms"] == 2000
        _h, r = next_record(eva, got_e[2]["channels"][0])
        assert msgpack.unpackb(r.payload)["phase"] == ev["phase"]
        # 迟到订阅：立即收到最新值（BATCH 置 SNAPSHOT）
        res.send(json.dumps({"op": "unsubscribe", "ids": [1]}))
        assert json.loads(recv_until(res, ctrl("unsubscribed")))["ids"] == [1]
        got = subscribe(res, {"id": 3, "topic": pv, "params": {"run_id": lab.RUN_D3}})
        h, r = next_record(res, got[3]["channels"][0])
        assert r.payload == blob and h.flags & F.BATCH_SNAPSHOT


def test_preview_rate_limited_to_subscription(lab) -> None:
    root = lab.settings.data_root
    pv = "uav/uav-01/sensor/cam0/preview"
    with serve(lab, poll_s=0.02) as (url, _app), open_ws(url, short(lab, "researcher")) as ws:
        handshake(ws)
        cid = subscribe(ws, {"id": 1, "topic": pv, "params": {"run_id": lab.RUN_D3}, "rate": 2})[1]["channels"][0]
        t_end = time.monotonic() + 2.0
        i = 0
        while time.monotonic() < t_end:                                 # 生产者 20 Hz 投递
            i += 1
            G.write_live(root, lab.RUN_D3, pv, G.PREVIEW_HDR.pack(i, i, 480, 270) + b"\xff\xd8x", i)
            time.sleep(0.05)
        n = 0
        with pytest.raises(TimeoutError):
            while True:
                m = ws.recv(timeout=0.6)
                if isinstance(m, bytes) and m[0] == F.OP_BATCH:
                    n += sum(1 for r in records(m) if r.channel_id == cid)
        assert 3 <= n <= 6                                              # 2 Hz × 2 s（首帧与收尾各允许 1 帧）


def test_revoked_bench_token_closes_connection_within_2s(lab) -> None:
    with serve(lab, token_check_s=0.5) as (url, _app), open_ws(url, short(lab, "researcher")) as ws:
        handshake(ws)
        subscribe(ws, {"id": 1, "topic": f"bench/run/{lab.RUN_D3}/status"})
        t0 = time.monotonic()
        assert lab.app.state.auth.revoke_token(lab.token_ids["researcher"])
        assert json.loads(recv_until(ws, lambda m: isinstance(m, str) and json.loads(m).get("code") == 302, 3.0))["op"] == "error"
        assert closed_code(ws) == 4401 and time.monotonic() - t0 <= 2.0
    acts = [r["action"] for r in lab.audit() if r.get("route") == "lab-rt"]
    assert "lab_rt_open" in acts and "lab_rt_close" in acts


def test_live_path_validation_and_healthz(lab) -> None:
    root = lab.settings.data_root
    with pytest.raises(ValueError):
        G.live_path(root, "../x", "event/status")
    with pytest.raises(ValueError):
        G.live_path(root, lab.RUN_D3, "uav/uav-01/state")
    p = G.write_live(root, lab.RUN_D3, "uav/uav-01/lidar/l0/v2", b"abc", 42)
    assert p.name == "uav~uav-01~lidar~l0~v2.awlv" and G.read_live(p) == (42, b"abc")
    p.write_bytes(b"XXXX" + b"\0" * 8)
    assert G.read_live(p) is None
    from fastapi.testclient import TestClient

    with TestClient(G.create_gateway_app(lab.settings)) as c:
        r = c.get(f"{G.WS_PATH}/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True, "protocol": PROTO, "connections": 0, "runs": 0}
    assert G.status_payload(None) == {"state": "UNKNOWN", "attempt": 1, "restart_count": 0, **G.STATUS_COUNTERS}


def _jpeg() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1920, 1080), (90, 120, 160)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_unbound_channels_are_released_and_ids_reused(lab) -> None:
    hub = G.LabHub(lab.settings, lab.app.state.auth)
    a = hub.channel(lab.RUN_D3, "event/status")
    b = hub.channel(lab.RUN_OTHER, "event/status")
    assert hub.channel(lab.RUN_D3, "event/status") is a and a.id != b.id
    hub.poll_once()                                                    # 无连接订阅：两个 channel 都释放
    assert hub.channels == {} and sorted(hub._free) == sorted([a.id, b.id])
    c = hub.channel(lab.RUN_TEAM, "event/status")
    assert c.id in (a.id, b.id) and hub._next_id == max(a.id, b.id) + 1
    hub.runs.close()
