"""机型摘要与成像传感器（M20 §6.7"估计项"、§6.9.2；M21 目录、M08 ProfileTable、M19 `model_sensor_spec`）。

- `model_info(T, model_id)`：`awr.tasking.model.ModelInfo`（构型、巡航与最大速度、悬停/巡航/盘旋功率、E_use、链路、抗风、盘旋半径下限、
  能力、扫描相机、电池包）。多旋翼巡航功率按 M09/M10 同式 `P_hover·(T/T_hover)^1.5`（v = 巡航速度、无风），固定翼与 VTOL 取 M21
  `fw_derived`；
- `station_specs(T, model_id, light)`：接力可用的成像传感器 `[(name, SensorSpecP)]`（M19 `model_sensor_spec`；夜间 EO 为 NIR）。
  P600 2.0.0 的 `sensors:` 仍为 D1 字符串映射（WP-02 遗留 L3，GX40 未进入挂载表），其载荷声明 `eo_gx40`（rgb.zoom）时以 M19 参考
  传感器 `gx40()`/`gx40_nir()` 代替（外挂照明器按机型目录中 `nir_ill_l` 挂载文件是否存在判定）。
"""

from __future__ import annotations

import logging
import math
from functools import lru_cache
from typing import Any

from awr.tasking.model import Camera, ModelInfo

__all__ = ["cameras_of", "model_info", "station_specs"]

log = logging.getLogger("awr.sim.tasking")
G = 9.80665
RHO = 1.225
FW_WIND_DESIGN_MPS = 10.0
FW_WIND_TAB_MPS = (0.0, 5.0, 10.0, 12.0, 14.0, 15.0, 16.0, 18.0, 20.0)   # 盘旋半径下限表的风速点（风下作业高度选择）


def _num(x: Any, d: float | None = None) -> float | None:
    if isinstance(x, dict):
        x = x.get("value", d)
    try:
        return float(x) if x is not None else d
    except (TypeError, ValueError):
        return d


def _rig(T: Any, model_id: str) -> Any:
    try:
        from awr.sim.perception.service import _rig_of

        return _rig_of(model_id, T)
    except Exception:
        return None


def _has_payload(doc: dict, name: str) -> bool:
    return any(isinstance(p, dict) and p.get("name") == name for p in doc.get("payload") or [])


_MEMO: dict[tuple, tuple[Any, Any]] = {}
_MEMO_MAX = 128


def _memo(key: tuple, T: Any, build: Any) -> Any:
    """按（机型表对象, 机型, 照度档）缓存挂载派生量（读挂载 YAML 约 30 ms，接力每次窗口与变焦都会用到）；机型表对象换了即失效。"""
    hit = _MEMO.get(key)
    if hit is not None and hit[0] is T:
        return hit[1]
    v = build()
    if len(_MEMO) >= _MEMO_MAX:
        _MEMO.clear()
    _MEMO[key] = (T, v)
    return v


def cameras_of(T: Any, model_id: str) -> tuple[list[Camera], list[str]]:
    """扫描与站位用的成像传感器：(Camera 列表, 接力传感器名列表)。"""
    cams, names = _memo(("cams", id(T), model_id), T, lambda: _cameras_of(T, model_id))
    return list(cams), list(names)


def _cameras_of(T: Any, model_id: str) -> tuple[list[Camera], list[str]]:
    cams: list[Camera] = []
    names: list[str] = []
    try:
        from awr.sim.perception.optics import p_eff_um
        from awr.sim.perception.service import model_sensor_spec
    except Exception:  # M19 未交付：只有 GX40 规格常量
        return [Camera("eo_gx40")], ["eo_gx40"]
    rig = _rig(T, model_id)
    for s in getattr(rig, "specs", ()) or ():
        sid = str(getattr(s, "schema_id", "") or (s.doc or {}).get("schema", ""))
        if sid not in ("awr.sensor.eo_zoom.v1", "awr.sensor.lwir.v1"):
            continue
        try:
            sp = model_sensor_spec(model_id, s.name, T)
        except Exception:
            continue
        out = sp.outputs.get(sp.analysis) if sp.outputs else None
        w_out = int(out[0]) if out else int(sp.w_native_px)
        crop = int(out[1]) if out else int(sp.w_native_px)
        h_out = round(w_out * sp.h_native_px / max(crop, 1))
        fixed = (float(sp.f_min_mm),) if sp.kind == "lwir" or sp.f_min_mm == sp.f_max_mm else ()
        cams.append(Camera(s.name if sp.kind == "lwir" else (s.model_id or s.name), "lwir" if sp.kind == "lwir" else "eo", w_out, h_out,
                           float(p_eff_um(sp)), float(sp.f_min_mm), float(sp.f_max_mm), fixed,
                           float(sp.t_int_s if sp.kind == "lwir" else sp.t_exp_day_s), 2.0))
        names.append(s.name)
    prof = T.get(model_id) if T is not None and T.has(model_id) else None
    doc = getattr(prof, "doc", {}) or {}
    if not any(c.kind == "eo" for c in cams) and _has_payload(doc, "eo_gx40"):
        from awr.sim.perception.specs import gx40

        sp = gx40("1080p")
        cams.append(Camera("eo_gx40", "eo", 1920, 1080, float(p_eff_um(sp)), sp.f_min_mm, sp.f_max_mm, (), float(sp.t_exp_day_s), 2.0))
        names.append("eo_gx40")
    return cams, names


@lru_cache(maxsize=16)
def _catalog_packs(vdir: str) -> dict[str, dict]:
    try:
        from pathlib import Path

        from awr.sim.fleet.profiles import read_catalog

        cat = read_catalog(Path(vdir)) or {}
        return {str(p["pack_id"]): dict(p) for p in cat.get("packs") or []}
    except Exception:
        return {}


def _mr_cruise_power(prof: Any, v: float) -> float | None:
    """多旋翼巡航功率：M09/M10 同式 `P_hover·(T/T_hover)^1.5`，`T = √(W² + (k·v)²)`，`k = ½ρ·CdA·v + Σω·c_rd`（参数取 Profile，与
    `awr.sim.mission.energy.EnergyParams.from_profile` 同口径；不导入 M10 包以免其插件在导入时登记）。"""
    try:
        bat = getattr(prof, "battery", None) or {}

        def val(k: str, d: float) -> float:
            x = bat.get(k) if isinstance(bat, dict) else None
            return float(x["value"]) if isinstance(x, dict) and "value" in x else (float(x) if isinstance(x, (int, float)) else d)

        m = float(getattr(prof, "mass_kg", 3.5))
        tmax = float(getattr(prof, "t_max_n", 76.92))
        om = float(getattr(prof, "omega_max_rad_s", 1500.0)) * math.sqrt(max(m * G / tmax, 0.0))
        omega_sum = int(getattr(prof, "n_rot", 4)) * om
        p_hover = val("p_hover_w", 515.0)
        th = m * G
        k = 0.5 * RHO * float(getattr(prof, "cda_m2", 0.035)) * v + omega_sum * float(getattr(prof, "c_rd", 1.05e-4))
        T = math.sqrt(th * th + (k * v) ** 2)
        return float(p_hover * (T / th) ** 1.5)
    except Exception:
        return None


def model_info(T: Any, model_id: str) -> ModelInfo:
    prof = T.get(model_id)
    doc = getattr(prof, "doc", {}) or {}
    from awr.sim.catalog import derive as D
    from awr.sim.catalog.loader import capabilities_of, category_of

    frame = category_of(doc)
    lid = None
    try:
        from awr.sim.fleet import kernels_l1 as K

        lid = T.limits_index(prof.sandbox_limits, T.key_of(model_id))
        v_cr = float(T.LT[lid, K.L_CRUISE])
        v_mx = float(T.LT[lid, K.L_VXY])
        climb = float(T.LT[lid, K.L_ZUP])
    except Exception:
        v_cr, v_mx, climb = 8.0, 12.0, 3.0
    e_use = D.e_use_wh(doc) or 150.0
    p_hover = D.multirotor_hover_power_w(doc) or 400.0
    fwd = D.fw_derived(doc) if frame in ("fixed_wing", "vtol") else {}
    fw = D.fw_aero(doc) if frame in ("fixed_wing", "vtol") else None
    if frame == "vtol":
        vt = doc.get("vtol") if isinstance(doc.get("vtol"), dict) else {}
        p_hover = _num(vt.get("p_hover_w"), p_hover) or p_hover
    if fw is not None:
        v_cr, v_mx = float(fw.v_cruise_mps), float(fw.v_max_mps)
        p_cruise = float(fwd.get("p_cruise_w", p_hover))
        p_loiter = float(fwd.get("p_loiter_w", p_cruise))
        try:
            r_min = float(fw.r_loiter_min(FW_WIND_DESIGN_MPS))
        except Exception:
            r_min = float(fwd.get("r_min_loiter_m", 80.0))
        tab = []
        for wv in FW_WIND_TAB_MPS:
            try:
                tab.append((float(wv), float(fw.r_loiter_min(float(wv)))))
            except Exception:
                break
        r_tab = tuple(tab)
    else:
        p_cruise = _mr_cruise_power(prof, v_cr) or 1.05 * p_hover
        p_loiter = p_hover
        r_min = 0.0
        r_tab = ()
    link = doc.get("link") if isinstance(doc.get("link"), dict) else {}
    wind = _num(doc.get("wind_rating_mps"), None) or _num((doc.get("env_limits") or {}).get("wind_mps"), 12.0) or 12.0
    cams, names = cameras_of(T, model_id)
    try:
        rig = _rig(T, model_id)
        caps = capabilities_of(doc, list(getattr(rig, "specs", ()) or ()))
    except Exception:
        caps = sorted({str(c) for c in doc.get("capabilities") or []})
    bat = doc.get("battery") if isinstance(doc.get("battery"), dict) else {}
    pk = _catalog_packs(str(getattr(T, "vdir", "") or "")).get(str(bat.get("pack_id") or ""), {})
    pack = {"pack_id": bat.get("pack_id"), "cells": int(_num(bat.get("cells"), pk.get("cells", 6)) or 6),
            "capacity_ah": float(_num(bat.get("capacity_ah"), pk.get("capacity_ah", 10.0)) or 10.0),
            "c_max": float(pk.get("c_max", 1.0)), "packs_per_swap": int(_num(bat.get("packs_per_swap"), pk.get("packs_per_swap", 1)) or 1)}
    return ModelInfo(model_id, frame, v_cr, v_mx, climb, float(getattr(prof, "mass_kg", 3.8)), float(p_hover), float(p_cruise), float(p_loiter),
                     float(e_use), float(_num(link.get("range_m"), 3500.0) or 3500.0), float(wind), r_min, tuple(caps), tuple(cams),
                     tuple(names), pack, r_tab)


def station_specs(T: Any, model_id: str, light: str = "day") -> list[tuple[str, Any]]:
    """接力用成像传感器 `[(name, SensorSpecP)]`（夜间 EO 为 NIR，含跟随云台的外挂照明器）。"""
    return list(_memo(("specs", id(T), model_id, light), T, lambda: _station_specs(T, model_id, light)))


def _station_specs(T: Any, model_id: str, light: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    try:
        from awr.sim.perception.service import model_sensor_spec
        from awr.sim.perception.specs import gx40, gx40_nir
    except Exception:
        return out
    _cams, names = cameras_of(T, model_id)
    for n in names:
        if n == "eo_gx40" and not any(getattr(s, "name", "") == "eo_gx40" for s in getattr(_rig(T, model_id), "specs", ()) or ()):
            prof = T.get(model_id)
            ext = False
            try:
                from pathlib import Path

                ext = (Path(prof.model_dir) / "sensors" / "nir_ill_l.yaml").exists() and "nir_ill_l" in (prof.sensors_map or {})
            except Exception:
                ext = False
            out.append((n, gx40_nir(ext) if light == "night" else gx40("1080p")))
            continue
        try:
            out.append((n, model_sensor_spec(model_id, n, T, light=light)))
        except Exception:
            continue
    return out
