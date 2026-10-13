"""挂载组与实例覆盖（M13-FR-202；AWR-20 §7.4、§7.7、§8.2 MT-1、MT-6、SN 接口级）。

机队实例的传感器配置有三种写法，统一为 `{槽位: {enabled?, analysis?, lens_mm?}}`：

- AWR-20 §7.7：`{"eo": {"enabled": true, "analysis": "1080p"}, "nir_ill": {"enabled": true}}`；
- 17 §17.6.3 `POST .../vehicles`：`[{name, type?, enabled}]`；
- M13-FR-202：`[{name, mounted?, lens_mm?, analysis_res?, rpy_deg?}]`。

规则：未列出的槽位取缺省挂载（对象形式且 `default = true`）；不存在的槽位或在沙盒实例中启用 D1 槽位为
`560 SENSOR_UNSUPPORTED`；拆除 `removable = false` 的槽位为 `522 VEHICLE_CLONE_INVALID`（detail `SLOT_NOT_REMOVABLE`）；
`analysis = 4k` 需要型号 `analysis.k4_requires` 所列的机型能力（P600 为 `compute.orin_nx`），否则
`561 SENSOR_PARAM_INVALID`；`lens_mm` 须在型号 `lenses[]` 内（561）；跟随者（`on_gimbal_of`）须与被跟随者一同挂载（560）。
`rpy_deg` 覆盖本版不支持（561，detail `MOUNT_RPY_OVERRIDE_UNSUPPORTED`）。质量与功率汇总供 M08 的挂载检查与 M21 的续航复算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .gimbal import SensorError
from .spec import Rig

__all__ = ["MountResult", "mount_summary", "normalize_mounts", "resolve_mounts"]


@dataclass(frozen=True)
class MountResult:
    mask: int
    lens: dict[int, float] = field(default_factory=dict)       # sensor_no -> 镜头焦距（mm）
    analysis: dict[int, str] = field(default_factory=dict)     # sensor_no -> 分析分辨率名


def normalize_mounts(mounts: Any) -> dict[str, dict]:
    if mounts is None:
        return {}
    if isinstance(mounts, dict):
        return {str(k): dict(v or {}) if isinstance(v, dict) else {"enabled": bool(v)} for k, v in mounts.items()}
    out: dict[str, dict] = {}
    for it in mounts:
        if not isinstance(it, dict) or "name" not in it:
            raise SensorError(110, "MOUNT_ENTRY_INVALID", entry=it)
        d = {k: v for k, v in it.items() if k not in ("name", "type")}
        if "mounted" in d and "enabled" not in d:
            d["enabled"] = d.pop("mounted")
        if "analysis_res" in d and "analysis" not in d:
            d["analysis"] = d.pop("analysis_res")
        out[str(it["name"])] = d
    return out


def resolve_mounts(rig: Rig, mounts: Any, capabilities: list[str] | tuple[str, ...] | None = None, *,
                   allow_legacy: bool = False) -> MountResult:
    """实例挂载 -> 掩码与覆盖；不合法时抛 SensorError（560、561、522）。`rig` 为机型的完整传感器组。"""
    root = rig.base or rig
    caps = set(capabilities or ())
    m = root.default_mask(sandbox=not allow_legacy)
    lens: dict[int, float] = {}
    analysis: dict[int, str] = {}
    for slot, o in normalize_mounts(mounts).items():
        sp = root.by_name(slot)
        if sp is None:
            raise SensorError(560, "SLOT_UNKNOWN", sensor=slot, slots=[s.name for s in root.specs])
        bit = 1 << sp.sensor_no
        en = o.get("enabled")
        if en is True:
            if sp.slot.legacy and not allow_legacy:
                raise SensorError(560, "LEGACY_SLOT_NOT_MOUNTABLE", sensor=slot)
            m |= bit
        elif en is False:
            if not sp.slot.removable:
                raise SensorError(522, "SLOT_NOT_REMOVABLE", sensor=slot)
            m &= ~bit
        an = o.get("analysis")
        if an is not None:
            z = sp.zoom
            if z is None:
                raise SensorError(561, "ANALYSIS_UNSUPPORTED", sensor=slot, field="analysis")
            names = [x.name for x in z.outputs]
            if str(an) not in names:
                raise SensorError(561, "ANALYSIS_UNKNOWN", sensor=slot, field="analysis", value=an, allowed=names)
            if str(an) == "4k" and z.k4_requires and z.k4_requires not in caps:
                raise SensorError(561, "ANALYSIS_4K_NEEDS_CAPABILITY", sensor=slot, field="analysis", requires=z.k4_requires)
            analysis[sp.sensor_no] = str(an)
        lm = o.get("lens_mm")
        if lm is not None:
            fl = [x.f_mm for x in sp.lenses]
            if not fl or not any(abs(float(lm) - f) < 1e-9 for f in fl):
                raise SensorError(561, "LENS_UNKNOWN", sensor=slot, field="lens_mm", value=lm, allowed=fl)
            lens[sp.sensor_no] = float(lm)
        if o.get("rpy_deg") is not None:
            raise SensorError(561, "MOUNT_RPY_OVERRIDE_UNSUPPORTED", sensor=slot, field="rpy_deg")
    for sp in root.specs:
        if (m >> sp.sensor_no) & 1 and sp.on_gimbal_of is not None:
            lead = root.by_name(sp.on_gimbal_of)
            if lead is None or not (m >> lead.sensor_no) & 1:
                raise SensorError(560, "LEADER_NOT_MOUNTED", sensor=sp.name, on_gimbal_of=sp.on_gimbal_of)
    return MountResult(m, lens, analysis)


def mount_summary(rig: Rig, mask: int | None = None) -> dict:
    """挂载质量与功率汇总（M13-FR-202、FR-216）：`{mass_kg, power_w, slots[{slot, sensor_no, model_id, mounted, default,
    removable, legacy, mass_kg, power_w, conf}]}`；未给质量或功率的 D1 文件不计入合计（`*_missing` 列出）。"""
    root = rig.base or rig
    if mask is None:
        mask = rig.mask or root.default_mask(sandbox=True)
    mass = power = 0.0
    miss_m: list[str] = []
    miss_p: list[str] = []
    slots = []
    for sp in root.specs:
        on = bool((int(mask) >> sp.sensor_no) & 1)
        eff = next((x for x in rig.specs if x.sensor_no == sp.sensor_no), sp) if rig.base is not None else sp
        slots.append({"slot": sp.name, "sensor_no": sp.sensor_no, "model_id": sp.model_id or None, "mounted": on,
                      "default": sp.slot.default and not sp.slot.legacy, "removable": sp.slot.removable, "legacy": sp.slot.legacy,
                      "mass_kg": _r(eff.mass_kg), "power_w": _r(eff.power_w), "conf": sp.conf})
        if not on:
            continue
        if math.isfinite(eff.mass_kg):
            mass += eff.mass_kg
        else:
            miss_m.append(sp.name)
        if math.isfinite(eff.power_w):
            power += eff.power_w
        else:
            miss_p.append(sp.name)
    return {"mass_kg": round(mass, 6), "power_w": round(power, 6), "mass_missing": miss_m, "power_missing": miss_p, "slots": slots}


def _r(x: float) -> float | None:
    return None if not math.isfinite(float(x)) else round(float(x), 6)
