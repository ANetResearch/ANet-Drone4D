"""awr.rt.v1 TIME and BATCH codecs (AWR-17 §6.4, §6.10; M12-FR-006; D1-AC-13)."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import load

from awr.contracts import frame as F
from awr.contracts.enums import TimeState, time_advances, time_is_replay, time_state


def test_time_golden():
    for c in load("golden/rt/time.json")["cases"]:
        b = bytes.fromhex(c["hex"])
        assert len(b) == F.TIME.size == 24
        tf = F.decode_time(b)
        assert (tf.state, tf.replay, tf.epoch, tf.rate, tf.t_sim_ns, tf.t_srv_ns) == (c["state"], c["replay"], c["epoch"], c["rate"], c["t_sim_ns"], c["t_srv_ns"])
        assert F.time_advances(tf) == c["advances"] == (c["state"] in (TimeState.PLAYING, TimeState.LIVE))
        assert time_state(b[1]) == c["state"] and time_is_replay(b[1]) == c["replay"] and time_advances(b[1]) == c["advances"]
        if c["reserved_bits"] == 0:
            assert F.encode_time(c["state"], c["epoch"], c["rate"], c["t_sim_ns"], c["t_srv_ns"], c["replay"]) == b


def test_time_covers_all_states_and_bits():
    cases = load("golden/rt/time.json")["cases"]
    assert {c["state"] for c in cases} == set(range(10))
    assert {c["replay"] for c in cases} == {False, True}
    assert {c["reserved_bits"] for c in cases} == {0, 0x70}


def test_time_rejects_other_opcodes():
    b = bytearray(F.encode_time(TimeState.LIVE, 1, 1.0, 0, 0))
    b[0] = F.OP_BATCH
    with pytest.raises(ValueError):
        F.decode_time(bytes(b))


def test_batch_golden():
    for fr in load("golden/rt/batch.json")["frames"]:
        b = bytes.fromhex(fr["hex"])
        h = F.decode_batch_header(b)
        assert (h.flags, h.epoch, h.frame_seq, h.t_sim_ns) == (fr["flags"], fr["epoch"], fr["frame_seq"], fr["t_sim_ns"])
        recs = list(F.iter_records(b))
        assert len(recs) == len(fr["records"])
        for r, e in zip(recs, fr["records"], strict=True):
            assert r.payload_off % 8 == 0
            assert r._asdict() == {k: v for k, v in e.items() if k != "payload_hex"}
            assert b[r.payload_off:r.payload_off + r.length].hex() == e["payload_hex"]
        re = F.encode_batch(h.flags, h.epoch, h.frame_seq, h.t_sim_ns,
                            [F.encode_record(r.channel_id, r.encoding, r.rflags, r.seq, r.dt_us, b[r.payload_off:r.payload_off + r.length]) for r in recs])
        assert re == b


def test_batch_fuzz_10k_frames():
    """Random and truncated frames: the walker either yields in-bounds records or raises ValueError; never reads out of range."""
    rng = np.random.default_rng(20260928)
    ok = bad = 0
    for i in range(10_000):
        n_rec = int(rng.integers(0, 5))
        recs = [F.encode_record(int(rng.integers(0, 65536)), int(rng.integers(0, 8)), int(rng.integers(0, 256)), int(rng.integers(0, 2**32)),
                                int(rng.integers(-(2**31), 2**31)), rng.bytes(int(rng.integers(0, 200)))) for _ in range(n_rec)]
        b = F.encode_batch(int(rng.integers(0, 256)), int(rng.integers(0, 65536)), i, int(rng.integers(-(2**62), 2**62)), recs)
        if i % 3 == 1:
            b = b[:int(rng.integers(16, len(b) + 1))]
        elif i % 3 == 2 and len(b) > 16:
            b = bytearray(b)
            b[int(rng.integers(16, len(b)))] ^= 0xFF
            b = bytes(b)
        try:
            for r in F.iter_records(b):
                assert 0 <= r.payload_off <= len(b) and r.payload_off + r.length <= len(b)
            ok += 1
        except ValueError:
            bad += 1
    assert ok > 3000 and bad > 100


def test_client_data_and_setpoint_sizes():
    assert F.CLIENT_DATA_HDR.size + F.VEL_SETPOINT16.size == 32
    assert F.SETPOINT_BUS.size + F.VEL_SETPOINT16.size == F.SETPOINT32.size == 32
    b = F.CLIENT_DATA_HDR.pack(F.OP_CLIENT_DATA, 0, 7, 3, 1_000) + F.VEL_SETPOINT16.pack(1.0, -2.0, 0.5, 0.1)
    assert struct.unpack_from("<B", b)[0] == F.OP_CLIENT_DATA and len(b) == 32


def test_awrrt_round_trip():
    recs = [F.AwrtRecord(F.AWRT_S2C, F.AWRT_TEXT, 0, b'{"op":"ping","t":1}'), F.AwrtRecord(F.AWRT_C2S, F.AWRT_BINARY, 5, b"\x01" * 13)]
    data = F.write_awrrt({"protocol": "awr.rt.v1", "n_uav": 0}, recs, F.AWRT_TIMED, 42)
    assert len(data) % 8 == 0
    f = F.read_awrrt(data)
    assert f.flags == F.AWRT_TIMED and f.t0_wall_ns == 42 and f.header["n_uav"] == 0 and f.records == recs
    with pytest.raises(ValueError):
        F.write_awrrt({}, [recs[1], recs[0]])
    with pytest.raises(ValueError):
        F.read_awrrt(b"XXXX" + data[4:])
