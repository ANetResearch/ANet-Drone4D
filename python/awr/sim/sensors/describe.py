"""`describe()`：`GET /api/fleet/profiles/{id}` 的 `sensors{}`（M13-FR-015；ADR-043；M08 `register_sensor_describer`）。

内容：每个传感器的内参、FOV、挂载、云台限位与缺省、检测器、速率、置信度与来源；`rig`（sensor_no、名称、kind，全部传感器）；
`twin`（Camera、LiDAR、RTK 三项的状态：present / missing / invalid 与 conf）。前端 `engine/sensors/specs.gen.ts` 由同一函数
生成（`tests/sensors/gen_web_specs.py`），因此浏览器与 sim-core 看到的传感器参数同源。纯函数，无副作用。
"""

from __future__ import annotations

import math
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from .enums import KIND_NAMES, SensorKind
from .spec import Rig, SensorSpec, SensorSpecError, _yaml_load, load_rig, vehicles_dir

__all__ = ["describe", "describe_model", "describe_rig", "dir_for_profile", "spec_json"]


def _r(x: float, nd: int = 6) -> float | None:
    return None if x is None or not math.isfinite(float(x)) else round(float(x), nd)


def spec_json(s: SensorSpec) -> dict[str, Any]:
    d: dict[str, Any] = {"name": s.name, "kind": KIND_NAMES[s.kind], "sensor_no": s.sensor_no,
                         "mount": {"xyz_m": [_r(v) for v in s.mount_t],
                                   "R": [[_r(float(s.mount_R[i, j]), 12) for j in range(3)] for i in range(3)]},
                         "range_m": _r(s.range_m), "rate_hz": _r(s.rate_hz), "conf": s.conf, "src": s.src}
    m = (s.doc.get("mount") or {}) if isinstance(s.doc, dict) else {}
    if "rpy_deg" in m:
        d["mount"]["rpy_deg"] = [float(v) for v in m["rpy_deg"]]
    if "preset" in m:
        d["mount"]["preset"] = str(m["preset"])
    if s.intr is not None:
        it = s.intr
        d["intrinsics"] = {"w": it.w, "h": it.h, "fx": it.fx, "fy": it.fy, "cx": it.cx, "cy": it.cy, "dist": list(it.dist)}
    if math.isfinite(s.hfov_rad):
        d["hfov_rad"] = _r(s.hfov_rad, 9)
        d["vfov_rad"] = _r(s.vfov_rad, 9)
        d["hfov_deg"] = _r(math.degrees(s.hfov_rad), 3)
        d["vfov_deg"] = _r(math.degrees(s.vfov_rad), 3)
    if s.gimbal is not None:
        g = s.gimbal
        d["gimbal"] = {"yaw_min_deg": _r(math.degrees(g.az_min_rad), 4), "yaw_max_deg": _r(math.degrees(g.az_max_rad), 4),
                       "pitch_min_deg": _r(math.degrees(g.el_min_rad), 4), "pitch_max_deg": _r(math.degrees(g.el_max_rad), 4),
                       "rate_max_deg_s": _r(math.degrees(g.rate_max_rad_s), 4), "default_mode": g.default_mode,
                       "default_yaw_deg": _r(math.degrees(g.default_az_rad), 4),
                       "default_pitch_deg": _r(math.degrees(g.default_el_rad), 4)}
    if s.detector is not None:
        dt = s.detector
        d["detector"] = {"capability": dt.capability, "p0": dt.p0, "r_fp_m": dt.r_fp_m, "t_look_s": dt.t_look_s,
                         "pos_sigma_m": dt.pos_sigma_m}
    if s.kind == SensorKind.GNSS:
        n = s.noise
        d["gnss"] = {"fix_type": n.get("fix_type"), "warm_start": n.get("warm_start"),
                     "rtk": bool((n.get("rtk") or {}).get("enabled")), "antenna_xyz_m": [_r(v) for v in s.mount_t]}
    if s.kind == SensorKind.IMU:
        d["imu"] = {"gyro": dict(s.noise.get("gyro") or {}), "accel": dict(s.noise.get("accel") or {})}
    if s.kind == SensorKind.THERMAL:
        d["netd_k"] = s.noise.get("netd_k")
    if s.kind == SensorKind.LIDAR:
        doc = s.doc
        d["lidar"] = {"fov": dict(doc.get("fov") or {}), "pattern": dict(doc.get("pattern") or {}),
                      "frame_rate_hz": doc.get("frame_rate_hz"), "mount_presets": dict(doc.get("mount_presets") or {})}
    return d


def describe_rig(rig: Rig) -> dict[str, Any]:
    out: dict[str, Any] = {s.name: spec_json(s) for s in rig.specs}
    out["rig"] = [{"sensor_no": s.sensor_no, "name": s.name, "kind": KIND_NAMES[s.kind]} for s in rig.specs]
    return out


def _twin(d: Path, sensors_map: dict) -> list[dict]:
    rows = []
    for item, key in (("Camera", "camera"), ("LiDAR", "mid360"), ("RTK", "gnss")):
        rel = sensors_map.get(key)
        if rel is None:
            rows.append({"item": item, "status": "missing", "conf": None})
            continue
        p = d / str(rel)
        try:
            doc = _yaml_load(p.read_text(encoding="utf-8"))
            from .spec import build_spec

            s = build_spec(key, doc, 0, str(p))
            rows.append({"item": item, "status": "present", "conf": s.conf})
        except (OSError, yaml.YAMLError, SensorSpecError):
            rows.append({"item": item, "status": "invalid", "conf": None})
    return rows


@cache
def _profile_dirs(base: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(Path(base).glob("*/params.yaml")):
        try:
            doc = _yaml_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if doc.get("id"):
            out[str(doc["id"])] = str(p.parent)
        for v in (doc.get("variants") or {}):
            out[str(v)] = str(p.parent)
    return out


def dir_for_profile(profile_id: str, base: Path | None = None) -> Path | None:
    d = _profile_dirs(str(base or vehicles_dir())).get(str(profile_id))
    return None if d is None else Path(d)


def describe_model(model_dir: Path | str) -> dict[str, Any]:
    d = Path(model_dir)
    try:
        params = _yaml_load((d / "params.yaml").read_text(encoding="utf-8")) or {}
    except OSError:
        params = {}
    smap = dict(params.get("sensors") or {})
    try:
        out = describe_rig(load_rig(d, smap))
    except SensorSpecError as e:
        out = {"error": e.to_json()}
    out["twin"] = _twin(d, smap)
    return out


def describe(profile_id: str, profile_doc: dict | None = None) -> dict[str, Any]:
    """M08 `register_sensor_describer` 的回调：`fn(profile_id, profile_doc) -> sensors{}`。"""
    d = dir_for_profile(profile_id)
    if d is None:
        return {}
    return describe_model(d)
