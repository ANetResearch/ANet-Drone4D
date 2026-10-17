"""契约 1.3.0 的运行侧 bench 契约（AWR-17 §18.5、§18.23、§18.24；M27 §7.2–§7.4；WP-D3-01）：run_spec.v1（含 x-rules 组合矩阵）、
task.v1、run_manifest.v1、evaluation.v1、session.v1、submission.v1、score.v1、board.v1。"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import errors, load

H = "b" * 64
DIGEST = "sha256:" + "c" * 64
RS = "bench/run_spec.v1.schema.json"


def spec(**kw) -> dict:
    s = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": "baseline-coverage"}, "world": {"id": "oryahovo-crop1"},
         "event": {"id": "abc-crossing-001"}, "fleet": [{"uav_id": "uav-01", "vehicle_ref": "p600_mid360_inv", "start": {"kind": "nest", "nest_id": "n1"}}],
         "policy": {"kind": "reference", "name": "coverage"}, "obs_level": "L2", "seed_count": 10, "outputs": ["rgb", "seg"]}
    s.update(kw)
    return s


def test_run_spec_shapes() -> None:
    assert errors(RS, spec()) == []
    assert errors(RS, spec(clock_profile={"name": "d1_250", "main_hz": 250, "physics_hz": 250, "l1_hz": 250}, physics_opts={"gz_profile": "xval"})) == []
    for bad in (spec(fleet=[]), spec(obs_level="L0"), spec(policy={"kind": "reference", "name": "random"}), spec(outputs=["rgb", "rgb"]),
                spec(fleet=[{"uav_id": "drone-1", "vehicle_ref": "p600", "start": {"kind": "nest", "nest_id": "n1"}}])):
        assert errors(RS, bad)


def test_x_rules_reference_known_fields_and_reasons() -> None:
    d = load(RS)
    reasons = {r["code"] for r in load("rt/reasons.json")["reasons"]}
    props = d["properties"]
    for r in d["x-rules"]:
        assert r["code"] in reasons and re.fullmatch(r"[a-z0-9_]{1,40}", r["why"]), r
        paths = [*r.get("if", {}), *r.get("allow", {}), *r.get("forbid", []), *r.get("require", []), *r.get("min", {}), *r.get("exactly_one", [])]
        for p in paths:
            head, _, rest = p.partition(".")
            assert head in props, p
            if rest and head != "clock_profile":
                assert rest in props[head]["properties"], p
    assert len({r["id"] for r in d["x-rules"]}) == len(d["x-rules"])


def _whys(s: dict, **kw) -> set[str]:
    from awr.bench.spec_rules import check

    return {v.why for v in check(s, **kw)}


def test_rule_evaluator_matrix() -> None:
    assert _whys(spec()) == set()
    assert _whys(spec(ranked=True)) == set()  # physics defaults to realistic
    assert _whys(spec(ranked=True, physics="fast")) == {"ranked_requires_realistic"}
    assert _whys(spec(ranked=True, physics_opts={"gz_profile": "xval"})) == {"ranked_requires_realistic"}
    assert _whys(spec(ranked=True, policy={"kind": "sdk"})) == {"sdk_ranked"}
    assert _whys(spec(ranked=True, seed_count=5)) == {"ranked_seed_rules"}
    s = spec(ranked=True, seeds=[1, 2])
    del s["seed_count"]
    assert _whys(s) == {"ranked_seed_rules"}
    assert _whys(spec(ranked=True, obs_level="T0")) == {"t0_ranked"}
    assert _whys(spec(physics="sitl", ranked=True)) == {"sitl_ranked", "ranked_requires_realistic"}
    assert _whys(spec(outputs=["depth"])) == {"depth_requires_l2_depth"}
    assert _whys(spec(outputs=["depth"], obs_level="L2-depth")) == set()
    assert _whys(spec(appearance="3dgs")) == {"appearance_source_required"}
    assert _whys(spec(appearance_source="aerial")) == {"appearance_source_forbidden"}
    assert _whys(spec(seeds=[1])) == {"seeds_xor_seed_count"}
    assert _whys(spec(outputs=["imu"], clock_profile="sandbox100")) == {"physics_hz_insufficient"}
    assert _whys(spec(clock_profile="sandbox100", ranked=True)) == {"sandbox100_ranked"}
    ev = spec(eval_ref={"submission_id": "s1", "board_version": "b1", "track": "std", "lane": "handoff"})
    assert _whys(ev) == {"eval_ref_reserved"} and _whys(ev, external=False) == set()


def test_task_projection_hides_scoring() -> None:
    t = load("bench/task.v1.schema.json")
    vis = set(t["x-agent-visible"])
    assert vis <= set(t["properties"]) and "scored_event_types" not in vis
    task = {"schema": "drone4d.bench.task.v1", "task_id": "handoff-001", "event_ref": "abc-crossing-001@v1", "version": 1, "event_type": "rendezvous_handoff",
            "brief": {"anchors_public": ["plaza"], "targets": [{"appearance_desc": "blue top", "class": "person"}],
                      "region_polygon_enu_m": [[0, 0], [300, 0], [300, -300]], "duration_s": 360},
            "known_targets": False, "min_obs_level": "L1", "altitude_envelope_agl_m": [30, 120], "scored_event_types": ["board_vehicle"],
            "report_types": ["board_vehicle"], "safety": {"min_sep_person_m": 10, "min_sep_vehicle_m": 5, "no_fly": []},
            "budget": {"distance_km_per_uav": 12, "comm_kb_per_uav": 512}}
    assert errors("bench/task.v1.schema.json", task) == []
    leak = copy.deepcopy(task)
    leak["prior"] = {"kind": "heatmap", "ref": "p.png", "actor": "person_A"}
    assert errors("bench/task.v1.schema.json", leak)


def test_manifest_evaluation_session() -> None:
    man = {"schema": "drone4d.bench.run_manifest.v1", "run_id": "r1", "experiment_id": "x1", "attempt": 1, "restart_count": 0, "spec": spec(ranked=True),
           "seed_commit": H, "config_sha256": H, "spec_sha256": H, "run_hash": H, "versions": {"renderer": "0.1.0"}, "determinism_level": "2",
           "split": "test", "ranked": True, "ranked_valid": None, "invalid_reasons": [], "timing": {"queued_unix_ns": 1}}
    assert errors("bench/run_manifest.v1.schema.json", man) == []
    man["spec"]["obs_level"] = "L9"
    assert errors("bench/run_manifest.v1.schema.json", man)
    ev = {"schema": "drone4d.bench.evaluation.v1", "run_id": "r1", "evaluator_version": "0.1.0", "thresholds_version": "1", "oracle_version": "0",
          "inputs": {"raw_mcap_sha256": H, "truth_sha256": H, "tape_sha256": H, "event_truth_sha256": H}, "obs_level": "L2", "ranked": False,
          "ranked_valid": True, "invalid_reasons": [], "ec": {"value": 0.5, "coverage": 0.6, "events": []}, "excluded": [],
          "ao": {"ao_real": 0.4, "ao_pos_los": 0.5, "ao_pos": 0.7, "oracle": {"version": "0", "camera_models": ["rgb720-gs"], "grid_m": 5, "alt_step_m": 10,
                                                                                "envelope_agl_m": [30, 120]}},
          "safety": {"ground_alerts": 0, "nofly_incursions": 0, "detected_count": 0, "min_sep_m": {"person": None, "vehicle": 12.5},
                     "violations": {"person": 0, "vehicle": 0}, "penetrations": 0}, "actors": {}}
    assert errors("bench/evaluation.v1.schema.json", ev) == []
    ses = {"schema": "drone4d.bench.session.v1", "principal": "team-a/alice", "role": "invited", "projects": ["p1"], "iat_unix_ns": 1, "exp_unix_ns": 2}
    assert errors("bench/session.v1.schema.json", ses) == []
    ses["token"] = "x"
    assert errors("bench/session.v1.schema.json", ses)


def test_submission_score_board_have_no_composite_without_weights() -> None:
    lane = {"lane": "handoff", "metrics": {"ec": {"value": 0.5, "ci95": [0.4, 0.6], "rank": 1, "tie_group": 0}}, "dominance_tier": 1,
            "runs": {"total": 30, "valid": 30, "invalid": 0}}
    sub = {"track": "standard", "image_digest": DIGEST, "entry": {"cmd": ["python", "-m", "agent"]}, "notes": "v1"}
    assert errors("bench/submission.v1.schema.json", sub) == []
    sub["result"] = {"lanes": [lane]}
    assert errors("bench/submission.v1.schema.json", sub) == []
    sc = {"schema": "drone4d.bench.score.v1", "score_version": "d3-score-0", "components": ["ec", "eu", "ao", "ce"], "weights": None, "lanes": [lane]}
    assert errors("bench/score.v1.schema.json", sc) == []
    for bad in ({"s": 50}, {"ci95": [40, 60]}, {"lanes": [{**lane, "composite": 0.5}]}):
        assert errors("bench/score.v1.schema.json", {**sc, **bad}), bad
    assert errors("bench/score.v1.schema.json", {**sc, "weights": {"ec": 0.4, "eu": 0.2, "ao": 0.2, "ce": 0.2}, "s": 55}) == []
    board = {"schema": "drone4d.bench.board.v1", "board_version": "b1",
             "tracks": [{"track": "standard", "fleet_ref": "p600x4", "obs_level": "L2", "physics": "realistic", "mode": "exogenous", "agent_class": "image",
                         "appearance": "mesh"}],
             "lanes": [{"lane": "handoff", "kind": "event_type", "event_type": "rendezvous_handoff", "test_events": ["e1"], "seed_set_commit": H}],
             "evaluator_major": 0, "platform_digest": DIGEST,
             "entries": [{"entry_id": "e1", "team": "baseline", "display_name": "coverage", "track": "standard", "lane": "handoff",
                          "metrics": lane["metrics"], "dominance_tier": 1, "first_listed_unix_ns": 1, "baseline": True}]}
    assert errors("bench/board.v1.schema.json", board) == []
    bad = json.loads(json.dumps(board))
    bad["tracks"][0]["appearance"] = "mesh_distilled"
    assert errors("bench/board.v1.schema.json", bad)
