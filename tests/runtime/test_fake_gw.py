"""tools/fake/fake_gw.py：合成 N ∈ {1, 200, 1000} 与回放 .awrrt（D1-AC-35 的 Python 部分；M11-AC-040；AWR-17 §6）。

参考客户端（本文件）用生成的 `awr.contracts.frame` 与 `layouts` 解码，按契约 schema 校验全部控制消息与 msgpack 载荷；
回放用例与 `packages/contracts/fixtures/payloads/*.json` golden 逐项比对。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import subprocess
import sys
import time
from pathlib import Path

import msgpack
import numpy as np
import pytest
import rtlib
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from awr.contracts import CONTRACTS_VERSION
from awr.contracts import frame as F
from awr.contracts.layouts import DRONE_STATE64, ENV_SAMPLE32, SWARM_LITE32

FAKE_GW = rtlib.ROOT / "tools" / "fake" / "fake_gw.py"
FIX = rtlib.ROOT / "packages" / "contracts" / "fixtures"
OPS = "rt/ops.schema.json"
S2C = "#/$defs/serverToClient"


class Gw:
    def __init__(self, *args: str) -> None:
        self.p = subprocess.Popen([sys.executable, str(FAKE_GW), "--port", "0", *args], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=rtlib.child_env())
        line = self.p.stdout.readline()
        assert "READY" in line, line + self.p.stderr.read()
        self.url = line.split()[2]

    def close(self) -> str:
        self.p.terminate()
        try:
            _, err = self.p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            err = ""
        return err


@pytest.fixture
def gw_factory():
    gws: list[Gw] = []

    def make(*args: str) -> Gw:
        g = Gw(*args)
        gws.append(g)
        return g

    yield make
    for g in gws:
        g.close()


def ops_errors(msg: dict) -> list[str]:
    return rtlib.schema_errors(OPS, msg, S2C)


async def handshake(ws) -> tuple[dict, dict, F.TimeFrame]:
    info = json.loads(await ws.recv())
    adv = json.loads(await ws.recv())
    t = F.decode_time(await ws.recv())
    return info, adv, t


@pytest.mark.parametrize("n", [1, 200, 1000])
def test_synthetic_session(gw_factory, n: int) -> None:
    g = gw_factory("--n", str(n), "--event-period-s", "0.5", "--env-period-s", "1.0", "--calls", "ack")

    async def main() -> None:
        async with connect(g.url, subprotocols=["awr.rt.v1", "bearer.test"], max_size=None) as ws:
            assert ws.subprotocol == "awr.rt.v1"
            info, adv, t0 = await handshake(ws)
            assert ops_errors(info) == [] and info["protocol"] == "awr.rt.v1" and info["contracts"] == CONTRACTS_VERSION
            assert ops_errors(adv) == []
            chans = {c["id"]: c for c in adv["channels"]}
            by_topic = {c["topic"]: c for c in adv["channels"]}
            assert "swarm/uav/state" in by_topic and "swarm/state" not in by_topic  # 别名不出现在 advertise
            assert t0.epoch == 1 and t0.state == 1
            uid = "uav0001"
            await ws.send(json.dumps({"op": "hello", "client": "pytest", "contracts": CONTRACTS_VERSION, "tier": "S"}))
            subs = [("fleet/roster", 10), ("swarm/state", 5), (f"uav/{uid}/state", 60), (f"uav/{uid}/state_ext", 2),
                    (f"uav/{uid}/env", 10), ("env/state", 10), ("perf/server", 1), ("sys/procs", 1)]
            await ws.send(json.dumps({"op": "subscribe", "subs": [{"id": i + 1, "topic": tp, "rate": r, "mode": "latest"}
                                                                  for i, (tp, r) in enumerate(subs)]
                                      + [{"id": 99, "topic": "event", "rate": 0, "mode": "all"}]}))
            await ws.send(json.dumps({"op": "ping", "t": 12.5, "srttMs": 1.0}))
            await ws.send(json.dumps({"op": "call", "id": "c-1", "service": f"uav/{uid}/cmd/hover", "args": {}}))
            await ws.send(json.dumps({"op": "call", "id": "c-2", "service": "bogus/op", "args": {}}))
            texts, frames, times = [], [], []
            t_end = time.monotonic() + 2.6
            while time.monotonic() < t_end:
                try:
                    m = await asyncio.wait_for(ws.recv(), 0.5)
                except TimeoutError:
                    continue
                if isinstance(m, bytes):
                    if m[0] == F.OP_BATCH:
                        frames.append(m)
                        await ws.send(json.dumps({"op": "ack", "frame": F.decode_batch_header(m).frame_seq}))
                    else:
                        times.append(F.decode_time(m))
                else:
                    texts.append(json.loads(m))
        # 控制消息全部符合 ops.schema.json
        for m in texts:
            assert ops_errors(m) == [], m
        subd = {m["id"]: m for m in texts if m["op"] == "subscribed"}
        assert subd[2]["topic"] == "swarm/uav/state" and subd[2]["rate"] == 10  # 别名可订阅；swarm ≥ 10 Hz
        assert subd[3]["rate"] == 60 and subd[99]["mode"] == "all"
        pong = next(m for m in texts if m["op"] == "pong")
        assert pong["t"] == 12.5 and pong["epoch"] == 1 and pong["server_ns"] > 0
        results = [m for m in texts if m["op"] == "result"]
        assert [(r["id"], r["status"]) for r in results if r["id"] == "c-1"] == [("c-1", "accepted"), ("c-1", "succeeded")]
        assert next(r for r in results if r["id"] == "c-2")["code"] == 110
        evs = [m for m in texts if m["op"] in ("event", "events")]
        assert evs
        # TIME 10 Hz、纪元一致、t_sim 单调
        assert len(times) >= 18 and all(tf.epoch == 1 for tf in times)
        assert all(b.t_sim_ns >= a.t_sim_ns for a, b in itertools.pairwise(times))
        # BATCH：frame_seq 连续、首帧 SNAPSHOT、记录 8 字节对齐、载荷按布局可解码
        hdrs = [F.decode_batch_header(f) for f in frames]
        assert [h.frame_seq for h in hdrs] == list(range(1, len(hdrs) + 1))
        assert hdrs[0].flags & F.BATCH_SNAPSHOT and all(h.epoch == 1 for h in hdrs)
        seen: dict[str, list] = {}
        for f in frames:
            for r in F.iter_records(f):
                assert r.payload_off % 8 == 0
                body = f[r.payload_off:r.payload_off + r.length]
                seen.setdefault(chans[r.channel_id]["topic"], []).append((r, body))
        _, roster = seen["fleet/roster"][0]
        ro = msgpack.unpackb(roster)
        assert rtlib.schema_errors("rt/payloads/fleet_roster.schema.json", ro) == [] and len(ro["entries"]) == n
        _, sw = seen["swarm/uav/state"][-1]
        lite = np.frombuffer(sw, SWARM_LITE32)
        assert len(lite) == n and list(lite["agent_no"]) == list(range(n))
        q = lite["q_snorm"].astype(np.float64) / 32767
        assert np.allclose((q ** 2).sum(axis=1), 1.0, atol=1e-3) and np.all(np.isfinite(lite["pos"]))
        full_recs = seen[f"uav/{uid}/state"]
        assert len(full_recs) >= 100  # 60 Hz × 2.6 s（逐帧 ack）
        full = np.frombuffer(full_recs[-1][1], DRONE_STATE64)
        assert len(full) == 1 and int(full["agent_no"][0]) == 0
        seqs = [r.seq for r, _ in full_recs]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        env = np.frombuffer(seen[f"uav/{uid}/env"][-1][1], ENV_SAMPLE32)
        assert len(env) == 1 and int(env["flags"][0]) & 1
        kf = msgpack.unpackb(seen["env/state"][-1][1])
        assert rtlib.schema_errors("env/env_state.schema.json", kf) == []
        ext = msgpack.unpackb(seen[f"uav/{uid}/state_ext"][-1][1])
        assert rtlib.schema_errors("rt/payloads/uav_state_ext.schema.json", ext) == []
        perf = msgpack.unpackb(seen["perf/server"][-1][1])
        assert rtlib.schema_errors("rt/payloads/perf_server.schema.json", perf) == []
        procs = msgpack.unpackb(seen["sys/procs"][-1][1])
        assert rtlib.schema_errors("rt/payloads/sys_procs.schema.json", procs) == []

    asyncio.run(main())


def test_credit_window_blocks_data_but_not_control_then_sends_latest(gw_factory) -> None:
    g = gw_factory("--n", "10", "--window", "3", "--event-period-s", "0.2")

    async def main() -> None:
        async with connect(g.url, subprotocols=["awr.rt.v1"], max_size=None) as ws:
            await handshake(ws)
            await ws.send(json.dumps({"op": "hello", "client": "pytest", "contracts": CONTRACTS_VERSION}))
            await ws.send(json.dumps({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/uav/state", "rate": 60,
                                                                   "mode": "latest"},
                                                                  {"id": 2, "topic": "event", "rate": 0, "mode": "all"}]}))
            frames, ctrl = [], 0
            t_end = time.monotonic() + 1.0
            while time.monotonic() < t_end:
                try:
                    m = await asyncio.wait_for(ws.recv(), 0.2)
                except TimeoutError:
                    continue
                if isinstance(m, bytes) and m[0] == F.OP_BATCH:
                    frames.append(F.decode_batch_header(m))
                elif isinstance(m, bytes) or json.loads(m)["op"] in ("event", "events"):
                    ctrl += 1
            assert len(frames) == 3  # 窗口满：数据帧停发
            assert ctrl >= 5  # 控制面（TIME、事件）照常
            await ws.send(json.dumps({"op": "ack", "frame": 3}))
            m = None
            while True:
                m = await asyncio.wait_for(ws.recv(), 2)
                if isinstance(m, bytes) and m[0] == F.OP_BATCH:
                    break
            h = F.decode_batch_header(m)
            rec = next(F.iter_records(m))
            assert h.frame_seq == 4  # 线上 frame_seq 连续
            assert rec.seq > 3 + 30  # 只发最新值，不补积压

    asyncio.run(main())


def test_protocol_errors(gw_factory) -> None:
    g = gw_factory("--n", "1", "--hello-timeout-s", "0.5")

    async def main() -> None:
        with pytest.raises(InvalidStatus) as ei:
            async with connect(g.url, subprotocols=["other.v1"]):
                pass
        assert ei.value.response.status_code == 400
        assert ei.value.response.headers.get("AWR-Supported-Protocols") == "awr.rt.v1"
        async with connect(g.url, subprotocols=["awr.rt.v1"]) as ws:  # hello 之前的其他 op：error 300
            await handshake(ws)
            await ws.send(json.dumps({"op": "subscribe", "subs": [{"id": 1, "topic": "fleet/roster", "rate": 1,
                                                                   "mode": "latest"}]}))
            m = json.loads(await ws.recv())
            assert m["op"] == "error" and m["code"] == 300 and ops_errors(m) == []
            with pytest.raises(ConnectionClosed) as ce:  # 0.5 s 无 hello：4408
                while True:
                    await asyncio.wait_for(ws.recv(), 3)
            assert ce.value.rcvd.code == 4408
        async with connect(g.url, subprotocols=["awr.rt.v1"]) as ws:  # contracts 主版本不同：311 + 4426
            await handshake(ws)
            await ws.send(json.dumps({"op": "hello", "client": "x", "contracts": "9.0.0"}))
            m = json.loads(await ws.recv())
            assert m["code"] == 311
            with pytest.raises(ConnectionClosed) as ce:
                while True:
                    await asyncio.wait_for(ws.recv(), 3)
            assert ce.value.rcvd.code == 4426
        async with connect(g.url, subprotocols=["awr.rt.v1"]) as ws:  # 非法 topic 314、未知 op 313
            await handshake(ws)
            await ws.send(json.dumps({"op": "hello", "client": "x", "contracts": CONTRACTS_VERSION}))
            await ws.send(json.dumps({"op": "subscribe", "subs": [{"id": 1, "topic": "Bad Topic", "rate": 1, "mode": "latest"}]}))
            await ws.send(json.dumps({"op": "frobnicate"}))
            codes = set()
            while len(codes) < 2:
                m = await asyncio.wait_for(ws.recv(), 3)
                if isinstance(m, str) and json.loads(m)["op"] == "error":
                    codes.add(json.loads(m)["code"])
            assert codes == {313, 314}

    asyncio.run(main())


def _golden_view(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in ("t_rel_ns",)}


@pytest.mark.parametrize("name", ["smoke_n1", "swarm_n200", "swarm_n1000", "time_epoch"])
def test_replay_fixture_matches_golden(gw_factory, name: str) -> None:
    sys.path.insert(0, str(rtlib.ROOT / "tools" / "contracts"))
    import gen_fixtures

    fixture = F.read_awrrt((FIX / "rt" / f"{name}.awrrt").read_bytes())
    want = [r for r in fixture.records if r.dir == F.AWRT_S2C]
    g = gw_factory("--replay", str(FIX / "rt" / f"{name}.awrrt"))

    async def main() -> list[F.AwrtRecord]:
        got: list[F.AwrtRecord] = []
        t0 = time.monotonic_ns()
        async with connect(g.url, subprotocols=["awr.rt.v1"], max_size=None) as ws:
            while len(got) < len(want):
                m = await asyncio.wait_for(ws.recv(), 5)
                kind = F.AWRT_BINARY if isinstance(m, bytes) else F.AWRT_TEXT
                got.append(F.AwrtRecord(F.AWRT_S2C, kind, time.monotonic_ns() - t0, m if isinstance(m, bytes) else m.encode()))
                if len(got) == 2:
                    await ws.send(json.dumps({"op": "hello", "client": "pytest", "contracts": CONTRACTS_VERSION}))
        return got

    got = asyncio.run(main())
    assert [(r.kind, r.payload) for r in got] == [(r.kind, r.payload) for r in want]  # 字节一致
    ours = gen_fixtures.decode_fixture(F.write_awrrt(fixture.header, got, F.AWRT_TIMED, fixture.t0_wall_ns))
    golden = json.loads((FIX / "payloads" / f"{name}.json").read_text(encoding="utf-8"))
    g_s2c = [_golden_view(r) for r in golden["records"] if r["dir"] == F.AWRT_S2C]
    assert [_golden_view(r) for r in ours["records"]] == g_s2c  # 参考客户端解码结果与 golden 一致


def test_record_capture_awrrt(gw_factory, tmp_path: Path) -> None:
    sys.path.insert(0, str(rtlib.ROOT / "tools" / "contracts"))
    import gen_fixtures

    out = tmp_path / "cap.awrrt"
    g = gw_factory("--n", "200", "--record", str(out), "--record-max-s", "5")

    async def main() -> None:
        async with connect(g.url, subprotocols=["awr.rt.v1"], max_size=None) as ws:
            await handshake(ws)
            await ws.send(json.dumps({"op": "hello", "client": "pytest", "contracts": CONTRACTS_VERSION}))
            await ws.send(json.dumps({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/uav/state", "rate": 20,
                                                                   "mode": "latest"}]}))
            await ws.send(json.dumps({"op": "ping", "t": 1}))
            t_end = time.monotonic() + 1.0
            while time.monotonic() < t_end:
                try:
                    m = await asyncio.wait_for(ws.recv(), 0.3)
                except TimeoutError:
                    continue
                if isinstance(m, bytes) and m[0] == F.OP_BATCH:
                    await ws.send(json.dumps({"op": "ack", "frame": F.decode_batch_header(m).frame_seq}))

    asyncio.run(main())
    assert rtlib.wait_until(out.exists, 5)
    cap = F.read_awrrt(out.read_bytes())
    assert cap.header["protocol"] == "awr.rt.v1" and cap.header["n_uav"] == 200 and cap.flags & F.AWRT_TIMED
    dirs = {r.dir for r in cap.records}
    assert dirs == {F.AWRT_S2C, F.AWRT_C2S}
    assert all(b.t_rel_ns >= a.t_rel_ns for a, b in itertools.pairwise(cap.records))
    dec = gen_fixtures.decode_fixture(out.read_bytes())
    assert dec["records"][0]["text"]["op"] == "serverInfo"
    batches = [r for r in dec["records"] if r.get("op") == "BATCH"]
    assert batches and batches[0]["batch"]["records"][0]["n_rows"] == 200
