"""Recon IR schema, semantic validator and AWTR v1 (M01-AC-005, AC-006; M01-FR-008 to FR-012).

The golden session (24 frames) validates with zero errors (deep); each of the 12 mutations of M01 §6.3.4 is rejected by
its expected rule; Python jsonschema and Ajv 8 strict reach the same per-document conclusions; trajectory.bin round-trips
byte for byte and has L(600) = 24,032 B.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from recon_common import ROOT

from awr.reconstruction.ir.golden import MUTATIONS, write_mutation
from awr.reconstruction.ir.schema import DOC_SCHEMAS, load_schema, schema_dir, schema_errors
from awr.reconstruction.ir.trajectory_bin import (
    AwtrError,
    decode_trajectory,
    encode_trajectory,
    expected_length,
    read_trajectory_bin,
)
from awr.reconstruction.ir.validate import validate_session

NODE = shutil.which("node") or str(Path.home() / ".local" / "node" / "bin" / "node")
need_node = pytest.mark.skipif(not (Path(NODE).exists() and (ROOT / "node_modules" / "ajv").exists()), reason="node or ajv missing")


def test_golden_session_is_valid(golden_session):
    rep = validate_session(golden_session, deep=True)
    assert rep.ok, rep.errors
    assert not rep.warnings, rep.warnings
    assert rep.rules_checked == 18


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_mutation_is_rejected(name, golden_session, tmp_path):
    d = write_mutation(golden_session, name, tmp_path)
    rep = validate_session(d, deep=True)
    assert not rep.ok
    assert rep.rules() & MUTATIONS[name][1], (name, rep.errors)


def _py_docs(d: Path) -> dict:
    out = {}
    for f, s in DOC_SCHEMAS.items():
        if f == "frames.jsonl":
            rows = [json.loads(x) for x in (d / f).read_text().splitlines() if x.strip()]
            out[f] = all(not schema_errors(r, s) for r in rows)
        else:
            out[f] = not schema_errors(json.loads((d / f).read_text()), s)
    return out


@need_node
def test_python_and_ajv_agree(golden_session, tmp_path):
    dirs = [golden_session] + [write_mutation(golden_session, n, tmp_path) for n in sorted(MUTATIONS)]
    r = subprocess.run([NODE, str(Path(__file__).with_name("ajv_check_recon.mjs")), *map(str, dirs)], capture_output=True,
                       text=True, cwd=ROOT, env=dict(os.environ), timeout=120)
    assert r.returncode == 0, r.stderr
    ajv = {Path(x["dir"]): x["docs"] for x in map(json.loads, r.stdout.splitlines())}
    for d in dirs:
        assert ajv[Path(d)] == _py_docs(Path(d)), d


def test_schema_scale_status_single_definition():
    """M01-AC-006: the recon scale_status is a $ref to the one ScaleStatus enum (common.schema.json / rt enums)."""
    from awr.contracts.enums import ScaleStatus

    for name in ("session.schema.json", "alignment.schema.json"):
        s = json.dumps(load_schema(name))
        assert "common.schema.json#/$defs/scaleStatus" in s
    common = json.loads((ROOT / "packages/contracts/schemas/world/common.schema.json").read_text())
    assert common["$defs"]["scaleStatus"]["enum"] == [m.name.lower() for m in ScaleStatus]
    for sub in ("relative", "gnss", "rtk", "lidar"):
        assert sub in common["$defs"]["scaleStatus"]["enum"]
    assert schema_dir().is_dir()


def test_job_params_schema_rejects_unknown_fields():
    ok = {"engine": "mock", "source": {"kind": "world_sample", "world_id": "shenzhen", "path": "helix", "frames": 600},
          "target_world_id": "shenzhen-recon-01", "seed": 1, "params": {"georef": {"mode": "gnss", "on_reject": "publish_relative"}}}
    assert schema_errors(ok, "recon-job-params.schema.json") == []
    for bad in ({**ok, "extra": 1}, {**ok, "engine": "nerf"}, {**ok, "source": {**ok["source"], "frames": 10}},
                {**ok, "params": {"georef": {"mode": "gnss", "on_reject": "maybe"}}}, {**ok, "target_world_id": "Shenzhen"}):
        assert schema_errors(bad, "recon-job-params.schema.json"), bad


# ---------------------------------------------------------------- trajectory.bin (AWTR v1)
def _traj(n: int, seed: int = 0):
    rng = np.random.default_rng(seed)
    q = rng.normal(size=(n, 4))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    return (123_456_789_012_345_678, np.arange(n, dtype=np.int64) * 100_000_000, rng.normal(0, 500, (n, 3)), q,
            rng.integers(0, 3, n), rng.integers(0, 256, n), np.ones(n, dtype=np.uint16))


@pytest.mark.parametrize("n", [0, 1, 7, 8, 9, 600, 1001])
def test_awtr_length_and_round_trip(n):
    args = _traj(n, n)
    b = encode_trajectory(*args)
    assert len(b) == expected_length(n)
    tr = decode_trajectory(b)
    assert tr.n == n and tr.t0_ns == args[0] and not tr.body
    assert np.array_equal(tr.t_rel_ns, args[1])
    assert np.array_equal(tr.pos, args[2].astype("<f4"))
    assert np.all(tr.q_xyzw[:, 3] >= 0) if n else True
    assert encode_trajectory(tr.t0_ns, tr.t_rel_ns, tr.pos, tr.q_xyzw, tr.frame_type, tr.conf_u8, tr.camera_id) == b


def test_awtr_600_is_24032_bytes_and_layout(tmp_path):
    assert expected_length(600) == 24_032
    args = _traj(600, 1)
    p = tmp_path / "trajectory.bin"
    p.write_bytes(encode_trajectory(*args))
    b = p.read_bytes()
    assert b[:4] == b"AWTR" and int.from_bytes(b[4:6], "little") == 1 and int.from_bytes(b[6:8], "little") == 0
    assert int.from_bytes(b[8:12], "little") == 600 and int.from_bytes(b[16:24], "little", signed=True) == args[0]
    # SoA offsets: t 32, pos 32 + 4800, q + 7200, frame_type + 9600, conf + 600 (8-aligned), camera_id + 600
    assert np.frombuffer(b, "<i8", 600, 32)[1] == 100_000_000
    assert np.frombuffer(b, "<f4", 3, 4832)[0] == np.float32(args[2][0, 0])
    assert np.frombuffer(b, np.uint8, 1, 4832 + 7200 + 9600)[0] == args[4][0]
    assert np.frombuffer(b, "<u2", 600, 22832)[0] == 1 and len(b) == 22832 + 1200
    tr = read_trajectory_bin(p)
    assert tr.n == 600
    with pytest.raises(AwtrError):
        decode_trajectory(b[:-1])
    with pytest.raises(AwtrError):
        decode_trajectory(b"XXXX" + b[4:])
