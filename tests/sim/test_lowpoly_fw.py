"""固定翼与 VTOL 程序化低模（AWR-20 §7.1；AWR-04 §14.2 WP-02 的固定翼低模工具；M08-FR-048 的三角形预算）。

- 固定翼：机身盒 + 主翼 + 平尾 + 垂尾 + 推进桨盘；VTOL 另加两根纵向升力杆与 4 个升力桨盘；
- 两档 mesh：`lowpoly` ≤ 300、`lowpoly_s` ≤ 150 三角形；包围盒的翼展与机长与 `airframe` 一致（±5%）；
- 写出 glb（trimesh）后按 mesh 名计数；参考机型的文件名为 `<id>_lowpoly.glb`。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "vehicles"))
# 全量收集时 tools/bench/ipc 等目录的同名 `_common` 可能已在 sys.modules 中：导入 lowpoly 期间换成 tools/vehicles 的那一个，之后还原
_prev_common = sys.modules.pop("_common", None)
import lowpoly as LP  # noqa: E402

if _prev_common is not None:
    sys.modules["_common"] = _prev_common
else:
    sys.modules.pop("_common", None)

F1 = yaml.safe_load((ROOT / "packages/contracts/fixtures/d2/vehicle/awr_f1.params.yaml").read_text(encoding="utf-8"))


def _v1() -> dict:
    d = copy.deepcopy(F1)
    d["id"] = "awr_v1"
    d["frame"] = {"type": "vtol_quadplane", "rotor_order": "px4", "body": "frd"}
    d["geometry"]["arm_xy_m"] = {"value": 0.4419, "conf": "D", "src": "AWR-20 §4.2"}
    d["prop"] = {"d_m": {"value": 0.457, "conf": "D", "src": "AWR-20 §4.2"}}
    d["airframe"]["span_m"]["value"] = 2.8
    d["airframe"]["length_m"]["value"] = 1.6
    d["airframe"]["wing_area_m2"]["value"] = 0.85
    return d


@pytest.mark.parametrize(("doc", "span", "length"), [(F1, 2.6, 1.4), (_v1(), 2.8, 1.6)])
def test_fw_lowpoly_budgets_and_size(doc: dict, span: float, length: float) -> None:
    for small, lim in ((False, LP.MAX_LOW), (True, LP.MAX_LOW_S)):
        v, f = LP.build(doc, small=small)
        assert 0 < len(f) <= lim and f.max() < len(v)
        ext = v.max(0) - v.min(0)
        assert ext[1] == pytest.approx(span, rel=0.05)
        assert ext[0] == pytest.approx(length, rel=0.06)
    if doc["frame"]["type"] == "vtol_quadplane":
        assert len(LP.build(doc, small=False)[1]) > len(LP.build(F1, small=False)[1])


def test_fw_lowpoly_glb(tmp_path: Path) -> None:
    pytest.importorskip("trimesh")
    out = tmp_path / "awr_v1_lowpoly.glb"
    tris = LP.write_glb(_v1(), out)
    assert set(tris) == {"lowpoly", "lowpoly_s"} and tris["lowpoly"] <= 300 and tris["lowpoly_s"] <= 150
    assert out.stat().st_size > 0
    assert np.isfinite(LP.build(_v1(), small=True)[0]).all()
