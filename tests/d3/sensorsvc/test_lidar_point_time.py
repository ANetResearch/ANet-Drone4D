"""网格 LiDAR 动态实体的逐点时刻（D3-AC-10 按 ADR-198 修订；M25 §6.8）：候选射线以自身槽位时刻 t_s + i·5 µs 取演员状态，
与相机同一磁带时刻函数（`TapeReader.actor_states`），实体位置与时刻函数之差 ≤ 1 mm；不以位姿节点段中点近似。

构造：穿越路口的行人（1.35 m/s，+x）、加速并转弯的车辆（≥ 8 m/s，+y）与帧内出现的背景行人（可见性阶跃）；
帧起点分对齐磁带帧与跨磁带帧边界两种。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("open3d")
pytest.importorskip("zstandard")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "world"))
sys.path.insert(0, str(Path(__file__).resolve().parent))                     # --import-mode=importlib 下引用同目录 test_mesh_lidar

from awr.sensorsvc.lidar import mesh as lm
from awr.sensorsvc.lidar.mesh import MeshLidar
from awr.sensorsvc.lidar.model import load_lidar_model

N_T = 40
ACTORS = [{"idx": 0, "id": "ped_cross", "class": "person", "scripted": True, "profile": "person", "size_mm": [500, 300, 1750],
           "appearance": {}},
          {"idx": 1, "id": "veh_cross", "class": "vehicle", "scripted": True, "profile": "vehicle", "size_mm": [4500, 1800, 1500],
           "appearance": {}},
          {"idx": 2, "id": "bg_ped", "class": "person", "scripted": False, "profile": "person", "size_mm": [500, 300, 1750],
           "appearance": {}}]
T_ALIGNED = 1_000_000_000                    # 帧起点与磁带帧对齐
T_CROSS = 1_137_500_000                      # 帧跨 1.2 s 磁带帧边界，背景行人在 1.2 s 出现


def _traj(t: np.ndarray) -> dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """各演员的解析轨迹 (位置, 速度, 航向)：行人 1.35 m/s 东行；车辆 v_y = 8 + 2t、v_x = 0.6t（加速、右转向偏东）；背景行人 1.4 m/s 南行。"""
    z = np.zeros_like(t)
    ped = (np.stack([-102.0 + 1.35 * t, z + 66.0, z], 1), np.stack([z + 1.35, z, z], 1), z)
    vx, vy = 0.6 * t, 8.0 + 2.0 * t
    veh = (np.stack([-95.0 + 0.3 * t * t, 40.0 + 8.0 * t + t * t, z], 1), np.stack([vx, vy, z], 1), np.arctan2(vy, vx))
    bg = (np.stack([z - 96.0, 64.0 - 1.4 * (t - 1.2), z], 1), np.stack([z, z - 1.4, z], 1), z - math.pi / 2)
    return {0: ped, 1: veh, 2: bg}


def _write_tape(d: Path) -> Path:
    from awr.sim.events import tape as tp

    t = np.arange(N_T) * 0.1
    f = np.zeros((N_T, 3), tp.RECORD)
    f["container"] = tp.NO_CONTAINER
    for a, (p, v, yaw) in _traj(t).items():
        f["x_mm"][:, a], f["y_mm"][:, a], f["z_mm"][:, a] = (np.round(p[:, k] * 1000).astype(np.int64) for k in range(3))
        f["vx_cmps"][:, a], f["vy_cmps"][:, a] = (np.round(v[:, k] * 100).astype(np.int64) for k in range(2))
        f["yaw_cdeg"][:, a] = np.round(np.degrees(yaw) * 100).astype(np.int64)
        f["state"][:, a] = 1
    f["state"][:12, 2] = 0                                                   # 背景行人自第 12 帧（1.2 s）出现
    h64 = "0" * 64
    hd = {"schema": "drone4d.tape.v1", "event_id": "ev-pt", "event_version": 1, "event_sha256": h64, "mode": "exogenous", "seed": 0,
          "tick_hz": 10, "n_ticks": N_T, "duration_ms": N_T * 100, "record": tp.record_spec(),
          "codec": {"name": "col-delta-zstd@1", "block_ticks": 100}, "actors": ACTORS,
          "world": {"id": "w1", "content_version": "0" * 12, "raycast_sha256": h64, "navmesh_sha256": {"person": h64},
                    "anchors_sha256": h64}, "environment": {}, "env_track": [], "comms": {}, "anim": None, "realized": None}
    h = tp.write_tape(d / "tape.bin", hd, f, n_scripted=2)
    (d / "tape.json").write_text(json.dumps({"content_sha256": h["content_sha256"]}))
    return d / "tape.bin"


@pytest.fixture(scope="module")
def tape(tmp_path_factory: pytest.TempPathFactory):
    from awr.sim.events.tape import TapeReader

    return TapeReader(_write_tape(tmp_path_factory.mktemp("pt")))


@pytest.fixture(scope="module")
def q(tmp_path_factory: pytest.TempPathFactory):
    from synthworld import build_synth_world

    from awr.world.mesh.query import open_mesh_world_query

    tmp = tmp_path_factory.mktemp("lw")
    mp = pytest.MonkeyPatch()
    mp.setenv("AWR_D3_ENABLED", "1")
    w = build_synth_world(tmp, mp)
    yield open_mesh_world_query(w, tmp / "cache")
    mp.undo()


def _slots(n: int, k: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.unique(np.concatenate([[0, 1, n // 2, n - 2, n - 1], rng.integers(0, n, k)]))


@pytest.mark.parametrize("t0", [T_ALIGNED, T_CROSS])
def test_track_equals_tape_time_function_at_every_point_time(tape, t0: int) -> None:
    """逐点状态与磁带时刻函数在该点整数时刻的直接求值一致：位置差 ≤ 1 µm（阈值 1 mm）、航向差 ≤ 1 µrad（车辆半长 2.25 m 处 ≤ 2.25 µm）、可见性相同。"""
    m = load_lidar_model()
    t_rel = np.arange(m.slots_per_frame, dtype=np.int64) * m.point_dt_ns
    tr = lm._ActorTrack(tape.actor_states, len(ACTORS), t0, t0 + int(t_rel[-1]))
    sl = _slots(m.slots_per_frame, 400, t0 % 997)
    t_ns = t0 + t_rel[sl]
    ref = [tape.actor_states(int(x)) for x in t_ns]
    for a in range(len(ACTORS)):
        pos, yaw, st = tr.eval(a, t_ns)
        P = np.array([r.pos_enu_m[a] for r in ref])
        Y = np.array([r.yaw_rad[a] for r in ref])
        S = np.array([r.state[a] for r in ref])
        assert np.array_equal(st, S)
        vis = np.isin(S, (1, 2))
        if not vis.any():
            continue
        assert np.linalg.norm(pos[vis] - P[vis], axis=1).max() <= 1e-6
        assert np.abs(lm._wrap(yaw[vis] - Y[vis])).max() <= 1e-6
    assert all(type(k) is int for k in tr.samples)                            # 缓存键为整数 t_ns
    if t0 == T_ALIGNED:
        assert tr.calls == 6 and tr.direct == 0                               # 帧内无磁带帧边界：一个区间、6 次查询
    else:
        assert not tr.frame_steady()[1:].any() and tr.calls <= 64             # 跨边界：二分定位边界，查询次数有界
        assert np.isin(tr.eval(2, np.array([t0, 1_199_999_999, 1_200_000_000, t0 + int(t_rel[-1])]))[2], (1, 2)).tolist() == \
            [False, False, True, True]                                        # 可见性在 1.2 s 精确阶跃


def test_track_handles_none_and_non_polynomial_time_functions() -> None:
    """时刻函数返回 None 时演员不可见；非多项式（正弦）轨迹二分至最小区间后逐时刻直接求值，结果与直接调用逐位相同。"""
    from awr.sim.events.tape import ActorStates

    def fn(t_ns: int):
        if t_ns < 30_000_000:
            return None
        x = 3.0 * math.sin(t_ns / 7e6)
        return ActorStates(np.array([0]), np.array([[x, 0.0, 0.0]]), np.zeros(1), np.zeros((1, 3)), np.array([1], np.uint8),
                           np.array([255], np.uint8))

    tr = lm._ActorTrack(fn, 1, 0, 99_995_000)
    t_ns = np.arange(0, 100_000_000, 5_000, dtype=np.int64)[::37]
    pos, _yaw, st = tr.eval(0, t_ns)
    assert np.array_equal(st, np.where(t_ns < 30_000_000, 0, 1))
    on = t_ns >= 30_000_000
    assert np.abs(pos[on, 0] - 3.0 * np.sin(t_ns[on] / 7e6)).max() <= 1e-6 and tr.direct > 0


def _surface_err(sc, nds, m, tape, a: int) -> tuple[int, float]:
    """演员 a 的回波点（世界系，按逐点传感器位姿还原）到该点时刻磁带位姿下代理网格表面的最大距离。"""
    import open3d as o3d
    from test_mesh_lidar import world_points

    from awr.sensorsvc.camera.actors import proxy_mesh

    slot = (sc.points["offset_time"] // m.point_dt_ns).astype(np.int64)
    on = sc.gt_inst[slot] == a + 1
    if not on.any():
        return 0, 0.0
    w = world_points(sc, nds, m)[on]
    t_ns = sc.t_start_ns + sc.points["offset_time"][on].astype(np.int64)
    loc = np.empty_like(w)
    for j, x in enumerate(t_ns):
        st = tape.actor_states(int(x))
        c, s = math.cos(st.yaw_rad[a]), math.sin(st.yaw_rad[a])
        r = w[j] - st.pos_enu_m[a]
        loc[j] = (c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2])
    P = np.ascontiguousarray(proxy_mesh(ACTORS[a])[0], np.float32)
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(P), o3d.core.Tensor(np.arange(len(P), dtype=np.uint32).reshape(-1, 3)))
    return int(on.sum()), float(scene.compute_distance(o3d.core.Tensor(loc.astype(np.float32))).numpy().max())


@pytest.mark.parametrize("t0", [T_ALIGNED, T_CROSS])
def test_scan_actor_points_lie_on_actor_at_point_time(q, tape, t0: int) -> None:
    """端到端：clean 档回波点在其自身时刻的实体位姿下落在代理网格表面上（≤ 1 mm）；同一场景以节点段中点状态求交
    （修订前的口径）时行人偏差 > 1 mm、车辆 > 1 cm，说明本断言能区分两种口径。"""
    from test_mesh_lidar import nodes

    m = load_lidar_model()
    nd = nodes((-100.0, 60.0, 6.0), v=(0.5, 0.0, 0.0))
    lid = MeshLidar(q, m, ACTORS)
    sc = lid.scan(0, t0, nd, actor_states=tape.actor_states, tier="clean")
    assert np.array_equal(sc.points, MeshLidar(q, m, ACTORS).scan(0, t0, nd, actor_states=tape.actor_states, tier="clean").points)
    n_ped, e_ped = _surface_err(sc, nd, m, tape, 0)
    n_veh, e_veh = _surface_err(sc, nd, m, tape, 1)
    assert n_ped > 8 and n_veh > 30 and e_ped <= 1e-3 and e_veh <= 1e-3
    n_bg, e_bg = _surface_err(sc, nd, m, tape, 2)
    assert sc.actor_rays >= sc.actor_hits == n_ped + n_veh + n_bg
    if t0 == T_CROSS:                                                          # 背景行人只在 1.2 s 之后的点上出现
        slot = (sc.points["offset_time"] // m.point_dt_ns).astype(np.int64)
        assert n_bg > 0 and e_bg <= 1e-3 and (sc.t_start_ns + sc.points["offset_time"][sc.gt_inst[slot] == 3] >= 1_200_000_000).all()
    else:
        assert n_bg == 0
    seg_ns = m.frame_ns // (len(nd) - 1)

    def seg_mid(t_ns: int):                                                    # 修订前：节点段中点时刻的状态
        s = min((t_ns - t0) // seg_ns, len(nd) - 2)
        return tape.actor_states(t0 + s * seg_ns + seg_ns // 2)

    old = MeshLidar(q, m, ACTORS).scan(0, t0, nd, actor_states=seg_mid, tier="clean")
    assert _surface_err(old, nd, m, tape, 0)[1] > 1e-3 and _surface_err(old, nd, m, tape, 1)[1] > 1e-2
