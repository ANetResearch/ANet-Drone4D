"""M05-AC-030 (AWR-18 §8.6): flight60 generator. The .bin is 3601 x 6 f32 (86,424 B); the .json validates against
awr.flight60.v1 and its coordinate_sha256 / bin_sha256 match the world and the .bin; median speed 20.7 m/s +-10 %, maximum
yaw rate 90 deg/s +-5 %; generation is deterministic; the checked-in outputs under apps/web/public/bench/flight60 are
fresh (skipped when the worlds are not built)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
WORLDS = ROOT / "worlds"
CONTRACTS = ROOT / "packages" / "contracts"
OUT = ROOT / "apps" / "web" / "public" / "bench" / "flight60"

spec = importlib.util.spec_from_file_location("flight60_gen", ROOT / "tools" / "bench" / "flight60" / "gen.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

CITIES = [c for c in ("shenzhen", "newyork", "shanghai", "suzhou", "sanfrancisco", "chicago") if (WORLDS / c / "world.json").exists()]
pytestmark = pytest.mark.skipif(not CITIES, reason="worlds not built (make worlds)")


def validator():
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    reg = Registry()
    for p in CONTRACTS.rglob("*.schema.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        if "$id" in d:
            reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    schema = json.loads((CONTRACTS / "perf" / "flight60.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=reg)


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("flight60")
    return out, {c: gen.gen_flight60(WORLDS / c, out) for c in CITIES[:3]}


def test_files_schema_and_hashes(generated):
    out, docs = generated
    v = validator()
    for city, doc in docs.items():
        b = (out / f"{city}.bin").read_bytes()
        assert len(b) == 86_424 == 3601 * 6 * 4
        errs = [e.message for e in v.iter_errors(doc)]
        assert not errs, errs
        assert doc["bin_sha256"] == hashlib.sha256(b).hexdigest()
        assert doc["coordinate_sha256"] == hashlib.sha256((WORLDS / city / "coordinate.json").read_bytes()).hexdigest()
        assert doc["content_version"] == json.loads((WORLDS / city / "world.json").read_text())["contentVersion"]
        assert [s["name"] for s in doc["segments"]] == ["overview-descent", "transit", "fast-yaw", "tower-orbit", "follow", "climb-out"]


def test_speed_and_yaw(generated):
    out, docs = generated
    for city, doc in docs.items():
        a = np.fromfile(out / f"{city}.bin", dtype="<f4").reshape(3601, 6).astype(np.float64)
        spd = np.linalg.norm(np.diff(a[:, :3], axis=0), axis=1) * 60
        assert abs(np.median(spd) - 20.7) <= 0.1 * 20.7, city
        assert abs(doc["stats"]["yawrate_max_dps"] - 90) <= 0.05 * 90, city
        fwd = a[:, 3:] - a[:, :3]
        assert np.allclose(np.linalg.norm(fwd, axis=1), 100.0, atol=1e-2)  # target = eye + 100 fwd


def test_peak_is_argmax_dsm_minus_dtm(generated):
    _, docs = generated
    city = next(iter(docs))
    dsm = gen.Grid(WORLDS / city / "geometry" / "terrain" / "dsm_2m.json")
    dtm = gen.Grid(WORLDS / city / "geometry" / "terrain" / "dtm_10m.json")
    x, y, h = gen.peak_of(dsm, dtm)
    p = docs[city]["peak"]
    assert (round(x, 3), round(y, 3), round(h, 3)) == (p["x_m"], p["y_m"], p["h_m"])
    assert h > 50


def test_deterministic(generated, tmp_path):
    out, docs = generated
    city = next(iter(docs))
    doc = gen.gen_flight60(WORLDS / city, tmp_path)
    assert (tmp_path / f"{city}.bin").read_bytes() == (out / f"{city}.bin").read_bytes()
    assert doc == docs[city]


def test_checked_in_outputs_fresh():
    missing = [c for c in CITIES if not (OUT / f"{c}.json").exists()]
    if missing:
        pytest.skip(f"flight60 not generated for {missing} (make flight60)")
    stale = [c for c in CITIES if not gen.is_fresh(WORLDS / c, OUT)]
    assert not stale, f"stale flight60 {stale}: run make flight60"
