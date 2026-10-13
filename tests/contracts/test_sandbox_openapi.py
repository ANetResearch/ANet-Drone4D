"""Sandbox REST v1 OpenAPI snapshot (API-AC-046; AWR-17 §17.5; AWR-04 §11.4): covers SB-00 to SB-68, every body reference resolves
to a contract schema, and the snapshot matches its generator (design table until the sandbox-api hook exists, then the live app)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import CONTRACTS, ROOT, TOOLS, registry

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import sandbox_api as SA

SNAP = CONTRACTS / "rest" / "sandbox.openapi.snapshot.json"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_openapi", ROOT / "tools" / "contracts" / "gen_openapi.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _paths(doc: dict) -> set[tuple[str, str]]:
    out = set()
    for path, ops in doc["paths"].items():
        rel = path[len(SA.PREFIX):] if path.startswith(SA.PREFIX) else path
        for m in ops:
            if m in ("get", "put", "post", "delete", "patch"):
                out.add((m, rel))
    return out


def test_route_table_is_complete():
    assert SA.sb_ids() == [f"SB-{i:02d}" for i in range(79)]            # D2.1 SB-69 至 SB-78（契约 1.2.0）
    assert len(SA.route_keys()) == len(SA.ENDPOINTS) == 82              # SB-55 has four action routes
    owners = {e.owner.split(" ")[0] for e in SA.ENDPOINTS}
    assert {"rest/sandbox.py", "rest/catalog.py", "rest/fleet.py", "rest/perception.py", "rest/targets.py", "rest/tasking.py", "rest/env.py"} <= owners
    assert {e.sb for e in SA.ENDPOINTS if e.priority == "P1"} == {"SB-17", "SB-65", "SB-66", "SB-67", "SB-76", "SB-77", "SB-78"}


def test_snapshot_covers_all_routes():
    doc = json.loads(SNAP.read_text(encoding="utf-8"))
    assert doc["openapi"].startswith("3.1")
    missing = SA.route_keys() - _paths(doc)
    assert not missing, sorted(missing)


def test_snapshot_matches_generator():
    g = _gen()
    assert g.canonical(g.sandbox_schema()) == SNAP.read_text(encoding="utf-8"), "python tools/contracts/gen_openapi.py --app sandbox"


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


def test_schema_references_resolve():
    doc = json.loads(SNAP.read_text(encoding="utf-8"))
    if doc.get("info", {}).get("x-awr-source") != "design":
        return                                                          # live app: FastAPI components, checked by the D1 mechanism
    reg = registry()
    resolver = reg.resolver()
    bad = []
    for ref in sorted(set(_refs(doc["paths"], []))):
        try:
            resolver.lookup(ref)
        except Exception as e:  # collect all failures
            bad.append(f"{ref}: {type(e).__name__}")
    assert not bad, bad


def test_design_operations_carry_route_metadata():
    doc = json.loads(SNAP.read_text(encoding="utf-8"))
    if doc.get("info", {}).get("x-awr-source") != "design":
        return
    for path, ops in doc["paths"].items():
        for m, op in ops.items():
            x = op["x-awr"]
            assert x["auth"] in ("public", "ticket", "session", "body_token") and x["priority"] in ("P0", "P1"), (m, path)
            if x["auth"] == "session":
                assert op["security"] == [{"sandbox": []}]
            assert "default" in op["responses"]
