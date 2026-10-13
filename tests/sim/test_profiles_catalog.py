"""机型目录装载、版本固定、按构型检查与会话克隆（M08-AC-206；M08-FR-205 至 FR-209；ADR-095、ADR-115；AWR-20 §3、§7、§8）。

- P600 profile 2.0.0 与 M08 §15.4.3 逐项一致（用户 MID-360 规格整理，AWR-20 §3.2、§3.3）；`p600_mid360@1.0.0` 从
  `vehicles/p600/history/params-1.0.0.yaml` 按需加载且取值等于 v1；当前版本的显式引用归一到同一表键；
- 限速表 LT 6 列（垂直速度按机型；D1 限速档缺省 3.0、1.5 m/s）；沙盒缺省限速档取 `catalog.limits_default`（`p600_std`）；
- 会话克隆：越界与非白名单字段返回 522 与逐项原因（契约 violation 形状）；电池加大 + 挂照明器超 MTOW 为 522（MT-3）；
  照明器（+0.18 kg、+3 W）派生 432.6 W、26.9 min（AWR-20 §3.3）；每会话 8 个后 505；删除被引用的克隆 105；
- 目录模式（`vehicles/catalog.json` 存在）：只装载目录列出的机型包，隐藏机型可按 id 加载但不出现在列表；固定翼文档按
  schema 1.1 加载（不含 L1 字段），执行登记的构型检查，检查失败以 353 拒绝。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import yaml

from awr.sim.fleet import kernels_l1 as K
from awr.sim.fleet.profiles import CloneError, ProfileError, ProfileTable
from awr.sim.fleet.stages import registry as R

ROOT = Path(__file__).resolve().parents[2]
VEH = ROOT / "vehicles"
F1_FIXTURE = ROOT / "packages" / "contracts" / "fixtures" / "d2" / "vehicle" / "awr_f1.params.yaml"


def _val(x):
    return x["value"] if isinstance(x, dict) and "value" in x else x


def test_p600_v2_parameters() -> None:
    T = ProfileTable()
    p = T.get("p600_mid360")
    d = p.doc
    assert p.version == "2.0.0" and p.frame_kind == 0 and p.frame_type == "quad_x"
    assert (p.mass_kg, p.mtow_kg) == (3.825, 4.1)
    assert p.inertia_kgm2 == (0.0598, 0.0598, 0.1102)
    b = d["battery"]
    assert (_val(b["v_nominal_v"]), _val(b["capacity_wh"]), _val(b["capacity_ah"]), b["cells"]) == (22.8, 228.0, 10.0, 6)
    assert (b["v_range_v"], b["v_storage_v"], b["chemistry"], b["pack_id"]) == ([22.0, 26.1], 23.1, "lihv", "lpb610hv")
    assert (_val(b["hover_endurance_s"]), _val(b["p_hover_w"]), _val(b["mass_kg"])) == (1740, 401.0, 1.2)
    assert _val(b["p_hover_w"]) == pytest.approx(0.85 * 228 / (1740 / 3600), rel=1e-3)
    der = T.describe("p600_mid360")["derived"]
    assert der["hover_thrust"] == pytest.approx(0.488, rel=1e-3) and der["twr"] == pytest.approx(2.05, rel=1e-3)
    assert der["tilt_at_8mps_deg"] == pytest.approx(7.43, abs=0.01) and der["endurance_check_s"] == pytest.approx(1740, rel=1e-3)
    assert d["nav"]["hold_sigma_m"] == {"rtk": {"h": 0.01, "v": 0.015}, "slam": {"h": 0.1, "v": 0.2}}
    assert d["env_limits"]["temp_c"] == [6, 40] and d["link"] == {"model": "gr01", "range_m": 3500}
    ac = d["acoustic"]
    assert (ac["lwa_hover_dba"], ac["bpf_hz"], ac["thrust_exp"], ac["blades"]) == (88.0, 150.0, 1.5, 2)
    assert d["visual"]["area_m2"] == {"top": 0.22, "side": 0.19, "front": 0.19} and _val(d["rcs_m2"]) == 0.05
    assert "compute.orin_nx" in d["capabilities"] and d["compute_tops"] == 100
    assert d["catalog"] == {"reference": False, "category": "multirotor", "size_class": "medium", "limits_default": "p600_std"}
    # AWR-20 §7.4 对象形式（D2-INT 切换）：eo_gx40 与 mid360 由槽位计入质量，payload 只留 orin_nx
    assert {x["name"]: x["mass_kg"] for x in d["payload"]} == {"orin_nx": 0.188}
    assert list(d["sensors"])[:7] == ["camera", "thermal", "mid360", "gnss", "imu", "eo", "nir_ill"]
    assert d["sensors"]["eo"]["default"] is True and d["sensors"]["nir_ill"]["default"] is False
    assert _val(d["mass_kg"]) - 1.2 - 0.4236 - 0.265 - 0.188 == pytest.approx(1.748, abs=1e-3)  # MT-2 机体残余质量
    assert p.default_limits == "prometheus_outdoor" and p.sandbox_limits == "p600_std"
    L = T.LT[T.limits_index("p600_std", "p600_mid360")]
    assert tuple(np.round(L, 6)) == (12.0, 8.0, 3.0, round(np.radians(60.0), 6), 5.0, 3.0)
    Ld = T.LT[T.limits_index("prometheus_outdoor", "p600_mid360")]
    assert (Ld[K.L_ZUP], Ld[K.L_ZDN]) == (3.0, 1.5)  # D1 限速档：与 D1 全局常量相同（行为不变）
    assert T.LT.shape[1] == 6
    chk = {c["id"]: c for c in T.describe("p600_mid360")["checks"]}
    assert chk["MNT-1"]["pass"] and chk["VH-7"]["pass"]


def test_history_version_pinning() -> None:
    T = ProfileTable()
    n0 = len(T.ids)
    assert "p600_mid360@1.0.0" not in T.ids  # 存档只按需加载，不进入目录
    v1 = T.get("p600_mid360@1.0.0")
    assert (v1.version, v1.mass_kg, v1.inertia_kgm2, v1.ref) == ("1.0.0", 3.5, (0.0548, 0.0548, 0.101), "p600_mid360@1.0.0")
    assert _val(v1.battery["p_hover_w"]) == 515.0 and _val(v1.battery["hover_endurance_s"]) == 1320
    assert len(T.ids) == n0 + 1 and T.index("p600_mid360@1.0.0") == n0
    assert T.PT[n0, K.P_MASS] == 3.5 and T.PT[T.index("p600_mid360"), K.P_MASS] == 3.825
    assert T.key_of("p600_mid360@2.0.0") == "p600_mid360" and T.index("p600_mid360@2.0.0") == T.index("p600_mid360")
    assert all(not x["profile_id"].endswith("@1.0.0") for x in T.summary())
    with pytest.raises(KeyError):
        T.get("p600_mid360@9.9.9")
    with pytest.raises(KeyError):
        T.index("nope_model")
    assert (VEH / "p600" / "history" / "params-1.0.0.yaml").exists()


def _d1_mapping_vehicles(tmp: Path) -> Path:
    """P600 2.0.0 参数、字符串形式的传感器映射（照明器不是槽位）：克隆以 `extra_mass_kg` 表达加挂照明器时 MNT-1 不重复计入可选槽位
    （对象形式下 M21 的目录克隆改写缺省标记，见 d2-WP-08-to-M08 第 4 条；本用例只验证 M08 的克隆算术）。"""
    dst = tmp / "vehicles"
    shutil.copytree(VEH, dst, ignore=shutil.ignore_patterns("*.glb"))
    f = dst / "p600" / "params.yaml"
    doc = yaml.safe_load(f.read_text(encoding="utf-8"))
    doc["sensors"] = {"camera": "sensors/camera.yaml", "thermal": "sensors/thermal.yaml", "mid360": "sensors/mid360.yaml",
                      "gnss": "sensors/gnss.yaml", "imu": "sensors/imu.yaml"}
    doc["payload"] = [{"name": "eo_gx40", "mass_kg": 0.4236, "capability_id": "rgb.zoom"},
                      {"name": "mid360", "mass_kg": 0.265, "capability_id": "lidar.mapping"},
                      {"name": "orin_nx", "mass_kg": 0.188, "capability_id": "compute.orin_nx"}]
    f.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return dst


def test_clone_bounds_and_derived(tmp_path: Path) -> None:
    veh = _d1_mapping_vehicles(tmp_path)
    T = ProfileTable(vehicles=veh)
    cid = T.add_session("p600_mid360", {}, extra_mass_kg=0.18, extra_power_w=3.0, display_name="P600 夜间")
    c = T.get(cid)
    assert cid == "p600_mid360_c1" and c.base_ref == "p600_mid360" and c.model == "p600"
    assert c.mass_kg == pytest.approx(4.005)
    assert _val(c.battery["p_hover_w"]) == pytest.approx(432.6, abs=0.1)  # 401·(4.005/3.825)^1.5 + 3
    assert _val(c.battery["hover_endurance_s"]) / 60 == pytest.approx(26.9, abs=0.05)
    with pytest.raises(CloneError) as ei:  # 12 Ah（+0.24 kg）+ 照明器（+0.18 kg）= 4.245 kg > 4.1（MT-3、CAT-AC-011）
        T.add_session("p600_mid360", {"battery.capacity_ah": 12.0}, extra_mass_kg=0.18, extra_power_w=3.0)
    assert ei.value.code == 522
    v = {x["field"]: x for x in ei.value.problems}
    assert set(v) >= {"VH-7", "MNT-1"} and v["VH-7"]["value"] == pytest.approx(4.245)
    assert set(ei.value.problems[0]) == {"field", "rule", "value", "limit", "message_zh"}
    with pytest.raises(CloneError) as ei:
        T.add_session("p600_mid360", {"battery.capacity_ah": 20.0, "frame.type": "fixed_wing", "mass_kg": 1.0})
    rules = {x["field"]: x["rule"] for x in ei.value.problems}
    assert rules == {"battery.capacity_ah": "CL-2", "frame.type": "CL-2", "mass_kg": "CL-2"}
    with pytest.raises(CloneError) as ei:
        T.add_session("p600_mid360", {"airframe.v_cruise_mps": 15.0})  # 多旋翼没有 airframe
    assert ei.value.code == 522
    with pytest.raises(CloneError) as ei:
        T.add_session("nope", {})
    assert ei.value.code == 520
    with pytest.raises(CloneError):
        T.add_session(cid, {})  # 不能以克隆为源
    c2 = T.add_session("p600_mid360", {"limits.cruise_mps": 9.0, "limits.z_up_mps": 4.0})
    assert c2 == "p600_mid360_c2" and T.get(c2).sandbox_limits == f"{c2}_std"
    L = T.LT[T.limits_index(f"{c2}_std", c2)]
    assert (L[K.L_VXY], L[K.L_CRUISE], L[K.L_ZUP], L[K.L_ZDN]) == (12.0, 9.0, 4.0, 3.0)
    with pytest.raises(CloneError):
        T.add_session("p600_mid360", {"limits.cruise_mps": 13.0})  # 巡航超过最大水平速度
    with pytest.raises(CloneError) as ei:
        T.remove_session(c2, in_use=lambda ref: True)
    assert ei.value.code == 105 and ei.value.detail == "MODEL_IN_USE"
    T.remove_session(c2)
    assert c2 not in T.ids and f"{c2}_std" not in T.limit_ids
    for _ in range(7):
        T.add_session("x500", {})
    with pytest.raises(CloneError) as ei:
        T.add_session("x500", {})
    assert ei.value.code == 505
    specs = T.session_specs()
    T2 = ProfileTable(vehicles=veh)
    assert T2.restore_sessions(specs) == [] and [s["id"] for s in T2.session_specs()] == [s["id"] for s in specs]
    assert T2.get(cid).mass_kg == pytest.approx(4.005)
    assert T.clone_docs()[0] == {"schema": "awr.vehicle_clone.v1", "base": "p600_mid360@2.0.0", "id": cid, "set": {},
                                 "display_name": "P600 夜间"}


def _catalog_dir(tmp: Path, *, extra_fw: bool) -> Path:
    for name in ("x500", "p600"):
        shutil.copytree(VEH / name, tmp / name, ignore=shutil.ignore_patterns("*.glb"))
    shutil.copytree(VEH / "p600", tmp / "unlisted", ignore=shutil.ignore_patterns("*.glb", "model", "history"))
    models = [{"id": "p600_mid360", "dir": "p600", "profile_version": "2.0.0", "visible": True},
              {"id": "x500", "dir": "x500", "profile_version": "1.0.0", "visible": False},
              {"id": "x500_sih", "dir": "x500", "profile_version": "1.0.0", "visible": False}]
    if extra_fw:
        (tmp / "awr_f1").mkdir()
        shutil.copyfile(F1_FIXTURE, tmp / "awr_f1" / "params.yaml")
        models.append({"id": "awr_f1", "dir": "awr_f1", "profile_version": "1.0.0", "visible": True})
    (tmp / "catalog.json").write_text(json.dumps({"schema": "awr.vehicle_catalog.v1", "catalog_version": "1.0.0",
                                                  "models": models, "sensor_models": [], "packs": [], "chargers": []}),
                                      encoding="utf-8")
    return tmp


def test_catalog_mode_and_fixed_wing(tmp_path: Path) -> None:
    calls = []

    def fw_check(doc: dict) -> list[dict]:
        calls.append(doc["id"])
        af = doc["airframe"]
        return [{"id": "FW-3", "value": af["v_loiter_mps"], "range": [af["v_min_mps"], af["v_cruise_mps"]],
                 "pass": af["v_min_mps"] <= af["v_loiter_mps"] <= af["v_cruise_mps"]}]

    with R.isolated_registry():
        R.register_profile_check(frozenset({"fixed_wing", "vtol_quadplane"}), "FW-3", fw_check, owner="M21")
        T = ProfileTable(vehicles=_catalog_dir(tmp_path, extra_fw=True))
        assert set(T.ids) == {"p600_mid360", "x500", "x500_sih", "awr_f1"}  # unlisted 不装载
        f1 = T.get("awr_f1")
        assert (f1.frame_type, f1.frame_kind, f1.model, f1.mass_kg) == ("fixed_wing", 1, "awr-f1", 6.0)
        assert int(T.frame_kind[T.index("awr_f1")]) == 1 and calls == ["awr_f1"]
        ids = [c["id"] for c in T.describe("awr_f1")["checks"]]
        assert ids == ["FW-3", "MNT-1"]
        assert T.hidden_ids() == {"x500", "x500_sih"}
        listed = {x["profile_id"]: x for x in T.summary()}
        assert listed["x500"]["hidden"] and listed["awr_f1"]["frame_kind"] == 1 and listed["awr_f1"]["reference"]
        assert T.describe("awr_f1")["frame_kind"] == 1 and T.describe("awr_f1")["sandbox_limits"] == "awr_f1_std"
        # 构型检查失败：353 拒绝加载
        doc = yaml.safe_load((tmp_path / "awr_f1" / "params.yaml").read_text(encoding="utf-8"))
        doc["airframe"]["v_loiter_mps"] = 30.0
        (tmp_path / "awr_f1" / "params.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        with pytest.raises(ProfileError) as ei:
            ProfileTable(vehicles=tmp_path)
        assert ei.value.code == 353 and ei.value.profile_id == "awr_f1" and any("FW-3" in p for p in ei.value.problems)


def test_catalog_missing_package_rejected(tmp_path: Path) -> None:
    d = _catalog_dir(tmp_path, extra_fw=False)
    cat = json.loads((d / "catalog.json").read_text(encoding="utf-8"))
    cat["models"].append({"id": "awr_q9", "dir": "awr_q9", "profile_version": "1.0.0", "visible": True})
    (d / "catalog.json").write_text(json.dumps(cat), encoding="utf-8")
    with pytest.raises(ProfileError) as ei:
        ProfileTable(vehicles=d)
    assert ei.value.code == 353 and "CI-1" in ei.value.problems[0]
