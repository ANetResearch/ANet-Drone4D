"""frames.py golden and property tests (M02-AC-001, 002, 003, 005, 007, 008, 010, 014; D1-AC-13)."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from georef_golden import close, flat, load_group

from awr.world.georef import frames as F

ANCHOR_KEYS = ("latDeg", "lonDeg", "hEllipsoidM")


def _anchor(a: dict) -> F.Anchor:
    return F.Anchor("synthetic", a.get("datum", "WGS84"), a["latDeg"], a["lonDeg"], a["hEllipsoidM"], a["hEllipsoidM"])


def _ell(d: str) -> F.Ellipsoid:
    return F.CGCS2000 if d == "CGCS2000" else F.WGS84


def run(fn: str, a: dict):
    if fn == "lla_to_ecef":
        return {"p": F.lla_to_ecef(a["lat_deg"], a["lon_deg"], a["h_m"], _ell(a["datum"]))}
    if fn == "ecef_to_lla":
        la, lo, h = F.ecef_to_lla(np.array(a["p"]), _ell(a["datum"]))
        return {"lat_deg": la, "lon_deg": lo, "h_m": h}
    if fn == "world_to_lla":
        la, lo, h = F.world_to_lla(np.array(a["p"]), _anchor(a["anchor"]))
        return {"lat_deg": la, "lon_deg": lo, "h_m": h}
    if fn == "lla_to_world":
        return {"p": F.lla_to_world(a["lat_deg"], a["lon_deg"], a["h_m"], _anchor(a["anchor"]))}
    if fn == "T_ecef_world":
        return {"T": F.T_ecef_world(_anchor(a["anchor"]))}
    if fn == "enu_to_ned":
        return {"v": F.enu_to_ned(a["v"])}
    if fn == "flu_to_frd":
        return {"v": F.flu_to_frd(a["v"])}
    if fn == "q_enuflu_from_nedfrd":
        return {"q_xyzw": F.q_enuflu_from_nedfrd(a["q_wxyz"])}
    if fn == "q_nedfrd_from_enuflu":
        return {"q_wxyz": F.q_nedfrd_from_enuflu(a["q_xyzw"])}
    if fn == "yaw_ned_from_enu":
        return {"yaw": F.yaw_ned_from_enu(a["psi"])}
    if fn == "heading_deg":
        return {"deg": F.heading_deg(a["psi"])}
    if fn == "yaw_enu_from_heading_deg":
        return {"yaw": F.yaw_enu_from_heading_deg(a["h_deg"])}
    if fn == "enu_to_three":
        return {"v": F.enu_to_three(a["v"])}
    if fn == "three_to_enu":
        return {"v": F.three_to_enu(a["v"])}
    if fn == "quat_enu_to_three":
        return {"q_xyzw": F.quat_enu_to_three(a["q_xyzw"])}
    if fn == "ue_cm_to_world":
        return {"p": F.ue_cm_to_world(a["p_ue_cm"], a["t_world_m"])}
    if fn == "ue_rot_to_q":
        q = F.ue_rot_to_q(a["pitch_deg"], a["roll_deg"], a["yaw_deg"])
        return {"q_xyzw": q, "forward": F.quat_to_mat(np.array(q))[:, 0]}
    if fn.startswith("sim3_"):
        s = F.Sim3.from_json(a["a"])
        if fn == "sim3_interpolate":
            c = s.interpolate(F.Sim3.from_json(a["b"]), a["u"])
            return {"s": c.s, "q": c.q, "t": c.t}
        if fn == "sim3_apply":
            return {"y": s.apply(a["x"])}
        return s.compose(F.Sim3.from_json(a["b"])).to_json()
    if fn == "px4_reproject":
        la, lo = F.px4_reproject(a["x_n"], a["y_e"], a["ref_lat"], a["ref_lon"])
        return {"lat_deg": la, "lon_deg": lo}
    if fn == "px4_project":
        x, y = F.px4_project(a["lat_deg"], a["lon_deg"], a["ref_lat"], a["ref_lon"])
        return {"x_n": x, "y_e": y}
    if fn == "world_from_px4_local":
        return {"p": F.world_from_px4_local(np.array(a["p_ned"]), F.Px4Origin(*a["origin"]), _anchor(a["anchor"]), geoid=a["geoid"])}
    if fn == "px4_local_from_world":
        return {"p_ned": F.px4_local_from_world(np.array(a["p_world"]), F.Px4Origin(*a["origin"]), _anchor(a["anchor"]), geoid=a["geoid"])}
    if fn == "sih_loc_for_spawn":
        s = F.sih_loc_for_spawn(np.array(a["spawn_world"]), a["yaw_enu_rad"], _anchor(a["anchor"]))
        return {"params": s.params, "quant_offset_m": s.quant_offset_m}
    raise KeyError(fn)


@pytest.mark.parametrize("group", ["geodesy", "enu_ned", "three", "ue", "sim3", "local_px4"])
def test_golden_group(group):
    g = load_group(group)
    tol = g["tolerance"]
    bad = []
    for c in g["cases"]:
        got = run(c["fn"], c["args"])
        for key, kind in c["kinds"].items():
            exp, act = flat(c["out"][key]), flat(np.asarray(got[key]).tolist() if not isinstance(got[key], dict) else got[key])
            if len(exp) != len(act) or not all(close(float(x), float(y), kind, tol) for x, y in zip(act, exp, strict=True)):
                bad.append((c["fn"], key, exp[:3], act[:3]))
    assert not bad, bad[:5]


def test_case_count_meets_ac001():
    n = sum(len(load_group(g)["cases"]) for g in ["geodesy", "enu_ned", "three", "ue", "sim3"])
    assert n >= 3000


def test_lla_ecef_round_trip_1e6_points():
    """M02-AC-002: round trip <= 1e-6 m over 10^6 random points."""
    rng = np.random.default_rng(7)
    n = 1_000_000
    lat, lon, h = rng.uniform(-89.9999, 89.9999, n), rng.uniform(-180, 180, n), rng.uniform(-500, 2e4, n)
    p = F.lla_to_ecef(lat, lon, h)
    la2, lo2, h2 = F.ecef_to_lla(p)
    p2 = F.lla_to_ecef(la2, lo2, h2)
    assert np.abs(p2 - p).max() <= 1e-6


def _colmap_ecef_to_lla(x, y, z, ell=F.WGS84):
    """COLMAP gps.cc L116-L151 iterative oracle (point-wise)."""
    a, e2 = ell.a, ell.e2
    xy = math.hypot(x, y)
    lat = math.atan2(z, xy)
    alt = 0.0
    for _ in range(100):
        prev_lat, prev_alt = lat, alt
        n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        alt = xy / math.cos(lat) - n
        lat = math.atan2(z, xy * (1 - e2 * n / (n + alt)))
        if abs(prev_lat - lat) < 1e-12 and abs(prev_alt - alt) < 1e-12:
            break
    return math.degrees(lat), math.degrees(math.atan2(y, x)), alt


def test_closed_form_vs_colmap_iteration():
    """M02-AC-003: height <= 1e-6 m, latitude <= 1e-12 rad."""
    rng = np.random.default_rng(3)
    for _ in range(2000):
        la, lo, h = rng.uniform(-89, 89), rng.uniform(-180, 180), rng.uniform(-500, 2e4)
        p = F.lla_to_ecef(la, lo, h)
        a1, _, h1 = F.ecef_to_lla(p)
        a2, _, h2 = _colmap_ecef_to_lla(*p)
        assert abs(float(h1) - h2) <= 1e-6
        assert abs(math.radians(float(a1) - a2)) <= 1e-12


def test_quaternion_formula_matches_matrix_form_and_is_involution():
    """M02-AC-005: q' = (1/sqrt2)(w+z, x+y, x-y, w-z) equals R_enu_flu = T R_ned_frd B; involution."""
    T = np.array([[0, 1, 0], [1, 0, 0], [0, 0, -1.0]])
    B = np.diag([1.0, -1.0, -1.0])
    rng = np.random.default_rng(1)
    for _ in range(10_000):
        q = rng.normal(size=4)
        q /= np.linalg.norm(q)  # wxyz, NED/FRD
        r_ned = F.quat_to_mat(np.array([q[1], q[2], q[3], q[0]]))
        qe = F.q_enuflu_from_nedfrd(q)
        assert np.abs(F.quat_to_mat(qe) - T @ r_ned @ B).max() <= 1e-12
        back = F.q_nedfrd_from_enuflu(qe)
        assert np.abs(np.abs(np.dot(back, q)) - 1.0) <= 1e-12


def test_batch_tap_matches_scalar_and_allocates_nothing():
    import tracemalloc

    rng = np.random.default_rng(2)
    n = 1000
    p, v = rng.normal(size=(n, 3)), rng.normal(size=(n, 3))
    q = rng.normal(size=(n, 4))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    op, ov, oq = np.empty((n, 3)), np.empty((n, 3)), np.empty((n, 4))
    F.ned_frd_to_enu_flu_batch(p, v, q, op, ov, oq)
    assert np.allclose(op, F.ned_to_enu(p)) and np.allclose(ov, F.ned_to_enu(v))
    canon = np.where(oq[:, 3:4] < 0, -oq, oq)
    assert np.allclose(canon, F.q_enuflu_from_nedfrd(q))
    tracemalloc.start()
    s1 = tracemalloc.take_snapshot()
    for _ in range(5):
        F.ned_frd_to_enu_flu_batch(p, v, q, op, ov, oq)
    s2 = tracemalloc.take_snapshot()
    tracemalloc.stop()
    grown = sum(st.size_diff for st in s2.compare_to(s1, "filename") if st.size_diff > 0 and "frames.py" in str(st.traceback))
    assert grown < 4096


def test_three_mapping_matches_worldlayer_rotation():
    """M02-AC-007 (Python side): R_THREE_ENU equals rotation about x by -pi/2."""
    c, s = math.cos(-math.pi / 2), math.sin(-math.pi / 2)
    rx = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    assert np.abs(rx - F.R_THREE_ENU).max() <= 1e-15
    v = np.array([3.0, 4.0, 5.0])
    assert np.allclose(F.enu_to_three(v), F.R_THREE_ENU @ v)
    assert np.allclose(F.three_to_enu(F.enu_to_three(v)), v)


def test_ue_forward_axes():
    """M02-AC-008: yaw 0 -> north (0,1,0); yaw 90 -> east (1,0,0); pitch 90 -> down (0,0,-1)."""
    for (p, r, y), fwd in [((0, 0, 0), (0, 1, 0)), ((0, 0, 90), (1, 0, 0)), ((90, 0, 0), (0, 0, -1))]:
        assert np.abs(F.quat_to_mat(np.array(F.ue_rot_to_q(p, r, y)))[:, 0] - fwd).max() <= 1e-12


def test_camera_frames():
    """AWR-03 §5.1 rule 7: RUB = RDF diag(1,-1,-1); optical axis = body x."""
    assert np.array_equal(F.R_FLU_RDF @ np.diag([1.0, -1.0, -1.0]), F.R_FLU_RUB)
    assert np.array_equal(F.R_FLU_RDF[:, 2], [1.0, 0.0, 0.0])
    T = np.eye(4)
    T[:3, :3] = F.R_FLU_RDF
    assert np.array_equal(F.opengl_from_opencv(T)[:3, :3], F.R_FLU_RUB)
    w2c = F.c2w_from_w2c(T)
    assert np.allclose(F.c2w_from_w2c(w2c), T)


def test_sim3_algebra_and_gtsam():
    """M02-AC-010."""
    rng = np.random.default_rng(4)
    for _ in range(200):
        q = rng.normal(size=4)
        a = F.Sim3(float(rng.uniform(0.5, 2)), tuple(q / np.linalg.norm(q)), tuple(rng.normal(size=3) * 10))
        q2 = rng.normal(size=4)
        b = F.Sim3(float(rng.uniform(0.5, 2)), tuple(q2 / np.linalg.norm(q2)), tuple(rng.normal(size=3) * 10))
        x = rng.normal(size=3) * 20
        assert np.allclose(a.compose(b).apply(x), a.apply(b.apply(x)), rtol=1e-12, atol=1e-9)
        assert np.allclose(a.inverse().apply(a.apply(x)), x, rtol=1e-12, atol=1e-9)
        assert np.allclose(a.to_matrix() @ np.r_[x, 1.0], np.r_[a.apply(x), 1.0], rtol=1e-12, atol=1e-9)
        m = F.Sim3.from_matrix(a.to_matrix())
        assert abs(m.s - a.s) <= 1e-12 * a.s and np.allclose(m.t, a.t, atol=1e-9)
        s, qq, tg = a.to_gtsam()
        assert np.allclose(np.asarray(tg) * s, a.t)
        assert F.Sim3.from_gtsam(s, qq, tg).t == pytest.approx(a.t)
        assert a.interpolate(b, 0.0) == a and a.interpolate(b, 1.0) == b


def test_mat4_contract_errors():
    with pytest.raises(F.FrameContractError):
        F.mat4_from_json([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 1, 1]])
    with pytest.raises(F.FrameContractError):
        F.mat4_from_json(np.diag([2.0, 1.0, 1.0, 1.0]).tolist(), rigid=True)
    assert F.mat4_to_json(np.eye(4))[3] == [0.0, 0.0, 0.0, 1.0]


def test_heading_and_yaw():
    assert F.heading_deg(0.0) == 90.0
    assert abs(F.heading_deg(math.pi / 2)) < 1e-12 or abs(F.heading_deg(math.pi / 2) - 360.0) < 1e-12
    assert abs(F.yaw_ned_from_enu(0.0) - math.pi / 2) < 1e-15
    assert F.wrap_pi(-math.pi) == math.pi


def test_precision_report_matches_g03():
    """M02-AC-014: San Francisco extent -> maxRadiusM 5229.6, curvatureDropM 2.146, float32UlpMm 0.2441."""
    r = F.precision_report([-3638.787, -3756.033, -114.549], [3638.787, 3756.033, 443.2])
    assert r == {"maxRadiusM": 5229.6, "curvatureDropM": 2.146, "float32UlpMm": 0.2441}


def test_t_ecef_world_matches_g03_instance():
    """M02-AC-014: San Francisco T_ecef_world of the g03 instance (6-decimal rounding)."""
    a = F.Anchor("synthetic", "WGS84", 37.7791216, -122.4212116, 39.5, 39.5)
    exp = [[0.844129, 0.328449, -0.423753, -2706172.418859], [-0.536139, 0.51713, -0.667182, -4260757.978574],
           [0, 0.790378, 0.612619, 3886120.042606], [0, 0, 0, 1]]
    assert np.abs(np.round(np.array(F.T_ecef_world(a)), 6) - np.array(exp)).max() <= 1e-6


def test_pyproj_cross_check():
    """M02-AC-004 (P2): pyproj geocent <-> geodetic agrees within 1e-6 m (skipped when pyproj is missing)."""
    pyproj = pytest.importorskip("pyproj")
    tr = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    rng = np.random.default_rng(9)
    for _ in range(200):
        la, lo, h = rng.uniform(-80, 80), rng.uniform(-180, 180), rng.uniform(-100, 5000)
        x, y, z = tr.transform(lo, la, h)
        assert np.abs(F.lla_to_ecef(la, lo, h) - [x, y, z]).max() <= 1e-6
