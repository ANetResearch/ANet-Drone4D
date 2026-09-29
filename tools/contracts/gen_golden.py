#!/usr/bin/env python3
"""Golden data for layouts, TIME, BATCH and admission lookups (AWR-17 §10.6 rules 3-5, 7; AWR-18 §8.3).

Python (numpy dtypes and awr.contracts.frame) is the reference; TS and Python tests read the same files:
    packages/contracts/golden/layouts/<schemaName>.bin   N random records per layout (seeded)
    packages/contracts/golden/layouts/values.json        decoded raw values of the first rows of every .bin
    packages/contracts/golden/layouts/vectors.json       g04 §5.5 golden byte vectors
    packages/contracts/golden/rt/time.json               TIME frames: every TimeState x bit7 x reserved bits
    packages/contracts/golden/rt/batch.json              BATCH frames incl. unknown encodings and reserved bits
    packages/contracts/golden/commands/admission.json    admission symbol for every (op, FlightState, FAILSAFE)

Usage: python tools/contracts/gen_golden.py [--check]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contracts_lib as L

from awr.contracts import commands, enums, frame, layouts

GOLDEN = L.CONTRACTS / "golden"
SEED = 20260928
MAIN = {"awr.DroneState64.v1", "awr.SwarmLite32.v1", "awr.EnvSample32.v1", "awr.VelSetpoint16.v1", "awr.SensorPose48.v1"}
N_MAIN, N_OTHER, N_VALUES = 10000, 256, 64
LIMITS = {"u8": (0, 255), "i8": (-128, 127), "u16": (0, 65535), "i16": (-32768, 32767), "u32": (0, 2**32 - 1), "i32": (-(2**31), 2**31 - 1),
          "u64": (0, 2**53 - 1), "i64": (-(2**53) + 1, 2**53 - 1)}


def jnum(x):
    """JSON-safe raw value (floats of float32 fields keep their exact double value)."""
    if isinstance(x, float):
        if math.isnan(x):
            return "NaN"
        if math.isinf(x):
            return "Infinity" if x > 0 else "-Infinity"
        if x == 0 and math.copysign(1.0, x) < 0:
            return "-0"
        return x
    return x


def random_records(sch: dict, n: int, rng: np.random.Generator) -> np.ndarray:
    dt = layouts.DTYPES[sch["schemaName"]]
    a = np.zeros(n, dt)
    for f in sch["fields"]:
        c = f.get("c", 1)
        shape = (n, c) if c > 1 else (n,)
        t = f["t"]
        if "const" in f:
            a[f["n"]] = f["const"]
            continue
        if t in LIMITS:
            lo, hi = LIMITS[t]
            if t in ("u64", "i64"):
                v = rng.integers(lo, hi, size=shape, dtype=np.int64, endpoint=True)
            else:
                v = rng.integers(lo, hi, size=shape, dtype=np.int64, endpoint=True)
            a[f["n"]] = v
        else:
            v = (rng.uniform(-1.0, 1.0, size=shape) * 10.0 ** rng.integers(-3, 5, size=shape)).astype(np.float32)
            a[f["n"]] = v
    # special float rows: canonical quiet NaN, +-0, +-inf, float32 extremes
    specials = [np.float32("nan"), np.float32(-0.0), np.float32(np.inf), np.float32(-np.inf), np.float32(3.4028235e38), np.float32(1.4e-45)]
    ffields = [f for f in sch["fields"] if f["t"] in ("f32", "f64") and "const" not in f]
    for i, sv in enumerate(specials):
        if i >= n:
            break
        for f in ffields:
            a[f["n"]][i] = sv
    return a


def record_values(rec: np.void, sch: dict) -> dict:
    out = {}
    for f in sch["fields"]:
        v = rec[f["n"]]
        if f.get("c", 1) > 1:
            out[f["n"]] = [jnum(float(x) if f["t"].startswith("f") else int(x)) for x in v]
        else:
            out[f["n"]] = jnum(float(v) if f["t"].startswith("f") else int(v))
    return out


def gen_layouts() -> dict[Path, bytes]:
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([SEED, 1])))
    files: dict[Path, bytes] = {}
    values = {"schema": "awr.golden.layouts.v1", "generator": "tools/contracts/gen_golden.py", "seed": SEED, "layout_id": layouts.LAYOUT_ID,
              "schema_hash": layouts.SCHEMA_HASH, "rows": {}}
    for sch in L.load_json("rt/layouts.json")["schemas"]:
        sn = sch["schemaName"]
        n = N_MAIN if sn in MAIN else N_OTHER
        a = random_records(sch, n, rng)
        files[GOLDEN / "layouts" / f"{sn}.bin"] = a.tobytes()
        values["rows"][sn] = {"count": n, "size": sch["size"], "values": [record_values(a[i], sch) for i in range(min(N_VALUES, n))]}
    files[GOLDEN / "layouts" / "values.json"] = (json.dumps(values, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    # g04 §5.5 golden vectors (AWR-17 §6.5)
    FS, Own, Nat, Pose, F = enums.FlightState, enums.Owner, enums.Native, enums.PoseSrc, enums.FlightFlags
    vec = [
        ("sih_goto_arrived_operator_truth", FS.FLYING, "HOVER", F.ARMED | F.IN_AIR | F.LOC_OK | F.GCS_LINK | F.FCU_LINK, (Own.OPERATOR, False, Nat.COMMAND, Pose.TRUTH), 0x05, 0x37, 0x61),
        ("sih_link_loss_failsafe_hold", FS.HOLD, "AUTOPILOT", F.ARMED | F.IN_AIR | F.LOC_OK | F.FAILSAFE | F.FCU_LINK | F.ALERT, (Own.SAFETY, False, Nat.COMMAND, Pose.ESTIMATE), 0xA7, 0xAF, 0x25),
        ("prometheus_odom_invalid_quick_land", FS.ELAND, "NO_POSITION", F.ARMED | F.IN_AIR | F.FAILSAFE | F.GCS_LINK | F.FCU_LINK | F.ALERT, (Own.SAFETY, False, Nat.LAND, Pose.ESTIMATE), 0x2A, 0xBB, 0x35),
        ("mock_safety_stop", FS.HOLD, "SAFETY_STOP", F.ARMED | F.IN_AIR | F.LOC_OK | F.GCS_LINK | F.FCU_LINK, (Own.SAFETY, True, Nat.COMMAND, Pose.TRUTH), 0x07, 0x37, 0x6D),
        ("lifecycle_lost", FS.UNKNOWN, "LINK_LOST", None, None, 0x40, None, None),
    ]
    out = []
    for name, fs, subn, flags, ctrl, efs, eflags, ectrl in vec:
        b_fs = layouts.pack_fs(fs, enums.sub_value(fs, subn))
        assert b_fs == efs, name
        item = {"name": name, "flight_state": b_fs, "state": int(fs), "sub": enums.sub_value(fs, subn), "sub_name": subn}
        if flags is not None:
            assert int(flags) == eflags and layouts.pack_ctrl(*ctrl) == ectrl, name
            rec = np.zeros(1, layouts.DRONE_STATE64)
            rec["agent_no"], rec["flight_state"], rec["flags"], rec["ctrl"] = 7, b_fs, int(flags), layouts.pack_ctrl(*ctrl)
            rec["mission_item"], rec["battery_pct"] = 0xFFFF, 255
            rec["pos"], rec["vel"], rec["q"], rec["omega"] = [120.0, 40.0, 60.0], [2.0, -1.0, 0.5], [0.0, 0.0, 0.2588190451, 0.9659258263], [0.0, 0.0, 0.01]
            lite = layouts.lite_from_full(rec)
            item.update({"flags": int(flags), "ctrl": layouts.pack_ctrl(*ctrl), "owner": int(ctrl[0]), "locked": bool(ctrl[1]), "native": int(ctrl[2]),
                         "pose_src": int(ctrl[3]), "full64_hex": rec.tobytes().hex(), "lite32_hex": lite.tobytes().hex()})
        out.append(item)
    files[GOLDEN / "layouts" / "vectors.json"] = (json.dumps({"schema": "awr.golden.state_vectors.v1", "source": "g04 §5.5; AWR-17 §6.5", "vectors": out}, indent=1) + "\n").encode()
    return files


def gen_time() -> bytes:
    cases = []
    epochs = [0, 1, 65535]
    rates = [1.0, 0.25, 10.0]
    times = [(0, 0), (4_000_000, 1_234_567), (123_456_789_012, 987_654_321_000), (2**53 - 1, 2**53 - 2)]
    k = 0
    for st in range(10):
        for replay in (False, True):
            for reserved in (0, 0x70):
                ep, rate, (ts, tv) = epochs[k % 3], rates[k % 3], times[k % 4]
                k += 1
                b = bytearray(frame.encode_time(st, ep, rate, ts, tv, replay=replay))
                b[1] |= reserved  # reserved bits 4-6 must be ignored by readers
                tf = frame.decode_time(bytes(b))
                cases.append({"hex": bytes(b).hex(), "state": tf.state, "state_name": enums.TimeState(st).name, "replay": tf.replay, "reserved_bits": reserved,
                              "epoch": tf.epoch, "rate": tf.rate, "t_sim_ns": tf.t_sim_ns, "t_srv_ns": tf.t_srv_ns, "advances": frame.time_advances(tf)})
    doc = {"schema": "awr.golden.time.v1", "generator": "tools/contracts/gen_golden.py",
           "rule": "state = byte1 & 0x0F; replay = byte1 & 0x80; SimClockView advances only for PLAYING (1) and LIVE (9)", "cases": cases}
    return (json.dumps(doc, indent=1) + "\n").encode()


def gen_batch() -> bytes:
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([SEED, 2])))
    full = np.zeros(2, layouts.DRONE_STATE64)
    full["agent_no"] = [0, 1]
    full["flight_state"] = [layouts.pack_fs(enums.FlightState.FLYING, 1), layouts.pack_fs(enums.FlightState.HOLD, 0)]
    full["pos"] = rng.uniform(-500, 500, (2, 3)).astype(np.float32)
    full["q"] = [[0, 0, 0, 1], [0, 0, 0.70710677, 0.70710677]]
    lite = layouts.lite_from_full(full)
    roster = json.dumps({"roster_version": 3, "entries": []}).encode()
    frames = []
    f1 = frame.encode_batch(frame.BATCH_SNAPSHOT, 7, 1, 123_400_000_000, [
        frame.encode_record(1, frame.ENC_JSON, frame.RF_KEYFRAME, 1, 0, roster),
        frame.encode_record(2, frame.ENC_RAW, 0, 42, 0, lite.tobytes()),
        frame.encode_record(10, frame.ENC_RAW, frame.RF_RESET, 43, -4000, full[:1].tobytes()),
    ])
    f2 = frame.encode_batch(frame.BATCH_REPLAY | frame.BATCH_GAP | 0xF0, 65535, 2, 2**53 - 1, [
        frame.encode_record(3, 7, 0xF0, 5, 0, b"\x01\x02\x03\x04\x05"),  # unknown encoding + reserved rflags
        frame.encode_record(4, frame.ENC_BLOB, 0, 6, 12, b""),            # empty payload
        frame.encode_record(65535, frame.ENC_MSGPACK, frame.RF_KEYFRAME, 0xFFFFFFFF, 2**31 - 1, b"\x81\xa1v\x01"),
    ])
    for fb in (f1, f2):
        h = frame.decode_batch_header(fb)
        recs = [dict(r._asdict(), payload_hex=fb[r.payload_off:r.payload_off + r.length].hex()) for r in frame.iter_records(fb)]
        frames.append({"hex": fb.hex(), "flags": h.flags, "epoch": h.epoch, "frame_seq": h.frame_seq, "t_sim_ns": h.t_sim_ns, "records": recs})
    doc = {"schema": "awr.golden.batch.v1", "generator": "tools/contracts/gen_golden.py",
           "rule": "16 B frame header, 16 B record headers, payload padded to 8; payload_off % 8 == 0", "frames": frames}
    return (json.dumps(doc, indent=1) + "\n").encode()


def gen_admission() -> bytes:
    rows = []
    for s in commands.SERVICES:
        if s.admission is None:
            continue
        for fs in enums.FlightState:
            for failsafe in (0, 1):
                sym, cond = commands.admit_symbol(s.op, fs, enums.FlightFlags.FAILSAFE if failsafe else 0)
                rows.append([s.op, int(fs), failsafe, sym, cond])
    doc = {"schema": "awr.golden.admission.v1", "generator": "tools/contracts/gen_golden.py", "columns": ["op", "flight_state", "failsafe", "symbol", "condition"], "rows": rows}
    return (json.dumps(doc) + "\n").encode()


def build() -> dict[Path, bytes]:
    files = gen_layouts()
    files[GOLDEN / "rt" / "time.json"] = gen_time()
    files[GOLDEN / "rt" / "batch.json"] = gen_batch()
    files[GOLDEN / "commands" / "admission.json"] = gen_admission()
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="layouts / TIME / BATCH / admission golden generator")
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
                print(f"wrote {p.relative_to(L.ROOT)}")
    if args.check and diffs:
        print("gen_golden.py --check: golden differs from the Python reference; run python tools/contracts/gen_golden.py", file=sys.stderr)
        return 1
    print(f"gen_golden.py: {'up to date' if not diffs else f'{diffs} file(s) updated'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
