"""D3-AC-17 中由本机覆盖的三项：跨环境三级容差（f32 深度 p90 ≤ 1 mm、像素偏移 ≤ 0.1 px、LiDAR 逐射线 p99 ≤ 1 mm；另含
M25-FR-172 的 RGB PSNR、实例 IoU、LiDAR 最大差与命中判定不一致率）的比较脚本与断言；`clock_profile` 或平台镜像摘要改变时运行哈希
改变；Agent 超时沿用上一动作并计数（进程内与 Unix 套接字两条路径）。

跨实现对照：同一批射线分别由 Open3D RaycastingScene（float32，与 MeshLidar、MeshWorldQuery 同一求交核）与 numpy float64
Möller–Trumbore 求交，深度图与 LiDAR 距离须落在三级容差内（GPU 型号与驱动之间的对拍在 dmax 上以同一脚本执行）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import socket
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from awr.agentsdk.env.guard import TIMEOUT_CODE, ActionGuard, Limits
from awr.agentsdk.env.spec import EnvSpec, FleetEntry, platform_versions, resolve_clock_profile, run_hash
from awr.agentsdk.env.tolerance import (
    TIER3,
    FrameDump,
    compare_camera,
    compare_dumps,
    compare_lidar,
    depth_stats,
    image_shift_px,
    instance_iou,
    main,
    psnr_db,
)

RNG = np.random.default_rng(20261018)


# ---------------------------------------------------------------------- 合成输入
def _texture(h: int = 270, w: int = 480, sigma: float = 0.1, seed: int = 3):
    """非周期纹理：大幅高斯低通噪声的中心裁剪；返回按 (dy, dx) 亚像素平移后的裁剪（傅里叶平移，平移量精确已知）。"""
    H, W = h + 200, w + 300
    F = np.fft.fft2(np.random.default_rng(seed).normal(size=(H, W)))
    fy, fx = np.fft.fftfreq(H)[:, None], np.fft.fftfreq(W)[None, :]
    F *= np.exp(-(fy ** 2 + fx ** 2) / (2 * sigma ** 2))

    def shifted(dy: float = 0.0, dx: float = 0.0) -> np.ndarray:
        x = np.real(np.fft.ifft2(F * np.exp(-2j * np.pi * (fy * dy + fx * dx))))[100:100 + h, 150:150 + w]
        return x

    return shifted


def _rgb(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    g = np.clip((x - lo) / (hi - lo) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return np.stack([g, (g // 2 + 60).astype(np.uint8), (255 - g)], -1)


def _lidar_obs(slots: np.ndarray, r: np.ndarray, dirs: np.ndarray) -> dict:
    """LidarObs 的字节布局（env.l2._scan）：xyz_m f32（传感器系方向 × 距离）、offset_time_ns u4（槽位 × 5 µs）。"""
    xyz = (dirs[slots] * r[:, None]).astype("<f4")
    return {"sensor_id": "lidar0", "seq": 0, "n": len(slots), "xyz_m": xyz.tobytes(),
            "offset_time_ns": (slots.astype(np.uint32) * np.uint32(5000)).astype("<u4").tobytes()}


def _dirs(n: int) -> np.ndarray:
    az = RNG.uniform(-math.pi, math.pi, n)
    el = RNG.uniform(math.radians(-52), math.radians(7), n)                    # 倒装 MID-360（M25 §6.8）
    return np.stack([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)], 1)


# ---------------------------------------------------------------------- 单项度量与阈值
def test_tier3_thresholds_match_acceptance_text() -> None:
    assert (TIER3.depth_p90_m, TIER3.shift_px, TIER3.lidar_p99_m) == (1e-3, 0.1, 1e-3)              # D3-AC-17
    assert (TIER3.rgb_psnr_db, TIER3.instance_iou, TIER3.lidar_max_m, TIER3.hit_mismatch) == (40.0, 0.98, 1e-2, 1e-4)  # M25-FR-172


def test_depth_p90() -> None:
    d = RNG.uniform(5.0, 300.0, (90, 160)).astype(np.float32)
    d[0, :10] = 0.0                                                                                 # 无效像素
    assert depth_stats(d, d) == {"n": d.size - 10, "p50_m": 0.0, "p90_m": 0.0, "max_m": 0.0, "valid_mismatch": 0.0}
    e = RNG.uniform(-0.6e-3, 0.6e-3, d.shape)
    big = RNG.random(d.shape) < 0.05                                                                # 5% 像素差 5 mm：p90 不受影响
    e[big] = 5e-3
    s = depth_stats(d, (d + e).astype(np.float32))
    assert s["p90_m"] <= TIER3.depth_p90_m and s["max_m"] > TIER3.depth_p90_m
    assert compare_camera({"depth": d}, {"depth": (d + e).astype(np.float32)})["fail"] == []
    off = (d + np.float32(2e-3)).astype(np.float32)
    off[0, :10] = 0.0
    r = compare_camera({"depth": d}, {"depth": off})
    assert 1.9e-3 < r["depth"]["p90_m"] < 2.1e-3 and r["fail"] == ["depth_p90"]
    bad = d.copy()
    bad[1, :20] = np.nan
    assert depth_stats(d, bad)["valid_mismatch"] == pytest.approx(20 / d.size)


@pytest.mark.parametrize("sigma", [0.03, 0.1])
def test_pixel_shift_phase_correlation(sigma: float) -> None:
    img = _texture(sigma=sigma)
    a = img()
    lo, hi = float(a.min()), float(a.max())
    for dy, dx in [(0.0, 0.0), (0.05, 0.0), (0.0, -0.08), (0.04, 0.05), (0.25, 0.1), (3.0, -2.0)]:
        got = image_shift_px(a, img(dy, dx))
        assert got == pytest.approx((dy, dx), abs=0.04), (dy, dx, got)
    ok = compare_camera({"rgb": _rgb(a, lo, hi)}, {"rgb": _rgb(img(0.05, 0.03), lo, hi)})
    assert ok["shift_px"] <= TIER3.shift_px and "shift" not in ok["fail"]
    bad = compare_camera({"rgb": _rgb(a, lo, hi)}, {"rgb": _rgb(img(0.15, 0.0), lo, hi)})
    assert bad["shift_px"] > TIER3.shift_px and "shift" in bad["fail"]
    n = RNG.normal(scale=0.02 * a.std(), size=a.shape)                                               # 噪声不产生虚假偏移
    assert math.hypot(*image_shift_px(a, a + n)) < 0.02


def test_shift_from_inverse_depth_when_no_rgb() -> None:
    img = _texture(90, 160, sigma=0.08)
    d0, d1 = (60.0 + 5.0 * img(), 60.0 + 5.0 * img(0.3, 0.0))
    r = compare_camera({"depth": d0.astype(np.float32)}, {"depth": d1.astype(np.float32)})
    assert r["shift_yx_px"][0] == pytest.approx(0.3, abs=0.05) and "shift" in r["fail"]


def test_psnr_and_instance_iou() -> None:
    a = RNG.integers(0, 256, (64, 96, 3), dtype=np.uint8)
    assert psnr_db(a, a) == math.inf
    b = np.clip(a.astype(np.int16) + RNG.integers(-2, 3, a.shape), 0, 255).astype(np.uint8)          # ±2 级：≈ 46 dB
    assert psnr_db(a, b) >= TIER3.rgb_psnr_db
    c = np.clip(a.astype(np.int16) + RNG.integers(-12, 13, a.shape), 0, 255).astype(np.uint8)
    assert compare_camera({"rgb": a}, {"rgb": c})["fail"] == ["rgb_psnr"]
    ia = np.zeros((100, 100), np.uint16)
    ia[10:60, 10:60] = 3                                                                            # 2500 px
    ia[70:80, 70:80] = 9
    ib = ia.copy()
    ib[10:60, 60] = 3                                                                               # +50 px：IoU = 2500/2550 ≈ 0.980
    iou = instance_iou(ia, ib)
    assert iou[9] == 1.0 and iou[3] == pytest.approx(2500 / 2550) and iou[3] >= TIER3.instance_iou
    ib[10:60, 61] = 3
    r = compare_camera({"instance": ia}, {"instance": ib})
    assert r["instance_iou_min"] < TIER3.instance_iou and r["fail"] == ["instance_iou"]


def test_lidar_per_ray_p99_max_and_hit_mismatch() -> None:
    n_rays = 20_000
    dirs = _dirs(n_rays)
    slots = np.sort(RNG.choice(n_rays, 15_000, replace=False))
    r = RNG.uniform(0.5, 70.0, len(slots))
    a = _lidar_obs(slots, r, dirs)
    same = compare_lidar(a, copy.deepcopy(a), n_rays)
    assert same["p99_m"] < 1e-5 and same["hit_mismatch"] == 0.0 and same["fail"] == []           # f32 量化往返
    e = RNG.uniform(-0.5e-3, 0.5e-3, len(slots))
    e[:100] = 4e-3                                                                                  # < 1% 射线 4 mm：p99 ≤ 1 mm、max ≤ 1 cm
    keep = np.ones(len(slots), bool)
    keep[RNG.choice(len(slots), 2, replace=False)] = False                                          # 2 条射线只在 A 命中：1e-4
    b = _lidar_obs(slots[keep], (r + e)[keep], dirs)
    ok = compare_lidar(a, b, n_rays)
    assert ok["p99_m"] <= TIER3.lidar_p99_m and ok["max_m"] <= TIER3.lidar_max_m and ok["hit_mismatch"] == 1e-4 and ok["fail"] == []
    e2 = e.copy()
    e2[:300] = 2e-3                                                                                 # 2% 射线 2 mm：p99 越限
    e2[300] = 0.02                                                                                  # 单条 2 cm：最大越限
    keep[RNG.choice(len(slots), 3, replace=False)] = False
    bad = compare_lidar(a, _lidar_obs(slots[keep], (r + e2)[keep], dirs), n_rays)
    assert set(bad["fail"]) == {"lidar_p99", "lidar_max", "hit_mismatch"}


# ---------------------------------------------------------------------- 跨实现对照（float32 BVH 与 float64 解析求交）
def _scene():
    """约 300 m × 300 m 起伏地面（20 × 20 格）与两栋楼，原点平移到 (1800, −1200, 95) m 量级（与 ENU 世界坐标同量级）。"""
    n, L = 20, 300.0
    xs = np.linspace(-L / 2, L / 2, n + 1)
    X, Y = np.meshgrid(xs, xs, indexing="ij")
    Z = 3.0 * np.sin(X / 37.0) * np.cos(Y / 23.0)
    V = np.stack([X.ravel(), Y.ravel(), Z.ravel()], 1)
    idx = np.arange((n + 1) ** 2).reshape(n + 1, n + 1)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel(), idx[:-1, 1:].ravel()
    F = [np.stack([a, b, c], 1), np.stack([a, c, d], 1)]
    for (cx, cy, hx, hy, h) in [(25.0, 10.0, 8.0, 12.0, 30.0), (-30.0, -20.0, 10.0, 6.0, 18.0)]:
        o = len(V)
        bx = np.array([[cx + sx * hx, cy + sy * hy, z] for z in (-2.0, h) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
        V = np.vstack([V, bx])
        q = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
        F += [np.array([[o + i, o + j, o + k], [o + i, o + k, o + m]]) for i, j, k, m in q]
    origin = np.array([1800.0, -1200.0, 95.0])
    return V + origin, np.vstack(F).astype(np.int64), origin


def _mt_f64(V: np.ndarray, F: np.ndarray, o: np.ndarray, d: np.ndarray) -> np.ndarray:
    """float64 Möller–Trumbore：每条射线最近交点距离（未命中为 inf）。"""
    v0, e1, e2 = V[F[:, 0]], V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]
    out = np.full(len(d), np.inf)
    for s in range(0, len(d), 512):
        dd, oo = d[s:s + 512, None, :], o[s:s + 512, None, :]
        p = np.cross(dd, e2[None])
        det = np.einsum("rtk,tk->rt", p, e1)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        tv = oo - v0[None]
        u = np.einsum("rtk,rtk->rt", tv, p) * inv
        q = np.cross(tv, e1[None])
        v = np.einsum("rtk,rtk->rt", dd, q) * inv
        t = np.einsum("rtk,tk->rt", q, e2) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-6)
        out[s:s + 512] = np.where(hit, t, np.inf).min(1)
    return out


def _o3d_f32(V: np.ndarray, F: np.ndarray, o: np.ndarray, d: np.ndarray) -> np.ndarray:
    o3d = pytest.importorskip("open3d")
    sc = o3d.t.geometry.RaycastingScene()
    sc.add_triangles(o3d.core.Tensor(V.astype(np.float32)), o3d.core.Tensor(F.astype(np.uint32)))
    rays = np.hstack([o, d]).astype(np.float32)
    return sc.cast_rays(o3d.core.Tensor(rays))["t_hit"].numpy().astype(np.float64)


def test_cross_implementation_depth_and_lidar_within_tier3(tmp_path: Path) -> None:
    V, F, org = _scene()
    # 相机：160 × 90，高 60 m、俯视 35°，平面深度 = t · (d · 光轴)
    w, h, f = 160, 90, 120.0
    pc = org + np.array([-90.0, -40.0, 60.0])
    pitch, yaw = math.radians(-35.0), math.radians(20.0)
    fwd = np.array([math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)])
    left = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
    up = np.cross(fwd, left)
    u, v = np.meshgrid(np.arange(w) + 0.5 - w / 2, np.arange(h) + 0.5 - h / 2)
    d = fwd[None] * f - u.ravel()[:, None] * left[None] - v.ravel()[:, None] * up[None]
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    o = np.broadcast_to(pc, d.shape).copy()
    t64, t32 = _mt_f64(V, F, o, d), _o3d_f32(V, F, o, d)
    cos = d @ fwd
    dep = [np.where(np.isfinite(t), t * cos, 0.0).reshape(h, w).astype(np.float32) for t in (t64, t32)]
    rep = compare_camera({"depth": dep[0]}, {"depth": dep[1]})
    assert rep["depth"]["n"] > 0.9 * w * h and rep["depth"]["p90_m"] <= TIER3.depth_p90_m and rep["shift_px"] <= TIER3.shift_px, rep
    # LiDAR：MID-360 视场 4000 条射线，原点在楼间 20 m 高
    n = 4000
    dl = _dirs(n)
    ol = np.broadcast_to(org + np.array([0.0, 0.0, 20.0]), dl.shape).copy()
    r64, r32 = _mt_f64(V, F, ol, dl), _o3d_f32(V, F, ol, dl)
    slots = np.arange(n)
    ha, hb = np.isfinite(r64) & (r64 <= 70.0), np.isfinite(r32) & (r32 <= 70.0)
    rl = compare_lidar(_lidar_obs(slots[ha], r64[ha], dl), _lidar_obs(slots[hb], r32[hb], dl), n)
    assert rl["n_common"] > 1000 and rl["fail"] == [], rl
    # 同一对拍经比较脚本（导出目录 → CLI）
    for name, dd, hit, r in (("a", dep[0], ha, r64), ("b", dep[1], hb, r32)):
        fd = FrameDump(tmp_path / name)
        fd.camera("uav-01", "eo", 7, depth=dd)
        fd.lidar("uav-01", "lidar0", 6, _lidar_obs(slots[hit], r[hit], dl), n)
    assert main([str(tmp_path / "a"), str(tmp_path / "b"), "--out", str(tmp_path / "rep.json")]) == 0
    doc = json.loads((tmp_path / "rep.json").read_text())
    assert doc["pass"] and doc["n_frames"] == 2 and doc["worst"]["depth_p90_m"] <= 1e-3 and doc["worst"]["lidar_p99_m"] <= 1e-3


def test_compare_script_exit_codes(tmp_path: Path, capsys) -> None:
    img = _texture(90, 160)
    x = img()
    lo, hi = float(x.min()), float(x.max())
    dep = (50.0 + x).astype(np.float32)
    inst = np.zeros((90, 160), np.uint16)
    inst[20:50, 30:70] = 4
    a, b, c = FrameDump(tmp_path / "a"), FrameDump(tmp_path / "b"), FrameDump(tmp_path / "c")
    for fd in (a, b, c):
        fd.camera("uav-01", "eo", 0, rgb=_rgb(x, lo, hi), depth=dep, instance=inst)
    a.camera("uav-01", "eo", 1, rgb=_rgb(x, lo, hi), depth=dep)
    b.camera("uav-01", "eo", 1, rgb=_rgb(x, lo, hi), depth=(dep + np.float32(0.5e-3)).astype(np.float32))
    c.camera("uav-01", "eo", 1, rgb=_rgb(img(0.0, 0.2), lo, hi), depth=dep)                           # 0.2 px 偏移
    assert main([str(tmp_path / "a"), str(tmp_path / "b")]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["pass"] and doc["worst"]["rgb_psnr_db"] is None and doc["worst"]["depth_p90_m"] <= 1e-3   # 逐字节相同的 PSNR 记 null
    assert main([str(tmp_path / "a"), str(tmp_path / "c")]) == 1
    doc = json.loads(capsys.readouterr().out)
    fr = {f["seq"]: f for f in doc["frames"]}
    assert fr[0]["fail"] == [] and "shift" in fr[1]["fail"] and fr[1]["shift_px"] == pytest.approx(0.2, abs=0.04) and doc["n_fail"] == 1
    c.camera("uav-02", "eo", 0, rgb=_rgb(x, lo, hi))
    assert main([str(tmp_path / "a"), str(tmp_path / "c")]) == 2                                      # 帧集合不一致
    assert json.loads(capsys.readouterr().out)["only_b"] == [["uav-02", "eo", 0]]


def test_frame_dump_from_agent_observation(tmp_path: Path) -> None:
    from awr.sensorsvc.frame import encode_jpeg

    x = _texture(90, 160)()
    rgb = _rgb(x, float(x.min()), float(x.max()))
    dep = (40.0 + x).astype("<f4")
    cam = {"sensor_id": "eo", "seq": 3, "width": 160, "height": 90, "rgb": {"codec": "jpeg", "data": encode_jpeg(rgb, 90)},
           "depth": {"encoding": "f32le", "data": dep.tobytes()}}
    dirs = _dirs(200)
    lid = _lidar_obs(np.arange(0, 200, 2), RNG.uniform(1, 50, 100), dirs)
    for name in ("a", "b"):
        FrameDump(tmp_path / name).obs("uav-01", {"eo": [cam], "lidar0": [lid], "gnss0": [{"seq": 3}]}, {"lidar0": 200})
    rep = compare_dumps(tmp_path / "a", tmp_path / "b")
    assert rep["pass"] and rep["n_frames"] == 2
    with np.load(tmp_path / "a/uav-01/eo/000003.npz") as z:
        assert z["rgb"].shape == (90, 160, 3) and np.array_equal(z["depth"], dep)


# ---------------------------------------------------------------------- 运行哈希
def _rs(**kw) -> dict:
    rs = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": "t"}, "world": {"id": "synth-glb"}, "event": {"id": "ev", "version": 1},
          "fleet": [{"uav_id": "uav-01", "vehicle_ref": "x500", "start": {"kind": "pose", "pos_enu_m": [-10.0, 50.0, 0.0], "yaw_deg": 0.0}},
                    {"uav_id": "uav-02", "vehicle_ref": "x500", "start": {"kind": "pose", "pos_enu_m": [10.0, 50.0, 0.0], "yaw_deg": 0.0}}],
          "policy": {"kind": "image", "image_digest": "sha256:" + "a" * 64}, "obs_level": "L2", "physics": "realistic", "duration_s": 60.0}
    rs.update(kw)
    return rs


def _id(rs: dict, seed: int = 11, **kw) -> dict:
    return EnvSpec.from_run_spec(rs, run_id="run-x", seed=seed, worlds_root=Path("/w"), events_root=Path("/e"), **kw).identity()


IMAGES = {"gz": "sha256:" + "1" * 64, "sensorsvc": "sha256:" + "2" * 64, "env": "sha256:" + "3" * 64}


def test_run_hash_tracks_clock_profile_and_platform_images() -> None:
    from awr.agentsdk.schemas import bench_validator

    rs = _rs()
    assert not list(bench_validator("run_spec").iter_errors(rs))
    base = _id(rs, images=IMAGES)
    assert base == _id(copy.deepcopy(rs), images=dict(IMAGES))                                        # 同一规格稳定
    assert base["clock_profile"] == {"name": "d1_250", "main_hz": 250, "physics_hz": 250, "l1_hz": 250}
    assert base["versions"]["image.gz"] == IMAGES["gz"] and base["versions"]["image.agent"] == "sha256:" + "a" * 64
    # clock_profile：缺省与显式 d1_250 及其四元组是同一配置；档名或任一频率改变即改变配置与运行哈希
    assert _id(_rs(clock_profile="d1_250"), images=IMAGES)["run_hash"] == base["run_hash"]
    assert _id(_rs(clock_profile=resolve_clock_profile("d1_250")), images=IMAGES)["run_hash"] == base["run_hash"]
    for cp in ("sandbox100", {"name": "d1_250", "main_hz": 250, "physics_hz": 125, "l1_hz": 125},
               {"name": "d1_250", "main_hz": 250, "physics_hz": 250, "l1_hz": 125}):
        o = _id(_rs(clock_profile=cp), images=IMAGES)
        assert o["run_hash"] != base["run_hash"] and o["config_sha256"] != base["config_sha256"], cp
    # 平台镜像摘要：gz、M25 Worker、Env 镜像与 Agent 镜像任一改变 → spec 不变而 run_hash 改变
    for k in ("gz", "sensorsvc", "env"):
        o = _id(rs, images={**IMAGES, k: "sha256:" + "f" * 64})
        assert o["spec_sha256"] == base["spec_sha256"] and o["run_hash"] != base["run_hash"], k
    o = _id(_rs(policy={"kind": "image", "image_digest": "sha256:" + "b" * 64}), images=IMAGES)
    assert o["run_hash"] != base["run_hash"]
    assert _id(rs, images={**IMAGES, "sensorsvc": None})["run_hash"] != base["run_hash"]             # 缺失摘要与有摘要不同
    # 不影响结果的键：实验名、retention、priority 不改变任何哈希；种子只改变 spec 与 run_hash
    same = _id(_rs(experiment={"name": "other"}, retention="30d", priority=3), images=IMAGES)
    assert {k: same[k] for k in ("config_sha256", "spec_sha256", "run_hash")} == {k: base[k] for k in ("config_sha256", "spec_sha256", "run_hash")}
    s2 = _id(rs, seed=12, images=IMAGES)
    assert s2["config_sha256"] == base["config_sha256"] and s2["spec_sha256"] != base["spec_sha256"] and s2["run_hash"] != base["run_hash"]
    # physics 覆盖（开发档）进入哈希
    assert _id(rs, images=IMAGES, physics="kinematic")["run_hash"] != base["run_hash"]


def test_run_hash_formula_and_ranked_seed_commit() -> None:
    from awr.sim.events.jcs import jcs

    v = platform_versions(IMAGES)
    assert set(v) == {"env", "python", "numpy", "image.gz", "image.sensorsvc", "image.env"}
    spec_h = hashlib.sha256(b"x").hexdigest()
    assert run_hash(spec_h, v) == hashlib.sha256(bytes.fromhex(spec_h) + jcs(v)).hexdigest()         # M27-FR-007
    with pytest.raises(TypeError):
        run_hash(spec_h, {"image.gz": 1})
    with pytest.raises(ValueError):
        platform_versions({"gpu": "a40"})
    with pytest.raises(ValueError, match="seed_commit"):
        _id(_rs(ranked=True))
    c1, c2 = "c" * 64, "d" * 64
    r1, r2 = _id(_rs(ranked=True), seed_commit=c1), _id(_rs(ranked=True), seed_commit=c2)
    assert r1["spec_sha256"] != r2["spec_sha256"] and r1["config_sha256"] == r2["config_sha256"]
    assert _id(_rs(ranked=True), seed=99, seed_commit=c1)["run_hash"] == r1["run_hash"]             # 排名运行的种子明文不进哈希


def test_direct_env_spec_identity() -> None:
    fleet = (FleetEntry("uav-01", (0.0, 0.0, 0.0)),)
    a = EnvSpec("r", 5, Path("/w/synth"), Path("/e/ev/v1"), fleet, versions=platform_versions(IMAGES))
    assert a.run_hash() == EnvSpec("r2", 5, Path("/w/synth"), Path("/e/ev/v1"), fleet, versions=platform_versions(IMAGES)).run_hash()
    assert a.run_hash() != EnvSpec("r", 5, Path("/w/synth"), Path("/e/ev/v1"), fleet, clock_profile="sandbox100",
                                   versions=platform_versions(IMAGES)).run_hash()
    assert a.run_hash() != EnvSpec("r", 5, Path("/w/synth"), Path("/e/ev/v1"), fleet,
                                   versions=platform_versions({**IMAGES, "gz": "sha256:" + "9" * 64})).run_hash()


# ---------------------------------------------------------------------- Agent 超时
class _Cam:
    sensor_id = "eo"
    pitch_range_deg = (-90.0, 30.0)
    yaw_range_deg = (-180.0, 180.0)


def _act(k: int, **kw) -> dict:
    return {"schema": "drone4d.bench.act.v1", "k": k, **kw}


def test_guard_timeout_holds_last_accepted_command() -> None:
    g = ActionGuard("uav-01", ["uav-02"], Limits(), ["meet"], True, 4096, _Cam())
    z = np.zeros(3)
    r = g.timeout(0)
    assert r.acks == [{"k": 0, "item": "act", "index": 0, "result": "timeout", "code": TIMEOUT_CODE}] and r.held == {"nav": None, "gimbal": None}
    g.check(_act(1, nav={"mode": "velocity", "vel_mps": [2.0, 0.0, 0.0], "frame": "world"},
                 gimbal={"sensor_id": "eo", "mode": "angles", "pitch_deg": -30.0, "yaw_deg": 10.0},
                 msgs=[{"dst": "uav-02", "topic": "x", "payload": b"1"}]), 1, z, 0.0, z, 0.0)
    held = {"nav": {"kind": "velocity", "vel_enu": (2.0, 0.0, 0.0), "yaw_rate": 0.0}, "gimbal": [-30.0, 10.0]}
    r = g.timeout(2)
    assert r.held == held and r.nav is None and r.msgs == [] and r.gimbal is None                    # 一次性条目不重放
    g.check({"k": 3}, 3, z, 0.0, z, 0.0)                                                            # schema 拒绝
    g.check(_act(5), 4, z, 0.0, z, 0.0)                                                             # k 不一致
    g.check(_act(5, nav={"mode": "orbit", "center_enu_m": [0, 0], "alt_m": 50, "radius_m": 20, "speed_mps": 5, "cw": True}), 5, z, 0.0, z, 0.0)
    assert g.timeout(6).held == held and g.timeouts == 3                                            # 被拒绝的动作不改变沿用指令
    g.check(_act(7, nav={"mode": "hover"}), 7, z, 0.0, z, 0.0)
    g.tier_rejected()                                                                               # 物理档拒收：恢复上一条
    assert g.timeout(8).held["nav"] == held["nav"]


@pytest.fixture()
def env_fixture(synth_event):
    pytest.importorskip("open3d")
    return synth_event


def _airborne_spec(synth_event, out: Path, dur: float, uavs=("uav-01", "uav-02")) -> EnvSpec:
    w, ev = synth_event
    fleet = tuple(FleetEntry(u, (-10.0 + 20.0 * i, 50.0, 40.0), airborne=True) for i, u in enumerate(uavs))
    return EnvSpec("t-timeout", 5, w, ev, fleet, physics="kinematic", duration_s=dur, out_dir=out)


def test_env_timeout_keeps_previous_action_and_counts(env_fixture, tmp_path: Path) -> None:
    """进程内：uav-01 在 k = 0 下达 2 m/s 东向速度后 k = 1…10 不返回动作 → 10 次 661 回执、清单计数 10，速度指令持续生效（与
    每步重复同一速度指令的对照运行位姿逐位一致）；uav-02 每步重复指令。"""
    from awr.agentsdk.env.core import BenchEnv

    vel = {"nav": {"mode": "velocity", "vel_mps": [2.0, 0.0, 0.0], "frame": "world"}}
    runs = {}
    for name, silent in (("timeout", set(range(1, 11))), ("repeat", set())):
        env = BenchEnv(_airborne_spec(env_fixture, tmp_path / name, 2.0))
        obs, _ = env.reset()
        acks = []
        while env.agents:
            acts = {}
            for u, o in obs.items():
                acks += [(u, a["k"], a["result"], a["code"]) for a in o["obs"]["acks"]]
                k = o["obs"]["k"]
                if not (u == "uav-01" and k in silent):
                    acts[u] = _act(k, **vel) if k <= 10 else _act(k, nav={"mode": "hover"})
            obs, *_ = env.step(acts)
        m = env.close()
        with np.load(tmp_path / name / "truth/fleet.npz") as z:
            runs[name] = (m, acks, z["pos"].copy(), z["t_ns"].copy())
    m, acks, pos, t = runs["timeout"]
    assert m["agents"]["uav-01"]["timeouts"] == 10 and m["agents"]["uav-02"]["timeouts"] == 0
    assert sorted(k for u, k, res, code in acks if u == "uav-01" and res == "timeout" and code == 661) == list(range(1, 11))
    i = int(np.searchsorted(t, 1_100_000_000))
    assert pos[i, 0, 0] - pos[0, 0, 0] > 1.0                                                        # 超时期间仍按 2 m/s 东向飞行
    assert np.array_equal(pos, runs["repeat"][2]) and m["pose_sha256"] == runs["repeat"][0]["pose_sha256"]


def test_socket_agent_timeout_is_counted_and_late_act_dropped(env_fixture, tmp_path: Path) -> None:
    """Unix 套接字：Agent 在 k = 3、7 超出墙钟预算后才回复 → 宿主 timeouts = 2、迟到 ACT 丢弃 late = 2，Env 回执 661 并计数。"""
    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.remote import SocketAgentHost, run_remote_episode
    from awr.agentsdk.wire import PROTOCOL, T, WireSocket

    spec = _airborne_spec(env_fixture, tmp_path / "run", 1.5, uavs=("uav-01",))
    sock = Path("/tmp") / f"d3s05-{time.time_ns() % 10**9}.sock"
    host = SocketAgentHost("uav-01", sock)
    budget, slow = 0.5, {3, 7}
    seen: list[tuple[int, list]] = []

    def agent() -> None:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock))
        ws = WireSocket(c)
        ws.send(T.HELLO, {"protocol": PROTOCOL, "sdk_version": "test", "prefs": {}})
        assert ws.recv(time.monotonic() + 10)[0] == T.CONFIG
        ws.send(T.READY, {})
        while True:
            t, body = ws.recv(time.monotonic() + 30)
            if t != T.OBS:
                break
            k = body["obs"]["k"]
            seen.append((k, [(a["k"], a["result"]) for a in body["obs"]["acks"]]))
            if k in slow:
                time.sleep(budget + 0.2)                                         # 超出预算、早于下一步预算到期
            ws.send(T.ACT, _act(k, nav={"mode": "velocity", "vel_mps": [1.0, 0.0, 0.0], "frame": "world"}))
        c.close()

    th = threading.Thread(target=agent, daemon=True)
    th.start()
    try:
        host.accept()
        m, stats = run_remote_episode(BenchEnv(spec), {"uav-01": host}, step_budget_s=budget)
    finally:
        th.join(10)
    assert stats["uav-01"]["timeouts"] == 2 and stats["uav-01"]["late"] == 2 and stats["uav-01"]["exited"] is None
    assert m["agents"]["uav-01"]["timeouts"] == 2
    acked = {kk: res for _k, acks in seen for kk, res in acks}
    assert acked[3] == "timeout" and acked[7] == "timeout" and acked[4] == "applied"
