"""签名谱形与充电时间（CAT-AC-010、CAT-AC-014；CAT-FR-007、FR-016；AWR-20 §4.4、§4.5；AWR-04 §8.2、§9.3；ADR-097）。

- 5 个可见机型的缺省谱形 A 计权能量和为 0 dB（±0.05 dB）；BPF 前 5 次谐波所在带高于两侧的宽带；P600 谱形与 §4.4 列表一致
  （±0.1 dB），未计权总声功率比 L_WA 高 1.6 dB；声功率推力律（F1 盘旋低 1.55 dB、V1 低 1.80 dB）；SG-1 至 SG-3 的负例；
- 充电时间：P600 4178 s、Q1 4178 s、Q9 每块 4178 s、F1 与 V1 8832 s（±1%）；稳态复核：P600 4 通道 5 块、挂照明器 4 通道 6 块、
  V1 2 通道 3 块；12S 电池不能用 6S 充电器（MT-8 节数条件）。
"""

from __future__ import annotations

import math

import pytest
import yaml
from catlib import VEH

from awr.sim.catalog import derive as D
from awr.sim.catalog.charge import charge_time_s, charger_fits, pack_charge_time_s, steady_state, t_on_s
from awr.sim.catalog.loader import Catalog
from awr.sim.catalog.signature import (
    A_WEIGHT_DB,
    a_weighted_sum_db,
    band_of,
    band_shapes,
    default_band_shape,
    lwa_at_thrust,
    sg_checks,
    unweighted_total_db,
)

P600_SHAPE = [-34.6, -31.5, -28.7, -26.0, -23.6, -9.1, -19.4, -17.6, -6.5, -14.7, -6.7, -8.0, -9.1, -11.6, -11.4, -11.4, -11.6,
              -12.1, -12.7, -13.6, -14.7, -16.1, -17.6, -19.4]


def test_default_shapes_all_models() -> None:
    cat = Catalog.load(VEH)
    for pid in cat.visible_ids():
        doc = cat.docs[pid][0]
        shapes = band_shapes(doc)
        assert shapes, pid
        for kind, s in shapes.items():
            assert len(s) == 24 and a_weighted_sum_db(s) == pytest.approx(0.0, abs=0.05), (pid, kind)
            ac = doc["acoustic"]
            bpf = ac.get("bpf_hz") if kind == "hover" else ac.get("bpf_cruise_hz")
            for h in range(1, 6):
                b = band_of(h * bpf)
                if b is None or b in (0, 23):
                    continue
                assert s[b] > min(s[b - 1], s[b + 1]), (pid, kind, h)
        assert all(r["pass"] for r in sg_checks(doc)), pid
    v1 = cat.docs["awr_v1"][0]
    assert set(band_shapes(v1)) == {"hover", "cruise"}
    assert set(band_shapes(cat.docs["awr_f1"][0])) == {"cruise"}


def test_p600_shape_and_totals() -> None:
    doc = yaml.safe_load((VEH / "p600" / "params.yaml").read_text(encoding="utf-8"))
    s = band_shapes(doc)["hover"]
    assert s == pytest.approx(P600_SHAPE, abs=0.1)
    assert unweighted_total_db(s) == pytest.approx(1.6, abs=0.05)
    assert default_band_shape(150, 12, 1414) == pytest.approx(s)
    assert len(A_WEIGHT_DB) == 24 and band_of(150.0) == 5 and band_of(20000.0) is None


def test_thrust_law() -> None:
    for pid, dl in (("awr_f1", -1.55), ("awr_v1", -1.80)):
        fw = D.fw_aero(Catalog.load(VEH).docs[pid][0])
        assert lwa_at_thrust(0.0, fw.thrust_n(fw.v_loiter_mps), fw.thrust_n(fw.v_cruise_mps), 1.5) == pytest.approx(dl, abs=0.01)


def test_sg_negative() -> None:
    doc = yaml.safe_load((VEH / "awr_q1" / "params.yaml").read_text(encoding="utf-8"))
    bad = dict(doc, acoustic={**doc["acoustic"], "band_shape_db": [0.0] * 24})
    assert any(r["id"] == "SG-1" and not r["pass"] for r in sg_checks(bad))
    bad = dict(doc, acoustic={**doc["acoustic"], "tonal_db": 25.0})
    assert any(r["id"] == "SG-2" and not r["pass"] for r in sg_checks(bad))
    bad = dict(doc, visual={**doc["visual"], "area_m2": {"top": 0.5, "side": 0.03, "front": 0.03}})
    assert any(r["id"] == "SG-3" and not r["pass"] for r in sg_checks(bad))
    bad = dict(doc, rcs_m2={"value": 0.0001, "conf": "D", "src": "x"})
    assert all(r["pass"] for r in sg_checks(bad))


def test_charge_times() -> None:
    cat = Catalog.load(VEH)
    c1, c12 = cat.charger("c1_xr"), cat.charger("chg12s_ref")
    assert pack_charge_time_s(cat.pack("lpb610hv"), c1) == pytest.approx(4178, rel=0.01)
    assert pack_charge_time_s(cat.pack("awr_p4s5"), c1) == pytest.approx(4178, rel=0.01)
    assert pack_charge_time_s(cat.pack("awr_p12s6"), c12) == pytest.approx(4178, rel=0.01)
    assert pack_charge_time_s(cat.pack("awr_l6s22"), c1) == pytest.approx(8832, rel=0.01)
    assert pack_charge_time_s(cat.pack("awr_l12s22"), c12) == pytest.approx(8832, rel=0.01)
    assert math.isinf(pack_charge_time_s(cat.pack("awr_l12s22"), c1)) and not charger_fits(cat.pack("awr_p12s6"), c1)
    assert charge_time_s(10.0, 1.0, 10.0, 0.9, 0.95) == pytest.approx(300 + 0.2 * 3600 * math.log(0.1 / 0.05), rel=1e-9)
    assert charge_time_s(10.0, 1.0, 10.0, 0.5, 0.5) == 300.0


def test_steady_state() -> None:
    t_on = t_on_s(1740.0, 100.0)
    assert t_on == pytest.approx(1192.0)
    ss = steady_state(t_on, 4178.0)
    assert (ss["n_ch"], ss["n_pack"]) == (4, 5)
    ss = steady_state(1090.0, 4178.0)
    assert (ss["n_ch"], ss["n_pack"]) == (4, 6)
    ss = steady_state(4700.0, 8832.0)
    assert (ss["n_ch"], ss["n_pack"]) == (2, 3)
    assert t_on_s(102.1 * 60, 100.0) == pytest.approx(4700, abs=1)  # V1 盘旋接力（AWR-04 §9.3）
