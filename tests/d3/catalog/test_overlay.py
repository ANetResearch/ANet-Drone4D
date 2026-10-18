"""D3 目录覆盖层 `vehicles/d3/` 与建图机型变体 `p600_mid360_inv`（AWR-20 §15.3、§15.5.1、§15.6.3、§15.6.5、§15.7、§15.8；ADR-157；
CAT-AC-019、CAT-AC-020、CAT-AC-026、CAT-AC-027、CAT-AC-030；D3-AC-13、D3-AC-32、D3-AC-01）。

- 加载规则：覆盖层只在 D3 进程中读取（`AWR_D3_ENABLED = 1` 或 `Catalog.load(d3=True)`）；D2 目录、机型文档、检查结果与
  REST 视图在读与不读覆盖层时逐字节一致，D2 文件不被改写；
- `p600_mid360_inv`：§15.5.1 派生值（≤ 1%）、倒装 MID-360 挂载与 FAST-LIVO2 `extrinsic_T/R` 一致、`slam_cam` 外参复现 r1 闭环
  配置的 `Rcl/Pcl`、MID-360 内置 IMU 噪声字段与 §15.6.5 逐项一致、D3 rig 与 §15.3 逐行一致；
- 校验规则 OV-1 至 OV-3、SD-1、LR-1、LR-2、LR-4、CM-1、RG-1、RG-2 的负向用例（在临时目录的副本上修改）；
- 覆盖层可承载自带 D3 字段的 LiDAR 型号（D3-AC-32 长量程型号的接入点）。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import shutil
import time
from pathlib import Path

import pytest

from awr.sim.catalog import loader as L
from awr.sim.catalog.loader import Catalog, CatalogError, Overlay, imu_noise_tier, rpy_matrix
from awr.sim.fleet import profiles as PF

ROOT = Path(__file__).resolve().parents[3]
VEH = ROOT / "vehicles"
VID = "p600_mid360_inv"

# r1 闭环（.cache/research/d3/r1-fast-livo2/results/runs/*/livo.yaml，sim_livo_bag.py 生成）的 FAST-LIVO2 外参
R1_EXTRINSIC_T = [-0.011, -0.02329, 0.04412]
R1_EXTRINSIC_R = [1, 0, 0, 0, 1, 0, 0, 0, 1]
R1_RCL = [0.0, 1.0, 0.0, -0.70710678, 0.0, 0.70710678, 0.70710678, 0.0, 0.70710678]
R1_PCL = [0.0, 0.08485281, -0.08485281]
R_FLU_RDF = ((0.0, 0.0, 1.0), (-1.0, 0.0, 0.0), (0.0, -1.0, 0.0))  # 列：光学 x(右)、y(下)、z(前) 在 FLU 中


def _rel(want: float, got: float) -> float:
    return abs(got - want) / abs(want)


def _mm(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def _mt(a):
    return tuple(tuple(a[j][i] for j in range(3)) for i in range(3))


def _mv(a, v):
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))


def _tree_sha(d: Path, skip: str | None = None) -> dict[str, str]:
    out = {}
    for p in sorted(d.rglob("*")):
        if p.is_file() and (skip is None or skip not in p.relative_to(d).parts):
            out[str(p.relative_to(d))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@pytest.fixture(scope="module")
def cat() -> Catalog:
    return Catalog.load(VEH, d3=True)


@pytest.fixture(scope="module")
def ov(cat: Catalog) -> Overlay:
    assert cat.overlay is not None
    return cat.overlay


@pytest.fixture
def vcopy(tmp_path: Path) -> Path:
    """机型目录副本（负向用例在副本上修改覆盖层文件）。"""
    d = tmp_path / "vehicles"
    shutil.copytree(VEH, d, ignore=shutil.ignore_patterns("*.glb", "__pycache__"))
    return d


def _edit_json(p: Path, fn) -> None:
    doc = json.loads(p.read_text(encoding="utf-8"))
    fn(doc)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _edit_yaml(p: Path, fn) -> None:
    import yaml

    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    fn(doc)
    p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _rules(vdir: Path) -> set[str]:
    return {p["rule"] for p in Overlay.load(Catalog.load(vdir, d3=False)).problems()}


# ---------------------------------------------------------------- 加载规则与 D2 零影响（CAT-AC-019、D3-AC-01）
def test_d2_loader_does_not_read_overlay(monkeypatch):
    monkeypatch.delenv("AWR_D3_ENABLED", raising=False)
    c = Catalog.load(VEH)
    assert c.overlay is None
    assert VID not in c.docs and VID not in PF.load_vehicle_docs(VEH)
    assert not list((VEH / "d3").rglob("params.yaml"))  # D1 回退扫描 vehicles/*/params.yaml 也读不到覆盖层
    assert VID not in PF.ProfileTable(vehicles=VEH).profiles
    monkeypatch.setenv("AWR_D3_ENABLED", "0")
    assert Catalog.load(VEH).overlay is None


def test_d3_process_reads_overlay(monkeypatch):
    monkeypatch.setenv("AWR_D3_ENABLED", "1")
    c = Catalog.load(VEH)
    assert c.overlay is not None and VID in c.overlay.variants
    assert c.overlay.visible_ids() == [VID]
    monkeypatch.delenv("AWR_D3_ENABLED")
    assert Catalog.load(VEH, d3=True).overlay is not None
    monkeypatch.setenv("AWR_D3_ENABLED", "1")
    assert Catalog.load(VEH, d3=False).overlay is None
    # D3 进程中 M08 的机型表仍只含 D2 目录（变体只经覆盖层可见）
    assert VID not in PF.ProfileTable(vehicles=VEH).profiles


def test_d2_catalog_unchanged_when_overlay_loaded(cat):
    from awr.sim.catalog.view import VIEW_PATH, build_view, canonical_bytes

    before = _tree_sha(VEH, skip="d3")
    d2 = Catalog.load(VEH, d3=False)
    assert cat.doc == d2.doc and cat.version == d2.version
    assert sorted(cat.docs) == sorted(d2.docs)
    assert all(cat.docs[k][0] == d2.docs[k][0] for k in d2.docs)
    assert {k: v[0] for k, v in cat.sensor_docs.items()} == {k: v[0] for k, v in d2.sensor_docs.items()}
    assert cat.problems() == d2.problems() == []
    v3, v2 = canonical_bytes(build_view(cat)), canonical_bytes(build_view(d2))
    assert v3 == v2
    assert VIEW_PATH.read_bytes() == v2 + b"\n"  # 公开沙盒目录响应与 D2 基线逐字节一致
    Overlay.load(d2).problems()
    assert _tree_sha(VEH, skip="d3") == before  # 读取与校验覆盖层不改写任何 D2 文件
    assert cat.docs["p600_mid360"][0]["mass_kg"]["value"] == 3.825  # 变体合并不改基机型文档


def test_overlay_checks_pass_and_load_time(cat, ov):
    assert ov.problems() == []
    assert ov.doc["base_catalog_version"] == cat.version
    best = min(_timed(cat) for _ in range(3))
    assert best <= 0.100, f"覆盖层加载与全部检查 {best * 1000:.1f} ms > 100 ms（CAT-NFR-006）"


def _timed(cat: Catalog) -> float:
    t0 = time.perf_counter()
    Overlay.load(cat).require_ok()
    return time.perf_counter() - t0


def test_overlay_is_deterministic(cat):
    a, b = Overlay.load(cat).variant(VID), Overlay.load(cat).variant(VID)
    ja = json.dumps([a.doc, a.rig_rows(), [s.mount() for s in a.rig], a.derived], sort_keys=True, ensure_ascii=False)
    jb = json.dumps([b.doc, b.rig_rows(), [s.mount() for s in b.rig], b.derived], sort_keys=True, ensure_ascii=False)
    assert ja == jb


# ---------------------------------------------------------------- p600_mid360_inv（CAT-AC-027；§15.5.1）
def test_variant_derived_values(ov):
    v = ov.variant(VID)
    d = v.derived
    assert v.base_ref == "p600_mid360@2.0.0" and v.base_dir.name == "p600"
    assert v.delta == {"dm_kg": pytest.approx(0.06), "dp_w": pytest.approx(1.5)}
    for key, want in (("mass_kg", 3.885), ("p_hover_w", 412.0), ("hover_endurance_s", 1693.5), ("hover_thrust", 0.495),
                      ("twr", 2.02), ("mtow_margin_kg", 0.215)):
        assert _rel(want, d[key]) <= 0.01, (key, d[key])
    assert abs(d["mass_kg"] - 3.885) < 1e-9 and abs(d["p_hover_w"] - 412.0) < 0.1 and abs(d["hover_endurance_s"] - 1693.5) < 0.5
    doc = v.doc
    assert doc["id"] == VID and doc["profile_version"] == "1.0.0" and doc["catalog"]["reference"] is False
    assert PF.validate_doc(doc, pid=VID, vdir=v.base_dir)[2] == []  # schema 1.1、VH-1 至 VH-7、MNT-1 均通过
    assert "slam_cam" not in doc["sensors"]  # D3 新增槽位只进 D3 rig
    assert v.tags == ["mapping_default", "slam_rig"]


def test_inverted_mid360_matches_fastlivo2_extrinsics(ov):
    v = ov.variant(VID)
    lid = v.sensor("lidar0")
    assert lid.mount_preset == "inverted_mapping" and lid.model_ref == "mid360@2.0.0"
    assert lid.xyz_m == (0.0, 0.0, -0.08) and lid.rpy_deg == (180.0, 0.0, 0.0)
    import yaml

    presets = yaml.safe_load((VEH / "p600" / "sensors" / "mid360.yaml").read_text(encoding="utf-8"))["mount_presets"]
    assert list(lid.xyz_m) == presets["inverted_mapping"]["xyz_m"] and list(lid.rpy_deg) == presets["inverted_mapping"]["rpy_deg"]
    ex = v.lidar_imu_extrinsics("lidar0")
    assert ex["extrinsic_T"] == R1_EXTRINSIC_T and ex["extrinsic_R"] == R1_EXTRINSIC_R
    d2_mid360 = ov.catalog.sensor_docs["mid360"][0]
    assert ex["extrinsic_T"] == d2_mid360["imu"]["lidar_in_imu"]["t"]
    # T_B_L = T_B_I · T_I_L（内置 IMU 随 LiDAR 倒装）
    R_BI, t_BI = ex["T_B_I"]["R"], ex["T_B_I"]["t"]
    R_IL = ((1.0, 0, 0), (0, 1.0, 0), (0, 0, 1.0))
    t_BL = tuple(_mv(R_BI, ex["extrinsic_T"])[i] + t_BI[i] for i in range(3))
    assert _mm(R_BI, R_IL) == tuple(tuple(r) for r in ex["T_B_L"]["R"])
    assert all(abs(t_BL[i] - lid.xyz_m[i]) < 1e-12 for i in range(3))
    imu = v.sensor("lidar0_imu")
    assert imu.parent == "lidar0" and list(imu.xyz_m) == t_BI and imu.rpy_deg == (180.0, 0.0, 0.0)
    # slam_cam 与倒装 LiDAR 的相对外参复现 r1 的 Rcl、Pcl（FAST-LIVO2 extrin_calib）
    cam = v.sensor("slam_cam")
    R_BC = _mm(cam.R, R_FLU_RDF)
    R_CL = _mm(_mt(R_BC), lid.R)
    P_CL = _mv(_mt(R_BC), tuple(lid.xyz_m[i] - cam.xyz_m[i] for i in range(3)))
    assert all(abs(a - b) < 1e-8 for a, b in zip([x for r in R_CL for x in r], R1_RCL, strict=True))
    assert all(abs(a - b) < 1e-8 for a, b in zip(P_CL, R1_PCL, strict=True))


def test_rig_matches_section_15_3(ov):
    v = ov.variant(VID)
    rows = [(s.sensor_id, s.slot, s.kind, s.model_ref, s.roles, s.default, s.agent_delivered) for s in v.rig]
    assert rows == [
        ("cam0", "eo", "camera", "eo_gx40@1.0.0", ("agent",), True, True),
        ("slam_cam", "slam_cam", "camera", "gs_cam640@1.0.0", ("slam",), True, False),
        ("lidar0", "mid360", "lidar", "mid360@2.0.0", ("agent",), True, True),
        ("lidar0_imu", None, "imu", "imu_icm40609_livox@1.0.0", ("agent", "slam"), True, True),
        ("imu0", "imu", "imu", None, ("agent",), True, True),
        ("gnss0", "gnss", "gnss", None, ("agent",), True, True),
        ("nav", None, "nav", None, ("agent",), True, True),
    ]
    assert [s.sensor_no for s in v.rig] == list(range(7)) and [s.sensor_id for s in v.rig] == v.rig_order
    assert {s.sensor_id: s.rate_hz for s in v.rig} == {"cam0": 10.0, "slam_cam": 10.0, "lidar0": 10.0, "lidar0_imu": 200.0,
                                                        "imu0": 200.0, "gnss0": 10.0, "nav": 10.0}
    assert [s.sensor_id for s in v.rig if s.controllable] == ["cam0"]
    assert v.sensor("slam_cam").gimbal is False and v.sensor("cam0").gimbal is True
    assert v.rig_rows()[1] == {"sensor_id": "slam_cam", "slot": "slam_cam", "kind": "camera", "model_ref": "gs_cam640@1.0.0",
                               "role": "slam", "default": True, "rate_hz": 10.0, "controllable": False}
    with pytest.raises(KeyError):
        v.sensor("therm0")


def test_slam_cam_model(ov):
    sd = ov.sensor_model("gs_cam640@1.0.0")[0]
    assert sd["sensor"]["shutter"] == "global" and sd["exposure"] == {"mode": "fixed", "s": 0.008}
    k = sd["intrinsics"]["vga512"]
    assert (k["fx"], k["fy"], k["cx"], k["cy"]) == (381.36, 381.36, 320.0, 256.0)
    fx = sd["optics"]["f_mm"] / (sd["sensor"]["pixel_um"] * 2 * 1e-3)
    assert abs(fx - 381.36) < 0.01
    assert abs(math.degrees(2 * math.atan(320 / fx)) - 80.0) < 0.01
    assert abs(math.degrees(2 * math.atan(256 / fx)) - 67.7) < 0.05
    assert sd["role_allowed"] == ["slam"] and sd["mass_kg"]["value"] == 0.06 and sd["power_w"]["value"] == 1.5
    cam = ov.variant(VID).sensor("slam_cam")
    assert cam.xyz_m == (0.12, 0.0, -0.08) and cam.rpy_deg == (0.0, 45.0, 0.0)


# ---------------------------------------------------------------- MID-360 内置 IMU 与侧表（CAT-AC-024、CAT-AC-026；D3-AC-13）
def test_builtin_imu_noise_fields(ov, cat):
    imu = ov.sensor_model("imu_icm40609_livox@1.0.0")[0]
    assert imu["schema"] == "awr.sensor.imu_mems.v1" and imu["chip"] == "icm40609"
    assert imu["rate_hz"] == 200 and imu["acc_unit_export"] == "g" and imu["aa_cutoff_hz"] == 80
    assert imu["range"] == {"acc_g": 32, "gyro_dps": 2000}
    # §15.6.5 噪声档表（MID-360 内置 IMU 行）
    table = {"clean": (0, 0, 0, 0, 0, 0, 0.0), "realistic": (1e-3, 7e-4, 1e-5, 1e-5, 0.196, 0.5, 0.0),
             "harsh": (1e-3, 7e-4, 1e-5, 1e-5, 0.196, 0.5, 1.5)}
    bpf = float(cat.docs["p600_mid360"][0]["acoustic"]["bpf_hz"])
    assert bpf == 150.0
    for tier, want in table.items():
        n = imu_noise_tier(imu, tier, bpf_hz=bpf)
        got = (n["acc_sigma_sample_g"], n["gyro_sigma_sample_rps"], n["acc_bias_rw_g_rts"], n["gyro_bias_rw_rps_rts"],
               n["turn_on_acc_mps2"], n["turn_on_gyro_dps"], n["vib_amp_mps2"])
        assert got == want, tier
        assert n["vib_hz"] == (bpf if tier == "harsh" else None)
        assert n["rate_hz"] == 200 and n["acc_unit_export"] == "g"
    with pytest.raises(ValueError):
        imu_noise_tier(imu, "harsh")
    with pytest.raises(ValueError):
        imu_noise_tier(imu, "extreme", bpf_hz=bpf)


def test_mid360_side_table(ov, cat):
    d = ov.lidar_d3("mid360@2.0.0")
    assert d is not None and d["side_table"]["schema"] == "awr.sensor.lidar_d3.v1"
    pat = d["pattern"]
    assert pat["slots_per_frame"] == 20000 == pat["point_rate_hz"] // pat["frame_rate_hz"]
    assert pat["offset_time_rule"] == L.OFFSET_TIME_RULE and d["return_mode"] == "first"
    n = pat["slots_per_frame"]
    assert all(i * 10**8 // n == i * 5000 for i in range(n))  # offset_time(i) = ⌊i·10⁸/N⌋ = i·5000 ns
    assert (n - 1) * 10**8 // n < 10**8
    assert d["imu_ref"] == "imu_icm40609_livox@1.0.0" and d["livo_profile"] == "mid360_r1_v1"
    d2 = cat.sensor_docs["mid360"][0]
    for k in ("fov", "range", "noise", "frame_rate_hz", "mass_kg", "dev_type"):
        assert d[k] == d2[k], k
    assert d2["pattern"]["mode"] == "non_repetitive"  # D2 型号文件不变；D3 花样模式只在侧表
    assert d["pattern"]["mode"] == "non_repetitive_table"


# ---------------------------------------------------------------- 负向用例（§15.8）
def test_ov1_version_binding_rejects_d3_start(vcopy):
    _edit_json(vcopy / "d3" / "catalog.d3.json", lambda d: d.update(base_catalog_version="9.9.9"))
    assert "OV-1" in _rules(vcopy)
    with pytest.raises(CatalogError) as ei:
        Catalog.load(vcopy, d3=True)
    assert ei.value.code == 353
    assert Catalog.load(vcopy, d3=False).overlay is None  # D2 进程不受影响


def test_ov1_shape_and_id_clash(vcopy):
    p = vcopy / "d3" / "catalog.d3.json"
    _edit_json(p, lambda d: d["sensor_models"].append(dict(d["sensor_models"][0], id="mid360")))
    assert "OV-1" in _rules(vcopy)
    _edit_json(p, lambda d: d.pop("variants"))
    with pytest.raises(CatalogError) as ei:
        Overlay.load(Catalog.load(vcopy, d3=False))
    assert ei.value.problems[0]["rule"] == "OV-1"


def test_ov1_entry_must_match_variant_file(vcopy):
    _edit_json(vcopy / "d3" / "catalog.d3.json", lambda d: d["variants"][0].update(profile_version="1.0.1", d3_tags=["slam_rig"]))
    probs = Overlay.load(Catalog.load(vcopy, d3=False)).problems()
    assert {p["path"] for p in probs} >= {f"variants[{VID}].profile_version", f"variants[{VID}].d3_tags"}


@pytest.mark.parametrize("edit, rule", [
    (lambda d: d.update(base="p600_mid360@1.0.0"), "OV-2"),
    (lambda d: d.update(set={"motor": {"n": 6}}), "OV-2"),
    (lambda d: d.update(set={"wind_rating_mps": 20.0}), "OV-2"),
    (lambda d: d.update(remove=["eo"]), "OV-2"),
    (lambda d: d["mounts"]["mid360"].update(mount_preset="nadir_fixed"), "OV-2"),
    (lambda d: d["derived"].update(mass_kg=3.95), "OV-3"),
    (lambda d: d.update(set={"mass_kg": 4.08}), "OV-3"),
    (lambda d: d["d3"].update(rig_order=["cam0", "slam_cam", "lidar0", "imu0", "gnss0", "nav"]), "RG-2"),
    (lambda d: d["mounts"]["gnss"].update(d3_sensor_id="GNSS"), "RG-2"),
])
def test_variant_rules(vcopy, edit, rule):
    _edit_yaml(vcopy / "d3" / "variants" / f"{VID}.yaml", edit)
    assert rule in _rules(vcopy)


def test_ov2_set_whitelist_applies(vcopy):
    _edit_yaml(vcopy / "d3" / "variants" / f"{VID}.yaml",
               lambda d: d.update(set={"labels": {"lidar": "MID-360 倒装"}, "capabilities": ["compute.orin_nx", "nav.slam3d"]}))
    ov = Overlay.load(Catalog.load(vcopy, d3=False))
    assert ov.problems() == []
    doc = ov.variant(VID).doc
    assert doc["labels"]["lidar"] == "MID-360 倒装" and doc["capabilities"] == ["compute.orin_nx", "nav.slam3d"]


def test_rg1_slam_rig_requires_global_shutter_camera(vcopy):
    def drop(d):
        d["mounts"].pop("slam_cam")
        d["d3"]["rig_order"].remove("slam_cam")
        d["derived"] = {}

    _edit_yaml(vcopy / "d3" / "variants" / f"{VID}.yaml", drop)
    assert _rules(vcopy) == {"RG-1"}


def test_cm1_and_rg1_on_camera_model(vcopy):
    p = vcopy / "d3" / "sensor_models" / "gs_cam640.yaml"
    _edit_yaml(p, lambda d: d["intrinsics"]["vga512"].update(fx=400.0))
    assert _rules(vcopy) == {"CM-1"}
    _edit_yaml(p, lambda d: (d["intrinsics"]["vga512"].update(fx=381.36), d["sensor"].update(shutter="rolling")))
    assert _rules(vcopy) == {"CM-1", "RG-1"}


@pytest.mark.parametrize("edit, rule", [
    (lambda d: d["fov"].update(v_max_deg=55.0), "SD-1"),
    (lambda d: d["lidar_in_imu"].update(t=[0.0, 0.0, 0.0]), "SD-1"),
    (lambda d: d["pattern"].update(slots_per_frame=20001), "LR-1"),
    (lambda d: d["pattern"].update(offset_time_rule="i*5000"), "LR-1"),
    (lambda d: d["range"].update(r_at_rho80_m=80.0), "LR-2"),
    (lambda d: d.update(imu_ref="imu_unknown@1.0.0"), "LR-4"),
])
def test_side_table_rules(vcopy, edit, rule):
    _edit_yaml(vcopy / "d3" / "side_tables" / "mid360@2.0.0.d3.yaml", edit)
    assert rule in _rules(vcopy)


def test_lr4_slam_imu_must_export_g(vcopy):
    _edit_yaml(vcopy / "d3" / "sensor_models" / "imu_icm40609_livox.yaml", lambda d: d.update(acc_unit_export="mps2"))
    assert _rules(vcopy) == {"LR-4"}


def test_overlay_lidar_model_with_d3_fields(vcopy):
    """覆盖层可登记自带 D3 字段的 LiDAR 型号（长量程型号的接入点，D3-AC-32）：LR-1、LR-2、LR-4 按型号执行。"""
    d3 = vcopy / "d3"
    model = {"schema": "awr.sensor.lidar_livox.v1", "model_id": "lr_test", "model_version": "1.0.0", "reference": True,
             "display_name": "AWR LR test", "pattern": {"mode": "rosette_fit", "beams": 6, "point_rate_hz": 240000,
                                                         "frame_rate_hz": 10, "slots_per_frame": 24000,
                                                         "offset_time_rule": L.OFFSET_TIME_RULE},
             "frame_rate_hz": 10, "range": {"blind_m": 1.0, "r_at_rho10_m": 190, "r_at_rho20_m": 230, "r_at_rho80_m": 320,
                                            "exponent": 0.251, "hard_max_m": 450},
             "imu_ref": "imu_icm40609_livox@1.0.0", "lidar_in_imu": {"t": [0.04165, 0.02326, -0.0284], "rpy_deg": [0, 0, 0]},
             "conf": "D", "src": "测试型号"}
    import yaml

    (d3 / "sensor_models" / "lr_test.yaml").write_text(yaml.safe_dump(model), encoding="utf-8")
    _edit_json(d3 / "catalog.d3.json", lambda d: d["sensor_models"].append(
        {"id": "lr_test", "file": "sensor_models/lr_test.yaml", "type": "lidar_livox", "model_version": "1.0.0",
         "reference": True, "visible": True}))
    ov = Overlay.load(Catalog.load(vcopy, d3=False))
    assert ov.problems() == []
    ld = ov.lidar_d3("lr_test@1.0.0")
    assert ld is not None and ld["pattern"]["slots_per_frame"] == 24000
    offs = [i * 10**8 // 24000 for i in range(24000)]
    assert {b - a for a, b in itertools.pairwise(offs)} == {4166, 4167} and offs[-1] < 10**8
    model["range"]["r_at_rho80_m"] = 360
    model["pattern"]["slots_per_frame"] = 20000
    (d3 / "sensor_models" / "lr_test.yaml").write_text(yaml.safe_dump(model), encoding="utf-8")
    assert _rules(vcopy) == {"LR-1", "LR-2"}


def test_rpy_matrix_matches_mount_convention():
    from awr.sim.sensors.frames import mount_R

    for rpy in ((180, 0, 0), (0, 45, 0), (10, -20, 30), (0, 90, 0)):
        a, b = rpy_matrix(rpy), mount_R(rpy)
        assert all(abs(a[i][j] - b[i][j]) < 1e-12 for i in range(3) for j in range(3))
