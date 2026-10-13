"""服务端查询的辅助：机型挂载 → 感知参数、可行窗口查询（M19-FR-025 的 SB-67，P1；17 §17.5、`sensor/perception.schema.json#/$defs`）。

`feasibility_query(rt, ctx, args)`：`{target, model_id, sensor, light?, mor_m?, mode?, level_req?}` →
`{feasible, d_lo_m, d_hi_m, why}`；`sensor` 为机型挂载的槽位名（eo、lwir…）或型号名（eo_gx40、lwir640）。错误：识别物不存在 543、
机型未知 520、机型无该传感器 560（含非成像传感器）、所需等级在该传感器上限下任何距离都不可达 562。缺省照度档与 MOR 取当前环境。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .feasibility import env_for, feasibility_detail
from .params import N_P90
from .specs import PerceptionError, SensorSpecP, eo_from_doc, illuminator_from_doc, lwir_from_doc

__all__ = ["feasibility_query", "model_sensor_spec"]


def _rig_of(model_id: str, profiles: Any) -> Any:
    from awr.sim.sensors.spec import load_rig, rig_for_model

    if profiles is not None and hasattr(profiles, "has") and profiles.has(model_id):
        p = profiles.get(model_id)
        src = getattr(p, "source", "")
        if src:
            return load_rig(Path(src).parent)
        return rig_for_model(str(getattr(p, "model", model_id)))
    return None


def model_sensor_spec(model_id: str, sensor: str, profiles: Any, *, light: str = "day") -> SensorSpecP:
    """机型（profile）上挂载的成像传感器 → SensorSpecP（夜间且有照明器时为 NIR；外挂照明器取跟随该云台的 `nir_illuminator`）。"""
    rig = _rig_of(model_id, profiles)
    if rig is None:
        raise PerceptionError(520, "model_id", "MODEL_UNKNOWN", value=model_id)
    sp = next((s for s in rig.specs if s.name == sensor), None) or next((s for s in rig.specs if s.model_id == sensor), None)
    if sp is None:
        raise PerceptionError(560, "sensor", "SENSOR_UNSUPPORTED", value=sensor, model_id=model_id)
    doc = sp.doc or {}
    sid = str(getattr(sp, "schema_id", "") or doc.get("schema", ""))
    if sid == "awr.sensor.eo_zoom.v1":
        ext = tuple(illuminator_from_doc(f.doc, kind=f.model_id or "nir_ill_l") for f in rig.specs
                    if f.on_gimbal_of == sp.name and str(f.schema_id) == "awr.sensor.nir_illuminator.v1")
        s = eo_from_doc(doc, name=sp.name, ext=ext)
        if light == "night" and s.illuminators:
            from dataclasses import replace

            s = replace(s, kind="nir")
        return s
    if sid == "awr.sensor.lwir.v1":
        return lwir_from_doc(doc, float(sp.lens_mm) if math.isfinite(float(sp.lens_mm)) else None, name=sp.name)
    if sp.intr is not None and sp.hfov_rad > 0:  # D1 相机与热像：虚拟像元
        fx = 0.5 * sp.intr.w / math.tan(0.5 * sp.hfov_rad)
        f = fx * 1e-3
        kind = "lwir" if sid == "awr.sensor.thermal.v1" else "eo"
        return SensorSpecP(sp.name, kind, 1.0, int(sp.intr.w), int(sp.intr.h), f, f, 0.0, 0.0, {}, "native", virtual=True,  # type: ignore[arg-type]
                           jitter_short_urad=20.0 if kind == "lwir" else 10.0, jitter_long_urad=20.0 if kind == "lwir" else 30.0)
    raise PerceptionError(560, "sensor", "NOT_IMAGING", value=sensor, model_id=model_id)


def feasibility_query(rt: Any, ctx: Any, a: dict) -> dict:
    trt = rt.trt
    tid = str(a.get("target", ""))
    cfg = trt.config(tid) if trt is not None else None
    if cfg is None:
        raise PerceptionError(543, "target", "TARGET_NOT_FOUND", value=tid)
    model_id = str(a.get("model_id", ""))
    profiles = getattr(ctx, "profiles", None) or getattr(trt, "profiles", None)
    if profiles is not None and hasattr(profiles, "has") and not profiles.has(model_id):
        raise PerceptionError(520, "model_id", "MODEL_UNKNOWN", value=model_id)
    t_ns = int(getattr(ctx, "t_ns", 0))
    env, _bg = rt.env(ctx, t_ns)
    light = str(a.get("light") or env.band)
    if light not in ("day", "low", "night"):
        raise PerceptionError(561, "light", "LIGHT_UNKNOWN", value=light)
    mor = float(a.get("mor_m") or (env.mor_m if math.isfinite(env.mor_m) else 20000.0))
    if not mor > 0:
        raise PerceptionError(561, "mor_m", "OUT_OF_RANGE", value=mor)
    mode = str(a.get("mode") or cfg.raw.get("criterion_mode", "geometric"))
    level_req = str(a.get("level_req") or cfg.raw.get("level_req", "R"))
    if level_req not in N_P90:
        raise PerceptionError(561, "level_req", "LEVEL_UNKNOWN", value=level_req)
    sn = model_sensor_spec(model_id, str(a.get("sensor", "")), profiles, light=light)
    from awr.sim.targets.signature import signature_of

    sig = signature_of(model_id, profiles)
    r = feasibility_detail(cfg, sn, light, mor, mode, sig, level_req=level_req, env=env_for(light, mor))  # type: ignore[arg-type]
    if r.why == "pixels":
        raise PerceptionError(562, "level_req", "LEVEL_UNREACHABLE", value=level_req, sensor=sn.name)
    return {"feasible": r.feasible, "d_lo_m": r.d_lo_m, "d_hi_m": r.d_hi_m, "why": r.why}
