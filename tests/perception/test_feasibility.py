"""M19-AC-016：纯函数与可行窗口。签名与 MS1 冻结版本一致；`feasibility_window` 重新生成的黄金表（`golden/feasibility_table.json`，
AWR-04 §9.2 的 L-05 基准，`AWR_REGEN_GOLDEN=1` 重写）逐格一致（±1 m）；geometric 行与 AWR-04 §9.2 v1.2 表 ±3%（偏差另列：狗的 NIR
按 speed_max 8 m/s 的模糊不可行，见实现报告）；`d_hi − d_lo < 10 m` 判不可行；`feasible_view` 的俯仰限位、变焦与比值；不可行原因。"""

from __future__ import annotations

import inspect
import json
import math
import os
from pathlib import Path

import numpy as np
import pytest

from awr.sim import perception as PM
from awr.sim.perception import (
    EnvSample,
    feasibility_detail,
    feasibility_window,
    feasible_view,
    gx40,
    gx40_nir,
    lwir640,
    zoom_for_level,
)
from awr.sim.perception.feasibility import golden_rows
from awr.sim.targets import P600, TargetConfig, TargetState1

GOLDEN = Path(__file__).resolve().parent / "golden" / "feasibility_table.json"
AWR04 = [  # AWR-04 §9.2 v1.2：class, sensor, MOR, d_lo, d_hi
    ("person", "eo_1080p_48mm", 10000.0, 320.0, 850.0), ("person", "eo_1080p_48mm", 3000.0, 320.0, 660.0),
    ("person", "nir_ill_l", 10000.0, 320.0, 455.0), ("person", "nir_ill_l", 3000.0, 320.0, 385.0),
    ("person", "lwir_100mm", 10000.0, 320.0, 425.0), ("person", "lwir_100mm", 3000.0, 320.0, 425.0),
    ("dog", "eo_1080p_48mm", 10000.0, 242.0, 550.0), ("dog", "eo_1080p_48mm", 3000.0, 242.0, 405.0),
    ("vehicle", "eo_1080p_48mm", 10000.0, 170.0, 2065.0), ("vehicle", "eo_1080p_48mm", 3000.0, 170.0, 1155.0),
    ("vehicle", "nir_builtin", 10000.0, 170.0, 225.0), ("vehicle", "nir_builtin", 3000.0, 170.0, 205.0),
    ("vehicle", "nir_ill_l", 10000.0, 170.0, 500.0), ("vehicle", "nir_ill_l", 3000.0, 170.0, 410.0),
    ("vehicle", "lwir_75mm", 10000.0, 170.0, 450.0), ("vehicle", "lwir_100mm", 10000.0, 170.0, 575.0),
]
AWR04_INFEASIBLE = [("person", "nir_builtin"), ("person", "lwir_75mm"), ("dog", "nir_builtin"), ("dog", "lwir_75mm")]


@pytest.fixture(scope="module")
def table():
    from awr.sim.fleet.profiles import ProfileTable
    from awr.sim.targets.signature import signature_of

    T = ProfileTable()
    rows = golden_rows({"p600": ("person", signature_of("p600_mid360", T)), "p600_dog": ("dog", signature_of("p600_mid360", T)),
                        "v1_dog": ("dog", signature_of("awr_v1", T))})
    if os.environ.get("AWR_REGEN_GOLDEN") == "1" or not GOLDEN.is_file():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        doc = {"schema": "awr.fixture.feasibility_table.v1", "source": "awr.sim.perception.feasibility.golden_rows (M19 reference)",
               "note": "AWR-04 §9.2 regenerated with K_MOR = ln 20 and the M07 Kim exponent (V2 = ln50/sigma_550); level R (14 px); physical rows "
                       "use M18 keepout_volumes (configured ∩ criterion, conservative)", "rows": rows}
        GOLDEN.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return rows


def test_signatures_frozen():
    params = {n: list(inspect.signature(getattr(PM, n)).parameters) for n in ("perception_level", "perception_level_batch", "feasible_view",
                                                                             "feasibility_window", "zoom_for_level")}
    assert params["feasibility_window"] == ["t", "sn", "light", "mor_m", "mode", "model", "level_req", "agl_m"]
    assert params["feasible_view"][:6] == ["vp", "t", "ts", "sn", "env", "level_req"]
    assert params["zoom_for_level"] == ["sn", "r_m", "d_c_m", "n_target"]


def test_golden_table_reproduces(table):
    old = json.loads(GOLDEN.read_text(encoding="utf-8"))["rows"]
    assert len(old) == len(table)
    for o, n in zip(old, table, strict=True):
        assert (o["class"], o["mode"], o["sensor"], o["mor_m"]) == (n["class"], n["mode"], n["sensor"], n["mor_m"])
        assert (o["d_hi_m"] is None) == (n["d_hi_m"] is None)
        if n["d_hi_m"] is not None:
            assert abs(o["d_hi_m"] - n["d_hi_m"]) <= 1.0 and abs(o["d_lo_m"] - n["d_lo_m"]) <= 1.0


def test_geometric_rows_track_awr04(table):
    by = {(r["class"], r["sensor"], r["mor_m"]): r for r in table if r["mode"] == "geometric"}
    for cls, sn, mor, lo, hi in AWR04:
        r = by[(cls, sn, mor)]
        assert r["d_hi_m"] is not None, (cls, sn, mor)
        assert abs(r["d_lo_m"] - lo) <= 1.0
        assert abs(r["d_hi_m"] - hi) / hi <= 0.03, (cls, sn, mor, r["d_hi_m"], hi)
    for cls, sn in AWR04_INFEASIBLE:
        assert by[(cls, sn, 10000.0)]["d_hi_m"] is None
    # 偏差（实现报告）：狗 NIR 外挂照明器按 speed_max 8 m/s 的残余模糊不可行（AWR-04 表 242–370）；LWIR 100 mm 264 m（表 275）
    assert by[("dog", "nir_ill_l", 10000.0)]["d_hi_m"] is None
    assert abs(by[("dog", "lwir_100mm", 10000.0)]["d_hi_m"] - 275.0) / 275.0 <= 0.05


def _t(cls="person", **over):
    return TargetConfig.from_dict({"class": cls, "pos": [0.0, 0.0, 0.0], **over}, f"{cls}-01")


def test_narrow_window_is_infeasible_and_reasons():
    p = _t()
    r = feasibility_detail(p, lwir640(75.0), "night", 10000.0, "geometric", P600)
    assert r.window() is None and r.why in ("narrow", "keepout_gt_range")      # 上界 ≈ 321 m，下界 320 m
    assert feasibility_window(p, gx40(), "night", 10000.0, "geometric", P600) is None
    assert feasibility_detail(p, gx40(), "night", 10000.0, "geometric", P600).why == "light"
    assert feasibility_detail(p, None, "day", 10000.0, "geometric", P600).why == "no_sensor"     # type: ignore[arg-type]
    w = feasibility_window(p, gx40(), "day", 10000.0, "geometric", P600)
    assert w is not None and w[1] - w[0] >= 10.0
    # 更大的敏感体：禁入半径大于识别距离
    big = _t(sensitivity=[{"kind": "visual", "shape": "cylinder", "radius_max_m": 1200.0}])
    assert feasibility_detail(big, gx40(), "day", 10000.0, "geometric", P600).why == "keepout_gt_range"
    # 所需等级 I：上界缩短
    wi = feasibility_window(p, gx40(), "day", 10000.0, "geometric", P600, level_req="I")
    assert wi is not None and wi[1] < w[1]


def test_feasible_view_pitch_zoom_and_ratio():
    t = _t(motion={"mode": "static"})
    s = TargetState1("person-01", 0, (0.0, 0.0, 0.0), 0.0, (0.0, 0.0, 0.0), 0.0, False, "UNAWARE", 0.5)
    vp = np.array([[400.0, 0.0, 60.0], [2000.0, 0.0, 60.0], [50.0, 0.0, -30.0], [0.0, 300.0, 120.0]])
    fv = feasible_view(vp, t, s, gx40(), EnvSample(), "R")
    assert fv.ok.tolist() == [True, False, False, True]
    assert fv.limiting[2] == "fov" and fv.limiting[1] == "pixels"
    assert 4.8 <= fv.f_mm[0] <= 48.0 and fv.ratio[0] >= 1.0
    assert fv.az_el[0, 0] == pytest.approx(math.pi) and fv.az_el[0, 1] == pytest.approx(-math.atan2(60.0, 400.0))
    assert zoom_for_level(gx40(), 100.0, 0.7246, 14.0 * 1.3) == pytest.approx(18.2 * 100.0 * 2.9e-3 / 0.7246, rel=1e-9)
    fv2 = feasible_view(vp[:1], t, s, gx40(), EnvSample(), "R", k_los=np.array([1 / 3]))
    assert not fv2.ok[0] and fv2.limiting[0] == "los"
    fvn = feasible_view(vp[:1], t, s, gx40_nir(True), EnvSample(illum_lx=0.001, band="night"), "R")
    assert fvn.ok[0]
