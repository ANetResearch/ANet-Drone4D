"""`public` 档时间窗切片与 `basemap.rrd`（M27 §6.7.8 第 4 步、FR-082、FR-058/059；AWR-05 §6.1.5；D3-AC-20、21、25）。

- 关键事件：`key_events[]` > 已验证 `annotation_checks[]` 的首个计算区间 > `plan.json` 窗口；采集运行按 `event.offset_s` 换算到运行
  仿真时刻；与运行不相交的关键事件跳过，越界窗口平移到运行区间内；
- 切片（3 机 10 s，720p 相机 + 深度 + LiDAR）：单一 recording、只含观测层与相机、LiDAR 静态标定，不含底图、真值与种子；`public`
  档参数（640×360 @ 5 Hz H.264 crf 28、深度 320×180 @ 2 Hz、LiDAR ≤ 5,000 点 @ 5 Hz）；视频首样本为关键帧；≤ 64 MB；
- 体积降级：依次 crf + 2、RGB 2.5 Hz、LiDAR 3,000 点，至多 3 轮，仍超出 696；
- 底图：事件舞台裁剪、体素均值降采样、体素逐轮增大直至 ≤ 上限；资产层只含 `/world/**`、无时间数据；
- 切片与底图共用 `application_id` 与 `recording_id`，合并后为单一 recording 且 `rerun rrd verify` 通过；
- 保留期：缺省输出登记为 `slices` 派生物，到期由 `retention_gc` 清理，静态制品引用时 pinned 不清理，`--dry-run` 不删除；
- 命令行：`--jobs 3` 多进程分片、`--at` 指定窗口中点。
"""

from __future__ import annotations

import importlib.util
import io
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rerun")
pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")
pytest.importorskip("zstandard")

from awr.bench import retention as ret
from awr.bench.recorder import rerun_export as rx
from awr.bench.recorder.lidar import LIDAR_RECORD
from awr.bench.recorder.writer import RunWriter

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools/bench/d3/public_slice.py"
_spec = importlib.util.spec_from_file_location("public_slice", SCRIPT)
ps = importlib.util.module_from_spec(_spec)
sys.modules["public_slice"] = ps
_spec.loader.exec_module(ps)

NS = 1_000_000_000
PERIOD = 100_000_000
T_S = 12.0
UAVS = ("uav-01", "uav-02", "uav-03")
W_IMG, H_IMG = 1280, 720
K = [710.79, 0.0, 640.0, 0.0, 710.79, 360.0, 0.0, 0.0, 1.0]
N_PTS = 20_000
POINT_DT = 5_000
OFFSET_S = 200.0                                                              # 采集运行：采集时刻 t ↔ 事件时刻 200 + t
EVENT = "ev-test-001"
RUN_ID = "cap-ev-test-001"
ANCHORS = [{"id": "a_west", "kind": "point", "xy": [-40.0, -30.0]}, {"id": "a_east", "kind": "point", "xy": [50.0, 40.0]}]
STAGE = (-40.0 - 60.0, -30.0 - 60.0, 50.0 + 60.0, 40.0 + 60.0)              # compile.stage_bbox：被引用锚点外扩 60 m
MB = 1024 * 1024


def _pos(uav: str, t_ns: int) -> np.ndarray:
    j = UAVS.index(uav)
    a = 0.05 * t_ns / NS + j * 2.1
    return np.array([120.0 * math.cos(a), 120.0 * math.sin(a), 80.0 + 10 * j])


def _jpeg(i: int, j: int) -> bytes:
    """有纹理且逐帧运动的 720p 画面（比真实航拍更难压缩，体积偏保守）。"""
    from PIL import Image

    yy, xx = np.mgrid[0:H_IMG, 0:W_IMG]
    a = np.empty((H_IMG, W_IMG, 3), np.uint8)
    a[..., 0] = ((xx + 7 * i) // 16 % 2 * 120 + yy * 100 // H_IMG).astype(np.uint8)
    a[..., 1] = ((yy + 5 * i) // 24 % 2 * 90 + xx * 120 // W_IMG).astype(np.uint8)
    a[..., 2] = ((xx // 40 + yy // 40 + i + j) % 5 * 50).astype(np.uint8)
    rng = np.random.default_rng(1000 * j + i)
    a[300:400, 200 + 8 * i:300 + 8 * i] = rng.integers(0, 255, (100, 100, 3), np.uint8)
    b = io.BytesIO()
    Image.fromarray(a).save(b, format="JPEG", quality=90)
    return b.getvalue()


def _depth(i: int) -> np.ndarray:
    d = np.linspace(40.0, 400.0, H_IMG, dtype=np.float32)[:, None] * np.ones((1, W_IMG), np.float32) + 0.1 * i
    d[:20, :] = np.inf                                                          # 天空
    return d


def _lidar(seq: int) -> np.ndarray:
    rng = np.random.default_rng(seq)
    p = np.zeros(N_PTS, LIDAR_RECORD)
    slots = np.sort(rng.choice(20_000, N_PTS, replace=False))
    p["x"], p["y"], p["z"] = rng.uniform(-60, 60, N_PTS), rng.uniform(-60, 60, N_PTS), rng.uniform(-80, 5, N_PTS)
    p["offset_time"] = (slots * POINT_DT).astype(np.uint32)
    p["reflectivity"] = rng.integers(0, 255, N_PTS, np.uint8)
    return p


def make_capture_run(run_dir: Path, *, t_s: float = T_S, nav: bool = True) -> Path:
    """合成采集运行（sensorsvc 形态）：3 机，12 s，相机 1280×720 @ 10 Hz（JPEG + 深度 + 真值帧 agent_view）、LiDAR 20,000 点 @ 10 Hz、
    IMU 200 Hz、导航解；清单 `event.offset_s = 200`。"""
    d = run_dir
    d.mkdir(parents=True)
    raw = RunWriter(d / "raw.mcap")
    tdir = d.parent.parent / "truth"
    tdir.mkdir(parents=True, exist_ok=True)
    tru = RunWriter(tdir / f"run_{d.name}.mcap")
    raw.metadata("run", {"run_id": d.name, "attempt": "1", "obs_level": "L2-depth", "ranked": "false"})
    raw.attachment("world_manifest.json", json.dumps({"contentVersion": "c0ffee000001"}).encode())
    n = int(t_s * NS // PERIOD)
    for k in range(n):
        t = k * PERIOD
        for j, u in enumerate(UAVS):
            p = _pos(u, t)
            sol = {"t_ns": t, "pos_enu_m": (p + np.array([0.7, -0.4, 1.1])).tolist(), "vel_enu_mps": [1.0, 0.0, 0.0], "q_wb_xyzw": [0.0, 0.0, 0.0, 1.0],
                   "omega_b_rps": [0.0, 0.0, 0.0], "pos_std_m": [1.2, 1.2, 2.0], "fix": "single", "battery_soc": 1.0 - 0.001 * k}
            if nav:
                raw.write_msgpack(f"/uav/{u}/obs/nav", "awr.bench.NavSolution", k, t, sol)
            tm = t + 5_000_000
            raw.write_camera(u, "cam0", k, tm, _jpeg(k, j), depth_m=_depth(k), period_ns=PERIOD)
            rec = {"schema": "drone4d.frame.v1", "frame_id": f"{d.name}/{u}/cam0/{k}", "kind": "camera",
                   "agent_view": {"sensor_id": "cam0", "seq": k, "t_mid_ns": tm, "width": W_IMG, "height": H_IMG, "K": K, "dist": [0.0] * 5,
                                  "mount_nominal": {"xyz_m": [0.1, 0.0, -0.05], "rpy_deg": [0.0, 0.0, 0.0]},
                                  "gimbal_reading": {"az_deg": 0.0, "el_deg": -30.0}},
                   "ground_truth": {"objects": []}}
            tru.write_camera_truth(u, "cam0", k, tm, rec, {})
            hdr = {"schema": "awr.sensor.lidar_frame.v2", "sensor_id": "lidar0", "frame_id": f"{d.name}/{u}/lidar0/{k}", "frame_seq": k,
                   "timebase_ns": t, "point_dt_ns": POINT_DT, "slots_per_frame": 20_000, "point_count": N_PTS,
                   "mount_nominal": {"xyz_m": [0.0, 0.0, -0.08], "rpy_deg": [180.0, 0.0, 0.0]}}
            raw.write_lidar(u, "lidar0", k, t, hdr, _lidar(1000 * j + k), period_ns=PERIOD)
            acc = np.tile([0.0, 0.0, 9.81], (20, 1)).astype("<f4")
            raw.write_imu(u, "lidar0_imu", k, t, {"sensor_id": "lidar0_imu", "seq": k, "t0_ns": t, "dt_ns": 5_000_000, "n": 20,
                                                   "acc_mps2": acc.tobytes(), "gyro_rps": np.zeros((20, 3), "<f4").tobytes()})
    raw.close()
    tru.close()
    man = {"schema": "drone4d.capture_manifest.v0", "run_id": d.name, "event_id": EVENT, "kind": "capture", "world_id": "synth-crop",
           "world_content_version": "c0ffee000001", "seed": 1, "event": {"event_id": EVENT, "version": 1, "offset_s": OFFSET_S}}
    (d / "run_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    return d


def make_event(ev_dir: Path, *, checks: list[dict] | None = None, key_events: list[dict] | None = None) -> Path:
    """事件版本目录：plan.json（标注窗口与引用锚点）、anchors.json、truth/r1/event_truth.json（门 3 计算区间）。"""
    (ev_dir / "truth" / "r1").mkdir(parents=True)
    plan = {"event_id": EVENT, "version": 1, "actors": [{"id": "person_A", "route": [{"at": "a_west", "via": []}, {"at": "a_east", "via": []}]}],
            "background": [], "annotations": [{"id": "encounter_AB", "type": "proximity_encounter", "participants": ["person_A"], "window": [195, 250]}]}
    (ev_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (ev_dir / "anchors.json").write_text(json.dumps({"schema": "drone4d.anchors.v1", "anchors": ANCHORS}), encoding="utf-8")
    checks = checks if checks is not None else [
        {"id": "encounter_AB", "type": "proximity_encounter", "verified": True, "computed_ms": [[203500, 206000], [208000, 209000]]},
        {"id": "board_BC", "type": "board_vehicle", "verified": True, "computed_ms": [[209000, 209000]]},
        {"id": "exit_AZ", "type": "exit_building", "verified": False, "computed_ms": [[205000, 205000]]},
        {"id": "late_D", "type": "zone_pass", "verified": True, "computed_ms": [[400000, 410000]]}]
    truth = {"schema": "drone4d.event_truth.v1", "event_id": EVENT, "annotation_checks": checks}
    if key_events is not None:
        truth["key_events"] = key_events
    (ev_dir / "truth" / "r1" / "event_truth.json").write_text(json.dumps(truth), encoding="utf-8")
    return ev_dir


def make_world(world_dir: Path, *, step: float = 0.5, half: float = 150.0) -> Path:
    """合成世界彩色点云（ANET_Q16 单根容器，sRGB 颜色随位置变化）：地面网格 + 一栋 20 m 高的建筑。"""
    from awr.world.pointcloud.tiler import tile

    g = np.arange(-half, half, step)
    X, Y = np.meshgrid(g, g)
    ground = np.stack([X.ravel(), Y.ravel(), np.zeros(X.size)], 1)
    zz, yy = np.meshgrid(np.arange(0, 20, step), np.arange(-10, 10, step))
    wall = np.stack([np.full(zz.size, 30.0), yy.ravel(), zz.ravel()], 1)
    P = np.concatenate([ground, wall])
    rgb = np.stack([(P[:, 0] + half) / (2 * half) * 255, (P[:, 1] + half) / (2 * half) * 255, P[:, 2] * 10 + 40], 1).astype(np.uint8)
    tile(P, None, np.zeros(len(P), np.uint8), world_dir / "visual" / "pointcloud", z_range=(0.0, 20.0), hag_range=None, nn_median_m=step, rgb=rgb)
    return world_dir


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("d3root")
    run = make_capture_run(root / "dataset" / EVENT / "runs" / RUN_ID)
    ev = make_event(root / "events" / EVENT / "v1")
    world = make_world(root / "worlds" / "synth-crop")
    return {"root": root, "run": run, "events_root": root / "events", "event_dir": ev, "world": world}


@pytest.fixture(scope="module")
def built(env: dict) -> dict:
    return ps.build_public(env["run"], events_root=env["events_root"], world_dir=env["world"])


# ---------------------------------------------------------------------- 纯函数


def test_key_events_priority_offset_and_window(tmp_path: Path) -> None:
    ev = make_event(tmp_path / "a")
    kes = ps.key_events(ev)
    assert [(k.id, k.a_s, k.b_s, k.source) for k in kes] == [("encounter_AB", 203.5, 206.0, "annotation_checks"),
                                                             ("board_BC", 209.0, 209.0, "annotation_checks"),
                                                             ("late_D", 400.0, 410.0, "annotation_checks")]   # 未验证的 exit_AZ 不计
    kev = make_event(tmp_path / "b", key_events=[{"id": "k2", "type": "turn", "interval": [12.0, 14.0]},
                                                 {"id": "k1", "type": "stop", "interval_ms": [5000, 7000]}])
    assert [(k.id, k.a_s, k.b_s, k.source) for k in ps.key_events(kev)] == [("k1", 5.0, 7.0, "key_events"), ("k2", 12.0, 14.0, "key_events")]
    pl = make_event(tmp_path / "c", checks=[])
    assert [(k.id, k.a_s, k.b_s, k.source) for k in ps.key_events(pl)] == [("encounter_AB", 195.0, 250.0, "plan")]
    W = 10 * NS
    assert ps.slice_window(50 * NS, (0, 90 * NS), W) == (45 * NS, 55 * NS)
    assert ps.slice_window(2 * NS, (0, 90 * NS), W) == (0, W)                    # 越界：平移到运行起点
    assert ps.slice_window(89 * NS, (0, 90 * NS), W) == (80 * NS + 1, 90 * NS + 1)   # 末条消息之后 1 ns 为右开端
    assert ps.slice_window(3 * NS, (0, 6 * NS), W) == (0, 6 * NS + 1)            # 运行短于窗口：整段
    base = rx.PROFILES["public"]
    lv = [ps.degrade(base, i) for i in range(4)]
    assert [(p.crf, p.rgb_hz, p.lidar_max_points) for p in lv] == [(28, 5.0, 5000), (30, 5.0, 5000), (30, 2.5, 5000), (30, 2.5, 3000)]
    assert all(p.layers == ("obs",) and p.rgb_max == (640, 360) and p.depth_hz == 2.0 for p in lv)
    with pytest.raises(ValueError):
        ps.degrade(base, 4)


def test_voxel_downsample_means_and_stage_bbox(env: dict) -> None:
    xyz = np.array([[0.1, 0.1, 0.0], [0.3, 0.2, 0.0], [1.2, 0.1, 0.0]])
    rgb = np.array([[10, 20, 30], [30, 40, 50], [200, 0, 0]], np.uint8)
    pos, col = ps.voxel_downsample(xyz, rgb, 1.0)
    order = np.argsort(pos[:, 0])
    assert np.allclose(pos[order], [[0.2, 0.15, 0.0], [1.2, 0.1, 0.0]]) and col[order].tolist() == [[20, 30, 40], [200, 0, 0]]
    p2, c2 = ps.voxel_downsample(xyz[::-1].copy(), rgb[::-1].copy(), 1.0)       # 与输入顺序无关
    assert np.allclose(np.sort(p2, 0), np.sort(pos, 0)) and sorted(c2.tolist()) == sorted(col.tolist())
    assert ps.stage_bbox_of(env["event_dir"]) == STAGE
    assert ps.stage_bbox_of(env["event_dir"], 5.0) == (STAGE[0] - 5, STAGE[1] - 5, STAGE[2] + 5, STAGE[3] + 5)


# ---------------------------------------------------------------------- 切片与底图


def test_public_slices_three_uav_10s(env: dict, built: dict) -> None:
    man = built
    assert man["ok"], man["failures"]
    assert man["schema"] == "drone4d.bench.public_slice.v1" and man["event_offset_ns"] == 200 * NS
    assert {s["id"]: s["reason"] for s in man["skipped"]} == {"late_D": "outside_run"}
    by = {s["key"]: s for s in man["slices"]}
    assert set(by) == {"encounter_ab", "board_bc"}
    # encounter：事件 [203.5, 206.0] s → 运行中点 4.75 s，窗口越界平移到 [0, 10) s；board：9.0 s → 平移到运行末端
    t_hi = man["run_t_range_ns"][1]
    assert by["encounter_ab"]["window_ns"] == [0, 10 * NS]
    assert by["board_bc"]["window_ns"] == [t_hi + 1 - 10 * NS, t_hi + 1]
    assert by["encounter_ab"]["event_t_from_s"] == 200.0 and by["encounter_ab"]["t_to_s"] == 10.0
    for key, s in by.items():
        p = Path(s["path"])
        assert p.parent == env["run"] / "derived" / "slices" and p.name == f"{key}.rrd"
        assert s["bytes"] == p.stat().st_size <= 64 * MB and s["level"] == 0 and s["rounds"][0]["profile"]["crf"] == 28
        assert s["verify"]["ok"], s["verify"]["problems"]
        assert rx.lint_rrd(p, "obs", run_id=RUN_ID, app_id=man["application_id"]) == []
        assert rx.verify_rrd([p])[0]
        inv = rx.scan_rrd(p)
        assert inv.n_recordings == 1 and inv.recording_id == RUN_ID and inv.application_id == "drone4d-d3-synth-crop-ev-test-001"
        assert inv.timelines == {"sim_time", "tick"}
        props = inv.properties
        assert props["layer"] == "obs" and props["uav"] == "uav-01,uav-02,uav-03" and props["slice"] == key and props["degrade_level"] == 0
        assert "seed" not in props                                              # 种子只在 truth / eval 层
        ents = set(inv.entities) | set(inv.static)
        assert not any(e.startswith(("/truth", "/eval", "/world/basemap")) for e in ents)
        assert not any("SegmentationImage" in a for a in [*inv.entities.values(), *inv.static.values()])
        for u in UAVS:
            U = f"/fleet/{u}"
            assert "Pinhole" in inv.static[f"{U}/cam0/image"] and "Transform3D" in inv.static[f"{U}/cam0"]   # 相机静态标定
            assert "Transform3D" in inv.static[f"{U}/lidar0"]                    # LiDAR 静态标定（机体 ← 传感器）
            ph = rx.read_entity(p, f"{U}/cam0/image")[0][1]
            assert ph["Pinhole:resolution"] == [[640.0, 360.0]]
            sh = s["shards"][u]["cameras"]["cam0"]
            assert sh["video"]["width"] == 640 and sh["video"]["height"] == 360 and sh["video"]["crf"] == 28
            assert sh["rgb_samples"] == 50 and sh["depth_frames"] == 20          # 10 s：5 Hz RGB、2 Hz 深度
            vid = [v for t, v in ps.merged_rows(rx.read_entity(p, f"{U}/cam0/image/rgb")) if t is not None and "VideoStream:sample" in v]
            assert len(vid) == 50 and vid[0]["VideoStream:is_keyframe"] in (True, [True])
            ts = [t for t, _ in ps.merged_rows(rx.read_entity(p, U)) if t is not None]
            assert s["window_ns"][0] <= min(ts) and max(ts) < s["window_ns"][1] and len(ts) == 100
            pts = [v for t, v in ps.merged_rows(rx.read_entity(p, f"{U}/lidar0")) if t is not None and "Points3D:positions" in v]
            assert len(pts) == 50 and max(len(v["Points3D:positions"]) for v in pts) == 5000
    idx = man["index"]
    assert idx["basemap"] == "basemap.rrd"
    assert [(x["event_id"], x["rrd"]) for x in idx["slices"]] == [("encounter_AB", "encounter_ab.rrd"), ("board_BC", "board_bc.rrd")]
    assert all(len(x["sha256"]) == 64 and x["bytes"] > 0 for x in idx["slices"])
    doc = json.loads((env["run"] / "derived" / "slices" / "slices.json").read_text(encoding="utf-8"))
    assert doc["index"] == idx and doc["ok"]


def test_basemap_stage_crop_and_merged_playback(env: dict, built: dict) -> None:
    bm = built["basemap"]
    p = Path(bm["path"])
    assert bm["bytes"] == p.stat().st_size <= 24 * MB and bm["verify"]["ok"], bm["verify"]
    assert bm["bbox_source"] == "event_stage" and bm["bbox_enu_m"] == list(STAGE) and bm["color"] == ["srgb"]
    assert bm["points_cropped"] < bm["points_total"]
    inv = rx.scan_rrd(p)
    assert inv.recording_id == RUN_ID and inv.application_id == built["application_id"] and inv.properties["layer"] == "asset"
    assert not inv.entities and "Points3D" in inv.static["/world/basemap"]      # 只有静态数据：任意播放时刻可见
    pos = np.asarray(rx.read_entity(p, "/world/basemap")[0][1]["Points3D:positions"])
    assert len(pos) == bm["points"]
    assert pos[:, 0].min() >= STAGE[0] and pos[:, 0].max() <= STAGE[2] and pos[:, 1].min() >= STAGE[1] and pos[:, 1].max() <= STAGE[3]
    mc = built["merged_check"]
    assert mc["ok"], mc["problems"]
    assert mc["n_recordings"] == 1 and mc["timelines"] == ["sim_time", "tick"]
    # 显式合并：切片 + 底图 → 单一 recording（Viewer 依次打开两个文件的合并语义），含机队与底图，verify 通过
    s0 = Path(built["slices"][0]["path"])
    out = env["root"] / "merged.rrd"
    ps._optimize([s0, p], out)
    m = rx.scan_rrd(out)
    assert m.n_recordings == 1 and {"/world/basemap", "/fleet/uav-01", "/fleet/uav-03/lidar0"} <= set(m.entities) | set(m.static)
    assert rx.verify_rrd([out])[0]


def test_basemap_voxel_grows_until_cap(env: dict, tmp_path: Path) -> None:
    src = rx.RunSource.open(env["run"])
    bm = ps.build_basemap(src, env["world"], tmp_path / "basemap.rrd", bbox=STAGE, cap_bytes=MB // 2, voxel_m=0.25)
    assert bm["bytes"] <= MB // 2 and len(bm["rounds"]) > 1 and bm["voxel_m"] > 0.25
    assert [r["voxel_m"] for r in bm["rounds"]] == sorted(r["voxel_m"] for r in bm["rounds"])
    assert ps.check_basemap(Path(bm["path"]), src) == []
    with pytest.raises(rx.RerunExportError) as ei:
        ps.build_basemap(src, env["world"], tmp_path / "none.rrd", bbox=(5000.0, 5000.0, 5100.0, 5100.0))
    assert ei.value.code == 696


def test_slice_degrade_rounds_then_696(env: dict, tmp_path: Path) -> None:
    src = rx.RunSource.open(env["run"])
    w = (4 * NS, 6 * NS)
    s0 = ps.build_slice(src, "probe", w, tmp_path / "probe.rrd")
    assert s0["level"] == 0 and len(s0["rounds"]) == 1
    cap = s0["bytes"] - 256                                                     # 同参数两次写出的字节数相差几个字节（RowId 压缩）
    s1 = ps.build_slice(src, "tight", w, tmp_path / "tight.rrd", cap_bytes=cap)
    assert s1["level"] >= 1 and s1["bytes"] <= cap < s1["rounds"][0]["bytes"] and abs(s1["rounds"][0]["bytes"] - s0["bytes"]) < 256
    assert s1["rounds"][1]["steps"] == ["crf+2"] and s1["rounds"][1]["profile"]["crf"] == 30
    with pytest.raises(rx.RerunExportError) as ei:
        ps.build_slice(src, "never", w, tmp_path / "never.rrd", cap_bytes=1)
    assert ei.value.code == 696 and [r["level"] for r in ei.value.detail["rounds"]] == [0, 1, 2, 3]
    last = ei.value.detail["rounds"][-1]["profile"]
    assert (last["crf"], last["rgb_hz"], last["lidar_max_points"]) == (30, 2.5, 3000)
    assert not (tmp_path / "never.rrd").exists() and not list(tmp_path.glob(".never-*"))


def test_capture_run_without_nav(tmp_path: Path) -> None:
    """采集运行（sensorsvc）不记录导航解：切片只有相机与 LiDAR 实体，`/fleet/<uav>` 无位姿，校验按各机首个时序实体判定窗口并告警。"""
    run = make_capture_run(tmp_path / "dataset" / EVENT / "runs" / "cap-nonav", t_s=3.0, nav=False)
    man = ps.build_public(run, at_s=[1.5], window_s=2.0, basemap=False, register=False, out=tmp_path / "out")
    assert man["ok"], man["failures"]
    assert len(man["warnings"]) == 3 and all("obs/nav" in w for w in man["warnings"])
    s = man["slices"][0]
    assert s["window_ns"] == [NS // 2, 5 * NS // 2] and s["verify"]["ok"]
    inv = rx.scan_rrd(Path(s["path"]))
    assert "/fleet/uav-02" not in inv.entities and "Pinhole" in inv.static["/fleet/uav-02/cam0/image"]


# ---------------------------------------------------------------------- 保留期与命令行


def test_slices_retention_registered_and_gc(env: dict, built: dict, tmp_path: Path) -> None:
    reg = built["retention"]
    assert reg["kind"] == "slices" and reg["retention"] == "standard"
    # 在独立数据根复制一份运行（只含清单与切片），以缩短时钟（1 天 = 1 min）执行清理
    root = tmp_path / "d3"
    run = root / "dataset" / EVENT / "runs" / RUN_ID
    run.mkdir(parents=True)
    shutil.copy(env["run"] / "run_manifest.json", run / "run_manifest.json")
    shutil.copytree(env["run"] / "derived" / "slices", run / "derived" / "slices")
    t0 = 1_800_000_000 * NS
    pol = ret.RetentionPolicy(day_ns=60 * NS)
    ret.register_run(run, retention="pinned", t0_unix_ns=t0)                    # 来源 Run 不清理，只看派生物级
    ret.register_derived(run, "slices", now=t0)
    assert ret.retention_gc(root, policy=pol, now=t0 + 29 * 60 * NS)["deleted"] == []          # standard 30 天未到期
    dry = ret.retention_gc(root, policy=pol, now=t0 + 31 * 60 * NS, dry_run=True)
    assert [Path(x["path"]).name for x in dry["deleted"]] == ["slices"] and (run / "derived" / "slices" / "basemap.rrd").is_file()
    ret.sync_references(root, "static:2026.10.18-1", [(RUN_ID, "slices")])      # 静态制品引用 → pinned
    assert ret.retention_gc(root, policy=pol, now=t0 + 31 * 60 * NS)["deleted"] == []
    ret.sync_references(root, "static:2026.10.18-1", [])                        # 引用撤销 → 回落 standard
    res = ret.retention_gc(root, policy=pol, now=t0 + 31 * 60 * NS)
    assert res["errors"] == [] and res["bytes"] > 0 and not (run / "derived" / "slices").exists()
    assert ret.expired_reason(run, "slices")["code"] == 697
    audit = list((root / "audit").glob("retention-*.jsonl"))
    assert audit and "derived_standard_expired" in audit[0].read_text(encoding="utf-8")


def test_cli_parallel_at_window(env: dict, tmp_path: Path) -> None:
    out = tmp_path / "cli"
    p = subprocess.run([sys.executable, str(SCRIPT), "--run", str(env["run"]), "--out", str(out), "--at", "6", "--window-s", "4", "--jobs", "3",
                        "--no-basemap", "--no-register"], capture_output=True, text=True, timeout=600,
                       env={"PYTHONPATH": str(ROOT / "python"), "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin", "CI": "1"})
    assert p.returncode == 0, p.stderr[-3000:]
    res = json.loads(p.stdout)
    assert res["ok"] and [s["key"] for s in res["slices"]] == ["t00006000ms"] and (res["slices"][0]["t_from_s"], res["slices"][0]["t_to_s"]) == (4.0, 8.0)
    doc = json.loads((out / "slices.json").read_text(encoding="utf-8"))
    assert doc["retention"] is None and doc["basemap"] is None and doc["slices"][0]["verify"]["ok"]
    assert doc["slices"][0]["key_event"]["source"] == "at" and doc["slices"][0]["event_t_from_s"] == 204.0
    bad = subprocess.run([sys.executable, str(SCRIPT), "--run", str(env["run"]), "--out", str(tmp_path / "bad")], capture_output=True, text=True,
                         timeout=120, env={"PYTHONPATH": str(ROOT / "python"), "CI": "1"})
    assert bad.returncode == 2 and "event-dir" in bad.stderr                   # 无事件目录且未给 --at
