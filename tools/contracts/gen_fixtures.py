#!/usr/bin/env python3
"""Contract fixtures (AWR-16 §13.9 DATA-FR-052, DATA-AC-018; D1-AC-35 contract part).

Writes, deterministically:
  packages/contracts/fixtures/rt/<name>.awrrt        awr.rt.v1 sessions (smoke_n1, swarm_n200, swarm_n1000, time_epoch)
  packages/contracts/fixtures/payloads/<name>.json   reference decoding of each fixture (the golden both ends must match)
  packages/contracts/fixtures/world/<city>/*.json    World Package examples (g03 instances revised to schema v1.0)
  packages/contracts/fixtures/scenario/s1-shenzhen-facade.json   AWR-16 §12.4 format example, extracted from the doc

Payload golden per record: dir, kind, t_rel_ns, len, payload_sha256, and the decoded content: text frames as JSON,
TIME fields, BATCH header plus records. Raw records carry n_rows and the first and last four rows (raw values); msgpack
and json records carry the decoded value when the payload is at most 4 KiB, otherwise decoded_sha256 =
sha256(canonical JSON of the decoded value), canonical as in contracts_lib.canonical_json (JS number formatting).

tools/fake/fake_gw.py replays these files; `fake_gw.py --record` produces new ones in the same format.

Usage: python tools/contracts/gen_fixtures.py [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

import msgpack
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contracts_lib as L

from awr.contracts import frame as F
from awr.contracts import layouts
from awr.contracts.enums import FlightState, Owner, PoseSrc, TimeState

FIX = L.CONTRACTS / "fixtures"
MS = 1_000_000
T0_WALL_NS = 1_790_553_600_000_000_000  # 2026-09-28T00:00:00Z, description only
INLINE_MAX = 4096
FLAGS_OK = 0x01 | 0x02 | 0x04 | 0x10 | 0x20  # armed, in_air, loc_ok, gcs_link, fcu_link
CH = {"roster": 1, "swarm": 2, "full": 3, "event": 4}


# ---------------------------------------------------------------- helpers
def jtext(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


def rec(dir_: int, kind: int, t_ms: float, payload: bytes) -> F.AwrtRecord:
    return F.AwrtRecord(dir_, kind, round(t_ms * MS), payload)


def server_info(world: str, mode: str = "live") -> dict:
    names = ["awr.DroneState64.v1", "awr.SwarmLite32.v1", "awr.EnvSample32.v1", "awr.VelSetpoint16.v1", "awr.SensorPose48.v1",
             "awr.rt.BatchHeader.v1", "awr.rt.RecordHeader.v1", "awr.rt.Time.v1", "awr.rt.ClientDataHeader.v1"]
    return {"op": "serverInfo", "name": "awr-gateway", "protocol": "awr.rt.v1", "sessionId": "fixture-session", "connId": "c1",
            "capabilities": ["time", "credit", "rpc", "events"], "window": 4, "tickHz": L.load_json("rt/topics.json")["tick_hz"], "rateClasses": L.load_json("rt/topics.json")["rate_classes"],
            "world": {"id": world, "frame": "world"}, "run": {"id": "fixture-run", "segment": 0}, "mode": mode,
            "contracts": L.load_all()["version"], "layouts": {n: layouts.SCHEMA_HASH[n] for n in names}}


def advertise(chs: list[tuple[int, str, str, str, str]]) -> dict:
    return {"op": "advertise", "channels": [{"id": i, "topic": t, "kind": k, "encoding": e, "schemaName": s} for i, t, k, e, s in chs]}


def roster(n: int, version: int = 1) -> dict:
    ents = [{"agent_no": a, "id": f"uav{a + 1:04d}", "kind": "uav", "model": "p600", "profile_id": "p600_mid360", "backend": "mock",
             "simulated": True, "producer": "sim-core", "lifecycle": "READY", "sensors": [], "t_world_local": None, "caps_ref": "mock"}
            for a in range(n)]
    return {"roster_version": version, "entries": ents}


def quat_yaw(yaw: float) -> tuple[float, float, float, float]:
    return (0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0))


def full_rows(n: int, k: int, state: int = FlightState.FLYING, sub: int = 0) -> np.ndarray:
    a = np.zeros(n, layouts.DRONE_STATE64)
    idx = np.arange(n)
    a["agent_no"] = idx
    a["flight_state"] = layouts.pack_fs(state, sub)
    a["flags"] = FLAGS_OK
    a["mission_item"] = 0xFFFF
    a["battery_pct"] = np.maximum(100 - (idx % 40) - k // 10, 0)
    a["ctrl"] = layouts.pack_ctrl(Owner.MISSION, False, 2, PoseSrc.TRUTH)
    ang = 2.0 * math.pi * idx / max(n, 1) + 0.05 * k
    r = 50.0 + (idx % 10) * 20.0
    a["pos"] = np.stack([r * np.cos(ang), r * np.sin(ang), 30.0 + (idx % 7) * 5.0 + 0.1 * k], axis=1).astype(np.float32)
    a["vel"] = np.stack([-r * np.sin(ang) * 0.05, r * np.cos(ang) * 0.05, np.full(n, 0.5)], axis=1).astype(np.float32)
    yaw = ang + math.pi / 2.0
    a["q"] = np.stack([np.zeros(n), np.zeros(n), np.sin(yaw / 2.0), np.cos(yaw / 2.0)], axis=1).astype(np.float32)
    a["omega"] = np.stack([np.zeros(n), np.zeros(n), np.full(n, 0.05)], axis=1).astype(np.float32)
    a["dt_us"] = -(idx % 8) * 125
    return a


def batch(flags: int, epoch: int, seq: int, t_sim_ns: int, records: list[bytes]) -> bytes:
    return F.encode_batch(flags, epoch, seq, t_sim_ns, records)


# ---------------------------------------------------------------- sessions
def handshake(world: str, chs, subs, mode: str = "live") -> list[F.AwrtRecord]:
    out = [rec(F.AWRT_S2C, F.AWRT_TEXT, 0.0, jtext(server_info(world, mode))),
           rec(F.AWRT_C2S, F.AWRT_TEXT, 1.0, jtext({"op": "hello", "client": "awr-web/fixture", "contracts": L.load_all()["version"], "role": "operator", "tier": "A"})),
           rec(F.AWRT_S2C, F.AWRT_TEXT, 2.0, jtext(advertise(chs))),
           rec(F.AWRT_C2S, F.AWRT_TEXT, 3.0, jtext({"op": "subscribe", "subs": [{"id": i + 1, "topic": t, "rate": r, "mode": "latest"} for i, (t, r) in enumerate(subs)]}))]
    for i, (t, r) in enumerate(subs):
        chan = next(c[0] for c in chs if c[1] == t)
        out.append(rec(F.AWRT_S2C, F.AWRT_TEXT, 4.0, jtext({"op": "subscribed", "id": i + 1, "topic": t, "channels": [chan], "rate": r, "mode": "latest"})))
    return out


def smoke_n1() -> tuple[dict, list[F.AwrtRecord]]:
    chs = [(CH["roster"], "fleet/roster", "roster", "msgpack", "awr.fleet.roster.v1"), (CH["swarm"], "swarm/uav/state", "state", "raw", "awr.SwarmLite32.v1"),
           (CH["full"], "uav/uav0001/state", "state", "raw", "awr.DroneState64.v1"), (CH["event"], "event", "event", "json", "awr.event.v1")]
    recs = handshake("shenzhen", chs, [("fleet/roster", 10), ("swarm/uav/state", 20), ("uav/uav0001/state", 30), ("event", 0)])
    recs.append(rec(F.AWRT_S2C, F.AWRT_BINARY, 5.0, F.encode_time(TimeState.LIVE, 1, 1.0, 0, T0_WALL_NS)))
    for k in range(20):
        t_sim = k * 50 * MS
        full = full_rows(1, k)
        rs = []
        if k == 0:
            rs.append(F.encode_record(CH["roster"], F.ENC_MSGPACK, F.RF_KEYFRAME, 1, 0, msgpack.packb(roster(1))))
        rs.append(F.encode_record(CH["swarm"], F.ENC_RAW, F.RF_KEYFRAME if k == 0 else 0, k + 1, 0, layouts.lite_from_full(full).tobytes()))
        rs.append(F.encode_record(CH["full"], F.ENC_RAW, F.RF_KEYFRAME if k == 0 else 0, k + 1, -250, full.tobytes()))
        recs.append(rec(F.AWRT_S2C, F.AWRT_BINARY, 10.0 + 50.0 * k, batch(F.BATCH_SNAPSHOT if k == 0 else 0, 1, k + 1, t_sim, rs)))
        if k == 12:  # reliable events travel as `event` text ops (AWR-17 §6.12)
            ev = {"op": "event", "seq": 1, "t_sim_ns": t_sim, "t_wall_ns": str(T0_WALL_NS + t_sim), "type": "mission.item_reached", "level": 0,
                  "producer": "sim-core", "uav": "uav0001", "cid": None, "data": {"mid": "m-fixture", "item": 3}}
            recs.append(rec(F.AWRT_S2C, F.AWRT_TEXT, 10.0 + 50.0 * k + 0.5, jtext(ev)))
        if k % 10 == 9:
            recs.append(rec(F.AWRT_S2C, F.AWRT_BINARY, 10.0 + 50.0 * k + 1.0, F.encode_time(TimeState.LIVE, 1, 1.0, t_sim, T0_WALL_NS + t_sim)))
    recs.append(rec(F.AWRT_C2S, F.AWRT_TEXT, 1100.0, jtext({"op": "ping", "t": 1100})))
    recs.append(rec(F.AWRT_S2C, F.AWRT_TEXT, 1101.0, jtext({"op": "pong", "t": 1100, "server_ns": 1_100 * MS, "sim_ns": 950 * MS, "epoch": 1})))
    hdr = {"n_uav": 1, "world_id": "shenzhen", "description": "Handshake, roster, 20 BATCH frames at 20 Hz with Lite32 and Full64 for one vehicle, "
           "TIME LIVE, one reliable event op, ping/pong."}
    return hdr, recs


def swarm_n(n: int, frames: int) -> tuple[dict, list[F.AwrtRecord]]:
    chs = [(CH["roster"], "fleet/roster", "roster", "msgpack", "awr.fleet.roster.v1"), (CH["swarm"], "swarm/uav/state", "state", "raw", "awr.SwarmLite32.v1")]
    recs = handshake("shenzhen", chs, [("fleet/roster", 10), ("swarm/uav/state", 20)])
    recs.append(rec(F.AWRT_S2C, F.AWRT_BINARY, 5.0, F.encode_time(TimeState.PLAYING, 3, 1.0, 1_000 * MS, T0_WALL_NS)))
    for k in range(frames):
        t_sim = 1_000 * MS + k * 50 * MS
        rs = []
        if k == 0:
            rs.append(F.encode_record(CH["roster"], F.ENC_MSGPACK, F.RF_KEYFRAME, 1, 0, msgpack.packb(roster(n))))
        rs.append(F.encode_record(CH["swarm"], F.ENC_RAW, F.RF_KEYFRAME if k == 0 else 0, k + 1, 0, layouts.lite_from_full(full_rows(n, k)).tobytes()))
        recs.append(rec(F.AWRT_S2C, F.AWRT_BINARY, 10.0 + 50.0 * k, batch(F.BATCH_SNAPSHOT if k == 0 else 0, 3, k + 1, t_sim, rs)))
    return {"n_uav": n, "world_id": "shenzhen", "description": f"Swarm of {n}: roster keyframe then {frames} Lite32 BATCH frames at 20 Hz."}, recs


def time_epoch() -> tuple[dict, list[F.AwrtRecord]]:
    """TIME and epoch semantics for M12 (M12-FR-006, M12-AC-006, M12-AC-022)."""
    chs = [(CH["roster"], "fleet/roster", "roster", "msgpack", "awr.fleet.roster.v1"), (CH["swarm"], "swarm/uav/state", "state", "raw", "awr.SwarmLite32.v1")]
    recs = handshake("shenzhen", chs, [("fleet/roster", 10), ("swarm/uav/state", 20)])
    t = [10.0]
    seq = [0]

    def at() -> float:
        t[0] += 10.0
        return t[0]

    def frame(epoch: int, t_sim: int, flags: int = 0, rflags: int = 0, n: int = 2, with_roster: bool = False, k: int = 0) -> None:
        seq[0] += 1
        rs = []
        if with_roster:
            rs.append(F.encode_record(CH["roster"], F.ENC_MSGPACK, F.RF_KEYFRAME, seq[0], 0, msgpack.packb(roster(n))))
        rs.append(F.encode_record(CH["swarm"], F.ENC_RAW, rflags, seq[0], 0, layouts.lite_from_full(full_rows(n, k)).tobytes()))
        recs.append(rec(F.AWRT_S2C, F.AWRT_BINARY, at(), batch(flags, epoch, seq[0], t_sim, rs)))

    def time(state: int, epoch: int, rate: float, t_sim: int, replay: bool = False, reserved: int = 0) -> None:
        b = bytearray(F.encode_time(state, epoch, rate, t_sim, T0_WALL_NS + t_sim, replay))
        b[1] |= reserved
        recs.append(rec(F.AWRT_S2C, F.AWRT_BINARY, at(), bytes(b)))

    # step 1: epoch 1, every TimeState in enum order, each followed by one BATCH frame
    ts = 0
    frame(1, 0, F.BATCH_SNAPSHOT, F.RF_KEYFRAME, with_roster=True)
    for st in TimeState:
        rate = 0.0 if st in (TimeState.STOPPED, TimeState.PAUSED, TimeState.ENDED, TimeState.FAILED) else (2.0 if st == TimeState.PLAYING else 1.0)
        time(st, 1, rate, ts)
        ts += 20 * MS
        frame(1, ts, k=int(st))
    # step 2: reserved bits 4..6 set on a TIME byte (clients mask them)
    time(TimeState.PLAYING, 1, 1.0, ts, reserved=0x70)
    # step 3: seek -> epoch 2; a stale epoch-1 BATCH arrives after the new TIME and must be dropped
    time(TimeState.BUFFERING, 2, 0.0, 60_000 * MS)
    frame(1, ts + 20 * MS)
    frame(2, 60_000 * MS, F.BATCH_SNAPSHOT, F.RF_KEYFRAME | F.RF_RESET, with_roster=True)
    time(TimeState.PLAYING, 2, 1.0, 60_000 * MS)
    frame(2, 60_020 * MS)
    # step 4: scenario reset -> epoch 3 at t_sim 0
    time(TimeState.STOPPED, 3, 0.0, 0)
    frame(3, 0, F.BATCH_SNAPSHOT, F.RF_KEYFRAME | F.RF_RESET, with_roster=True)
    # step 5: checkpoint restore -> epoch 4, sim-core restarted (RESTARTING, then PLAYING)
    time(TimeState.RESTARTING, 3, 0.0, 0)
    time(TimeState.PLAYING, 4, 1.0, 30_000 * MS)
    frame(4, 30_000 * MS, F.BATCH_SNAPSHOT, F.RF_KEYFRAME | F.RF_RESET, with_roster=True)
    # step 6: replay session: TIME bit7 REPLAY, BATCH replay flag, epoch 5
    time(TimeState.PLAYING, 5, 4.0, 10_000 * MS, replay=True)
    frame(5, 10_000 * MS, F.BATCH_SNAPSHOT | F.BATCH_REPLAY, F.RF_KEYFRAME | F.RF_RESET, with_roster=True)
    frame(5, 10_080 * MS, F.BATCH_REPLAY)
    # step 7: epoch wrap (u16): 65535 -> 0 is a change, compared by equality only
    time(TimeState.LIVE, 65535, 1.0, 0)
    time(TimeState.LIVE, 0, 1.0, 20 * MS)
    frame(0, 20 * MS, F.BATCH_SNAPSHOT, F.RF_KEYFRAME | F.RF_RESET)
    hdr = {"n_uav": 2, "world_id": "shenzhen",
           "description": "TIME and epoch: all 10 TimeState values in order (epoch 1); reserved TIME bits 4-6 set; seek to epoch 2 with a stale "
                          "epoch-1 BATCH after the new TIME; scenario reset (epoch 3); checkpoint restore via RESTARTING (epoch 4); replay "
                          "with TIME bit7 and BATCH replay flag (epoch 5); u16 epoch wrap 65535 -> 0."}
    return hdr, recs


SESSIONS = {"smoke_n1": smoke_n1, "swarm_n200": lambda: swarm_n(200, 5), "swarm_n1000": lambda: swarm_n(1000, 3), "time_epoch": time_epoch}


# ---------------------------------------------------------------- reference decoder (the payload golden)
def jv(x):
    if isinstance(x, float):
        if math.isnan(x):
            return "NaN"
        if x == 0 and math.copysign(1.0, x) < 0:
            return "-0"
    return x


def rows_json(a: np.ndarray, sn: str) -> list[dict]:
    sch = next(s for s in L.load_all()["layouts"]["schemas"] if s["schemaName"] == sn)
    out = []
    for r in a:
        d = {}
        for f in sch["fields"]:
            v = r[f["n"]]
            cast = float if f["t"].startswith("f") else int
            d[f["n"]] = [jv(cast(x)) for x in v] if f.get("c", 1) > 1 else jv(cast(v))
        out.append(d)
    return out


def decode_payload(ch: dict | None, enc: int, payload: bytes) -> dict:
    out: dict = {}
    if ch is None:
        return {"unknown_channel": True}
    if enc == F.ENC_RAW and ch["schemaName"] in layouts.DTYPES:
        dt = layouts.DTYPES[ch["schemaName"]]
        a = np.frombuffer(payload, dt)
        out["n_rows"] = int(a.size)
        out["rows_head"] = rows_json(a[:4], ch["schemaName"])
        out["rows_tail"] = rows_json(a[-4:], ch["schemaName"]) if a.size > 4 else []
    elif enc in (F.ENC_MSGPACK, F.ENC_JSON):
        obj = msgpack.unpackb(payload) if enc == F.ENC_MSGPACK else json.loads(payload)
        if len(payload) <= INLINE_MAX:
            out["decoded"] = obj
        else:
            out["decoded_sha256"] = hashlib.sha256(L.canonical_json(obj).encode()).hexdigest()
    return out


def decode_fixture(data: bytes) -> dict:
    f = F.read_awrrt(data)
    channels: dict[int, dict] = {}
    recs = []
    for r in f.records:
        d = {"dir": r.dir, "kind": r.kind, "t_rel_ns": r.t_rel_ns, "len": len(r.payload), "payload_sha256": hashlib.sha256(r.payload).hexdigest()}
        if r.kind == F.AWRT_TEXT:
            msg = json.loads(r.payload)
            d["text"] = msg
            if msg.get("op") == "advertise":
                channels.update({c["id"]: c for c in msg["channels"]})
        else:
            op = r.payload[0]
            if op == F.OP_TIME:
                tf = F.decode_time(r.payload)
                d["op"] = "TIME"
                d["time"] = {"state": tf.state, "replay": tf.replay, "epoch": tf.epoch, "rate": tf.rate, "t_sim_ns": tf.t_sim_ns, "t_srv_ns": tf.t_srv_ns,
                             "state_byte": r.payload[1], "advances": F.time_advances(tf)}
            elif op == F.OP_BATCH:
                h = F.decode_batch_header(r.payload)
                d["op"] = "BATCH"
                rr = []
                for x in F.iter_records(r.payload):
                    body = r.payload[x.payload_off:x.payload_off + x.length]
                    rr.append({"channel_id": x.channel_id, "encoding": x.encoding, "rflags": x.rflags, "length": x.length, "seq": x.seq, "dt_us": x.dt_us,
                               **decode_payload(channels.get(x.channel_id), x.encoding, body)})
                d["batch"] = {"flags": h.flags, "epoch": h.epoch, "frame_seq": h.frame_seq, "t_sim_ns": h.t_sim_ns, "records": rr}
            else:
                d["op"] = f"0x{op:02x}"
        recs.append(d)
    return {"flags": f.flags, "t0_wall_ns": f.t0_wall_ns, "header": f.header, "records": recs}


# ---------------------------------------------------------------- world and scenario examples
def world_examples() -> dict[Path, bytes]:
    src = L.ROOT / ".cache" / "research" / "g03" / "instances"
    files: dict[Path, bytes] = {}
    if not src.exists():
        return files
    for city in ("shenzhen", "sanfrancisco"):
        def load(rel: str, city: str = city) -> dict:
            d = json.loads((src / city / rel).read_text(encoding="utf-8"))
            d.pop("$schema", None)
            return d

        coord = load("coordinate.json")
        coord["conventions"]["px4Boundary"] = "NED/FRD at PX4 adapter boundary only"
        coord_b = (json.dumps(coord, indent=1, ensure_ascii=False) + "\n").encode()
        world = load("world.json")
        world["coordinate"]["sha256"] = hashlib.sha256(coord_b).hexdigest()
        ds = world.get("dataset") or {}
        ds.setdefault("version", "2022")
        ds.setdefault("license", "UrbanScene3D terms of use (research only, no redistribution)")
        ds.setdefault("sourceFiles", [{"name": f"{city}.ply", "bytes": 0, "sha256": "0" * 64}])
        world["dataset"] = ds
        out = FIX / "world" / city
        files[out / "coordinate.json"] = coord_b
        files[out / "world.json"] = (json.dumps(world, indent=1, ensure_ascii=False) + "\n").encode()
        for rel, name in (("visual/pointcloud/metadata.json", "pointcloud-metadata.json"), ("geometry/terrain/dtm_10m.json", "dtm_10m.json")):
            files[out / name] = (json.dumps(load(rel), indent=1, ensure_ascii=False) + "\n").encode()
    return files


def scenario_example() -> dict[Path, bytes]:
    doc = (L.ROOT / "docs" / "16-World数据规范.md").read_text(encoding="utf-8")
    sec = doc.split("### 12.4", 1)[1].split("### 12.5", 1)[0]
    m = re.search(r"```json\n(.*?)\n```", sec, re.S)
    if not m:
        return {}
    obj = json.loads(m.group(1))
    return {FIX / "scenario" / "s1-shenzhen-facade.json": (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode()}


def build() -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    lid = L.load_all()["layout_id"]
    for name, fn in SESSIONS.items():
        hdr, recs = fn()
        header = {"protocol": "awr.rt.v1", "layout_id": f"0x{lid:08X}", "contracts_version": L.load_all()["version"], "created_by": "tools/contracts/gen_fixtures.py", **hdr}
        data = F.write_awrrt(header, recs, F.AWRT_TIMED, T0_WALL_NS)
        files[FIX / "rt" / f"{name}.awrrt"] = data
        golden = {"schema": "awr.golden.awrrt.v1", "fixture": f"rt/{name}.awrrt", "sha256": hashlib.sha256(data).hexdigest(), **decode_fixture(data)}
        files[FIX / "payloads" / f"{name}.json"] = (json.dumps(golden, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    files.update(world_examples())
    files.update(scenario_example())
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="contract fixtures (.awrrt, payload golden, world and scenario examples)")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    diffs = 0
    for p, data in sorted(build().items()):
        cur = p.read_bytes() if p.exists() else None
        if cur != data:
            diffs += 1
            if args.check:
                print(f"out of date: {p.relative_to(L.ROOT)}")
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                print(f"wrote {p.relative_to(L.ROOT)} ({len(data)} B)")
    if args.check and diffs:
        print("gen_fixtures.py --check: fixtures out of date; run python tools/contracts/gen_fixtures.py", file=sys.stderr)
        return 1
    print(f"gen_fixtures.py: {'up to date' if not diffs else f'{diffs} file(s) updated'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
