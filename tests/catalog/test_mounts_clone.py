"""挂载、能力、机巢兼容与会话克隆（CAT-AC-011、CAT-AC-012、CAT-AC-017；CAT-FR-014、FR-015、FR-017、FR-020、FR-021；AWR-20 §4.6、
§5.5、§7.4、§7.6、§8.2 MT、CL、EN；D2-AC-09 的克隆与挂载部分）。

P600 的 `sensors:` 映射在本测试中按 AWR-20 §7.4 改为对象形式（临时机型目录；正式文件由 M08 切换，见请求 d2-WP-08-to-M08）：
- 克隆把电池提到 12 Ah（+0.24 kg）并缺省挂载 nir_ill_l（+0.18 kg）：4.245 kg > 4.1，522（违规项含 VH-7、MNT-1，值 4.245）；
- 只挂 nir_ill_l：派生 4.005 kg、432.6 W、26.9 min；克隆文档的 nir_ill 槽位成为缺省挂载；
- 启用不存在的 `radar` 槽位或 D1 槽位 `thermal` 为 560，拆除不可拆的 `eo` 为 522；越界与非白名单字段 522 且逐项给出
  field、rule、limit；改电池节数 CL-4；固定翼克隆的巡航空速改变后派生值按新空速重算，破坏 FW-7 时以 CL-3 拒绝；配额 505；
- 修改与删除：只改显示名就地生效；被实例引用时改参数或删除 105；未引用时删除；
- 机巢兼容（MT-8）：Q9 进只有 c1_xr 的机巢 525（cells），F1 进机巢箱 525（kind）；V1 进带 12S 充电器的 VTOL 起降台通过；
- 有效能力集按 §4.6 合成；工作温度交集与越界只告警（521）；P600 v2 的 `sensor_no`：camera 0、thermal 1、mid360 2、gnss 3、imu 4、
  eo 5、nir_ill 6（D1 槽位编号不变）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from catlib import VEH, vehicles_copy

from awr.sim.catalog import checks as C
from awr.sim.catalog import clone as CL
from awr.sim.catalog import derive as D
from awr.sim.catalog.loader import Catalog, capabilities_of, env_temp_check, nest_compatible
from awr.sim.fleet.profiles import ProfileTable
from awr.sim.fleet.stages import registry as R


@pytest.fixture
def T(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    d = vehicles_copy(tmp_path, p600_object_mapping=True)
    monkeypatch.setenv("AWR_VEHICLES_DIR", str(d))
    with R.isolated_registry():
        C.register(R)
        yield ProfileTable(vehicles=d)


def _v(e: pytest.ExceptionInfo) -> dict[str, dict]:
    return {x["field"]: x for x in (e.value.detail or {}).get("violations", [])}


def test_p600_v2_sensor_numbering(T) -> None:
    from awr.sim.sensors.spec import load_rig

    rig = load_rig(Path(T.get("p600_mid360").model_dir))
    assert [(s.name, s.sensor_no) for s in rig.specs] == [("camera", 0), ("thermal", 1), ("mid360", 2), ("gnss", 3), ("imu", 4),
                                                          ("eo", 5), ("nir_ill", 6)]
    assert all(r["pass"] for r in T.describe("p600_mid360")["checks"])
    res = next(r for r in C.mount_checks(T.get("p600_mid360").doc) if r["id"] == "MT-2")
    assert res["value"] == pytest.approx(1.748, abs=1e-3)  # 3.825 − 1.2 − 0.4236 − 0.265 − 0.188


def test_clone_mtow_exceeded(T) -> None:
    with pytest.raises(CL.CloneFailure) as ei:
        CL.create_clone(T, {"from_model_id": "p600_mid360", "patch": {"battery": {"capacity_ah": 12.0},
                                                                         "sensors": [{"name": "nir_ill", "enabled": True}]}})
    assert ei.value.code == 522
    v = _v(ei)
    assert {"VH-7", "MNT-1"} <= set(v) and v["VH-7"]["value"] == pytest.approx(4.245, abs=1e-4)
    assert set(v["VH-7"]) == {"field", "rule", "value", "limit", "message_zh"} and v["VH-7"]["rule"] == "CL-3"


def test_clone_with_illuminator(T) -> None:
    cid = CL.create_clone(T, {"from_model_id": "p600_mid360", "label": "P600 夜间", "patch": {"sensors": [{"name": "nir_ill"}]}})
    assert cid == "p600_mid360_c1"
    c = T.get(cid)
    assert c.mass_kg == pytest.approx(4.005) and c.display_name == "P600 夜间"
    assert D.num(c.battery["p_hover_w"]) == pytest.approx(432.6, abs=0.1)
    assert D.num(c.battery["hover_endurance_s"]) / 60 == pytest.approx(26.9, abs=0.05)
    assert c.doc["sensors"]["nir_ill"]["default"] is True and T.sessions[cid]["default_mounts"] == {"nir_ill": True}
    assert T.sessions[cid]["extra_power_w"] == pytest.approx(3.0) and T.sessions[cid]["extra_mass_kg"] == pytest.approx(0.18)


@pytest.mark.parametrize(("mounts", "code", "why"), [
    ([{"name": "radar", "enabled": True}], 560, "SLOT_UNKNOWN"),
    ([{"name": "thermal", "enabled": True}], 560, "LEGACY_SLOT_NOT_MOUNTABLE"),
    ([{"name": "eo", "enabled": False}], 522, "SLOT_NOT_REMOVABLE"),
])
def test_clone_mount_errors(T, mounts: list, code: int, why: str) -> None:
    with pytest.raises(CL.CloneFailure) as ei:
        CL.create_clone(T, {"from_model_id": "p600_mid360", "patch": {"sensors": mounts}})
    assert ei.value.code == code and ei.value.detail["why"] == why


def test_clone_bounds_violations(T) -> None:
    with pytest.raises(CL.CloneFailure) as ei:
        CL.create_clone(T, {"from_model_id": "p600_mid360", "patch": {"mass_kg": 2.0, "battery": {"capacity_ah": 30.0},
                                                                         "acoustic": {"bpf_hz": 300.0}, "link": {"range_m": 100.0}}})
    v = _v(ei)
    assert ei.value.code == 522 and set(v) == {"mass_kg", "battery.capacity_ah", "acoustic.bpf_hz", "link.range_m"}
    assert v["mass_kg"]["limit"] == [pytest.approx(0.7 * 3.825), 4.1] and v["battery.capacity_ah"]["limit"] == [5.0, 15.0]
    assert all(x["rule"] == "CL-2" for x in v.values())
    with pytest.raises(CL.CloneFailure) as ei:
        CL.create_clone(T, {"from_model_id": "p600_mid360", "patch": {"battery": {"cells": 12}}})
    assert _v(ei)["battery.cells"]["rule"] == "CL-4"
    with pytest.raises(CL.CloneFailure) as ei:
        CL.create_clone(T, {"from_model_id": "p600_mid360", "patch": {"speed": {"cruise_mps": 20.0}}})
    assert "limits.cruise_mps" in _v(ei)  # 多旋翼：17 的 speed.cruise_mps 映射为 limits.cruise_mps（≤ vxy_max）
    with pytest.raises(CL.CloneFailure) as ei:
        CL.create_clone(T, {"from_model_id": "nope", "patch": {}})
    assert ei.value.code == 520
    with pytest.raises(CL.CloneFailure) as ei:
        CL.create_clone(T, {"from_model_id": "x500", "patch": {}})
    assert ei.value.code == 522  # 隐藏机型不可克隆（CL-1）


def test_fixed_wing_clone(T) -> None:
    cid = CL.create_clone(T, {"from_model_id": "awr_f1", "patch": {"speed": {"cruise_mps": 18.5}, "airframe": {"bank_max_deg": 30.0}}})
    d = T.get(cid).doc
    assert D.num(d["airframe"]["v_cruise_mps"]) == 18.5 and D.num(d["airframe"]["phi_max_deg"]) == 30.0
    fw = D.fw_aero(d)
    assert d["derived"]["p_cruise_w"] == pytest.approx(fw.p_elec(18.5), rel=1e-6)
    assert d["derived"]["r_min_loiter_m"] == pytest.approx(13.5 ** 2 / (9.80665 * 0.5773502692), rel=1e-4)
    with pytest.raises(CL.CloneFailure) as ei:  # 巡航 20 m/s：P(v_cruise) + 爬升功率 680.9 W > p_max 650 W（FW-6）
        CL.create_clone(T, {"from_model_id": "awr_f1", "patch": {"speed": {"cruise_mps": 20.0}}})
    assert "FW-6" in _v(ei)
    with pytest.raises(CL.CloneFailure) as ei:
        CL.create_clone(T, {"from_model_id": "awr_f1", "patch": {"speed": {"cruise_mps": 15.0}}})  # wind_rating 15 > 15 − 3：FW-7
    v = _v(ei)
    assert ei.value.code == 522 and "FW-7" in v and v["FW-7"]["rule"] == "CL-3"
    with pytest.raises(CL.CloneFailure) as ei:
        CL.create_clone(T, {"from_model_id": "awr_f1", "patch": {"speed": {"min_mps": 12.0}}})
    assert "airframe.v_min_mps" in _v(ei)  # v_min 不可编辑


def test_clone_quota_update_delete(T) -> None:
    class Roster:
        def __init__(self) -> None:
            self.by_slot: dict = {}

    class E:
        def __init__(self, pid: str) -> None:
            self.profile_id = pid

    ids = [CL.create_clone(T, {"from_model_id": "awr_q1", "patch": {}}, quota=8) for _ in range(8)]
    with pytest.raises(CL.CloneFailure) as ei:
        CL.create_clone(T, {"from_model_id": "awr_q1", "patch": {}}, quota=8)
    assert ei.value.code == 505
    roster = Roster()
    roster.by_slot[0] = E(ids[0])
    assert CL.update_clone(T, ids[0], {"label": "Q1 改"}, roster) == ids[0] and T.get(ids[0]).display_name == "Q1 改"
    with pytest.raises(CL.CloneFailure) as ei:
        CL.update_clone(T, ids[0], {"patch": {"link": {"range_m": 9000.0}}}, roster)
    assert ei.value.code == 105
    with pytest.raises(CL.CloneFailure) as ei:
        CL.delete_clone(T, ids[0], roster)
    assert ei.value.code == 105
    CL.update_clone(T, ids[1], {"patch": {"link": {"range_m": 9000.0}, "sensors": [{"name": "lwir"}]}}, roster)
    c = T.get(ids[1])
    assert c.doc["link"]["range_m"] == 9000.0 and c.mass_kg == pytest.approx(1.04) and ids[1] in T.sessions
    assert D.num(c.battery["p_hover_w"]) == pytest.approx(130.0, abs=0.1)
    with pytest.raises(CL.CloneFailure):
        CL.update_clone(T, ids[1], {"patch": {"mass_kg": 5.0}}, roster)
    assert T.get(ids[1]).doc["link"]["range_m"] == 9000.0  # 失败不留部分修改
    CL.delete_clone(T, ids[2], roster)
    assert ids[2] not in T.ids
    with pytest.raises(CL.CloneFailure) as ei:
        CL.delete_clone(T, "awr_q1", roster)
    assert ei.value.code == 520


def test_nest_compat_and_capabilities() -> None:
    cat = Catalog.load(VEH)
    doc = {k: v[0] for k, v in cat.docs.items()}
    c1 = [{"model": "c1_xr", "cells_max": 6}]
    c12 = [{"model": "chg12s_ref", "cells_max": 12}]
    r = nest_compatible(doc["awr_q9"], {"kind": "box", "chargers": c1})
    assert (r["ok"], r["code"], r["why"]) == (False, 525, "cells")
    assert nest_compatible(doc["awr_f1"], {"kind": "box", "chargers": c1})["why"] == "kind"
    assert nest_compatible(doc["awr_v1"], {"kind": "vtol_pad", "chargers": c12})["ok"]
    assert nest_compatible(doc["p600_mid360"], {"kind": "vtol_pad", "chargers": c1})["ok"]
    assert nest_compatible(doc["awr_q9"], {"kind": "box", "chargers": c12})["ok"]
    assert not nest_compatible(doc["awr_v1"], {"kind": "launcher", "chargers": c12})["ok"]

    def caps(pid: str) -> set[str]:
        rig = C.default_rig(doc[pid], cat.model_dir(pid))
        return set(capabilities_of(doc[pid], list(rig.specs) if rig is not None else []))

    assert caps("awr_q9") >= {"rgb.zoom", "nir.lowlight", "nir.active", "thermal.imaging", "radar.mmw", "acoustic.array",
                              "nav.rtk", "flight.hover"}
    assert caps("awr_q1") == {"rgb.zoom", "nir.lowlight", "flight.hover"}  # EO-Z8 无照明器
    assert caps("awr_f1") == {"rgb.zoom", "nir.lowlight", "nir.active", "thermal.imaging", "nav.rtk", "flight.loiter",
                              "ops.crewed_recovery"}
    assert {"flight.hover", "flight.loiter", "nir.active"} <= caps("awr_v1")


def test_p600_capabilities_object_mapping(T) -> None:
    p = T.get("p600_mid360")
    rig = C.default_rig(p.doc, Path(p.model_dir))
    caps = set(capabilities_of(p.doc, list(rig.specs)))
    assert caps == {"rgb.zoom", "nir.lowlight", "nir.active", "lidar.mapping", "nav.slam3d", "compute.orin_nx", "nav.rtk",
                    "flight.hover"}


def test_env_temperature_warning(T) -> None:
    p = T.get("p600_mid360")
    rig = C.default_rig(p.doc, Path(p.model_dir))
    r = env_temp_check(p.doc, list(rig.specs), 45.0)
    assert r["range"] == [6.0, 40.0] and r["ok"] and r["warning"]["code"] == 521
    assert "warning" not in env_temp_check(p.doc, list(rig.specs), 20.0)


@pytest.mark.perf
def test_clone_validation_time(T) -> None:
    t0 = time.perf_counter()
    CL.create_clone(T, {"from_model_id": "p600_mid360", "patch": {"sensors": [{"name": "nir_ill"}]}})
    assert time.perf_counter() - t0 <= 0.050  # CAT-NFR-003
