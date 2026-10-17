"""契约 1.3.0 的 sensor/v2（AWR-05 §8.1、M25 §7.2；WP-D3-01）：frame.v1 三分区、render_req.v1、pose_history.v1 与 d3-semantic@1 码表。"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import CONTRACTS, errors, load

SHA = "0" * 64


def _frame() -> dict:
    return {
        "schema": "drone4d.frame.v1", "frame_id": "r1/uav01/cam0/42", "kind": "camera",
        "agent_view": {
            "sensor_id": "cam0", "seq": 42, "t_mid_ns": 4_200_000_000, "exposure_ns": 1_000_000, "readout_ns": 20_000_000,
            "width": 1280, "height": 720, "K": [1108.5, 0, 640, 0, 1108.5, 360, 0, 0, 1], "dist": [0, 0, 0, 0, 0],
            "pixel_convention": "colmap_center_0.5", "mount_nominal": {"xyz_m": [0.1, 0, -0.05], "rpy_deg": [0, 30, 0]},
            "gimbal_reading": {"az_deg": 0.02, "el_deg": -44.9}, "rgb": {"codec": "jpeg", "quality": 90, "ref": "rgb", "sha256": SHA},
        },
        "ground_truth": {
            "T_world_sensor": {"pos_enu_m": [10, 20, 60], "q_ws_xyzw": [0, 0, 0, 1], "frame": "ENU<-RDF"},
            "gimbal_true": {"az_deg": 0, "el_deg": -45},
            "instance": {"dtype": "u16", "codec": "zstd", "ref": "inst"}, "instance_unocc": {"dtype": "u16", "codec": "zstd", "ref": "inst_u"},
            "semantic": {"dtype": "u8", "codec": "zstd", "code_table": "d3-semantic@1", "ref": "sem"},
            "invalid_mask": {"dtype": "u8", "codec": "zstd", "bits": ["void_below_horizon", "stretched", "outside_coverage", "hole_edge", "water"],
                             "ref": "qual"},
            "objects": [{"inst": 1, "actor": "p01", "class": 32, "px_visible": 300, "px_unoccluded": 400, "h_px_unocc": 31, "h_px_visible": 25,
                         "visible_ratio": 0.75, "bbox_xywh": [600, 300, 12, 25], "bbox_unocc_xywh": [600, 294, 12, 31],
                         "occluder_sem_px": {"3": 100}, "flags": []}],
            "timing": {"t_truth_ns": [4_200_000_000], "blur_subframes": 1, "rs_bands": 1, "rs_method": "vertex"},
        },
        "internal": {
            "run_id": "r1", "session_id": "s1", "world_content_version": "w@1", "dynamic_source_id": SHA, "backend": "raster-glb",
            "renderer_version": "0.1.0", "fx_version": "0.1.0", "appearance": "mesh", "noise_tier": "standard", "noise_table_version": "1",
            "encoder": "nvimagecodec", "gpu_model": "A40", "driver": "550", "derived": False,
        },
    }


def test_frame_partitions_validate() -> None:
    v = "sensor/v2/frame.v1.schema.json"
    assert errors(v, _frame()) == []
    f = _frame()
    f["agent_view"]["depth"] = {"dtype": "f32", "unit": "m", "type": "planar_z", "range_m": [0.3, 500], "ref": "d", "sha256": SHA}
    assert errors(v, f) == []


@pytest.mark.parametrize("leak", ["T_world_sensor", "objects", "instance", "gimbal_true"])
def test_truth_fields_cannot_enter_agent_view(leak: str) -> None:
    f = _frame()
    f["agent_view"][leak] = copy.deepcopy(f["ground_truth"][leak])
    assert errors("sensor/v2/frame.v1.schema.json", f)


def test_run_seed_is_internal_and_rs_method_enumerated() -> None:
    v = "sensor/v2/frame.v1.schema.json"
    f = _frame()
    f["agent_view"]["run_seed"] = 7
    assert errors(v, f)
    f = _frame()
    f["internal"]["run_seed"] = 7
    f["ground_truth"]["timing"]["rs_method"] = "bands"
    assert errors(v, f) == []
    f["ground_truth"]["timing"]["rs_method"] = "rows"
    assert errors(v, f)


def _ph() -> dict:
    blk = lambda dt, c, r: {"dtype": dt, "cols": c, "ref": r}  # noqa: E731
    return {"schema": "drone4d.pose_history.v1", "uav": "uav01", "hz": 250, "frame": "ENU<-FLU", "n": 250,
            "t_ns": blk("i64", 1, "t"), "pos_enu_m": blk("f64", 3, "p"), "q_ws_xyzw": blk("f64", 4, "q"), "vel_enu_mps": blk("f32", 3, "v"),
            "omega_b_rps": blk("f32", 3, "w"), "f_b_mps2": blk("f32", 3, "f")}


def test_render_request_embeds_pose_history() -> None:
    v = "sensor/v2/render_req.v1.schema.json"
    r = {"schema": "drone4d.render_req.v1", "run_id": "r1", "session_id": "s1", "uav": "uav01", "sensor": "cam0", "seq": 42,
         "t_trigger_ns": 4_210_000_000, "exposure_ns": 1_000_000, "readout_ns": 20_000_000, "quality": "online", "outputs": ["rgb", "seg", "vis"],
         "pose_window": {"t_from_ns": 4_180_000_000, "t_to_ns": 4_210_000_000}, "dynamic_ref": {"kind": "tape", "id": SHA},
         "rig_ref": {"vehicle_ref": "p600", "sensor_no": 0, "model_ref": "rgb1080-rs"}}
    assert errors(v, r) == []
    r["pose_samples"] = _ph()
    assert errors(v, r) == []
    r["pose_samples"]["frame"] = "NED"
    assert errors(v, r)
    r.pop("pose_samples")
    r["outputs"] = ["rgb", "rgb"]
    assert errors(v, r)


def test_pose_history_layout() -> None:
    v = "sensor/v2/pose_history.v1.schema.json"
    assert errors(v, _ph()) == []
    bad = _ph()
    bad["q_ws_xyzw"]["cols"] = 3
    assert errors(v, bad)


def test_d3_semantic_table() -> None:
    t = load("classes/d3-semantic-v1.json")
    assert errors("classes/d3-semantic.schema.json", t) == []
    codes = {c["code"]: c["key"] for c in t["classes"]}
    anet = {c["index"]: c["key"] for c in json.loads((CONTRACTS / "classes/anet-classes-v1.json").read_text(encoding="utf-8"))["classes"]}
    assert {k: v for k, v in codes.items() if k <= 15} == anet
    assert {k: v for k, v in codes.items() if 32 <= k <= 37} == {32: "person", 33: "vehicle", 34: "dog", 35: "uav", 36: "machine", 37: "object"}
    assert codes[255] == "sky_or_void" and not [k for k in codes if 16 <= k <= 31 or 38 <= k <= 254]
