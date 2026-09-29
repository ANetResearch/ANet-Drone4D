"""Schema self-check (D1-AC-13 contract part; AWR-16 §15 V-SCHEMA; AWR-17 §10.3)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import CONTRACTS, L, errors, load, registry

SCHEMAS = L.schema_files()
ID_RE = re.compile(r"^https://schemas\.anet-drone\.dev/[a-z0-9_-]+/1/[A-Za-z0-9_.-]+\.schema\.json$")

# data file -> meta schema (every committed contract data file is covered)
DATA = {
    "rt/layouts.json": "rt/layouts.schema.json", "rt/enums.json": "rt/enums.schema.json", "rt/commands.json": "rt/commands.schema.json",
    "rt/reasons.json": "rt/reasons.schema.json", "rt/topics.json": "rt/topics.schema.json", "rt/units.json": "rt/units.schema.json",
    "rt/rng_streams.json": "rt/rng_streams.schema.json", "rt/safety_codes.json": "rt/safety_codes.schema.json",
    "rt/caps/mock.json": "rt/caps.schema.json", "rt/caps/replay.json": "rt/caps.schema.json", "rt/caps/px4_sih.json": "rt/caps.schema.json",
    "rt/caps/prometheus.json": "rt/caps.schema.json", "bus/keys.json": "bus/keys.schema.json", "env/presets.json": "env/presets.schema.json",
    "env/env_world_defaults.json": "env/env_world_defaults.schema.json", "rec/mcap_channels.json": "rec/mcap_channels.schema.json",
    "agent/capability_catalog.json": "agent/capability_catalog.schema.json", "classes/anet-classes-v1.json": "schemas/world/class-table.schema.json",
}


def test_schema_count():
    assert len(SCHEMAS) >= 70


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: str(p.relative_to(CONTRACTS)))
def test_schema_is_valid_2020_12(path: Path):
    d = json.loads(path.read_text(encoding="utf-8"))
    assert d.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert ID_RE.match(d["$id"]), d["$id"]
    Draft202012Validator.check_schema(d)


def _refs(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                out.append(v)
            else:
                _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)
    return out


def test_all_refs_resolve():
    reg = registry()
    bad = []
    for p in SCHEMAS:
        d = json.loads(p.read_text(encoding="utf-8"))
        resolver = reg.resolver(base_uri=d["$id"])
        for ref in _refs(d, []):
            try:
                resolver.lookup(ref)
            except Exception as e:  # collect every failure
                bad.append(f"{p.relative_to(CONTRACTS)} -> {ref}: {type(e).__name__}")
    assert not bad, bad


def test_ids_unique():
    ids = [json.loads(p.read_text(encoding="utf-8"))["$id"] for p in SCHEMAS]
    assert len(ids) == len(set(ids))


def test_every_data_file_is_covered():
    files = sorted(str(p.relative_to(CONTRACTS)) for p in CONTRACTS.rglob("*.json")
                   if not p.name.endswith(".schema.json") and not {"gen", "golden", "fixtures", "node_modules"} & set(p.relative_to(CONTRACTS).parts)
                   and p.name != "package.json")
    assert sorted(DATA) == files


@pytest.mark.parametrize(("data", "schema"), sorted(DATA.items()))
def test_data_file_validates(data: str, schema: str):
    assert errors(schema, load(data)) == []


WORLD = [("world.json", "schemas/world/world.schema.json"), ("coordinate.json", "schemas/world/coordinate.schema.json"),
         ("pointcloud-metadata.json", "schemas/world/pointcloud-metadata.schema.json"), ("dtm_10m.json", "schemas/world/grid.schema.json")]


@pytest.mark.parametrize("city", ["shenzhen", "sanfrancisco"])
def test_world_examples_validate(city: str):
    for name, schema in WORLD:
        assert errors(schema, load(f"fixtures/world/{city}/{name}")) == [], name


def test_world_example_coordinate_hash():
    import hashlib

    for city in ("shenzhen", "sanfrancisco"):
        w = load(f"fixtures/world/{city}/world.json")
        raw = (CONTRACTS / "fixtures" / "world" / city / "coordinate.json").read_bytes()
        assert w["coordinate"]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_world_schema_negative_cases():
    w = load("fixtures/world/shenzhen/world.json")
    for mutate in (lambda d: d.pop("dataset"), lambda d: d.pop("scaleStatus"), lambda d: d.__setitem__("id", "Bad Id"),
                   lambda d: d["dataset"].pop("license")):
        bad = json.loads(json.dumps(w))
        mutate(bad)
        assert errors("schemas/world/world.schema.json", bad), mutate
    c = load("fixtures/world/shenzhen/coordinate.json")
    c["conventions"]["px4Boundary"] = "NED/FRD at gateway only"
    assert errors("schemas/world/coordinate.schema.json", c)


def test_grid_world_z_forbids_float16():
    g = load("fixtures/world/shenzhen/dtm_10m.json")
    assert g["valueFrame"] == "world-z-m"
    assert errors("schemas/world/grid.schema.json", dict(g, dtype="float16"))
    assert errors("schemas/world/grid.schema.json", dict(g, dtype="uint16"))  # integer dtypes need scale and offset
    assert errors("schemas/world/grid.schema.json", dict(g, dtype="uint16", scale=0.01, offset=-100.0)) == []


def test_scenario_s1_example_validates():
    assert errors("scenario/scenario.schema.json", load("fixtures/scenario/s1-shenzhen-facade.json")) == []


def test_class_table_16_classes():
    t = load("classes/anet-classes-v1.json")
    assert len(t["classes"]) == 16
