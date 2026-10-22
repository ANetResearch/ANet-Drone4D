"""Check a deployed life template against its locked recipe in real wall time.

Creates and closes its own SDK session. Does not send keepalive, pause, or model
generation requests. Run from a checkout with the exact deployed engine version.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "tools/sdk"), str(ROOT / "python")]

from awr_sandbox import Sandbox  # noqa: E402

from awr.sim.activities.life import LifeWorld  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe", type=Path, help="Deployed life-<sha256>.json")
    parser.add_argument("--url", default="http://127.0.0.1:28660")
    parser.add_argument("--world", default="synthcity")
    parser.add_argument("--template", default="t13_all_vehicle_types")
    parser.add_argument("--duration", type=int, default=1500)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.duration < 1 or args.interval < 1:
        parser.error("duration and interval must be positive")
    if args.output.exists():
        parser.error("output already exists; use a fresh path to preserve evidence")
    reference = LifeWorld(json.loads(args.recipe.read_text()))
    recipe_sha = args.recipe.stem.removeprefix("life-")
    client = Sandbox(args.url, principal_hint=uuid.uuid4().hex,
                     timeout_s=120, auto_keepalive=False)
    session = None
    report: dict = {"status": "running", "recipe_sha256": recipe_sha,
                    "duration_s": args.duration, "history": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    try:
        session = client.create(args.world, args.template, queue=False, wait_ready=False)
        session.wait_ready(120)
        ids = sorted(x["id"] for x in session.targets.list()["items"])
        assert ids == sorted(reference.actors), "deployed population differs from recipe"
        report["targets"] = len(ids)
        session.clock("play")
        start = time.monotonic()
        max_skew = 0.0
        checkpoints = sorted({*range(0, args.duration + 1, args.interval), args.duration})
        for checkpoint in checkpoints:
            while time.monotonic() - start < checkpoint:
                time.sleep(max(0, min(1, checkpoint - (time.monotonic() - start))))
            records = []
            for aid in ids:
                state = session.targets.state(aid)
                activity = state["activity"]
                skew = abs(state["t_sim_ns"] / 1e9 - (time.monotonic() - start))
                max_skew = max(max_skew, skew)
                assert skew < 2, (aid, "clock skew", skew)
                assert abs(activity["updates"] - math.floor(state["t_sim_ns"] / 1e8)) <= 1, (
                    aid, "tick coverage")
                reference.advance(activity["updates"] / 10)
                expected = reference.sample(aid)[3]
                for key in ("phase", "reason", "hidden", "visits"):
                    assert activity[key] == expected[key], (aid, checkpoint, key)
                assert abs(activity["walked_m"] - expected["walked_m"]) <= .11, (
                    aid, "distance replay mismatch")
                records.append({"id": aid, **{key: activity[key] for key in (
                    "updates", "phase", "reason", "hidden", "visits", "walked_m")}})
            report["history"].append({"checkpoint_s": checkpoint,
                "max_clock_skew_s": round(max_skew, 3), "actors": records})
            save()
            print(f"checkpoint={checkpoint}s actors={len(ids)} "
                  f"min_updates={min(x['updates'] for x in records)} "
                  f"max_clock_skew={max_skew:.3f}s replay=matched", flush=True)
        assert sorted(x["id"] for x in session.targets.list()["items"]) == ids
        assert all(x["updates"] >= args.duration * 10 for x in records)
        if args.duration >= 1500:
            assert all(x["visits"] >= 2 for x in records
                       if not reference.actors[x["id"]].spec.get("stationary"))
        report.update(status="passed", max_clock_skew_s=round(max_skew, 3),
                      all_checkpoints_match_seed_replay=True)
        save()
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        save()
        raise
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            client.close()


if __name__ == "__main__":
    main()
