#!/usr/bin/env python3
"""Frame-conversion golden (M02-FR-011; AWR-18 §8.3): Python frames.py / time.py is the only oracle.

Writes packages/contracts/golden/frames/<group>.json for groups geodesy, enu_ned, three, ue, sim3 (Python and TS)
and local_px4, time (Python only; TS does not implement PX4 projection or time scales, M02 §7.2).
Each case: {fn, args, out, kinds, ts}; kinds select the mixed tolerance |a - b| <= atol + rtol |b| (AWR-03 §5.1 rule 8).

Usage: python tools/contracts/gen_frames_golden.py [--check]
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contracts_lib as L

from awr.world.georef import frames as F
from awr.world.georef import time as T

OUT = L.CONTRACTS / "golden" / "frames"
SEED = 20260928
TOL = {"rtol": 1e-9, "atol": {"position_m": 1e-6, "angle_rad": 1e-12, "velocity_mps": 1e-9, "dimensionless": 1e-12}}
# six illustrative city anchors (g03 instances) and Hefei (M02 §6.4.2)
ANCHORS = {
    "shenzhen": (22.5160584, 113.9432472, 12.2), "shanghai": (31.2281892, 121.5316942, 4.0), "newyork": (40.7130611, -74.0023445, 6.3),
    "sanfrancisco": (37.7791216, -122.4212116, 39.5), "suzhou": (31.3, 120.62, 3.0), "chicago": (41.8841302, -87.6225307, 178.9),
    "hefei": (31.8206, 117.2272, 30.0),
}


def anchor(name: str, datum: str = "WGS84") -> F.Anchor:
    la, lo, h = ANCHORS[name]
    return F.Anchor("synthetic", datum, la, lo, h, h)


def ajs(name: str, datum: str = "WGS84") -> dict:
    la, lo, h = ANCHORS[name]
    return {"latDeg": la, "lonDeg": lo, "hEllipsoidM": h, "datum": datum}


def fl(x) -> float:
    return float(x)


def v3(a) -> list[float]:
    return [float(v) for v in np.asarray(a).reshape(-1)]


def case(fn: str, args: dict, out: dict, kinds: dict, ts: bool = True) -> dict:
    return {"fn": fn, "args": args, "out": out, "kinds": kinds, "ts": ts}


def rand_quat(rng) -> np.ndarray:
    q = rng.normal(size=4)
    return q / np.linalg.norm(q)


def geodesy(rng) -> list[dict]:
    cs = []
    lat = np.r_[rng.uniform(-89.9999, 89.9999, 290), [89.9999, -89.9999, 0.0, 0.0, 45.0, -45.0, 89.0, 1e-9, 60.0, 30.0]]
    lon = np.r_[rng.uniform(-180.0, 180.0, 290), [0.0, 180.0, -180.0, 180.0, 90.0, -90.0, 179.9999, 0.0, -179.9999, 1e-9]]
    h = np.r_[rng.uniform(-500.0, 2e4, 290), [-500.0, 2e4, 0.0, 0.0, 100.0, 8848.0, -400.0, 0.0, 1e3, 2e4]]
    for datum, ell in (("WGS84", F.WGS84), ("CGCS2000", F.CGCS2000)):
        n = 300 if datum == "WGS84" else 60
        for i in range(n):
            p = F.lla_to_ecef(lat[i], lon[i], h[i], ell)
            cs.append(case("lla_to_ecef", {"lat_deg": fl(lat[i]), "lon_deg": fl(lon[i]), "h_m": fl(h[i]), "datum": datum}, {"p": v3(p)}, {"p": "position_m"}))
            la2, lo2, h2 = F.ecef_to_lla(p, ell)
            cs.append(case("ecef_to_lla", {"p": v3(p), "datum": datum}, {"lat_deg": fl(la2), "lon_deg": fl(lo2), "h_m": fl(h2)},
                           {"lat_deg": "angle_deg", "lon_deg": "angle_deg", "h_m": "position_m"}))
    for pole in ([0.0, 0.0, 6356752.314245179], [0.0, 0.0, -6356852.0]):
        la2, lo2, h2 = F.ecef_to_lla(np.array(pole))
        cs.append(case("ecef_to_lla", {"p": pole, "datum": "WGS84"}, {"lat_deg": fl(la2), "lon_deg": fl(lo2), "h_m": fl(h2)},
                       {"lat_deg": "angle_deg", "lon_deg": "angle_deg", "h_m": "position_m"}))
    for name in ANCHORS:
        a = anchor(name)
        cs.append(case("T_ecef_world", {"anchor": ajs(name)}, {"T": F.T_ecef_world(a)}, {"T": "position_m"}, ts=False))
        pts = np.c_[rng.uniform(-10000, 10000, (150, 2)), rng.uniform(-500, 2e4, 150)]
        pts[0] = [0.0, 0.0, 0.0]
        pts[1] = [1200.5, -350.25, 42.0]
        for p in pts:
            la2, lo2, h2 = F.world_to_lla(p, a)
            cs.append(case("world_to_lla", {"p": v3(p), "anchor": ajs(name)}, {"lat_deg": fl(la2), "lon_deg": fl(lo2), "h_m": fl(h2)},
                           {"lat_deg": "angle_deg", "lon_deg": "angle_deg", "h_m": "position_m"}))
        for _ in range(60):
            la_, lo_, h_ = a.lat_deg + rng.uniform(-0.09, 0.09), a.lon_deg + rng.uniform(-0.09, 0.09), rng.uniform(-500, 2e4)
            w = F.lla_to_world(la_, lo_, h_, a)
            cs.append(case("lla_to_world", {"lat_deg": fl(la_), "lon_deg": fl(lo_), "h_m": fl(h_), "anchor": ajs(name)}, {"p": v3(w)}, {"p": "position_m"}))
    return cs


def enu_ned(rng) -> list[dict]:
    cs = []
    for _ in range(100):
        v = rng.uniform(-5000, 5000, 3)
        cs.append(case("enu_to_ned", {"v": v3(v)}, {"v": v3(F.enu_to_ned(v))}, {"v": "position_m"}))
        cs.append(case("flu_to_frd", {"v": v3(v)}, {"v": v3(F.flu_to_frd(v))}, {"v": "position_m"}))
    for _ in range(200):
        q = rand_quat(rng)
        cs.append(case("q_enuflu_from_nedfrd", {"q_wxyz": v3(q)}, {"q_xyzw": v3(F.q_enuflu_from_nedfrd(q))}, {"q_xyzw": "dimensionless"}))
    for _ in range(100):
        q = rand_quat(rng)
        cs.append(case("q_nedfrd_from_enuflu", {"q_xyzw": v3(q)}, {"q_wxyz": v3(F.q_nedfrd_from_enuflu(q))}, {"q_wxyz": "dimensionless"}, ts=False))
    psis = np.r_[rng.uniform(-math.pi, math.pi, 90), [0.0, math.pi / 2, -math.pi / 2, math.pi, -math.pi, 1e-12, 3.0, -3.0, 2 * math.pi, 7.0]]
    for p in psis:
        cs.append(case("yaw_ned_from_enu", {"psi": fl(p)}, {"yaw": fl(F.yaw_ned_from_enu(p))}, {"yaw": "angle_rad"}))
        cs.append(case("heading_deg", {"psi": fl(p)}, {"deg": fl(F.heading_deg(p))}, {"deg": "angle_deg"}))
    cs.extend(case("yaw_enu_from_heading_deg", {"h_deg": fl(hd)}, {"yaw": fl(F.yaw_enu_from_heading_deg(hd))}, {"yaw": "angle_rad"})
              for hd in np.r_[rng.uniform(0, 360, 45), [0.0, 90.0, 180.0, 270.0, 359.9999]])
    return cs


def three(rng) -> list[dict]:
    cs = []
    for _ in range(100):
        v = rng.uniform(-8000, 8000, 3)
        cs.append(case("enu_to_three", {"v": v3(v)}, {"v": v3(F.enu_to_three(v))}, {"v": "position_m"}))
        cs.append(case("three_to_enu", {"v": v3(v)}, {"v": v3(F.three_to_enu(v))}, {"v": "position_m"}))
        q = rand_quat(rng)
        cs.append(case("quat_enu_to_three", {"q_xyzw": v3(q)}, {"q_xyzw": v3(F.quat_enu_to_three(q))}, {"q_xyzw": "dimensionless"}))
    return cs


def ue(rng) -> list[dict]:
    cs = []
    for _ in range(100):
        p, t = rng.uniform(-5e5, 5e5, 3), rng.uniform(-100, 100, 3)
        cs.append(case("ue_cm_to_world", {"p_ue_cm": v3(p), "t_world_m": v3(t)}, {"p": v3(F.ue_cm_to_world(p, t))}, {"p": "position_m"}))
    angles = [(0.0, 0.0, 0.0), (0.0, 0.0, 90.0), (90.0, 0.0, 0.0)] + [tuple(rng.uniform(-180, 180, 3)) for _ in range(100)]
    for pitch, roll, yaw in angles:
        q = F.ue_rot_to_q(pitch, roll, yaw)
        fwd = F.quat_to_mat(np.array(q))[:, 0]
        cs.append(case("ue_rot_to_q", {"pitch_deg": fl(pitch), "roll_deg": fl(roll), "yaw_deg": fl(yaw)}, {"q_xyzw": list(q), "forward": v3(fwd)},
                       {"q_xyzw": "dimensionless", "forward": "dimensionless"}))
    return cs


def sim3(rng) -> list[dict]:
    cs = []
    for _ in range(100):
        a = F.Sim3(float(rng.uniform(0.5, 2.0)), tuple(rand_quat(rng)), tuple(rng.uniform(-100, 100, 3)))
        b = F.Sim3(float(rng.uniform(0.5, 2.0)), tuple(rand_quat(rng)), tuple(rng.uniform(-100, 100, 3)))
        u = float(rng.choice([0.0, 1.0, *rng.uniform(0, 1, 6)]))
        c = a.interpolate(b, u)
        cs.append(case("sim3_interpolate", {"a": a.to_json(), "b": b.to_json(), "u": u}, {"s": c.s, "q": list(c.q), "t": list(c.t)},
                       {"s": "dimensionless", "q": "dimensionless", "t": "position_m"}))
        x = rng.uniform(-50, 50, 3)
        cs.append(case("sim3_apply", {"a": a.to_json(), "x": v3(x)}, {"y": v3(a.apply(x))}, {"y": "position_m"}, ts=False))
        ab = a.compose(b)
        cs.append(case("sim3_compose", {"a": a.to_json(), "b": b.to_json()}, ab.to_json(), {"s": "dimensionless", "q": "dimensionless", "t": "position_m"}, ts=False))
    return cs


def local_px4(rng) -> list[dict]:
    cs = []
    ref = (473566094 / 1e7, 85190237 / 1e7)  # PX4 test_geo.cpp reference
    lat, lon = F.px4_reproject(0.5, 1.0, *ref)
    cs.append(case("px4_reproject", {"x_n": 0.5, "y_e": 1.0, "ref_lat": ref[0], "ref_lon": ref[1]}, {"lat_deg": fl(lat), "lon_deg": fl(lon)},
                   {"lat_deg": "angle_deg", "lon_deg": "angle_deg"}, ts=False))
    x, y = F.px4_project(47.356616973876953, 8.5190505981445313, *ref)
    cs.append(case("px4_project", {"lat_deg": 47.356616973876953, "lon_deg": 8.5190505981445313, "ref_lat": ref[0], "ref_lon": ref[1]},
                   {"x_n": fl(x), "y_e": fl(y)}, {"x_n": "position_m", "y_e": "position_m"}, ts=False))
    for name in ANCHORS:
        a = anchor(name)
        o = F.Px4Origin(a.lat_deg + 0.001, a.lon_deg - 0.002, a.h_ellipsoid_m + 5.0)
        for _ in range(20):
            p = np.r_[rng.uniform(-5000, 5000, 2), rng.uniform(-300, 50)]
            w = F.world_from_px4_local(p, o, a)
            cs.append(case("world_from_px4_local", {"p_ned": v3(p), "origin": [o.lat_deg, o.lon_deg, o.alt_msl_m], "anchor": ajs(name), "geoid": "anchor"},
                           {"p": v3(w)}, {"p": "position_m"}, ts=False))
        s = F.sih_loc_for_spawn(np.array([-230.0, 20.0, 5.0]), 0.3, a)
        cs.append(case("sih_loc_for_spawn", {"spawn_world": [-230.0, 20.0, 5.0], "yaw_enu_rad": 0.3, "anchor": ajs(name)},
                       {"params": s.params, "quant_offset_m": v3(s.quant_offset_m)}, {"params": "dimensionless", "quant_offset_m": "position_m"}, ts=False))
    # Hefei deviation table (M02 §6.4.2): world point at distance d north/east -> PX4 local
    a = anchor("hefei")
    o = F.Px4Origin(a.lat_deg, a.lon_deg, a.h_ellipsoid_m)
    for d in (100.0, 1000.0, 5000.0):
        for axis, e in (("N", [0.0, d, 0.0]), ("E", [d, 0.0, 0.0])):
            loc = F.px4_local_from_world(np.array(e), o, a)
            cs.append(case("px4_local_from_world", {"p_world": e, "origin": [o.lat_deg, o.lon_deg, o.alt_msl_m], "anchor": ajs("hefei"), "geoid": "anchor", "axis": axis},
                           {"p_ned": v3(loc)}, {"p_ned": "position_m"}, ts=False))
    return cs


def timecases() -> list[dict]:
    cs = []
    NS = T.NS
    for t_s, off in T.LEAP_TABLE:
        for dt in (-1, 0, 1):
            u = (t_s + dt) * NS
            cs.append(case("tai_minus_utc_s", {"unix_utc_ns": u}, {"s": T.tai_minus_utc_s(u)}, {"s": "exact"}, ts=False))
            g = T.utc_to_gpst_ns(u)
            cs.append(case("utc_to_gpst_ns", {"unix_utc_ns": u}, {"gpst_ns": g}, {"gpst_ns": "exact"}, ts=False))
        if off > 10:
            g = T.utc_to_gpst_ns(t_s * NS) - NS // 2  # inside the inserted leap second
            u, leap = T.gpst_to_utc_ns(g)
            cs.append(case("gpst_to_utc_ns", {"gpst_ns": g}, {"unix_utc_ns": u, "leap_second": leap}, {"unix_utc_ns": "exact", "leap_second": "exact"}, ts=False))
    u2026 = calendar.timegm((2026, 9, 28, 0, 0, 0)) * NS
    cs.append(case("gpst_minus_utc_s", {"unix_utc_ns": u2026}, {"s": T.gpst_minus_utc_s(u2026)}, {"s": "exact"}, ts=False))
    g = T.utc_to_gpst_ns(u2026)
    cs.append(case("gps_week_tow", {"gpst_ns": g}, {"week": T.gps_week_tow(g)[0], "tow_ns": T.gps_week_tow(g)[1]}, {"week": "exact", "tow_ns": "exact"}, ts=False))
    tai = T.gpst_to_tai_ptp_ns(g)
    cs.append(case("gpst_to_tai_ptp_ns", {"gpst_ns": g}, {"tai_ns": tai}, {"tai_ns": "exact"}, ts=False))
    cs.append(case("leap_table_status", {"now_unix_ns": u2026}, {"status": T.leap_table_status(u2026)}, {"status": "exact"}, ts=False))
    return cs


def build() -> dict[Path, bytes]:
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([SEED, 3])))
    groups = {"geodesy": geodesy(rng), "enu_ned": enu_ned(rng), "three": three(rng), "ue": ue(rng), "sim3": sim3(rng),
              "local_px4": local_px4(rng), "time": timecases()}
    files = {}
    for g, cases in groups.items():
        doc = {"schema": "awr.golden.frames.v1", "generator": "tools/contracts/gen_frames_golden.py", "group": g, "seed": SEED, "tolerance": TOL, "cases": cases}
        files[OUT / f"{g}.json"] = (json.dumps(doc, separators=(",", ":")) + "\n").encode()
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="frame conversion golden generator (M02)")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    diffs, total = 0, 0
    for p, data in sorted(build().items()):
        total += len(json.loads(data)["cases"])
        cur = p.read_bytes() if p.exists() else None
        if cur != data:
            diffs += 1
            if args.check:
                print(f"out of date: {p.relative_to(L.ROOT)}")
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                print(f"wrote {p.relative_to(L.ROOT)}")
    if args.check and diffs:
        print("gen_frames_golden.py --check: golden differs from frames.py; run python tools/contracts/gen_frames_golden.py", file=sys.stderr)
        return 1
    print(f"gen_frames_golden.py: {total} cases, {'up to date' if not diffs else f'{diffs} file(s) updated'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
