"""D2 early data source (WP-01; AWR-17 §17.11 last paragraph; D2-MS4 "front end first with FakeSource and fixtures"):
fake_gw `--sandbox` serves the sandbox handshake, every D2 topic, the D2 events and the minimal sandbox REST with contract shapes, and
the D2 .awrrt fixture decodes to its golden."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from functools import cache
from pathlib import Path

import msgpack
import numpy as np
import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "packages" / "contracts"
sys.path.insert(0, str(ROOT / "tools" / "contracts"))
sys.path.insert(0, str(ROOT / "tools" / "fake"))
import contracts_lib as L  # noqa: E402
import d2_synth  # noqa: E402

from awr.contracts import CONTRACTS_VERSION  # noqa: E402
from awr.contracts import frame as F  # noqa: E402
from awr.contracts.layouts import TARGET_LITE32  # noqa: E402

FAKE_GW = ROOT / "tools" / "fake" / "fake_gw.py"


@cache
def _registry():
    from referencing import Registry, Resource

    reg = Registry()
    for p in L.schema_files():
        d = json.loads(p.read_text(encoding="utf-8"))
        reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    return reg


@cache
def _by_title() -> dict[str, str]:
    out = {}
    for p in L.schema_files():
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("title", "").startswith("awr."):
            out[d["title"].split()[0]] = d["$id"]
    return out


def errs(schema_id: str, inst) -> list[str]:
    v = Draft202012Validator({"$ref": schema_id}, registry=_registry())
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:160]}" for e in v.iter_errors(inst)]


OPS = "https://schemas.anet-drone.dev/rt/1/ops.schema.json#/$defs/serverToClient"


@pytest.fixture
def gw():
    env = dict(os.environ)
    env.pop("AWR_SUPERVISOR_PID", None)
    env["PYTHONUNBUFFERED"] = "1"
    p = subprocess.Popen([sys.executable, str(FAKE_GW), "--port", "0", "--n", "3", "--world", "synthcity", "--sandbox", "--event-period-s", "0"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    line = p.stdout.readline()
    assert "READY" in line, line + p.stderr.read()
    sbx = p.stdout.readline()
    assert "sandbox" in sbx, sbx
    yield line.split()[2], sbx.split()[2]
    p.terminate()
    try:
        p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


def _http(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


SB = "https://schemas.anet-drone.dev/sandbox/1/session.schema.json"


def test_sandbox_rest_shapes(gw):
    base = gw[0].replace("ws://", "http://").replace("/api/rt", "/api/sandbox/v1")
    sid = d2_synth.SID
    checks = [("/meta", "GET", None, 200, SB + "#/$defs/meta"), ("/worlds", "GET", None, 200, SB + "#/$defs/worlds"),
              ("/templates?world=synthcity", "GET", None, 200, SB + "#/$defs/templates"),
              ("/sessions", "POST", {"world": "synthcity", "principal_hint": "0" * 32}, 201, SB + "#/$defs/created"),
              (f"/sessions/{sid}", "GET", None, 200, SB), (f"/sessions/{sid}/keepalive", "POST", {}, 200, SB + "#/$defs/keepalive_reply"),
              ("/target_classes", "GET", None, 200, "https://schemas.anet-drone.dev/target/1/class_defaults.schema.json"),
              (f"/sessions/{sid}/targets", "GET", None, 200, "https://schemas.anet-drone.dev/target/1/target.schema.json#/$defs/list"),
              (f"/sessions/{sid}/nests", "GET", None, 200, "https://schemas.anet-drone.dev/tasking/1/nest.schema.json#/$defs/list")]
    for path, method, body, status, schema in checks:
        st, doc = _http(base + path, method, body)
        assert st == status, path
        assert errs(schema, doc) == [], path
    st, doc = _http(base + "/openapi.json")
    assert st == 200 and doc["openapi"].startswith("3.1")


async def _collect(url: str, seconds: float) -> tuple[dict, dict, list, list]:
    from websockets.asyncio.client import connect

    async with connect(url, subprotocols=["awr.rt.v1", "bearer.fake"], max_size=None) as ws:
        info = json.loads(await ws.recv())
        adv = json.loads(await ws.recv())
        F.decode_time(await ws.recv())
        await ws.send(json.dumps({"op": "hello", "client": "pytest", "contracts": CONTRACTS_VERSION, "role": "operator", "tier": "S"}))
        topics = sorted({c["topic"] for c in adv["channels"] if c["kind"] != "event" and not c["topic"].startswith(("uav/uav0002", "uav/uav0003"))
                         and "/state" not in c["topic"].replace("swarm/target/state", "") and "/env" not in c["topic"]})
        subs = [{"id": i + 1, "topic": t, "rate": 10, "mode": "latest"} for i, t in enumerate(topics)] + [{"id": 999, "topic": "event", "rate": 0, "mode": "all"}]
        await ws.send(json.dumps({"op": "subscribe", "subs": subs}))
        frames, texts = [], []
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            try:
                m = await asyncio.wait_for(ws.recv(), 0.5)
            except TimeoutError:
                continue
            if isinstance(m, bytes):
                frames.append(m)
                if m[0] == F.OP_BATCH:
                    await ws.send(json.dumps({"op": "ack", "frame": F.decode_batch_header(m).frame_seq}))
            else:
                texts.append(json.loads(m))
        return info, adv, frames, texts


def test_sandbox_ws_d2_topics_and_events(gw):
    info, adv, frames, texts = asyncio.run(_collect(gw[1], 9.0))
    assert errs(OPS, info) == [] and errs(OPS, adv) == []
    assert info["sandbox"]["sid"] == d2_synth.SID and info["clock"]["tick_hz"] == 100 and "targets" in info["capabilities"]
    ch = {c["id"]: c for c in adv["channels"]}
    seen: dict[str, int] = {}
    titles = _by_title()
    for fr in frames:
        if fr[0] != F.OP_BATCH:
            continue
        for r in F.iter_records(fr):
            c = ch[r.channel_id]
            body = fr[r.payload_off:r.payload_off + r.length]
            seen[c["schemaName"]] = seen.get(c["schemaName"], 0) + 1
            if c["encoding"] == "raw" and c["schemaName"] == "awr.TargetLite32.v1":
                rows = np.frombuffer(body, TARGET_LITE32)
                assert len(rows) == 2 and set(rows["class"]) == {1, 4}
            elif c["encoding"] == "msgpack" and c["schemaName"] in titles:
                obj = msgpack.unpackb(body)
                assert errs(titles[c["schemaName"]], obj) == [], c["topic"]
    for sn in ("awr.TargetLite32.v1", "awr.target.detail.v1", "awr.uav.perception.v1", "awr.uav.payload.v1", "awr.sensor.nav.v1",
               "awr.sensor.radar_tracks.v1", "awr.sensor.acoustic_bearings.v1", "awr.nest.status.v1", "awr.task.status.v1", "awr.relay.stats.v1",
               "awr.sandbox.session_state.v1", "awr.fleet.roster.v1"):
        assert seen.get(sn, 0) > 0, sn
    evs = {m["type"] for m in texts if m.get("op") == "event"} | {e["type"] for m in texts if m.get("op") == "events" for e in m.get("items", [])}
    assert {"perception.capture", "target.discovered", "relay.dispatch", "sandbox.rate"} <= evs, evs
    for m in texts:                                                          # control and event ops follow rt/ops.schema.json
        assert errs(OPS, m) == [], m.get("op")


def test_d2_fixture_matches_golden():
    import gen_fixtures as G

    data = (CONTRACTS / "fixtures" / "d2" / "sandbox_t5.awrrt").read_bytes()
    gold = json.loads((CONTRACTS / "fixtures" / "d2" / "sandbox_t5.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(data).hexdigest() == gold["sha256"]
    dec = G.decode_fixture(data)
    assert json.loads(json.dumps(dec)) == {k: gold[k] for k in ("flags", "t0_wall_ns", "header", "records")}
    titles = _by_title()
    f = F.read_awrrt(data)
    channels = {}
    n_payload = 0
    for r in f.records:
        if r.kind == F.AWRT_TEXT:
            m = json.loads(r.payload)
            if m["op"] == "advertise":
                channels = {c["id"]: c for c in m["channels"]}
            if r.dir == F.AWRT_S2C:
                assert errs(OPS, m) == [], m["op"]
        elif r.payload[0] == F.OP_BATCH:
            for x in F.iter_records(r.payload):
                c = channels[x.channel_id]
                if c["encoding"] == "msgpack" and c["schemaName"] in titles:
                    assert errs(titles[c["schemaName"]], msgpack.unpackb(r.payload[x.payload_off:x.payload_off + x.length])) == [], c["topic"]
                    n_payload += 1
    assert n_payload > 50
