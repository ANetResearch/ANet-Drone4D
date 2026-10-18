"""L0 离线已知位姿地图（M27-FR-107，§6.7.7 `l0map`；D3-AC-15 的 L0 地图与 Jaccard 条款、D3-AC-27 的 Jaccard 口径）。

- 3D-DDA 与每帧更新语义对独立参考实现（逐轴交点按 (t, 轴) 归并、Python 集合）逐体素、逐 log-odds 相同；射线顺序无关；扩容重入不改变结果；
- 逐点位姿积分：与 `awr.sensorsvc.poses.sensor_pose` 同式；8 m/s 下按逐点位姿积分的占据体素全部落在网格半对角线内，按帧位姿积分偏离显著；
- 过滤（深度上限 60 m、虚警 tag、零点占位）、钳位与占据判据、内存预算 684、合并与持久化；
- 0.5 m 表面体素两两 Jaccard、覆盖与合并覆盖对暴力集合计算一致；机队报告 JSON；到网格距离与 u8 误差量化（反量化差 ≤ 1 mm）。
"""

from __future__ import annotations

import json
import sys
from itertools import combinations, pairwise
from pathlib import Path

import numpy as np
import pytest

from awr.bench.eval.lidar_map import (
    JACCARD_MAX,
    L0Map,
    L0MapError,
    L0Params,
    SensorTrajectory,
    coverage_and_jaccard,
    fleet_report,
    mesh_distance,
    mesh_error_u8,
    pack_keys,
    unpack_keys,
    world_rays,
)
from awr.bench.recorder.lidar import LIDAR_RECORD

VOX = 0.25
HALF_DIAG = np.sqrt(3.0) / 2.0 * VOX
FIX = dict(hit=85, miss=-40, lo=-200, hi=350, occ=85)


# ---------------------------------------------------------------------------------------------------------------- 参考实现
def ref_traverse(a: np.ndarray, b: np.ndarray) -> list[tuple[int, int, int]]:
    """体素单位坐标的射线 a→b：起点体素至终点体素的 6 连通序列（含两端）。逐轴边界参数 (边界 − a)/d，按 (t, 轴) 归并。"""
    i = [int(np.floor(v)) for v in a]
    e = [int(np.floor(v)) for v in b]
    d = [float(b[k] - a[k]) for k in range(3)]
    cross = []
    for ax in range(3):
        for k in range(abs(e[ax] - i[ax])):
            bnd = i[ax] + 1 + k if e[ax] > i[ax] else i[ax] - k
            cross.append(((bnd - float(a[ax])) / d[ax], ax))
    cross.sort()
    cur = list(i)
    seq = [tuple(cur)]
    for _t, ax in cross:
        cur[ax] += 1 if e[ax] > i[ax] else -1
        seq.append(tuple(cur))
    assert seq[-1] == tuple(e)
    return seq


class RefMap:
    """Python 字典参考：每帧命中集合与穿越集合（去掉命中）各更新一次，逐次钳位。"""

    def __init__(self) -> None:
        self.lo: dict[tuple[int, int, int], int] = {}

    def scan(self, o: np.ndarray, e: np.ndarray, vox: float = VOX) -> None:
        a, b = o / vox, e / vox
        hits, free = set(), set()
        for k in range(len(a)):
            seq = ref_traverse(a[k], b[k])
            hits.add(seq[-1])
            free.update(seq[:-1])
        for v in hits:
            self.lo[v] = min(max(self.lo.get(v, 0) + FIX["hit"], FIX["lo"]), FIX["hi"])
        for v in free - hits:
            self.lo[v] = min(max(self.lo.get(v, 0) + FIX["miss"], FIX["lo"]), FIX["hi"])


def random_rays(rng: np.random.Generator, n: int, scale: float = 6.0) -> tuple[np.ndarray, np.ndarray]:
    o = rng.uniform(-scale, scale, (n, 3))
    e = o + rng.normal(0, scale, (n, 3))
    # 特例：轴对齐、精确对角线（平局）、同体素、跨块边界的负坐标、落在体素边界上的端点
    o[:6] = [[0.1, 0.1, 0.1], [0.125, 0.125, 0.125], [1.01, 1.02, 1.03], [-0.3, -2.1, -0.01], [2.0, 2.0, 2.0], [-1.0, 0.0, 3.0]]
    e[:6] = [[3.1, 0.1, 0.1], [2.125, 2.125, 0.125], [1.05, 1.04, 1.06], [-4.4, 3.9, -5.2], [-2.0, 2.0, 0.0], [-1.0, -3.0, 0.0]]
    return o, e


def assert_same_as_ref(m: L0Map, ref: RefMap) -> None:
    v = np.array(sorted(ref.lo), np.int64)
    lo, obs = m.logodds(v)
    assert obs.all() and m.observed_count() == len(ref.lo)
    assert np.array_equal(np.round(lo / 0.01).astype(int), np.array([ref.lo[tuple(x)] for x in v]))
    occ = np.array(sorted(k for k, x in ref.lo.items() if x >= FIX["occ"]), np.int64).reshape(-1, 3)
    assert np.array_equal(m.occupied_voxels(), occ)


# ---------------------------------------------------------------------------------------------------------------- DDA 与更新语义
def test_ref_traverse_is_connected_and_covers_the_segment() -> None:
    rng = np.random.default_rng(1)
    o, e = random_rays(rng, 200)
    for k in range(len(o)):
        a, b = o[k] / VOX, e[k] / VOX
        seq = ref_traverse(a, b)
        assert all(sum(abs(p - q) for p, q in zip(s, t, strict=True)) == 1 for s, t in pairwise(seq))
        s = set(seq)
        ts = np.linspace(0, 1, 4001)[1:-1]
        P = a + ts[:, None] * (b - a)
        frac = np.abs(P - np.round(P))
        P = P[(frac > 1e-6).all(axis=1)]                                    # 边界上的采样点归属不定，不检
        assert {tuple(x) for x in np.floor(P).astype(int)} <= s


def test_scan_matches_reference_dda_and_update_rules() -> None:
    rng = np.random.default_rng(2)
    m, ref = L0Map(), RefMap()
    for _ in range(4):
        o, e = random_rays(rng, 300)
        m.integrate_scan(o, e)
        ref.scan(o, e)
    assert_same_as_ref(m, ref)
    assert m.counts["hit_updates"] > 0 and m.counts["free_updates"] > m.counts["hit_updates"]


def test_same_scan_hit_wins_and_each_voxel_updates_once() -> None:
    m = L0Map()
    o = np.array([[0.1, 0.1, 0.1], [0.1, 0.1, 0.1], [0.1, 0.1, 0.1]])
    e = np.array([[2.1, 0.1, 0.1], [2.1, 0.1, 0.1], [1.1, 0.1, 0.1]])               # 第 3 条射线的终点体素 (4, 0, 0) 被前两条穿越
    m.integrate_scan(o, e)
    lo, _ = m.logodds(np.array([[8, 0, 0], [4, 0, 0], [0, 0, 0], [5, 0, 0]]))
    assert np.allclose(lo, [0.85, 0.85, -0.40, -0.40])                              # 重复命中只加一次；命中优先于穿越
    for _ in range(10):
        m.integrate_scan(o, e)
    lo, _ = m.logodds(np.array([[8, 0, 0], [0, 0, 0], [99, 99, 99]]))
    assert np.allclose(lo, [3.5, -2.0, 0.0])                                        # 钳位；未观测体素为 0


def test_ray_order_and_growth_resume_do_not_change_the_map() -> None:
    rng = np.random.default_rng(3)
    scans = [random_rays(rng, 400, 20.0) for _ in range(3)]
    m1, m2, m3 = L0Map(), L0Map(), L0Map()
    m3._lo, m3._flag = np.zeros((1, 512), np.int16), np.zeros((1, 512), np.uint8)   # 最小容量：块池、哈希表与断点记录反复扩容重入
    m3._keys, m3._vals, m3._touched = np.full(2, -1, np.int64), np.zeros(2, np.int32), np.empty(1, np.int64)
    for o, e in scans:
        m1.integrate_scan(o, e)
        p = rng.permutation(len(o))
        m2.integrate_scan(o[p], e[p])
        m3.integrate_scan(o, e)
    b1, b2, b3 = m1._block_arrays(), m2._block_arrays(), m3._block_arrays()
    for x, y, z in zip(b1, b2, b3, strict=True):
        assert np.array_equal(x, y) and np.array_equal(x, z)
    assert m1.counts == m2.counts == m3.counts and m3.n_blocks == m1.n_blocks > 100


def test_keys_roundtrip_and_range_guard() -> None:
    v = np.array([[0, 0, 0], [-1, 2, -3], [(1 << 20) - 1, -(1 << 20), 7]], np.int64)
    k = pack_keys(v)
    assert np.array_equal(unpack_keys(k), v) and (np.argsort(k) == np.lexsort(v.T[::-1])).all()
    with pytest.raises(L0MapError):
        pack_keys(np.array([[1 << 20, 0, 0]]))
    with pytest.raises(L0MapError) as ei:
        L0Map().integrate_scan(np.zeros((1, 3)), np.array([[3e6, 0.0, 0.0]]))
    assert ei.value.code == 684


def test_memory_budget_raises_684_and_invalidates_the_map() -> None:
    m = L0Map(L0Params(budget_bytes=40 * 1600))                                      # 约 40 块
    o, e = random_rays(np.random.default_rng(4), 200, 20.0)
    with pytest.raises(L0MapError) as ei:
        m.integrate_scan(o, e)
    assert ei.value.code == 684 and ei.value.name == "SLAM_JOB_FAILED" and m.n_blocks <= m.max_blocks
    with pytest.raises(L0MapError):
        m.occupied_voxels()
    ok = L0Map()
    ok.integrate_scan(o, e)
    assert ok.peak_nbytes >= ok.nbytes > 0 and ok.stats()["blocks"] == ok.n_blocks


# ---------------------------------------------------------------------------------------------------------------- 逐点位姿与过滤
def _base_samples(n=400, hz=250, v=(8.0, 0.0, 0.0), yaw_rate=0.3):
    from awr.sim.sensors.frames import R_to_quat_xyzw, rot_z

    t = np.arange(n) * (1_000_000_000 // hz)
    p = np.array([-60.0, 60.0, 30.0]) + (t / 1e9)[:, None] * np.asarray(v)
    q = np.array([R_to_quat_xyzw(rot_z(yaw_rate * s)) for s in t / 1e9])
    return t, p, q


def test_trajectory_matches_sensor_pose() -> None:
    from awr.sensorsvc.poses import Mount, PoseHistory, sensor_pose

    t, p, q = _base_samples()
    mount = Mount((0.05, 0.0, -0.08), (180.0, 0.0, 0.0))
    H = PoseHistory("u1")
    for k in range(len(t)):
        H.append(int(t[k]), p[k], q[k])
    tr = SensorTrajectory.from_base(t, p, q, mount.xyz_m, mount.rpy_deg)
    tq = np.random.default_rng(5).integers(t[0], t[-1], 500)
    R, P = tr.at(tq)
    for k in range(len(tq)):
        pos, Rws, _ = sensor_pose(H, mount, 0, int(tq[k]))
        assert np.abs(P[k] - pos).max() < 1e-7 and np.abs(R[k] - Rws).max() < 1e-9       # 挂载偏置先合成后插值：差 ~1e-9 m
    with pytest.raises(L0MapError):
        tr.at([t[-1] + 1])
    with pytest.raises(L0MapError):
        SensorTrajectory(t[::-1], p, q)


def test_trajectory_from_pose_history_messages() -> None:
    from awr.phys.pose_history import pack_pose_history

    t, p, q = _base_samples(100)
    st = np.zeros((len(t), 16))
    st[:, 0:3], st[:, 3:7] = p, q
    msgs = [pack_pose_history("u1", 250, t[:60], st[:60]), pack_pose_history("u1", 250, t[59:], st[59:])]   # 跨消息重复一个样本
    tr = SensorTrajectory.from_pose_history(msgs, (0.0, 0.0, 0.0), (180.0, 0.0, 0.0))
    ref = SensorTrajectory.from_base(t, p, q, (0.0, 0.0, 0.0), (180.0, 0.0, 0.0))
    assert np.array_equal(tr.t_ns, ref.t_ns) and np.allclose(tr.p, ref.p) and np.allclose(np.abs(tr.q), np.abs(ref.q))


def test_world_rays_filters_depth_tag_and_placeholders() -> None:
    t, p, q = _base_samples(100, v=(0.0, 0.0, 0.0), yaw_rate=0.0)
    tr = SensorTrajectory.from_base(t, p, q)
    pts = np.zeros(6, LIDAR_RECORD)
    pts["x"] = [10.0, 59.9, 60.1, 0.0, 20.0, np.nan]
    pts["tag"] = [0, 0, 0, 0, 1, 0]
    pts["offset_time"] = [0, 5000, 10000, 15000, 20000, 25000]
    o, e, cnt = world_rays(int(t[10]), pts, tr)
    assert cnt == {"points": 6, "used": 2, "dropped_depth": 1, "dropped_tag": 1, "dropped_invalid": 2}
    assert np.allclose(o, p[10]) and np.allclose(e[:, 0] - o[:, 0], [10.0, 59.9], atol=1e-5)
    o2, _, cnt2 = world_rays(int(t[10]), pts, tr, L0Params(max_depth_m=100.0, tag_drop_mask=0))
    assert cnt2["used"] == 4 and len(o2) == 4


# ---------------------------------------------------------------------------------------------------------------- 合成世界：MeshLidar → L0
@pytest.fixture(scope="module")
def q(tmp_path_factory: pytest.TempPathFactory):
    pytest.importorskip("open3d")
    pytest.importorskip("PIL")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "world"))
    from synthworld import build_synth_world

    from awr.world.mesh.query import open_mesh_world_query

    tmp = tmp_path_factory.mktemp("l0")
    mp = pytest.MonkeyPatch()
    mp.setenv("AWR_D3_ENABLED", "1")
    try:
        yield open_mesh_world_query(build_synth_world(tmp, mp), tmp / "cache")
    finally:
        mp.undo()


def fly(q, start, v, frames: int, *, tier="clean", yaw_rate=0.0, uav_no=0, seed=3):
    """倒装 MID-360 匀速飞行：250 Hz 机体真值 → 传感器轨迹；扫描节点取自同一轨迹。返回 (轨迹, [(timebase, points)])。"""
    from awr.sensorsvc.lidar.mesh import MeshLidar
    from awr.sensorsvc.lidar.model import load_lidar_model
    from awr.sim.sensors.frames import R_to_quat_xyzw, rot_z

    m = load_lidar_model()
    n = frames * 25 + 2
    t = np.arange(n) * 4_000_000
    p = np.asarray(start, float) + (t / 1e9)[:, None] * np.asarray(v, float)
    qb = np.array([R_to_quat_xyzw(rot_z(yaw_rate * s)) for s in t / 1e9])
    tr = SensorTrajectory.from_base(t, p, qb, (0.0, 0.0, -0.05), (180.0, 0.0, 0.0))
    lid = MeshLidar(q, m)
    out = []
    for f in range(frames):
        tb = f * m.frame_ns
        R, P = tr.at(tb + np.arange(m.pose_nodes) * (m.frame_ns // (m.pose_nodes - 1)))
        out.append((tb, lid.scan(f, tb, list(zip(R, P, strict=True)), tier=tier, seed=seed, uav_no=uav_no).points))
    return tr, out


def test_per_point_pose_integration_removes_motion_distortion(q) -> None:
    """8 m/s（水平 6 m/s、爬升 5.3 m/s）、0.6 rad/s 偏航：逐点位姿积分的占据体素中心全部在网格半对角线（0.217 m）内；
    按帧首位姿积分时地面点随帧内爬升偏离，p90 > 0.3 m（水平运动在平地上不改变点到网格的距离，故取爬升）。"""
    tr, frames = fly(q, (-60.0, 60.0, 30.0), (6.0, 0.0, 5.3), 4, yaw_rate=0.6)
    m = L0Map(uav="u1").integrate(frames, tr)
    d = mesh_distance(m.occupied_centers(), q)
    assert len(d) > 2000 and d.max() <= HALF_DIAG + 1e-3 and np.median(d) < 0.13
    flat = L0Map()
    for tb, pts in frames:
        R, P = tr.at([tb])
        xyz = np.stack([pts["x"], pts["y"], pts["z"]], 1).astype(np.float64)
        keep = np.linalg.norm(xyz, axis=1) <= 60.0
        flat.integrate_scan(np.repeat(P, keep.sum(), 0), xyz[keep] @ R[0].T + P[0])
    df = mesh_distance(flat.occupied_centers(), q)
    assert np.percentile(df, 90) > 0.3 and (df > HALF_DIAG + 1e-3).mean() > 0.3
    st = m.stats()
    assert st["frames"] == 4 and st["used"] + st["dropped_depth"] + st["dropped_tag"] + st["dropped_invalid"] == st["points"]
    assert st["dropped_depth"] > 0 and st["occupied_voxels"] == len(d)


def test_three_uav_coverage_and_jaccard(q, tmp_path: Path) -> None:
    """3 机：两机相距 200 m（Jaccard 0），一机与 u1 航线错开 15 m（部分重叠）；结果与暴力集合计算一致。"""
    plan = {"u1": (-90.0, 90.0), "u2": (110.0, 90.0), "u3": (-75.0, 90.0)}
    maps = {}
    for k, (u, (x, y)) in enumerate(plan.items()):
        tr, frames = fly(q, (x, y, 30.0), (5.0, 0.0, 0.0), 3, tier="realistic", uav_no=k)
        maps[u] = L0Map(uav=u).integrate(frames, tr)
    rep = fleet_report(maps, q)
    json.dumps(rep)
    surf = {u: {tuple(v) for v in np.floor_divide(m.occupied_voxels(), 2)} for u, m in maps.items()}
    ref = set()
    for m in maps.values():
        ref |= {tuple(v) for v in unpack_keys(m.hit_surface_keys())}
    for u, w in combinations(sorted(surf), 2):
        assert rep["jaccard"][f"{u}|{w}"] == pytest.approx(len(surf[u] & surf[w]) / len(surf[u] | surf[w]))
    for u in surf:
        assert rep["coverage"][u] == pytest.approx(len(surf[u] & ref) / len(ref))
    union = set().union(*surf.values())
    assert rep["merged_coverage"] == pytest.approx(len(union & ref) / len(ref)) and rep["s_ref_voxels"] == len(ref)
    assert rep["jaccard"]["u1|u2"] == 0.0 and rep["jaccard"]["u2|u3"] == 0.0 and 0.02 < rep["jaccard"]["u1|u3"] < 1.0
    assert rep["jaccard_max"] == rep["jaccard"]["u1|u3"] and rep["jaccard_ok"] == (rep["jaccard_max"] <= JACCARD_MAX)
    assert rep["merged_coverage"] >= max(rep["coverage"].values()) and rep["merged_over_best"] >= 1.0
    md = rep["per_uav"]["u1"]["mesh_distance"]
    assert md["n"] == rep["per_uav"]["u1"]["occupied_voxels"] and md["median_m"] < 0.15 and md["within_half_diag"] > 0.95
    same = coverage_and_jaccard({"a": maps["u1"].surface_keys(), "b": maps["u1"].surface_keys()})
    assert same["jaccard"]["a|b"] == 1.0 and same["merged_coverage"] == 1.0
    # 合并（log-odds 相加）与持久化
    mg = maps["u1"].merge(maps["u2"])
    assert set(map(tuple, mg.occupied_voxels())) == set(map(tuple, maps["u1"].occupied_voxels())) | set(map(tuple, maps["u2"].occupied_voxels()))
    p = maps["u3"].save(tmp_path / "u3.npz")
    back = L0Map.load(p)
    for x, y in zip(back._block_arrays(), maps["u3"]._block_arrays(), strict=True):
        assert np.array_equal(x, y)
    assert np.array_equal(back.hit_surface_keys(), maps["u3"].hit_surface_keys()) and back.counts == maps["u3"].counts


def test_merge_adds_logodds_and_clamps() -> None:
    a, b = L0Map(), L0Map()
    o, e = np.array([[0.1, 0.1, 0.1]]), np.array([[2.1, 0.1, 0.1]])
    for _ in range(3):
        a.integrate_scan(o, e)
    for _ in range(2):
        b.integrate_scan(o, e)
    lo, obs = a.merge(b).logodds(np.array([[8, 0, 0], [0, 0, 0], [8, 8, 8]]))
    assert np.allclose(lo, [3.5, -2.0, 0.0]) and obs.tolist() == [True, True, False]
    with pytest.raises(ValueError):
        a.merge(L0Map(L0Params(voxel_m=0.5, surface_voxel_m=1.0)))


def test_mesh_error_u8_quantization() -> None:
    d = np.array([0.0, 0.001, 0.0011, 0.1, 0.4999, 0.5, 0.73, 5.0])
    u = mesh_error_u8(d)
    assert u.dtype == np.uint8 and u.tolist() == [0, 1, 1, 50, 250, 250, 250, 250]
    x = np.random.default_rng(6).uniform(0, 0.5, 10_000)
    assert np.abs(mesh_error_u8(x) * 0.002 - x).max() <= 0.001 + 1e-12                # 与 eval_sim 抽样 1 万点差 ≤ 1 mm（M27-AC-025）
