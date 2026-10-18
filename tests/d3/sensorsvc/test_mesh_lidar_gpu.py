"""Warp GPU 网格 LiDAR 产品路径（D3-AC-12；M25 §6.8、M25-FR-070、M25-AC-012/021）：与 Open3D CPU 参考同一算法与随机键。

阈值逐条对照 docs/05 §11.2 D3-AC-12：MID-360 花样每帧 20,000 射线、逐点位姿 K = 11、语义反射率、32 个动态实体、天气开；
与 Open3D CPU 参考（均为瓦片局部原点 float32 求交，同一批射线）逐射线距离差 p99 ≤ 1 mm、最大 ≤ 1 cm，命中判定不一致 ≤ 1e-4。
另断言（M25-AC-012/021）：同键随机数逐位一致；GPU 花样与 `pattern.py` 角度差 ≤ 0.03°；批大小 1 与 16、批内次序打乱时逐帧字节一致，
两次运行逐字节一致；有效点经 GPU 前缀和压缩后按槽位升序、字段与 `LIDAR_RECORD` 相同。吞吐（100 架 p95 ≤ 30 ms 等）在
`tools/bench/d3/lidar_concurrency.py`（S16）与独占卡（S21）上验收，这里只在真实世界用例中记录参考耗时。

构造场景：3 × 3 个 80 m 瓦片，世界原点偏移 (12 km, −8 km, 150 m)（检验瓦片局部 float32），起伏地面加带坡屋顶的楼块（语义码
地面、道路、建筑、屋顶），共约 4 万个三角形；32 个演员（行人、车辆、狗、物体）写成磁带，含加速转弯、帧内出现与离场。
帧起点分对齐磁带帧与跨磁带帧边界两种；机体 8 m/s 平移叠加 90°/s 偏航，倒装 MID-360。

Warp 用例在有 CUDA 时用 GPU，否则用 Warp 的 CPU 设备（同一内核）；真实世界用例（oryahovo-crop1，2.9M 三角形）需要 CUDA 与已构建的
世界包。环境变量 `AWR_LIDAR_GPU_REPORT=<path>` 时把对拍分布与耗时写成 JSON（验收记录引用）。
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("open3d")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from awr.sensorsvc.lidar import warp_mesh as wm
from awr.sensorsvc.lidar.mesh import INST_NONE, LIDAR_RECORD, MeshLidar, _ActorTrack
from awr.sensorsvc.lidar.model import load_lidar_model
from awr.sim.sensors.frames import mount_R

P99_MM, MAX_CM, HIT_MISMATCH = 1e-3, 1e-2, 1e-4          # D3-AC-12
PATTERN_DEG = 0.03                                         # M25-AC-021
W0 = np.array([12_000.0, -8_000.0, 150.0])                 # 场景世界偏移
TILE_M, N_TILE = 80.0, 3
SEM_GROUND, SEM_ROAD, SEM_BUILDING, SEM_ROOF = 1, 2, 6, 5
R_INV = mount_R((180.0, 0.0, 0.0))
T_ALIGNED, T_CROSS = 1_000_000_000, 1_137_500_000
N_T = 30
_REPORT: dict = {}


# ---------------------------------------------------------------- 构造场景


def _ground_z(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return W0[2] + 0.4 * np.sin(x / 7.0) + 0.3 * np.cos(y / 5.0)


def _box(c: np.ndarray, sx: float, sy: float, h: float, roof: float) -> tuple[np.ndarray, np.ndarray]:
    """楼块：四面墙（建筑）与沿 x 方向起脊的双坡屋顶（屋顶），返回 (三角形 (n, 3, 3), 语义码 (n,))。"""
    x0, x1, y0, y1 = c[0] - sx / 2, c[0] + sx / 2, c[1] - sy / 2, c[1] + sy / 2
    z0, z1, zr = c[2] - 1.0, c[2] + h, c[2] + h + roof
    q = lambda a, b, cc, d: [[a, b, cc], [a, cc, d]]                                  # noqa: E731
    walls = (q([x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1]) + q([x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [x1, y0, z1])
             + q([x1, y1, z0], [x0, y1, z0], [x0, y1, z1], [x1, y1, z1]) + q([x0, y1, z0], [x0, y0, z0], [x0, y0, z1], [x0, y1, z1]))
    ym = 0.5 * (y0 + y1)
    roofs = (q([x0, y0, z1], [x1, y0, z1], [x1, ym, zr], [x0, ym, zr]) + q([x0, ym, zr], [x1, ym, zr], [x1, y1, z1], [x0, y1, z1])
             + [[[x0, y0, z1], [x0, ym, zr], [x0, y1, z1]], [[x1, y0, z1], [x1, y1, z1], [x1, ym, zr]]])
    T = np.array(walls + roofs, np.float64)
    return T, np.r_[np.full(len(walls), SEM_BUILDING), np.full(len(roofs), SEM_ROOF)].astype(np.uint8)


def build_scene(seed: int = 12):
    """3 × 3 瓦片的 MeshWorldQuery（不经 DSM 与世界包，只设网格求交所需字段）。"""
    import open3d as o3d

    from awr.world.mesh.query import MeshTile, MeshWorldQuery

    rng = np.random.default_rng(seed)
    tiles, sems, t0 = [], [], 0
    for iy in range(N_TILE):
        for ix in range(N_TILE):
            org = W0 + np.array([ix * TILE_M, iy * TILE_M, 0.0])
            xs = org[0] + np.arange(0.0, TILE_M + 1e-9, 2.0)
            ys = org[1] + np.arange(0.0, TILE_M + 1e-9, 2.0)
            X, Y = np.meshgrid(xs, ys)
            G = np.stack([X, Y, _ground_z(X, Y)], -1)
            a, b, c, d = G[:-1, :-1], G[:-1, 1:], G[1:, 1:], G[1:, :-1]
            tri = [np.stack([a, b, c], -2).reshape(-1, 3, 3), np.stack([a, c, d], -2).reshape(-1, 3, 3)]
            cx = 0.5 * (a[..., 0] + c[..., 0]) - org[0]
            road = (np.abs(cx - 40.0) < 4.0).ravel()
            sem = [np.where(road, SEM_ROAD, SEM_GROUND).astype(np.uint8)] * 2
            for _ in range(3):
                cc = org + np.array([rng.uniform(14, 66), rng.uniform(14, 66), 0.0])
                if abs(cc[0] - org[0] - 40.0) < 12:
                    cc[0] += 24.0 * (1 if cc[0] - org[0] < 40 else -1)
                cc[2] = float(_ground_z(cc[0], cc[1]))
                T, s = _box(cc, rng.uniform(6, 14), rng.uniform(6, 14), rng.uniform(5, 22), rng.uniform(1, 4))
                tri.append(T)
                sem.append(s)
            T = np.concatenate(tri)
            S = np.concatenate(sem)
            V = np.ascontiguousarray((T.reshape(-1, 3) - org).astype(np.float32))
            F = np.arange(len(V), dtype=np.uint32).reshape(-1, 3)
            sc = o3d.t.geometry.RaycastingScene()
            sc.add_triangles(o3d.core.Tensor(V), o3d.core.Tensor(F))
            Wv = V.astype(np.float64) + org
            tiles.append(MeshTile(len(tiles), org, Wv.min(0), Wv.max(0), t0, len(F), sc, V, F))
            sems.append(S)
            t0 += len(F)
    q = MeshWorldQuery.__new__(MeshWorldQuery)
    q.tiles, q.n_triangles, q.semantic = tiles, t0, np.concatenate(sems)
    q._zlo = min(float(t.lo[2]) for t in tiles)
    q._zhi = max(float(t.hi[2]) for t in tiles)
    return q


@pytest.fixture(scope="module")
def scene():
    return build_scene()


# ---------------------------------------------------------------- 演员磁带（32 个）

KINDS = [("person", [500, 300, 1750]), ("vehicle", [4500, 1800, 1500]), ("dog", [800, 300, 600]), ("object", [1200, 1200, 1000])]


def actors32() -> list[dict]:
    out = []
    for i in range(32):
        cls, size = KINDS[i % 4]
        out.append({"idx": i, "id": f"a{i:02d}", "class": cls, "scripted": i < 16, "profile": "person" if cls != "vehicle" else "vehicle",
                    "size_mm": size, "appearance": {}})
    return out


def _actor_traj(i: int, t: np.ndarray, c: np.ndarray):
    """演员 i 的解析轨迹（位置、速度、航向）：环绕 c 的不同半径与相位，部分加速转弯。"""
    rng = np.random.default_rng(1000 + i)
    r0, ph = rng.uniform(4.0, 26.0), rng.uniform(0, 2 * math.pi)
    v = rng.uniform(0.8, 9.0) if i % 4 == 1 else rng.uniform(0.5, 1.6)
    acc = rng.uniform(0.5, 2.0) if i % 3 == 0 else 0.0
    hd = ph + math.pi / 2 + rng.uniform(-0.6, 0.6)
    s = v * t + 0.5 * acc * t * t
    sp = v + acc * t
    turn = 0.15 * (1 if i % 2 else -1) * t
    x = c[0] + r0 * math.cos(ph) + s * np.cos(hd + turn)
    y = c[1] + r0 * math.sin(ph) + s * np.sin(hd + turn)
    vx, vy = sp * np.cos(hd + turn), sp * np.sin(hd + turn)
    z = _ground_z(x, y) - 0.05
    return np.stack([x, y, z], 1), np.stack([vx, vy, np.zeros_like(t)], 1), np.arctan2(vy, vx)


def write_tape(d: Path, actors: list[dict], c: np.ndarray) -> Path:
    from awr.sim.events import tape as tp

    t = np.arange(N_T) * 0.1
    A = len(actors)
    f = np.zeros((N_T, A), tp.RECORD)
    f["container"] = tp.NO_CONTAINER
    for a in range(A):
        p, v, yaw = _actor_traj(a, t, c)
        f["x_mm"][:, a], f["y_mm"][:, a], f["z_mm"][:, a] = (np.round(p[:, k] * 1000).astype(np.int64) for k in range(3))
        f["vx_cmps"][:, a], f["vy_cmps"][:, a] = (np.round(v[:, k] * 100).astype(np.int64) for k in range(2))
        f["yaw_cdeg"][:, a] = np.round(np.degrees(yaw) * 100).astype(np.int64)
        f["state"][:, a] = 1
    for a in range(3, A, 7):
        f["state"][:12, a] = 0                                            # 1.2 s 出现（跨边界帧内阶跃）
    for a in range(5, A, 9):
        f["state"][12:, a] = 0                                            # 1.2 s 离场
    h64 = "0" * 64
    hd = {"schema": "drone4d.tape.v1", "event_id": "ev-gpu", "event_version": 1, "event_sha256": h64, "mode": "exogenous", "seed": 0,
          "tick_hz": 10, "n_ticks": N_T, "duration_ms": N_T * 100, "record": tp.record_spec(),
          "codec": {"name": "col-delta-zstd@1", "block_ticks": 100}, "actors": actors,
          "world": {"id": "w1", "content_version": "0" * 12, "raycast_sha256": h64, "navmesh_sha256": {"person": h64},
                    "anchors_sha256": h64}, "environment": {}, "env_track": [], "comms": {}, "anim": None, "realized": None}
    h = tp.write_tape(d / "tape.bin", hd, f, n_scripted=16)
    (d / "tape.json").write_text(json.dumps({"content_sha256": h["content_sha256"]}))
    return d / "tape.bin"


CENTER = W0 + np.array([120.0, 120.0, 0.0])


@pytest.fixture(scope="module")
def tape(tmp_path_factory: pytest.TempPathFactory):
    pytest.importorskip("zstandard")
    from awr.sim.events.tape import TapeReader

    return TapeReader(write_tape(tmp_path_factory.mktemp("gpu-tape"), actors32(), CENTER))


def nodes(p0, *, v=(0.0, 0.0, 0.0), yaw0: float = 0.0, rate_dps: float = 0.0, K: int = 11):
    """K 个位姿节点（10 ms 间隔）：匀速平移叠加匀角速度偏航，倒装 MID-360。"""
    out = []
    for k in range(K):
        t = 0.01 * k
        y = math.radians(yaw0 + rate_dps * t)
        Rz = np.array([[math.cos(y), -math.sin(y), 0.0], [math.sin(y), math.cos(y), 0.0], [0.0, 0.0, 1.0]])
        out.append((Rz @ R_INV, np.asarray(p0, float) + np.asarray(v, float) * t))
    return out


def poses():
    """覆盖瓦片边界、楼块近处与不同高度的位姿（8 m/s 平移叠加 90°/s 偏航）。"""
    return [nodes(CENTER + np.array([0.0, 0.0, 12.0]), v=(8.0, 0.0, 0.0), yaw0=10.0, rate_dps=90.0),
            nodes(W0 + np.array([80.0, 80.0, 25.0]), v=(0.0, 8.0, 0.0), yaw0=-40.0, rate_dps=-90.0),
            nodes(W0 + np.array([35.0, 150.0, 6.0]), v=(5.6, 5.6, 0.0), yaw0=75.0, rate_dps=90.0),
            nodes(W0 + np.array([200.0, 40.0, 20.0]))]


# ---------------------------------------------------------------- CPU 参考的逐射线记录


def cpu_scan(lid: MeshLidar, *args, **kw):
    """CPU 参考扫描并记录逐射线结果（静态求交与演员求交后的 t、inst），不改动参考实现：在实例上临时包装 `_cast` 与 `_actors`。"""
    q = lid.q
    rec: dict = {}
    cast0, act0 = q._cast, lid._actors

    def cast(o, d, tmax):
        r = cast0(o, d, tmax)
        rec["t"], rec["inst"] = r[0], np.zeros(len(o), np.int64)
        return r

    def actors(o, d, t, nrm, sem, inst, *a):
        n = act0(o, d, t, nrm, sem, inst, *a)
        rec["t"], rec["inst"] = t, inst
        return n

    q._cast, lid._actors = cast, actors
    try:
        sc = lid.scan(*args, **kw)
    finally:
        del q._cast, lid._actors
    return sc, rec


def ray_diff(rec: dict, rays: dict, f: int = 0, N: int = 20000) -> dict:
    """逐射线对拍：命中判定不一致率与双方都命中时的距离差分布。"""
    tc = rec["t"]
    tg = rays["t"][f * N:(f + 1) * N]
    hc, hg = np.isfinite(tc), tg >= 0
    both = hc & hg
    dd = np.abs(tc[both] - tg[both])
    return {"rays": int(N), "hit_cpu": int(hc.sum()), "hit_gpu": int(hg.sum()), "hit_mismatch": int((hc != hg).sum()),
            "inst_mismatch": int((rec["inst"][both] != rays["inst"][f * N:(f + 1) * N][both]).sum()),
            "d_p50_m": float(np.percentile(dd, 50)) if dd.size else 0.0, "d_p99_m": float(np.percentile(dd, 99)) if dd.size else 0.0,
            "d_max_m": float(dd.max()) if dd.size else 0.0}


def merge(ds: list[dict]) -> dict:
    n = sum(d["rays"] for d in ds)
    return {"rays": n, "hit_mismatch_rate": sum(d["hit_mismatch"] for d in ds) / n, "inst_mismatch": sum(d["inst_mismatch"] for d in ds),
            "d_p99_m_max": max(d["d_p99_m"] for d in ds), "d_max_m": max(d["d_max_m"] for d in ds)}


def assert_ac12(ds: list[dict]) -> dict:
    m = merge(ds)
    assert m["hit_mismatch_rate"] <= HIT_MISMATCH, m
    assert m["d_p99_m_max"] <= P99_MM and m["d_max_m"] <= MAX_CM, m
    return m


def point_parity(a, b) -> dict:
    """观测帧对拍：有效槽位集合、字段与坐标（坐标为 float32，允许舍入差）。"""
    sa = (a.points["offset_time"] // 5000).astype(np.int64)
    sb = (b.points["offset_time"] // 5000).astype(np.int64)
    common, ia, ib = np.intersect1d(sa, sb, return_indices=True)
    pa, pb = a.points[ia], b.points[ib]
    xyz = lambda p: np.stack([p["x"], p["y"], p["z"]], 1).astype(np.float64)       # noqa: E731
    dx = np.linalg.norm(xyz(pa) - xyz(pb), axis=1)
    return {"n_a": len(sa), "n_b": len(sb), "slot_sym_diff": int(len(sa) + len(sb) - 2 * len(common)),
            "xyz_max_m": float(dx.max()) if dx.size else 0.0,
            "refl_diff": int((pa["reflectivity"] != pb["reflectivity"]).sum()), "tag_diff": int((pa["tag"] != pb["tag"]).sum()),
            "gt_inst_diff": int((a.gt_inst != b.gt_inst).sum()), "gt_flags_diff": int((a.gt_flags != b.gt_flags).sum())}


@pytest.fixture(scope="module")
def warp_dev():
    pytest.importorskip("warp")
    wp = wm._wp()
    return "cuda:0" if wp.get_cuda_device_count() > 0 else "cpu"


@pytest.fixture(scope="module")
def glid(scene, warp_dev):
    return wm.WarpMeshLidar(scene, load_lidar_model(), actors32(), device=warp_dev)


# ---------------------------------------------------------------- 主机端（不需要 warp）


def test_beta_905_weather_formula() -> None:
    """M25 §6.8.5：10 mm/h 雨 1.16e-3 /m；V = 200 m 雾按 Kim（q = 0）为 3.912/200；二者相加；晴天为 0。"""
    assert wm.beta_905_per_m(None, 10.0) == pytest.approx(1.16e-3, rel=2e-3)
    assert wm.beta_905_per_m(200.0) == pytest.approx(3.912 / 200.0)
    assert wm.beta_905_per_m(3000.0) == pytest.approx(3.912 / 3000.0 * (905 / 550) ** -(0.16 * 3 + 0.34))
    assert wm.beta_905_per_m(200.0, 10.0) == pytest.approx(wm.beta_905_per_m(200.0) + wm.beta_905_per_m(None, 10.0))
    assert wm.beta_905_per_m() == 0.0


@pytest.mark.parametrize("t0", [T_ALIGNED, T_CROSS])
def test_actor_segments_equal_tape_time_function(tape, t0: int) -> None:
    """分段表在每个槽位时刻与磁带时刻函数的直接求值一致（位置 ≤ 1 µm、航向 ≤ 1 µrad、可见性相同）；对齐磁带帧时每个演员一段、
    全帧 6 次查询；跨边界时分段定位边界，查询次数有界。"""
    m = load_lidar_model()
    t_slots = t0 + np.arange(m.slots_per_frame, dtype=np.int64) * m.point_dt_ns
    tr = _ActorTrack(tape.actor_states, 32, int(t_slots[0]), int(t_slots[-1]))
    sl = np.unique(np.r_[0, 1, 2, 3, 4, 5, m.slots_per_frame - 1, np.random.default_rng(t0 % 991).integers(0, m.slots_per_frame, 300)])
    ref = [tape.actor_states(int(x)) for x in t_slots[sl]]
    n_segs = []
    for a in range(32):
        sg = wm.actor_segments(tr, a, t_slots)
        n_segs.append(len(sg.lo))
        assert (np.diff(sg.lo) > 0).all() and sg.lo[0] == t_slots[0]
        pos, yaw, st = wm.eval_segments(sg, t_slots[sl])
        S = np.array([r.state[a] for r in ref])
        assert np.array_equal(st, S)
        vis = np.isin(S, (1, 2))
        if vis.any():
            P = np.array([r.pos_enu_m[a] for r in ref])
            Y = np.array([r.yaw_rad[a] for r in ref])
            assert np.linalg.norm(pos[vis] - P[vis], axis=1).max() <= 1e-6
            assert np.abs((yaw[vis] - Y[vis] + np.pi) % (2 * np.pi) - np.pi).max() <= 1e-6
    if t0 == T_ALIGNED:
        assert set(n_segs) == {1} and tr.calls == 6
    else:
        assert max(n_segs) <= 40 and tr.calls <= 200
        a = 3                                                             # 1.2 s 出现：边界两侧槽位的可见性
        sg = wm.actor_segments(tr, a, t_slots)
        k = int(np.searchsorted(t_slots, 1_200_000_000))
        assert wm.eval_segments(sg, t_slots[[k - 1, k]])[2].tolist() == [0, 1]


def test_actor_segments_non_polynomial_time_function() -> None:
    """非多项式（正弦）时刻函数与 None 返回：分段表在全部槽位时刻与直接调用一致（逐时刻样本兜底）。"""
    from awr.sim.events.tape import ActorStates

    def fn(t_ns: int):
        if t_ns < 30_000_000:
            return None
        return ActorStates(np.array([0]), np.array([[3.0 * math.sin(t_ns / 7e6), 0.0, 0.0]]), np.zeros(1), np.zeros((1, 3)),
                           np.array([1], np.uint8), np.array([255], np.uint8))

    t_slots = np.arange(0, 100_000_000, 5_000, dtype=np.int64)
    tr = _ActorTrack(fn, 1, 0, int(t_slots[-1]))
    sg = wm.actor_segments(tr, 0, t_slots)
    pos, _y, st = wm.eval_segments(sg, t_slots)
    assert np.array_equal(st, np.where(t_slots < 30_000_000, 0, 1))
    on = t_slots >= 30_000_000
    assert np.abs(pos[on, 0] - 3.0 * np.sin(t_slots[on] / 7e6)).max() <= 1e-6


def test_backend_selection(scene, monkeypatch: pytest.MonkeyPatch) -> None:
    m = load_lidar_model()
    monkeypatch.setenv("AWR_LIDAR_BACKEND", "cpu")
    assert type(wm.open_mesh_lidar(scene, m)) is MeshLidar
    with pytest.raises(ValueError):
        wm.open_mesh_lidar(scene, m, backend="gpu")
    monkeypatch.setenv("AWR_LIDAR_BACKEND", "auto")
    lid = wm.open_mesh_lidar(scene, m)
    assert type(lid) is (wm.WarpMeshLidar if wm.warp_cuda_available() else MeshLidar)


# ---------------------------------------------------------------- Warp 内核（CUDA 或 Warp CPU 设备）


def test_rng_bitwise_equal_cpu(warp_dev) -> None:
    """同键随机数：Warp 的 sm、U01 与 Box–Muller 和 `cbrng.sm`、`mesh._u01`、`mesh._normal2` 逐位一致（GPU 上 10⁶ 个键）。"""
    from awr.sensorsvc.lidar import mesh as lm
    from awr.sim.sensors import cbrng

    n = 1_000_000 if warp_dev.startswith("cuda") else 100_000
    rng = np.random.default_rng(7)
    keys = rng.integers(0, 2**63, n, dtype=np.uint64) * np.uint64(2) + rng.integers(0, 2, n, dtype=np.uint64)
    keys[:4] = [0, 1, 2**63, 2**64 - 1]
    h, u, nn = wm.rng_warp(keys, warp_dev)
    hc = cbrng.sm(keys)
    assert np.array_equal(h, hc) and np.array_equal(u, lm._u01(hc))
    n0, n1 = lm._normal2(hc)
    e = np.abs(nn - np.stack([n0, n1], 1))
    assert e.max() <= 1e-12 * max(1.0, float(np.abs(nn).max()))           # log/cos 的设备实现与 libm 差 ≤ 数 ulp


def test_static_parity_and_pattern_clean(scene, glid) -> None:
    """clean 档静态场景：逐射线命中不一致 ≤ 1e-4、距离差 p99 ≤ 1 mm、最大 ≤ 1 cm；观测帧有效槽位与真值槽位一致，点坐标差 ≤ 0.1 mm；
    点方向与 `pattern.py` 名义方向角度差 ≤ 0.03°。"""
    from awr.sim.sensors.lidar.pattern import directions_flu, mid360_pattern

    m = load_lidar_model()
    clid = MeshLidar(scene, m)
    ds, pp = [], []
    for k, nd in enumerate(poses()):
        seq = 3 + 7 * k
        c, rec = cpu_scan(clid, seq, T_ALIGNED, nd, tier="clean")
        g = glid.scan_batch([wm.LidarFrameReq(seq, T_ALIGNED, nd, tier="clean")], rays=True)[0]
        ds.append(ray_diff(rec, glid.last_rays))
        pp.append(point_parity(c, g))
        assert len(g.points) > 3000 and g.points.dtype == LIDAR_RECORD
        az, el = np.empty(m.slots_per_frame), np.empty(m.slots_per_frame)
        mid360_pattern(seq, az, el)
        d_nom = directions_flu(az, el)
        slot = (g.points["offset_time"] // 5000).astype(np.int64)
        xyz = np.stack([g.points["x"], g.points["y"], g.points["z"]], 1).astype(np.float64)
        cosang = np.einsum("ij,ij->i", xyz / np.linalg.norm(xyz, axis=1, keepdims=True), d_nom[slot])
        assert np.degrees(np.arccos(np.clip(cosang, -1, 1))).max() <= PATTERN_DEG
    _REPORT["static_clean"] = assert_ac12(ds)
    for p in pp:
        assert p["slot_sym_diff"] <= 2 * HIT_MISMATCH * 20000 + 2 and p["xyz_max_m"] <= 1e-4 and p["gt_inst_diff"] <= 4, p
    _REPORT["static_clean_points"] = pp


@pytest.mark.parametrize("tier", ["realistic", "harsh"])
def test_noise_tiers_same_keys(scene, glid, tier: str) -> None:
    """realistic、harsh 档：角度、测距噪声与虚警按同一帧键与槽位键抽样，观测帧与真值逐槽位一致（舍入差引起的个别槽位除外）；
    逐射线对拍满足 D3-AC-12。"""
    m = load_lidar_model()
    clid = MeshLidar(scene, m)
    ds = []
    for k, nd in enumerate(poses()[:3]):
        kw = {"seed": 11 + k, "uav_no": 2 + k, "sensor_no": k % 2, "tier": tier}
        c, rec = cpu_scan(clid, 40 + k, T_ALIGNED, nd, **kw)
        g = glid.scan_batch([wm.LidarFrameReq(40 + k, T_ALIGNED, nd, **kw)], rays=True)[0]
        ds.append(ray_diff(rec, glid.last_rays))
        p = point_parity(c, g)
        assert p["slot_sym_diff"] <= 4 and p["xyz_max_m"] <= 1e-4 and p["tag_diff"] == 0 and p["gt_flags_diff"] <= 4, p
        assert (g.points["tag"] == 1).sum() == (c.points["tag"] == 1).sum()
    _REPORT[f"static_{tier}"] = assert_ac12(ds)


@pytest.mark.parametrize("t0", [T_ALIGNED, T_CROSS])
def test_actor_parity_32_entities_weather(scene, glid, tape, t0: int) -> None:
    """32 个动态实体（逐射线时刻）+ 天气开（雾 V = 300 m、雨 10 mm/h）+ realistic 档：逐射线对拍满足 D3-AC-12，演员命中实例与 CPU 一致，
    演员回波数、最近交点为演员的射线数相同；天气使有效点减少。"""
    m = load_lidar_model()
    clid = MeshLidar(scene, m, actors32())
    beta = wm.beta_905_per_m(300.0, 10.0)
    ds, n_act = [], 0
    for k, nd in enumerate([nodes(CENTER + np.array([0.0, 0.0, 7.0]), v=(8.0, 0.0, 0.0), yaw0=10.0, rate_dps=90.0),
                            nodes(CENTER + np.array([-10.0, 6.0, 4.0]), v=(0.0, -8.0, 0.0), yaw0=200.0, rate_dps=-90.0)]):
        kw = {"seed": 5, "uav_no": k, "tier": "realistic", "beta_905": beta}
        c, rec = cpu_scan(clid, 10 + k, t0, nd, actor_states=tape.actor_states, **kw)
        g = glid.scan_batch([wm.LidarFrameReq(10 + k, t0, nd, **kw)], actor_states=tape.actor_states, rays=True)[0]
        ds.append(ray_diff(rec, glid.last_rays))
        assert ds[-1]["inst_mismatch"] <= 2, ds[-1]
        assert abs(g.actor_rays - c.actor_rays) <= 2 and abs(g.actor_hits - c.actor_hits) <= 2
        n_act += g.actor_rays
        p = point_parity(c, g)
        assert p["slot_sym_diff"] <= 6 and p["xyz_max_m"] <= 1e-4, p
        clear = glid.scan(10 + k, t0, nd, actor_states=tape.actor_states, seed=5, uav_no=k, tier="realistic")
        assert len(g.points) < len(clear.points)
    assert n_act > 200                                                     # 演员确实被大量射线命中
    hit_ids = np.unique(np.r_[rec["inst"][rec["inst"] > 0]])
    assert len(hit_ids) >= 4
    _REPORT[f"actors32_weather_{'aligned' if t0 == T_ALIGNED else 'cross'}"] = {**assert_ac12(ds), "actor_rays": int(n_act),
                                                                                "segments": glid.last_timing["segments"]}


def test_batch_invariance_and_determinism(glid, tape) -> None:
    """批大小 1 与 16、批内次序打乱、重复运行：逐帧观测与真值逐字节一致（M25-AC-012）；有效点按槽位升序、line = 槽位 & 3、
    有效点数等于真值中非"无回波"槽位数（GPU 前缀和压缩）。"""
    reqs = []
    for k in range(16):
        nd = poses()[k % 4]
        reqs.append(wm.LidarFrameReq(100 + k, T_ALIGNED if k % 3 else T_CROSS, nd, seed=k % 5, uav_no=k, sensor_no=k % 2,
                                     tier=("clean", "realistic", "harsh")[k % 3], beta_905=0.002 * (k % 2)))
    one = [glid.scan_batch([r], actor_states=tape.actor_states)[0] for r in reqs]
    full = glid.scan_batch(reqs, actor_states=tape.actor_states)
    perm = np.random.default_rng(3).permutation(16)
    shuf = glid.scan_batch([reqs[i] for i in perm], actor_states=tape.actor_states)
    again = glid.scan_batch(reqs, actor_states=tape.actor_states)
    key = lambda s: (s.points.tobytes(), s.gt_inst.tobytes(), s.gt_flags.tobytes(), s.actor_hits, s.actor_rays)   # noqa: E731
    for k in range(16):
        assert key(one[k]) == key(full[k]) == key(again[k]) == key(shuf[int(np.nonzero(perm == k)[0][0])])
        s = full[k]
        slot = (s.points["offset_time"] // 5000).astype(np.int64)
        assert (np.diff(slot) > 0).all() and (s.points["line"] == slot & 3).all() and (s.points["pad"] == 0).all()
        assert len(s.points) == int((s.gt_inst != INST_NONE).sum()) and s.t_start_ns == reqs[k].t_start_ns
    assert glid.last_timing["frames"] == 16 and glid.last_timing["points"] == sum(len(s.points) for s in full)
    views = glid.scan_batch(reqs, actor_states=tape.actor_states, copy=False)            # 锁页回传缓冲区视图：内容与复制模式相同
    assert all(key(views[k]) == key(full[k]) for k in range(16))
    ring = glid._ring[glid._ring_i][0].numpy()
    assert np.shares_memory(views[0].points, ring) and not np.shares_memory(full[0].points, ring)
    v = glid.versions()
    assert v["backend"] == "warp-mesh" and v["warp"] and v["bvh"]


# ---------------------------------------------------------------- 真实世界（CUDA + 世界包）


def _world_dir() -> Path | None:
    for base in (os.environ.get("AWR_WORLDS_DIR"), str(Path(__file__).resolve().parents[3] / "worlds")):
        if base and (Path(base) / "oryahovo-crop1" / "geometry" / "mesh" / "raycast" / "index.json").exists():
            return Path(base) / "oryahovo-crop1"
    return None


def test_real_world_parity_and_timing(tmp_path_factory: pytest.TempPathFactory) -> None:
    """oryahovo-crop1（21 瓦片、2.9M 三角形）：8 帧（街道低空到 40 m、8 m/s + 90°/s）+ 32 个演员 + 天气，GPU 与 CPU 逐射线对拍满足
    D3-AC-12；另记录 100 帧与 400 帧批（每架 1 帧、32 个演员）的分段耗时（共享卡参考值，正式吞吐由 S16/S21 在独占卡上验收）。"""
    pytest.importorskip("warp")
    if not wm.warp_cuda_available():
        pytest.skip("[gpu] no CUDA device for warp")
    wd = _world_dir()
    if wd is None:
        pytest.skip("needs worlds/oryahovo-crop1 (make worlds)")
    pytest.importorskip("zstandard")
    from awr.sim.events.tape import TapeReader
    from awr.world.mesh.query import open_mesh_world_query

    tmp = tmp_path_factory.mktemp("real")
    q = open_mesh_world_query(wd, tmp / "cache")
    m = load_lidar_model()
    centers = np.array([[-250.0, -250.0], [250.0, -250.0], [-250.0, 250.0], [100.0, 120.0], [-400.0, -100.0], [300.0, 60.0],
                        [-120.0, -400.0], [60.0, -120.0]])
    gz = q.ground_dtm(centers)
    hz = q.height_dsm(centers)
    c0 = np.r_[centers[3], gz[3]]
    tape = TapeReader(write_tape(tmp, actors32(), c0))
    acts = actors32()
    clid = MeshLidar(q, m, acts)
    glid = wm.WarpMeshLidar(q, m, acts)
    beta = wm.beta_905_per_m(500.0, 10.0)
    ds = []
    for k, (xy, g0, h0) in enumerate(zip(centers, gz, hz, strict=True)):
        alt = float(max(h0, g0) + (4.0, 10.0, 25.0, 40.0)[k % 4])
        nd = nodes([xy[0], xy[1], alt], v=(8.0 * math.cos(k), 8.0 * math.sin(k), 0.0), yaw0=30.0 * k, rate_dps=90.0 * (-1) ** k)
        kw = {"seed": 21, "uav_no": k, "tier": ("clean", "realistic")[k % 2], "beta_905": beta if k % 3 == 0 else 0.0}
        st = tape.actor_states if k == 3 else None
        _c, rec = cpu_scan(clid, 200 + k, T_ALIGNED, nd, actor_states=st, **kw)
        glid.scan_batch([wm.LidarFrameReq(200 + k, T_ALIGNED, nd, **kw)], actor_states=st, rays=True)
        ds.append(ray_diff(rec, glid.last_rays))
    rep = {"world": "oryahovo-crop1", "frames": ds, "merged": assert_ac12(ds), "versions": glid.versions(), "build_ms": glid.build_ms}
    reqs = [wm.LidarFrameReq(300, T_ALIGNED, nodes([centers[k % 8][0] + k, centers[k % 8][1], float(hz[k % 8] + 12.0)],
                                                     v=(5.0, 0.0, 0.0), rate_dps=20.0), seed=1, uav_no=k) for k in range(100)]
    glid.scan_batch(reqs, actor_states=tape.actor_states)                  # 预热（内核已编译，分配缓冲区）
    tm = []
    for _ in range(5):
        glid.scan_batch(reqs, actor_states=tape.actor_states)
        tm.append(dict(glid.last_timing))
    rep["batch100_timing_ms"] = tm
    rep["batch100_total_ms_median"] = float(np.median([t["total_ms"] for t in tm]))
    for n, cp in ((100, False), (400, False), (400, True)):
        rq = [wm.LidarFrameReq(300, T_ALIGNED, reqs[k % 100].nodes, seed=1, uav_no=k) for k in range(n)]
        glid.scan_batch(rq, actor_states=tape.actor_states, copy=cp)
        tt = []
        for _ in range(5):
            glid.scan_batch(rq, actor_states=tape.actor_states, copy=cp)
            tt.append(glid.last_timing["total_ms"])
        rep[f"batch{n}_{'copy' if cp else 'views'}_total_ms"] = tt
    _REPORT["real_world"] = rep
    assert all(t["points"] > 100 * 2000 for t in tm)


@pytest.fixture(scope="module", autouse=True)
def _write_report():
    yield
    p = os.environ.get("AWR_LIDAR_GPU_REPORT")
    if p and _REPORT:
        Path(p).write_text(json.dumps(_REPORT, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
