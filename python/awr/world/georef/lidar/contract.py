"""LiDAR frame and IMU sample contract (M02 §6.3.2; M02-FR-013, FR-014; AWR-16 §14.3). D1 stub: validation, codec,
driver mapping and fixtures shared by the V0.5a importer and the M13 virtual MID-360.

Frame = JSON header (`packages/contracts/sensor/lidar_frame.schema.json`, `awr.sensor.lidar_frame.v1`) + SoA payload,
little-endian, 20 B/point: f32 x[N], f32 y[N], f32 z[N] (Livox lidar frame FLU, m), u32 offset_time[N] (ns from
`timebase_ns`), u8 reflectivity[N] (0-150 diffuse, 151-255 retro-reflective), u8 tag[N], u8 line[N] (< 4), u8 pad[N] = 0.

Validation rules of M02 §6.3.2 (errors carry the M02 reason codes 490-492 proposed for AWR-17 §8.4, V0.5):
L-01 line < 4; L-02 offset_time non-decreasing within the frame; L-03 synchronised time bases sit on the absolute
100 ms grid (|raw_timebase_ns mod 100 ms| <= 1 ms, warning); L-04 sync_type none warns, and without an offset estimate is
490 TIME_UNSYNCED; L-05 static IMU acceleration norm within 0.95-1.05 g (else 491 UNIT_MISMATCH); L-06 model matches the
Livox dev_type (9 MID-360, 35 MID-360S); L-07 point_count <= 22 000 and equals the payload length; L-08 driver-side
extrinsics are the identity (else 492 EXTRINSIC_NOT_IDENTITY).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

from awr.contracts._paths import contracts_root

__all__ = [
    "FRAME_PERIOD_NS",
    "G0_MPS2",
    "MAX_POINTS",
    "MODEL_DEV_TYPE",
    "POINT_BYTES",
    "Finding",
    "LidarFrame",
    "check_driver_extrinsic",
    "check_imu_static",
    "custommsg_to_frame",
    "decode_payload",
    "encode_payload",
    "imu_acc_to_mps2",
    "lio_valid_mask",
    "make_fixture",
    "pointcloud2_time_resolution_ns",
    "validate_frame",
]

POINT_BYTES = 20
MAX_POINTS = 22_000                     # 200 kHz x 0.1 s + 10 %
FRAME_PERIOD_NS = 100_000_000           # 10 Hz
GRID_TOL_NS = 1_000_000
G0_MPS2 = 9.80665
MODEL_DEV_TYPE = {"livox_mid360": 9, "livox_mid360s": 35}
SCHEMA = "awr.sensor.lidar_frame.v1"


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str                         # error | warn
    message: str
    code: int | None = None               # 490 TIME_UNSYNCED, 491 UNIT_MISMATCH, 492 EXTRINSIC_NOT_IDENTITY


@dataclass
class LidarFrame:
    header: dict
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    offset_time: np.ndarray
    reflectivity: np.ndarray
    tag: np.ndarray
    line: np.ndarray
    extra: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.x)


def encode_payload(f: LidarFrame) -> bytes:
    n = f.n
    parts = [np.asarray(f.x, "<f4"), np.asarray(f.y, "<f4"), np.asarray(f.z, "<f4"), np.asarray(f.offset_time, "<u4"),
             np.asarray(f.reflectivity, np.uint8), np.asarray(f.tag, np.uint8), np.asarray(f.line, np.uint8),
             np.zeros(n, np.uint8)]
    if any(len(p) != n for p in parts):
        raise ValueError("all point streams must have the same length")
    return b"".join(p.tobytes() for p in parts)


def decode_payload(header: dict, data: bytes) -> LidarFrame:
    n = int(header["point_count"])
    if len(data) != POINT_BYTES * n:
        raise ValueError(f"payload is {len(data)} B, expected {POINT_BYTES} x {n}")
    o = 0
    out = []
    for dt, size in (("<f4", 4), ("<f4", 4), ("<f4", 4), ("<u4", 4), (np.uint8, 1), (np.uint8, 1), (np.uint8, 1)):
        out.append(np.frombuffer(data, dt, n, o).copy())
        o += size * n
    return LidarFrame(dict(header), *out)


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    root = contracts_root()
    res = []
    for p in sorted((root / "schemas" / "world").glob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        res.append((s["$id"], Resource.from_contents(s)))
    s = json.loads((root / "sensor" / "lidar_frame.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(s, registry=Registry().with_resources(res))


def lio_valid_mask(tag: np.ndarray) -> np.ndarray:
    """Points usable by LIO: `(tag & 0x30) in {0x00, 0x10}` (r04 §3.1.4)."""
    t = np.asarray(tag, np.uint8) & 0x30
    return (t == 0x00) | (t == 0x10)


def validate_frame(f: LidarFrame, *, dev_type: int | None = None, prev_seq: int | None = None) -> list[Finding]:
    """Rules L-01..L-07 plus the header schema (S-01); `prev_seq` detects dropped frames (warning)."""
    h = f.header
    out: list[Finding] = [Finding("S-01", "error", f"header /{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}")
                          for e in sorted(_validator().iter_errors(h), key=lambda e: list(map(str, e.absolute_path)))[:10]]
    line = np.asarray(f.line)
    if f.n and int(line.max()) >= 4:
        out.append(Finding("L-01", "error", f"line must be < 4 (max {int(line.max())})"))
    ot = np.asarray(f.offset_time, np.int64)
    if f.n > 1 and np.any(np.diff(ot) < 0):
        out.append(Finding("L-02", "error", f"offset_time decreases at {int(np.argmax(np.diff(ot) < 0)) + 1}"))
    sync = h.get("sync_type")
    raw = h.get("raw_timebase_ns")
    if sync in ("ptp", "gps_pps") and raw is not None:
        r = int(raw) % FRAME_PERIOD_NS
        if min(r, FRAME_PERIOD_NS - r) > GRID_TOL_NS:
            out.append(Finding("L-03", "warn", f"synchronised timebase {raw} is {r / 1e6:.3f} ms off the 100 ms grid"))
    if sync == "none":
        if h.get("offset_est_ns") is None:
            out.append(Finding("L-04", "error", "sync_type none and no offset estimate", 490))
        else:
            out.append(Finding("L-04", "warn", "sync_type none: time base from an offset estimate"))
    model = h.get("model")
    if model is not None and dev_type is not None and MODEL_DEV_TYPE.get(model) != dev_type:
        out.append(Finding("L-06", "error", f"model {model} does not match Livox dev_type {dev_type}"))
    pc = int(h.get("point_count", -1))
    if pc > MAX_POINTS:
        out.append(Finding("L-07", "error", f"point_count {pc} > {MAX_POINTS}"))
    if pc != f.n:
        out.append(Finding("L-07", "error", f"point_count {pc} != payload points {f.n}"))
    seq = h.get("frame_seq")
    if prev_seq is not None and seq is not None and int(seq) != prev_seq + 1:
        out.append(Finding("L-09", "warn", f"frame_seq jumped from {prev_seq} to {seq} (dropped frames)"))
    return out


# ---------------------------------------------------------------- IMU and driver checks
def imu_acc_to_mps2(acc, unit: str) -> np.ndarray:
    """Import boundary conversion (M02-FR-014): Livox built-in IMU reports g; the session stores m/s^2."""
    a = np.asarray(acc, dtype=np.float64)
    if unit == "g":
        return a * G0_MPS2
    if unit == "m/s^2":
        return a
    raise ValueError(f"unknown acceleration unit {unit!r}")


def check_imu_static(acc_raw, unit: str) -> list[Finding]:
    """L-05: during a static window the acceleration norm must be 0.95-1.05 g after conversion (else 491)."""
    a = imu_acc_to_mps2(acc_raw, unit)
    g = float(np.median(np.linalg.norm(a, axis=-1))) / G0_MPS2
    if not 0.95 <= g <= 1.05:
        return [Finding("L-05", "error", f"static |acc| = {g:.3f} g with declared unit {unit!r}", 491)]
    return []


def check_driver_extrinsic(ext: dict | list | np.ndarray) -> list[Finding]:
    """L-08: livox_ros_driver2 extrinsics must be all zero (roll, pitch, yaw, x, y, z); calibration lives in rig.json."""
    v = np.asarray(list(ext.values()) if isinstance(ext, dict) else ext, dtype=np.float64).ravel()
    ok = np.allclose(v.reshape(4, 4), np.eye(4), atol=1e-12) if v.size == 16 else np.allclose(v, 0.0, atol=1e-12)
    return [] if ok else [Finding("L-08", "error", "driver-side extrinsic is not the identity", 492)]


def pointcloud2_time_resolution_ns(t_ns: float) -> float:
    """Resolution of a float64 absolute time stamp in ns (PointCloud2 XYZRTLT): about 256 ns near 1.7e18 ns, so such
    inputs are downgraded to frame-level time with a warning (r04 §6 item 5)."""
    return float(np.spacing(np.float64(t_ns)))


# ---------------------------------------------------------------- driver mapping (CustomMsg -> frame)
def custommsg_to_frame(msg: dict, *, sensor_id: str, frame_id: str, t_sim_ns: int, sync_type: str, timescale: str,
                       model: str = "livox_mid360", frame_seq: int | None = None, offset_est_ns: int | None = None,
                       simulated: bool = False) -> LidarFrame:
    """livox_ros_driver2 CustomMsg (dict form: timebase, points[{x, y, z, offset_time, reflectivity, tag, line}]) ->
    frame. `t_sim_ns` is `SessionTimebase.to_t_sim_ns(stream, timebase)`; the raw value is kept as raw_timebase_ns."""
    pts = msg["points"]
    arr = {k: np.array([p[k] for p in pts]) for k in ("x", "y", "z", "offset_time", "reflectivity", "tag", "line")}
    h = {"schema": SCHEMA, "sensor_id": sensor_id, "frame_id": frame_id, "timebase_ns": int(t_sim_ns), "sync_type": sync_type,
         "point_count": len(pts), "T_world_sensor": None, "model": model, "timescale": timescale,
         "raw_timebase_ns": int(msg["timebase"]), "simulated": bool(simulated)}
    if frame_seq is not None:
        h["frame_seq"] = int(frame_seq)
    if offset_est_ns is not None:
        h["offset_est_ns"] = int(offset_est_ns)
    return LidarFrame(h, arr["x"].astype(np.float32), arr["y"].astype(np.float32), arr["z"].astype(np.float32),
                      arr["offset_time"].astype(np.uint32), arr["reflectivity"].astype(np.uint8), arr["tag"].astype(np.uint8),
                      arr["line"].astype(np.uint8))


# ---------------------------------------------------------------- fixtures (shared with M13)
def make_fixture(kind: str = "valid", n: int = 20_000, seed: int = 1) -> LidarFrame:
    """Positive fixture `valid` (MID-360 non-repetitive pattern proxy, PTP time on the 100 ms grid) and the negative
    ones of M02-AC-015: `line4`, `offset_backwards`, `unsynced_no_offset`, `too_many_points`, `wrong_model`."""
    r = np.random.default_rng(seed)
    az = r.uniform(-math.pi, math.pi, n)
    el = np.radians(r.uniform(-7.0, 52.0, n))
    rng_m = r.uniform(0.5, 40.0, n)
    x = rng_m * np.cos(el) * np.cos(az)
    y = rng_m * np.cos(el) * np.sin(az)
    z = rng_m * np.sin(el)
    raw = 1_790_000_000_000_000_000 - 1_790_000_000_000_000_000 % FRAME_PERIOD_NS
    h = {"schema": SCHEMA, "sensor_id": "p600-01/mid360", "frame_id": "uav01/mid360", "timebase_ns": 12_300_000_000,
         "sync_type": "ptp", "point_count": n, "T_world_sensor": None, "frame_seq": 123, "model": "livox_mid360",
         "timescale": "tai", "raw_timebase_ns": raw, "offset_est_ns": 0, "frame_dur_ns": FRAME_PERIOD_NS, "pattern_mode": 0,
         "extrinsic_ref": "calib/p600-01/lidar_imu@2026-10-01.1", "simulated": False}
    f = LidarFrame(h, x.astype(np.float32), y.astype(np.float32), z.astype(np.float32),
                   np.sort(r.integers(0, FRAME_PERIOD_NS, n)).astype(np.uint32), r.integers(0, 256, n).astype(np.uint8),
                   r.choice(np.array([0x00, 0x10, 0x20, 0x01], np.uint8), n, p=[0.85, 0.1, 0.03, 0.02]),
                   (np.arange(n) % 4).astype(np.uint8))
    if kind == "valid":
        return f
    if kind == "line4":
        f.line[7] = 4
    elif kind == "offset_backwards":
        f.offset_time[100], f.offset_time[101] = f.offset_time[101] + 1000, f.offset_time[100]
    elif kind == "unsynced_no_offset":
        f.header.update(sync_type="none", timescale="host_mono")
        f.header.pop("offset_est_ns")
    elif kind == "too_many_points":
        f = make_fixture("valid", MAX_POINTS + 1, seed)
    elif kind == "wrong_model":
        f.header["model"] = "livox_mid360s"
    else:
        raise ValueError(f"unknown fixture {kind!r}")
    return f
