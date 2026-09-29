"""presets.json and environment golden (M07-AC-001, M07-AC-002; D1-AC-13 contract part).

env_ref.py (tools/contracts) is the oracle; these tests check that the golden is self-consistent with presets.json,
covers what M07-AC-002 lists, and that M07's implementation (when importable) reproduces it.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctlib
import env_ref as E

from awr.contracts import presets as P

GOLD = ctlib.CONTRACTS / "env" / "golden"
GROUPS = ["conventions", "profile", "derive", "eval_env", "optical_depth", "gust", "sector_slots", "isa", "anchors"]
# r16 §3.7.1 author table: total MOR per preset (M07 §6.5)
MOR_TOTAL = {"clear": 30000, "partlyCloudy": 20000, "overcast": 12000, "lightRain": 6000, "rain": 3000, "heavyRain": 1200, "thunderstorm": 800,
             "fog": 150, "haze": 3000, "snow": 1500, "blizzard": 150, "sandstorm": 400}


def gold(g: str) -> dict:
    return json.loads((GOLD / f"{g}.json").read_text(encoding="utf-8"))


def close(a, b, kind: str, tol: dict) -> bool:
    if isinstance(b, list):
        return len(a) == len(b) and all(close(x, y, kind, tol) for x, y in zip(a, b, strict=True))
    if isinstance(b, dict):
        return set(a) == set(b) and all(close(a[k], b[k], kind, tol) for k in b)
    if kind == "exact" or isinstance(b, (bool, str)):
        return a == b
    if kind == "angle_deg":
        a, b, kind = math.radians(a), math.radians(b), "angle_rad"
    return abs(a - b) <= tol["atol"][kind] + tol["rtol"] * abs(b)


def test_presets_bytes_and_sha256():
    raw = (ctlib.CONTRACTS / "env" / "presets.json").read_bytes()
    assert raw == P.PRESETS_JSON
    assert hashlib.sha256(raw).hexdigest() == P.PRESETS_SHA256


def test_presets_content():
    d = P.PRESETS
    assert len(d["presets"]) == 12 and tuple(p["id"] for p in d["presets"]) == P.PRESET_IDS
    assert len(d["fields"]) == P.NF == 21
    user = {f["path"] for f in d["fields"] if f.get("user_axis")}
    assert user == {"wind.dir_from_deg", "wind.w_mean_mps", "atmosphere.isa_dt_c", "atmosphere.rh"}
    for p in d["presets"]:
        snap = E.preset_snapshot(p["id"])
        assert len(snap) == 17, p["id"]
        v = E.preset_vector(p["id"])
        for i, f in enumerate(d["fields"]):
            assert f["min"] <= v[i] <= f["max"], (p["id"], f["path"])
        assert v[E.TOP] >= v[E.BASE] + 100
    for r in d["routes"]:
        assert r["from"] in P.PRESET_IDS and r["to"] in P.PRESET_IDS and set(r["via"]) <= set(P.PRESET_IDS) and len(r["via"]) <= 4


@pytest.mark.parametrize("pid", sorted(MOR_TOTAL))
def test_preset_total_mor_reproduces_author_table(pid: str):
    d = E.derive(E.preset_vector(pid))
    assert abs(d["mor_m"] - MOR_TOTAL[pid]) / MOR_TOTAL[pid] < 5e-4


def test_derive_spot_values():
    assert abs(E.profile(40.0) - 1.463) < 5e-4 and abs(E.profile(50.0) - 1.537) < 5e-4 and abs(E.profile(120.0) - 1.829) < 5e-4
    for R, v in ((0.1, 2.5), (6.0, 5.3), (45.0, 6.8)):
        s = E.default_vector()
        s[E.COVER], s[E.RAIN] = 1.0, R
        assert abs(E.derive(s)["v_rain_mps"] - v) < 0.06
    assert math.log(20.0) == E.K_MOR


@pytest.mark.parametrize("g", GROUPS)
def test_golden_header(g: str):
    d = gold(g)
    assert d["schema"] == "awr.golden.env.v1" and d["presets_sha256"] == P.PRESETS_SHA256 and d["fields"] == list(P.FIELD_PATHS)
    assert d["tolerance"]["rtol"] == 1e-9 and d["tolerance"]["atol"]["position_m"] == 1e-6 and d["tolerance"]["atol"]["velocity_mps"] == 1e-9
    assert d["cases"]


def test_golden_coverage_per_m07_ac_002():
    der = gold("derive")["cases"]
    assert sum(1 for c in der if c["fn"] == "derive" and "preset" in c["args"]) == 12
    assert sum(1 for c in der if c["fn"] == "derive" and "preset" not in c["args"]) == 1000
    ev = gold("eval_env")["cases"]
    names = {c["args"]["name"] for c in ev}
    for r in P.PRESETS["routes"]:
        for mode in ("smooth", "exp", "step"):
            assert sum(1 for c in ev if c["args"]["name"] == f"{r['from']}->{r['to']}:{mode}") >= 50
    assert len(names) >= 3 * len(P.PRESETS["routes"])
    od = [c for c in gold("optical_depth")["cases"] if c["fn"] == "optical_depth"]
    assert len(od) == 600 and len({c["args"]["state"] for c in od}) == 3
    conv = gold("conventions")["cases"]
    assert {c["args"]["dir_from_deg"] for c in conv if c["fn"] == "e_n"} == {i * 0.5 for i in range(720)}
    assert any(c["fn"] == "uv_to_from" and c["out"]["calm"] for c in conv)


_DISPATCH = {
    "from_to_uv": lambda a: dict(zip(("u_mps", "v_mps"), E.from_to_uv(a["speed_mps"], a["dir_from_deg"]), strict=True)),
    "uv_to_from": lambda a: dict(zip(("speed_mps", "dir_from_deg", "calm"), E.uv_to_from(a["u_mps"], a["v_mps"]), strict=True)),
    "e_n": lambda a: {"e": list(E.e(a["dir_from_deg"])), "n": list(E.n(a["dir_from_deg"]))},
    "shortest_arc": lambda a: {"d_deg": E.shortest_arc(a["a_deg"], a["b_deg"])},
    "enu_to_three": lambda a: {"v": list(E.enu_to_three(*a["v"]))},
    "three_to_enu": lambda a: {"v": list(E.three_to_enu(*a["v"]))},
    "enu_to_ned": lambda a: {"v": list(E.enu_to_ned(*a["v"]))},
    "profile": lambda a: {"f": E.profile(a["z_agl_m"], a["kind"], a["z_ref"], a["z0"], a["d"], a["alpha"])},
    "derive": lambda a: E.derive(a["s"]),
    "mor_bg_from_total": lambda a: {"mor_bg_m": E.mor_bg_from_total(a["mor_total_m"], a["rain_mmh"], a["snow_mmh"], a["cover"])},
    "eval_env": lambda a: {"s": E.eval_env(E.Keyframe(a["kf"]["mode"], a["kf"]["t0_ns"], a["kf"]["t1_ns"], a["kf"]["from"], a["kf"]["to"], a["kf"]["via"]), a["t_ns"])},
    "optical_depth": lambda a: {"od": E.optical_depth(a["z0_agl_m"], a["rd_z"], a["len_m"], E.derive(a["s"]), a["fog_top_m"], a["cloud_base_m"])},
    "sigma_at": lambda a: dict(zip(("sigma_per_m", "flags"), E.sigma_at(a["z_agl_m"], E.derive(a["s"]), a["fog_top_m"], a["cloud_base_m"]), strict=True)),
    "sigma_lambda": lambda a: {"sigma_per_m": E.sigma_lambda(E.derive(a["s"]), a["lam_nm"]), "t2_100m": E.lidar_two_way(E.derive(a["s"]), a["lam_nm"], 100.0)},
    "kim_q": lambda a: {"q": E.kim_q(a["v2_km"])},
    "f_adv": lambda a: {"f_adv": E.profile_cfg(float(a["profile"]["adv_height_m"]), a["profile"])},
    "gust_create": lambda a: {"ev": E.gust_create(a["id"], a["t_ns"], a["amp_mps"], a["d_m"], a["dir_from_deg"], a["S_t_m"], a["speed_ref_mps"],
                                                  a["bounds_min"], a["bounds_max"], a["f_adv"]).to_list()},
    "gust": lambda a: {"g_mps": E.gust(a["p_xy_m"], a["S_t_m"], E.GustEvent(*a["ev"]), a["f_adv"]), "expired": E.gust_expired(a["S_t_m"], E.GustEvent(*a["ev"]), a["f_adv"])},
    "sector_slots": lambda a: (lambda sl: {"file_index": [x[0] for x in sl], "weight": [x[1] for x in sl], "sign": [x[2] for x in sl], "a_deg": [x[3] for x in sl]})(
        E.sector_slots(a["theta_from_deg"], a["sectors_deg"], a["antisymmetric"])),
    "isa": lambda a: E.isa(a["anchor_h_msl_m"] + a["z_m"], a["isa_dt_c"]),
}


def _advance(a: dict) -> dict:
    kf = E.Keyframe(a["kf"]["mode"], a["kf"]["t0_ns"], a["kf"]["t1_ns"], a["kf"]["from"], a["kf"]["to"], a["kf"]["via"])
    A = E.anchors_initial(kf, 0)
    outs, prev = [], 0
    for k in a["checkpoints_k"]:
        E.advance(A, kf, prev, k)
        prev = k
        outs.append(A.to_json())
    return {"anchors": outs}


@pytest.mark.parametrize("g", GROUPS)
def test_reference_reproduces_golden(g: str):
    d = gold(g)
    bad = []
    for c in d["cases"]:
        got = _advance(c["args"]) if c["fn"] == "advance" else _DISPATCH[c["fn"]](c["args"])
        for k, kind in c["kinds"].items():
            if not close(got[k], c["out"][k], kind, d["tolerance"]):
                bad.append((c["fn"], k))
    assert not bad, bad[:5]


def test_optical_depth_closed_form_vs_numeric():
    """M07-FR-006: relative error against a 20000-segment midpoint integral <= 2e-4."""
    worst = 0.0
    for c in [c for c in gold("optical_depth")["cases"] if c["fn"] == "optical_depth"][::10]:
        a = c["args"]
        d = E.derive(a["s"])
        z0, rz, L = a["z0_agl_m"], a["rd_z"], a["len_m"]
        n = 20000
        h = L / n
        num = 0.0
        for i in range(n):
            z = z0 + rz * (i + 0.5) * h
            s = d["sigma_haze0"] * math.exp(-z / E.H_HAZE)
            if z < a["fog_top_m"]:
                s += d["sigma_fog"]
            if z < a["cloud_base_m"]:
                s += d["sigma_precip"]
            num += s * h
        worst = max(worst, abs(c["out"]["od"] - num) / max(num, 1e-12))
    assert worst <= 2e-4


def test_eval_env_continuity_and_endpoints():
    for c in gold("eval_env")["cases"]:
        kf = c["args"]["kf"]
        t = c["args"]["t_ns"]
        if t <= kf["t0_ns"] and kf["mode"] != "step":
            assert c["out"]["s"] == kf["from"]
        if t >= kf["t1_ns"] or kf["mode"] == "step":
            assert c["out"]["s"] == kf["to"]


def test_m07_implementation_if_present():
    try:
        conv = importlib.import_module("awr.environment.conventions")
    except ImportError:
        pytest.skip("awr.environment.conventions (M07) not implemented yet")
    d = gold("conventions")
    for c in d["cases"]:
        if c["fn"] == "from_to_uv" and hasattr(conv, "from_to_uv"):
            got = dict(zip(("u_mps", "v_mps"), conv.from_to_uv(c["args"]["speed_mps"], c["args"]["dir_from_deg"]), strict=True))
            assert close(got, c["out"], "velocity_mps", d["tolerance"])
