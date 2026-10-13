"""传感器型号库（CAT-AC-006 至 CAT-AC-009、CAT-AC-013；CAT-FR-008 至 FR-013；AWR-20 §6、§7.5；ADR-101）。

- GX40 在 4.8 mm 与 48 mm 的 HFOV、VFOV、DFOV 与规格误差 ≤ 0.2°；5 个输出档的等效像元与 §6.2 一致；4K 在 f > 35.8 mm 时
  p' = λ·F#（550 nm），48 mm 时 1.76 µm；850 nm 时 1080P 不受衍射约束；EO-Z8 两端视场与 F 数（CAT-AC-006）；
- LWIR 四种镜头视场 ≤ 0.1°、IFOV 与 100 m 处 GSD；`R_nir`：GX40 广角 21.9 m、长焦 198.8 m、nir_ill_l 464.8 m（无消光，±1%），人
  （rho 0.45）MOR 10 km 时 234 m、521 m，MOR 3 km 时 212 m、430 m（±1%，Kim 模型、K_MOR = ln 20）（CAT-AC-007）；
- 雷达 `R_D(sigma) = R_ref·sigma^(1/4)` 与 §6.9 一致（±1%），SNR 符合 R⁻⁴ 律，R 级 0.676·R_D；声阵列 P600 悬停自噪声带值与 §6.6 一致
  （±0.1 dB）（CAT-AC-008）；
- MID-360 `R_max(rho)` 表与角密度 3498 点/sr（±1%）、`hard_max_m = 100`（CAT-AC-009）；
- 挂载文件覆盖非白名单字段（optics.f_max_mm）时 SN-4 失败；`spec_ref` 版本不符时 SN-2 失败；型号文件均通过类型 schema（SN-1）
  （CAT-AC-013）。
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest
import yaml
from catlib import VEH, rel

from awr.sim.catalog.signature import band_shapes
from awr.sim.sensors.spec import SensorSpecError, build_spec, load_rig, schema_problems

LIB = VEH / "sensor_models"


def _model(mid: str) -> dict:
    d = yaml.safe_load((LIB / f"{mid}.yaml").read_text(encoding="utf-8"))
    d.setdefault("mount", {"xyz_m": [0.0, 0.0, 0.0], "rpy_deg": [0, 0, 0]})
    return d


def _spec(mid: str):
    return build_spec(mid, _model(mid), 0, str(LIB / f"{mid}.yaml"))


def test_model_files_schema() -> None:
    for f in sorted(LIB.glob("*.yaml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert schema_problems(d) == [], f
        assert d["model_id"] == f.stem and d["conf"] and d["src"]


def test_gx40_optics() -> None:
    z = _spec("eo_gx40").zoom
    for f, (h, v, dd) in ((4.8, (60.2, 36.1, 67.2)), (48.0, (6.6, 3.7, 7.6))):
        assert abs(math.degrees(z.hfov(f)) - h) <= 0.2 and abs(math.degrees(z.vfov(f)) - v) <= 0.2
        assert abs(math.degrees(z.dfov(f)) - dd) <= 0.2
    assert math.degrees(z.hfov(10.63)) == pytest.approx(29.35, abs=0.02) and z.fnum(10.63) == pytest.approx(1.90, abs=0.01)
    assert math.degrees(z.hfov(24.0)) == pytest.approx(13.23, abs=0.02) and z.fnum(24.0) == pytest.approx(2.37, abs=0.01)
    peff = {o.name: z.p_eff_um(o.name) for o in z.outputs}
    assert peff == pytest.approx({"4k": 1.45, "1080p": 2.90, "720p": 4.35, "sxga": 3.06, "1p3m": 3.26}, abs=0.01)
    assert math.degrees(z.hfov(4.8, "sxga")) == pytest.approx(44.4, abs=0.1) and math.degrees(z.hfov(48.0, "1p3m")) == \
        pytest.approx(4.98, abs=0.02)
    # 衍射：4K 在 F# > 1.45/0.55 = 2.64 时受约束（线性 F 数律下为 f > 31.8 mm；AWR-20 §6.2 写作 35.8 mm，见实现报告偏差）
    f_c = 4.8 + (1.45 / 0.55 - 1.7) / 1.5 * 43.2
    assert f_c == pytest.approx(31.77, abs=0.01)
    assert z.p_prime_um(f_c - 0.1, "4k") == pytest.approx(1.45) and z.p_prime_um(f_c + 0.1, "4k") == pytest.approx(0.55 * z.fnum(f_c + 0.1))
    assert z.p_prime_um(36.5, "4k") == pytest.approx(0.55 * z.fnum(36.5))
    assert z.p_prime_um(48.0, "4k") == pytest.approx(1.76, abs=0.005)
    assert z.p_prime_um(48.0, "1080p", lambda_um=0.85) == pytest.approx(2.90)  # 850 nm：2.72 < 2.90
    assert z.f_min_mm / z.f_max_mm == pytest.approx(0.1) and z.zoom_time_s == 4.0
    # GSD（1080P，R·p'/f）：48 mm、100 m 为 0.60 cm
    assert 100.0 * z.p_prime_um(48.0, "1080p") * 1e-6 / 48e-3 * 100 == pytest.approx(0.604, abs=0.002)


def test_eo_z8() -> None:
    s = _spec("eo_z8")
    z = s.zoom
    assert math.degrees(z.hfov(4.4)) == pytest.approx(64.6, abs=0.2) and math.degrees(z.hfov(35.0)) == pytest.approx(9.10, abs=0.2)
    assert z.fnum(4.4) == pytest.approx(2.0) and z.fnum(35.0) == pytest.approx(3.8)
    assert 0.55 * z.fnum(35.0) < 2.9  # 不受衍射约束
    assert s.illum is None


@pytest.mark.parametrize(("lens", "h", "v", "ifov", "gsd"), [(13, 32.9, 26.6, 923, 9.23), (25, 17.5, 14.0, 480, 4.80),
                                                             (75, 5.86, 4.69, 160, 1.60), (100, 4.40, 3.52, 120, 1.20)])
def test_lwir_lenses(lens: int, h: float, v: float, ifov: float, gsd: float) -> None:
    d = _model("lwir640")
    d["lens_mm"] = lens
    s = build_spec("lwir", d, 0)
    assert math.degrees(s.hfov_rad) == pytest.approx(h, abs=0.1) and math.degrees(s.vfov_rad) == pytest.approx(v, abs=0.1)
    assert 12.0 / lens * 1e3 == pytest.approx(ifov, rel=0.01)
    assert 100.0 * 12e-6 / (lens * 1e-3) * 100 == pytest.approx(gsd, rel=0.01)
    assert s.mass_kg == pytest.approx({13: 0.09, 25: 0.12, 75: 0.35, 100: 0.48}[lens] + 0.15)  # 型号自带独立云台
    assert 10.0 * 1.0 < 12.0  # λ·F# 10 µm < 12 µm：不受衍射约束（SN-7 不告警）


def _sigma_850(mor_m: float) -> float:
    """M07 的地面点消光（`sigma_lambda`：sigma_bg = ln20/MOR，Kim 的 q 按 V2 = ln50/sigma_bg 给出）。"""
    from awr.environment.atmosphere.optics import kim_q

    sigma_bg = math.log(20.0) / mor_m
    v2_km = math.log(50.0) / sigma_bg / 1000.0
    return sigma_bg * (850.0 / 550.0) ** (-kim_q(v2_km))


def test_illuminator_ranges() -> None:
    gx = _spec("eo_gx40")
    il = gx.illum
    z = gx.zoom
    assert il.range_m(z.hfov(4.8)) == pytest.approx(21.9, rel=0.01)
    assert il.range_m(z.hfov(24.0)) == pytest.approx(99.7, rel=0.01)
    assert il.range_m(z.hfov(48.0)) == pytest.approx(198.8, rel=0.01)
    ext = _spec("nir_ill_l").illum
    assert ext.range_m(ext.divergence_rad) == pytest.approx(464.8, rel=0.01)
    s10, s3 = _sigma_850(10000.0), _sigma_850(3000.0)
    assert il.range_m(z.hfov(48.0), 0.45, s10) == pytest.approx(234, rel=0.01)
    assert ext.range_m(ext.divergence_rad, 0.45, s10) == pytest.approx(521, rel=0.01)
    assert il.range_m(z.hfov(48.0), 0.45, s3) == pytest.approx(212, rel=0.01)
    assert ext.range_m(ext.divergence_rad, 0.45, s3) == pytest.approx(430, rel=0.01)


@pytest.mark.parametrize(("sigma", "rd"), [(0.5, 151), (0.1, 101), (0.01, 57), (10.0, 320), (2.0, 214), (0.3, 133)])
def test_radar_detection_range(sigma: float, rd: float) -> None:
    d = _model("mmw77")
    r_ref = float(d["r_ref_m"])
    r_d = r_ref * (sigma / float(d["sigma_ref_m2"])) ** 0.25
    assert rel(r_d, rd) < 0.01
    snr = float(d["snr_req_db"]) + 10 * math.log10(sigma) - 40 * math.log10(r_d / r_ref)
    assert snr == pytest.approx(13.2 + 10 * math.log10(sigma) - 10 * math.log10(sigma), abs=1e-9)  # R⁻⁴ 律
    assert r_d * 10 ** (-6.8 / 40) == pytest.approx(0.676 * r_d, rel=1e-3)
    s = _spec("mmw77")
    assert math.degrees(s.hfov_rad) == pytest.approx(120.0) and math.degrees(s.vfov_rad) == pytest.approx(30.0)


def test_rain_attenuation_person() -> None:
    """雨 10 mm/h（gamma = 1.0·10^0.7 = 5.0 dB/km）时人的检测距离约 139 m（§6.5）。"""
    d = _model("mmw77")
    g = d["rain"]["k"] * 10 ** d["rain"]["alpha"]
    r = 151.0
    for _ in range(50):
        snr = 13.2 + 10 * math.log10(0.5) - 40 * math.log10(r / 180.0) - 2 * g * r / 1000.0
        r *= 10 ** ((snr - 13.2) / 40.0)
    assert r == pytest.approx(139, abs=1.0)


def test_acoustic_self_noise_p600() -> None:
    d = _model("mic8")
    doc = yaml.safe_load((VEH / "p600" / "params.yaml").read_text(encoding="utf-8"))
    shape = band_shapes(doc)["hover"]
    lwa = doc["acoustic"]["lwa_hover_dba"]
    nr = d["nr_db"]["multirotor"]
    r_ego = d["r_ego_m"]["multirotor"]
    ego = [lwa + shape[b] - 20 * math.log10(r_ego) - 11 - nr for b in range(24)]
    for b, val in ((5, 48.9), (8, 51.5), (10, 51.4), (11, 50.0), (13, 46.4), (16, 46.4)):
        assert ego[b] == pytest.approx(val, abs=0.1), (b, ego[b])
    assert d["array_gain_db"] == pytest.approx(10 * math.log10(8), abs=0.05)


def test_mid360_v2() -> None:
    d = _model("mid360")
    rg = d["range"]
    assert rg["hard_max_m"] == 100 and d["model_version"] == "2.0.0"
    for rho, rmax in ((0.1, 40.0), (0.2, 48.2), (0.3, 53.8), (0.4, 58.1), (0.5, 61.7), (0.8, 70.0), (1.0, 74.3)):
        assert min(100.0, 40.0 * (rho / 0.1) ** rg["exponent"]) == pytest.approx(rmax, rel=0.01)
    omega = 2 * math.pi * (math.sin(math.radians(52.0)) + math.sin(math.radians(7.0)))
    assert omega == pytest.approx(5.717, abs=0.001)
    assert 200000 / 10 / omega == pytest.approx(3498, rel=0.01)
    assert (d["mass_kg"]["value"], d["power_w"]["value"]) == (0.265, 6.5)
    # 点数判据：人侧向 0.525 m²，n = 3500·A/R²·10 = 10 时 R = 42.9 m（D 级）
    assert math.sqrt(3500 * 0.525 * 10 / 10) == pytest.approx(42.9, abs=0.1)


def _pkg(tmp: Path) -> Path:
    d = tmp / "veh"
    shutil.copytree(VEH / "awr_f1", d / "awr_f1", ignore=shutil.ignore_patterns("*.glb"))
    shutil.copytree(LIB, d / "sensor_models")
    return d / "awr_f1"


def test_spec_ref_whitelist_and_version(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    rig = load_rig(pkg)
    eo = rig.by_name("eo")
    assert eo.model_id == "eo_gx40" and eo.mass_kg == pytest.approx(0.25)  # mass_kg 在白名单内
    f = pkg / "sensors" / "eo_pod.yaml"
    m = yaml.safe_load(f.read_text(encoding="utf-8"))
    model = yaml.safe_load((LIB / "eo_gx40.yaml").read_text(encoding="utf-8"))
    bad = dict(m, optics={**model["optics"], "f_max_mm": 60.0})
    f.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
    with pytest.raises(SensorSpecError) as ei:
        load_rig(pkg)
    assert any("SN-4" in p for p in ei.value.problems)
    bad = dict(m, spec_ref="eo_gx40@1.1.0")
    f.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
    with pytest.raises(SensorSpecError) as ei:
        load_rig(pkg)
    assert any("SN-2" in p for p in ei.value.problems)
