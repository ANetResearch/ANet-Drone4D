"""flight60 generator (M05-FR-055; AWR-18 §8.6; port of .cache/research/g02/prep.py::flight()). Owner: M05.

A fixed 60 s, six-segment camera flight per world (overview-descent, transit, fast-yaw, tower-orbit, follow, climb-out),
written as apps/web/public/bench/flight60/<world>.bin (f32 little endian [3601 x 6]: eye xyz, target xyz, world ENU,
60 Hz, 86,424 B) and <world>.json (awr.flight60.v1, snake_case with unit suffixes). Differences to the prototype
(AWR-18 §8.6(2)): the tallest building is argmax(DSM - DTM) on the World Package grids instead of the x01 analysis peak,
the safety height map is the DSM (max over a +-12 m window), and E is the larger horizontal extent of world.json.

Usage:
    python tools/bench/flight60/gen.py [--worlds DIR] [--out DIR] [--check] [world ...]
--check exits 1 when an output is missing or stale (coordinate or bin hashes differ), without writing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORLDS = ROOT / "worlds"
DEFAULT_OUT = ROOT / "apps" / "web" / "public" / "bench" / "flight60"
GENERATOR = {"name": "awr.flight60.gen", "version": "1.0.0"}
SCHEMA = "awr.flight60.v1"
FPS = 60
FRAMES = 3601
SEGMENTS = [("overview-descent", 0, 6), ("transit", 6, 20), ("fast-yaw", 20, 24), ("tower-orbit", 24, 40), ("follow", 40, 50),
            ("climb-out", 50, 60)]


class Grid:
    """f32 grid sidecar (AWR-16 §6.2): row 0 south, originXY = south-west corner of cell (0, 0)."""

    def __init__(self, sidecar: Path):
        sc = json.loads(sidecar.read_text(encoding="utf-8"))
        if sc["dtype"] != "float32" or sc["rowOrder"] != "south-to-north":
            raise ValueError(f"{sidecar}: unsupported grid {sc['dtype']} {sc['rowOrder']}")
        self.w, self.h = int(sc["width"]), int(sc["height"])
        self.cell = float(sc["cellM"])
        self.x0, self.y0 = float(sc["originXY"][0]), float(sc["originXY"][1])
        self.a = np.fromfile(sidecar.parent / sc["href"], dtype="<f4").reshape(self.h, self.w).astype(np.float64)

    def centres(self) -> tuple[np.ndarray, np.ndarray]:
        return self.x0 + (np.arange(self.w) + 0.5) * self.cell, self.y0 + (np.arange(self.h) + 0.5) * self.cell

    def bilinear(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """cell-centre bilinear, clamped at the border (same rule as M04 grids.Grid.bilinear)"""
        gx = np.clip((np.asarray(x, np.float64) - self.x0) / self.cell - 0.5, 0.0, max(self.w - 1, 0))
        gy = np.clip((np.asarray(y, np.float64) - self.y0) / self.cell - 0.5, 0.0, max(self.h - 1, 0))
        c0 = np.minimum(np.floor(gx).astype(np.int64), max(self.w - 2, 0))
        r0 = np.minimum(np.floor(gy).astype(np.int64), max(self.h - 2, 0))
        tx, ty = gx - c0, gy - r0
        c1, r1 = np.minimum(c0 + 1, self.w - 1), np.minimum(r0 + 1, self.h - 1)
        a = self.a
        return (a[r0, c0] * (1 - tx) + a[r0, c1] * tx) * (1 - ty) + (a[r1, c0] * (1 - tx) + a[r1, c1] * tx) * ty

    def window_max(self, x: float, y: float, r: float = 12.0) -> float:
        """max over a +-r window around (x, y) (prep.py HM.get on the DSM)"""
        k = math.ceil(r / self.cell)
        i = int(np.clip((y - self.y0) / self.cell, 0, self.h - 1))
        j = int(np.clip((x - self.x0) / self.cell, 0, self.w - 1))
        return float(self.a[max(0, i - k):i + k + 1, max(0, j - k):j + k + 1].max())


def smooth(a: np.ndarray, sig: float) -> np.ndarray:
    if sig <= 0:
        return a
    r = int(3 * sig)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sig) ** 2)
    k /= k.sum()
    p = np.pad(a, ((r, r), (0, 0)), mode="edge")
    return np.stack([np.convolve(p[:, c], k, mode="valid") for c in range(a.shape[1])], 1)


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def peak_of(dsm: Grid, dtm: Grid) -> tuple[float, float, float]:
    """tallest building: argmax(DSM - DTM) over the DSM cells (DTM bilinear at the cell centres)"""
    cx, cy = dsm.centres()
    X, Y = np.meshgrid(cx, cy)
    diff = dsm.a - dtm.bilinear(X, Y)
    k = int(np.argmax(diff))
    i, j = divmod(k, dsm.w)
    return float(cx[j]), float(cy[i]), float(diff[i, j])


def flight(world_dir: Path) -> tuple[np.ndarray, dict]:
    """eye/target table [3601 x 6] and the descriptive part of the json (prep.py::flight() rules)"""
    world = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
    dsm = Grid(world_dir / "geometry" / "terrain" / "dsm_2m.json")
    dtm = Grid(world_dir / "geometry" / "terrain" / "dtm_10m.json")
    b = world["bounds"]
    E = max(b["max"][0] - b["min"][0], b["max"][1] - b["min"][1])
    px, py, ph = peak_of(dsm, dtm)
    P = np.array([px, py])
    nP = float(np.linalg.norm(P))
    dirIn = P / (nP + 1e-9) if nP > 50 else np.array([0.7071, 0.7071])
    yawIn = math.degrees(math.atan2(dirIn[1], dirIn[0]))
    A0 = P - dirIn * 420.0
    B0 = P - dirIn * 130.0
    safe = dsm.window_max
    altT = max(max(safe(*(A0 + (B0 - A0) * f)) for f in np.linspace(0, 1, 30)) + 25.0, 90.0)
    kf: list[tuple[float, list[float], float, float]] = []
    ov = P - dirIn * min(0.35 * E, 900.0)
    kf.append((0.0, [ov[0], ov[1], min(max(0.3 * E, 500.0), 800.0)], yawIn, -38.0))
    kf.append((6.0, [A0[0], A0[1], altT], yawIn, -18.0))
    kf.append((20.0, [B0[0], B0[1], altT], yawIn, -12.0))
    kf.append((22.0, [B0[0], B0[1], altT], yawIn + 180.0, -12.0))
    kf.append((24.0, [B0[0], B0[1], altT], yawIn, -8.0))
    ro, zo = 75.0, max(0.55 * ph, 40.0)
    a0 = math.atan2(-dirIn[1], -dirIn[0])
    for k in range(8):
        f = k / 7.0
        ang = a0 + math.pi * f
        e = P + ro * np.array([math.cos(ang), math.sin(ang)])
        yaw = math.degrees(math.atan2(P[1] - e[1], P[0] - e[0]))
        kf.append((26.0 + 12.0 * f, [e[0], e[1], zo], yaw, -2.0))
    eF = P + ro * np.array([math.cos(a0 + math.pi), math.sin(a0 + math.pi)])
    dF = (eF - P) / np.linalg.norm(eF - P)
    yawF = math.degrees(math.atan2(dF[1], dF[0]))
    e1 = eF + dF * 180.0
    zf = max(max(safe(*(eF + (e1 - eF) * f)) for f in np.linspace(0, 1, 20)) + 30.0, 120.0)
    kf.append((40.0, [eF[0], eF[1], zf], yawF, -25.0))
    kf.append((50.0, [e1[0], e1[1], zf], yawF, -25.0))
    e2 = e1 + dF * min(0.25 * E, 700.0)
    yawC = math.degrees(math.atan2(-e2[1], -e2[0]))
    kf.append((60.0, [e2[0], e2[1], min(max(0.35 * E, 600.0), 900.0)], yawC, -35.0))
    T = np.array([k[0] for k in kf])
    EYE = np.array([k[1] for k in kf], dtype=np.float64)
    YAW = np.unwrap(np.radians([k[2] for k in kf]))
    PIT = np.radians([k[3] for k in kf])
    ts = np.arange(FRAMES) / float(FPS)
    eye = np.stack([np.interp(ts, T, EYE[:, c]) for c in range(3)], 1)
    yp = np.stack([np.interp(ts, T, YAW), np.interp(ts, T, PIT)], 1)
    eye = smooth(eye, 12)
    yp = smooth(yp, 9)
    fwd = np.stack([np.cos(yp[:, 1]) * np.cos(yp[:, 0]), np.cos(yp[:, 1]) * np.sin(yp[:, 0]), np.sin(yp[:, 1])], 1)
    tgt = eye + fwd * 100.0
    arr = np.hstack([eye, tgt]).astype("<f4")
    spd = np.linalg.norm(np.diff(eye, axis=0), axis=1) * FPS
    yawrate = np.degrees(np.abs(np.diff(yp[:, 0]))) * FPS
    info = {
        "peak": {"x_m": round(px, 3), "y_m": round(py, 3), "h_m": round(ph, 3)},
        "segments": [{"name": n, "t0_s": t0, "t1_s": t1} for n, t0, t1 in SEGMENTS],
        "keyframes": [{"t_s": k[0], "eye_m": [round(float(v), 2) for v in k[1]], "yaw_deg": round(float(k[2]), 4), "pitch_deg": k[3]} for k in kf],
        "stats": {"speed_max_mps": round(float(spd.max()), 4), "speed_p50_mps": round(float(np.median(spd)), 4),
                  "yawrate_max_dps": round(float(yawrate.max()), 4), "alt_min_m": round(float(eye[:, 2].min()), 3),
                  "alt_max_m": round(float(eye[:, 2].max()), 3)},
    }
    return arr, info


def gen_flight60(world_dir: Path, out_dir: Path, *, fps: int = FPS, seconds: float = 60.0, fov_y_deg: float = 60.0, width: int = 1280,
                 height: int = 720) -> dict:
    """write <out_dir>/<world>.{bin,json}; returns the awr.flight60.v1 json content"""
    if fps != FPS or seconds != 60.0:
        raise ValueError("awr.flight60.v1 is fixed at 60 Hz and 60 s")
    world = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
    arr, info = flight(world_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wid = world["id"]
    bin_path = out_dir / f"{wid}.bin"
    tmp = bin_path.with_suffix(".bin.tmp")
    tmp.write_bytes(arr.tobytes())
    tmp.replace(bin_path)
    doc = {
        "schema": SCHEMA, "world_id": wid, "content_version": world["contentVersion"],
        "coordinate_sha256": sha256_file(world_dir / "coordinate.json"), "bin_sha256": sha256_file(bin_path),
        "fps": FPS, "frames": FRAMES, "fov_y_deg": fov_y_deg, "width_px": width, "height_px": height, "near_m": 1, "far_m": 20000,
        **info, "generator": dict(GENERATOR),
    }
    jp = out_dir / f"{wid}.json"
    jt = jp.with_suffix(".json.tmp")
    jt.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    jt.replace(jp)
    return doc


def is_fresh(world_dir: Path, out_dir: Path) -> bool:
    world = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
    wid = world["id"]
    jp, bp = out_dir / f"{wid}.json", out_dir / f"{wid}.bin"
    if not jp.exists() or not bp.exists():
        return False
    doc = json.loads(jp.read_text(encoding="utf-8"))
    return (doc.get("schema") == SCHEMA and doc.get("coordinate_sha256") == sha256_file(world_dir / "coordinate.json")
            and doc.get("bin_sha256") == sha256_file(bp) and doc.get("content_version") == world["contentVersion"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="generate the flight60 camera scripts of the World Packages (AWR-18 §8.6)")
    ap.add_argument("worlds", nargs="*", help="world ids (default: every world with world.json)")
    ap.add_argument("--worlds-dir", type=Path, default=DEFAULT_WORLDS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="exit 1 when an output is missing or stale")
    ap.add_argument("--missing", action="store_true", help="only (re)generate missing or stale outputs")
    a = ap.parse_args(argv)
    ids = a.worlds or sorted(p.name for p in a.worlds_dir.iterdir() if (p / "world.json").exists() and not p.name.startswith((".", "_")))
    stale = []
    for wid in ids:
        wd = a.worlds_dir / wid
        if not (wd / "world.json").exists():
            print(f"flight60: world {wid} not found in {a.worlds_dir}", file=sys.stderr)
            return 2
        fresh = is_fresh(wd, a.out)
        if a.check:
            if not fresh:
                stale.append(wid)
            continue
        if a.missing and fresh:
            continue
        doc = gen_flight60(wd, a.out)
        s = doc["stats"]
        print(f"flight60 {wid}: speed p50 {s['speed_p50_mps']:.1f} m/s, max {s['speed_max_mps']:.1f}, yaw {s['yawrate_max_dps']:.0f} deg/s, "
              f"alt {s['alt_min_m']:.0f}..{s['alt_max_m']:.0f} m, peak h {doc['peak']['h_m']:.0f} m")
    if stale:
        print("flight60 stale or missing: " + ", ".join(stale) + " (fix: make flight60)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
