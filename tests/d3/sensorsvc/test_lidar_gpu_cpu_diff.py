"""网格 LiDAR 多机并发与 GPU/CPU 逐射线对拍（D3-AC-12；D3-AC-47 的逐射线距离差分布与逐点/单一位姿 ATE 比值）：
`tools/bench/d3/lidar_concurrency.py`（S16）。

阈值逐条对照 docs/05 §11.2 D3-AC-12：与 Open3D CPU 参考（均为瓦片局部原点 float32 求交）逐射线距离差 p99 ≤ 1 mm、最大 ≤ 1 cm，
命中判定不一致 ≤ 1e-4；吞吐 100 架 p95 ≤ 30 ms、400 架 p95 ≤ 80 ms 只在独占卡（ADR-195 A 级）上计入，共享卡数字只作开发参考，
因此本文件只核对吞吐测量的结构、独占级别标注与判定逻辑，A 级时才断言阈值。

- 主机端（不需要 warp）：统计与判定（计入规则）、逐射线累加器、热点与机队航迹、合成 32 演员磁带、CPU 参考逐射线记录、ATE 航迹与逐点
  去畸变几何、逐点与单一位姿两组 SLAM 算例（bag 合同、配置、真值、畸变量 A/B）与 FAST-LIVO2 结果汇总的比值。
- Warp（有 CUDA 用 GPU，否则 Warp CPU 设备）：构造场景上按吞吐场景抽样的多帧（32 个演员、天气、三档噪声）汇总对拍分布；吞吐循环
  结构（小机队）。
- 真实世界（CUDA + `worlds/oryahovo-crop1`）：96 帧对拍分布、100 架与 400 架吞吐（共享卡参考）；
  `AWR_LIDAR_CONCURRENCY_REPORT=<path>` 时写出 JSON（验收记录引用）。

构造场景与 32 演员见 `test_mesh_lidar_gpu.py`（同一几何：3 × 3 个 80 m 瓦片、世界偏移 (12 km, −8 km, 150 m)）。
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("open3d")
pytest.importorskip("zstandard")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_mesh_lidar_gpu import _world_dir, build_scene

from awr.sensorsvc.lidar.mesh import MeshLidar
from awr.sensorsvc.lidar.model import load_lidar_model

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("lidar_concurrency", REPO / "tools/bench/d3/lidar_concurrency.py")
lc = importlib.util.module_from_spec(_spec)
sys.modules["lidar_concurrency"] = lc
_spec.loader.exec_module(lc)

P99_M, MAX_M, HIT_MISMATCH = 1e-3, 1e-2, 1e-4                    # D3-AC-12
_REPORT: dict = {}


@pytest.fixture(scope="module")
def q():
    return build_scene()


@pytest.fixture(scope="module")
def centers(q):
    return lc.hotspots(q, 4, min_sep_m=50.0)


@pytest.fixture(scope="module")
def scene(q, centers, tmp_path_factory: pytest.TempPathFactory):
    tape = lc.write_actor_tape(tmp_path_factory.mktemp("lc-tape") / "tape.bin", q, centers, n_ticks=200)
    return lc.Scene(q, tape, centers, visibility_m=300.0, rain_mmh=10.0)


# ---------------------------------------------------------------- 统计、判定与累加器


def _thr(levels: list[str], p95: float) -> dict:
    return {"fleets": {"100": {"levels": levels, "tick_p95_ms_median": p95}, "400": {"levels": levels, "tick_p95_ms_median": 3 * p95}}}


def _diff(p99: float, mx: float, rate: float) -> dict:
    return {"d_m": {"p50": p99 / 10, "p99": p99, "max": mx}, "hit_mismatch_rate": rate}


def test_judge_counting_rules() -> None:
    """吞吐条款：全部重复为 A 级才计入；B 级待用户确认、shared 与 unknown 不计入；几何一致性条款不受独占级别约束；缺测为 None。"""
    ok = lc.judge({"throughput": _thr(["A", "A", "A"], 20.0), "diff": _diff(1e-6, 1e-5, 0.0)})
    assert ok["counted_pass"] and ok["seal_ready"]
    assert [i["counted"] for i in ok["items"] if i["class"] != "拟议"] == [True] * 5
    assert all(not i["counted"] for i in ok["items"] if i["class"] == "拟议")
    sh = lc.judge({"throughput": _thr(["A", "shared", "A"], 20.0), "diff": _diff(1e-6, 1e-5, 0.0)})
    t100 = sh["items"][0]
    assert t100["level"] == "shared" and not t100["counted"] and t100["pass"]
    assert sh["counted_pass"] and not sh["seal_ready"]                  # 不计入的条款不影响退出码，但不能封板
    b = lc.judge({"throughput": _thr(["B", "A", "B"], 20.0), "diff": _diff(1e-6, 1e-5, 0.0)})
    assert b["items"][0]["level"] == "B" and not b["items"][0]["counted"]
    slow = lc.judge({"throughput": _thr(["A"] * 3, 31.0), "diff": _diff(1e-6, 1e-5, 0.0)})
    assert not slow["items"][0]["pass"] and not slow["counted_pass"]
    for d in (_diff(1.1e-3, 1e-3, 0.0), _diff(1e-4, 1.1e-2, 0.0), _diff(1e-4, 1e-3, 1.1e-4)):
        assert not lc.judge({"throughput": _thr(["A"] * 3, 20.0), "diff": d})["counted_pass"]
    # 原文口径的最大距离差超限但全部来自轮廓边判定歧义：原文条款不通过（不得自行放宽），拟议口径条款通过并注明
    edge = {**_diff(1e-6, 5.2e-2, 0.0), "d_same_surface_m": {"p99": 1e-6, "max": 1.5e-5}, "hit_or_edge_mismatch_rate": 5e-7,
            "edge": {"rays": 1}}
    je = lc.judge({"throughput": _thr(["A"] * 3, 20.0), "diff": edge})
    by = {i["id"]: i for i in je["items"]}
    assert not by["AC12.ray_d_max_m"]["pass"] and "轮廓边" in by["AC12.ray_d_max_m"]["note"] and not je["counted_pass"]
    assert by["AC12.ray_d_max_m.same_surface"]["pass"] and by["AC12.hit_or_edge_mismatch_rate"]["pass"]
    assert not by["AC12.ray_d_max_m.same_surface"]["counted"]
    err = _thr(["A"] * 3, 20.0)
    err["fleets"]["400"] = {"repeats": [], "error": "RuntimeError: out of memory", "tick_p95_ms_median": None, "levels": []}
    i400 = lc.judge({"throughput": err, "diff": _diff(1e-6, 1e-5, 0.0)})["items"][1]
    assert i400["pass"] is False and not i400["counted"] and i400["note"].startswith("未测完")
    none = lc.judge({})
    assert not none["counted_pass"] and all(i["value"] is None for i in none["items"])
    info = lc.judge({"diff": _diff(1e-6, 2e-5, 0.0), "ate": {"slam": {"clean": {"lio": {"ate_ratio_single_over_per_point": 12.0}}}}})["info"]
    assert info["AC47.ray_diff_distribution"]["max"] == 2e-5 and info["AC47.ate_ratio"] == {"clean/lio": 12.0}


def test_ray_diff_accumulator() -> None:
    """命中判定（CPU inf / GPU −1 为未命中）、双方命中时的距离差分布（静态与演员分列）、实例不一致与有效槽位差。"""
    acc = lc.RayDiff()
    tc = np.array([1.0, 2.0, np.inf, 4.0, 5.0, np.inf])
    tg = np.array([1.0 + 1e-6, 2.0 - 3e-6, -1.0, -1.0, 5.0 + 2e-3, 7.0])
    ic = np.array([0, 3, 0, 0, 0, 0])
    ig = np.array([0, 3, 0, 0, 2, 0])
    acc.add(tc, ic, tg, ig, slots_cpu=[0, 1, 4], slots_gpu=[0, 1, 5])
    s = acc.summary()
    assert s["rays"] == 6 and s["hit_cpu"] == 4 and s["hit_gpu"] == 4 and s["both_hit"] == 3
    assert s["hit_mismatch"] == 2 and s["hit_mismatch_rate"] == pytest.approx(2 / 6) and s["inst_mismatch"] == 1
    assert s["d_static_m"]["n"] == 1 and s["d_actor_m"]["n"] == 2
    assert s["d_m"]["max"] == pytest.approx(2e-3) and s["slot_sym_diff"] == {"sum": 2, "max": 2}
    assert s["edge"]["rays"] == 0 and s["d_same_surface_m"]["max"] == pytest.approx(2e-3)
    # 轮廓边歧义射线：进入原文口径分布，不进入同一表面分布；歧义计入拟议的不一致率
    acc2 = lc.RayDiff()
    tc2, tg2 = np.array([10.0, 20.0, 30.0, np.inf]), np.array([10.0 + 1e-9, 19.948, 30.0, -1.0])
    pc, pg = np.array([5, 7, 9, -1]), np.array([5, 8, 9, -1])
    acc2.add(tc2, np.zeros(4), tg2, np.zeros(4), prim_cpu=pc, prim_gpu=pg, edge=np.array([False, True, False, False]),
             edge_gap=np.array([np.nan, -1e-5, np.nan, np.nan]))
    s2 = acc2.summary()
    assert s2["d_m"]["max"] == pytest.approx(0.052) and s2["d_same_surface_m"]["max"] == pytest.approx(1e-9)
    assert s2["edge"]["rays"] == 1 and s2["edge"]["gap_abs_max_m"] == pytest.approx(1e-5) and s2["prim_mismatch"] == 1
    assert s2["hit_or_edge_mismatch_rate"] == pytest.approx(1 / 4) and s2["worst_frames"][0]["edge_ambiguous"] == 1
    assert lc.dist([])["n"] == 0 and lc.dist([1.0, 3.0])["p50"] == 2.0


# ---------------------------------------------------------------- 场景：热点、机队与磁带


def test_hotspots_fleet_and_tape(q, centers, scene) -> None:
    """热点确定、互相分开且落在世界内；机队 8 m/s 环绕、航高高于最高表面 4 m 以上、自旋机 90°/s；磁带 32 个演员绕热点、贴地、有可见性阶跃。"""
    lo, hi = lc.world_extent(q)
    assert np.array_equal(centers, lc.hotspots(q, 4, min_sep_m=50.0))
    assert ((centers >= lo[:2]) & (centers <= hi[:2])).all()
    assert min(np.linalg.norm(a - b) for i, a in enumerate(centers) for b in centers[i + 1:]) >= 50.0
    f = lc.Fleet(q, centers, 16)
    R, p = f.nodes(2_000_000_000)
    assert R.shape == (16, 11, 3, 3) and p.shape == (16, 11, 3)
    assert np.abs(np.einsum("nkji,nkjl->nkil", R, R) - np.eye(3)).max() < 1e-12
    v = np.linalg.norm(np.diff(p[..., :2], axis=1), axis=-1) / 0.01
    assert np.abs(v - 8.0).max() < 0.05
    ring_top = lc.top_z(q, p[:, 0, :2])
    assert (p[:, 0, 2] >= ring_top + 4.0 - 1e-9).all()
    fwd = R[..., :, 0]                                                    # 雷达 x 轴（倒装后仍沿航向）
    yaw = np.unwrap(np.arctan2(fwd[..., 1], fwd[..., 0]), axis=1)
    rate = np.degrees(yaw[:, -1] - yaw[:, 0]) / 0.1
    spin = np.arange(16) % 4 == 3
    assert (np.abs(rate[spin]) > 60.0).all() and (np.abs(rate[~spin]) < 50.0).all()
    reqs = f.reqs(5, 2_500_000_000, tier="harsh", beta_905=0.01, uavs=[2, 7])
    assert [r.uav_no for r in reqs] == [2, 7] and all(r.frame_seq == 5 and len(r.nodes) == 11 for r in reqs)
    tape = scene.tape
    assert len(scene.actors) == 32 and tape.n_ticks == 200
    st = tape.actor_states(10_000_000_000)
    d = np.min(np.linalg.norm(st.pos_enu_m[:, None, :2] - centers[None], axis=-1), axis=1)
    assert d.max() < 20.0
    assert np.abs(st.pos_enu_m[:, 2] - lc.ground_z(q, st.pos_enu_m[:, :2])).max() < 0.02
    s0, s1 = tape.actor_states(1_000_000_000).state, tape.actor_states(19_000_000_000).state
    assert s0[3] == 0 and st.state[3] == 1 and s1[5] == 0 and st.state[5] == 1
    clock = scene.clock()
    assert clock(2_000_000_000) is clock(2_000_000_000) and clock.calls == 1


# ---------------------------------------------------------------- 轮廓边判定歧义（GPU/CPU 命中不同静态三角形）


class _Tile:
    def __init__(self, V, F, origin, t0):
        self.V, self.F, self.origin, self.t0, self.nt = np.asarray(V, np.float32), np.asarray(F, np.uint32), np.asarray(origin, np.float64), t0, len(F)
        self.lo, self.hi = self.V.min(0) + self.origin, self.V.max(0) + self.origin


def _step_q():
    """两层平行正方形（各 2 个三角形）：近层 x ∈ [300, 301]、z = 0（全局三角形 0、1），远层 x ∈ [299, 302]、z = −0.052（2、3）；瓦片原点
    (0, 0, 0)，瓦片局部坐标为 300 m 量级（判定带宽约 1.1 mm）。射线自上方竖直向下：近层边缘 x = 301 处有 5.2 cm 的深度台阶（与 dmax
    实测的屋檐轮廓边同型）。"""
    near = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]]
    far = [[-1, -1, -0.052], [2, -1, -0.052], [2, 2, -0.052], [-1, 2, -0.052]]
    V = np.array([*near, *far], np.float64) + np.array([300.0, 0.0, 0.0])
    F = [[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]]
    return type("Q", (), {"tiles": [_Tile(V, F, (0.0, 0.0, 0.0), 0)]})()


def test_edge_margin_and_ambiguity() -> None:
    """平面内带符号边界距离：内正外负；近层边缘 1e-5 m 以内一侧命中近层、另一侧命中远层 → 轮廓边歧义；边缘内 0.1 m 仍命中远层 → 真实
    求交差异（不归入歧义）；同一三角形或距离差很小的不同三角形不核查。"""
    q = _step_q()
    P, org = lc.tri_world(q, np.array([0, 0, 2]))
    assert np.allclose(org[0], 0.0) and np.allclose(P[0, 1], [301, 0, 0])
    o = np.array([[300.5, 0.1, 5.0], [301.0 + 1e-5, 0.4, 5.0], [300.5, 0.1, 5.0]])
    d = np.tile([0.0, 0.0, -1.0], (3, 1))
    g = lc.edge_margin(P, o, d)
    assert g[0] == pytest.approx(0.1) and g[1] == pytest.approx(-1e-5, abs=1e-7) and g[2] == pytest.approx(0.4 / math.sqrt(2))
    # 射线 0：边缘内 0.1 m，CPU 命中远层（三角形 2）而 GPU 命中近层（0）→ 真实差异；射线 1：边缘外 1e-5 m，GPU 命中近层（容差内）
    # 而 CPU 命中远层 → 歧义；射线 2：同一三角形；射线 3：不同三角形但距离差 < EDGE_CHECK_M（共边）
    o4 = np.vstack([o[:2], [[300.5, 0.1, 5.0], [300.5, 0.5, 5.0]]])
    d4 = np.tile([0.0, 0.0, -1.0], (4, 1))
    tc = np.array([5.052, 5.052, 5.0, 5.0])
    tg = np.array([5.0, 5.0, 5.0, 5.0 + 1e-9])
    pc, pg = np.array([2, 2, 0, 0]), np.array([0, 0, 0, 1])
    amb, gap = lc.edge_ambiguous(q, o4, d4, tc, pc, tg, pg)
    assert amb.tolist() == [False, True, False, False]
    assert np.isnan(gap[2]) and np.isnan(gap[3]) and gap[1] == pytest.approx(-1e-5, abs=1e-7)
    # 带宽随瓦片局部坐标量级：1e-3 m 在 300 m 量级（带宽约 1.1 mm）内为歧义，0.01 m 不是
    for off, want in ((1e-3, True), (1e-2, False)):
        a, _ = lc.edge_ambiguous(q, np.array([[301.0 + off, 0.4, 5.0]]), d4[:1], tc[:1], pc[:1], tg[:1], pg[:1])
        assert bool(a[0]) is want
    # 未命中与演员不核查
    a, _ = lc.edge_ambiguous(q, o4[:2], d4[:2], np.array([np.inf, 5.052]), np.array([-1, -2]), np.array([5.0, 5.0]), np.array([0, 0]))
    assert not a.any()


def test_loop_track_centre_stays_on_mesh() -> None:
    """环心只在网格覆盖内选取：瓦片 AABB 的并为 [−1000, 1000]²，但只有 x < 0 的半边有网格（D3 crop 世界即有无网格的角区）；
    覆盖外未命中时旧的"环线最高表面最低"会选到覆盖外（取世界最低高度，dmax 实测每帧只有虚警点），现在环线与圆盘样本都须有交点。
    有 DTM 且存在结构丰富的候选时取其中对地航高最低者（平地上 LIO 退化，dmax 实测 realistic 逐点 ATE 1.9 m）。"""

    class HalfQ:
        def __init__(self) -> None:
            self.tiles = [type("T", (), {"lo": np.array([-1000.0, -1000.0, 0.0]), "hi": np.array([1000.0, 1000.0, 20.0])})()]
            self._zlo, self._zhi = 0.0, 20.0

        def _cast(self, o, d, tmax):
            z = 5.0 + 0.001 * np.abs(o[:, 1])                            # 有网格处表面高 5 m 起，向 y 两侧缓升
            t = np.where(o[:, 0] < 0.0, o[:, 2] - z, np.inf)
            return t, np.zeros(len(o), np.uint32), np.zeros((len(o), 3), np.float32)

    q = HalfQ()
    tr = lc.LoopTrack(q, flight_s=60.0)
    assert tr.c[0] + tr.R < 0.0, (tr.c, tr.R)
    assert tr.site["rule"] == "covered_lowest_top" and tr.site["covered"] < tr.site["candidates"]
    assert tr.alt == pytest.approx(lc.top_z(q, tr.c[None] + [[0.0, tr.R]]).max() + 8.0, abs=0.2)
    assert np.isnan(lc.top_z(q, [[10.0, 0.0]], miss=np.nan)[0]) and lc.top_z(q, [[10.0, 0.0]])[0] == 0.0

    class SiteQ(HalfQ):
        """全覆盖；x < 0 为平地（最高表面即地面 0 m），x > 0 为结构区（最高表面 3 m 起、随 |y| 缓升，DTM 为 0）。"""

        def _cast(self, o, d, tmax):
            z = np.where(o[:, 0] > 0.0, 3.0 + 0.01 * np.abs(o[:, 1]), 0.0)
            return o[:, 2] - z, np.zeros(len(o), np.uint32), np.zeros((len(o), 3), np.float32)

        def ground_dtm(self, xy):
            return np.zeros(len(xy))

    sq = SiteQ()
    ts = lc.LoopTrack(sq, flight_s=60.0)
    assert ts.site["rule"] == "structure_lowest_agl" and ts.site["structure_frac"] >= lc.LOOP_STRUCT_FRAC, ts.site
    assert ts.c[0] - (ts.R + 20.0) > 0.0 and abs(ts.c[1]) < 150.0, ts.c                     # 结构区内、|y| 最小处（对地航高最低）
    assert ts.site["alt_over_ground_m"] == pytest.approx(ts.alt, abs=1e-9)                     # DTM 为 0：对地航高即航高
    c2, _ = lc.loop_center(sq, ts.R, 8.0)
    assert np.array_equal(c2, ts.c)                                                            # 确定性


# ---------------------------------------------------------------- CPU 参考与几何


def test_cpu_rays_wrapper_is_transparent(scene) -> None:
    """逐射线记录的包装不改动参考实现：观测帧与直接扫描逐字节相同，记录的最近交点与真值一致（有回波槽位必有交点，演员实例相同）。"""
    m = load_lidar_model()
    lid = MeshLidar(scene.q, m, scene.actors)
    r = lc.Fleet(scene.q, scene.centers, 4).reqs(30, scene.t_start(30), seed=3, tier="realistic", beta_905=scene.beta, uavs=[0])[0]
    s, t, inst, rays = lc.cpu_rays(lid, r, scene.clock())
    s2 = lid.scan(r.frame_seq, r.t_start_ns, list(r.nodes), actor_states=scene.clock(), seed=3, uav_no=0, tier="realistic", beta_905=scene.beta)
    assert s.points.tobytes() == s2.points.tobytes() and s.gt_inst.tobytes() == s2.gt_inst.tobytes()
    assert "_cast" not in vars(scene.q) and "_actors" not in vars(lid)
    ret = (s.gt_inst != 65535) & (s.gt_inst != 65534)
    assert np.isfinite(t[ret]).all()
    assert np.array_equal(s.gt_inst[ret].astype(np.int64), inst[ret])
    pr = rays["prim"]
    assert rays["o"].shape == rays["d"].shape == (20000, 3) and np.allclose(np.linalg.norm(rays["d"], axis=1), 1.0)
    assert (pr[inst > 0] == -2).all() and (pr[~np.isfinite(t)] == -1).all() and (pr[np.isfinite(t) & (inst == 0)] >= 0).all()
    st = np.isfinite(t) & (inst == 0)                                    # 静态命中点落在所记录的三角形上
    P, _org = lc.tri_world(scene.q, pr[st])
    g = lc.edge_margin(P, rays["o"][st], rays["d"][st])
    assert (g >= -1e-4).all()


def test_loop_track_and_deskew_geometry(q) -> None:
    """ATE 航迹：悬停窗口速度 0、加速后 8 m/s、偏航角速度峰值约 32°/s 加环线转弯；clean 档逐点位姿扫描按槽位时刻真值位姿（`slot_poses`）
    还原的世界点落在网格上（99% ≤ 1 mm）；单一位姿扫描经同样的去畸变后偏离其真实几何（畸变量 B，中位 > 0.1 m）。"""
    tr = lc.LoopTrack(q, flight_s=12.0, hover_s=1.0, ramp_s=1.0)
    t = tr.t_base + np.arange(0, 12_000_000_000, 4_000_000, dtype=np.int64)
    _R, p = tr.pose(t)
    v = np.linalg.norm(np.diff(p, axis=0), axis=1) / 0.004
    assert v[: int(0.9 / 0.004)].max() < 1e-9 and abs(v[-100:].mean() - 8.0) < 1e-4                # 弦长与弧长之差
    d = tr.describe()
    assert 32.0 <= d["yaw_rate_max_dps"] <= 32.0 + math.degrees(8.0 / tr.R) + 1.0
    m = load_lidar_model()
    lid = MeshLidar(q, m)
    ts = tr.t_base + 60 * lc.TICK_NS
    Xw = []
    for nd in (tr.nodes(ts), tr.single(ts)):
        pt = lid.scan(60, ts, nd, tier="clean").points
        xyz = np.stack([pt["x"], pt["y"], pt["z"]], 1).astype(np.float64)
        Ri, oi = lc.slot_poses(tr.nodes(ts), (pt["offset_time"] // m.point_dt_ns).astype(np.int64), m)
        Xw.append((xyz, np.einsum("nij,nj->ni", Ri, xyz) + oi))
    assert np.percentile(q.closest_batch(Xw[0][1], 1.0), 99) <= 1e-3
    R_m, o_m = tr.single(ts)[0]                                           # 单一位姿数据的真实几何：帧中点位姿下的点
    assert np.median(np.linalg.norm(Xw[1][1] - (Xw[1][0] @ R_m.T + o_m), axis=1)) > 0.1


def test_ate_cases_bag_contract_and_distortion(q, tmp_path: Path) -> None:
    """逐点与单一位姿两组算例：bag 通过 SLAM 导出合同校验（全槽位 CustomMsg、10 Hz、IMU 200 Hz 绝对网格）、slam_eval 可装载、配置与 rig
    一致、真值为 IMU 位姿（LiDAR 位姿加 MID-360 杆臂）；两组只有 LiDAR 点不同（IMU 与真值相同）；畸变量 A、B 为分米量级（r4：8 m/s 时
    A 中位 0.47 m、B 0.29 m）。`collect_ate` 由 slam_eval 的 metrics.json 汇总比值。"""
    from slam_eval.case import load_case
    from slam_eval.traj import load_tum

    from awr.sensorsvc.slam.bag import IMU_TOPIC, LIDAR_TOPIC, Bag1Reader

    r = lc.build_ate_cases(q, tmp_path / "cases", tiers=("realistic",), flight_s=8.0, hover_s=1.0, ramp_s=1.0, backend="cpu", world="synthetic")
    assert set(r["cases"]) == {"per_point-realistic", "single_pose-realistic"} and r["frames"] == 79
    for name, c in r["cases"].items():
        b = c["bag"]
        assert b["lidar_frames"] == 79 and b["images"] == 0 and b["imu_samples"] == 1600 and c["points_per_frame_mean"] > 2000
        case = load_case(c["dir"])
        assert case.meta["pose_nodes"] == ("11" if name.startswith("per_point") else "single_mid") and case.flight_s == 8.0
        assert np.allclose(case.T_I_L[:3, 3], [-0.011, -0.02329, 0.04412])
        gt = load_tum(Path(c["dir"]) / "gt_imu.tum")
        assert len(gt.t) == 1600 and np.all(np.diff(gt.t) > 0)
    tr = lc.LoopTrack(q, flight_s=8.0, hover_s=1.0, ramp_s=1.0)
    gt = load_tum(Path(r["cases"]["per_point-realistic"]["dir"]) / "gt_imu.tum")
    R, p = tr.pose(np.round(gt.t[:5] * 1e9).astype(np.int64))
    assert np.allclose(gt.p[:5], p + np.einsum("nij,j->ni", R, [0.011, 0.02329, -0.04412]), atol=2e-6)
    msgs = {}
    for name, c in r["cases"].items():
        with Bag1Reader(Path(c["dir"]) / "sim.bag") as rd:
            msgs[name] = {tp: [bytes(m.data) for m in rd.messages([tp])] for tp in (IMU_TOPIC, LIDAR_TOPIC)}
    a, b = msgs.values()
    assert a[IMU_TOPIC] == b[IMU_TOPIC] and a[LIDAR_TOPIC] != b[LIDAR_TOPIC]
    dd = r["distortion"]["realistic"]
    assert dd["A_m"]["p50"] >= 0.1 and dd["B_m"]["p50"] >= 0.05, dd
    slam = tmp_path / "slam"
    for name, ate in (("per_point-realistic", 0.04), ("single_pose-realistic", 0.43)):
        (slam / name).mkdir(parents=True)
        (slam / name / "metrics.json").write_text(json.dumps({"status": "completed", "summary": {"lio": {
            "runs": 3, "ok": 3, "ate_aligned_rmse_m": {"median": ate}, "rpe100_pct": {"median": 0.1}, "drift_max_m": {"median": 0.3}}}}))
    col = lc.collect_ate(slam, r["cases"])
    assert col["realistic"]["lio"]["ate_ratio_single_over_per_point"] == pytest.approx(0.43 / 0.04)
    assert lc.judge({"ate": {"slam": col}})["info"]["AC47.ate_ratio"] == {"realistic/lio": pytest.approx(0.43 / 0.04)}


# ---------------------------------------------------------------- Warp（CUDA 或 Warp CPU 设备）


def _assert_diff(d: dict) -> None:
    """对拍断言：原文口径的 p99 与命中不一致率；最大距离差除轮廓边 float32 判定歧义外 ≤ 1 cm（同一表面），歧义与命中不一致合计
    ≤ 1e-4。原文口径的最大值（含歧义射线）由 `judge` 逐条判定并写入报告，不在测试中放宽或断言。"""
    assert d["hit_mismatch_rate"] <= HIT_MISMATCH and d["d_m"]["p99"] <= P99_M, d
    assert d["d_same_surface_m"]["max"] <= MAX_M and d["hit_or_edge_mismatch_rate"] <= HIT_MISMATCH, d
    assert d["edge"]["rays"] == 0 or d["edge"]["gap_abs_max_m"] <= 2e-3, d["edge"]


@pytest.fixture(scope="module")
def warp_dev():
    pytest.importorskip("warp")
    from awr.sensorsvc.lidar import warp_mesh as wm

    return "cuda:0" if wm._wp().get_cuda_device_count() > 0 else "cpu"


def test_pooled_ray_diff_32_actors_weather(scene, warp_dev) -> None:
    """吞吐场景抽样的多帧（不同机、不同 tick；32 个演员；2/3 的帧开天气；clean/realistic/harsh）：全部射线汇总的距离差 p99 ≤ 1 mm、
    最大 ≤ 1 cm，命中不一致 ≤ 1e-4；演员确实被命中且实例一致，观测帧有效槽位差有界。"""
    n = 24 if warp_dev.startswith("cuda") else 10
    reqs = lc.diff_frames(scene, n, fleet_n=16, seed=11)
    assert len({r.uav_no for r in reqs}) > 3 and {r.tier for r in reqs} == {"clean", "realistic", "harsh"}
    d = lc.run_diff(scene, reqs, device=warp_dev, batch=8)
    _REPORT["synthetic_diff"] = d
    assert d["rays"] == n * 20000 and d["frames"] == n
    _assert_diff(d)
    assert d["actor_rays_gpu"] > 50 and d["d_actor_m"]["n"] > 50 and d["inst_mismatch"] <= 2 * n
    assert d["slot_sym_diff"]["max"] <= 6


def test_throughput_loop_and_report(scene, warp_dev) -> None:
    """吞吐循环：每 tick 一次 `scan_batch(copy=False)`，统计后 `ticks` 个；报告含 tick 墙钟分布、`last_timing` 分解、演员候选与分段数、
    同卡负载与独占级别（本机无 nvidia-smi 时为 unknown，不计入）。"""
    r = lc.run_throughput(scene, [6], ticks=4, warmup=1, repeats=2, gpu=(0, None, None), device=warp_dev)
    f = r["fleets"]["6"]
    assert len(f["repeats"]) == 2 and f["tick_p95_ms_median"] > 0
    rep = f["repeats"][0]
    assert rep["tick_ms"]["n"] == 4 and set(rep["breakdown_ms_median"]) == set(lc.TIMING_KEYS)
    assert rep["points_per_frame_mean"] > 1000 and rep["actor_candidates"]["max"] >= 1 and rep["actor_clock"]["calls"] > 0
    assert rep["tick_ms"]["p50"] >= rep["scan_total_ms"]["p50"] * 0.99         # tick 墙钟含请求构造，不小于扫描本身
    assert r["versions"]["backend"] == "warp-mesh"
    j = lc.judge({"throughput": {"fleets": {"100": f}}})
    assert j["items"][0]["counted"] is (set(f["levels"]) == {"A"})


# ---------------------------------------------------------------- 真实世界（CUDA + 世界包）


def test_real_world_diff_and_throughput(tmp_path_factory: pytest.TempPathFactory) -> None:
    """oryahovo-crop1（21 瓦片、2.9M 三角形）：96 帧（32 个演员、天气、三档）对拍满足 D3-AC-12；100 架与 400 架吞吐（每 tick 一批，
    `copy=False`）在 A 级独占时断言 p95 ≤ 30 ms / 80 ms，否则只记录（共享卡开发参考）。ATE 比值需 FAST-LIVO2 容器，由工具链在宿主上测量。"""
    pytest.importorskip("warp")
    from awr.sensorsvc.lidar import warp_mesh as wm

    if not wm.warp_cuda_available():
        pytest.skip("[gpu] no CUDA device for warp")
    wd = _world_dir()
    if wd is None:
        pytest.skip("needs worlds/oryahovo-crop1 (make worlds)")
    tmp = tmp_path_factory.mktemp("lc-real")
    ticks = int(os.environ.get("AWR_LIDAR_CONCURRENCY_TICKS", "100"))
    sc = lc.open_scene(wd, tmp / "cache", n_ticks=max(600, 2 * (ticks + 10) + 60), visibility_m=500.0, rain_mmh=10.0, phase_ns=0)
    d = lc.run_diff(sc, lc.diff_frames(sc, 96), batch=32)
    _assert_diff(d)
    assert d["actor_rays_gpu"] > 100
    gpu = lc.ct.visible_gpu(None)
    thr = lc.run_throughput(sc, [100, 400], ticks=ticks, warmup=10, repeats=1, gpu=gpu)
    doc = {"schema": lc.SCHEMA, "world": str(wd), "scene": sc.describe(), "diff": d, "throughput": thr}
    doc["judge"] = lc.judge(doc)
    _REPORT["real_world"] = doc
    for it in doc["judge"]["items"]:
        if it["counted"]:
            assert it["pass"], it
    assert "error" not in thr["fleets"]["100"], thr["fleets"]["100"]
    for n in ("100", "400"):
        f = thr["fleets"][n]
        if f.get("error"):                                                # 共享卡显存不足（外部进程占用）：记录为未测完，不计入
            assert f["error"].startswith(("RuntimeError", "MemoryError")) and not doc["judge"]["items"][1]["counted"]
            continue
        assert f["repeats"][0]["points_per_frame_mean"] > 1000


@pytest.fixture(scope="module", autouse=True)
def _write_report():
    yield
    p = os.environ.get("AWR_LIDAR_CONCURRENCY_REPORT")
    if p and _REPORT:
        Path(p).write_text(json.dumps(_REPORT, indent=1, ensure_ascii=False, default=float) + "\n", encoding="utf-8")


def test_module_constants_follow_ac12_text() -> None:
    """阈值常数与 §11.2 D3-AC-12 原文一致。"""
    assert (lc.AC12_N100_P95_MS, lc.AC12_N400_P95_MS) == (30.0, 80.0)
    assert (lc.AC12_D_P99_M, lc.AC12_D_MAX_M, lc.AC12_HIT_MISMATCH) == (P99_M, MAX_M, HIT_MISMATCH)
    assert lc.N_ACTORS == 32 and load_lidar_model().slots_per_frame == 20000 and math.isclose(lc.SPIN_DPS, 90.0)
