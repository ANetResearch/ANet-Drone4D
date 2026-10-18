"""crop `physics` 导出档校验（D3-AC-34 第 1 句；M24 §6.7.4 A1、A2，§6.7.1 环境行）。

- 离线（本机 .venv / .venv-d3）：碰撞同点偏差的度量、故意错误的变换返回 636 且不发布、运行级组合写出显式重力 9.80665；
  有 `worlds/oryahovo-crop1` 时导出 crop 并核对 10,000 个碰撞顶点的同点偏差 ≤ 1 cm，有 open3d 时再以 MeshWorldQuery
  （raycast 运行时）求碰撞面采样点到 raycast 网格表面的距离。
- d3-gz 容器（dmax，tools/deploy/dmax/run_gz_tests.sh 传入本文件）：crop 导出的 `gz sdf -k world_static.sdf` 与样例 run.sdf、
  `gz sdf -p` 解析后的重力，以及锁步下 crop 上空自由落体的实测加速度。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from awr.phys.gz.compose import G, UavSpawn, compose_run
from awr.phys.pose_history import G_W
from awr.world.export import gz as gzx
from awr.world.export.gz import (
    DEVIATION_MAX_M,
    GRAVITY_MPS2,
    MODEL,
    GzExportError,
    collision_deviation,
    export_world,
    sdf_check,
)

WORLD = Path(__file__).resolve().parents[3] / "worlds" / "oryahovo-crop1"
HAS_CROP = (WORLD / "geometry/mesh/raycast/index.json").exists()
HAS_GZ = shutil.which("gz") is not None
HAS_PLUGIN = any((Path(p) / "libDrone4DGz.so").exists() for p in os.environ.get("GZ_SIM_SYSTEM_PLUGIN_PATH", "").split(":") if p)
needs_crop = pytest.mark.skipif(not HAS_CROP, reason="needs worlds/oryahovo-crop1")
needs_gz = pytest.mark.skipif(not HAS_GZ, reason="needs gz (d3-gz image)")


def _world(root: Path) -> Path:
    """两瓦片的合成 awr-trimesh@1 世界（瓦片局部 float32 顶点，第二瓦片远离原点）。"""
    w = root / "w1"
    d = w / "geometry/mesh/raycast"
    d.mkdir(parents=True)
    rng = np.random.default_rng(5)
    tiles, xyz, tri = [], [], []
    v0 = t0 = 0
    for i, origin in enumerate(([-512.0, 0.0, 0.0], [2048.0, -1536.0, 0.0])):
        V = rng.uniform(0, 400, (300, 3)).astype(np.float32)
        F = np.stack([rng.permutation(300)[:3] for _ in range(500)]).astype(np.uint32)
        tiles.append({"id": i, "origin_m": origin, "aabb_m": {"min": (np.asarray(origin) + V.min(0)).tolist(),
                                                             "max": (np.asarray(origin) + V.max(0)).tolist()},
                      "v0": v0, "nv": len(V), "t0": t0, "nt": len(F)})
        xyz.append(V)
        tri.append(F)
        v0 += len(V)
        t0 += len(F)
    np.concatenate(xyz).tofile(d / "xyz.f32")
    np.concatenate(tri).tofile(d / "tri.u32")
    (d / "index.json").write_text(json.dumps({"coordinate_sha256": "c" * 64, "counts": {"vertices": v0, "triangles": t0}, "tiles": tiles}))
    (w / "world.json").write_text(json.dumps({"id": "w1", "contentVersion": "abc123", "layers": [
        {"role": "geometry", "type": "mesh", "format": "awr-trimesh@1", "href": "geometry/mesh/raycast"}]}))
    (w / "coordinate.json").write_text(json.dumps({"scaleStatus": "landmark", "anchor": {"latDeg": 43.7, "lonDeg": 23.9, "hEllipsoidM": 115.2}}))
    return w


def _gravity(sdf_text: str) -> list[float]:
    return [float(x) for x in ET.fromstring(sdf_text).find("world/gravity").text.split()]


# ------------------------------------------------------------------ 离线：度量与拒绝


def test_nearest_within_matches_brute_force() -> None:
    rng = np.random.default_rng(1)
    ref = rng.uniform(-3.0, 3.0, (4000, 3)) + np.array([2300.0, -1800.0, 90.0])
    q = ref[rng.choice(len(ref), 600, replace=False)] + rng.normal(0.0, 0.03, (600, 3))
    d = gzx._nearest_within(ref, q, 0.05)
    bf = np.linalg.norm(q[:, None, :] - ref[None, :, :], axis=2).min(1)
    assert np.array_equal(np.isfinite(d), bf <= 0.05)
    assert np.allclose(d[np.isfinite(d)], bf[bf <= 0.05], rtol=0, atol=1e-12)


def test_synthetic_export_deviation_is_zero(tmp_path: Path) -> None:
    w = _world(tmp_path)
    e = export_world(w, out_root=tmp_path / "e", max_faces=128, check_sdf=False)
    a2 = e.manifest["qa"]["A2_collision_deviation"]
    assert a2 == {"samples": 3000, "max_m": 0.0, "p99_m": 0.0, "mean_m": 0.0, "unmatched_rate": 0.0, "radius_m": 0.05,
                  "threshold_m": DEVIATION_MAX_M, "pass": True}                   # 1000 面 × 3 顶点 < 10,000：全部抽取
    assert e.manifest["environment"] == {"gravity_mps2": [0.0, 0.0, -GRAVITY_MPS2]}
    assert e.manifest["counts"]["chunks"] == 8


@pytest.mark.parametrize("shift, rot", [((0.02, 0.0, 0.0), ""), ((0.0, 0.0, -0.015), ""), ((0.0, 0.0, 0.0), "0 0 0.0002"),
                                        ((1.0, 0.0, 0.0), "")])
def test_wrong_transform_is_rejected_with_636(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shift, rot) -> None:
    """故意错误的碰撞位姿（平移 2 cm、1.5 cm、1 m 或偏航 0.2 mrad）使同点偏差失败：636，导出目录不发布、staging 已清理。"""
    orig = gzx._render_sdf

    def bad(cols):
        sdf = orig([{**c, "origin_m": [o + s for o, s in zip(c["origin_m"], shift, strict=True)]} for c in cols])
        return sdf.replace(" 0 0 0</pose>", f" {rot}</pose>") if rot else sdf

    monkeypatch.setattr(gzx, "_render_sdf", bad)
    out = tmp_path / "bad"
    with pytest.raises(GzExportError) as e:
        export_world(_world(tmp_path), out_root=out, max_faces=128, check_sdf=False)
    assert e.value.code == 636 and "A2" in str(e.value)
    assert not (out / "physics").exists() and not list(out.glob("physics.staging-*"))


def test_deviation_measures_corrupted_export(tmp_path: Path) -> None:
    """对已发布导出改写 SDF 位姿后直接度量：偏差等于注入量；1 m 平移全部未匹配。"""
    w = _world(tmp_path)
    e = export_world(w, out_root=tmp_path / "e", max_faces=128, check_sdf=False)
    sdf_path = e.model_dir / f"{MODEL}.sdf"
    good = sdf_path.read_text()
    raycast = w / "geometry/mesh/raycast"
    for dz, expect_max, expect_unmatched in ((0.008, 0.008, 0.0), (0.03, 0.03, 0.0), (1.0, None, 1.0)):
        sdf_path.write_text(good.replace(" 0.0 0 0 0</pose>", f" {dz!r} 0 0 0</pose>"))
        d = collision_deviation(e.model_dir, raycast)
        assert d["unmatched_rate"] == expect_unmatched
        if expect_max is not None:
            assert abs(d["max_m"] - expect_max) < 1e-9 and d["pass"] == (expect_max <= DEVIATION_MAX_M)
        else:
            assert not d["pass"]
    sdf_path.write_text(good)
    assert collision_deviation(e.model_dir, raycast)["max_m"] == 0.0


def test_run_composition_writes_explicit_gravity(tmp_path: Path) -> None:
    """重力 9.80665 显式写入 run.sdf（不依赖 SDFormat 缺省 9.8），导出清单、组合与 PoseHistory 三处取值相同。"""
    e = export_world(_world(tmp_path), out_root=tmp_path / "e", max_faces=128, check_sdf=False)
    sdf = compose_run([UavSpawn("uav-01", (0.0, 0.0, 30.0))], world_static_uri=e.uri, spherical=e.spherical)
    assert "<gravity>0 0 -9.80665</gravity>" in sdf
    assert _gravity(sdf) == [0.0, 0.0, -9.80665] == e.manifest["environment"]["gravity_mps2"]
    assert G == GRAVITY_MPS2 == -float(G_W[2]) == 9.80665
    assert ET.fromstring(sdf).find("world/physics/max_step_size").text == "0.004000"


# ------------------------------------------------------------------ crop 导出（本机或 d3-gz 容器）


@pytest.fixture(scope="module")
def crop(tmp_path_factory: pytest.TempPathFactory):
    """crop `physics` 导出（有 gz 时执行 A1）；本模块共用一次导出。"""
    if not HAS_CROP:
        pytest.skip("needs worlds/oryahovo-crop1")
    return export_world(WORLD, out_root=tmp_path_factory.mktemp("crop"), check_sdf=None, force=True)


@needs_crop
def test_crop_physics_export_deviation(crop) -> None:
    m = crop.manifest
    a2 = m["qa"]["A2_collision_deviation"]
    assert a2["samples"] == 10_000 and a2["pass"]
    assert a2["max_m"] <= DEVIATION_MAX_M and a2["unmatched_rate"] <= 0.001        # D3-AC-34：同点偏差 ≤ 1 cm
    assert m["qa"]["A1_sdf_check"] == ("pass" if HAS_GZ else "skipped")
    assert m["counts"]["triangles"] == json.loads((WORLD / "geometry/mesh/raycast/index.json").read_text())["counts"]["triangles"]
    assert all(c["triangles"] <= 50_000 for c in m["collisions"])
    assert m["environment"]["gravity_mps2"] == [0.0, 0.0, -9.80665]


@needs_crop
def test_crop_collision_surface_vs_raycast_runtime(crop) -> None:
    """独立口径：碰撞 STL 上 5,000 个三角形内点（重心坐标随机）经 SDF 位姿变到 world，由 MeshWorldQuery（open3d raycast 运行时）求到
    raycast 网格表面的距离，最大值 ≤ 1 cm。"""
    pytest.importorskip("open3d")
    from awr.world.mesh.query import open_mesh_world_query

    rng = np.random.default_rng(7)
    cols = ET.parse(crop.model_dir / f"{MODEL}.sdf").getroot().findall("model/link/collision")
    pick = rng.choice(len(cols), 5000)
    pts = []
    for i in np.unique(pick):
        c = cols[i]
        pose = np.array([float(x) for x in c.findtext("pose").split()])
        assert np.all(pose[3:] == 0.0)                                            # 碰撞位姿只含平移
        raw = (crop.model_dir / c.findtext("geometry/mesh/uri").removeprefix(f"model://{MODEL}/")).read_bytes()
        n = int(np.frombuffer(raw[80:84], "<u4")[0])
        tri = np.frombuffer(raw, gzx._STL, count=n, offset=84)["v"].astype(np.float64)
        k = int((pick == i).sum())
        t = tri[rng.integers(0, n, k)]
        b = rng.dirichlet((1.0, 1.0, 1.0), k)
        pts.append(np.einsum("kj,kjd->kd", b, t) + pose[:3])
    q = open_mesh_world_query(WORLD)
    d = q.closest_batch(np.concatenate(pts), 0.05)
    assert len(d) == 5000 and float(d.max()) <= DEVIATION_MAX_M, float(d.max())


# ------------------------------------------------------------------ d3-gz 容器：A1、解析后重力、自由落体实测


def _flat_spot() -> tuple[float, float, float]:
    """DSM 上 4 m 窗口起伏 < 0.05 m、距世界原点 150 m 内的最低处（与 test_gz_lockstep 同法）。"""
    meta = json.loads((WORLD / "geometry/terrain/dsm_2m.json").read_text())
    z = np.fromfile(WORLD / "geometry/terrain/dsm_2m.f32", "<f4").reshape(meta["height"], meta["width"]).astype(np.float64)
    w = np.lib.stride_tricks.sliding_window_view(z, (2, 2))
    relief = w.max(axis=(2, 3)) - w.min(axis=(2, 3))
    h, wd = relief.shape
    yy, xx = np.mgrid[0:h, 0:wd]
    c = meta["cellM"]
    ex, ny = meta["originXY"][0] + (xx + 1) * c, meta["originXY"][1] + (yy + 1) * c
    ok = (relief < 0.05) & (np.hypot(ex, ny) < 150.0)
    zc = w.max(axis=(2, 3))
    i = np.argmin(np.where(ok, zc, np.inf))
    return float(ex.flat[i]), float(ny.flat[i]), float(zc.flat[i])


@needs_crop
@needs_gz
def test_crop_sample_run_sdf_check_and_parsed_gravity(crop, tmp_path: Path) -> None:
    """A1：world_static.sdf（导出时）与样例 run.sdf（3 机 + world_static）`gz sdf -k` 退出码 0；`gz sdf -p` 解析结果的重力为 9.80665。"""
    assert crop.manifest["qa"]["A1_sdf_check"] == "pass"
    ok, msg = sdf_check(crop.model_dir / f"{MODEL}.sdf")
    assert ok, msg
    x, y, zg = _flat_spot()
    spawns = [UavSpawn(f"uav-0{i + 1}", (x + 3.0 * i, y, zg + 20.0)) for i in range(3)]
    run = tmp_path / "run.sdf"
    run.write_text(compose_run(spawns, world_static_uri=crop.uri, spherical=crop.spherical), encoding="utf-8")
    ok, msg = sdf_check(run, resource_paths=(crop.root,))
    assert ok, msg
    env = dict(os.environ)
    env["SDF_PATH"] = ":".join(p for p in (env.get("SDF_PATH", ""), env.get("GZ_SIM_RESOURCE_PATH", ""), str(crop.root.resolve())) if p)
    assert "Error" not in msg                                                     # 无未解析的 model://（缺模型时 -k 也报错）
    r = subprocess.run(["gz", "sdf", "-p", str(run)], capture_output=True, text=True, check=False, env=env)
    assert r.returncode == 0, r.stderr[-400:]
    assert _gravity(r.stdout[r.stdout.index("<sdf"):]) == [0.0, 0.0, -9.80665]


@needs_crop
@pytest.mark.skipif(not (HAS_GZ and HAS_PLUGIN), reason="needs gz-sim with libDrone4DGz.so (d3-gz image)")
def test_crop_free_fall_measures_standard_gravity(crop, tmp_path: Path) -> None:
    """锁步下 crop 平坦处上方 40 m 断电自由落体：电机停转后（断电 0.3 s 起）逐物理步速度差分的竖直加速度与 −9.80665 之差 < 1e-6，
    水平加速度与机体比力 < 1e-6；随后在世界碰撞网格上记 IMPACT（碰撞 STL 生效）。"""
    pytest.importorskip("zmq")
    from awr.phys.adapter import TierCommand, VehicleStatus
    from awr.phys.gz.backend import GzSimBackend
    from awr.phys.gz.instance import GzInstance
    from awr.phys.pose_history import COLS

    x, y, zg = _flat_spot()
    inst = GzInstance(tmp_path / "run", "local", launcher="local", world_export=crop.root)
    inst.start(compose_run([UavSpawn("uav-01", (x, y, zg + 40.0))], world_static_uri=crop.uri, endpoint=inst.gz_endpoint,
                           spherical=crop.spherical))
    ev, ts, ss = [], [], []
    kill_k = 3
    try:
        inst.wait_socket(120)
        be = GzSimBackend(["uav-01"], inst.endpoint, grounded=[])
        be.open()
        for k in range(45):
            be.submit([TierCommand("uav-01", "kill", k)] if k == kill_k else [])
            r = be.advance(k)
            ev += r.contacts
            sl = be.history.slice("uav-01", r.t_start_ns + 1, r.t_end_ns)
            ts.append(sl.t_ns[1:])
            ss.append(sl.state[1:])
        st = be.status()["uav-01"]
        be.close()
    finally:
        inst.stop()
    t, s = np.concatenate(ts), np.concatenate(ss)
    t_kill = ts[kill_k][-1] if len(ts[kill_k]) else t[25 * kill_k]
    impact = next((e for e in ev if e.kind == "IMPACT"), None)
    assert impact is not None and st == VehicleStatus.CRASHED
    t_imp = int(impact.t_ns)
    win = (t >= t_kill + 300_000_000) & (t <= t_imp - 200_000_000)               # 电机停转后、触地前
    assert win.sum() >= 250                                                       # ≥ 1 s 自由落体
    v = s[win][:, COLS["vel"]]
    dt = np.diff(t[win]) * 1e-9
    a = np.diff(v, axis=0) / dt[:, None]
    assert np.max(np.abs(a[:, 2] + 9.80665)) < 1e-6 and np.max(np.abs(a[:, :2])) < 1e-6   # dmax 实测 1.9e-13、2.4e-19
    assert np.max(np.abs(s[win][:, COLS["f_b"]])) < 1e-6                           # 自由落体：比力 ≈ 0
