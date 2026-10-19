"""跨传感器时间一致（D3-AC-10，AWR-05 v1.6 按 ADR-198 修订；M23 §6.8、M25 §6.4.4、§6.8）。

构造：合成世界（tests/d3/world/synthworld，300 m 平地）上以 (0, 60) 为中心的路口；磁带（真实 `write_tape`/`TapeReader`，10 Hz）含
东行穿越路口的行人（1.35 m/s，约 1.5 s 过路口中心）、北行加速并略向东转向穿越路口的车辆（v_y = 8 + 1.2t，v_x = 0.3t，8 至
12.8 m/s，约 1.5 s 过路口中心），以及 1.2 s 出现、南行的背景行人（B 子块，可见性阶跃）。

判据（阈值逐条对照 §11.2 D3-AC-10 修订文本）：

1. 采集作业（`capture.run_capture`：两机、全局快门 10 fps 与卷帘 15 fps 相机、MID-360 LiDAR、clean 档、事件偏移 0.25 s 使 LiDAR 帧
   跨磁带帧边界）：每个相机帧绘制所用的演员位姿等于磁带时刻函数在该帧曝光中点 t_mid 的值（逐位相同）；LiDAR 写出的每个演员回波点
   按真值文件中的位姿节点还原到世界系后，落在该点自身时刻的磁带位姿下的代理网格表面上（≤ 1 mm）；同一时刻相机与 LiDAR 求交所用的
   演员位置之差 ≤ 1 mm；
2. 实体状态缓存（`capture.ActorClock`）：缓存键为整数 t_ns（断言，numpy 整数与浮点拒绝），只在时刻精确相同时复用，返回数组只读；
3. sim-core stage 次序（M24 两个 stage 换序，并插入一个在仿真 tick 时刻经 M18 运动源查询同一磁带读取器的 stage）、时钟档
   （d1_250 与 sandbox100；d1_250_l1_125 的 8 ms 物理步不整除 100 ms 控制步，Env 以 637 拒绝）与物理档（kinematic、fast）变化时，
   L2 相机与 LiDAR 读到的演员状态逐位不变；
4. GPU（dmax，EGL 光栅相机）：演员像素按平面深度反投影后落在 t_mid 磁带位姿下的代理网格表面上（≤ 1 mm）；以帧末时刻
   （t_mid + 4 ms）的磁带位姿判定时行人 > 1 mm、车辆 > 1 cm（负对照，说明判据能区分时刻口径）。
"""

from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

pytest.importorskip("open3d")
pytest.importorskip("zstandard")
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "world"))
sys.path.insert(0, str(HERE.parents[1] / "events"))

N_T = 40                                     # 磁带 4 s
OFFSET_NS = 250_000_000                      # 采集的事件偏移：LiDAR 帧 [t_s, t_s + 100 ms) 跨磁带帧边界
ACTORS = [{"idx": 0, "id": "ped_cross", "class": "person", "scripted": True, "profile": "person", "size_mm": [500, 300, 1750],
           "appearance": {}},
          {"idx": 1, "id": "veh_cross", "class": "vehicle", "scripted": True, "profile": "vehicle", "size_mm": [4500, 1800, 1500],
           "appearance": {}},
          {"idx": 2, "id": "bg_ped", "class": "person", "scripted": False, "profile": "person", "size_mm": [500, 300, 1750],
           "appearance": {}}]
T_BG_NS = 1_200_000_000                      # 背景行人出现时刻（磁带第 12 帧）
VIS = (1, 2)


def _traj(t: np.ndarray) -> dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """各演员的解析轨迹 (位置, 速度, 航向)，路口中心 (0, 60)：行人沿 y = 58.5 东行；车辆沿 x ≈ 3 北行加速并略向东转向；背景行人南行。"""
    z = np.zeros_like(t)
    ped = (np.stack([-2.0 + 1.35 * t, z + 58.5, z], 1), np.stack([z + 1.35, z, z], 1), z)
    vx, vy = 0.3 * t, 8.0 + 1.2 * t
    veh = (np.stack([3.0 + 0.15 * t * t, 47.0 + 8.0 * t + 0.6 * t * t, z], 1), np.stack([vx, vy, z], 1), np.arctan2(vy, vx))
    bg = (np.stack([z - 3.0, 63.0 - 1.4 * (t - 1.2), z], 1), np.stack([z, z - 1.4, z], 1), z - math.pi / 2)
    return {0: ped, 1: veh, 2: bg}


def _write_tape(d: Path, content_version: str) -> Path:
    from awr.sim.events import tape as tp

    d.mkdir(parents=True, exist_ok=True)
    t = np.arange(N_T) * 0.1
    f = np.zeros((N_T, 3), tp.RECORD)
    f["container"] = tp.NO_CONTAINER
    for a, (p, v, yaw) in _traj(t).items():
        f["x_mm"][:, a], f["y_mm"][:, a], f["z_mm"][:, a] = (np.round(p[:, k] * 1000).astype(np.int64) for k in range(3))
        f["vx_cmps"][:, a], f["vy_cmps"][:, a] = (np.round(v[:, k] * 100).astype(np.int64) for k in range(2))
        f["yaw_cdeg"][:, a] = np.round(np.degrees(yaw) * 100).astype(np.int64)
        f["state"][:, a] = 1
    f["state"][:T_BG_NS // 100_000_000, 2] = 0
    h64 = "0" * 64
    hd = {"schema": "drone4d.tape.v1", "event_id": "ev-cross", "event_version": 1, "event_sha256": h64, "mode": "exogenous", "seed": 0,
          "tick_hz": 10, "n_ticks": N_T, "duration_ms": N_T * 100, "record": tp.record_spec(),
          "codec": {"name": "col-delta-zstd@1", "block_ticks": 100}, "actors": ACTORS,
          "world": {"id": "synth-glb", "content_version": content_version, "raycast_sha256": h64, "navmesh_sha256": {"person": h64},
                    "anchors_sha256": h64}, "environment": {}, "env_track": [], "comms": {}, "anim": None, "realized": None}
    h = tp.write_tape(d / "tape.bin", hd, f, n_scripted=2)
    (d / "tape.json").write_text(json.dumps({"content_sha256": h["content_sha256"]}))
    return d / "tape.bin"


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory):
    """(数据根, 世界目录, 事件版本目录)：合成世界与路口磁带（磁带按该世界的内容版本编译）。"""
    from synthworld import build_synth_world

    root = tmp_path_factory.mktemp("tc")
    mp = pytest.MonkeyPatch()
    mp.setenv("AWR_D3_ENABLED", "1")
    w = build_synth_world(root, mp)
    cv = json.loads((w / "world.json").read_text(encoding="utf-8"))["contentVersion"]
    ev = root / "events" / "ev-cross" / "v1"
    _write_tape(ev, cv)
    yield root, w, ev
    mp.undo()


def _reader(ev: Path):
    """独立的磁带读取器（不与被测路径共享块缓存），作为时刻函数的参照。"""
    from awr.sim.events.tape import open_tape

    return open_tape(ev)


def _proxy_scene(a: int):
    import open3d as o3d

    from awr.sensorsvc.camera.actors import proxy_mesh

    P = np.ascontiguousarray(proxy_mesh(ACTORS[a])[0], np.float32)
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(P), o3d.core.Tensor(np.arange(len(P), dtype=np.uint32).reshape(-1, 3)))
    return scene


def _surface_dist(scene, w: np.ndarray, pos: np.ndarray, yaw: np.ndarray) -> np.ndarray:
    """世界点 w (n, 3) 按各自的演员位姿 (pos (n, 3), yaw (n,)) 变到演员局部系后到代理网格表面的距离 (n,)。"""
    import open3d as o3d

    r = w - pos
    c, s = np.cos(yaw), np.sin(yaw)
    loc = np.stack([c * r[:, 0] + s * r[:, 1], -s * r[:, 0] + c * r[:, 1], r[:, 2]], 1)
    return scene.compute_distance(o3d.core.Tensor(loc.astype(np.float32))).numpy().astype(np.float64)


def _tape_pose(tape, a: int, t_ns: np.ndarray, offset_ns: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """演员 a 在各整数时刻（事件时刻 = offset + t）的磁带位姿 (pos (n, 3), yaw (n,), state (n,))，逐时刻直接调用时刻函数。"""
    sts = [tape.actor_states(offset_ns + int(x)) for x in t_ns]
    return (np.array([s.pos_enu_m[a] for s in sts]).reshape(-1, 3), np.array([s.yaw_rad[a] for s in sts]),
            np.array([s.state[a] for s in sts], np.uint8))


def test_tape_construction_crosses_the_junction_at_required_speeds(site) -> None:
    """构造本身满足 D3-AC-10 的前提：行人 ≥ 1.3 m/s、车辆 ≥ 8 m/s，二者都在采集与 Env 窗口内穿越路口中心 (0, 60)。"""
    _, _, ev = site
    tape = _reader(ev)
    t = np.arange(0, 2_400_000_001, 50_000_000, dtype=np.int64)
    sp = {a: np.linalg.norm(np.diff(_tape_pose(tape, a, t)[0], axis=0), axis=1) / 0.05 for a in (0, 1)}
    assert sp[0].min() >= 1.3 and sp[1].min() >= 8.0
    ped, veh = _tape_pose(tape, 0, t)[0], _tape_pose(tape, 1, t)[0]
    assert ped[0, 0] < 0.0 < ped[-1, 0] and veh[0, 1] < 60.0 < veh[-1, 1]
    st = _tape_pose(tape, 2, np.array([T_BG_NS - 1, T_BG_NS]))[2]
    assert st.tolist() == [0, 1]


# ---------------------------------------------------------------------------------------------------------------- 实体状态缓存
def test_actor_clock_keys_are_integer_times_and_states_are_not_shared(site) -> None:
    """`ActorClock`：键必须是 Python int（浮点、numpy 整数触发断言）；同一整数时刻复用同一次求值，相邻 1 ns 即重新求值；
    结果与磁带时刻函数逐位相同、数组只读；超过容量按最近使用淘汰。"""
    from awr.sensorsvc.capture import ActorClock

    _, _, ev = site
    ref = _reader(ev)
    clk = ActorClock(_reader(ev), OFFSET_NS)
    t = 1_234_567_891
    for bad in (float(t), np.int64(t), 1.2345e9):
        with pytest.raises(AssertionError):
            clk(bad)
    a = clk(t)
    assert clk(t) is a and (clk.calls, clk.hits) == (1, 1)
    b = clk(t + 1)
    assert b is not a and clk.calls == 2 and not np.shares_memory(a.pos_enu_m, b.pos_enu_m)
    assert np.linalg.norm(b.pos_enu_m[1] - a.pos_enu_m[1]) > 0.0                       # 车辆 1 ns 位移约 9 nm，确为不同时刻的状态
    r = ref.actor_states(OFFSET_NS + t)
    for k in ("ids", "pos_enu_m", "yaw_rad", "vel_enu_mps", "state", "container"):
        assert np.array_equal(getattr(a, k), getattr(r, k))
        with pytest.raises(ValueError):
            getattr(a, k)[0] = 0
    fr = clk.frame(OFFSET_NS)                                                      # 事件 0.5 s：背景行人未出现
    assert fr.idx.tolist() == [0, 1] and np.array_equal(fr.pos, ref.actor_states(2 * OFFSET_NS).pos_enu_m[:2])
    assert all(type(k) is int for k in clk.keys)
    for i in range(ActorClock.CACHE_TIMES + 2):
        clk(10 * i)
    assert len(clk.keys) == ActorClock.CACHE_TIMES and t not in clk.keys
    n = clk.calls
    clk(t)
    assert clk.calls == n + 1                                                      # 被淘汰后重新求值，不沿用旧对象


# ---------------------------------------------------------------------------------------------------------------- 采集作业
def _capture_spec(run_id: str, size: tuple[int, int] | None) -> dict:
    """两机：uav-01 自西向东低空平飞（全局快门 10 fps），uav-02 绕路口环绕（卷帘 15 fps）；均带倒装 MID-360。"""
    cam = lambda prof, fps, az, el: {"name": "cam0", "profile": prof, "fps": fps, "gimbal": {"az_deg": az, "el_deg": el},   # noqa: E731
                                     "outputs": ["rgb", "depth"], **({"size": list(size)} if size else {})}
    return {"run_id": run_id, "event_id": "ev-cross", "world_id": "synth-glb", "duration_s": 2.0, "noise_profile": "clean", "seed": 3,
            "event": {"path": "events/ev-cross/v1", "offset_s": OFFSET_NS / 1e9},
            "fleet": [{"uav": "uav-01", "path": {"kind": "waypoints", "points": [[0, -14, 54, 12, 20], [3, -8, 54, 12, 20]]},
                       "cameras": [cam("rgb720-gs", "10", 0.0, -40.0)], "lidars": [{"name": "lidar0"}]},
                      {"uav": "uav-02", "path": {"kind": "orbit", "center": [0, 60], "radius_m": 22, "agl_m": 16, "period_s": 90,
                                                 "phase_deg": 270},
                       "cameras": [cam("rgb1080-rs", "15", 90.0, -36.0)], "lidars": [{"name": "lidar0"}]}]}


def _expected_camera_frames(spec: dict) -> list[tuple[str, int, int, int]]:
    """按 capture 的触发网格与排序（时刻、机、传感器号）列出各相机帧 (uav, seq, t_mid, readout_ns)：t_mid = t_end − ⌊(e + T_ro)/2⌋。"""
    from awr.sensorsvc.camera.model import CAMERA_PROFILES
    from awr.sensorsvc.timing import parse_rate, t_end_ns, t_mid_ns

    out = []
    for u in spec["fleet"]:
        c = u["cameras"][0]
        p = CAMERA_PROFILES[c["profile"]]
        e, ro = round(p["exposure_s"] * 1e9), round(p["readout_s"] * 1e9)
        rate, k = parse_rate(c["fps"]), 1
        while (te := t_end_ns(0, rate, k)) <= spec["duration_s"] * 1e9:
            out.append((u["uav"], k, t_mid_ns(te, e, ro), ro))
            k += 1
    return sorted(out, key=lambda j: (j[2], j[0]))


def _lidar_world_points(gh: dict, pts: np.ndarray, frame_ns: int) -> np.ndarray:
    """按真值文件的 11 个位姿节点（与扫描同一插值：位置线性、四元数同半球归一化线性）把点还原到世界系。"""
    from awr.sensorsvc.lidar.mesh import _quat_R

    nd = gh["T_world_sensor_nodes"]
    P = np.array([n["pos_enu_m"] for n in nd], np.float64)
    Q = np.array([n["q_ws_xyzw"] for n in nd], np.float64)
    for k in range(1, len(Q)):
        if np.dot(Q[k], Q[k - 1]) < 0:
            Q[k] = -Q[k]
    t = pts["offset_time"].astype(np.int64)
    seg_ns = frame_ns // (len(nd) - 1)
    s = np.minimum(t // seg_ns, len(nd) - 2)
    u = (t - s * seg_ns) / seg_ns
    o = P[s] * (1 - u)[:, None] + P[s + 1] * u[:, None]
    q = Q[s] * (1 - u)[:, None] + Q[s + 1] * u[:, None]
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    xyz = np.stack([pts["x"], pts["y"], pts["z"]], 1).astype(np.float64)
    return np.einsum("nij,nj->ni", _quat_R(q), xyz) + o


def _instrument_capture(monkeypatch: pytest.MonkeyPatch, camera_cls) -> dict:
    """替换 capture 用到的相机类、LiDAR 类与 ActorClock，记录每帧绘制所用的演员位姿、每个 LiDAR 帧的逐点求值器与时钟实例。"""
    import awr.sensorsvc.camera.raster as raster
    import awr.sensorsvc.lidar.mesh as lm
    from awr.sensorsvc import capture as cap

    log: dict = {"cams": [], "tracks": [], "clocks": []}

    class SpyLidar(lm.MeshLidar):
        def scan(self, frame_seq, t_start_ns, nodes, **kw):
            sc = super().scan(frame_seq, t_start_ns, nodes, **kw)
            log["tracks"].append((kw.get("uav_no"), int(frame_seq), int(t_start_ns), self.last_track))
            return sc

    class SpyClock(cap.ActorClock):
        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            log["clocks"].append(self)

    camera_cls.log = log["cams"]
    monkeypatch.setattr(raster, "RasterCamera", camera_cls)
    monkeypatch.setattr(lm, "MeshLidar", SpyLidar)
    monkeypatch.setattr(cap, "ActorClock", SpyClock)
    return log


class _StubCamera:
    """GPU 光栅相机的 CPU 替身：只记录每帧绘制所用的演员位姿与相机位姿，输出常量图。"""

    log: ClassVar[list] = []

    def __init__(self, scene) -> None:
        self.info = {"GL_RENDERER": "stub", "GL_VERSION": "4.5.0 stub 1.0"}

    def set_actors(self, actors) -> None:
        self.actors = actors

    def set_sun(self, az_el) -> None:
        pass

    def render(self, cm, R, pos, *, linear: bool = False, fx=None, rs_nodes=None, actors=None):
        from awr.sensorsvc.camera.effects import apply_fx_reference
        from awr.sensorsvc.camera.raster import RenderOut

        type(self).log.append({"actors": actors, "pos": np.asarray(pos, np.float64).copy(), "rs": rs_nodes is not None})
        h, w = cm.height, cm.width
        lin = np.full((h, w, 3), 0.1, np.float32)
        depth = np.full((h, w), 20.0, np.float32)
        rgb, d_obs = apply_fx_reference(lin, depth, cm, fx)
        z16 = np.zeros((h, w), np.uint16)
        return RenderOut(rgb, depth, z16, np.zeros((h, w), np.uint8), 1.0, 1.0, 1, depth_obs=d_obs,
                         meter_sub=lin[::8, ::8] if fx.meter else None, instance_unocc=z16.copy())

    def release(self) -> None:
        pass


def _check_capture_actor_times(root: Path, ev: Path, spec: dict, log: dict) -> dict:
    """采集产物与记录的公共判据（CPU 替身与 GPU 运行共用）→ 统计。"""
    from awr.bench.recorder.lidar import unpack_lidar_frame, unpack_lidar_gt
    from awr.bench.recorder.read import FrameStore, TruthStore
    from awr.bench.recorder.truth import unpack_camera_gt
    from awr.bench.runs_store import truth_paths
    from awr.sensorsvc.lidar.model import load_lidar_model

    tape = _reader(ev)
    run = root / "dataset" / spec["event_id"] / "runs" / spec["run_id"]
    exp = _expected_camera_frames(spec)
    cams = log["cams"]
    assert len(cams) == len(exp) == 50
    ts = TruthStore(*truth_paths(run))
    stats: dict = {"cam_frames": len(exp), "cam_max_m": 0.0, "lidar_max_m": {}, "lidar_pts": {}, "cross_max_m": 0.0, "cross_n": 0}
    # 1) 相机：绘制所用演员位姿 = 曝光中点 t_mid 的磁带时刻函数值（逐位相同）；真值记录的 t_mid 一致
    for (uav, seq, tm, ro), c in zip(exp, cams, strict=True):
        assert c["rs"] == (ro > 0)
        r = tape.actor_states(OFFSET_NS + tm)
        v = np.isin(r.state, VIS)
        af = c["actors"]
        assert af.idx.tolist() == r.ids[v].tolist()
        assert np.array_equal(af.pos, r.pos_enu_m[v]) and np.array_equal(af.yaw, r.yaw_rad[v])
        rec, _ = unpack_camera_gt(ts.get(uav, "cam0", seq)[1])
        assert rec["agent_view"]["t_mid_ns"] == tm and rec["ground_truth"]["timing"]["t_truth_ns"] == [tm]
        assert (2 in af.idx.tolist()) == (OFFSET_NS + tm >= T_BG_NS)                  # 背景行人按 t_mid 阶跃出现
    for i in range(len(cams)):
        for j in range(i + 1, min(i + 4, len(cams))):
            if exp[i][2] != exp[j][2]:                                                 # 不同时刻的帧不共用实体状态数组
                assert not np.shares_memory(cams[i]["actors"].pos, cams[j]["actors"].pos)
    # 2) LiDAR：写出的演员回波点在其自身时刻的磁带位姿下落在代理表面上（≤ 1 mm）
    m = load_lidar_model()
    fs = FrameStore(run)
    scenes = {a: _proxy_scene(a) for a in range(len(ACTORS))}
    for u in spec["fleet"]:
        for seq in range(20):
            f = fs.frame(u["uav"], "lidar0", seq)
            hdr, pts = unpack_lidar_frame(f.payload)
            gh, inst, _flags = unpack_lidar_gt(ts.get(u["uav"], "lidar0", seq)[1])
            t_s = int(hdr["timebase_ns"])
            assert t_s == seq * m.frame_ns and gh["timebase_ns"] == t_s
            slot = (pts["offset_time"] // m.point_dt_ns).astype(np.int64)
            w = _lidar_world_points(gh, pts, m.frame_ns)
            for a in range(len(ACTORS)):
                on = inst[slot] == a + 1
                if not on.any():
                    continue
                t_pt = t_s + pts["offset_time"][on].astype(np.int64)
                pos, yaw, st = _tape_pose(tape, a, t_pt, OFFSET_NS)
                assert np.isin(st, VIS).all()                                          # 不可见时刻没有该演员的回波
                d = _surface_dist(scenes[a], w[on], pos, yaw)
                stats["lidar_max_m"][a] = max(stats["lidar_max_m"].get(a, 0.0), float(d.max()))
                stats["lidar_pts"][a] = stats["lidar_pts"].get(a, 0) + int(on.sum())
    fs.close()
    ts.close()
    assert stats["lidar_pts"].get(0, 0) >= 10 and stats["lidar_pts"].get(1, 0) >= 50 and stats["lidar_pts"].get(2, 0) >= 5
    assert max(stats["lidar_max_m"].values()) <= 1e-3
    # 3) 缓存（在本函数其后的求值之前统计）：整数键，只在时刻精确相同时复用。两机 LiDAR 帧的 t_s 相同，区间样本时刻（两端、三等分点与校验点）相同，uav-02 查询
    #    uav-01 已求值的同一时刻时命中；磁带时刻函数的调用次数恰为全部不同查询时刻的个数（相机 t_mid 与 LiDAR 样本时刻之并）
    (clk,) = log["clocks"]
    assert all(type(k) is int for k in clk.keys)
    by = {(n, sq): set(tr.samples) for n, sq, _t, tr in log["tracks"]}
    shared = sum(len(by[(0, sq)] & by[(1, sq)]) for sq in range(20))
    times = {tm for _u, _s, tm, _r in exp}.union(*by.values())
    assert shared >= 6 * 20 and clk.hits >= shared and clk.calls == len(times)
    stats.update(clock_calls=clk.calls, clock_hits=clk.hits, shared_lidar_times=shared,
                 track_calls={no: sum(tr.calls for n, _s, _t, tr in log["tracks"] if n == no) for no in (0, 1)})
    # 4) 同一时刻相机与 LiDAR 所用的演员位置：相机 t_mid 落在同机 LiDAR 帧区间内时，逐点求值器在 t_mid 的位置与相机所用位置之差
    tracks = {(no, ts_): tr for no, _seq, ts_, tr in log["tracks"]}
    uav_no = {u["uav"]: i for i, u in enumerate(spec["fleet"])}
    for (uav, _seq, tm, _ro), c in zip(exp, cams, strict=True):
        tr = tracks.get((uav_no[uav], (tm // m.frame_ns) * m.frame_ns))
        if tr is None or not tr.t0 <= tm <= tr.t1:
            continue
        for j, a in enumerate(c["actors"].idx.tolist()):
            p, y, s = tr.eval(a, np.array([tm]))
            assert s[0] in VIS
            stats["cross_max_m"] = max(stats["cross_max_m"], float(np.linalg.norm(p[0] - c["actors"].pos[j])))
            assert abs(float(y[0]) - float(c["actors"].yaw[j])) <= 1e-6
            stats["cross_n"] += 1
    assert stats["cross_n"] >= 40 and stats["cross_max_m"] <= 1e-3
    return stats


def test_capture_camera_and_lidar_use_the_tape_time_function_at_their_own_times(site, monkeypatch: pytest.MonkeyPatch) -> None:
    """采集作业（CPU 替身相机、真实网格 LiDAR）：相机按曝光中点、LiDAR 按逐点时刻取同一磁带时刻函数；见模块说明判据 1、2。"""
    pytest.importorskip("mcap_protobuf")
    import awr.sensorsvc.camera.scene as scene
    from awr.sensorsvc import capture as cap

    root, w, ev = site
    log = _instrument_capture(monkeypatch, _StubCamera)
    _StubCamera.log.clear()
    monkeypatch.setattr(scene, "load_appearance", lambda d: None)
    spec = _capture_spec("tc-cpu", (64, 48))
    m = cap.run_capture(spec, root, worlds_dir=w.parent, log=lambda s: None)
    assert m["frames"] == 50 + 40 and m["event"]["offset_s"] == OFFSET_NS / 1e9
    st = _check_capture_actor_times(root, ev, spec, log)
    print(f"[S07] capture (stub camera): {st}")
    assert st["cross_max_m"] <= 1e-6                                                   # 同一时刻函数：差值只有插值舍入


# ---------------------------------------------------------------------------------------------------------------- stage 次序与时钟档
@pytest.fixture(scope="module")
def env_event(site, tmp_path_factory: pytest.TempPathFactory):
    """Env 运行所需的事件目录（计划、锚点；合成交接事件 lenient 编译），磁带换用本模块的路口磁带。"""
    from test_compile import EVENT, OVERLAY

    from awr.sim.events.compile import CompileOptions, compile_event

    _, w, ev = site
    d = tmp_path_factory.mktemp("envev")
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    ev = copy.deepcopy(EVENT)
    ev["brief"] = {"public_anchors": ["ev_plaza", "ev_door"],                   # 与 tests/d3/env 的 synth_event 同一简报
                   "targets": [{"actor": "person_a", "description_en": "Blue top", "description_zh": "蓝色上衣"}]}
    (d / "event.json").write_text(json.dumps(ev))
    r = compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=d / "cache", strict=False))
    assert r.status == "compiled", r.as_dict()
    return d / "v1"


class _SpyRaster:
    """L2 渲染器替身：记录每次绘制的演员位姿与相机位置。"""

    def __init__(self) -> None:
        self.last: tuple | None = None

    def render(self, cm, R_ws, p_w, *, fx=None, rs_nodes=None, actors=None, linear=False):
        from awr.sensorsvc.camera.raster import RenderOut

        self.last = (actors, np.asarray(p_w, np.float64).copy())
        h, w = cm.height, cm.width
        z16 = np.zeros((h, w), np.uint16)
        return RenderOut(np.zeros((h, w, 3), np.uint8), np.zeros((h, w), np.float32), z16, np.zeros((h, w), np.uint8), 0.0, 0.0, 0,
                         instance_unocc=z16.copy())


GRID = np.unique(np.concatenate([np.arange(0, 20000, 997), [19999]])) * 5000     # LiDAR 帧内抽样槽位时刻（相对 t_s，ns）


def _env_run(site, env_event, tape_dir: Path, out: Path, *, physics: str, clock: str, lidar: bool, permute: bool,
             monkeypatch: pytest.MonkeyPatch) -> dict:
    """一次 2 s 的 L2 Env 运行 → {cam: {(uav, k): (t_mid, idx, pos, yaw)}, lid: {(uav_no, seq): (t_s, [(pos, yaw, state)…])},
    cam_pos: {(uav, k): 相机位置}, stages: [(name, order)], probe_calls, n_ticks}。`permute`：M24 的 mesh_contact 与 pose_history
    换序（197、196），并在 order 198 插入一个按物理频率经 M18 运动源（`TapeMotionSource`，与传感器共用同一磁带读取器）在仿真时刻
    查询演员状态的 stage。"""
    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.l2 import L2Config, L2Sensors
    from awr.agentsdk.env.spec import EnvSpec, FleetEntry
    from awr.sensorsvc.lidar.mesh import MeshLidar
    from awr.sensorsvc.lidar.model import load_lidar_model
    from awr.sim.events.runtime import TapeMotionSource
    from awr.sim.fleet.stages import registry as R
    from awr.world.mesh.query import open_mesh_world_query

    _, w, _ = site
    out.mkdir(parents=True, exist_ok=True)
    tape = _reader(tape_dir)
    rec: dict = {"cam": {}, "lid": {}, "cam_pos": {}, "probe": 0}
    with monkeypatch.context() as mp:
        if permute:
            src = TapeMotionSource(tape, {"ped": 0, "veh": 1}, 0)
            orig = R.make_stage

            def probe(S, ctx) -> None:
                st = src.states(int(ctx.t_ns))
                assert st.pos_enu_m.shape == (2, 3)
                rec["probe"] += 1

            def make_stage(name, every, phase, order, fn, **kw):
                out_ = orig(name, every, phase, {"mesh_contact": 197, "pose_history": 196}.get(name, order), fn, **kw)
                if name == "pose_history":
                    out_ = out_ + orig("tape_probe", None, None, 198, probe, owner="M24", hz=kw["hz"], phase_slot=0,
                                       fidelity=kw["fidelity"], budget_core=0.0001)
                return out_

            mp.setattr(R, "make_stage", make_stage)

        class SpyLidar(MeshLidar):
            def scan(self, frame_seq, t_start_ns, nodes, **kw):
                sc = super().scan(frame_seq, t_start_ns, nodes, **kw)
                tr = self.last_track
                rec["lid"][(kw["uav_no"], int(frame_seq))] = (int(t_start_ns), [tr.eval(a, t_start_ns + GRID) for a in range(len(ACTORS))])
                return sc

        class SpyL2(L2Sensors):
            def _render(self, uav, uav_no, k):
                out_, meta = super()._render(uav, uav_no, k)
                af, p = self.cam.last
                rec["cam"][(uav, k)] = (int(meta[0]), af.idx.copy(), af.pos.copy(), af.yaw.copy())
                rec["cam_pos"][(uav, k)] = p
                return out_, meta

        def l2(env):
            lid = SpyLidar(open_mesh_world_query(w, out / "wq"), load_lidar_model(), list(env.tape.actors)) if lidar else None
            return SpyL2(world_dir=w, tape=env.tape, uavs=env.uavs, seed=env.spec.seed, noise=env.spec.noise, obs_level=env.spec.obs_level,
                         nav=env.nav, mount_t_bs_m=env.cam.mount_t_bs_m, hfov_deg=env.cam.hfov_deg,
                         cfg=L2Config(camera_profile="rgb720-gs", lidar=lidar, imu=lidar, cache_dir=out / "wq", lidar_proc=False),
                         renderer=_SpyRaster(), lidar=lid)

        fleet = (FleetEntry("uav-01", (-12.0, 54.0, 12.0), 0.0, airborne=True), FleetEntry("uav-02", (12.0, 66.0, 15.0), math.pi, airborne=True))
        spec = EnvSpec(f"tc-{physics}-{clock}-{int(permute)}", 5, w, env_event, fleet, obs_level="L2", noise="clean", physics=physics,
                       duration_s=2.0, out_dir=None, l2={"lidar": lidar, "imu": lidar}, clock_profile=clock)
        env = BenchEnv(spec, tape=tape, l2=l2)
        obs, _ = env.reset()
        while env.agents:
            acts = {u: {"schema": "drone4d.bench.act.v1", "k": o["obs"]["k"], "nav": {"mode": "goto", "pos_enu_m": [0.0, 60.0, 14.0], "speed_mps": 3.0},
                        "gimbal": {"sensor_id": "eo", "mode": "angles", "pitch_deg": -40.0, "yaw_deg": 0.0}} for u, o in obs.items()}
            obs, *_ = env.step(acts)
        rec["stages"] = [(s.name, s.order) for s in env.tier.f.pipeline.stages] if physics == "fast" else []
        rec["n_ticks"] = env.k
        rec["pre_ticks"] = env.preroll.n_ticks              # 预滚 [−2 s, 0)（env.preroll）：物理步照常执行
        env.close()
    return rec


def test_stage_order_and_clock_profile_do_not_change_sensor_actor_states(site, env_event, tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """L2 Env：物理档、sim-core stage 次序与时钟档变化时机体位姿随之变化，传感器读到的演员状态逐位不变，且等于各自时刻的磁带
    时刻函数值（相机逐位相同，LiDAR 逐点求值 ≤ 1 µm）。LiDAR 与 IMU 只允许 d1_250（250 Hz 物理，M24-FR-005），因此 sandbox100 只比较
    相机（10 ms 主时钟、100 Hz 物理）。"""
    _, _, ev = site
    cfgs = {"kin": ("kinematic", "d1_250", True, False), "fast": ("fast", "d1_250", True, False), "fast-perm": ("fast", "d1_250", True, True),
            "fast-sb100-cam": ("fast", "sandbox100", False, False), "fast-sb100": ("fast", "sandbox100", False, True)}
    runs = {n: _env_run(site, env_event, ev, tmp_path / n, physics=p, clock=c, lidar=lid, permute=perm, monkeypatch=monkeypatch)
            for n, (p, c, lid, perm) in cfgs.items()}
    base = runs["fast"]
    assert base["n_ticks"] == 20 and len(base["cam"]) == 2 * 21 and len(base["lid"]) == 2 * 20
    # 运行确有差异：stage 次序已换、探针 stage 按物理频率执行，物理档与时钟档改变机体位姿
    order = lambda r: [n for n, _o in r["stages"] if n in ("mesh_contact", "pose_history", "tape_probe")]  # noqa: E731
    assert order(base) == ["mesh_contact", "pose_history"] and order(runs["fast-perm"]) == ["pose_history", "mesh_contact", "tape_probe"]
    assert runs["fast-perm"]["probe"] == (20 + runs["fast-perm"]["pre_ticks"]) * 25
    assert runs["fast-sb100"]["probe"] == (20 + runs["fast-sb100"]["pre_ticks"]) * 10
    moved = {n: max(float(np.linalg.norm(runs[n]["cam_pos"][k] - base["cam_pos"][k])) for k in base["cam_pos"]) for n in runs}
    travel = max(float(np.linalg.norm(base["cam_pos"][(u, 20)] - base["cam_pos"][(u, 0)])) for u in ("uav-01", "uav-02"))
    print(f"[S07] env camera-position deviation from fast/d1_250 (m): {moved}; travel {travel:.2f} m; probe calls "
          f"{runs['fast-perm']['probe']}, {runs['fast-sb100']['probe']}")
    assert travel > 2.0 and all(moved[n] > 1e-3 for n in ("kin", "fast-sb100-cam", "fast-sb100"))
    # 相机：同一 (uav, k) 的 t_mid 与演员位姿逐位相同，且等于 t_mid 的磁带时刻函数值
    tape = _reader(ev)
    for k, (tm, idx, pos, yaw) in base["cam"].items():
        r = tape.actor_states(tm)
        v = np.isin(r.state, VIS)
        assert idx.tolist() == r.ids[v].tolist() and np.array_equal(pos, r.pos_enu_m[v]) and np.array_equal(yaw, r.yaw_rad[v])
        for n, run in runs.items():
            tm2, idx2, pos2, yaw2 = run["cam"][k]
            assert tm2 == tm and np.array_equal(idx2, idx) and np.array_equal(pos2, pos) and np.array_equal(yaw2, yaw), (n, k)
    # LiDAR：逐点求值器在帧内抽样时刻的状态逐位相同，且与时刻函数直接求值之差 ≤ 1 µm
    for k, (t_s, ev_a) in base["lid"].items():
        for n in ("kin", "fast-perm"):
            t2, ev_b = runs[n]["lid"][k]
            assert t2 == t_s and all(np.array_equal(x, y) for A, B in zip(ev_a, ev_b, strict=True) for x, y in zip(A, B, strict=True)), (n, k)
        for a in range(len(ACTORS)):
            pos, yaw, st = _tape_pose(tape, a, t_s + GRID)
            assert np.array_equal(ev_a[a][2], st)
            vis = np.isin(st, VIS)
            if vis.any():
                assert np.linalg.norm(ev_a[a][0][vis] - pos[vis], axis=1).max() <= 1e-6
                assert np.abs(ev_a[a][1][vis] - yaw[vis]).max() <= 1e-6


# ---------------------------------------------------------------------------------------------------------------- GPU：光栅相机
def _egl_or_skip() -> None:
    moderngl = pytest.importorskip("moderngl")
    pytest.importorskip("PIL")
    pytest.importorskip("mcap_protobuf")
    try:
        ctx = moderngl.create_standalone_context(backend="egl", require=450)
    except Exception as e:                                   # 无 EGL 设备
        pytest.skip(f"[gpu] EGL unavailable: {e}")
    ctx.release()


def test_gl_camera_actor_pixels_lie_on_actor_at_exposure_midpoint(site, monkeypatch: pytest.MonkeyPatch) -> None:
    """dmax GPU：采集作业以真实 EGL 光栅相机渲染（1280 × 720 全局快门、1920 × 1080 卷帘）。全局快门帧中演员像素（可见实例）
    以像素中心与平面深度反投影到世界系，按 t_mid 的磁带位姿变到演员局部系，到代理网格表面的距离 ≤ 1 mm；以帧末时刻
    （t_mid + 4 ms）的位姿判定时行人 > 1 mm、车辆 > 1 cm。另按判据 1、2 检查同一次运行的相机与 LiDAR 产物。"""
    _egl_or_skip()
    import awr.sensorsvc.camera.raster as raster
    from awr.sensorsvc import capture as cap
    from awr.sensorsvc.camera.proj import R_FLU_OPT

    root, w, ev = site

    class GlSpy(raster.RasterCamera):
        log: ClassVar[list] = []

        def render(self, cm, R, pos, **kw):
            out = super().render(cm, R, pos, **kw)
            rec = {"actors": kw.get("actors"), "pos": np.asarray(pos, np.float64).copy(), "rs": kw.get("rs_nodes") is not None}
            if not rec["rs"]:
                rec.update(cm=cm, R=np.asarray(R, np.float64).copy(), depth=out.depth_m, inst=out.instance)
            type(self).log.append(rec)
            return out

    log = _instrument_capture(monkeypatch, GlSpy)
    GlSpy.log.clear()
    spec = _capture_spec("tc-gl", None)
    cap.run_capture(spec, root, worlds_dir=w.parent, log=lambda s: None)
    st = _check_capture_actor_times(root, ev, spec, log)
    tape = _reader(ev)
    exp = _expected_camera_frames(spec)
    scenes = {a: _proxy_scene(a) for a in range(len(ACTORS))}
    err = {a: 0.0 for a in range(len(ACTORS))}
    late = {a: 0.0 for a in range(len(ACTORS))}
    npx = {a: 0 for a in range(len(ACTORS))}
    for (_uav, _seq, tm, ro), c in zip(exp, log["cams"], strict=True):
        if ro > 0:
            continue
        cm, R, p = c["cm"], c["R"], c["pos"]
        Rwo = R @ R_FLU_OPT
        for a in c["actors"].idx.tolist():
            jj, ii = np.nonzero(c["inst"] == a + 1)
            if len(ii) == 0:
                continue
            z = c["depth"][jj, ii].astype(np.float64)
            Xo = np.stack([(ii + 0.5 - cm.cx) / cm.fx * z, (jj + 0.5 - cm.cy) / cm.fy * z, z], 1)
            Xw = Xo @ Rwo.T + p
            n = len(ii)
            for t_eval, acc in ((tm, err), (tm + 4_000_000, late)):
                s = tape.actor_states(OFFSET_NS + t_eval)
                d = _surface_dist(scenes[a], Xw, np.repeat(s.pos_enu_m[a][None], n, 0), np.full(n, s.yaw_rad[a]))
                acc[a] = max(acc[a], float(d.max()))
            npx[a] += n
    print(f"[S07] gl actor px {npx}, max surface err at t_mid {err} m, at t_mid+4ms {late} m; lidar max {st['lidar_max_m']} m "
          f"(pts {st['lidar_pts']}), camera-lidar same-instant max {st['cross_max_m']} m over {st['cross_n']}; "
          f"clock calls {st['clock_calls']} hits {st['clock_hits']}")
    assert npx[0] >= 100 and npx[1] >= 1000
    assert max(err.values()) <= 1e-3
    assert late[0] > 1e-3 and late[1] > 1e-2
