"""网格 LiDAR（M25 §6.8；M25-FR-070）：逐点时刻、静态与演员求交、回波与真值槽位、确定性与噪声档（CPU 路径，合成世界）。"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("open3d")
pytest.importorskip("PIL")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "world"))

from awr.sensorsvc.lidar.mesh import INST_NONE, LIDAR_RECORD, MeshLidar
from awr.sensorsvc.lidar.model import LidarModel, load_lidar_model
from awr.sim.events.tape import ActorStates
from awr.sim.sensors.frames import mount_R


@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory):
    from synthworld import build_synth_world

    tmp = tmp_path_factory.mktemp("lw")
    mp = pytest.MonkeyPatch()
    mp.setenv("AWR_D3_ENABLED", "1")
    yield build_synth_world(tmp, mp), tmp
    mp.undo()


@pytest.fixture(scope="module")
def q(synth):
    from awr.world.mesh.query import open_mesh_world_query

    w, tmp = synth
    return open_mesh_world_query(w, tmp / "cache")


R_INV = mount_R((180.0, 0.0, 0.0))                                   # 倒装：雷达系 z 朝下


def nodes(p0, v=(0.0, 0.0, 0.0), K: int = 11):
    return [(R_INV, np.asarray(p0, float) + np.asarray(v, float) * 0.01 * k) for k in range(K)]


def world_points(sc, nds, model: LidarModel) -> np.ndarray:
    """按逐点时刻的传感器位姿把点还原到世界系（与扫描同一插值）。"""
    from awr.sensorsvc.lidar.mesh import _quat, _quat_R

    t = sc.points["offset_time"].astype(np.int64)
    seg_ns = model.frame_ns // (len(nds) - 1)
    s = np.minimum(t // seg_ns, len(nds) - 2)
    u = (t - s * seg_ns) / seg_ns
    P = np.array([p for _, p in nds])
    Q = np.array([_quat(R) for R, _ in nds])
    o = P[s] * (1 - u)[:, None] + P[s + 1] * u[:, None]
    qq = Q[s] * (1 - u)[:, None] + Q[s + 1] * u[:, None]
    qq /= np.linalg.norm(qq, axis=1, keepdims=True)
    xyz = np.stack([sc.points["x"], sc.points["y"], sc.points["z"]], 1).astype(np.float64)
    return np.einsum("nij,nj->ni", _quat_R(qq), xyz) + o


def test_model_from_m21_file() -> None:
    m = load_lidar_model()
    assert (m.slots_per_frame, m.point_dt_ns, m.hard_max_m, m.frame_ns) == (20000, 5000, 100.0, 100_000_000)
    assert m.r_max(np.array([0.1]))[0] == pytest.approx(40.0) and m.r_max(np.array([0.8]))[0] == pytest.approx(70.0, rel=0.02)


def test_static_ground_box_and_slot_truth(q) -> None:
    m = load_lidar_model()
    lid = MeshLidar(q, m)
    nd = nodes((20.0, -40.0, 8.0))                                       # 盒子西面 10 m 外、离地 8 m
    sc = lid.scan(3, 1_000_000_000, nd, tier="clean")
    assert sc.points.dtype == LIDAR_RECORD and sc.gt_inst.shape == (20000,) and 5000 < len(sc.points) < 20000
    w = world_points(sc, nd, m)
    ground = np.abs(w[:, 2]) < 1e-3
    box = (w[:, 0] > 29.99) & (w[:, 0] < 50.01) & (w[:, 1] > -50.01) & (w[:, 1] < -29.99) & (w[:, 2] > -1e-3)
    assert (ground | box).all() and (box & ~ground).sum() > 100 and ground.sum() > 1000
    assert (np.diff(sc.points["offset_time"].astype(np.int64)) > 0).all() and (sc.points["line"] == (sc.points["offset_time"] // 5000) % 4).all()
    ret = sc.gt_inst != INST_NONE
    assert ret.sum() == len(sc.points) and (sc.gt_inst[ret] == 0).all()
    assert np.array_equal(sc.points, lid.scan(3, 1_000_000_000, nd, tier="clean").points)


def test_per_point_time_keeps_motion_distortion(q) -> None:
    """竖直爬升 5 m/s：按逐点时刻位姿还原的地面点 z 精确为 0；若按帧首位姿还原，误差达到 0.5 m 量级。"""
    m = load_lidar_model()
    nd = nodes((-100.0, 60.0, 10.0), v=(0.0, 0.0, 5.0))
    sc = MeshLidar(q, m).scan(0, 0, nd, tier="clean")
    w = world_points(sc, nd, m)
    assert np.abs(w[:, 2]).max() < 1e-3
    flat = world_points(sc, [nd[0]] * 11, m)
    assert np.abs(flat[:, 2]).max() > 0.4


def test_actor_hits_and_occlusion(q) -> None:
    m = load_lidar_model()
    actors = [{"idx": 0, "id": "p0", "class": "person", "size_mm": [500, 300, 1750], "appearance": {}},
              {"idx": 1, "id": "v0", "class": "vehicle", "size_mm": [4500, 1800, 1500], "appearance": {}}]

    def st(t_ns: int) -> ActorStates:
        x = -100.0 + 1.0 * t_ns / 1e9                                    # 行人以 1 m/s 东行
        return ActorStates(np.array([0, 1]), np.array([[x, 66.0, 0.0], [-95.0, 54.0, 0.0]]), np.zeros(2), np.zeros((2, 3)),
                           np.array([1, 5], np.uint8), np.array([255, 255], np.uint8))

    nd = nodes((-100.0, 60.0, 6.0))
    sc = MeshLidar(q, m, actors).scan(0, 0, nd, actor_states=st, tier="clean")
    hit_p = sc.gt_inst == 1
    assert hit_p.sum() > 8 and sc.actor_hits == hit_p.sum() == sc.actor_rays and not (sc.gt_inst == 2).any()     # 离场车辆不参与
    assert ((sc.gt_flags[hit_p] & 0x3F) == 32).all()
    w = world_points(sc, nd, m)
    on_p = np.isin(sc.points["offset_time"] // 5000, np.nonzero(hit_p)[0])
    assert (w[on_p, 2] > 0.0).all() and (w[on_p, 2] < 1.76).all() and np.abs(w[on_p, 1] - 66.0).max() < 0.4
    far = MeshLidar(q, replace(m, r_at_rho10_m=6.0), actors).scan(0, 0, nd, actor_states=st, tier="realistic", seed=3)   # 检测极限附近
    n_act = int(((far.gt_inst > 0) & (far.gt_inst < 65534)).sum())
    assert far.actor_hits == n_act < far.actor_rays
    plain = MeshLidar(q, m).scan(0, 0, nd, tier="clean")
    assert len(plain.points) >= len(sc.points) - 5                       # 演员只遮挡地面，不凭空增加回波


def test_noise_tiers_determinism_and_detection(q) -> None:
    m = load_lidar_model()
    lid = MeshLidar(q, m)
    nd = nodes((-100.0, 60.0, 10.0))
    a = lid.scan(5, 0, nd, seed=7, uav_no=1, tier="realistic")
    b = lid.scan(5, 0, nd, seed=7, uav_no=1, tier="realistic")
    c = lid.scan(5, 0, nd, seed=8, uav_no=1, tier="realistic")
    assert np.array_equal(a.points, b.points) and not np.array_equal(a.points["x"][:100], c.points["x"][:100])
    w = world_points(a, nd, m)
    near = np.linalg.norm(w - nd[0][1], axis=1) < 19.0
    assert np.std(w[near, 2]) == pytest.approx(0.03, rel=0.3)                       # 近程 sigma 0.03 m（含 0.1° 角度噪声）
    far = lid.scan(5, 0, nodes((-100.0, 60.0, 60.0)), tier="clean")                   # 地面 rho 0.25：r_max ≈ 51 m，离地 60 m 时斜距 ≥ 76 m，无回波
    assert len(far.points) < 200
    loose = MeshLidar(q, replace(m, r_at_rho10_m=400.0))
    assert len(loose.scan(5, 0, nodes((-100.0, 60.0, 60.0)), tier="clean").points) > 1000


def test_capture_writes_lidar_frames_and_slot_truth(synth, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcap_protobuf")
    from test_capture import _StubCamera

    import awr.sensorsvc.camera.raster as raster
    import awr.sensorsvc.camera.scene as scene
    from awr.bench.recorder.lidar import unpack_lidar_frame, unpack_lidar_gt
    from awr.bench.recorder.read import FrameStore, TruthStore
    from awr.bench.runs_store import truth_paths
    from awr.sensorsvc import capture as cap

    w, _ = synth
    monkeypatch.setattr(raster, "RasterCamera", _StubCamera)
    monkeypatch.setattr(scene, "load_appearance", lambda d: None)
    spec = {"run_id": "cap-lidar", "event_id": "capture", "world_id": w.name, "duration_s": 0.5, "noise_profile": "clean",
            "fleet": [{"uav": "uav-01", "path": {"kind": "waypoints", "points": [[0, -100, 60, 8, 0], [1, -95, 60, 8, 0]]},
                       "cameras": [], "lidars": [{"name": "lidar0"}]}]}
    m = cap.run_capture(spec, tmp_path, worlds_dir=w.parent, log=lambda s: None)
    assert m["frames"] == 5 and m["fleet"][0]["lidars"][0]["mount"]["rpy_deg"] == [180.0, 0.0, 0.0]
    run = tmp_path / "dataset/capture/runs/cap-lidar"
    fs = FrameStore(run)
    assert fs.latest_seq("uav-01", "lidar0") == 4
    f = fs.frame("uav-01", "lidar0", 2)
    hdr, pts = unpack_lidar_frame(f.payload)
    assert f.t_ns == 200_000_000 and hdr["timebase_ns"] == 200_000_000 and hdr["point_count"] == len(pts) > 5000
    assert np.abs(np.linalg.norm(np.stack([pts["x"], pts["y"], pts["z"]], 1), axis=1)).min() > 7.9     # 离地 8 m：最近点即正下方附近
    ts = TruthStore(*truth_paths(run))
    gh, inst, _flags = unpack_lidar_gt(ts.get("uav-01", "lidar0", 2)[1])
    assert gh["slots"] == 20000 and len(gh["T_world_sensor_nodes"]) == 11 and (inst != 65535).sum() == len(pts)
    assert gh["T_world_sensor_nodes"][0]["t_ns"] == 200_000_000 and gh["T_world_sensor_nodes"][-1]["t_ns"] == 300_000_000
    fs.close()
    ts.close()
    import msgpack
    from mcap.reader import make_reader

    with (run / "raw.mcap").open("rb") as fh:
        blocks = [(ch.topic, ch.message_encoding, msgpack.unpackb(msg.data)) for _, ch, msg in make_reader(fh).iter_messages()
                  if "/imu/" in ch.topic]
    assert len(blocks) == 5 and all(t == "/uav/uav-01/imu/lidar0_imu" and e == "msgpack" for t, e, _ in blocks)
    b = blocks[2][2]
    assert (b["seq"], b["t0_ns"], b["dt_ns"], b["n"]) == (2, 200_000_000, 5_000_000, 20)
    acc = np.frombuffer(b["acc_mps2"], "<f4").reshape(-1, 3)
    assert np.allclose(acc.mean(0), [0.0, 0.0, -9.80665], atol=0.05)                        # 倒装：IMU z 朝下，匀速平飞读 −g
    assert m["fleet"][0]["lidars"][0]["imu"]["name"] == "lidar0_imu"
