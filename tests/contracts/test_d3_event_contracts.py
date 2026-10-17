"""契约 1.3.0 的 event/（AWR-05 §8.1、M23 §6.2、§6.7、§6.10；WP-D3-01）：DSL、锚点、磁带规范头与清单、真值、事件图、简报。
DSL 与覆盖层示例直接取自 M23 PRD，文档与 schema 不一致时失败。"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import errors

ROOT = Path(__file__).resolve().parents[2]
M23 = next((ROOT / "docs/modules").glob("M23-*.md")).read_text(encoding="utf-8")
H = "a" * 64
EV = "event/event.v1.schema.json"


def _example() -> dict:
    sec = M23[M23.index("#### 6.2.7"):M23.index("### 6.3")]
    return yaml.safe_load(re.search(r"```yaml\n(.*?)```", sec, re.S).group(1))


def test_m23_dsl_example_validates() -> None:
    ex = _example()
    assert errors(EV, ex) == []
    assert ex["actors"][2]["class"] == "vehicle" and len(ex["annotations"]) == 4


@pytest.mark.parametrize(("path", "value"), [
    (("event_id",), "ABC"), (("tick_hz",), 20), (("duration_s",), 4000), (("mode",), "mixed"),
    (("actors", 0, "route", 0, "do"), "fly"), (("actors", 0, "gait", "speed_mps"), [0, 1]),
    (("annotations", 0, "type"), "mutual_occlusion"), (("relations", 0, "rel"), "near"),
    (("event_type",), "chase"), (("actors", 0, "appearance", "top_rgb"), [300, 0, 0]),
])
def test_dsl_rejects_out_of_range(path: tuple, value) -> None:
    ex = _example()
    node = ex
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    assert errors(EV, ex)


def test_dsl_is_closed_and_needs_actors() -> None:
    ex = _example()
    ex["actors"][0]["speed"] = 1
    assert errors(EV, ex)
    ex = _example()
    ex["actors"] = []
    assert errors(EV, ex)
    ex = _example()
    ex["actors"][0]["reaction"] = {"bogus": 1}
    assert errors(EV, ex)  # reaction follows M18 reaction.schema.json


def test_m23_overlay_example_and_world_layer() -> None:
    sec = M23[M23.index("### 6.3"):M23.index("### 6.4")]
    overlay = json.loads(re.search(r"```json\n(.*?)```", sec, re.S).group(1))
    A = "event/anchors.schema.json"
    assert errors(A, overlay) == []
    world = {"schema": "awr.anchors.v1", "schema_version": "1.0.0", "world_id": "oryahovo-crop1", "frame": "world", "units": "m",
             "coordinate_sha256": H, "license": "CC-BY-4.0", "attribution": "OSM contributors", "sources": [{"name": "osm", "sha256": H}],
             "anchors": [{"id": "plaza", "kind": "zone", "category": "plaza", "profile": "person", "polygon": [[0, 0], [10, 0], [10, 10]], "z": 1.0,
                          "source": "osm:way/123", "status": "auto", "quality": {"in_coverage": True, "bits": 0}}]}
    assert errors(A, world) == []
    bad = copy.deepcopy(world)
    del bad["anchors"][0]["source"]
    assert errors(A, bad)  # world layers carry provenance
    bad = copy.deepcopy(world)
    bad["remove"] = ["plaza"]
    assert errors(A, bad)  # remove only in overlays
    bad = copy.deepcopy(overlay)
    bad["anchors"][0]["kind"] = "door"
    assert errors(A, bad)  # door needs building
    bad = copy.deepcopy(overlay)
    bad["anchors"][0]["kind"] = "zone"
    assert errors(A, bad)  # zone needs polygon


def _tape() -> dict:
    return {
        "schema": "drone4d.tape.v1", "event_id": "abc-crossing-001", "event_version": 1, "event_sha256": H, "mode": "exogenous", "seed": 20261011,
        "tick_hz": 10, "n_ticks": 3600, "duration_ms": 360000,
        "record": {"bytes": 22, "endian": "le", "fields": [["x_mm", "i32"], ["y_mm", "i32"], ["z_mm", "i32"], ["yaw_cdeg", "i16"], ["vx_cmps", "i16"],
                                                           ["vy_cmps", "i16"], ["vz_cmps", "i16"], ["state", "u8"], ["container", "u8"]],
                   "states": {"0": "absent", "1": "moving", "2": "waiting", "3": "in_vehicle", "4": "inside", "5": "gone"}, "frame": "world-enu"},
        "codec": {"name": "col-delta-zstd@1", "block_ticks": 100},
        "actors": [{"idx": 0, "id": "person_A", "class": "person", "scripted": True, "profile": "person", "size_mm": [500, 300, 1760],
                    "appearance": {"asset": "CesiumMan", "top_rgb": [42, 93, 176], "height_mm": 1760}}],
        "world": {"id": "oryahovo-crop1", "content_version": "cv1", "raycast_sha256": H, "navmesh_sha256": {"person": H}, "anchors_sha256": H},
        "environment": {}, "env_track": [{"t_ms": 0, "op": {"kind": "env.set"}}], "comms": {"zones": []}, "anim": None, "realized": None,
    }


def test_tape_header_and_manifest() -> None:
    assert errors("event/tape.v1.schema.json", _tape()) == []
    bad = _tape()
    bad["record"]["bytes"] = 24
    assert errors("event/tape.v1.schema.json", bad)
    bad = _tape()
    bad["duration_ms"] = 360050
    assert errors("event/tape.v1.schema.json", bad)
    man = {"schema": "drone4d.tape_manifest.v1", "content_sha256": H, "body_sha256": H, "header_sha256": H, "file_sha256": H, "bytes": 1000,
           "n_actors": 1, "n_ticks": 3600, "header": _tape(),
           "provenance": {"generator": "awr.events.compile", "generator_version": "0.1.0", "recast_navigation": "0.39.0", "wasm_sha256": H,
                          "node": "22.11.0", "host": "dmax", "wall_ms": 1200, "rss_mb": 300.5, "created_at": "2026-10-17T12:00:00Z"},
           "report": {"arrivals": [], "boards": [], "warnings": [], "min_dist_m": {"scripted_scripted": None, "bg_scripted": 4.2}}}
    assert errors("event/tape_manifest.v1.schema.json", man) == []
    man["header"]["record"]["endian"] = "be"
    assert errors("event/tape_manifest.v1.schema.json", man)


def test_truth_graph_brief() -> None:
    truth = {"schema": "drone4d.event_truth.v1", "event_id": "abc-crossing-001", "event_version": 1, "truth_rev": 1, "tape_content_sha256": H,
             "dsl_sha256": H, "strict": True, "detector_params": {"proximity_encounter": {"dist_m": 3, "min_dur_s": 5}},
             "events": [{"id": "encounter_AB", "type": "proximity_encounter", "participants": ["person_A", "person_B"], "interval_ms": [200000, 212000],
                         "declared": True, "annotation": "encounter_AB", "salience": 1, "in_incomplete_region": False, "evidence": {"min_dist_m": 1.2}}],
             "annotation_checks": [{"id": "encounter_AB", "type": "proximity_encounter", "window_ms": [195000, 250000], "verified": True,
                                    "computed_ms": [[200000, 212000]], "n_candidates": 1}],
             "relation_checks": [{"src": "encounter_AB", "rel": "before", "dst": "board_BC", "verified": True}],
             "skyvis_1hz": {"person_A": [0.812, None]}, "consistency": {}, "perf": {"rays": 1000}}
    assert errors("event/truth.v1.schema.json", truth) == []
    truth["events"][0]["interval_ms"] = [1]
    assert errors("event/truth.v1.schema.json", truth)
    graph = {"schema": "drone4d.event_graph.v1", "event_id": "abc-crossing-001", "event_version": 1, "truth_rev": 1,
             "nodes": [{"id": "person_A", "kind": "entity", "class": "person", "scripted": True},
                       {"id": "encounter_AB", "kind": "event", "type": "proximity_encounter", "interval_ms": [200000, 212000], "declared": True}],
             "edges": [{"src": "person_A", "rel": "participates_in", "dst": "encounter_AB"}]}
    assert errors("event/graph.v1.schema.json", graph) == []
    graph["nodes"][0]["kind"] = "actor"
    assert errors("event/graph.v1.schema.json", graph)
    brief = {"schema": "drone4d.event_brief.v1", "event_id": "abc-crossing-001", "event_version": 1, "truth_rev": 1,
             "event_type": "rendezvous_handoff", "public_anchors": [{"id": "plaza", "kind": "zone", "pos_enu_m": [100, -100, 2]}],
             "targets": [{"actor": "person_A", "description_zh": "蓝色上衣", "description_en": "Blue top"}],
             "task_area": [[0, 0], [300, 0], [300, -300], [0, -300]]}
    assert errors("event/brief.v1.schema.json", brief) == []
    brief["targets"][0]["route"] = []
    assert errors("event/brief.v1.schema.json", brief)  # briefs never carry routes or times
