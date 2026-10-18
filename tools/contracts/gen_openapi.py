#!/usr/bin/env python3
"""OpenAPI snapshot of the REST surface (AWR-17 §2 item 10, §10.6 item 9; API-FR-046; M00-B-to-M11 item 6, M11-api-to-M00
item 2, SK-B-to-M00 item 1).

Writes packages/contracts/rest/openapi.snapshot.json: `create_app(ApiSettings(...)).openapi()` of the api with fixed settings
(dev profile, no supervisor, repository worlds). Building the app has no side effects (no bus, no Gateway: those start in the
ASGI lifespan). The snapshot is canonical JSON (sorted keys, two-space indent, trailing newline) so that a route change shows
up as a small diff. `--check` exits 1 when the committed snapshot differs from the live schema; the contract test
tests/contracts/test_openapi_snapshot.py runs the same comparison and also checks that every REST error code raised by the
api is registered in reasons.json.

Sandbox (D2, AWR-17 §17.5, API-AC-046): `--app sandbox` writes packages/contracts/rest/sandbox.openapi.snapshot.json. While
`awr.api.sandbox_main.openapi_for_snapshot()` (M11, WP-03) does not exist the document is the design table of
tools/contracts/sandbox_api.py (SB-00 to SB-78); afterwards it is the live schema of the sandbox-api, like the D1 snapshot.

Bench (D3, AWR-17 §18.4 rule 6, API-AC-070, D3-AC-51): `--app bench` writes packages/contracts/bench/openapi/bench.openapi.snapshot.json,
the live schema of the bench-api (`awr.bench.api.app.create_bench_app` over a throw-away data root). Every operation carries
`x-d3-role` (minimum role actually enforced), `x-d3-bn`, `x-d3-stage` and `x-awr-reasons` (awr.bench.api.deps.annotate). The bench
app needs the D3 environment (.venv-d3: mcap etc.), so `all` does not include it.

Usage: python tools/contracts/gen_openapi.py [--check] [--app show|sandbox|bench|all]   (default all = show + sandbox)
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "packages" / "contracts" / "rest" / "openapi.snapshot.json"
SANDBOX_SNAPSHOT = ROOT / "packages" / "contracts" / "rest" / "sandbox.openapi.snapshot.json"
BENCH_SNAPSHOT = ROOT / "packages" / "contracts" / "bench" / "openapi" / "bench.openapi.snapshot.json"
# environment the app reads while it is built; cleared so that the snapshot does not depend on the caller's shell
_ENV_KEYS = ("AWR_PROFILE", "AWR_SUPERVISOR_PID", "AWR_SCENARIOS_DIR", "AWR_WORLDS_DIR", "AWR_RUNS_DIR", "AWR_WEB_DIST")


_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def _stable_operation_ids(spec: dict) -> dict:
    """FastAPI suffixes the operation id of a multi-method route (GET + HEAD) with `list(route.methods)[0]`, the first
    element of a set: it changes with PYTHONHASHSEED. Suffix every operation id with its own method instead."""
    for ops in spec.get("paths", {}).values():
        for method, op in ops.items():
            oid = op.get("operationId") if isinstance(op, dict) else None
            if isinstance(oid, str):
                for m in _METHODS:
                    if oid.endswith("_" + m):
                        op["operationId"] = oid[: -len(m)] + method
                        break
    return spec


def live_schema() -> dict:
    saved = {k: os.environ.pop(k) for k in _ENV_KEYS if k in os.environ}
    try:
        sys.path.insert(0, str(ROOT / "python"))
        from awr.api.main import create_app
        from awr.api.settings import ApiSettings

        s = ApiSettings(run_id="r20260101-000000-0000", world_id="shenzhen", run_dir=Path("/nonexistent/awr-openapi"),
                        persist_dir=None, worlds_dir=ROOT / "worlds", web_dist=None, serve_web=False, profile="dev",
                        secret=b"\0" * 32, admin_password="openapi-snapshot")
        return _stable_operation_ids(create_app(s).openapi())
    finally:
        os.environ.update(saved)


def canonical(spec: dict) -> str:
    return json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def sandbox_schema() -> dict:
    """Live sandbox-api schema when the M11 hook exists, else the frozen design table (tools/contracts/sandbox_api.py)."""
    sys.path.insert(0, str(ROOT / "python"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from awr.api.sandbox_main import openapi_for_snapshot  # type: ignore[attr-defined]
    except ImportError:
        openapi_for_snapshot = None
    if openapi_for_snapshot is not None:
        saved = {k: os.environ.pop(k) for k in _ENV_KEYS if k in os.environ}
        try:
            return _stable_operation_ids(openapi_for_snapshot())
        finally:
            os.environ.update(saved)
    import contracts_lib
    import sandbox_api

    return sandbox_api.design_openapi(contracts_lib.contracts_version())


def bench_schema() -> dict:
    """Live bench-api schema; the app is built over a temporary data root (keys and bench.sqlite are created there and removed)."""
    import tempfile

    sys.path.insert(0, str(ROOT / "python"))
    from awr.bench.api.app import create_bench_app
    from awr.bench.settings import BenchSettings

    with tempfile.TemporaryDirectory(prefix="awr-bench-openapi-") as tmp:
        app = create_bench_app(BenchSettings(data_root=Path(tmp) / "d3"))
        spec = _stable_operation_ids(app.openapi())
        app.state.auth._db.close()
    return spec


def _one(path: Path, text: str, check: bool, label: str) -> int:
    if check:
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        if old != text:
            diff = difflib.unified_diff(old.splitlines(), text.splitlines(), "snapshot", "live", lineterm="", n=1)
            sys.stderr.write("\n".join(list(diff)[:60]) + "\n")
            print(f"gen_openapi.py --check: {label} REST surface differs from {path.relative_to(ROOT)}; "
                  "review the change and run python tools/contracts/gen_openapi.py (or make contracts)", file=sys.stderr)
            return 1
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    spec = json.loads(text)
    print(f"wrote {path.relative_to(ROOT)}: {len(spec.get('paths', {}))} paths")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OpenAPI snapshots (packages/contracts/rest/{openapi,sandbox.openapi}.snapshot.json)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--app", choices=("show", "sandbox", "bench", "all"), default="all")
    a = ap.parse_args(argv)
    rc = 0
    if a.app in ("show", "all"):
        rc |= _one(SNAPSHOT, canonical(live_schema()), a.check, "api")
    if a.app in ("sandbox", "all"):
        rc |= _one(SANDBOX_SNAPSHOT, canonical(sandbox_schema()), a.check, "sandbox-api")
    if a.app == "bench":
        rc |= _one(BENCH_SNAPSHOT, canonical(bench_schema()), a.check, "bench-api")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
