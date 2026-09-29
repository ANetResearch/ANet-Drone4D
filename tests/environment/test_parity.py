"""M07 实现对拍 golden（M07-AC-002；D1-AC-13 环境部分；AWR-03 §5.1 规则 8）。

读取 `packages/contracts/env/golden/*.json`（由 tools/contracts/gen_env_golden.py 从 env_ref.py 生成），逐例用
`awr.environment` 的实现求值，按混合容差 |a − b| ≤ atol + 1e-9*|b| 判定（位置 1e-6 m、速度 1e-9 m/s、无量纲 1e-12；
angle_deg 换算为弧度按 1e-12；exact 完全相等）。TS 端同一 golden 的对拍在 apps/web/tests/environment/parity.test.ts。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from awr.contracts.presets import PRESETS_SHA256
from awr.environment import conventions as CV
from awr.environment.anchors import advance, anchors_initial
from awr.environment.atmosphere.isa import isa
from awr.environment.atmosphere.optics import kim_q, lidar_two_way, optical_depth, sigma_at, sigma_lambda
from awr.environment.weather.derive import derive, mor_bg_from_total
from awr.environment.weather.transitions import TransitionKf, eval_env
from awr.environment.wind.gust import GustEvent, gust, gust_create, gust_expired
from awr.environment.wind.library import sector_slots
from awr.environment.wind.profile import profile, profile_cfg

ROOT = Path(__file__).resolve().parents[2]
GOLD = ROOT / "packages" / "contracts" / "env" / "golden"
GROUPS = ["conventions", "profile", "derive", "eval_env", "optical_depth", "gust", "sector_slots", "isa", "anchors"]


def close(a, b, kind: str, tol: dict) -> bool:
    if isinstance(b, list):
        return isinstance(a, (list, tuple)) and len(a) == len(b) and all(close(x, y, kind, tol) for x, y in zip(a, b, strict=True))
    if isinstance(b, dict):
        return set(a) == set(b) and all(close(a[k], b[k], kind, tol) for k in b)
    if kind == "exact" or isinstance(b, (bool, str)):
        return a == b
    if kind == "angle_deg":
        a, b, kind = math.radians(a), math.radians(b), "angle_rad"
    return abs(float(a) - float(b)) <= tol["atol"][kind] + tol["rtol"] * abs(float(b))


def kf_of(d: dict) -> TransitionKf:
    return TransitionKf(d["mode"], d["t0_ns"], d["t1_ns"], np.asarray(d["from"], float), np.asarray(d["to"], float), list(d["via"]))


def _anchors(a: dict) -> dict:
    kf = kf_of(a["kf"])
    A = anchors_initial(kf, 0)
    outs, prev = [], 0
    for k in a["checkpoints_k"]:
        advance(A, kf, prev, k)
        prev = k
        outs.append(A.to_json())
    return {"anchors": outs}


DISPATCH = {
    "from_to_uv": lambda a: dict(zip(("u_mps", "v_mps"), CV.from_to_uv(a["speed_mps"], a["dir_from_deg"]), strict=True)),
    "uv_to_from": lambda a: dict(zip(("speed_mps", "dir_from_deg", "calm"), CV.uv_to_from(a["u_mps"], a["v_mps"]), strict=True)),
    "e_n": lambda a: {"e": list(CV.e(a["dir_from_deg"])), "n": list(CV.n(a["dir_from_deg"]))},
    "shortest_arc": lambda a: {"d_deg": CV.shortest_arc(a["a_deg"], a["b_deg"])},
    "enu_to_three": lambda a: {"v": list(CV.enu_to_three(*a["v"]))},
    "three_to_enu": lambda a: {"v": list(CV.three_to_enu(*a["v"]))},
    "enu_to_ned": lambda a: {"v": list(CV.enu_to_ned(*a["v"]))},
    "profile": lambda a: {"f": profile(a["z_agl_m"], a["kind"], a["z_ref"], a["z0"], a["d"], a["alpha"])},
    "derive": lambda a: derive(a["s"]).as_dict(),
    "mor_bg_from_total": lambda a: {"mor_bg_m": mor_bg_from_total(a["mor_total_m"], a["rain_mmh"], a["snow_mmh"], a["cover"])},
    "eval_env": lambda a: {"s": [float(x) for x in eval_env(kf_of(a["kf"]), a["t_ns"])]},
    "optical_depth": lambda a: {"od": optical_depth(a["z0_agl_m"], a["rd_z"], a["len_m"], derive(a["s"]), a["fog_top_m"], a["cloud_base_m"])},
    "sigma_at": lambda a: dict(zip(("sigma_per_m", "flags"), sigma_at(a["z_agl_m"], derive(a["s"]), a["fog_top_m"], a["cloud_base_m"]),
                                   strict=True)),
    "sigma_lambda": lambda a: {"sigma_per_m": sigma_lambda(derive(a["s"]), a["lam_nm"]), "t2_100m": lidar_two_way(derive(a["s"]), a["lam_nm"], 100.0)},
    "kim_q": lambda a: {"q": kim_q(a["v2_km"])},
    "f_adv": lambda a: {"f_adv": profile_cfg(float(a["profile"]["adv_height_m"]), a["profile"])},
    "gust_create": lambda a: {"ev": gust_create(a["id"], a["t_ns"], a["amp_mps"], a["d_m"], a["dir_from_deg"], a["S_t_m"], a["speed_ref_mps"],
                                                a["bounds_min"], a["bounds_max"], a["f_adv"]).to_list()},
    "gust": lambda a: {"g_mps": gust(a["p_xy_m"], a["S_t_m"], GustEvent.from_list(a["ev"]), a["f_adv"]),
                       "expired": gust_expired(a["S_t_m"], GustEvent.from_list(a["ev"]), a["f_adv"])},
    "sector_slots": lambda a: (lambda sl: {"file_index": [x[0] for x in sl], "weight": [x[1] for x in sl], "sign": [x[2] for x in sl],
                                           "a_deg": [x[3] for x in sl]})(sector_slots(a["theta_from_deg"], a["sectors_deg"], a["antisymmetric"])),
    "isa": lambda a: isa(a["anchor_h_msl_m"] + a["z_m"], a["isa_dt_c"]),
    "advance": _anchors,
}


def gold(g: str) -> dict:
    return json.loads((GOLD / f"{g}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("g", GROUPS)
def test_golden_header_matches_presets(g: str):
    d = gold(g)
    assert d["presets_sha256"] == PRESETS_SHA256


@pytest.mark.parametrize("g", GROUPS)
def test_implementation_reproduces_golden(g: str):
    d = gold(g)
    bad = []
    for c in d["cases"]:
        got = DISPATCH[c["fn"]](c["args"])
        for k, kind in c["kinds"].items():
            if not close(got[k], c["out"][k], kind, d["tolerance"]):
                bad.append((c["fn"], k, c["args"].get("name"), got[k] if not isinstance(got[k], list) else "...", c["out"][k] if not isinstance(c["out"][k], list) else "..."))
    assert not bad, f"{len(bad)} mismatches, first: {bad[:3]}"


def test_turb_golden_if_present():
    """湍流盒采样（`awrv/turb_small.awrv` + `turb.json`，由 gen_env_golden.py 的 turb 组生成）。"""
    p = GOLD / "turb.json"
    if not p.exists():
        pytest.skip("turb golden not generated")
    from awr.environment.io import awrv
    from awr.environment.wind.turbulence import SIGMA_W_OVER_REF, TurbBox, mil_sigma_arr

    d = json.loads(p.read_text(encoding="utf-8"))
    vol = awrv.read(GOLD / d["asset"])
    box = TurbBox.from_volume(vol)
    tol = d["tolerance"]
    for c in d["cases"]:
        if c["fn"] == "turb_sample":
            got = box.sample(np.asarray([c["args"]["q_m"]], float))[0].tolist()
            assert close(got, c["out"]["b"], "dimensionless", tol), c
        elif c["fn"] == "mil_sigma":
            su, sw = mil_sigma_arr(np.asarray([c["args"]["z_agl_m"]]), c["args"]["sigma_ref_mps"])
            assert close(float(su[0]), c["out"]["sigma_u_mps"], "velocity_mps", tol)
            assert close(float(sw), c["out"]["sigma_w_mps"], "velocity_mps", tol)
    assert abs(SIGMA_W_OVER_REF - 0.5295) < 1e-4
