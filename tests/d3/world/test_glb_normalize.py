"""M22 世界帧合成与配准记录（§6.6.3、§6.6.5、§7.6.2；FR-001、FR-002）：线性部分、原点规则、共帧继承、原始资产校验与记录状态门。"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from glbfix import write_glb

from awr.world.glb.normalize import R_ENU_GLTF, frame_of_relative, level_matrix, linear_part, synthesize
from awr.world.glb.reader import load_scene
from awr.world.glb.recipe import file_sha256, load_registration, verify_raw
from awr.world.ingest.types import ConfigError, RawDataError

ROOT = Path(__file__).resolve().parents[3]


def _grid(n: int = 41, size: float = 100.0) -> np.ndarray:
    """glTF Y-up 场景帧中的平地点阵（y = 0）加一根 10 单位高的柱。"""
    x, z = np.meshgrid(np.linspace(-size / 2, size / 2, n), np.linspace(-size / 2, size / 2, n))
    G = np.stack([x.ravel(), np.zeros(x.size), z.ravel()], 1)
    return np.vstack([G, [[0.0, 10.0, 0.0]]])


def test_linear_part_maps_gltf_to_enu_with_scale_and_yaw() -> None:
    A, R, t = linear_part(2.0, 90.0, (0.0, 0.0))
    assert level_matrix((0.0, 0.0)) == R and t == (0.0, 0.0, 0.0)
    v = np.array([1.0, 3.0, -1.0])                                  # glTF：x = 1，上 3，z = −1（北 1）
    enu = np.array(R_ENU_GLTF) @ v * 2.0                             # (2, 2, 6)
    rz = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1.0]])
    np.testing.assert_allclose(np.array(A) @ v, rz @ enu, atol=1e-12)


def test_origin_is_bbox_centre_and_dtm_median() -> None:
    V = _grid() + np.array([500.0, 7.0, 0.0])                        # 地面高 7 单位
    wf = synthesize(V, s=1.0, yaw_deg=0.0, g=(0.0, 0.0), t_osm_m=(100.0, -50.0), osm_ref=(43.7, 23.9))
    W = wf.apply(V)
    np.testing.assert_allclose(W[:-1, 2], 0.0, atol=1e-9)           # 平地落在 z = 0
    np.testing.assert_allclose((W[:, :2].min(0) + W[:, :2].max(0)) / 2, 0.0, atol=1e-9)
    assert math.isclose(wf.origin_osm_m[0], 600.0) and math.isclose(wf.origin_osm_m[1], -50.0)
    assert wf.lat_deg < 43.7 and wf.lon_deg > 23.9 and wf.leveled_deg == 0.0                  # 原点在参照点东南（E 600、N −50）
    T = np.array(wf.T_world_source)
    np.testing.assert_allclose(V @ T[:3, :3].T + T[:3, 3], W, atol=1e-9)


def test_levelling_flattens_a_tilted_ground() -> None:
    V = _grid()[:-1]
    gE, gN = 0.02, -0.01
    E, N = V[:, 0], -V[:, 2]
    V[:, 1] = -(gE * E + gN * N)                                     # 模型地面 z = −g·(E, N)，法线 (gE, gN, 1)
    wf = synthesize(V, s=1.0, yaw_deg=0.0, g=(gE, gN), t_osm_m=(0.0, 0.0), osm_ref=(0.0, 0.0))
    assert np.ptp(wf.apply(V)[:, 2]) < 1e-9 and math.isclose(wf.leveled_deg, math.degrees(math.atan(math.hypot(gE, gN))))


def test_inherit_frame_maps_shared_obj_points_identically(tmp_path: Path) -> None:
    """两份 GLB 共用 OBJ 局部帧、根节点矩阵不同：同一 OBJ 点经各自世界帧（相同配准量）落在同一 OSM 位置。"""
    q = [0.0, 0.0, math.sin(0.05), math.cos(0.05)]
    ref = load_scene(write_glb(tmp_path / "town.glb", nodes=[{"mesh": 0, "scale": [2.5, 2.5, 2.5], "rotation": q, "translation": [3, 0, 1]}]))
    sc = load_scene(write_glb(tmp_path / "crop.glb", nodes=[{"mesh": 0, "translation": [-4, 1, 2]}]))
    rel = frame_of_relative(sc, ref)
    reg = {"s": 8.0, "yaw_deg": 168.0, "g": (0.001, -0.02), "t_osm_m": (-800.0, -1200.0)}
    A_ref, _, t_ref = linear_part(reg["s"], reg["yaw_deg"], reg["g"])
    A, _, t = linear_part(reg["s"], reg["yaw_deg"], reg["g"], rel)
    np.testing.assert_allclose(np.array(A) @ sc.prims[0].pos.T + np.array(t)[:, None], np.array(A_ref) @ ref.prims[0].pos.T + np.array(t_ref)[:, None],
                               atol=1e-9)


def test_verify_raw_and_registration_gate(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "w").mkdir(parents=True)
    f = raw / "w" / "a.glb"
    f.write_bytes(b"x" * 10)
    man = tmp_path / "raw-manifest.json"
    man.write_text(json.dumps({"files": [{"name": "w/a.glb", "bytes": 10, "sha256": file_sha256(f)}]}))
    assert verify_raw("w/a.glb", raw_dir=raw, manifest=man) == f
    with pytest.raises(RawDataError) as e:
        verify_raw("w/missing.glb", raw_dir=raw, manifest=man)
    assert e.value.exit_code == 4 and e.value.error_code == "RAW_MISSING"
    f.write_bytes(b"y" * 10)
    with pytest.raises(RawDataError, match="sha256"):
        verify_raw("w/a.glb", raw_dir=raw, manifest=man)
    reg = tmp_path / "registration.json"
    reg.write_text(json.dumps({"schema": "awr.glb.registration.v1", "method": "manual", "status": "draft"}))
    with pytest.raises(ConfigError, match="accepted"):
        load_registration(reg)
    assert load_registration(reg, require_accepted=False)["status"] == "draft"


def test_committed_registrations_match_m22_expectations() -> None:
    """configs/worlds.d3 的配准记录与 M22 §8.1 预期一致（尺度 ±0.5%、锚点、包围盒）。"""
    town = load_registration(ROOT / "configs/worlds.d3/smalltown-01/registration.json")["result"]
    crop_doc = load_registration(ROOT / "configs/worlds.d3/oryahovo-crop1/registration.json")
    crop = crop_doc["result"]
    assert abs(town["m_per_unit"] / 8.374 - 1) < 0.005 and abs(crop["m_per_unit"] / 20.935 - 1) < 0.005
    assert crop_doc["method"] == "inherit-frame" and crop_doc["frame_of"]["world_id"] == "smalltown-01"
    for r, (lat, lon), (bx, by) in ((town, (43.731199, 23.962713), (2287, 1393)), (crop, (43.7352474, 23.9612838), (1256, 850))):
        assert abs(r["anchor"]["lat_deg"] - lat) < 2e-5 and abs(r["anchor"]["lon_deg"] - lon) < 2e-5        # ≈ 2 m
        assert abs(r["bounds_m"]["max"][0] - bx) < 3 and abs(r["bounds_m"]["max"][1] - by) < 3
