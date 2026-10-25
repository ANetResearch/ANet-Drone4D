"""传感器侧"不完整即未知"（M25 §6.6.2、§6.8.6；M22 §7.3.6、M22-FR-043；D3-AC-04 "打到无数据区的 LiDAR 点在真值侧车中带 `void`
标记；相机真值层输出 `invalid_mask`；occupancy 中覆盖外体素为 unknown"）。

夹具沿用 `test_quality_layer` 的带洞合成 GLB 世界（缺角在覆盖外、20 m 内洞、封闭盒子、OSM 水面），另加两处构件：
- 拉伸条带：离地 3 m、80 m × 4 m 的两个三角形（最长边 80 m > max(p99, 6 × 中位边长)，quality 阶段置 stretched）；
- 覆盖外小岛：缺角内 12 m × 12 m 的地面片（不属于最大连通分量，覆盖外，三角形重心在覆盖外）。
构建走完整 GlbPipeline。期望值由独立实现给出：洞缘三角形按全网格焊接后的边界边与洞的方框求得，覆盖判定按覆盖多边形逐点偶奇，
三角形质量位读 `q.u8`；LiDAR 与相机输出逐槽位、逐像素与之完全相等。端到端用例经 `run_capture` 写出真值文件后读回核对。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("open3d")
pytest.importorskip("PIL")
pytest.importorskip("zstandard")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "world"))

import test_quality_layer as tql

from awr.sensorsvc.camera import CameraModel
from awr.sensorsvc.camera.proj import R_FLU_OPT, look_at_R
from awr.sensorsvc.camera.raster import RenderOut
from awr.sensorsvc.lidar.mesh import INST_NONE, MeshLidar
from awr.sensorsvc.lidar.model import load_lidar_model
from awr.sensorsvc.quality import (
    CAM_HOLE_EDGE,
    CAM_OUTSIDE_COVERAGE,
    CAM_STRETCHED,
    CAM_WATER,
    SensorQuality,
    hole_edge_triangles,
)
from awr.sim.sensors.frames import mount_R
from awr.sim.sensors.lidar.pattern import directions_flu, mid360_pattern
from awr.world.glb.derive.occupancy import UNKNOWN, read_states
from awr.world.glb.derive.quality import BIT_HOLE, BIT_NODATA, BIT_STRETCHED, BIT_WATER
from awr.world.glb.raster import trimesh_world
from awr.world.package.streams import read_stream
from awr.world.package.validate import _points_in_rings

STRIP = ((-130.0, -50.0), (60.0, 64.0), 3.0)      # glTF x 区间、z 区间、离地高度 y
ISLET = ((100.0, 112.0), (100.0, 112.0))         # 缺角内（x ≥ 50 且 z ≥ 50 已去掉）
R_INV = mount_R((180.0, 0.0, 0.0))               # 倒装：雷达系 z 朝下
VOID = 1 << 6


# ---------------------------------------------------------------- 夹具


def _quad(x: tuple[float, float], z: tuple[float, float], y: float) -> tuple[np.ndarray, np.ndarray]:
    V = np.array([[x[0], y, z[0]], [x[1], y, z[0]], [x[1], y, z[1]], [x[0], y, z[1]]], np.float64)
    F = []
    for t in ((0, 2, 1), (0, 3, 2)):
        p = V[list(t)]
        F.append(list(t) if np.cross(p[1] - p[0], p[2] - p[0])[1] > 0 else list(t[::-1]))   # 法线朝上（glTF +y）
    return V, np.asarray(F, np.int64)


def _scene() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    V, F, UV = _BASE_SCENE()
    Vs, Fs, UVs = [V.astype(np.float64)], [F.astype(np.int64)], [UV]
    n = len(V)
    for qv, qf in (_quad(STRIP[0], STRIP[1], STRIP[2]), _quad(ISLET[0], ISLET[1], 0.0)):
        Vs.append(qv)
        Fs.append(qf + n)
        UVs.append(np.full((4, 2), 0.5, np.float32))
        n += 4
    return np.vstack(Vs).astype(np.float32), np.vstack(Fs).astype(np.uint32), np.vstack(UVs).astype(np.float32)


_BASE_SCENE = tql._scene


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory):
    tmp = tmp_path_factory.mktemp("sensor-unknown")
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(tql, "_scene", _scene)
        rec, T = tql._write_fixture(tmp, mp)
        res = tql._build(rec, tmp / "worlds")
        assert res.exit_code == 0, res.message
    finally:
        mp.undo()
    return tmp / "worlds" / tql.WID, T, tmp / "cache"


@pytest.fixture(scope="module")
def q(world):
    from awr.world.mesh.query import open_mesh_world_query

    d, _T, cache = world
    return open_mesh_world_query(d, cache)


@pytest.fixture(scope="module")
def sq(world, q) -> SensorQuality:
    d, _T, cache = world
    s = SensorQuality.load(d, q=q, cache_root=cache)
    assert s is not None
    return s


@pytest.fixture(scope="module")
def ref(world):
    """独立参照：求交网格、q.u8、全网格边界边求得的洞缘三角形与覆盖多边形。"""
    d, T, _c = world
    ix = d / "geometry/mesh/raycast/index.json"
    V, F = trimesh_world(ix)
    q8 = read_stream(ix, "q")
    fc = json.loads((d / "semantic/quality.geojson").read_text(encoding="utf-8"))
    ring = np.asarray(next(f for f in fc["features"] if f["properties"]["kind"] == "scan-footprint")["geometry"]["coordinates"][0])[:, :2]
    # 洞缘：焊接（1 mm）后只属于一个三角形的边，且两端点都在洞的方框边上
    key = np.round(V / 1e-3).astype(np.int64)
    _u, wid = np.unique(key, axis=0, return_inverse=True)
    wid = wid.reshape(-1)
    Tw = wid[F]
    e = np.concatenate([Tw[:, [0, 1]], Tw[:, [1, 2]], Tw[:, [2, 0]]])
    e.sort(1)
    owner = np.tile(np.arange(len(F)), 3)
    k = e[:, 0] * (int(wid.max()) + 1) + e[:, 1]
    _uk, inv, cnt = np.unique(k, return_inverse=True, return_counts=True)
    bnd = cnt[inv.reshape(-1)] == 1
    (e0, n0), (e1, n1) = np.sort([tql._to_world(T, tql.HOLE[0], tql.HOLE[0])[:2], tql._to_world(T, tql.HOLE[1], tql.HOLE[1])[:2]], axis=0)
    Vw = np.zeros((int(wid.max()) + 1, 3))
    Vw[wid] = V

    def on_ring(P: np.ndarray) -> np.ndarray:
        inbox = (P[:, 0] >= e0 - 1e-3) & (P[:, 0] <= e1 + 1e-3) & (P[:, 1] >= n0 - 1e-3) & (P[:, 1] <= n1 + 1e-3)
        side = (np.abs(P[:, 0] - e0) < 1e-3) | (np.abs(P[:, 0] - e1) < 1e-3) | (np.abs(P[:, 1] - n0) < 1e-3) | (np.abs(P[:, 1] - n1) < 1e-3)
        return inbox & side

    he = np.zeros(len(F), bool)
    he[owner[bnd & on_ring(Vw[e[:, 0]]) & on_ring(Vw[e[:, 1]])]] = True
    return {"V": V, "F": F, "q8": q8, "ring": ring, "he": he, "hole_box": (e0, n0, e1, n1), "T": T}


def _w(T: np.ndarray, x: float, z: float, y: float = 0.0) -> np.ndarray:
    return tql._to_world(T, x, z, y)


def _rays(model, frame_seq: int, R: np.ndarray, p0: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """静止位姿下（11 个节点相同）clean 档的槽位射线：起点与世界系单位方向。"""
    az, el = np.empty(model.slots_per_frame), np.empty(model.slots_per_frame)
    mid360_pattern(frame_seq, az, el)
    d = directions_flu(az, el) @ np.asarray(R, np.float64).T
    return np.broadcast_to(np.asarray(p0, np.float64), d.shape).copy(), d


def _expected_void(q, ref, o: np.ndarray, d: np.ndarray, hard_max: float) -> np.ndarray:
    """M25 §6.8.6 的独立实现：静态最近交点为拉伸、洞缘或覆盖外三角形；或任意距离无交点、仰角 < 0 且起点在覆盖多边形内。"""
    t, prim, _n = q._cast(o, d, np.full(len(o), hard_max))
    hit = np.isfinite(t)
    bad = ((ref["q8"] & (BIT_NODATA | BIT_STRETCHED)) > 0) | ref["he"]
    v = np.zeros(len(o), bool)
    v[hit] = bad[prim[hit].astype(np.int64)]
    md = np.flatnonzero(~hit & (d[:, 2] < 0))
    t2, _p, _n = q._cast(o[md], d[md], np.full(len(md), 1.0e6))
    nohit = md[~np.isfinite(t2)]
    v[nohit] = _points_in_rings(o[nohit, :2], [ref["ring"]])
    return v


def _scan(q, sq_, p0: np.ndarray, frame_seq: int = 3, lm=None):
    lm = lm or load_lidar_model()
    li = MeshLidar(q, lm, None, quality=sq_)
    sc = li.scan(frame_seq, 0, [(R_INV, np.asarray(p0, np.float64))] * lm.pose_nodes, tier="clean")
    o, d = _rays(lm, frame_seq, R_INV, p0)
    return sc, o, d, lm


def _occ(d: Path, P: np.ndarray) -> np.ndarray:
    ix = d / "geometry/voxel/occ_1m/index.json"
    doc = json.loads(ix.read_text(encoding="utf-8"))
    vox = np.floor((np.asarray(P, np.float64) - np.asarray(doc["origin_m"])) / float(doc["voxel_m"])).astype(np.int64)
    return read_states(ix, vox)


# ---------------------------------------------------------------- 装载


def test_sensor_quality_loads_the_world_quality_streams(sq, ref) -> None:
    assert np.array_equal(sq.tri_q, ref["q8"]) and len(sq.tri_q) == len(ref["F"])
    assert np.array_equal(sq.hole_edge, ref["he"])                              # 洞缘：与全网格边界边的独立求法逐三角形相同
    assert 8 <= int(ref["he"].sum()) <= 16                                       # 20 m 洞、10 m 网格：每条洞边 2 段，各属一个三角形
    strip = (ref["q8"] & BIT_STRETCHED) > 0
    islet = (ref["q8"] & BIT_NODATA) > 0
    assert strip.sum() == 2 and islet.sum() == 2                                 # 条带两个三角形拉伸，小岛两个三角形覆盖外
    cam = sq.cam_tri_bits
    assert np.array_equal((cam & CAM_STRETCHED) > 0, strip) and np.array_equal((cam & CAM_OUTSIDE_COVERAGE) > 0, islet)
    assert np.array_equal((cam & CAM_HOLE_EDGE) > 0, ref["he"]) and np.array_equal((cam & CAM_WATER) > 0, (ref["q8"] & BIT_WATER) > 0)
    assert np.array_equal(sq.lidar_void_tri, strip | islet | ref["he"])
    # 覆盖判定：洞在覆盖内（fill_holes），缺角与小岛在覆盖外；与多边形逐点偶奇一致
    rng = np.random.default_rng(5)
    P = rng.uniform(-160.0, 160.0, (20_000, 2))
    assert np.array_equal(sq.in_coverage(P), _points_in_rings(P, [ref["ring"]]))
    T = ref["T"]
    assert sq.in_coverage(np.array([_w(T, -50.0, -50.0)[:2], _w(T, 106.0, 106.0)[:2]])).tolist() == [True, False]


def test_world_without_quality_layers_loads_none(tmp_path: Path) -> None:
    (tmp_path / "world.json").write_text(json.dumps({"contentVersion": "abc", "layers": []}))
    assert SensorQuality.load(tmp_path) is None
    assert SensorQuality.load(tmp_path / "missing") is None


def test_hole_edge_needs_hole_bits() -> None:
    V = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float64)
    assert not hole_edge_triangles(V, np.array([[0, 1, 2]]), np.zeros((4, 4), np.uint8), (0.0, 0.0), 1.0).any()
    Q = np.zeros((4, 4), np.uint8)
    Q[0, 0] = BIT_HOLE
    assert hole_edge_triangles(V, np.array([[0, 1, 2]]), Q, (0.0, 0.0), 1.0).all()          # 孤立三角形的三条边都是边界边


# ---------------------------------------------------------------- LiDAR void


def test_lidar_void_through_the_hole_and_on_hole_edges(world, q, sq, ref) -> None:
    d, T, _c = world
    p0 = _w(T, -50.0, -50.0, 8.0)                                                 # 洞中心上方 8 m
    sc, o, dr, lm = _scan(q, sq, p0)
    void = (sc.gt_flags & VOID) > 0
    assert np.array_equal(void, _expected_void(q, ref, o, dr, lm.hard_max_m))
    t, prim, _n = q._cast(o, dr, np.full(len(o), lm.hard_max_m))
    hit = np.isfinite(t)
    t_far, _p, _n = q._cast(o, dr, np.full(len(o), 1.0e6))
    through = ~np.isfinite(t_far) & (dr[:, 2] < 0)
    assert through.sum() > 1000 and np.all(void[through])                        # 穿洞：任意距离无交点、下视、起点在覆盖内
    beyond = ~hit & np.isfinite(t_far) & (dr[:, 2] < 0)
    assert beyond.sum() > 0 and not void[beyond].any()                           # 量程外的有效地面不置位
    assert np.all(sc.gt_inst[through] == INST_NONE)
    edge_hit = hit & ref["he"][np.where(hit, prim, 0).astype(np.int64)]
    assert edge_hit.sum() > 0 and np.all(void[edge_hit])
    assert np.all(sc.gt_inst[edge_hit & (sc.gt_inst != INST_NONE)] == 0)         # 洞缘命中按回波模型输出、静态实例 0
    ok_ground = hit & ~edge_hit
    assert ok_ground.sum() > 1000 and not void[ok_ground].any()                  # 完整地面不置位
    assert not void[dr[:, 2] >= 0].any()                                          # 上视未命中不置位
    # 联动：穿洞射线在地面高度的落点（离洞边 ≥ 1 m）处，occupancy 为 unknown（100%）
    s = (0.25 - o[through, 2]) / dr[through, 2]
    P = o[through] + s[:, None] * dr[through]
    e0, n0, e1, n1 = ref["hole_box"]
    inner = (P[:, 0] > e0 + 1) & (P[:, 0] < e1 - 1) & (P[:, 1] > n0 + 1) & (P[:, 1] < n1 - 1)
    assert inner.sum() > 500 and np.all(_occ(d, P[inner]) == UNKNOWN)


def test_lidar_void_on_stretched_strip_keeps_the_observation(world, q, sq, ref) -> None:
    _d, T, _c = world
    p0 = _w(T, -90.0, 62.0, 6.0)                                                   # 拉伸条带中部上方 3 m
    sc, o, dr, lm = _scan(q, sq, p0)
    base, *_ = _scan(q, None, p0)
    void = (sc.gt_flags & VOID) > 0
    assert np.array_equal(void, _expected_void(q, ref, o, dr, lm.hard_max_m))
    t, prim, _n = q._cast(o, dr, np.full(len(o), lm.hard_max_m))
    on_strip = np.isfinite(t) & ((ref["q8"][np.where(np.isfinite(t), prim, 0).astype(np.int64)] & BIT_STRETCHED) > 0)
    assert on_strip.sum() > 200 and np.all(void[on_strip])
    assert np.all(sc.gt_inst[on_strip & (sc.gt_inst != INST_NONE)] == 0)
    # 观测帧按回波模型输出，与无质量信息时逐字节相同；真值只在 bit6 上不同
    assert sc.points.tobytes() == base.points.tobytes() and np.array_equal(sc.gt_inst, base.gt_inst)
    assert np.array_equal(sc.gt_flags & ~np.uint8(VOID), base.gt_flags & ~np.uint8(VOID))


def test_lidar_void_outside_coverage_and_occupancy_unknown(world, q, sq, ref) -> None:
    d, T, _c = world
    p0 = _w(T, 106.0, 106.0, 3.0)                                                  # 覆盖外小岛中心上方 3 m
    sc, o, dr, lm = _scan(q, sq, p0)
    void = (sc.gt_flags & VOID) > 0
    assert np.array_equal(void, _expected_void(q, ref, o, dr, lm.hard_max_m))
    t, prim, _n = q._cast(o, dr, np.full(len(o), lm.hard_max_m))
    hit = np.isfinite(t)
    on_islet = hit & ((ref["q8"][np.where(hit, prim, 0).astype(np.int64)] & BIT_NODATA) > 0)
    assert on_islet.sum() > 200 and np.all(void[on_islet])                       # §6.8.6 第 4 行：命中覆盖外的面
    P = o[on_islet] + (t[on_islet] - 0.05)[:, None] * dr[on_islet]
    assert np.all(_occ(d, P) == UNKNOWN)                                          # 覆盖外体素（含表面）unknown，与 void 一致
    miss = ~hit & (dr[:, 2] < 0)
    assert miss.sum() > 1000 and not void[miss].any()                            # §6.8.6 第 2 行：起点在覆盖外的未命中不置位


def test_lidar_range_miss_over_valid_ground_is_not_void(world, q, sq, ref) -> None:
    """量程外的有效表面不是无数据区：下视射线在量程内未命中、但在更远处与地面相交时不置 `void`。"""
    from dataclasses import replace

    _d, T, _c = world
    lm = replace(load_lidar_model(), hard_max_m=5.0)                              # 离地 8 m：下视射线的最短斜距 10.2 m 超出量程
    p0 = _w(T, -5.0, -75.0, 8.0)                                                  # 有效地面上方（离洞、盒子、水面、条带 ≥ 25 m）
    sc, o, dr, _lm = _scan(q, sq, p0, lm=lm)
    assert np.array_equal((sc.gt_flags & VOID) > 0, _expected_void(q, ref, o, dr, lm.hard_max_m))
    steep = dr[:, 2] < -np.sin(np.radians(20.0))                                  # 落点在 22 m 内（有效地面）
    assert steep.sum() > 2000 and (sc.gt_inst[steep] == INST_NONE).all() and not ((sc.gt_flags[steep] & VOID) > 0).any()
    legacy, *_ = _scan(q, None, p0, lm=lm)                                        # 无质量信息：沿用"量程内未命中且下视"
    assert ((legacy.gt_flags[steep] & VOID) > 0).all()


# ---------------------------------------------------------------- 相机 invalid_mask


def _render(q, cm: CameraModel, R_ws: np.ndarray, pos: np.ndarray) -> tuple[RenderOut, np.ndarray, np.ndarray]:
    """CPU 参照渲染：像素中心视线与求交网格的首个交点（平面深度、静态实例 0），返回 (RenderOut, prim (H, W), 视线仰角 (H, W))。"""
    W, H = cm.width, cm.height
    x, y = np.meshgrid((np.arange(W) + 0.5 - cm.cx) / cm.fx, (np.arange(H) + 0.5 - cm.cy) / cm.fy)
    dv = np.stack([x, y, np.ones_like(x)], -1).reshape(-1, 3) @ (np.asarray(R_ws) @ R_FLU_OPT).T
    nrm = np.linalg.norm(dv, axis=1)
    dv /= nrm[:, None]
    t, prim, _n = q._cast(np.broadcast_to(np.asarray(pos, np.float64), dv.shape).copy(), dv, np.full(len(dv), 5000.0))
    depth = np.where(np.isfinite(t), t / nrm, 0.0).astype(np.float32).reshape(H, W)
    out = RenderOut(np.zeros((H, W, 3), np.uint8), depth, np.zeros((H, W), np.uint16), np.where(depth > 0, 0, 255).astype(np.uint8),
                    1.0, 1.0, 1)
    return out, np.where(np.isfinite(t), prim, 0).astype(np.int64).reshape(H, W), dv[:, 2].reshape(H, W)


def _expected_mask(ref, out: RenderOut, prim: np.ndarray, el: np.ndarray) -> np.ndarray:
    q8 = ref["q8"][prim]
    geo = out.depth_m > 0
    m = ((~geo) & (el < 0)).astype(np.uint8)
    static = geo & (out.instance == 0)
    m[static & ((q8 & BIT_STRETCHED) > 0)] |= CAM_STRETCHED
    m[static & ((q8 & BIT_NODATA) > 0)] |= CAM_OUTSIDE_COVERAGE
    m[static & ref["he"][prim]] |= CAM_HOLE_EDGE
    m[static & ((q8 & BIT_WATER) > 0)] |= CAM_WATER
    return m


def _frame(out: RenderOut, cm: CameraModel, R: np.ndarray, pos: np.ndarray, quality, actors=None) -> tuple[dict, np.ndarray]:
    import zstandard

    from awr.sensorsvc.frame import FrameContext, camera_frame
    from awr.sensorsvc.poses import Mount

    rec, blocks = camera_frame(FrameContext("run-q", "sess-q", "v", "cpu", "drv", encoder="pillow"), uav="u1", sensor_id="cam0", seq=1,
                               t_mid_ns=0, exposure_ns=1_000_000, readout_ns=0, cm=cm, mount=Mount(), gimbal_reading=(0.0, 0.0, 0.0),
                               gimbal_true=(0.0, 0.0, 0.0), pos_enu_m=pos, R_ws=R, out=out, outputs={"rgb"}, actors=actors, quality=quality,
                               rgb=b"\xff\xd8stub")
    ref_ = rec["ground_truth"]["invalid_mask"]["ref"]
    mask = np.frombuffer(zstandard.ZstdDecompressor().decompress(blocks[ref_]), np.uint8).reshape(cm.height, cm.width)
    return rec, mask


@pytest.mark.parametrize(("name", "xz", "alt", "bits"), [
    ("hole", (-50.0, -50.0), 25.0, CAM_HOLE_EDGE | 1),
    ("strip-water", (-95.0, 80.0), 60.0, CAM_STRETCHED | CAM_WATER),
    ("outside", (106.0, 106.0), 30.0, CAM_OUTSIDE_COVERAGE | 1),
])
def test_camera_invalid_mask_bits_match_the_reference(world, q, sq, ref, name, xz, alt, bits) -> None:
    _d, T, _c = world
    cm = CameraModel.from_hfov(160, 120, 84.0)
    pos = _w(T, xz[0], xz[1], alt)
    R = look_at_R(pos, pos - np.array([0.0, 0.0, 1.0]))                           # 正下视
    out, prim, el = _render(q, cm, R, pos)
    rec, mask = _frame(out, cm, R, pos, sq)
    exp = _expected_mask(ref, out, prim, el)
    assert np.array_equal(mask, exp), (name, np.argwhere(mask != exp)[:5])
    for b in (1, CAM_STRETCHED, CAM_OUTSIDE_COVERAGE, CAM_HOLE_EDGE, CAM_WATER):
        assert bool((mask & b).any()) == bool(bits & b), (name, b)                 # 每个视角恰好出现预期的位
    assert rec["ground_truth"]["invalid_mask"]["bits"] == ["void_below_horizon", "stretched", "outside_coverage", "hole_edge", "water"]
    _rec0, legacy = _frame(out, cm, R, pos, None)
    assert np.array_equal(legacy, exp & 1)                                        # 无质量信息：只有第 0 位


def test_camera_actor_pixels_unflagged_and_visibility_excludes_masked(world, q, sq, ref) -> None:
    _d, T, _c = world
    cm = CameraModel.from_hfov(160, 120, 84.0)
    pos = _w(T, -90.0, 62.0, 30.0)                                                 # 拉伸条带上方
    R = look_at_R(pos, pos - np.array([0.0, 0.0, 1.0]))
    out, prim, _el = _render(q, cm, R, pos)
    strip = (out.depth_m > 0) & ((ref["q8"][prim] & BIT_STRETCHED) > 0)
    rows = np.flatnonzero(strip.any(1))
    r0, r1 = int(rows[len(rows) // 2]) - 3, int(rows[len(rows) // 2]) + 3
    unocc = np.zeros_like(out.instance)
    unocc[r0:r1, 70:90] = 1                                                      # 演员的无遮挡掩膜横跨条带
    inst = unocc.copy()
    inst[r0:r1, 80:90] = 0                                                       # 右半被条带（静态、拉伸）遮挡
    out = RenderOut(out.rgb8, out.depth_m, inst, np.where(inst > 0, 32, out.semantic).astype(np.uint8), 1.0, 1.0, 1, instance_unocc=unocc,
                    actors_drawn=1)
    rec, mask = _frame(out, cm, R, pos, sq, actors=[{"idx": 0, "id": "person_A", "class": "person"}])
    assert np.all(mask[inst > 0] == 0)                                            # 演员像素不置位
    occl = (unocc > 0) & (inst == 0)
    assert np.all(mask[occl & strip] & CAM_STRETCHED) and (occl & strip).sum() > 0
    (o,) = rec["ground_truth"]["objects"]
    assert o["px_visible"] == int((inst > 0).sum())
    assert o["px_unoccluded"] == int(((unocc > 0) & (mask == 0)).sum()) < int((unocc > 0).sum())   # 被标记像素不计入


# ---------------------------------------------------------------- 端到端：run_capture 写出的真值文件


class _CpuCamera:
    """GL 光栅的 CPU 替身：按求交网格渲染真实深度（像素中心视线），供端到端核对 invalid_mask。"""
    q = None

    def __init__(self, scene) -> None:
        self.info = {"GL_RENDERER": "cpu-ref", "GL_VERSION": "4.5.0 cpu 1.0"}

    def set_actors(self, actors) -> None:
        pass

    def set_sun(self, az_el) -> None:
        pass

    def render(self, cm, R, pos, *, linear: bool = False, fx=None, rs_nodes=None, actors=None) -> RenderOut:
        from awr.sensorsvc.camera.effects import apply_fx_reference

        out, _prim, _el = _render(_CpuCamera.q, cm, R, pos)
        lin = np.full((cm.height, cm.width, 3), 0.1, np.float32)
        rgb, d_obs = apply_fx_reference(lin, out.depth_m, cm, fx)
        return RenderOut(rgb, out.depth_m, out.instance, out.semantic, 1.0, 1.0, 1, depth_obs=d_obs,
                         meter_sub=lin[::8, ::8] if fx.meter else None)

    def release(self) -> None:
        pass


def test_capture_truth_sidecar_carries_void_and_invalid_mask(world, q, ref, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D3-AC-04 端到端：`run_capture` 在带洞世界上悬停，真值文件中的 LiDAR `void` 与相机 `invalid_mask` 与独立参照逐槽位、逐像素相同。"""
    pytest.importorskip("mcap_protobuf")
    import awr.sensorsvc.camera.raster as raster
    import awr.sensorsvc.camera.scene as scene
    from awr.bench.recorder.lidar import unpack_lidar_gt
    from awr.bench.recorder.read import TruthStore
    from awr.bench.recorder.truth import unpack_camera_gt
    from awr.bench.runs_store import truth_paths
    from awr.sensorsvc import capture as cap
    from awr.sensorsvc.lidar.mesh import _quat_R

    d, T, _c = world
    _CpuCamera.q = q
    monkeypatch.setattr(raster, "RasterCamera", _CpuCamera)
    monkeypatch.setattr(scene, "load_appearance", lambda w: None)
    monkeypatch.setenv("AWR_D3_ENABLED", "1")
    c = _w(T, -50.0, -50.0)
    hover = lambda u: {"kind": "waypoints", "points": [[0, float(c[0]), float(c[1]), u, 0], [1, float(c[0]), float(c[1]), u, 0]]}  # noqa: E731
    spec = {"run_id": "cap-quality", "event_id": "capture", "world_id": d.name, "duration_s": 0.2, "noise_profile": "clean",
            "fleet": [{"uav": "uav-01", "path": hover(8), "cameras": [], "lidars": [{"name": "lidar0"}]},     # 洞中心上方：LiDAR 穿洞
                      {"uav": "uav-02", "path": hover(25), "cameras": [{"name": "cam0", "size": [96, 72], "hfov_deg": 84, "fps": "10",
                                                                        "outputs": ["rgb"], "gimbal": {"az_deg": 0, "el_deg": -90}}]}]}
    m = cap.run_capture(spec, tmp_path, worlds_dir=d.parent, log=lambda s: None)
    assert m["frames"] == 2 + 2
    run = tmp_path / "dataset/capture/runs/cap-quality"
    ts = TruthStore(*truth_paths(run))
    try:
        lm = load_lidar_model()
        gh, inst, flags = unpack_lidar_gt(ts.get("uav-01", "lidar0", 1)[1])
        nd = gh["T_world_sensor_nodes"]
        assert all(np.allclose(n["pos_enu_m"], nd[0]["pos_enu_m"]) for n in nd)              # 悬停：节点位姿相同
        p0 = np.asarray(nd[0]["pos_enu_m"], np.float64)
        Rl = _quat_R(np.asarray([nd[0]["q_ws_xyzw"]], np.float64))[0]
        o, dr = _rays(lm, 1, Rl, p0)
        void = (flags & VOID) > 0
        assert void.sum() > 1000 and np.array_equal(void, _expected_void(q, ref, o, dr, lm.hard_max_m))
        assert np.all(inst[void & ~np.isin(inst, [0])] == INST_NONE)                         # void 槽位：穿洞无回波或静态命中
        rec, blocks = unpack_camera_gt(ts.get("uav-02", "cam0", 1)[1])
        import zstandard

        im = rec["ground_truth"]["invalid_mask"]
        mask = np.frombuffer(zstandard.ZstdDecompressor().decompress(blocks[im["ref"]]), np.uint8).reshape(72, 96)
        tw = rec["ground_truth"]["T_world_sensor"]
        Rwo = _quat_R(np.asarray([tw["q_ws_xyzw"]], np.float64))[0]                          # ENU←RDF
        cm = CameraModel.from_hfov(96, 72, 84.0)
        out, prim, el = _render(q, cm, Rwo @ R_FLU_OPT.T, np.asarray(tw["pos_enu_m"], np.float64))
        exp = _expected_mask(ref, out, prim, el)
        assert np.array_equal(mask, exp)
        assert (mask & CAM_HOLE_EDGE).any() and (mask & 1).any()                            # 洞缘与穿洞（无几何下视）
    finally:
        ts.close()
