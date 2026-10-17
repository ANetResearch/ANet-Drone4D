"""契约 1.3.0 的 Agent 侧 bench 契约（AWR-05 §8.1、M26 §7.3–§7.7、§7.11；WP-D3-01）：obs.v1 白名单、act.v1、agent_config.v1、
obs_privileged.v1 与 wire.v1 帧表。"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import errors, load

ROOT = Path(__file__).resolve().parents[2]
OBS, ACT, CFG, PRIV, WIRE = (f"bench/{n}.schema.json" for n in ("obs.v1", "act.v1", "agent_config.v1", "obs_privileged.v1", "wire.v1"))
MOUNT = {"t_bs_m": [0.1, 0, -0.05], "q_bs_xyzw": [0, 0, 0, 1]}
GIM = {"roll_deg": 0, "pitch_deg": -45, "yaw_deg": 0}


def _cam() -> dict:
    return {"sensor_id": "cam0", "seq": 3, "t_mid_ns": 300_000_000, "exposure_ns": 1_000_000, "readout_ns": 20_000_000, "width": 1280, "height": 720,
            "K": [1108.5, 0, 640, 0, 1108.5, 360, 0, 0, 1], "dist": [0, 0, 0, 0, 0], "mount_nominal": MOUNT, "gimbal_reading": GIM,
            "rgb": {"codec": "jpeg", "data": b"\xff\xd8\xff\xd9"}}


def _obs() -> dict:
    return {"obs": {
        "schema": "drone4d.bench.obs.v1", "k": 3, "t_ns": 300_000_000,
        "nav": {"t_ns": 300_000_000, "pos_enu_m": [1, 2, 60], "vel_enu_mps": [0, 0, 0], "q_wb_xyzw": [0, 0, 0, 1], "omega_b_rps": [0, 0, 0],
                "pos_std_m": [1.5, 1.5, 3], "fix": "single", "battery_soc": 0.93},
        "status": {"mode": "airborne", "nav_cmd": "goto", "goto_reached": False, "contact": False, "safety": "normal", "locked_sensors": []},
        "ground_hint": {"dsm_m": 2.0, "dtm_m": 1.5},
        "sensors": {"cam0": [_cam()], "detections": []},
        "acks": [{"k": 2, "item": "nav", "index": 0, "result": "applied", "code": 0}],
        "comm": {"enabled": True, "bucket_B": 4096, "sent": 1, "dropped_budget": 0, "rejected": 0}, "dropped": {"cam0": 0},
        "term": {"terminated": False, "truncated": False, "reason": "none"}}, "inbox": []}


def test_observation_validates_with_binary_payloads() -> None:
    assert errors(OBS, _obs()) == []
    o = _obs()
    o["obs"]["sensors"]["imu0"] = [{"sensor_id": "imu0", "seq": 0, "t0_ns": 0, "dt_ns": 5_000_000, "n": 2, "acc_mps2": b"\0" * 24, "gyro_rps": b"\0" * 24,
                                    "mount_nominal": MOUNT}]
    o["obs"]["sensors"]["detections"] = [{"det_id": "0123456789abcdef", "sensor_id": "cam0", "seq": 3, "t_mid_ns": 300_000_000, "class": "person",
                                          "bbox_xywh": [1, 2, 3, 4], "h_px": 30, "slant_range_m": 80, "ground_xy_noisy": [5, 6]}]
    assert errors(OBS, o) == []


@pytest.mark.parametrize("leak", ["T_world_sensor", "objects", "instance", "run_seed", "frame_id", "renderer_version"])
def test_truth_and_internal_fields_are_rejected(leak: str) -> None:
    o = _obs()
    o["obs"]["sensors"]["cam0"][0][leak] = 1
    assert errors(OBS, o)


def test_camera_whitelist_is_a_projection_of_frame_agent_view() -> None:
    cam = set(load(OBS)["$defs"]["CameraObs"]["properties"])
    frame = load("sensor/v2/frame.v1.schema.json")["properties"]
    assert cam <= set(frame["agent_view"]["properties"])
    assert not cam & (set(frame["ground_truth"]["properties"]) | set(frame["internal"]["properties"]))


def test_privileged_fields_only_in_obs_privileged() -> None:
    o = _obs()
    o["oracle"] = {"actors": [], "true_pose": {"pos_enu_m": [0, 0, 0], "q_wb_xyzw": [0, 0, 0, 1]}}
    assert errors(OBS, o)
    assert errors(PRIV, o) == []


def test_actions() -> None:
    a = {"schema": "drone4d.bench.act.v1", "k": 3, "nav": {"mode": "velocity", "vel_mps": [1, None, 0], "yaw_rate_rps": None, "frame": "body"},
         "gimbal": {"sensor_id": "cam0", "mode": "look_at", "target_enu_m": [10, 10, 0]},
         "report": [{"type": "board_vehicle", "participants": [{"track_id": "t1", "evidence": [{"uav": "uav-01", "sensor": "cam0", "frame_seq": 3,
                                                                                          "bbox_xywh": [1, 2, 3, 4]}]}],
                     "window": {"t_start_ns": 0, "t_end_ns": 300_000_000}, "confidence": 0.7}]}
    assert errors(ACT, a) == []
    b = copy.deepcopy(a)
    b["report"][0]["participants"][0]["evidence"][0]["det_id"] = "0123456789abcdef"
    assert errors(ACT, b)  # exactly one evidence locator
    b = copy.deepcopy(a)
    b["nav"] = {"mode": "goto", "pos_enu_m": [1, 2], "accept_m": 1}
    assert errors(ACT, b)
    assert errors(ACT, {"schema": "drone4d.bench.act.v1", "k": 4}) == []


def test_agent_config_never_carries_run_identity() -> None:
    cfg = {"schema": "drone4d.bench.agent_config.v1", "uav_id": "uav-01", "kind": "uav", "peers": [],
           "vehicle": {"ref": "p600", "limits": {"vmax_h_mps": 12, "vup_mps": 4, "vdown_mps": 3, "yaw_rate_max_rps": 1.5, "acc_h_mps2": 3}, "endurance_s": 1800},
           "sensors": [{"sensor_id": "cam0", "kind": "camera", "model_ref": "rgb720-gs", "rate_hz": 10, "mount_nominal": MOUNT, "width": 1280, "height": 720}],
           "start": {"kind": "nest", "state": "landed", "nav_pos_enu_m": [0, 0, 0]}, "obs_level": "L2", "control_hz": 10,
           "decision": {"mode": "fixed", "tau_ms": 100, "tau_min_ms": 0, "step_budget_s": 1, "wall_ratio": 1},
           "comm": {"enabled": False, "bucket_Bps": 0, "burst_B": 0, "msg_max_B": 65536, "model": "fspl24_v1"}, "episode": {"duration_s": 360},
           "world": {"id": "oryahovo-crop1", "content_version": "cv1", "origin": {"lat_deg": 43.7, "lon_deg": 23.7, "alt_m": 30}},
           "brief": {"task": {"type": "event_capture", "description": "-"}, "area": {"polygon_enu_m": [[0, 0], [1, 0], [1, 1]], "alt_agl_range_m": [30, 120]},
                     "keepouts": [], "anchors": [], "targets": [], "report_vocab": ["board_vehicle"], "maps": {"dsm": "point"}, "nominal_noise": {}},
           "protocol": "drone4d.bench.wire.v1"}
    assert errors(CFG, cfg) == []
    for k in ("run_id", "run_seed", "ranked", "split", "event_id", "tape_content_sha256"):
        bad = copy.deepcopy(cfg)
        bad[k] = "x"
        assert errors(CFG, bad), k


def test_wire_frame_table_matches_m26() -> None:
    w = load(WIRE)
    m26 = next((ROOT / "docs/modules").glob("M26-*.md")).read_text(encoding="utf-8")
    sec = m26[m26.index("### 7.7"):m26.index("### 7.8")]
    rows = re.findall(r"^\| ([A-Z]+)(?: / ([A-Z]+))? \| 0x([0-9A-F]{2})(?: / 0x([0-9A-F]{2}))? \|", sec, re.M)
    doc = {}
    for a, b, ta, tb in rows:
        doc[a] = int(ta, 16)
        if b:
            doc[b] = int(tb, 16)
    assert {f["name"]: f["type"] for f in w["x-awr-frames"]} == doc
    assert len({f["type"] for f in w["x-awr-frames"]}) == len(w["x-awr-frames"])
    assert errors(WIRE, {"protocol": "drone4d.bench.wire.v1", "sdk_version": "0.1.0", "prefs": {"camera": "latest"}}) == []
    assert errors(WIRE, {"code": 666, "why": "decode"}) == []
    assert errors(WIRE, {"code": 666, "why": "decode", "message": "free text"})
