"""`python -m awr.reconstruction {run, validate, engines, golden, bench}` (M01 §9.5; UC-M01-05).

run       run a recon job in this process (same stages as the job-worker) and publish the product world
validate  semantic validation of a session directory (V01-V18; exit 0 ok, 1 errors, 2 usage)
engines   capability probe of every engine (the R63 payload)
golden    write the golden session and the 12 mutation samples (M01-AC-005 fixtures)
bench     INFERRING throughput of the MockEngine (single-frame timings and RSS; not a performance acceptance run)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _cmd_validate(a) -> int:
    from .ir.validate import validate_session

    rep = validate_session(Path(a.session_dir), deep=a.deep)
    if a.json:
        print(json.dumps(rep.to_json(), ensure_ascii=False, indent=1))
    else:
        for e in rep.errors:
            print(f"{e['rule']} {e['message']}")
        for w in rep.warnings:
            print(f"warning {w['rule']} {w['message']}")
        print(f"{'OK' if rep.ok else 'FAILED'} {len(rep.errors)} errors, {len(rep.warnings)} warnings, "
              f"{rep.rules_checked} rules{' (deep)' if a.deep else ''}, {rep.seconds:.2f} s")
    return 0 if rep.ok else 1


def _cmd_engines(a) -> int:
    from .engines.base import engine_catalog

    items = engine_catalog()
    if a.json:
        print(json.dumps({"items": items}, ensure_ascii=False, indent=1))
    else:
        for it in items:
            print(f"{it['engine']:<12} {it['variant']:<18} {'available' if it['available'] else 'unavailable':<11} "
                  f"{it['reason'] or '-':<16} {it['device']:<5} {it['engine_scale']:<18} {it['target_version']}")
    return 0


def _cmd_golden(a) -> int:
    from .ir.golden import MUTATIONS, write_golden_session, write_mutation

    out = Path(a.out_dir)
    g = write_golden_session(out / "golden-session")
    for name in MUTATIONS:
        write_mutation(g, name, out / "mutations")
    print(f"golden session {g}; {len(MUTATIONS)} mutations under {out / 'mutations'}")
    return 0


def _params(a) -> dict:
    p: dict = {"engine": a.engine, "source": {"kind": "world_sample", "world_id": a.world, "path": a.path, "frames": a.frames},
               "seed": a.seed, "params": {"mock": {"source_keep": a.keep}, "georef": {"mode": a.georef, "on_reject": a.on_reject}}}
    if a.target:
        p["target_world_id"] = a.target
    if a.depth_res:
        w, h = (int(v) for v in a.depth_res.split("x"))
        p["params"]["camera"] = {"depth_res": [w, h]}
    return p


def _cmd_run(a) -> int:
    from .jobs.recon_job import run_local

    t0 = time.perf_counter()
    res = run_local(_params(a), worlds_dir=Path(a.worlds) if a.worlds else None, runs_dir=Path(a.runs) if a.runs else None)
    res.pop("events", None)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print(f"{'SUCCEEDED' if res['exit_code'] == 0 else 'FAILED'} in {time.perf_counter() - t0:.1f} s", file=sys.stderr)
    return 0 if res["exit_code"] == 0 else 1


def _cmd_bench(a) -> int:
    import resource

    import numpy as np

    from .engines.base import get_engine
    from .engines.mock import MockInputs
    from .engines.mock_paths import make_path
    from .jobs.params import resolve_params
    from .pipeline.source import prepare_source
    from .types import EngineContext, FrameInput, SessionSpec

    p = resolve_params(_params(a))
    worlds = Path(a.worlds) if a.worlds else Path(__file__).resolve().parents[3] / "worlds"
    t0 = time.perf_counter()
    src = prepare_source(worlds, a.world, keep=a.keep, seed=a.seed)
    lo, hi = src.meta.extent
    path = make_path(a.path, a.frames, 10.0, extent_min=lo, extent_max=hi, ground_z_m=src.ground_z_m)
    t_prep = time.perf_counter() - t0
    eng = get_engine("mock")
    eng.prepare(SessionSpec("rs-000000000000", a.frames, 10.0, 1920, 1080, a.seed, p, source=MockInputs(src, path, p)),
                EngineContext(workdir=Path(".")))
    dts = []
    t = time.perf_counter()
    for _ in eng.infer(FrameInput(k, int(path.t_ns[k])) for k in range(a.frames)):
        now = time.perf_counter()
        dts.append(now - t)
        t = now
    ms = np.asarray(dts) * 1000
    print(json.dumps({"world": a.world, "path": a.path, "frames": a.frames, "source_keep": a.keep, "points": len(src.xyz),
                      "prep_s": round(t_prep, 2), "frame_ms_mean": round(float(ms.mean()), 1),
                      "frame_ms_p95": round(float(np.percentile(ms, 95)), 1),
                      "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.reconstruction", description="M01 reconstruction tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="validate a Recon IR session directory")
    v.add_argument("session_dir")
    v.add_argument("--deep", action="store_true")
    v.add_argument("--json", action="store_true")
    e = sub.add_parser("engines", help="print the engine capability probe")
    e.add_argument("--json", action="store_true")
    g = sub.add_parser("golden", help="write the golden session and mutation fixtures")
    g.add_argument("out_dir")
    for name in ("run", "bench"):
        r = sub.add_parser(name, help="run a Mock recon job" if name == "run" else "MockEngine inference timings")
        r.add_argument("--engine", default="mock")
        r.add_argument("--world", default="shenzhen")
        r.add_argument("--path", default="helix", choices=("helix", "lawnmower"))
        r.add_argument("--frames", type=int, default=600)
        r.add_argument("--keep", type=float, default=0.25, help="params.mock.source_keep")
        r.add_argument("--seed", type=int, default=1)
        r.add_argument("--target", default=None)
        r.add_argument("--georef", default="gnss", choices=("gnss", "none"))
        r.add_argument("--on-reject", default="publish_relative", choices=("publish_relative", "fail"))
        r.add_argument("--depth-res", default=None, help="128x72 or 256x144")
        r.add_argument("--worlds", default=os.environ.get("AWR_WORLDS_DIR"))
        r.add_argument("--runs", default=os.environ.get("AWR_RUNS_DIR"))
    a = ap.parse_args(argv)
    return {"validate": _cmd_validate, "engines": _cmd_engines, "golden": _cmd_golden, "run": _cmd_run, "bench": _cmd_bench}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
