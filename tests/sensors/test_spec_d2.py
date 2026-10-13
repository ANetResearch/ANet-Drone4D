"""M13-AC-201：D2 规格加载（五种新结构与 mid360 v2、spec_ref 合并与 SN 规则、挂载组、describe）；M13-FR-201、202、212、215、216。"""

from __future__ import annotations

import copy
import math
from pathlib import Path

import pytest
import yaml

from awr.sim.sensors.enums import SensorKind
from awr.sim.sensors.gimbal import SensorError
from awr.sim.sensors.mounts import mount_summary, resolve_mounts
from awr.sim.sensors.spec import SensorSpecError, build_spec, load_rig, load_sensor_file, resolve_doc, vehicles_dir

P600 = vehicles_dir() / "p600" / "sensors"
LIB = Path(__file__).resolve().parent / "fixtures" / "sensor_models"


def ydoc(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 型号与挂载文件
@pytest.mark.parametrize("name", ["eo_gx40", "eo_z8", "nir_ill_l", "lwir640", "mmw77", "mic8", "mid360"])
def test_model_files_pass_schema(name):
    from awr.sim.sensors.spec import _read_model

    d = _read_model(LIB / f"{name}.yaml")
    assert d["model_id"] == name and d["conf"] in "ABCD" and d["src"]


def test_p600_eo_gx40_mount_resolves_to_spec():
    s = load_sensor_file(P600 / "eo_gx40.yaml", "eo", 5, model_dirs_=[LIB])
    z = s.zoom
    assert s.kind == SensorKind.CAMERA and s.spec_ref == "eo_gx40@1.0.0" and s.model_id == "eo_gx40"
    # 规格（AWR-04 §6.1）：HFOV 60.2°–6.6°、VFOV 36.1°–3.7°、DFOV 67.2°–7.6°，误差 ≤ 0.2°（SN-5、AC-202）
    for f, hf, vf, df in ((4.8, 60.2, 36.1, 67.2), (48.0, 6.6, 3.7, 7.6)):
        assert abs(math.degrees(z.hfov(f)) - hf) <= 0.2 and abs(math.degrees(z.vfov(f)) - vf) <= 0.2
        assert abs(math.degrees(z.dfov(f)) - df) <= 0.2
    assert z.fnum(4.8) == pytest.approx(1.7) and z.fnum(48.0) == pytest.approx(3.2)
    assert z.log_rate == pytest.approx(math.log(10) / 4.0)
    # 等效像元（AWR-20 §6.2）：4K 1.45、1080P 2.90、720P 4.35、SXGA 3.06、1.3M 3.26 µm
    assert [round(z.p_eff_um(n), 2) for n in ("4k", "1080p", "720p", "sxga", "1p3m")] == [1.45, 2.9, 4.35, 3.06, 3.26]
    # 衍射：550 nm、48 mm 时 4K 的 p' = λ·F# = 1.76 µm，1080P 不受约束
    assert z.p_prime_um(48.0, "4k") == pytest.approx(0.55 * 3.2) and z.p_prime_um(48.0) == pytest.approx(2.9)
    assert s.intr.w == 1920 and s.intr.fx == pytest.approx(4.8 / 2.9e-3)
    assert s.mass_kg == pytest.approx(0.4236) and s.power_w == pytest.approx(8.0)
    assert s.gimbal is not None and math.degrees(s.gimbal.az_max_rad) == pytest.approx(160.0)
    # 机内照明：广角 21.9 m、长焦 198.8 m（无消光，AWR-20 §6.3 表）
    assert s.illum is not None and s.illum.builtin and s.illum.follow_hfov
    assert s.illum.range_m(z.hfov(4.8)) == pytest.approx(21.9, abs=0.05)
    assert s.illum.range_m(z.hfov(48.0)) == pytest.approx(198.8, abs=0.05)


def test_nir_ill_l_and_mid360_v2():
    n = load_sensor_file(P600 / "nir_ill_l.yaml", "nir_ill", 6, model_dirs_=[LIB])
    assert n.kind == SensorKind.ILLUMINATOR and n.on_gimbal_of == "eo" and n.gimbal is None
    assert n.illum.range_m(n.illum.theta()) == pytest.approx(464.8, abs=0.1)
    assert n.mass_kg == pytest.approx(0.18) and n.power_w == pytest.approx(3.0)
    r0 = n.illum.range_m(n.illum.theta())
    assert n.illum.range_m(n.illum.theta(), rho=0.45) == pytest.approx(r0 * math.sqrt(0.45 / 0.3), rel=1e-12)
    # 人（rho 0.45）MOR 10 km：sigma_850 = ln 20 / MOR · (850/550)^−1.3（Kim，6–50 km）时 521 m，GX40 长焦 234 m（AWR-20 §6.3，±1%）
    sig = math.log(20.0) / 10000.0 * (850.0 / 550.0) ** -1.3
    assert n.illum.range_m(n.illum.theta(), rho=0.45, sigma_per_m=sig) == pytest.approx(521.0, rel=0.01)
    eo = load_sensor_file(P600 / "eo_gx40.yaml", "eo", 5, model_dirs_=[LIB])
    assert eo.illum.range_m(eo.zoom.hfov(48.0), rho=0.45, sigma_per_m=sig) == pytest.approx(234.0, rel=0.01)
    m = load_sensor_file(P600 / "mid360.yaml", "mid360", 2)
    assert m.range_m == 100.0 and m.mass_kg == pytest.approx(0.265) and m.power_w == pytest.approx(6.5)
    assert m.env_limits == (-20.0, 55.0)
    # 型号库的 mid360@2.0.0 与仓库文件除挂载外取值一致（型号库交付后改 spec_ref，AWR-20 §7.5）
    lib = ydoc(LIB / "mid360.yaml")
    rep = ydoc(P600 / "mid360.yaml")
    for k in ("range", "fov", "pattern", "noise", "frame_rate_hz", "imu"):
        assert lib[k] == rep[k], k


def test_lwir_lens_and_radar_fov(d2_vehicles):
    rig = load_rig(d2_vehicles / "tq9")
    lw = rig.by_name("lwir")
    assert lw.kind == SensorKind.THERMAL and lw.lens_mm == 100.0
    assert math.degrees(lw.hfov_rad) == pytest.approx(4.40, abs=0.01) and math.degrees(lw.vfov_rad) == pytest.approx(3.52, abs=0.01)
    assert lw.mass_kg == pytest.approx(0.48 + 0.15) and lw.zoom is None
    rd = rig.by_name("radar")
    assert rd.kind == SensorKind.RADAR and math.degrees(rd.hfov_rad) == pytest.approx(120.0) and rd.range_m == 400.0
    assert rig.by_name("mic").kind == SensorKind.ACOUSTIC


# ---------------------------------------------------------------- 拒绝（SN 规则、FR-201）
def _mount(**over) -> dict:
    d = ydoc(P600 / "eo_gx40.yaml")
    d.update(over)
    return d


def test_reject_unknown_spec_ref_version():
    with pytest.raises(SensorSpecError) as ei:
        build_spec("eo", _mount(spec_ref="eo_gx40@9.9.9"), 5, model_dirs_=[LIB])
    assert ei.value.code == 110 and any("SN-2" in p for p in ei.value.problems)


def test_reject_override_outside_whitelist():
    d = _mount(optics={"f_min_mm": 4.8, "f_max_mm": 60.0})
    with pytest.raises(SensorSpecError) as ei:
        build_spec("eo", d, 5, model_dirs_=[LIB])
    assert any("SN-4" in p and "optics" in p for p in ei.value.problems)
    # 与型号相同的值不算覆盖
    same = _mount(optics=copy.deepcopy(ydoc(LIB / "eo_gx40.yaml")["optics"]))
    assert build_spec("eo", same, 5, model_dirs_=[LIB]).zoom.f_max_mm == 48.0


def test_reject_inverted_focal_range_and_missing_conf(tmp_path):
    m = ydoc(LIB / "eo_gx40.yaml")
    m["optics"]["f_min_mm"], m["optics"]["f_max_mm"] = 48.0, 4.8
    with pytest.raises(SensorSpecError) as ei:
        build_spec("eo", m, 0)
    assert any("FOCAL_RANGE_INVERTED" in p for p in ei.value.problems)
    m2 = ydoc(LIB / "eo_gx40.yaml")
    del m2["conf"]
    with pytest.raises(SensorSpecError) as ei2:
        build_spec("eo", m2, 0)
    assert any("conf is required" in p for p in ei2.value.problems)


def test_reject_spec_fov_mismatch():
    m = ydoc(LIB / "eo_gx40.yaml")
    m["spec_fov"]["hfov_deg"] = [61.0, 6.6]
    with pytest.raises(SensorSpecError) as ei:
        build_spec("eo", m, 0)
    assert any("SN-5" in p for p in ei.value.problems)


def test_reject_on_gimbal_of_unknown_slot(tmp_path):
    (tmp_path / "sensors").mkdir()
    d = ydoc(P600 / "nir_ill_l.yaml")
    d["on_gimbal_of"] = "mid360"
    (tmp_path / "sensors" / "n.yaml").write_text(yaml.safe_dump(d), encoding="utf-8")
    (tmp_path / "sensors" / "m.yaml").write_text((P600 / "mid360.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(SensorSpecError) as ei:
        load_rig(tmp_path, {"mid360": {"file": "sensors/m.yaml", "default": True}, "nir_ill": {"file": "sensors/n.yaml"}},
                 model_dirs_=[LIB])
    assert any("SN-8" in p for p in ei.value.problems)


def test_resolve_doc_passthrough_for_d1_files():
    d = ydoc(P600 / "camera.yaml")
    assert resolve_doc(d) is d


# ---------------------------------------------------------------- 组与编号（FR-215、CAT-AC-017）
def test_p600_v2_rig_numbering(d2_vehicles):
    rig = load_rig(d2_vehicles / "p600")
    assert [(s.name, s.sensor_no) for s in rig.specs] == [("camera", 0), ("thermal", 1), ("mid360", 2), ("gnss", 3), ("imu", 4),
                                                         ("eo", 5), ("nir_ill", 6)]
    assert [s.slot.legacy for s in rig.specs] == [True, True, False, False, False, False, False]
    assert rig.d2 and [c.name for c in rig.col_specs] == ["camera", "thermal"]  # 完整组（D1 剧本）：camera 占列 0
    sub = rig.subset(rig.default_mask(sandbox=True))
    assert [s.name for s in sub.specs] == ["mid360", "gnss", "imu", "eo"]
    assert sub.col_specs[0].name == "eo" and sub.col_specs[1] is None and sub.col_of("eo") == 0
    full = rig.subset(rig.default_mask(sandbox=True) | (1 << 6))
    assert full.col_of("nir_ill") == 0 and [f.name for f in full.followers(0)] == ["nir_ill"]
    # D1 剧本缺省：D1 槽位加 default 槽位（nir_ill 可选，缺省不装）
    assert rig.default_mask(sandbox=False) == 0b0111111


def test_d1_rigs_unchanged():
    from awr.sim.sensors.spec import rig_for_model

    # 纯 D1 映射（x500）不变；P600 自 2.0.0 起为对象形式，D1 槽位编号不变（D2-INT）
    r = rig_for_model("x500")
    assert not r.d2 and [s.sensor_no for s in r.specs] == [0]
    p = rig_for_model("p600")
    assert p.d2 and [s.sensor_no for s in p.specs][:5] == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------- 挂载（FR-202）
def test_mounts_default_and_overrides(d2_vehicles):
    rig = load_rig(d2_vehicles / "p600")
    r = resolve_mounts(rig, None)
    assert r.mask == rig.default_mask(sandbox=True)
    r2 = resolve_mounts(rig, {"nir_ill": {"enabled": True}, "eo": {"enabled": True, "analysis": "1080p"}})
    assert r2.mask == r.mask | (1 << 6) and r2.analysis == {5: "1080p"}
    r3 = resolve_mounts(rig, [{"name": "mid360", "enabled": False}])
    assert not (r3.mask >> 2) & 1
    with pytest.raises(SensorError) as e1:
        resolve_mounts(rig, {"camera": {"enabled": True}})          # D1 槽位不能在沙盒实例中启用（MT-6）
    assert e1.value.code == 560
    with pytest.raises(SensorError) as e2:
        resolve_mounts(rig, {"radar": {"enabled": True}})           # 不存在的槽位（MT-1）
    assert e2.value.code == 560
    with pytest.raises(SensorError) as e3:
        resolve_mounts(rig, {"eo": {"enabled": False}})             # removable = false
    assert e3.value.code == 522 and e3.value.detail == "SLOT_NOT_REMOVABLE"
    with pytest.raises(SensorError) as e4:
        resolve_mounts(rig, {"eo": {"analysis": "4k"}}, [])         # 4K 需要 compute.orin_nx
    assert e4.value.code == 561
    assert resolve_mounts(rig, {"eo": {"analysis": "4k"}}, ["compute.orin_nx"]).analysis == {5: "4k"}
    with pytest.raises(SensorError) as e5:
        resolve_mounts(rig, [{"name": "eo", "rpy_deg": [0, 0, 10]}])
    assert e5.value.code == 561


def test_lwir_lens_override_and_follower_rule(d2_vehicles):
    rig = load_rig(d2_vehicles / "tq9")
    assert resolve_mounts(rig, {"lwir": {"lens_mm": 75}}).lens == {1: 75.0}
    with pytest.raises(SensorError) as e:
        resolve_mounts(rig, {"lwir": {"lens_mm": 50}})
    assert e.value.code == 561
    q1 = load_rig(d2_vehicles / "tq1")
    sub = q1.subset(resolve_mounts(q1, {"lwir": {"enabled": True}}).mask)
    assert sub.col_of("lwir") == 0 and sub.col_specs[1] is None  # 跟随 EO 云台的 LWIR 不占列


def test_mount_summary_mass_and_power(d2_vehicles):
    rig = load_rig(d2_vehicles / "p600")
    s0 = mount_summary(rig, rig.default_mask(sandbox=True))
    assert s0["mass_kg"] == pytest.approx(0.4236 + 0.265) and s0["power_w"] == pytest.approx(8.0 + 6.5)
    assert set(s0["mass_missing"]) == {"gnss", "imu"}  # D1 导航文件没有质量（计入机型 mass_kg）
    s1 = mount_summary(rig, rig.default_mask(sandbox=True) | (1 << 6))
    assert s1["mass_kg"] == pytest.approx(0.4236 + 0.265 + 0.18) and s1["power_w"] == pytest.approx(17.5)
    assert {r["slot"]: r["mounted"] for r in s1["slots"]}["nir_ill"] is True


# ---------------------------------------------------------------- describe（FR-216）
def test_describe_d2_fields(d2_vehicles):
    from awr.sim.sensors.describe import describe_model

    d = describe_model(d2_vehicles / "p600")
    eo = d["eo"]
    assert eo["spec_ref"] == "eo_gx40@1.0.0" and eo["slot"] == {"legacy": False, "default": True, "removable": False}
    assert eo["zoom"]["hfov_deg"] == [pytest.approx(60.227, abs=1e-3), pytest.approx(6.639, abs=1e-3)]
    assert eo["illuminator"]["range_m"] == [pytest.approx(21.92, abs=0.01), pytest.approx(198.83, abs=0.01)]
    assert d["nir_ill"]["illuminator"]["range_m"] == [pytest.approx(464.76, abs=0.01)]
    assert d["mid360"]["lidar_range"]["hard_max_m"] == 100.0
    assert d["mid360"]["lidar_range"]["r_max_m"]["0.2"] == pytest.approx(48.2, abs=0.05)
    assert d["mid360"]["lidar_range"]["density_pts_sr"] == pytest.approx(3500, rel=0.01)
    assert d["mounts"]["mass_kg"] == pytest.approx(0.6886)
    assert d["camera"]["slot"]["legacy"] is True
    q9 = describe_model(d2_vehicles / "tq9")
    rd = q9["radar"]["radar"]["r_detect_m"]
    assert rd["m2_1"] == pytest.approx(180.0) and rd["person"] == pytest.approx(151.4, abs=0.1)
    assert rd["car"] == pytest.approx(320.1, abs=0.1)
    assert [x["hfov_deg"] for x in q9["lwir"]["lenses"]] == [pytest.approx(v, abs=0.06) for v in (32.9, 17.5, 5.86, 4.40)]


def test_validate_profiles_reports_bad_rigs(d2_vehicles, tmp_path):
    """M13-FR-201：传感器文件无效即拒绝该机型（M08 在加载时调用 validate_profiles，非空以 353 拒绝）。"""
    import shutil

    from awr.sim.fleet.profiles import ProfileTable
    from awr.sim.sensors.runtime import SensorRuntime

    assert SensorRuntime.validate_profiles(ProfileTable(vehicles=d2_vehicles)) == {}
    v = tmp_path / "vehicles"
    shutil.copytree(d2_vehicles, v)
    d = ydoc(v / "p600" / "sensors" / "eo_gx40.yaml")
    d["spec_ref"] = "eo_gx40@2.0.0"
    (v / "p600" / "sensors" / "eo_gx40.yaml").write_text(yaml.safe_dump(d), encoding="utf-8")
    bad = SensorRuntime.validate_profiles(ProfileTable(vehicles=v))
    assert list(bad) == ["p600_mid360"] and any("SN-2" in x for x in bad["p600_mid360"])
