"""`describe()`：`GET /api/fleet/profiles/{id}` 的 `sensors{}`（M13-FR-015；ADR-043；M08 `register_sensor_describer`）。

内容：每个传感器的内参、FOV、挂载、云台限位与缺省、检测器、速率、置信度与来源；`rig`（sensor_no、名称、kind，全部传感器）；
`twin`（Camera、LiDAR、RTK 三项的状态：present / missing / invalid 与 conf）。前端 `engine/sensors/specs.gen.ts` 由同一函数
生成（`tests/sensors/gen_web_specs.py`），因此浏览器与 sim-core 看到的传感器参数同源。纯函数，无副作用。

D2（M13-FR-216）：每个传感器另给槽位属性（`slot{legacy, default, removable}`）、型号（`model_id`、`spec_ref`）、质量与功率、
派生值（eo_zoom 两端的 HFOV、VFOV、DFOV、F 数、各输出档的等效像元与 4K 门槛；照明器的参考作用距离；LWIR 各镜头视场；雷达
`R_ref` 对 1 m²、人 0.5 m²、车 10 m² 的检测距离；MID-360 反射率量程与角密度），`mounts` 为缺省挂载的质量与功率汇总。
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
    d2 = d2_json(s)
    if d2:
        d.update(d2)
    return d


# 雷达参考截面（AWR-04 §6.1、AWR-20 §6.9）
RCS_REF = {"m2_1": 1.0, "person": 0.5, "dog": 0.1, "uav_small": 0.01, "car": 10.0}


def d2_json(s: SensorSpec) -> dict[str, Any]:
    """D2 字段（M13-FR-216）；D1 文件只给槽位属性。"""
    out: dict[str, Any] = {"slot": {"legacy": s.slot.legacy, "default": s.slot.default, "removable": s.slot.removable}}
    if s.schema_id:
        out["type"] = s.schema_id.split(".")[2]
    if s.model_id or s.spec_ref:
        out["model_id"] = s.model_id or None
        out["spec_ref"] = s.spec_ref or None
    if math.isfinite(s.mass_kg):
        out["mass_kg"] = _r(s.mass_kg)
    if math.isfinite(s.power_w):
        out["power_w"] = _r(s.power_w)
    if s.env_limits is not None:
        out["env_limits"] = {"temp_c": list(s.env_limits)}
    if s.on_gimbal_of:
        out["on_gimbal_of"] = s.on_gimbal_of
    z = s.zoom
    if z is not None:
        f0, f1 = z.f_min_mm, z.f_max_mm
        out["zoom"] = {"f_mm": [f0, f1], "f_number": [_r(z.fnum(f0), 4), _r(z.fnum(f1), 4)], "law": z.law,
                       "zoom_time_s": z.zoom_time_s, "pixel_um": z.pixel_um, "sensor_px": [z.width_px, z.height_px],
                       "sensor_mm": [_r(z.sensor_w_mm, 4), _r(z.sensor_h_mm, 4)],
                       "hfov_deg": [_r(math.degrees(z.hfov(f0)), 3), _r(math.degrees(z.hfov(f1)), 3)],
                       "vfov_deg": [_r(math.degrees(z.vfov(f0)), 3), _r(math.degrees(z.vfov(f1)), 3)],
                       "dfov_deg": [_r(math.degrees(z.dfov(f0)), 3), _r(math.degrees(z.dfov(f1)), 3)],
                       "analysis_default": z.analysis_default, "k4_requires": z.k4_requires,
                       "outputs": [{"name": o.name, "w_px": o.w, "h_px": o.h, "crop_w_px": o.crop_w, "crop_h_px": o.crop_h,
                                    "fps": o.fps, "p_eff_um": _r(z.p_eff_um(o.name), 4),
                                    "hfov_deg": [_r(math.degrees(z.hfov(f0, o.name)), 3), _r(math.degrees(z.hfov(f1, o.name)), 3)]}
                                   for o in z.outputs],
                       "exposure": dict(z.exposure), "jitter_urad": dict(z.jitter_urad)}
        night = s.doc.get("night") or {}
        if night:
            out["night"] = {"e_min_color_lx": night.get("e_min_color_lx"), "e_min_mono_lx": night.get("e_min_mono_lx")}
    il = s.illum
    if il is not None:
        if il.follow_hfov and z is not None:
            th = (z.hfov(z.f_min_mm), z.hfov(z.f_max_mm))
            rng = [_r(il.range_m(th[0]), 2), _r(il.range_m(th[1]), 2)]
            div = [_r(math.degrees(th[0]), 3), _r(math.degrees(th[1]), 3)]
        else:
            rng = [_r(il.range_m(il.divergence_rad), 2)]
            div = [_r(math.degrees(il.divergence_rad), 3)]
        out["illuminator"] = {"builtin": il.builtin, "wavelength_nm": il.wavelength_nm, "power_w": il.p_w,
                              "divergence": "follow_hfov" if il.follow_hfov else "fixed", "divergence_deg": div,
                              "range_m": rng, "ref": {"r_m": il.r_ref_m, "p_w": il.p_ref_w,
                                                     "theta_deg": _r(math.degrees(il.theta_ref_rad), 4), "rho": il.rho_ref}}
    if s.lenses:
        det = s.doc.get("detector") or {}
        w, h, p = int(det.get("width_px", 640)), int(det.get("height_px", 512)), float(det.get("pixel_um", 12.0))
        out["lenses"] = [{"f_mm": ln.f_mm, "f_number": ln.f_number, "mass_kg": _r(ln.mass_kg), "power_w": _r(ln.power_w),
                          "hfov_deg": _r(math.degrees(2 * math.atan(w * p * 1e-3 / (2 * ln.f_mm))), 3),
                          "vfov_deg": _r(math.degrees(2 * math.atan(h * p * 1e-3 / (2 * ln.f_mm))), 3),
                          "ifov_urad": _r(p / ln.f_mm * 1e3, 2)} for ln in s.lenses]
        out["lens_mm"] = _r(s.lens_mm)
    if s.kind == SensorKind.RADAR:
        doc = s.doc
        r_ref = float(doc.get("r_ref_m", float("nan")))
        sig = float(doc.get("sigma_ref_m2", 1.0))
        out["radar"] = {"freq_ghz": doc.get("freq_ghz"), "fov": dict(doc.get("fov") or {}), "r_ref_m": r_ref,
                        "snr_req_db": doc.get("snr_req_db"), "rate_hz": doc.get("rate_hz"), "r_max_m": doc.get("r_max_m"),
                        "r_detect_m": {k: _r(r_ref * (v / sig) ** 0.25, 2) for k, v in RCS_REF.items()}}
    if s.kind == SensorKind.ACOUSTIC:
        doc = s.doc
        out["acoustic"] = {k: doc.get(k) for k in ("n_mics", "aperture_m", "band_hz", "array_gain_db", "nr_db", "r_ego_m",
                                                   "az_acc_deg", "rate_hz", "detect_snr_db") if k in doc}
    if s.kind == SensorKind.LIDAR:
        rg = s.doc.get("range") or {}
        r10, ex, hm = float(rg.get("r_at_rho10_m", 40.0)), float(rg.get("exponent", 0.269)), float(rg.get("hard_max_m", s.range_m))
        fov = s.doc.get("fov") or {}
        omega = 2 * math.pi * (math.sin(math.radians(float(fov.get("v_max_deg", 52.2)))) +
                               math.sin(math.radians(-float(fov.get("v_min_deg", -7.2)))))
        pts = float((s.doc.get("pattern") or {}).get("point_rate_hz", 200000)) / float(s.doc.get("frame_rate_hz", 10) or 10)
        out["lidar_range"] = {"hard_max_m": hm, "r_max_m": {str(rho): _r(min(hm, r10 * (rho / 0.1) ** ex), 2)
                                                            for rho in (0.1, 0.2, 0.3, 0.5, 0.8, 1.0)},
                              "points_per_frame": int(pts), "density_pts_sr": _r(pts / omega, 1)}
    return out


def describe_rig(rig: Rig) -> dict[str, Any]:
    out: dict[str, Any] = {s.name: spec_json(s) for s in rig.specs}
    out["rig"] = [{"sensor_no": s.sensor_no, "name": s.name, "kind": KIND_NAMES[s.kind]} for s in rig.specs]
    return out


def _twin(d: Path, sensors_map: dict) -> list[dict]:
    rows = []
    for item, key in (("Camera", "camera"), ("LiDAR", "mid360"), ("RTK", "gnss")):
        rel = sensors_map.get(key)
        if isinstance(rel, dict):  # D2 对象形式槽位
            rel = rel.get("file")
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
        rig = load_rig(d, smap)
        out = describe_rig(rig)
        if rig.d2:
            from .mounts import mount_summary

            out["mounts"] = mount_summary(rig, rig.default_mask(sandbox=True))
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
