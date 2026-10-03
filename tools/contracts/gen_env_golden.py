#!/usr/bin/env python3
"""Environment golden generator (M07-FR-007, M07-AC-002; drafted per M07 §6.3, merged by M00).

Writes packages/contracts/env/golden/<group>.json from tools/contracts/env_ref.py (float64 oracle):
conventions, profile, derive (12 presets + 1000 random), eval_env (all routes x smooth/exp/step x 50 t, plus direct
transitions), optical_depth (200 rays x 3 states, sigma_at, Kim), gust, sector_slots, isa, anchors.
Each file header carries presets_sha256 so a stale golden is detected when presets.json changes.
Cases: {fn, args, out, kinds}; kinds select the mixed tolerance |a - b| <= atol + rtol |b| (AWR-03 §5.1 rule 8);
"exact" means equal (indices, signs, flags, booleans).

Group `turb` (M07, owner of this generator since the M07 work package): a small frozen von Karman box
(awrv/turb_small.awrv, vk_box(n=16, dx=4, L=30, seed=7) from awr.environment, f16 AWRV kind 2) and periodic trilinear
samples with the cell-centre convention evaluated by an independent float64 oracle below (M07 §6.3.8), plus the
MIL-F-8785C height scaling. Both ends read the same f16 asset (M07-AC-002 "turbulence box sampling").

Usage: python tools/contracts/gen_env_golden.py [--check]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contracts_lib as L
import env_ref as E

from awr.contracts.presets import FIELD_PATHS, NF, PRESET_IDS, PRESETS_SHA256

OUT = L.CONTRACTS / "env" / "golden"
SEED = 20260928
TOL = {"rtol": 1e-9, "atol": {"position_m": 1e-6, "velocity_mps": 1e-9, "dimensionless": 1e-12, "angle_rad": 1e-12}}
SEC = 1_000_000_000


def kind_of(key: str) -> str:
    if key.endswith("_per_m"):
        return "dimensionless"
    if key.endswith("_mps"):
        return "velocity_mps"
    if key.endswith("_deg"):
        return "angle_deg"
    if key.endswith("_m") or key in ("x0_m", "s0_m"):
        return "position_m"
    if key in ("calm", "flags", "file_index", "sign", "expired", "t_ns", "kind", "id"):
        return "exact"
    return "dimensionless"


def case(fn: str, args: dict, out: dict, kinds: dict | None = None) -> dict:
    return {"fn": fn, "args": args, "out": out, "kinds": kinds or {k: kind_of(k) for k in out}}


def conventions() -> list[dict]:
    cs = []
    for i in range(720):
        deg = i * 0.5
        for speed in (1.0, 7.5):
            u, v = E.from_to_uv(speed, deg)
            cs.append(case("from_to_uv", {"speed_mps": speed, "dir_from_deg": deg}, {"u_mps": u, "v_mps": v}))
            s, back, calm = E.uv_to_from(u, v)
            cs.append(case("uv_to_from", {"u_mps": u, "v_mps": v}, {"speed_mps": s, "dir_from_deg": back, "calm": calm}))
        ex, ey = E.e(deg)
        nx, ny = E.n(deg)
        cs.append(case("e_n", {"dir_from_deg": deg}, {"e": [ex, ey], "n": [nx, ny]}, {"e": "dimensionless", "n": "dimensionless"}))
    for u, v in ((0.0, 0.0), (5e-7, 0.0), (0.0, -9.9e-7), (1e-6, 0.0), (-3e-7, 4e-7)):
        s, back, calm = E.uv_to_from(u, v)
        cs.append(case("uv_to_from", {"u_mps": u, "v_mps": v}, {"speed_mps": s, "dir_from_deg": back, "calm": calm}))
    pairs = [(350.0, 10.0), (10.0, 350.0), (0.0, 180.0), (180.0, 0.0), (90.0, 270.0), (359.5, 0.0), (0.0, 359.5), (45.0, 45.0), (270.0, 90.5)]
    rng = np.random.default_rng(np.random.SeedSequence([SEED, 11]))
    pairs += [(float(a), float(b)) for a, b in rng.uniform(0, 360, (40, 2))]
    cs.extend(case("shortest_arc", {"a_deg": a, "b_deg": b}, {"d_deg": E.shortest_arc(a, b)}) for a, b in pairs)
    for v in rng.uniform(-500, 500, (20, 3)).tolist():
        cs.append(case("enu_to_three", {"v": v}, {"v": list(E.enu_to_three(*v))}, {"v": "position_m"}))
        cs.append(case("three_to_enu", {"v": v}, {"v": list(E.three_to_enu(*v))}, {"v": "position_m"}))
        cs.append(case("enu_to_ned", {"v": v}, {"v": list(E.enu_to_ned(*v))}, {"v": "position_m"}))
    return cs


def profile() -> list[dict]:
    cs = []
    zs = [-5.0, 0.0, 0.25, 0.5, 0.5000001, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 50.0, 100.0, 120.0, 150.0, 300.0, 1000.0]
    params = [dict(kind="log", z_ref=10.0, z0=0.5, d=0.0, alpha=0.25), dict(kind="log", z_ref=10.0, z0=0.03, d=0.0, alpha=0.25),
              dict(kind="log", z_ref=40.0, z0=1.0, d=8.0, alpha=0.25), dict(kind="power", z_ref=10.0, z0=0.5, d=0.0, alpha=0.25),
              dict(kind="power", z_ref=10.0, z0=0.5, d=0.0, alpha=0.14), dict(kind="uniform", z_ref=10.0, z0=0.5, d=0.0, alpha=0.25)]
    for p in params:
        cs.extend(case("profile", {"z_agl_m": z, **p}, {"f": E.profile(z, **p)}) for z in zs)
    return cs


def random_state(rng) -> list[float]:
    s = [0.0] * NF
    for i, f in enumerate(E.FIELDS):
        lo, hi = float(f["min"]), float(f["max"])
        if E.SPACE[i] == "log":
            s[i] = float(math.exp(rng.uniform(math.log(lo), math.log(hi))))
        elif E.SPACE[i] == "arc":
            s[i] = float(rng.uniform(0.0, 360.0))
        else:
            s[i] = float(rng.uniform(lo, hi))
    s[E.TOP] = max(s[E.TOP], s[E.BASE] + 100.0)
    # sprinkle exact zeros so the R > 0 / fog_top > 0 branches are covered
    for i in (E.RAIN, E.SNOW, E.FOG_TOP, E.DUST, E.W_MEAN, E.GUST_AMP):
        if rng.uniform() < 0.3:
            s[i] = 0.0
    return s


def derive_cases() -> list[dict]:
    cs = []
    for pid in PRESET_IDS:
        s = E.preset_vector(pid)
        cs.append(case("derive", {"preset": pid, "s": s}, E.derive(s)))
    rng = np.random.default_rng(np.random.SeedSequence([SEED, 12]))
    for _ in range(1000):
        s = random_state(rng)
        cs.append(case("derive", {"s": s}, E.derive(s)))
    # mor_bg_from_total: the authoring conversion behind presets.json (M07 §6.5)
    for mor, r, sn, cov in ((30000, 0, 0, 0.05), (3000, 6, 0, 0.95), (1200, 25, 0, 1.0), (800, 45, 0, 1.0), (1500, 0, 2, 0.95), (150, 0, 5, 1.0)):
        cs.append(case("mor_bg_from_total", {"mor_total_m": float(mor), "rain_mmh": float(r), "snow_mmh": float(sn), "cover": cov},
                       {"mor_bg_m": E.mor_bg_from_total(mor, r, sn, cov)}))
    return cs


def keyframes() -> list[tuple[str, E.Keyframe]]:
    """All routes x three modes, plus direct transitions without a route."""
    kfs = []
    t0 = 3 * SEC + 20_000_000
    base = E.default_vector()
    base[E.DIR], base[E.W_MEAN], base[E.ISA_DT], base[E.RH] = 300.0, 0.4, -5.0, 0.7
    for r in E.PRESETS["routes"]:
        a = E.overlay(base, r["from"])
        b = E.overlay(a, r["to"])
        dur = E.PRESETS["durations_s"]["preset"] * SEC
        kfs.append((f"{r['from']}->{r['to']}:smooth", E.Keyframe("smooth", t0, t0 + dur, a, b, list(r["via"]))))
        kfs.append((f"{r['from']}->{r['to']}:exp", E.Keyframe("exp", t0, E.exp_t1_ns(t0), a, b)))
        kfs.append((f"{r['from']}->{r['to']}:step", E.Keyframe("step", t0, t0, a, b)))
    # direct (no route) smooth transitions incl. wind-direction arc across north and a user edit (ui_edit 3 s)
    a = E.overlay(base, "rain")
    b = E.overlay(a, "fog")
    kfs.append(("rain->fog:smooth", E.Keyframe("smooth", t0, t0 + 30 * SEC, a, b)))
    a2 = list(b)
    a2[E.DIR] = 350.0
    b2 = list(a2)
    b2[E.DIR], b2[E.SPEED_REF], b2[E.MOR_BG] = 20.0, 12.0, 800.0
    kfs.append(("edit:dir350->20:smooth", E.Keyframe("smooth", t0, t0 + E.PRESETS["durations_s"]["ui_edit"] * SEC, a2, b2)))
    kfs.append(("edit:dir350->20:exp", E.Keyframe("exp", t0, E.exp_t1_ns(t0), a2, b2)))
    return kfs


def eval_env_cases() -> list[dict]:
    cs = []
    for name, kf in keyframes():
        span = max(kf.t1_ns - kf.t0_ns, SEC)
        ts = [kf.t0_ns - SEC, kf.t0_ns, kf.t1_ns, kf.t1_ns + SEC]
        ts += [kf.t0_ns + (span * i) // 46 for i in range(1, 47)]
        kinds = {"s": "dimensionless"}
        cs.extend(case("eval_env", {"name": name, "kf": kf.to_json(), "t_ns": int(t)}, {"s": E.eval_env(kf, int(t))}, kinds) for t in ts)
    return cs


def optics() -> list[dict]:
    cs = []
    states = []
    s = E.preset_vector("heavyRain")
    s[E.MOR_BG], s[E.FOG_TOP] = 2000.0, 60.0  # g06 §4.3 test state: haze + fog layer + precipitation
    states.append(("heavyRain+fog60", s))
    states.append(("fog", E.preset_vector("fog")))
    states.append(("clear", E.preset_vector("clear")))
    rng = np.random.default_rng(np.random.SeedSequence([SEED, 13]))
    for name, s in states:
        d = E.derive(s)
        fog_top, base = s[E.FOG_TOP], s[E.BASE]
        rays = [(float(z), float(rz), float(L)) for z, rz, L in zip(rng.uniform(1, 300, 200), rng.uniform(-1, 1, 200), rng.uniform(10, 3000, 200), strict=True)]
        rays[0] = (10.0, 0.0, 1000.0)  # horizontal
        rays[1] = (50.0, 1e-6, 500.0)  # |rd_z| below the flat-layer threshold
        rays[2] = (80.0, -1.0, 80.0)  # straight down to the ground
        cs.extend(case("optical_depth", {"state": name, "s": s, "z0_agl_m": z0, "rd_z": rz, "len_m": ln, "fog_top_m": fog_top, "cloud_base_m": base},
                       {"od": E.optical_depth(z0, rz, ln, d, fog_top, base)}) for z0, rz, ln in rays)
        for z in (0.0, 1.0, 30.0, 59.999, 60.0, 100.0, 449.0, 450.0, 800.0, 3000.0):
            sg, fl = E.sigma_at(z, d, fog_top, base)
            cs.append(case("sigma_at", {"state": name, "s": s, "z_agl_m": z, "fog_top_m": fog_top, "cloud_base_m": base},
                           {"sigma_per_m": sg, "flags": fl}))
        cs.extend(case("sigma_lambda", {"state": name, "s": s, "lam_nm": lam},
                       {"sigma_per_m": E.sigma_lambda(d, lam), "t2_100m": E.lidar_two_way(d, lam, 100.0)}) for lam in (550.0, 905.0, 1550.0))
    cs.extend(case("kim_q", {"v2_km": v}, {"q": E.kim_q(v)}) for v in (0.1, 0.5, 0.75, 1.0, 3.0, 6.0, 10.0, 50.0, 80.0))
    return cs


def gust_cases() -> list[dict]:
    cs = []
    f_adv = E.profile_cfg(float(E.DEFAULT_PROFILE["adv_height_m"]))
    cs.append(case("f_adv", {"profile": E.DEFAULT_PROFILE}, {"f_adv": f_adv}))
    bmin, bmax = [-3638.787, -3756.033], [3638.787, 3756.033]
    rng = np.random.default_rng(np.random.SeedSequence([SEED, 14]))
    events = []
    for i, (t, amp, dm, dirf, S, spd) in enumerate([(420 * SEC, 6.0, 60.0, 270.0, 1260.0, 3.0), (10 * SEC, 8.0, 120.0, 45.0, 0.0, 14.0),
                                                     (60 * SEC, 3.0, 30.0, 359.5, 500.0, 8.0), (5 * SEC, 5.0, 250.0, 180.0, 20.0, 1.5)]):
        ev = E.gust_create(i + 1, t, amp, dm, dirf, S, spd, bmin, bmax, f_adv)
        events.append((ev, S, spd))
        cs.append(case("gust_create", {"id": i + 1, "t_ns": t, "amp_mps": amp, "d_m": dm, "dir_from_deg": dirf, "S_t_m": S, "speed_ref_mps": spd,
                                       "bounds_min": bmin, "bounds_max": bmax, "f_adv": f_adv},
                       {"ev": ev.to_list()}, {"ev": "position_m"}))
    for ev, S0, _spd in events:
        # sweep: the front crosses the whole world while S grows
        travel = (ev.s_span_m + ev.lam_m) / f_adv
        for S in np.linspace(S0, S0 + travel * 1.05, 25).tolist():
            pts = [*rng.uniform(bmin, bmax, (8, 2)).tolist(), [0.0, 0.0]]
            cs.extend(case("gust", {"ev": ev.to_list(), "p_xy_m": p, "S_t_m": S, "f_adv": f_adv},
                           {"g_mps": E.gust(p, S, ev, f_adv), "expired": E.gust_expired(S, ev, f_adv)}) for p in pts)
    return cs


def sector_cases() -> list[dict]:
    cs = []
    configs = [([0.0, 30.0, 60.0, 90.0, 120.0, 150.0], True), ([i * 30.0 for i in range(12)], False), ([0.0, 22.5, 45.0, 67.5, 90.0, 112.5, 135.0, 157.5], True)]
    for secs, anti in configs:
        for th in [i * 0.5 for i in range(720)] + [-30.0, 360.0, 725.0]:
            sl = E.sector_slots(th, secs, anti)
            out = {"file_index": [x[0] for x in sl], "weight": [x[1] for x in sl], "sign": [x[2] for x in sl], "a_deg": [x[3] for x in sl]}
            cs.append(case("sector_slots", {"theta_from_deg": th, "sectors_deg": secs, "antisymmetric": anti}, out,
                           {"file_index": "exact", "weight": "dimensionless", "sign": "exact", "a_deg": "angle_deg"}))
    return cs


def isa_cases() -> list[dict]:
    cs = []
    for h_anchor in (0.0, 12.2, 39.5, 1500.0):
        for z in (-50.0, 0.0, 30.0, 120.0, 500.0, 3000.0):
            cs.extend(case("isa", {"anchor_h_msl_m": h_anchor, "z_m": z, "isa_dt_c": dt}, E.isa(h_anchor + z, dt),
                           {"temperature_c": "dimensionless", "pressure_pa": "dimensionless", "rho_kgm3": "dimensionless"})
                      for dt in (-40.0, -5.0, 0.0, 15.0, 40.0))
    return cs


def anchor_cases() -> list[dict]:
    cs = []
    H = E.H_NS
    base = E.default_vector()
    base[E.W_MEAN] = 0.3
    steady_rain = E.overlay(base, "rain")
    t0 = 0
    kfs = [("steady:rain", E.Keyframe("step", t0, t0, steady_rain, steady_rain)),
           ("clear->thunderstorm:smooth", E.Keyframe("smooth", 2 * SEC, 32 * SEC, E.overlay(base, "clear"), E.overlay(base, "thunderstorm"),
                                                     E.route_for("clear", "thunderstorm"))),
           ("blizzard->clear:exp", E.Keyframe("exp", 1 * SEC, E.exp_t1_ns(1 * SEC), E.overlay(base, "blizzard"), E.overlay(base, "clear"))),
           ("fog->heavyRain:step", E.Keyframe("step", 0, 0, E.overlay(base, "fog"), E.overlay(base, "heavyRain")))]
    checkpoints = [0, 1, 10, 100, 500, 1500, 3000]
    for name, kf in kfs:
        A = E.anchors_initial(kf, 0)
        outs = []
        prev = 0
        for k in checkpoints:
            E.advance(A, kf, prev, k)
            prev = k
            outs.append(A.to_json())
        init = E.anchors_initial(kf, 0).to_json()
        cs.append(case("advance", {"name": name, "kf": kf.to_json(), "grid_ns": H, "initial": init, "checkpoints_k": checkpoints},
                       {"anchors": outs}, {"anchors": "position_m"}))
    return cs


TURB_ASSET = "awrv/turb_small.awrv"


def _turb_volume():
    from awr.environment.io import awrv
    from awr.environment.io.assets import turb_params
    from awr.environment.wind.turbulence import box_to_rgba, vk_box

    n, dx, L, seed = 16, 4.0, 30.0, 7
    return awrv.AwrvVolume(awrv.KIND["turb_box"], box_to_rgba(vk_box(n, dx, L, seed)), (0.0, 0.0, 0.0), (dx, dx, dx), float("nan"), 1.0,
                           awrv.field_version(turb_params(seed, n, dx, L)))


def _trilerp_periodic(rgba: np.ndarray, dx: float, q: list[float]) -> list[float]:
    """Oracle: g = q/dx - 0.5, periodic wrap, weights from the fractional part (cell (i) centre at (i + 0.5) dx)."""
    nz, ny, nx = rgba.shape[:3]
    g = [q[0] / dx - 0.5, q[1] / dx - 0.5, q[2] / dx - 0.5]
    f0 = [math.floor(v) for v in g]
    fr = [g[i] - f0[i] for i in range(3)]
    out = [0.0, 0.0, 0.0]
    for dz in (0, 1):
        for dy in (0, 1):
            for dxx in (0, 1):
                w = (fr[2] if dz else 1 - fr[2]) * (fr[1] if dy else 1 - fr[1]) * (fr[0] if dxx else 1 - fr[0])
                i, j, k = (f0[0] + dxx) % nx, (f0[1] + dy) % ny, (f0[2] + dz) % nz
                for c in range(3):
                    out[c] += w * float(rgba[k, j, i, c])
    return out


def turb_cases() -> list[dict]:
    vol = _turb_volume()
    rgba = vol.data
    rng = np.random.default_rng(np.random.SeedSequence([SEED, 15]))
    pts = [*rng.uniform(-300.0, 300.0, (200, 3)).tolist(), [0.0, 0.0, 0.0], [2.0, 2.0, 2.0], [64.0, -2.0, 30.0], [-0.001, 63.999, 1e-9]]
    cs = [case("turb_sample", {"q_m": q}, {"b": _trilerp_periodic(rgba, 4.0, q)}, {"b": "dimensionless"}) for q in pts]
    for z in (-3.0, 0.0, 1.0, 3.048, 10.0, 30.0, 50.0, 120.0, 300.0):
        for sref in (0.5, 1.45, 5.0):
            sw = sref * (0.177 + 0.000823 * (10.0 / 0.3048)) ** 0.4
            h_ft = max(z / 0.3048, 10.0)
            cs.append(case("mil_sigma", {"z_agl_m": z, "sigma_ref_mps": sref},
                           {"sigma_u_mps": sw / (0.177 + 0.000823 * h_ft) ** 0.4, "sigma_w_mps": sw}))
    return cs


GROUPS = {"conventions": conventions, "profile": profile, "derive": derive_cases, "eval_env": eval_env_cases, "optical_depth": optics,
          "gust": gust_cases, "sector_slots": sector_cases, "isa": isa_cases, "anchors": anchor_cases, "turb": turb_cases}


def build() -> dict[Path, bytes]:
    files = {}
    for g, fn in GROUPS.items():
        doc = {"schema": "awr.golden.env.v1", "generator": "tools/contracts/gen_env_golden.py", "group": g, "presets_sha256": PRESETS_SHA256,
               "fields": list(FIELD_PATHS), "seed": SEED, "tolerance": TOL, "cases": fn()}
        if g == "turb":
            doc["asset"] = TURB_ASSET
        files[OUT / f"{g}.json"] = (json.dumps(doc, separators=(",", ":"), allow_nan=False) + "\n").encode()
    from awr.environment.io import awrv

    files[OUT / TURB_ASSET] = awrv.encode(_turb_volume())
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="environment golden generator (M07 pure functions)")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    diffs, total = 0, 0
    for p, data in sorted(build().items()):
        if p.suffix == ".json":
            total += len(json.loads(data)["cases"])
        cur = p.read_bytes() if p.exists() else None
        if not L.golden_equivalent(cur, data):
            diffs += 1
            if args.check:
                print(f"out of date: {p.relative_to(L.ROOT)}")
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                print(f"wrote {p.relative_to(L.ROOT)}")
    if args.check and diffs:
        print("gen_env_golden.py --check: golden differs from env_ref.py / presets.json; run python tools/contracts/gen_env_golden.py", file=sys.stderr)
        return 1
    print(f"gen_env_golden.py: {total} cases, {'up to date' if not diffs else f'{diffs} file(s) updated'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
