"""机型 profile 加载与 Digital Twin（M08-AC-009；M08-FR-042 至 FR-048；AWR-16 §11.3、§11.4）。

- x500、x500_sih、p600_mid360 通过 schema 与 VH-1..VH-7，`derived` 与文件值相对差 ≤ 1%，关键参数等于 PRD 与黄金数据注入值；
- P600 质量改为 1.505 kg：VH-1、VH-2 失败且 `ProfileError.code == 353`（sim-core 拒绝启动，另见 test_startup）；
- E 级值（rejected[] 中的值）出现在参数位时拒绝；schema 不合法（conf E）拒绝；id 重复拒绝；
- `describe()`：11 行孪生表（M08 7 行、M09 1 行、M13 3 行）与 7 条检查；限速配置三套取值（FR-046）；
- 低模与高模 glb 三角形数（≤ 5k、≤ 300、≤ 150；M08-FR-048）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
import yaml

from awr.sim.fleet.profiles import ProfileError, ProfileTable, derived_values, run_checks, validate_doc

ROOT = Path(__file__).resolve().parents[2]
VEH = ROOT / "vehicles"


def test_three_profiles_load_and_pass_checks() -> None:
    T = ProfileTable()
    assert set(T.ids) == {"x500", "x500_sih", "p600_mid360"}
    for pid in T.ids:
        d = T.describe(pid)
        assert [c["id"] for c in d["checks"]] == ["VH-1", "VH-2", "VH-3", "VH-4", "VH-5", "VH-6", "VH-7"]
        assert all(c["pass"] for c in d["checks"]), d["checks"]
        doc = d["profile"]
        for k, v in (doc.get("derived") or {}).items():
            calc = d["derived"][k]
            assert calc is not None and abs(v - calc) <= 0.01 * abs(calc), (pid, k, v, calc)


def test_regression_and_p600_parameters() -> None:
    T = ProfileTable()
    x, xs, p = T.get("x500"), T.get("x500_sih"), T.get("p600_mid360")
    assert (x.mass_kg, x.t_max_rotor_n, x.tau_motor_s, x.cda_m2, x.c_rd) == (2.064, 8.55, 0.03, 0.02065, 8.06428e-5)
    assert (xs.aero, xs.k_dv) == ("linear", 0.35)
    assert x.status == "regression" and x.default_limits == "px4_default"
    assert (p.mass_kg, p.t_max_rotor_n, p.tau_motor_s, p.cda_m2, p.c_rd) == (3.5, 19.23, 0.04, 0.035, 1.05e-4)
    assert p.inertia_kgm2 == (0.0548, 0.0548, 0.101)
    assert p.status == "placeholder" and p.default_limits == "prometheus_outdoor"
    assert round(p.twr, 2) == 2.24 and round(p.hover, 3) == 0.446
    L = {k: T.limits[k] for k in T.limit_ids}
    assert (L["px4_default"].vxy_max_mps, L["px4_default"].cruise_mps, L["px4_default"].acc_hor_mps2) == (12.0, 5.0, 3.0)
    assert (L["prometheus_outdoor"].vxy_max_mps, L["prometheus_outdoor"].cruise_mps) == (3.0, 3.0)
    assert (L["prometheus_command"].vxy_max_mps, L["prometheus_command"].acc_hor_mps2) == (1.0, 2.0)


def _p600_doc() -> dict:
    return yaml.safe_load((VEH / "p600" / "params.yaml").read_text(encoding="utf-8"))


def test_p600_mass_1505_rejected_353() -> None:
    doc = _p600_doc()
    doc["mass_kg"]["value"] = 1.505
    doc.pop("derived")
    checks, _d, probs = validate_doc(doc)
    failed = {c["id"] for c in checks if not c["pass"]}
    assert {"VH-1", "VH-2"} <= failed
    assert any(p.startswith("E_VALUE_IN_PARAM") for p in probs)


def test_e_level_and_schema_rejected(tmp_path: Path) -> None:
    doc = _p600_doc()
    doc["mass_kg"]["conf"] = "E"
    assert validate_doc(doc)[2], "conf E 在参数位必须被 schema 拒绝"
    doc = _p600_doc()
    doc["prop"]["c_rd"]["value"] = 8.06428e-4
    doc.pop("derived")
    probs = validate_doc(doc)[2]
    assert any("E_VALUE_IN_PARAM" in p for p in probs) and any(p.startswith("VH-3") for p in probs)


def test_loader_rejects_bad_dir_with_353(tmp_path: Path) -> None:
    for name in ("x500", "p600"):
        shutil.copytree(VEH / name, tmp_path / name, ignore=shutil.ignore_patterns("*.glb", "sensors"))
    doc = yaml.safe_load((tmp_path / "p600" / "params.yaml").read_text(encoding="utf-8"))
    doc["mass_kg"]["value"] = 1.505
    (tmp_path / "p600" / "params.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ProfileError) as ei:
        ProfileTable(vehicles=tmp_path)
    assert ei.value.code == 353 and ei.value.profile_id == "p600_mid360"


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    for name in ("x500", "p600"):
        shutil.copytree(VEH / name, tmp_path / name, ignore=shutil.ignore_patterns("*.glb", "sensors"))
    shutil.copytree(VEH / "p600", tmp_path / "p600b", ignore=shutil.ignore_patterns("*.glb", "sensors", "model"))
    with pytest.raises(ProfileError):
        ProfileTable(vehicles=tmp_path)


def test_vh_formulas() -> None:
    doc = _p600_doc()
    d = derived_values(doc)
    assert d["hover_thrust"] == pytest.approx(3.5 * 9.80665 / (4 * 19.23))
    assert d["endurance_check_s"] == pytest.approx(0.85 * 222 * 3600 / 515)
    so = 4 * 1500 * np.sqrt(d["hover_thrust"])
    F = 0.5 * 1.225 * 0.035 * 64 + so * 1.05e-4 * 8
    assert d["tilt_at_8mps_deg"] == pytest.approx(np.degrees(np.arctan(F / (3.5 * 9.80665))))
    c = {x["id"]: x for x in run_checks(doc)}
    assert c["VH-7"]["pass"] and c["VH-4"]["pass"]


def test_describe_twin_table() -> None:
    d = ProfileTable().describe("p600_mid360")
    twin = d["twin"]
    assert len(twin) == 11
    assert [r["component"] for r in twin] == ["Geometry", "Mass", "Inertia", "Motor", "Propeller", "Battery",
                                              "Flight Controller", "Camera", "LiDAR", "RTK", "Payload"]
    owners = [r["owner"] for r in twin]
    assert owners.count("M08") == 7 and owners.count("M09") == 1 and owners.count("M13") == 3
    by = {r["component"]: r for r in twin}
    assert by["Mass"]["conf"] == "B" and by["Inertia"]["conf"] == "D" and by["Motor"]["conf"] == "D"
    assert by["Mass"]["status"] == "placeholder"
    s = ProfileTable().summary()
    assert {x["profile_id"] for x in s} == {"x500", "x500_sih", "p600_mid360"}


def test_model_glb_triangle_budgets() -> None:
    trimesh = pytest.importorskip("trimesh")
    hero = VEH / "p600" / "model" / "p600.glb"
    low = VEH / "p600" / "model" / "p600_lowpoly.glb"
    if not hero.exists() or not low.exists():
        pytest.skip("glb 为生成物（make vehicles-models）")
    h = trimesh.load(hero, force="scene")
    assert sum(len(g.faces) for g in h.geometry.values()) <= 5000
    lo = trimesh.load(low, force="scene")
    tris = {n: len(g.faces) for n, g in lo.geometry.items()}
    assert tris["lowpoly"] <= 300 and tris["lowpoly_s"] <= 150
    m = yaml.safe_load((VEH / "p600" / "model" / "model.yaml").read_text(encoding="utf-8"))
    assert m["schema"] == "awr.vehicle_model.v1" and m["fallback"] == "lowpoly_only"
