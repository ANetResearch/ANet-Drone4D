"""公开样例版本目录的制品生产（`tools/bench/d3/publish_static.py`；M27 publish_static；AWR-17 §18.8.5；D3-AC-21）。

- 公开运行 id：`run-[a-z2-7]{10}`（与前端解析器、`lab_static_index.v1` 一致），由内部 id 确定性派生，不同内部 id 不同；
- 轨迹改写：`tracks_obs.bin` 头的 `run_id` 改为公开运行 id，记录逐字节不变，尾部 sha256 重新计算；
- 相机位姿：与 Env L2 `_cam_pose` 同一约定（机体位置 + R_z(偏航)·挂载偏置；姿态 R_z(偏航)·gimbal_R(相对偏航, 俯仰)），
  真值位姿与云台角按时刻线性插值；
- 视频：10 Hz、PTS 为 tick 序号、首帧关键帧、GOP ≤ 10、无 B 帧（§18.8.5 规则 5）；
- 组装：版本目录布局与索引草稿（视频种类、轨迹与磁带路径），草稿可由 `labctl.py index` 消费。
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return sys.modules[name]


sp = _load("sample_pack", ROOT / "tools" / "bench" / "d3" / "sample_pack.py")
ps = _load("publish_static", ROOT / "tools" / "bench" / "d3" / "publish_static.py")

UAVS = ["uav-01", "uav-02"]
N = 21


def _tracks(run_id: str, pitch: float = -30.0, yaw_rel: float = 0.0) -> bytes:
    uavs = []
    for j, u in enumerate(UAVS):
        rows = [(float(k), float(j), 30.0, 0.0, 0.0, 0.0, 1.0, pitch + 0.5 * k, yaw_rel, 1.0, 3, 0) for k in range(N)]
        uavs.append({"uav_id": u, "primary_camera": "eo", "rows": rows})
    return sp.build_tracks(run_id, 10, N, uavs)


def _fleet(path: Path, yaw0: float = 0.0) -> Path:
    t = np.arange(N, dtype=np.int64) * ps.TICK_NS
    pos = np.zeros((N, len(UAVS), 3))
    pos[:, :, 0] = np.arange(N)[:, None] * 2.0
    pos[:, :, 2] = 40.0
    yaw = np.full((N, len(UAVS)), yaw0)
    yaw[:, 1] += np.linspace(0.0, 0.2, N)
    np.savez(path, uavs=np.array(UAVS), t_ns=t, pos=pos, vel=np.zeros_like(pos), yaw=yaw, nav=pos, status=np.zeros((N, len(UAVS))))
    return path


def test_public_run_id_pattern_and_determinism():
    a = ps.public_run_id("b10-formal-r2-l2-track_share-k04-p1")
    assert re.fullmatch(r"run-[a-z2-7]{10}", a)
    assert a == ps.public_run_id("b10-formal-r2-l2-track_share-k04-p1")
    assert a != ps.public_run_id("b10-formal-r2-l2-track_share-k05-p1")
    schema = json.loads((ROOT / "packages/contracts/bench/lab_static_index.v1.schema.json").read_text(encoding="utf-8"))
    assert re.fullmatch(schema["$defs"]["run"]["properties"]["run_id"]["pattern"].strip("^$"), a)


def test_retag_tracks_keeps_records(tmp_path):
    src = tmp_path / "tracks_obs.bin"
    src.write_bytes(_tracks("b10-formal-r2-l2-track_share-k04-p1"))
    out = ps._retag_tracks(src, "run-abcdefghij")
    a, b = sp.parse_tracks(src.read_bytes()), sp.parse_tracks(out)
    assert b["header"]["run_id"] == "run-abcdefghij"
    assert {k: v for k, v in a["header"].items() if k != "run_id"} == {k: v for k, v in b["header"].items() if k != "run_id"}
    assert a["rows"] == b["rows"]


def test_camera_pose_matches_env_l2_convention(tmp_path):
    from awr.sim.sensors.frames import gimbal_R

    tr = tmp_path / "tracks_obs.bin"
    tr.write_bytes(_tracks("x", pitch=-30.0, yaw_rel=10.0))
    fp = ps.FleetPoses(_fleet(tmp_path / "fleet.npz", yaw0=0.5), tr)
    R, p = fp.pose("uav-01", 3 * ps.TICK_NS)
    Rz = ps._rz(0.5)
    assert np.allclose(R, Rz @ gimbal_R(math.radians(10.0), math.radians(-30.0 + 1.5)))
    assert np.allclose(p, np.array([6.0, 0.0, 40.0]) + Rz @ np.asarray(ps.MOUNT_T_BS_M))
    R2, p2 = fp.pose("uav-02", 3 * ps.TICK_NS + ps.TICK_NS // 2)          # 两个 tick 之间：位置、偏航与俯仰线性插值
    yaw = 0.5 + 0.2 * 3.5 / (N - 1)
    assert np.allclose(p2, np.array([7.0, 0.0, 40.0]) + ps._rz(yaw) @ np.asarray(ps.MOUNT_T_BS_M))
    assert np.allclose(R2, ps._rz(yaw) @ gimbal_R(math.radians(10.0), math.radians(-30.0 + 1.75)))
    fwd = R[:, 0]                                                          # 光轴俯角 = 云台俯仰（第 3 tick 为 −28.5°）
    assert math.degrees(math.asin(fwd[2])) == pytest.approx(-28.5, abs=1e-9)


def test_tracks_must_be_10hz_and_cover_fleet(tmp_path):
    tr = tmp_path / "tracks_obs.bin"
    tr.write_bytes(sp.build_tracks("x", 10, N, [{"uav_id": "uav-01", "primary_camera": "eo",
                                                  "rows": [(0.0,) * 7 + (-30.0, 0.0, 1.0, 3, 0)] * N}]))
    with pytest.raises(ps.PublishError, match="uav-02"):
        ps.FleetPoses(_fleet(tmp_path / "fleet.npz"), tr)


def test_white_hot_window():
    t = np.array([[28000, 28500, 29000, 30000]], np.uint16)
    g = ps.white_hot(t, 28500.0, 29500.0)
    assert g.dtype == np.uint8
    assert g.tolist() == [[0, 0, 128, 255]]


def test_video_gop_pts_and_keyframes(tmp_path):
    pytest.importorskip("av")
    enc = ps.FrameEncoder(tmp_path / "v" / "uav-01_therm0.mp4", 64, 48, crf=30, preset="ultrafast", threads=1)
    for k in range(35):
        enc.push(k, np.full((48, 64), (k * 7) % 256, np.uint8))
    rec = enc.close()
    assert rec["frames"] == 35 and rec["first_pts"] == 0 and rec["duration_s"] == pytest.approx(3.5)
    pr = ps.probe(tmp_path / "v" / "uav-01_therm0.mp4")
    assert pr["frames"] == 35 and pr["first_keyframe"] and pr["monotonic"] and pr["max_gop"] == ps.GOP
    assert not (tmp_path / "v" / "uav-01_therm0.mp4.part").exists()


def test_assemble_layout_and_draft(tmp_path):
    run = tmp_path / "b10-formal-r2-l2-track_share-k04-p1"
    (run / "derived" / "event").mkdir(parents=True)
    (run / "derived" / "event" / "tape.bin").write_bytes(b"TAPE")
    (run / "derived" / "event" / "tape.json").write_text("{}", encoding="utf-8")
    (run / "derived" / "tracks_obs.bin").write_bytes(_tracks(run.name))
    media = tmp_path / "media" / "video"
    media.mkdir(parents=True)
    for u in UAVS:
        for cam in ("eo", "therm0"):
            (media / f"{u}_{cam}.mp4").write_bytes(b"MP4")
    lic = tmp_path / "LICENSE"
    lic.write_text("CC BY 4.0", encoding="utf-8")
    vdir = tmp_path / "pub" / "2026.10.25-1"
    rc = ps.main(["assemble", "--run", str(run), "--media", str(media.parent), "--vdir", str(vdir), "--event", "crop-handoff-001",
                  "--world", "oryahovo-crop1", "--policy", "track_share", "--obs-level", "L2", "--license", str(lic), "--notice", str(lic),
                  "--attribution", "ANetResearch"])
    assert rc == 0
    rid = ps.public_run_id(run.name)
    draft = json.loads((tmp_path / "pub" / "2026.10.25-1.draft.json").read_text(encoding="utf-8"))
    ev = draft["events"][0]
    assert ev["tape"] == "events/crop-handoff-001/tape.bin" and (vdir / ev["tape"]).read_bytes() == b"TAPE"
    r = ev["runs"][0]
    assert r["run_id"] == rid and r["n_uav"] == 2 and r["tracks_obs"] == f"runs/{rid}/tracks_obs.bin"
    assert sp.parse_tracks((vdir / r["tracks_obs"]).read_bytes())["header"]["run_id"] == rid
    assert sorted((v["uav"], v["cam"], v["kind"]) for v in r["videos"]) == [
        ("uav-01", "eo", "rgb"), ("uav-01", "therm0", "thermal"), ("uav-02", "eo", "rgb"), ("uav-02", "therm0", "thermal")]
    assert all((vdir / v["path"]).is_file() for v in r["videos"])
    assert draft["license"]["files"] == ["LICENSE", "NOTICE"] and (vdir / "NOTICE").is_file()
    assert ps.main(["assemble", "--run", str(run), "--media", str(media.parent), "--vdir", str(vdir), "--event", "crop-handoff-001",
                    "--world", "oryahovo-crop1", "--policy", "track_share", "--obs-level", "L2", "--license", str(lic),
                    "--notice", str(lic)]) == 2                              # 非空目录：拒绝覆盖
