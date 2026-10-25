"""town 世界包 `smalltown-01` 按 crop 同一判据复验（D3-AC-02 的 town 部分，P0b；M22-AC-008 town 部分；W08）。

前半部分只读入库的配方与配准记录（不需要原始资产）：与 crop 同一组标定 QA 输入（水面与开阔面抽取、交叉核对 DEM、r2 对照尺度），
配准方法与 M22 §8.1 一致（`osm-road-correlation`，`scaleStatus = landmark`）。后半部分读已发布的 `worlds/smalltown-01`
（生成物不入 git，缺失时按 data 层跳过；构建命令 `python -m awr.world.glb build smalltown-01`），逐条对照 §11 阈值：
`metric_ok()` 为真且可排名；尺度复测与 r2 差 ≤ 0.5%、R5 ≤ 0.5%；两法调平差 ≤ 0.5°、剩余倾角 < 0.5°；L3、L4 或 L5 至少一项成立；
V-GLB 全部通过（锚点图层尚未生成时 V-GLB-06 为不适用，与 crop 相同）；`dsm_2m` 与网格竖向射线差 < 1 cm；许可、署名与
`derivedFrom[]`（含 ODbL）齐全。D2 运行时加载与 dsm 的独立抽样复测由 `test_d2_load.py`、`test_dsm_vs_mesh.py` 承担（W07）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WID = "smalltown-01"
CFG = ROOT / "configs" / "worlds.d3" / WID
TOWN = Path(os.environ.get("AWR_WORLDS_DIR") or ROOT / "worlds") / WID
needs_town = pytest.mark.skipif(not (TOWN / "world.json").is_file(), reason=f"needs worlds/{WID} (python -m awr.world.glb build {WID})")

SCALE_MAX_REL = 0.005           # 尺度与 r2 差 ≤ 0.5%（D3-AC-02）
L1_MAX_DEG = 0.5                # DEM 回归与墙面竖直度两法之差（D3-AC-02）
G05_MAX_DEG = 0.5               # 调平后剩余倾角（M22 §6.7）
DSM_MAX_ABS_M = 0.01            # dsm_2m 与网格竖向射线抽样差 < 1 cm（D3-AC-02）
R2_TOWN_M_PER_UNIT = 8.374      # M22 §8.1 预期尺度


def _json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _recipe() -> dict:
    return yaml.safe_load((CFG / "recipe.yaml").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------- 配方与配准记录（入库文件）
def test_town_recipe_uses_crop_calibration_inputs() -> None:
    """town 与 crop 同一组标定 QA 输入：水面与开阔面抽取（L3）、GLO-30 交叉核对 DEM（L5）、主 DEM 与 OSM；r2 对照尺度 8.374。"""
    town, crop = _recipe(), yaml.safe_load((ROOT / "configs/worlds.d3/oryahovo-crop1/recipe.yaml").read_text(encoding="utf-8"))
    q, qc = town["qa"], crop["qa"]
    assert q["reference_m_per_unit"] == R2_TOWN_M_PER_UNIT
    key = lambda es: sorted((e["path"], e["sha256"]) for e in es)          # noqa: E731
    assert key(q["osm_extra"]) == key(qc["osm_extra"])
    assert key(q["dem"]) == key(qc["dem"]) and {e["model"] for e in q["dem"]} == {"glo30"}
    assert (town["osm"]["sha256"], town["dem"]["sha256"]) == (crop["osm"]["sha256"], crop["dem"]["sha256"])
    assert all(e["use"] == "levelling" for e in q["osm_extra"] + q["dem"])     # 契约枚举词（W03 归一规则）
    man = {f["name"]: f["sha256"] for f in _json(ROOT / "configs/worlds.d3/raw-manifest.json")["files"]}
    for e in [town["source"]["glb"], town["osm"], town["dem"], *q["osm_extra"], *q["dem"]]:
        assert man.get(e["path"]) == e["sha256"], e["path"]


def test_town_registration_method_matches_m22() -> None:
    """配准记录：M22 §8.1 town 为 `osm-road-correlation`（r2 E03/E03b 实测导入），`landmark`，V-GLB-02 不再因 manual 告警。"""
    reg = _json(CFG / "registration.json")
    assert reg["status"] == "accepted" and reg["method"] == "osm-road-correlation" and reg["scale_status"] == "landmark"
    assert abs(reg["result"]["m_per_unit"] / R2_TOWN_M_PER_UNIT - 1) <= SCALE_MAX_REL
    assert reg["evidence"] and reg["evidence"][0].startswith("osm-road-correlation")
    assert reg["inputs"]["glb"]["sha256"] == _recipe()["source"]["glb"]["sha256"]


# ------------------------------------------------------------------------------------------------- 已发布世界包
@needs_town
def test_town_world_is_metric_and_rankable() -> None:
    """M22-AC-008（town 部分）、D3-AC-02：`scaleStatus = landmark`，`metric_ok()` 的 ok 与 ranked_ok 均为真。"""
    from awr.world.geometry.status import metric_ok

    ms = metric_ok(TOWN)
    assert ms.ok and ms.ranked_ok and ms.reason is None and ms.missing == ()
    assert ms.scale_status == "landmark" and ms.world_id == WID
    assert ms.content_version == _json(TOWN / "world.json")["contentVersion"]


@needs_town
def test_town_package_binds_recipe_and_registration() -> None:
    """世界包与入库配准一致：profile d3-glb、配准方法与尺度、qa 报告的 contentVersion 与 world.json 相同。"""
    w, c, r = _json(TOWN / "world.json"), _json(TOWN / "coordinate.json"), _json(TOWN / "qa/report.json")
    reg = _json(CFG / "registration.json")
    assert w["profile"] == "d3-glb" and w["scaleStatus"] == c["scaleStatus"] == "landmark"
    assert r["content_version"] == w["contentVersion"]
    assert c["registration"]["method"] == reg["method"] == r["glb"]["registration"]["method"]
    assert c["source"]["unitsToMeters"] == pytest.approx(reg["result"]["m_per_unit"], rel=1e-12)


@needs_town
def test_town_calibration_thresholds() -> None:
    """D3-AC-02 标定条款逐条：尺度复测 vs r2 ≤ 0.5%、R5 ≤ 0.5%；两法差 ≤ 0.5°、剩余倾角 < 0.5°；L3、L4、L5 至少一项成立。"""
    r = _json(TOWN / "qa/report.json")
    gates = {g["id"]: g for g in r["gates"]}
    for gid in ("G-04", "G-05", "G-11", "G-13", "G-14", "G-15"):
        assert gates[gid]["pass"], (gid, gates[gid]["value"])
    sc = r["glb"]["scale"]
    assert sc["reference_m_per_unit"] == R2_TOWN_M_PER_UNIT
    assert abs(sc["rel_diff"]) <= SCALE_MAX_REL and sc["fit"]["restarts_rel_std"] <= SCALE_MAX_REL
    lv = r["glb"]["leveling"]
    assert lv["km_scale"] and lv["primary"] == "dem"
    assert lv["L1"] and lv["delta_deg"] <= L1_MAX_DEG and lv["residual_tilt_deg"] < G05_MAX_DEG
    assert lv["L3"] or lv["L4"] or lv["L5"]
    assert not lv["needs_control_points"]


@needs_town
def test_town_vglb_all_pass() -> None:
    """V-GLB 全部通过；不适用只允许 V-GLB-06（锚点图层尚未由流水线生成，与 crop 相同）；V-GLB-04 的 dsm 差 < 1 cm。"""
    r = _json(TOWN / "qa/report.json")
    res = {v["id"]: v for v in r["glb"]["vglb"]}
    assert sorted(res) == [f"V-GLB-{i:02d}" for i in range(1, 17)]
    bad = {k: v for k, v in res.items() if v["status"] != "pass" and not (k == "V-GLB-06" and v["status"] == "n/a")}
    assert not bad, bad
    m = res["V-GLB-04"]["measured"]
    assert m["cells"] == 10_000 and m["hit_cells"] > 0 and m["max_abs_m"] < DSM_MAX_ABS_M
    assert r["status"] in ("pass", "warn") and r["validation"]["errors"] == []


@needs_town
def test_town_license_and_derived_from() -> None:
    """`dataset.license`、`citation`、`derivedFrom[]`（OSM 为 ODbL-1.0，主 DEM 与交叉核对 DEM 均登记 sha256）齐全。"""
    d = _json(TOWN / "world.json")["dataset"]
    assert d["license"] == "CC-BY-4.0" and "Mitko" in d["citation"] and d["redistribution"] is True
    assert "ODbL-1.0" in d["notice"]
    by = {(e["name"], e["use"]): e for e in d["derivedFrom"]}
    osm = [e for e in d["derivedFrom"] if e["license"] == "ODbL-1.0"]
    assert {e["use"] for e in osm} == {"registration,anchors,navgraph", "levelling"}
    assert all(e["attribution"] == "OpenStreetMap contributors" for e in osm)
    assert ("SRTM 1 arc-second", "levelling,hMsl") in by and ("Copernicus DEM GLO-30", "levelling") in by
    assert all(len(e["sha256"]) == 64 for e in d["derivedFrom"])
