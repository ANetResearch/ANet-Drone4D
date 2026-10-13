"""Frozen M19 pure-function signatures and the M18 state block (WP-01; AWR-04 §14.1 D2-MS1; M18-M19 PRD §7.1), checked on the stubs in
tools/fake and, once delivered, on awr.sim.perception and awr.sim.targets; plus the closed-form anchors of the stub physics."""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import ROOT, load

sys.path.insert(0, str(ROOT / "tools" / "fake"))
import perception_stub as P
import targets_stub as T

FROZEN_FUNCS = {
    "perception_level": ["sn", "t", "i", "geom", "env", "n_req"],
    "perception_level_batch": ["sn", "t", "idx", "geom", "env", "n_req"],
    "feasible_view": ["vp", "t", "ts", "sn", "env", "level_req", "*n_req_px", "*k_los"],
    "feasibility_window": ["t", "sn", "light", "mor_m", "mode", "model", "*level_req", "*agl_m"],
    "zoom_for_level": ["sn", "r_m", "d_c_m", "n_target"],
}
FROZEN_TYPES = {
    "LevelResult": ["level", "n", "n_eff", "n_req", "p", "gsd_m", "range_m", "k", "limiting", "dt_app_k"],
    "FeasibleView": ["ok", "ratio", "f_mm", "az_el", "limiting"],
}
FROZEN_TARGET_FUNCS = {
    "critical_dim": ["size_m", "eps_rad", "beta_rad", "*worst", "*override_m"],
    "keepout_volumes": ["t", "state", "mode", "margin_m", "*conservative", "*moving_as_circle"],
}
FROZEN_VIEW = ["n", "target_no", "cls", "pos", "yaw_rad", "vel", "size_m", "occlusion", "cfg", "rev"]


def _params(fn) -> list[str]:
    out = []
    for p in inspect.signature(fn).parameters.values():
        if p.kind is inspect.Parameter.KEYWORD_ONLY:
            out.append("*" + p.name)
        elif p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY):
            out.append(p.name)
    return out


def _compatible(got: list[str], frozen: list[str]) -> bool:
    """Same positional parameters in order; the frozen keyword-only ones present; extra parameters only keyword-only."""
    pos_g = [x for x in got if not x.startswith("*")]
    pos_f = [x for x in frozen if not x.startswith("*")]
    return pos_g == pos_f and set(x for x in frozen if x.startswith("*")) <= set(got)


def test_stub_signatures_are_the_frozen_ones():
    for name, frozen in FROZEN_FUNCS.items():
        assert _params(getattr(P, name)) == frozen, name
    for name, fields in FROZEN_TYPES.items():
        assert [f.name for f in dataclasses.fields(getattr(P, name))] == fields, name
    for name, frozen in FROZEN_TARGET_FUNCS.items():
        assert _params(getattr(T, name)) == frozen, name
    assert [f.name for f in dataclasses.fields(T.TargetView)] == FROZEN_VIEW


def _module(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def test_delivered_modules_keep_the_frozen_signatures():
    """Active once M19 (awr.sim.perception) and M18 (awr.sim.targets) deliver; until then the stubs are the reference."""
    perc = _module("awr.sim.perception")
    if perc is not None:
        for name, frozen in FROZEN_FUNCS.items():
            if hasattr(perc, name):
                assert _compatible(_params(getattr(perc, name)), frozen), name
        for name, fields in FROZEN_TYPES.items():
            if hasattr(perc, name):
                got = [f.name for f in dataclasses.fields(getattr(perc, name))]
                assert got[: len(fields)] == fields, name
    tg = _module("awr.sim.targets")
    if tg is not None:
        for name, frozen in FROZEN_TARGET_FUNCS.items():
            if hasattr(tg, name):
                assert _compatible(_params(getattr(tg, name)), frozen), name
        if hasattr(tg, "TargetView"):
            assert [f.name for f in dataclasses.fields(tg.TargetView)][: len(FROZEN_VIEW)] == FROZEN_VIEW


def test_target_block_fields():
    names = [n for spec in T.BLOCK_FIELDS.values() for n in spec]
    assert len(names) == len(set(names))
    for owner in ("M18", "M19", "M20"):
        assert all(n.startswith("tgt_") for n in T.BLOCK_FIELDS[owner]), owner
    assert set(T.M08_FIELDS) == {"active", "target_no", "roster_flags"}
    blk = T.new_block()
    assert all(a.shape[0] == T.CAPACITY == 32 for a in blk.values())
    for spec in T.BLOCK_FIELDS.values():
        for name, (dt, shape) in spec.items():
            assert np.dtype(dt).kind in "uif" and isinstance(shape, tuple), name


# ---------------------------------------------------------------- stub physics anchors (M19-AC-002 to 004, M18-AC-003)
def _person(**kw):
    c = T.TargetConfig.from_dict({"class": "person", "pos": [0.0, 0.0, 0.0], **kw}, "person-01")
    s = T.TargetState1("person-01", 0, (0.0, 0.0, 0.0), 0.0, (0.0, 0.0, 0.0), 0.0, False, "UNAWARE", 0.5)
    return c, s, T.view_of([c], [s])


def test_ttpf_and_optics_anchors():
    assert abs(P.ttpf(1.75 * 8.0, 8.0) - 0.900) <= 0.001
    eo = P.gx40()
    fov = [math.degrees(P.hfov_rad(eo, f, "4k")) for f in (4.8, 48.0)] + [math.degrees(P.vfov_rad(eo, f)) for f in (4.8, 48.0)]
    for got, exp in zip(fov, (60.23, 6.64, 36.14, 3.74), strict=True):
        assert abs(got - exp) <= 0.2
    assert abs(P.p_prime_um(eo, 48.0, "4k") - 1.76) < 0.001 and P.p_prime_um(eo, 48.0, "1080p") == pytest.approx(2.90)
    d = 0.7245688373094719
    for res, exp in (("1080p", 857.0), ("4k", 1412.0)):
        r = d * 48.0 / (14.0 * P.p_prime_um(eo, 48.0, res) * 1e-3)
        assert abs(r - exp) / exp < 0.01, res
    assert abs(d * 100.0 / (14.0 * 12e-3) - 431.0) / 431.0 < 0.01


def test_blur_anchors_moving_platform():
    _c, _s, v = _person(contrast0=4.0)
    eo = P.perception_level(P.SensorState(P.gx40(), 16.0), v, 0, P.ViewGeom((190.0, 0.0, 0.0), 1.0, True, 15.0), P.EnvSample(illum_lx=1.0, mor_m=1e9), 14.0)
    lw = P.perception_level(P.SensorState(P.lwir640(75.0), 75.0), v, 0, P.ViewGeom((190.0, 0.0, 0.0), 1.0, True, 15.0),
                            P.EnvSample(illum_lx=1.0, band="night", mor_m=1e9), 14.0)
    assert eo.k[3] >= 0.5 and abs(eo.k[3] - 0.526) < 0.01
    assert lw.k[3] >= 0.8 and abs(lw.k[3] - 0.851) < 0.01


def test_monotone_in_mor_and_limiting():
    _c, _s, v = _person()
    sn = P.SensorState(P.gx40(), 48.0)
    prev = None
    for mor in (5000.0, 3000.0, 2000.0, 1000.0, 500.0):
        r = P.perception_level(sn, v, 0, P.ViewGeom((600.0, 0.0, 60.0)), P.EnvSample(mor_m=mor), 14.0)
        if prev is not None:
            assert r.n_eff <= prev
        prev = r.n_eff
    far = P.perception_level(sn, v, 0, P.ViewGeom((3000.0, 0.0, 60.0)), P.EnvSample(), 14.0)
    assert far.limiting == "pixels" and far.level <= 1
    hidden = P.perception_level(sn, v, 0, P.ViewGeom((300.0, 0.0, 60.0), k_los=1 / 3), P.EnvSample(), 14.0)
    assert hidden.limiting == "los"
    night = P.perception_level(sn, v, 0, P.ViewGeom((300.0, 0.0, 60.0)), P.EnvSample(illum_lx=0.001, band="night"), 14.0)
    assert night.limiting == "light" and night.n_eff < 14.0


AWR04_TABLE = [  # AWR-04 §9.2 (v1.2, K_MOR = ln 20): class, sensor, light, MOR, d_hi; the stub tracks these within 3 %
    ("person", "eo", "day", 10000.0, 850.0), ("person", "eo", "day", 3000.0, 660.0), ("person", "nir_ill_l", "night", 10000.0, 455.0),
    ("person", "nir_ill_l", "night", 3000.0, 385.0), ("person", "lwir100", "night", 10000.0, 425.0), ("dog", "eo", "day", 10000.0, 550.0),
    ("vehicle", "eo", "day", 10000.0, 2065.0), ("vehicle", "nir_ill_l", "night", 10000.0, 500.0), ("vehicle", "lwir100", "night", 10000.0, 575.0),
]


@pytest.mark.parametrize(("cls", "sensor", "light", "mor", "d_hi"), AWR04_TABLE)
def test_feasibility_window_stub_tracks_awr04_table(cls, sensor, light, mor, d_hi):
    extra = {"motion": {"mode": "path", "speed_mps": 10.0, "speed_max_mps": 15.0}} if cls == "vehicle" else {}
    t = T.TargetConfig.from_dict({"class": cls, "pos": [0.0, 0.0, 0.0], **extra}, f"{cls}-01")
    sn = {"eo": P.gx40(), "nir_ill_l": P.gx40_nir(True), "lwir100": P.lwir640(100.0)}[sensor]
    w = P.feasibility_window(t, sn, light, mor, "geometric", T.P600)
    assert w is not None
    lo_exp = {"person": 320.0, "dog": 242.0, "vehicle": 170.0}[cls]
    assert abs(w[0] - lo_exp) <= 1.0
    assert abs(w[1] - d_hi) / d_hi <= 0.03


def test_feasibility_window_infeasible_cases():
    t = T.TargetConfig.from_dict({"class": "person", "pos": [0.0, 0.0, 0.0]}, "person-01")
    assert P.feasibility_window(t, P.gx40(), "night", 10000.0, "geometric", T.P600) is None        # passive EO at night
    assert P.feasibility_window(t, P.gx40_nir(False), "night", 10000.0, "geometric", T.P600) is None   # built-in NIR: about 195 m
    assert P.feasibility_window(t, P.lwir640(75.0), "night", 10000.0, "geometric", T.P600) is None  # 310 m < d_lo


def test_feasible_view_and_zoom():
    t, s, _ = _person()
    vp = np.array([[400.0, 0.0, 60.0], [2000.0, 0.0, 60.0], [50.0, 0.0, -30.0]])
    fv = P.feasible_view(vp, t, s, P.gx40(), P.EnvSample(), "R")
    assert fv.ok.tolist() == [True, False, False]
    assert fv.limiting[2] == "fov"                                          # line of sight 31 deg above the horizon, gimbal limit +30 deg
    assert 4.8 <= fv.f_mm[0] <= 48.0 and fv.ratio[0] >= 1.0
    assert P.zoom_for_level(P.gx40(), 100.0, 0.7246, 14.0 * 1.3) == pytest.approx(18.2 * 100.0 * 2.9e-3 / 0.7246, rel=1e-6)


def test_fixture_tables_match_stub():
    cases = load("fixtures/perception/ttpf_cases.json")["cases"]
    for c in cases:
        assert abs(P.ttpf(c["n"], c["n50_px"]) - c["p"]) < 1e-9
    for c in load("fixtures/targets/dc_cases.json")["cases"]:
        got = T.critical_dim(c["size_m"], math.radians(c["eps_deg"]), math.radians(c["beta_deg"]))
        assert abs(got - c["d_c_m"]) < 1e-9
