"""样例包用例的合成正式轮次（tests/d3/bench/test_sample_pack.py 与 tests/d3/lab/test_samples_api.py 共用；按路径加载）。

`build_round(root)` 在 `root/out/<ROUND>/` 下写出 mbl_e2e 报告、evalstore 与 4 个代表运行（L1、L2 两组 × static_guard、track_share）：

- L1 运行只有导航解、L1 检测、动作与消息记录（与正式 L1 运行一致，没有相机通道）；
- L2 运行另有 2 机 `eo` 相机（Agent 收到的 JPEG，64×48，10 Hz）与 1 路不交付给 Agent 的相机（不得进入视频）；
- 运行目录带 `truth/run_seed.json`（秘密种子）、`truth/run_<id>.mcap`、`phys/` 与 Agent 容器日志，样例包不得安装；
- 动作流在第 5 步发出 look_at 云台指令（第 6 步生效），第 15 步发出 angles 指令。
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import numpy as np

ROUND = "sp-round"
EVENT = "crop-handoff-001"
WORLD = "oryahovo-crop1"
UAVS = ("uav-01", "uav-02")
N = 31                                     # 3 s × 10 Hz + 1
PERIOD = 100_000_000
SEEDS = [6_917_529_027_641_081_857, 1_234_567_890_123_456_789]
TAPE = b"D4DTAPE" + bytes(range(200))
TAPE_SHA = hashlib.sha256(TAPE).hexdigest()
K_EVAL = "ab" * 32
POLICIES = ("static_guard", "track_share")
START = {"uav-01": (10.0, -5.0, 3.0), "uav-02": (12.0, -5.0, 3.0)}
GIMBAL = {"pitch_range_deg": [-90.0, 30.0], "rate_max_dps": 90.0, "yaw_range_deg": [-150.0, 150.0]}


def jpeg(seed: int, w: int = 64, h: int = 48) -> bytes:
    from PIL import Image

    a = np.random.default_rng(seed).integers(0, 255, (h // 8, w // 8, 3), dtype=np.uint8).repeat(8, 0).repeat(8, 1)
    b = io.BytesIO()
    Image.fromarray(a).save(b, format="JPEG", quality=85)
    return b.getvalue()


def nav(u: str, k: int) -> dict:
    x, y, z = START[u]
    up = min(k, 10) * 2.0                                                     # 1 s 后起飞，爬升到 +20 m
    return {"t_ns": k * PERIOD, "pos_enu_m": [x + 0.5 * k, y, z + (up if k > 10 else 0.0)], "vel_enu_mps": [0.0 if k < 11 else 5.0, 0.0, 0.0],
            "q_wb_xyzw": [0.0, 0.0, 0.7071067811865476, 0.7071067811865476], "omega_b_rps": [0.0, 0.0, 0.0], "pos_std_m": [1.2, 1.2, 2.0],
            "fix": "single", "battery_soc": 1.0}


def action(k: int) -> dict:
    act: dict = {"k": k, "schema": "drone4d.bench.act.v1"}
    acks: list[dict] = []
    if k == 5:
        act["gimbal"] = {"mode": "look_at", "sensor_id": "eo", "target_enu_m": [40.0, -5.0, 0.0]}
        acks.append({"code": 0, "index": 0, "item": "gimbal", "k": k, "result": "applied"})
    if k == 15:
        act["gimbal"] = {"mode": "angles", "sensor_id": "eo", "pitch_deg": -10.0, "yaw_deg": 20.0}
        acks.append({"code": 0, "index": 0, "item": "gimbal", "k": k, "result": "applied"})
    return {"acks": acks, "act": act, "apply_tick": k + 1, "k": k}


def run_id(group: str, policy: str) -> str:
    return f"{ROUND}-{group.lower()}-{policy}-k0{len(policy) % 2}-p1"


def _config(u: str, group: str) -> dict:
    return {"schema": "drone4d.bench.agent_config.v1", "uav_id": u, "obs_level": group, "control_hz": 10,
            "sensors": [{"kind": "camera", "sensor_id": "eo", "gimbal": GIMBAL, "width": 64, "height": 48}],
            # 起始导航解高度与录制不一致（正式轮次的 p600_bench 即如此）：起降点高度须取首个导航解
            "start": {"kind": "pose", "nav_pos_enu_m": [*START[u][:2], START[u][2] - 25.0], "state": "landed"}}


def build_run(d: Path, rid: str, group: str, policy: str) -> dict:
    from awr.bench.recorder.frames_idx import build_frames_idx
    from awr.bench.recorder.writer import RunWriter

    d.mkdir(parents=True)
    with RunWriter(d / "raw.mcap") as w:
        w.metadata("run", {"run_id": rid, "attempt": "1", "obs_level": group, "ranked": "true"})
        w.attachment("tape.bin", TAPE, "application/octet-stream")
        w.attachment("tape.json", json.dumps({"schema": "drone4d.tape_manifest.v1", "content_sha256": TAPE_SHA}).encode())
        w.attachment("run_spec.json", json.dumps({"event": {"id": EVENT, "version": 1}, "obs_level": group}, sort_keys=True).encode())
        for k in range(N):
            t = k * PERIOD
            for u in UAVS:
                w.write_msgpack(f"/uav/{u}/obs/nav", "awr.bench.NavSolution", k, t, nav(u, k))
                if k < N - 1:
                    w.write_json(f"/uav/{u}/action", "drone4d.bench.act.v1", k, t, json.dumps(action(k), sort_keys=True).encode())
                if group == "L1":
                    w.write_json(f"/uav/{u}/obs/detections", "awr.bench.Detection[]", k, t, b"[]")
                else:
                    w.write_camera(u, "eo", k, t, jpeg(1000 * UAVS.index(u) + k), period_ns=PERIOD)
            if group == "L2":
                w.write_camera("uav-01", "slam_cam", k, t + 1, jpeg(9000 + k), period_ns=PERIOD, agent_delivered=False)
            if k % 4 == 0:
                w.write_json("/net/records", "awr.bench.net_record.v1", k, t + 2, json.dumps(
                    {"src": "uav-01", "dst": "uav-02", "ok": k < 20, "t_ns": t + 2, "bytes": 64}, sort_keys=True).encode())
    if group == "L2":
        build_frames_idx(d / "raw.mcap", d / "frames.idx")
    (d / "truth").mkdir()
    (d / "truth" / "run_seed.json").write_text(json.dumps({"run_seed": SEEDS[0]}))
    (d / "truth" / f"run_{rid}.mcap").write_bytes(b"truth")
    (d / "phys").mkdir()
    (d / "phys" / "run.sdf").write_text("<sdf/>")
    for u in UAVS:
        a = d / "agents" / u
        a.mkdir(parents=True)
        (a / "config.json").write_text(json.dumps(_config(u, group)))
        (a / "acts.jsonl").write_text("\n".join(json.dumps(action(k)) for k in range(N - 1)) + "\n")
        (a / "obs.sha256").write_text("0" * 64 + "\n")
        (a / "container.log").write_text("agent log\n")
    (d / "agents" / "obs_audit.json").write_text(json.dumps({"passed": True, "unknown_fields": 0}))
    (d / "net").mkdir()
    (d / "net" / "summary.json").write_text(json.dumps({"sent": 3}))
    (d / "manifest.json").write_text(json.dumps({"world": WORLD, "obs_level": group, "event": {"id": EVENT, "version": 1, "content_sha256": TAPE_SHA},
                                                 "policy": {"kind": "reference", "name": policy}, "fleet": [{"uav": u} for u in UAVS],
                                                 "recorder": {"raw": {"bytes": (d / "raw.mcap").stat().st_size}}}))
    (d / "run_manifest.json").write_text(json.dumps({"schema": "drone4d.bench.run_manifest.v1", "run_id": rid, "seed_commit": "e" * 64,
                                                     "ranked": True}))
    (d / "task.json").write_text(json.dumps({"event_ref": f"{EVENT}@1"}))
    (d / "reports.jsonl").write_text("")
    (d / "evaluation.json").write_text(json.dumps({"schema": "drone4d.bench.evaluation.v1", "run_id": rid, "inputs": {"raw_mcap_sha256": "d" * 64,
                                                                                                                       "tape_sha256": TAPE_SHA}}))
    ev_sha = hashlib.sha256((d / "evaluation.json").read_bytes()).hexdigest()
    return {"evaluation_sha256": ev_sha}


def build_round(root: Path) -> dict:
    """→ {report, evalstore, base, runs{(group, policy): run_dir}}。"""
    out = root / "out" / ROUND
    recs, reps, runs = [], [], {}
    for g in ("L1", "L2"):
        for pol in POLICIES:
            rid = run_id(g, pol)
            key = rid[len(ROUND) + 1:]
            d = out / "runs" / key
            info = build_run(d, rid, g, pol)
            runs[(g, pol)] = d
            comps = {"ec": 0.5 if pol == "static_guard" else 1.0, "eu": 0.0, "ao": 0.25, "ce": None if g == "L1" else 0.1}
            recs.append({"key": key, "run_id": rid, "group": g, "policy": pol, "seed_index": 1, "rep": 1, "status": "COMPLETED", "ranked_valid": True,
                         "sim_s": 3.0, "components": comps, "evaluation_sha256": info["evaluation_sha256"]})
            reps.append({"group": g, "policy": pol, "key": key, "run_id": rid, "run_dir": f"out/{ROUND}/runs/{key}"})
    rep = {"schema": "drone4d.bench.mbl_e2e.v1", "round": ROUND, "fingerprint": "f" * 64, "formal": True, "formal_passed": True,
           "event": {"id": EVENT, "version": 1, "tape_content_sha256": TAPE_SHA},
           "boards": {"L1": {"board_version": "sp-l1", "current": False}, "L2": {"board_version": "sp-l2", "current": True}},
           "runs": recs, "representative_runs": reps}
    (out / "mbl_e2e.json").write_text(json.dumps(rep, indent=1, sort_keys=True))
    es = out / "evalstore"
    es.mkdir()
    (es / "secrets.json").write_text(json.dumps({"schema": "drone4d.bench.evalstore.v1", "k_eval": K_EVAL, "seeds": {EVENT: SEEDS}}))
    return {"report": out / "mbl_e2e.json", "evalstore": es / "secrets.json", "base": root, "runs": runs}
