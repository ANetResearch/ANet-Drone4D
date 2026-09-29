"""Protocol fixtures (.awrrt) against the payload golden (AWR-16 §13.9, DATA-AC-018; D1-AC-35 contract part)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import msgpack
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctlib
import gen_fixtures as G

from awr.contracts import frame as F
from awr.contracts import layouts

FIX = ctlib.CONTRACTS / "fixtures"
NAMES = sorted(p.stem for p in (FIX / "rt").glob("*.awrrt"))


def test_fixture_set():
    assert {"smoke_n1", "swarm_n200", "swarm_n1000", "time_epoch"} <= set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_fixture_decodes_to_golden(name: str):
    data = (FIX / "rt" / f"{name}.awrrt").read_bytes()
    gold = json.loads((FIX / "payloads" / f"{name}.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(data).hexdigest() == gold["sha256"]
    dec = G.decode_fixture(data)
    assert dec == {k: gold[k] for k in ("flags", "t0_wall_ns", "header", "records")}
    hdr = gold["header"]
    assert hdr["protocol"] == "awr.rt.v1" and int(hdr["layout_id"], 16) == layouts.LAYOUT_ID
    assert len(data) % 8 == 0


@pytest.mark.parametrize("name", NAMES)
def test_fixture_messages_validate(name: str):
    f = F.read_awrrt((FIX / "rt" / f"{name}.awrrt").read_bytes())
    last = -1
    for r in f.records:
        assert r.t_rel_ns >= last
        last = r.t_rel_ns
        if r.kind == F.AWRT_TEXT:
            assert ctlib.errors("rt/ops.schema.json", json.loads(r.payload)) == []
        elif r.payload[0] == F.OP_BATCH:
            for x in F.iter_records(r.payload):
                if x.channel_id == G.CH["roster"]:
                    obj = msgpack.unpackb(r.payload[x.payload_off:x.payload_off + x.length])
                    assert ctlib.errors("rt/payloads/fleet_roster.schema.json", obj) == []


def test_swarm_sizes():
    for name, n in (("swarm_n200", 200), ("swarm_n1000", 1000), ("smoke_n1", 1)):
        gold = json.loads((FIX / "payloads" / f"{name}.json").read_text(encoding="utf-8"))
        rows = [x["n_rows"] for r in gold["records"] if r.get("op") == "BATCH" for x in r["batch"]["records"] if x["channel_id"] == G.CH["swarm"]]
        assert rows and all(v == n for v in rows)


def test_time_epoch_covers_m12_cases():
    gold = json.loads((FIX / "payloads" / "time_epoch.json").read_text(encoding="utf-8"))
    times = [r["time"] for r in gold["records"] if r.get("op") == "TIME"]
    assert {t["state"] for t in times} == set(range(10))
    assert any(t["replay"] for t in times) and any(t["state_byte"] & 0x70 for t in times)
    epochs = [t["epoch"] for t in times]
    assert 65535 in epochs and 0 in epochs
    batches = [r["batch"] for r in gold["records"] if r.get("op") == "BATCH"]
    assert any(any(x["rflags"] & F.RF_RESET for x in b["records"]) for b in batches)
    assert any(b["flags"] & F.BATCH_REPLAY for b in batches)
