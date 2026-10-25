"""MCAP → `.rrd` 离线导出（M27 §6.7.8、FR-075–078、081；AWR-05 §6.1.3–6.1.5；D3-AC-20、25、42 的导出部分）。

- 版本：rerun-sdk 锁定 0.38.1（requirements-d3.in / .lock 与运行时一致），段属性与清单记录 `rerun_version`；不同 minor 以 694 拒绝并
  给出迁移提示；`.rrd` 文件头版本可读；
- 命名：`application_id = drone4d-d3-<world>-<event>` 无点号，`recording_id = run_id`，路径段只用 `[a-z0-9_-]`，路径过滤只支持末尾
  `/**`；时间轴只有 `sim_time` 与 `tick`；
- 分层：每机一个观测分片 + truth + eval；`/fleet/<uav>` 为带噪导航解（与 `/uav/{id}/obs/nav` 相同、与真值相差噪声），真值位姿只在
  `/truth/**`，instance / semantic 只在 truth 层，种子只在 truth 与 eval 层；`rerun rrd verify` 与路径校验器全部通过；
- 校验器能识别点号 id、跨层实体、Pinhole 位置、他机实体、多余时间轴、非法字符与观测层分割图；
- 内容：Pinhole 与相机位姿（静态与逐帧云台）、u16 深度量化与越界计数、LiDAR 帧窗口结束时刻与逐点时刻、真值 LiDAR 逐点去畸变与
  语义码、`dataset` 档 H.264（无 B 帧、GOP 2 s、Annex B 带 SPS，可解码）、`public` 档缩放抽帧与点数上限且只含观测层、时间窗切片；
- 同一 MCAP 两次导出 `rerun rrd compare` 一致（离线 / 在线逐实体一致判据的基础），内容不同时不一致；命令行与多进程分片；
- Env L2-depth 运行（CPU 替身渲染）导出后校验通过，`/fleet/<uav>` 逐条等于 NavSolution，`/truth/fleet/<uav>` 等于真值状态。
"""

from __future__ import annotations

import importlib.util
import io
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rerun")
pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")
pytest.importorskip("zstandard")

from awr.bench.recorder import rerun_export as rx
from awr.bench.recorder.lidar import LIDAR_RECORD
from awr.bench.recorder.writer import RunWriter

ROOT = Path(__file__).resolve().parents[3]
T_S = 3.0
NS = 1_000_000_000
PERIOD = 100_000_000
UAVS = ("uav-01", "uav-02")
NAV_OFF = {"uav-01": (0.8, -0.5, 1.2), "uav-02": (-0.6, 0.9, -1.1)}          # 导航误差（确定），真值与观测之差
W_IMG, H_IMG = 800, 450
K = [500.0, 0.0, 400.0, 0.0, 500.0, 225.0, 0.0, 0.0, 1.0]
FAR_M = 3000.0
N_PTS = 6000
POINT_DT = 10_000                                                               # 10 µs / 槽位，10,000 槽位 / 帧
YAW_RATE = 0.5                                                                  # rad/s，真值 LiDAR 节点的偏航角速度
VEL = np.array([2.0, 1.0, 0.0])


def _truth_pos(uav: str, t_ns: int) -> np.ndarray:
    base = np.array([-10.0, 50.0, 30.0]) if uav == "uav-01" else np.array([10.0, 50.0, 30.0])
    return base + VEL * (t_ns / NS)


def _jpeg(i: int) -> bytes:
    from PIL import Image

    rng = np.random.default_rng(i)
    a = np.zeros((H_IMG, W_IMG, 3), np.uint8)
    a[..., 0] = (np.arange(W_IMG) * 255 // W_IMG)[None, :]
    a[..., 1] = (np.arange(H_IMG) * 255 // H_IMG)[:, None]
    a[..., 2] = (i * 37) % 255
    a[100:140, 100 + 10 * i:140 + 10 * i] = rng.integers(0, 255, (40, 40, 3), np.uint8)
    b = io.BytesIO()
    Image.fromarray(a).save(b, format="JPEG", quality=90)
    return b.getvalue()


def _depth(i: int) -> tuple[np.ndarray, int]:
    d = np.full((H_IMG, W_IMG), 50.0 + i, np.float32)
    d[0, :10] = 0.0                                                             # 无回波
    d[1, :5] = np.inf
    d[2, :3] = FAR_M + 1.0                                                      # 越界
    return d, 18


def _zstd(b: bytes) -> bytes:
    import zstandard

    return zstandard.ZstdCompressor(level=3).compress(b)


def _quat_z(yaw: float) -> list[float]:
    return [0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)]


def _lidar_points(seq: int) -> np.ndarray:
    rng = np.random.default_rng(100 + seq)
    p = np.zeros(N_PTS, LIDAR_RECORD)
    slots = np.sort(rng.choice(10_000, N_PTS, replace=False))
    p["x"], p["y"], p["z"] = rng.uniform(1, 40, N_PTS), rng.uniform(-10, 10, N_PTS), rng.uniform(-5, 5, N_PTS)
    p["offset_time"] = (slots * POINT_DT).astype(np.uint32)
    p["reflectivity"] = rng.integers(0, 255, N_PTS, np.uint8)
    return p


def _nodes(uav: str, t_s: int) -> list[dict]:
    """真值 world ← sensor 位姿节点：位置线性、偏航匀速（插值在节点间精确）。"""
    out = []
    for k in range(11):
        t = t_s + k * 10_000_000
        out.append({"t_ns": int(t), "pos_enu_m": _truth_pos(uav, t).tolist(), "q_ws_xyzw": _quat_z(YAW_RATE * t / NS)})
    return out


def make_run(root: Path, *, run_id: str = "run-0001", nav_scale: float = 1.0, ranked: bool = False) -> Path:
    """合成 Run 目录（sensorsvc 采集形态 + Env 通道）：2 机，3 s，相机 10 Hz（JPEG + L2-depth + 真值帧）、LiDAR 10 Hz（6,000 点 +
    槽位真值与位姿节点）、IMU 200 Hz、GNSS、导航解、L1 检测、链路记录、可见度归约。"""
    import msgpack

    d = root / run_id
    (d / "truth").mkdir(parents=True)
    raw = RunWriter(d / "raw.mcap")
    tru = RunWriter(d / "truth" / f"run_{run_id}.mcap")
    raw.metadata("run", {"run_id": run_id, "attempt": "1", "obs_level": "L2-depth", "ranked": str(ranked).lower()})
    spec = {"world": {"id": "Campus.V2"}, "event": {"id": "handoff.01"}, "clock_profile": {"name": "d1_250", "main_hz": 250},
            "policy": {"kind": "sdk"}, "physics": "kinematic", "noise": "realistic", "obs_level": "L2-depth", "ranked": ranked}
    if not ranked:
        spec["seed"] = 7
    raw.attachment("run_spec.json", json.dumps(spec).encode())
    raw.attachment("world_manifest.json", json.dumps({"contentVersion": "abc123def456"}).encode())
    raw.attachment("tape.bin", b"tape-bytes", "application/octet-stream")
    n = int(T_S * NS // PERIOD)
    for k in range(n):
        t = k * PERIOD
        for j, u in enumerate(UAVS):
            tp = _truth_pos(u, t)
            nav = {"t_ns": t, "pos_enu_m": (tp + nav_scale * np.array(NAV_OFF[u])).tolist(), "vel_enu_mps": VEL.tolist(),
                   "q_wb_xyzw": _quat_z(0.1 * j), "omega_b_rps": [0.0, 0.0, 0.0], "pos_std_m": [1.2, 1.2, 2.0], "fix": "single",
                   "battery_soc": 1.0 - 0.01 * k}
            raw.write_msgpack(f"/uav/{u}/obs/nav", "awr.bench.NavSolution", k, t, nav)
            tru.write_msgpack(f"/uav/{u}/state", "awr.bench.TruthState.v1", k, t,
                              {"t_ns": t, "pos_enu_m": tp.tolist(), "vel_enu_mps": VEL.tolist(), "yaw_rad": 0.0, "status": "AIRBORNE"})
            # 相机：t_mid = t + 5 ms
            tm = t + 5_000_000
            dep, _bad = _depth(k)
            raw.write_camera(u, "eo", k, tm, _jpeg(k), depth_m=dep, period_ns=PERIOD)
            inst = np.zeros((H_IMG, W_IMG), "<u2")
            inst[200:260, 300:330] = 5
            sem = np.full((H_IMG, W_IMG), 1, np.uint8)
            sem[200:260, 300:330] = 32
            gimbal = {"az_deg": 0.0, "el_deg": -30.0 if u == "uav-01" else -30.0 + k}
            rec = {"schema": "drone4d.frame.v1", "frame_id": f"{run_id}/{u}/eo/{k}", "kind": "camera",
                   "agent_view": {"sensor_id": "eo", "seq": k, "t_mid_ns": tm, "width": W_IMG, "height": H_IMG, "K": K, "dist": [0.0] * 5,
                                  "mount_nominal": {"xyz_m": [0.1, 0.0, -0.05], "rpy_deg": [0.0, 0.0, 0.0]}, "gimbal_reading": gimbal},
                   "ground_truth": {"instance": {"dtype": "u16", "codec": "zstd", "ref": "instance.u16.zst"},
                                    "semantic": {"dtype": "u8", "codec": "zstd", "ref": "semantic.u8.zst"},
                                    "objects": [{"inst": 5, "actor": "person_a", "px_visible": 1500 + k, "px_unoccluded": 1800}]}}
            tru.write_camera_truth(u, "eo", k, tm, rec, {"instance.u16.zst": _zstd(inst.tobytes()), "semantic.u8.zst": _zstd(sem.tobytes())})
            # LiDAR：帧窗口 [t, t + 100 ms]
            pts = _lidar_points(k)
            hdr = {"schema": "awr.sensor.lidar_frame.v2", "sensor_id": "lidar0", "frame_id": f"{run_id}/{u}/lidar0/{k}", "frame_seq": k,
                   "timebase_ns": t, "point_dt_ns": POINT_DT, "slots_per_frame": 10_000, "point_count": N_PTS,
                   "mount_nominal": {"xyz_m": [0.0, 0.0, 0.1], "rpy_deg": [0.0, 0.0, 0.0]}}
            raw.write_lidar(u, "lidar0", k, t, hdr, pts, period_ns=PERIOD)
            flags = (np.arange(10_000) % 7).astype(np.uint8)
            gt = {"schema": "awr.sensor.lidar_gt.v1", "frame_seq": k, "timebase_ns": t, "slots": 10_000, "T_world_sensor_nodes": _nodes(u, t)}
            tru.write_lidar_truth(u, "lidar0", k, t, gt, np.zeros(10_000, "<u2"), flags)
            # IMU 块（20 样本 @ 200 Hz）与 GNSS
            acc = np.tile([0.0, 0.0, 9.81], (20, 1)).astype("<f4")
            gyr = np.zeros((20, 3), "<f4")
            raw.write_imu(u, "lidar0_imu", k, t, {"sensor_id": "lidar0_imu", "seq": k, "t0_ns": t, "dt_ns": 5_000_000, "n": 20,
                                                   "acc_mps2": acc.tobytes(), "gyro_rps": gyr.tobytes(),
                                                   "mount_nominal": {"t_bs_m": [0.0, 0.0, 0.0], "q_bs_xyzw": [0.0, 0.0, 0.0, 1.0]}})
            raw.write_gnss(u, "gnss0", k, t, {"sensor_id": "gnss0", "seq": k, "t_ns": t, "pos_enu_m": nav["pos_enu_m"], "vel_enu_mps": [0, 0, 0],
                                               "fix": "single", "pos_std_m": [1.2, 1.2, 2.0]})
            det = [{"det_id": f"{k:08x}{j:08x}", "sensor_id": "eo", "seq": k, "t_mid_ns": tm, "class": "person", "bbox_xywh": [300, 200, 30, 60],
                    "h_px": 60, "slant_range_m": 40.0, "ground_xy_noisy": [1.0, 2.0]}]
            raw.write_json(f"/uav/{u}/obs/detections", "awr.bench.Detection[]", k, t, json.dumps(det).encode())
            tru.write_json(f"/eval/hidden/vis/{u}/eo", "awr.bench.l2_vis.v1", k, tm,
                           json.dumps({"uav": u, "sensor": "eo", "seq": k, "t_ns": tm, "actor": "person_b", "h_unocc_px": 30, "vis": 0.5}).encode())
        if k % 10 == 0:
            for i, (a, b) in enumerate((("uav-01", "uav-02"), ("uav-02", "uav-01"))):
                raw.write_json("/net/records", "awr.bench.net_record.v1", 2 * k + i, t + 1000,
                               json.dumps({"bytes": 28, "src": a, "dst": b, "lat_ms": 20.0 + i, "ok": True, "t_ns": t + 1000}).encode())
    raw.close()
    tru.close()
    (d / "manifest.json").write_text(json.dumps({"run_id": run_id, "world": "Campus.V2", "ranked": ranked,
                                                  "event": {"id": "handoff.01", "content_sha256": "e" * 64}}), encoding="utf-8")
    (d / "truth" / "run_seed.json").write_text(json.dumps({"run_seed": 7}), encoding="utf-8")
    _ = msgpack
    return d


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory) -> Path:
    return make_run(tmp_path_factory.mktemp("rr"))


@pytest.fixture(scope="module")
def review(run_dir: Path, tmp_path_factory) -> dict:
    return rx.export_run(run_dir, tmp_path_factory.mktemp("review"), profile="review", verify=True)


def _K(ph: dict) -> np.ndarray:
    return np.array(ph["Pinhole:image_from_camera"][0]).reshape(3, 3).T


def _vals(rows, comp):
    return [(t, v[comp]) for t, v in rows if comp in v]


# ---------------------------------------------------------------------- 版本与命名


def test_rerun_version_locked_and_migration_hint(review: dict) -> None:
    import rerun

    assert rerun.__version__ == rx.RERUN_LOCKED == "0.38.1"
    for f in ("requirements-d3.in", "requirements-d3.lock"):
        assert re.search(r"^rerun-sdk==0\.38\.1\b", (ROOT / f).read_text(encoding="utf-8"), re.M), f
    assert review["rerun_version"] == "0.38.1" and review["rerun_locked"] == "0.38.1"
    for f in review["files"].values():
        inv = rx.scan_rrd(Path(f["path"]))
        assert inv.version == "0.38.1" and inv.properties["rerun_version"] == "0.38.1"
    assert rx.check_version("0.38.0") is None and rx.check_version("0.38.9") is None
    h = rx.check_version("0.37.2")
    assert h["code"] == 694 and "rerun rrd migrate" in h["hint"]
    assert rx.check_version("0.39.0")["code"] == 694


def test_version_mismatch_refuses_export(run_dir: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(rx, "sdk_version", lambda: "0.37.2")
    with pytest.raises(rx.RerunVersionError) as e:
        rx.export_run(run_dir, tmp_path / "x", profile="review")
    assert e.value.code == 694 and "rerun rrd migrate" in e.value.detail["hint"]
    assert not (tmp_path / "x").exists()


def test_application_id_paths_and_filters() -> None:
    a = rx.application_id("Campus.V2", "handoff.01")
    assert a == "drone4d-d3-campus-v2-handoff-01" and "." not in a
    assert rx.path_part("UAV_01") == "uav_01" and rx.path_part("uav-01") == "uav-01"
    with pytest.raises(ValueError):
        rx.path_part("...")
    assert rx.sensor_map(["eo", "ir"], "cam") == {"eo": "cam0", "ir": "cam1"}
    assert rx.sensor_map(["lidar0", "lidar1"], "lidar") == {"lidar0": "lidar0", "lidar1": "lidar1"}
    rx.check_filter("/fleet/uav-01/**")
    rx.check_filter("/truth/fleet/uav-01")
    for bad in ("/fleet/*/cam0", "/fleet/**/image", "fleet/uav-01", "/fleet/UAV-01/**"):
        with pytest.raises(ValueError):
            rx.check_filter(bad)
    assert rx.Timebase(250).tick(1_000_000_000) == 250 and rx.Timebase(250).tick(3_999_999) == 0


# ---------------------------------------------------------------------- 分层、校验与属性


def test_review_export_layers_verify_and_lint(review: dict, run_dir: Path) -> None:
    assert set(review["files"]) == {"obs.uav-01", "obs.uav-02", "truth", "eval"}
    assert review["verify"] == {"ok": True, "problems": {}}
    assert review["application_id"] == "drone4d-d3-campus-v2-handoff-01" and review["recording_id"] == "run-0001"
    paths = [Path(f["path"]) for f in review["files"].values()]
    ok, out = rx.verify_rrd(paths)
    assert ok, out
    for k, f in review["files"].items():
        layer = "obs" if k.startswith("obs.") else k
        assert rx.lint_rrd(Path(f["path"]), layer, uav=f.get("uav"), run_id="run-0001", app_id=review["application_id"]) == []
        inv = rx.scan_rrd(Path(f["path"]))
        assert inv.application_id == review["application_id"] and inv.recording_id == "run-0001"
        assert inv.timelines == {"sim_time", "tick"}
        assert inv.properties["layer"] == layer and inv.properties["n_uav"] == 2
        assert inv.properties["world_content_version"] == "abc123def456" and inv.properties["tape_sha256"] == "e" * 64
        assert ("seed" in inv.properties) == (layer in ("truth", "eval"))       # 种子只进入评测 catalog 的层
    man = json.loads((Path(review["files"]["obs.uav-01"]["path"]).parent / "export.json").read_text(encoding="utf-8"))
    assert man["rerun_version"] == "0.38.1" and man["sensor_map"] == {"cam": {"eo": "cam0"}, "lidar": {"lidar0": "lidar0"}}
    assert man["warnings"] == [] and man["timelines"] == ["sim_time", "tick"]


def test_fleet_is_noisy_nav_and_truth_only_in_truth(review: dict) -> None:
    f = review["files"]
    for u in UAVS:
        obs = Path(f[f"obs.{u}"]["path"])
        rows = _vals(rx.read_entity(obs, f"/fleet/{u}"), "Transform3D:translation")
        assert len(rows) == 30
        for t, tr in rows:
            nav = _truth_pos(u, t) + np.array(NAV_OFF[u])
            assert np.allclose(tr[0], nav, atol=1e-4)                          # Agent 收到的带噪导航解
            assert np.linalg.norm(np.array(tr[0]) - _truth_pos(u, t)) > 1.0      # 与真值相差导航误差
        inv = rx.scan_rrd(obs)
        ents = set(inv.entities) | set(inv.static)
        assert not [e for e in ents if e.startswith("/truth") or e.startswith("/eval")]
        assert not [e for e, a in inv.entities.items() if "SegmentationImage" in a]
        other = next(x for x in UAVS if x != u)
        assert not [e for e in ents if e.startswith(f"/fleet/{other}")]
        tru = _vals(rx.read_entity(Path(f["truth"]["path"]), f"/truth/fleet/{u}"), "Transform3D:translation")
        assert len(tru) == 30 and all(np.allclose(tr[0], _truth_pos(u, t), atol=1e-4) for t, tr in tru)
    tinv = rx.scan_rrd(Path(f["truth"]["path"]))
    assert not [e for e, a in tinv.entities.items() if e.startswith("/fleet/") and "Transform3D" in a]
    assert {"/fleet/uav-01/cam0/image/instance", "/fleet/uav-01/cam0/image/semantic"} <= set(tinv.entities)
    seg = rx.read_entity(Path(f["truth"]["path"]), "/fleet/uav-01/cam0/image/semantic")
    assert len(seg) == 30
    assert f["truth"]["segmentation"] == 2 * 2 * 30


def test_camera_pinhole_pose_depth_and_lidar(review: dict) -> None:
    obs = Path(review["files"]["obs.uav-01"]["path"])
    cs = review["files"]["obs.uav-01"]["cameras"]["cam0"]
    assert cs["calib_source"] == "agent_view" and cs["cam_pose"] == "static" and cs["rgb_samples"] == 30
    ph = rx.read_entity(obs, "/fleet/uav-01/cam0/image")
    assert ph[0][0] is None                                                    # 静态
    assert np.allclose(_K(ph[0][1]).ravel(), K)                                 # Mat3x3 按列主序存储
    assert ph[0][1]["Pinhole:resolution"] == [[W_IMG, H_IMG]]
    pose = rx.read_entity(obs, "/fleet/uav-01/cam0")
    assert pose[0][0] is None and np.allclose(pose[0][1]["Transform3D:translation"][0], [0.1, 0.0, -0.05])
    cs2 = review["files"]["obs.uav-02"]["cameras"]["cam0"]
    assert cs2["cam_pose"] == "per_frame"                                      # 云台读数随帧变化
    assert len(_vals(rx.read_entity(Path(review["files"]["obs.uav-02"]["path"]), "/fleet/uav-02/cam0"), "Transform3D:quaternion")) == 30
    rgb = _vals(rx.read_entity(obs, "/fleet/uav-01/cam0/image/rgb"), "EncodedImage:blob")
    assert len(rgb) == 30 and rgb[0][0] == 5_000_000
    assert bytes(rgb[3][1][0]) == _jpeg(3)                                        # review 档：存档 JPEG 原样
    meter = math.floor(65535 / FAR_M)
    assert cs["depth_meter"] == meter and cs["depth_out_of_range"] == 30 * _depth(0)[1] and cs["depth_frames"] == 30
    dep = rx.read_entity(obs, "/fleet/uav-01/cam0/image/depth")
    assert dep[0][1]["DepthImage:meter"] == [float(meter)]
    lid = rx.read_entity(obs, "/fleet/uav-01/lidar0")
    assert lid[0][0] is None and np.allclose(lid[0][1]["Transform3D:translation"][0], [0.0, 0.0, 0.1])
    pts = [(t, v) for t, v in lid if t is not None and "Points3D:positions" in v]
    assert len(pts) == 30 and [t for t, _ in pts[:3]] == [PERIOD, 2 * PERIOD, 3 * PERIOD]  # 帧窗口结束时刻
    assert len(pts[0][1]["Points3D:positions"]) == N_PTS and pts[0][1]["offset_time"] == _lidar_points(0)["offset_time"].tolist()
    det = rx.read_entity(obs, "/fleet/uav-01/cam0/image/det")
    assert len(det) == 30 and det[0][1]["Boxes2D:labels"] == ["person"]
    imu = _vals(rx.read_entity(obs, "/fleet/uav-01/telemetry/lidar0_imu"), "Scalars:scalars")
    assert len(imu) == 30 * 20 and np.allclose(imu[0][1], [0, 0, 9.81, 0, 0, 0], atol=1e-5)
    assert len(_vals(rx.read_entity(obs, "/fleet/uav-01/telemetry/altitude"), "Scalars:scalars")) == 30


def test_truth_lidar_deskewed_world_points(review: dict) -> None:
    from awr.sim.sensors.frames import rot_z

    rows = [(t, v) for t, v in rx.read_entity(Path(review["files"]["truth"]["path"]), "/truth/fleet/uav-01/lidar0") if t is not None]
    assert len(rows) == 30
    k = 4
    t_end, v = rows[k]
    assert t_end == (k + 1) * PERIOD
    p = _lidar_points(k)
    xyz = np.stack([p["x"], p["y"], p["z"]], 1).astype(np.float64)
    tp = k * PERIOD + p["offset_time"].astype(np.int64)
    exp = np.stack([rot_z(YAW_RATE * t / NS) @ q + _truth_pos("uav-01", t) for q, t in zip(xyz, tp, strict=True)])
    assert np.allclose(np.array(v["Points3D:positions"]), exp, atol=2e-3)
    slots = p["offset_time"] // POINT_DT
    assert v["Points3D:class_ids"] == ((slots % 7) & 0x3F).tolist()


def test_eval_layer(review: dict) -> None:
    ev = Path(review["files"]["eval"]["path"])
    vis = _vals(rx.read_entity(ev, "/eval/visibility/uav-01/person_a"), "Scalars:scalars")
    assert len(vis) == 30 and vis[2][1] == [1502.0, 1800.0]
    assert len(_vals(rx.read_entity(ev, "/eval/visibility/uav-01/person_b"), "Scalars:scalars")) == 30
    lat = _vals(rx.read_entity(ev, "/eval/metrics/comm_latency_ms_uav-02_uav-01"), "Scalars:scalars")
    assert [x for _, x in lat] == [[21.0]] * 3


# ---------------------------------------------------------------------- 编码档位


def test_dataset_profile_h264(run_dir: Path, tmp_path: Path) -> None:
    import av

    man = rx.export_run(run_dir, tmp_path, profile="dataset", layers=["obs"], uavs=["uav-01"], verify=True)
    cs = man["files"]["obs.uav-01"]["cameras"]["cam0"]
    assert cs["video"] == {"codec": "H264", "width": W_IMG, "height": H_IMG, "gop_frames": 20, "crf": 23, "bframes": 0, "preset": "veryfast"}
    assert cs["rgb_samples"] == 30 and cs["keyframes"] == 2                    # GOP 2 s @ 10 Hz：第 0、20 帧
    rows = rx.read_entity(Path(man["files"]["obs.uav-01"]["path"]), "/fleet/uav-01/cam0/image/rgb")
    samp = [(t, bytes(v["VideoStream:sample"][0]), v["VideoStream:is_keyframe"][0]) for t, v in rows if t is not None]
    assert [t for t, *_ in samp] == [k * PERIOD + 5_000_000 for k in range(30)]
    first = samp[0][1]
    assert first[:4] == b"\x00\x00\x00\x01" and (first[4] & 0x1F) in (7, 9)  # Annex B，SPS（或 AUD 后接 SPS）
    assert b"\x00\x00\x01\x67" in first or b"\x00\x00\x00\x01\x67" in first
    assert [k for k, (_t, _b, key) in enumerate(samp) if key] == [0, 20]
    dec = av.CodecContext.create("h264", "r")
    frames = []
    for _t, b, _k in samp:                                                     # 每个样本即一个访问单元
        frames += dec.decode(av.Packet(b))
    frames += dec.decode(None)
    assert len(frames) == 30
    from PIL import Image

    ref = np.asarray(Image.open(io.BytesIO(_jpeg(0))).convert("RGB"), np.int16)
    got = frames[0].to_ndarray(format="rgb24").astype(np.int16)
    assert got.shape == ref.shape and np.abs(got - ref).mean() < 4.0           # 色彩与取值范围正确（全幅 JPEG → 有限幅 H.264）
    dep = rx.read_entity(Path(man["files"]["obs.uav-01"]["path"]), "/fleet/uav-01/cam0/image/depth")
    assert dep[0][1]["EncodedDepthImage:media_type"] == ["image/png"] and dep[0][1]["EncodedDepthImage:meter"] == [21.0]
    assert bytes(dep[0][1]["EncodedDepthImage:blob"][0])[:8] == b"\x89PNG\r\n\x1a\n"


def test_public_profile_scales_decimates_and_has_no_truth(run_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        rx.export_run(run_dir, tmp_path / "bad", profile="public", layers=["obs", "truth"])
    man = rx.export_run(run_dir, tmp_path, profile="public", verify=True)
    assert set(man["files"]) == {"obs.uav-01", "obs.uav-02"}                    # 只含观测层
    f = Path(man["files"]["obs.uav-01"]["path"])
    cs = man["files"]["obs.uav-01"]["cameras"]["cam0"]
    assert cs["video"]["width"] == 640 and cs["video"]["height"] == 360 and cs["video"]["crf"] == 28
    assert cs["rgb_samples"] == 15 and cs["depth_frames"] == 6                 # 10 Hz → 5 Hz；深度 2 Hz
    ph = rx.read_entity(f, "/fleet/uav-01/cam0/image")[0][1]
    assert ph["Pinhole:resolution"] == [[640.0, 360.0]]
    assert np.allclose(_K(ph), [[400.0, 0.0, 320.0], [0.0, 400.0, 180.0], [0.0, 0.0, 1.0]])
    dep = [v for t, v in rx.read_entity(f, "/fleet/uav-01/cam0/image/depth") if t is not None]
    fmt = dep[0]["DepthImage:format"][0]
    assert "DepthImage:buffer" in dep[0] and fmt["width"] == 320 and fmt["height"] == 180
    pts = [v for t, v in rx.read_entity(f, "/fleet/uav-01/lidar0") if t is not None and "Points3D:positions" in v]
    assert len(pts) == 16 and max(len(v["Points3D:positions"]) for v in pts) == 5000   # 帧尾 0.1–3.0 s 按 200 ms 分桶
    assert man["files"]["obs.uav-01"]["bytes"] < 64 << 20


def test_time_window_slice(run_dir: Path, tmp_path: Path) -> None:
    man = rx.export_run(run_dir, tmp_path, profile="review", window_ns=(NS, 2 * NS), verify=True)
    rows = _vals(rx.read_entity(Path(man["files"]["obs.uav-01"]["path"]), "/fleet/uav-01"), "Transform3D:translation")
    assert [t for t, _ in rows] == [k * PERIOD for k in range(10, 20)]
    tru = _vals(rx.read_entity(Path(man["files"]["truth"]["path"]), "/truth/fleet/uav-02"), "Transform3D:translation")
    assert len(tru) == 10 and man["window_ns"] == [NS, 2 * NS] and man["sim_s"] == 1.0


# ---------------------------------------------------------------------- 校验器反例


def _bad_rrd(path: Path, *, app: str = "drone4d-d3-w-e", entities=(), log_time: bool = False) -> Path:
    import rerun as rr

    rec = rr.RecordingStream(app, recording_id="run-0001", send_properties=False)
    rec.save(str(path))
    rr.set_log_time_enabled(log_time, recording=rec)
    rr.set_log_tick_enabled(False, recording=rec)
    rec.set_time("sim_time", duration=np.timedelta64(0, "ns"))
    for ent, *arch in entities:
        rr.log(ent, *arch, recording=rec)
    rec.flush()
    rec.disconnect()
    return path


def test_lint_rejects_violations(tmp_path: Path) -> None:
    import rerun as rr

    tf = rr.Transform3D(translation=[0, 0, 0])
    ph = rr.Pinhole(focal_length=100.0, width=64, height=48)
    cases = {
        "application_id": dict(app="drone4d.d3.w.e", entities=[("/fleet/uav-01", tf)]),        # SDK 改写为带哈希后缀的 id
        "/truth/** only in the truth layer": dict(entities=[("/truth/fleet/uav-01", tf)]),
        "Pinhole must sit on an '/image'": dict(entities=[("/fleet/uav-01/cam0", ph)]),
        "must not share an entity": dict(entities=[("/fleet/uav-01/cam0/image", ph, tf)]),
        "entity of another vehicle": dict(entities=[("/fleet/uav-02", tf)]),
        "timelines": dict(entities=[("/fleet/uav-01", tf)], log_time=True),
        "outside [a-z0-9_-]": dict(entities=[("/fleet/UAV-01", tf)]),
        "belongs to the truth layer": dict(entities=[("/fleet/uav-01/cam0/image/semantic", rr.SegmentationImage(np.zeros((4, 4), np.uint8)))]),
        "/eval/** only in the eval layer": dict(entities=[("/eval/metrics/x", rr.Scalars(1.0))]),
        "not part of the D3 path scheme": dict(entities=[("/misc/x", tf)]),
        "not an observation entity": dict(entities=[("/fleet/uav-01/cam0/image/foo", tf)]),
    }
    for i, (msg, kw) in enumerate(cases.items()):
        p = _bad_rrd(tmp_path / f"bad{i}.rrd", **kw)
        errs = rx.lint_rrd(p, "obs", uav="uav-01", run_id="run-0001", app_id="drone4d-d3-w-e")
        assert any(msg in e for e in errs), (msg, errs)
    p = _bad_rrd(tmp_path / "truthfleet.rrd", entities=[("/fleet/uav-01", tf)])
    assert any("truth layer carries only" in e for e in rx.lint_rrd(p, "truth"))
    p = _bad_rrd(tmp_path / "agent.rrd", entities=[("/agents/uav-01/belief", tf), ("/fleet/uav-01/cam0/image/det", rr.Boxes2D(array=[[0, 0, 1, 1]], array_format=rr.Box2DFormat.XYWH))])
    assert rx.lint_rrd(p, "agent", uav="uav-01") == []
    assert any("agent shard of uav-02" in e for e in rx.lint_rrd(p, "agent", uav="uav-02"))
    assert any("recording_id" in e for e in rx.lint_rrd(p, "agent", run_id="run-9999"))


# ---------------------------------------------------------------------- 一致性、命令行与并行


def test_reexport_compares_equal_and_detects_change(run_dir: Path, review: dict, tmp_path: Path) -> None:
    again = rx.export_run(run_dir, tmp_path / "again", profile="review")
    for k, f in review["files"].items():
        ok, out = rx.compare_rrd(Path(f["path"]), Path(again["files"][k]["path"]))
        assert ok, (k, out)
    other = make_run(tmp_path / "src2", nav_scale=1.5)
    changed = rx.export_run(other, tmp_path / "changed", profile="review", layers=["obs"], uavs=["uav-01"])
    ok, _ = rx.compare_rrd(Path(review["files"]["obs.uav-01"]["path"]), Path(changed["files"]["obs.uav-01"]["path"]))
    assert not ok


def test_cli_parallel_shards(run_dir: Path, review: dict, tmp_path: Path, capsys) -> None:
    rc = rx.main(["--from-mcap", str(run_dir), "--out", str(tmp_path / "o"), "--eval-out", str(tmp_path / "ev"), "--profile", "review",
                  "--jobs", "2", "--verify"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] and out["rerun_version"] == "0.38.1" and out["application_id"] == "drone4d-d3-campus-v2-handoff-01"
    assert (tmp_path / "ev" / "truth.rrd").is_file() and (tmp_path / "o" / "obs.uav-02.rrd").is_file()
    man = json.loads((tmp_path / "o" / "export.json").read_text(encoding="utf-8"))
    assert man["verify"]["ok"] and man["realtime_factor_min_shard"] is not None
    ok, msg = rx.compare_rrd(Path(review["files"]["obs.uav-02"]["path"]), tmp_path / "o" / "obs.uav-02.rrd")
    assert ok, msg


def test_ranked_run_obs_has_no_seed(tmp_path: Path) -> None:
    d = make_run(tmp_path, run_id="run-r1", ranked=True)
    man = rx.export_run(d, tmp_path / "o", profile="review", layers=["obs", "truth"], uavs=["uav-01"], verify=True)
    assert "seed" not in rx.scan_rrd(Path(man["files"]["obs.uav-01"]["path"])).properties
    assert rx.scan_rrd(Path(man["files"]["truth"]["path"])).properties["seed"] == 7


def test_dataset_layout_truth_beside_runs(tmp_path: Path) -> None:
    """M27 §5.6.4 数据集目录：`<event>/runs/<run>/raw.mcap` 与 `<event>/truth/run_<id>.mcap`；采集清单 run_manifest.json 给出身份。"""
    d = make_run(tmp_path / "handoff-01" / "runs", run_id="cap-1")
    (tmp_path / "handoff-01" / "truth").mkdir()
    (d / "truth" / "run_cap-1.mcap").rename(tmp_path / "handoff-01" / "truth" / "run_cap-1.mcap")
    (d / "manifest.json").unlink()
    (d / "run_manifest.json").write_text(json.dumps({"schema": "drone4d.capture_manifest.v0", "run_id": "cap-1", "event_id": "capture",
                                                     "kind": "capture", "world_id": "oryahovo-crop1", "seed": 1}), encoding="utf-8")
    man = rx.export_run(d, tmp_path / "o", profile="review", layers=["truth"], verify=True)
    assert man["application_id"] == "drone4d-d3-oryahovo-crop1-capture" and man["files"]["truth"]["poses"] == 60


# ---------------------------------------------------------------------- Env 运行


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


if importlib.util.find_spec("open3d") is not None:                             # Env 合成世界需要 open3d；缺失时只跳过本用例
    synth_event = _load("d3_env_conftest_for_rerun", ROOT / "tests" / "d3" / "env" / "conftest.py").synth_event
else:
    @pytest.fixture(scope="session")
    def synth_event():
        pytest.skip("open3d not installed (Env synthetic world)")


def test_env_l2_depth_run_exports(synth_event, tmp_path: Path) -> None:
    import msgpack
    from mcap.reader import make_reader

    t_rec = _load("d3_recorder_tests_for_rerun", ROOT / "tests" / "d3" / "bench" / "test_recorder.py")
    run = tmp_path / "run-env"
    t_rec._episode(synth_event, run, "L2-depth", duration_s=1.0)
    man = rx.export_run(run, tmp_path / "rrd", profile="dataset", verify=True)
    assert man["verify"]["ok"] and set(man["files"]) == {"obs.uav-01", "obs.uav-02", "truth", "eval"}
    assert man["application_id"] == rx.application_id(man["world"], man["event"]) and "." not in man["application_id"]
    nav, truth = {}, {}
    with (run / "raw.mcap").open("rb") as f:
        for _s, _ch, m in make_reader(f).iter_messages(topics=["/uav/uav-01/obs/nav"]):
            o = msgpack.unpackb(m.data)
            nav[o["t_ns"]] = o["pos_enu_m"]
    with next((run / "truth").glob("run_*.mcap")).open("rb") as f:
        for _s, _ch, m in make_reader(f).iter_messages(topics=["/uav/uav-01/state"]):
            o = msgpack.unpackb(m.data)
            truth[o["t_ns"]] = o["pos_enu_m"]
    rows = _vals(rx.read_entity(Path(man["files"]["obs.uav-01"]["path"]), "/fleet/uav-01"), "Transform3D:translation")
    assert len(rows) == len(nav) > 0 and all(np.allclose(tr[0], nav[t], atol=1e-4) for t, tr in rows)
    tr = _vals(rx.read_entity(Path(man["files"]["truth"]["path"]), "/truth/fleet/uav-01"), "Transform3D:translation")
    assert len(tr) == len(truth) and all(np.allclose(x[0], truth[t], atol=1e-4) for t, x in tr)
    assert any(not np.allclose(nav[t], truth[t]) for t in nav)                # 导航解带噪
    cs = man["files"]["obs.uav-01"]["cameras"]["cam0"]
    assert cs["sensor_id"] == "eo" and cs["video"]["codec"] == "H264" and cs["depth_frames"] > 0
    assert man["files"]["obs.uav-01"]["lidars"]["lidar0"]["frames"] > 0
