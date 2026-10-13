"""目录索引与目录校验命令（CAT-AC-001、CAT-AC-018 的确定性部分；CAT-FR-001、FR-003、FR-022；AWR-20 §7.1、§7.2、§8.2 CI-1 至 CI-4）。

- `vehicles/catalog.json` 通过契约 `vehicle/catalog.schema.json`，列出 5 个可见机型（按 order）与隐藏的 x500、x500_sih，7 个传感器
  型号、5 种电池包与 2 种充电器；CI-1 至 CI-4 无问题；M08 ProfileTable 在目录模式下装载全部机型并通过 M21 登记的检查；
- `python -m awr.sim.catalog.check` 退出码 0，REST 视图与源数据一致；
- 负例：参考机型 id 不以 awr_ 开头或显示名含真实产品词（CI-3）、可见固定翼类不足 2 个（CI-2）、型号版本不一致（CI-4）、
  电池包不在目录中（CI-4）、目录与 params.yaml 的 profile_version 不一致（CI-1）；
- REST 视图两次生成逐字节一致（CAT-NFR-002）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from catlib import ROOT, VEH, vehicles_copy
from jsonschema import Draft202012Validator

from awr.sim.catalog.loader import Catalog
from awr.sim.catalog.view import VIEW_PATH, build_view, canonical_bytes
from awr.sim.fleet.profiles import ProfileTable
from awr.sim.fleet.stages import registry as R

VISIBLE = ["p600_mid360", "awr_q1", "awr_q9", "awr_f1", "awr_v1"]


def test_catalog_index_contents() -> None:
    doc = json.loads((VEH / "catalog.json").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "packages/contracts/vehicle/catalog.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(doc)) == []
    cat = Catalog.load(VEH)
    assert cat.visible_ids() == VISIBLE
    hidden = [m["id"] for m in cat.entries() if not m["visible"]]
    assert hidden == ["x500", "x500_sih"]
    assert {s["id"] for s in doc["sensor_models"]} == {"eo_gx40", "eo_z8", "nir_ill_l", "lwir640", "mmw77", "mic8", "mid360"}
    assert {p["pack_id"] for p in doc["packs"]} == {"lpb610hv", "awr_p4s5", "awr_p12s6", "awr_l6s22", "awr_l12s22"}
    assert {c["model"] for c in doc["chargers"]} == {"c1_xr", "chg12s_ref"}
    assert cat.problems() == []
    cats = {m["id"]: m["category"] for m in cat.entries()}
    assert sum(1 for i in VISIBLE if cats[i] == "multirotor") >= 2
    assert sum(1 for i in VISIBLE if cats[i] in ("fixed_wing", "vtol")) >= 2
    for m in cat.entries():
        if m["reference"]:
            assert m["id"].startswith("awr_")
    for mid in ("awr_q1", "awr_q9", "awr_f1", "awr_v1"):
        d = VEH / mid
        assert (d / "params.yaml").exists() and (d / "model" / "model.yaml").exists() and (d / "sensors").is_dir()


def test_profile_table_catalog_mode_with_m21_checks() -> None:
    from awr.sim.catalog import checks as C

    with R.isolated_registry():
        C.register(R)
        T = ProfileTable(vehicles=VEH)
        assert set(T.ids) == set(VISIBLE) | {"x500", "x500_sih"}
        assert T.hidden_ids() == {"x500", "x500_sih"}
        for pid in VISIBLE:
            rows = T.describe(pid)["checks"]
            assert all(r["pass"] for r in rows), (pid, [r for r in rows if not r["pass"]])
        ids = {r["id"] for r in T.describe("awr_f1")["checks"]}
        assert {"FW-1", "FW-2", "FW-3", "FW-4", "FW-5", "FW-6", "FW-7", "FW-8", "MT-5", "SG-1", "SG-2", "SG-3", "MT-2", "SN-9",
                "MNT-1"} <= ids
        assert {"VT-1", "VT-2", "VT-3", "VT-4", "VH-1"} <= {r["id"] for r in T.describe("awr_v1")["checks"]}
        assert T.get("awr_f1").frame_kind == 1 and T.get("awr_v1").frame_kind == 2
        assert T.get("awr_q9").sandbox_limits == "awr_q9_std"


def test_check_cli_passes() -> None:
    r = subprocess.run([sys.executable, "-m", "awr.sim.catalog.check", "--json"], capture_output=True, text=True, cwd=ROOT,
                       timeout=300, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "python")})
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    res = json.loads(r.stdout)
    assert res["ok"] and res["view"]["up_to_date"]
    assert all(not m["problems"] for m in res["models"].values())


def test_view_deterministic_and_written() -> None:
    a = canonical_bytes(build_view())
    b = canonical_bytes(build_view())
    assert a == b
    assert VIEW_PATH.read_bytes() == a + b"\n"
    assert b"/data/" not in a and b"/home/" not in a  # 视图不含本机绝对路径


def _cat_edit(d: Path, fn) -> None:
    f = d / "catalog.json"
    doc = json.loads(f.read_text(encoding="utf-8"))
    fn(doc)
    f.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(("edit", "rule"), [
    (lambda c: c["models"][1].update({"display_name_zh": "DJI 轻型四旋翼"}), "CI-3"),
    (lambda c: [m.update({"visible": False}) for m in c["models"] if m["id"] == "awr_v1"], "CI-2"),
    (lambda c: c["sensor_models"][0].update({"model_version": "9.9.9"}), "CI-4"),
    (lambda c: c["packs"].pop(1), "CI-4"),
    (lambda c: c["models"][3].update({"profile_version": "2.0.0"}), "CI-1"),
])
def test_ci_rules_negative(tmp_path: Path, edit, rule: str) -> None:
    d = vehicles_copy(tmp_path)
    _cat_edit(d, edit)
    probs = Catalog.load(d).problems()
    assert any(p["rule"] == rule for p in probs), probs


def test_ci3_reference_id_prefix(tmp_path: Path) -> None:
    d = vehicles_copy(tmp_path)

    def edit(c: dict) -> None:
        for m in c["models"]:
            if m["id"] == "p600_mid360":
                m["reference"] = True
    _cat_edit(d, edit)
    probs = Catalog.load(d).problems()
    assert {p["rule"] for p in probs} >= {"CI-1", "CI-3"}  # reference 与 params.yaml 不一致，且 id 不以 awr_ 开头


def test_reference_model_packages() -> None:
    """参考机型的 `model/model.yaml` 通过契约（source: null、lowpoly_only），低模由 tools/vehicles/lowpoly.py 生成且三角形数在预算内。"""
    import yaml

    from awr.sim.schemas import contracts_registry

    v = Draft202012Validator({"$ref": "https://schemas.anet-drone.dev/vehicle/1/model.schema.json"}, registry=contracts_registry())
    for mid in ("awr_q1", "awr_q9", "awr_f1", "awr_v1"):
        doc = yaml.safe_load((VEH / mid / "model" / "model.yaml").read_text(encoding="utf-8"))
        assert list(v.iter_errors(doc)) == [] and doc["source"] is None and doc["fallback"] == "lowpoly_only"
        trimesh = pytest.importorskip("trimesh")
        low = VEH / mid / "model" / f"{mid}_lowpoly.glb"
        if low.exists():
            tris = {n: len(g.faces) for n, g in trimesh.load(low, force="scene").geometry.items()}
            assert tris["lowpoly"] <= 300 and tris["lowpoly_s"] <= 150, (mid, tris)
