"""Georeferencing gates and reject policy (M01-AC-018; M01-FR-028, FR-037, §6.6.5, §6.6.6)."""

from __future__ import annotations

import numpy as np
import pytest
from georef_fixture import inputs

from awr.reconstruction.pipeline.georef import derive_scale_status, georeference, placeholder_sim3
from awr.reconstruction.types import GeorefRejected, NoGnss
from awr.world.georef.mock_gnss import FixType

P = {"mode": "gnss", "gravity": "auto", "gravity_weight": 1.0, "on_reject": "publish_relative"}


def test_accepted_gnss():
    inp, (s, _, _, _) = inputs()
    al = georeference(inp, P, seed=1)
    assert al.status == "accepted" and al.method == "gnss-sim3" and al.scale_status == "gnss" and not al.needs_review
    g = {x["name"]: x for x in al.gates}
    assert g["n_sync"]["result"] == g["inlier_ratio"]["result"] == g["rmse_m"]["result"] == g["degeneracy"]["result"] == "pass"
    assert g["rmse_m"]["reject_above"] == pytest.approx(5.0) and g["rmse_m"]["pass_at_most"] == pytest.approx(4.64, abs=0.01)
    assert abs(al.sim3.s * s - 1.0) <= 2e-3
    doc = al.to_json()
    assert doc["reference"]["kind"] == "gnss" and doc["library"]["module"] == "awr.world.georef.sim3"


def test_45pct_outliers_publish_relative():
    inp, _ = inputs(outlier=0.45)
    al = georeference(inp, P, seed=1)
    assert al.status == "rejected" and al.method == "none" and al.scale_status == "relative" and al.needs_review
    assert al.sim3.s == 1.0 and al.sim3.t == (0.0, 0.0, 0.0)
    # the placeholder levels gravity: engine "down" maps onto world -Z
    from awr.world.georef.frames import quat_to_mat

    gm = inp.g_engine.mean(0)
    np.testing.assert_allclose(quat_to_mat(np.asarray(al.sim3.q)) @ (gm / np.linalg.norm(gm)), [0, 0, -1], atol=1e-9)
    assert any(g["result"] == "reject" for g in al.gates)


def test_45pct_outliers_fail_policy_is_339():
    inp, _ = inputs(outlier=0.45)
    with pytest.raises(GeorefRejected) as ei:
        georeference(inp, {**P, "on_reject": "fail"}, seed=1)
    assert ei.value.code == 339 and not ei.value.resumable


def test_all_dropouts():
    inp, _ = inputs(dropout=1.0)
    assert inp.n_sync == 0
    with pytest.raises(NoGnss) as ei:
        georeference(inp, {**P, "on_reject": "fail"}, seed=1)
    assert ei.value.code == 338 and not ei.value.resumable
    al = georeference(inp, P, seed=1)
    assert al.scale_status == "relative" and al.method == "none" and al.status == "rejected"
    assert al.gates[0]["name"] == "n_sync" and al.gates[0]["result"] == "reject"


def test_mode_none_is_skipped():
    inp, _ = inputs()
    al = georeference(inp, {**P, "mode": "none"}, seed=1)
    assert al.status == "skipped" and al.method == "none" and al.scale_status == "relative" and not al.needs_review


def test_rtk_fixed_majority_gives_rtk():
    inp, (s, _, _, _) = inputs(sigma=(0.02, 0.02, 0.03), fix=FixType.RTK_FIX, outlier=0.0, dropout=0.0)
    al = georeference(inp, P, seed=2)
    assert al.scale_status == "rtk" and al.method == "rtk-sim3"
    assert abs(al.sim3.s * s - 1.0) <= 1e-4


def test_scale_status_order_and_placeholder():
    ss, m = derive_scale_status("rejected", None, np.array([]), "relative")
    assert (ss, m) == ("relative", "none")
    ss, m = derive_scale_status("accepted", None, np.array([]), "metric_conditioned", {"poses": "rtk"})
    assert (ss, m) == ("rtk", "pose-prior-ba")
    assert placeholder_sim3(None).s == 1.0
