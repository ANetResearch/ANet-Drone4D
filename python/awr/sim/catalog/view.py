"""目录 REST 视图（CAT-FR-018；AWR-20 §9；17 §17.6.2；契约 `vehicle/catalog.schema.json#/$defs/{models, model_summary,
model_detail, sensors, sensor_item}`）。

公开目录接口 `GET /api/sandbox/v1/catalog/{models, models/{id}, sensors}`（SB-21 至 SB-23）的响应体由本模块按目录、机型文件、
型号库与 M13 的有效传感器规格离线生成，写入 `rest_view.json`（规范化 JSON：键排序、UTF-8、紧凑分隔符）；`rest/catalog.py`
只读取该文件（awr.api 不导入 awr.sim，AWR-03 §4.2），ETag 为内容 sha256 的前 16 位（17 §17.5 第 5 条）。`python -m
awr.sim.catalog.check` 校验该文件与源数据一致（`--write-view` 重新生成）。

- 摘要（SB-21 条目）：`model_summary` 的字段（契约 additionalProperties: false）；
- 详情（SB-22）：摘要 + `profile`（awr.vehicle.v1 文档）+ `params_table`（分组参数表，置信度 A–D 或 label 与仿真用途）+
  `derived`（数值）+ AWR-20 §9 的 `catalog{reference, disclaimer_zh, …}`、`slots[]`、`signature`、`fw`、`checks`、`sensors`、
  `capabilities`、`battery`；
- 传感器（SB-23 条目）：`sensor_item`（`spec` 按类别给 17 §17.6.2 表中的字段，另含 `model_version`、`derived`、`conf`、`src`）。

全部取值确定（无时间戳、无随机数，CAT-NFR-002）：同一输入两次生成逐字节一致。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from ..fleet import profiles as PF
from . import derive as D
from .charge import S_FULL, pack_charge_time_s
from .checks import default_rig, fw_checks, mount_checks, vt_checks
from .loader import DISCLAIMER_ZH, Catalog, capabilities_of, category_of
from .signature import sg_checks, signature_summary

__all__ = ["VIEW_PATH", "VIEW_SCHEMA", "build_view", "canonical_bytes", "detail_of", "etag_of", "model_detail", "model_summary",
           "params_table", "sensor_items", "summary_of", "write_view"]

VIEW_SCHEMA = "awr.catalog_rest_view.v1"
VIEW_PATH = Path(__file__).with_name("rest_view.json")
WIND_TABLE_REF_MPS = 10.0
KIND_OF_SCHEMA = {"awr.sensor.eo_zoom.v1": "eo", "awr.sensor.nir_illuminator.v1": "nir_illuminator",
                  "awr.sensor.lwir.v1": "lwir", "awr.sensor.radar_mmw.v1": "radar",
                  "awr.sensor.acoustic_array.v1": "acoustic", "awr.sensor.lidar_livox.v1": "lidar"}


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def etag_of(obj: Any) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()[:16]


def _r(x: Any, nd: int = 4) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def _clean(obj: Any) -> Any:
    """JSON 兼容（非有限浮点为 null，tuple 为 list）。"""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def _limits_of(doc: dict) -> dict:
    lp = doc.get("limits_profiles") or {}
    name = (doc.get("catalog") or {}).get("limits_default") or lp.get("default")
    v = lp.get(str(name)) if name else None
    return dict((v or {}).get("px4_params") or {}) if isinstance(v, dict) else {}


# ---------------------------------------------------------------- 摘要与详情
def _sensor_lists(doc: dict, mdir: Path | None) -> tuple[list[str], list[str], Any]:
    """缺省与可选挂载的传感器型号 id（对象形式槽位）；纯 D1 映射的机型（P600 2.0.0 过渡期）取 `payload[]` 中型号库已有的名称。"""
    from awr.sim.sensors.spec import load_rig

    smap = doc.get("sensors") or {}
    rig = None
    if mdir is not None and smap:
        try:
            rig = load_rig(mdir, dict(smap))
        except Exception:
            rig = None
    dflt: list[str] = []
    opt: list[str] = []
    if rig is not None and rig.d2:
        for s in rig.specs:
            if s.slot.legacy or not (s.model_id or s.schema_id in KIND_OF_SCHEMA):
                continue
            mid = s.model_id or s.name
            (dflt if s.slot.default else opt).append(mid)
    else:
        lib = mdir.parent / "sensor_models" if mdir is not None else None
        for p in doc.get("payload") or []:
            name = str(p.get("name"))
            if lib is not None and (lib / f"{name}.yaml").exists():
                dflt.append(name)
    return dflt, opt, rig


def model_summary(cat: Catalog, pid: str) -> dict:
    return summary_of(cat.docs[pid][0], cat.entry(pid) or {}, cat.model_dir(pid), model_id=pid)


def summary_of(doc: dict, e: dict, mdir: Path | None, *, model_id: str | None = None, session_clone: bool = False) -> dict:
    """摘要（契约 `model_summary`）：机型文档、目录条目与机型包目录 -> 条目；会话克隆取源机型的目录条目。"""
    pid = str(model_id or doc.get("id"))
    cat_name = category_of(doc)
    lim = _limits_of(doc)
    fw = D.fw_aero(doc)
    if fw is not None and cat_name == "fixed_wing":
        speed = {"cruise_mps": fw.v_cruise_mps, "max_mps": fw.v_max_mps, "min_mps": fw.v_min_mps, "stall_mps": _r(fw.v_stall(), 2)}
    else:
        speed = {"cruise_mps": float(lim.get("MPC_XY_CRUISE", 0.0)), "max_mps": float(lim.get("MPC_XY_VEL_MAX", 0.0))}
        if fw is not None:  # VTOL：巡航与最大取固定翼段，MC 模式见详情的 limits
            speed = {"cruise_mps": fw.v_cruise_mps, "max_mps": fw.v_max_mps, "min_mps": fw.v_min_mps,
                     "stall_mps": _r(fw.v_stall(), 2)}
    dflt, opt, _rig = _sensor_lists(doc, mdir)
    name = doc.get("display_name") if session_clone else (e.get("display_name_zh") or doc.get("display_name"))
    out = {"model_id": pid, "name_zh": str(name or pid), "category": cat_name,
           "reference": bool(e.get("reference", (doc.get("catalog") or {}).get("reference", False))),
           "version": str(doc.get("profile_version")), "mass_kg": D.num(doc.get("mass_kg")),
           "endurance": {k: _r(v, 1) for k, v in D.endurance_summary(doc).items()}, "speed": speed,
           "default_sensors": dflt, "optional_sensors": opt, "nest_kinds": list(e.get("nest_kinds") or []),
           "session_clone": bool(session_clone)}
    mtow = D.num(doc.get("mtow_kg"))
    if mtow is not None:
        out["mtow_kg"] = mtow
    lk = (doc.get("link") or {}).get("range_m")
    if lk is not None:
        out["link_range_m"] = float(lk)
    return out


_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str | None, str], ...]], ...] = (
    ("飞行器", (("frame.type", "构型", None, "选择 L1 多旋翼或 fw_kin 固定翼模型"),
             ("mass_kg", "起飞重量", "kg", "悬停推力、能量、挂载余量"), ("mtow_kg", "最大起飞重量", "kg", "VH-7、MT-3 挂载校验"),
             ("geometry.wheelbase_m", "对角轴距", "m", "碰撞半径、低模"), ("geometry.size_m", "外形尺寸 l × w × h", "m", "包围盒、可见截面"),
             ("geometry.collision_radius_m", "碰撞半径", "m", "机间间距与碰撞"),
             ("battery.hover_endurance_s", "悬停续航", "s", "反推悬停功率（ADR-052）"), ("wind_rating_mps", "抗风", "m/s", "盘旋与站位可行性"),
             ("nav.hold_sigma_m.rtk", "悬停精度（RTK）", "m", "站位保持误差注入（机巢有 RTK 基站时）"),
             ("nav.hold_sigma_m.slam", "悬停精度（激光 SLAM）", "m", "无 RTK 基站时"),
             ("nav.hold_sigma_m.gnss", "悬停精度（GNSS）", "m", "无 RTK 时"),
             ("env_limits.temp_c", "工作温度", "°C", "环境越界告警（EN-1，只告警）"))),
    ("飞控", (("labels.fcu", "飞控", None, "标签（不进入物理）"),)),
    ("机载计算", (("capabilities", "能力标签", None, "M20 能力匹配；compute.orin_nx 允许 4K 分析分辨率"),
               ("compute_tops", "算力", "TOPS", "标签（不进入物理）"), ("labels.compute", "机载计算", None, "标签（不进入物理）"))),
    ("动力电池", (("battery.cells", "节数", None, "MT-8 机巢充电器兼容"), ("battery.chemistry", "化学体系", None, "OCV 显示曲线"),
               ("battery.capacity_ah", "容量", "Ah", "能量模型、换电库存"), ("battery.v_nominal_v", "标称电压", "V", "能量模型"),
               ("battery.capacity_wh", "能量", "Wh", "能量模型"), ("battery.usable_frac", "可用比例", None, "E_use = 0.85·E_nom"),
               ("battery.mass_kg", "电池质量", "kg", "质量预算"), ("battery.v_range_v", "电压范围", "V", "标签（显示）"),
               ("battery.v_storage_v", "存放电压", "V", "标签（不进入物理）"), ("battery.pack_id", "电池包", None, "换电库存与充电"),
               ("battery.packs_per_swap", "每次换电块数", None, "电池队列"))),
    ("遥控器", (("labels.rc", "遥控器", None, "标签（D2 不模拟射频）"),)),
    ("数传", (("link.range_m", "链路距离", "m", "站位与机巢距离可行性"), ("link.model", "数传型号", None, "标签"),
            ("labels.link", "数传", None, "标签（不进入物理）"))),
    ("RTK", (("labels.rtk", "RTK 基站", None, "精度档选择（机巢 rtk_base）"),)),
    ("充电器", (("labels.charger", "充电器", None, "充电时间模型（AWR-20 §4.5）"),)),
    ("L1 参数", (("motor.t_max_n", "单桨最大推力", "N", "L1 多旋翼"), ("motor.omega_max_rad_s", "最大转速", "rad/s", "L1 多旋翼"),
               ("motor.k_f", "推力系数", "N·s²/rad²", "L1 多旋翼"), ("motor.tau_s", "电机时间常数", "s", "L1 多旋翼"),
               ("prop.d_m", "桨径", "m", "L1 多旋翼"), ("prop.c_rd", "转子阻力系数", None, "风中悬停倾角"),
               ("aero.cda_m2", "阻力面积 CdA", "m²", "风中悬停倾角"), ("inertia_kgm2", "惯量", "kg·m²", "姿态响应"))),
    ("固定翼", (("airframe.span_m", "翼展", "m", "fw_kin"), ("airframe.length_m", "机长", "m", "fw_kin"),
              ("airframe.wing_area_m2", "翼面积", "m²", "能量模型、失速速度"), ("airframe.cd0", "零升阻力系数", None, "能量模型"),
              ("airframe.oswald_e", "Oswald 效率", None, "诱导功率"), ("airframe.cl_max", "最大升力系数", None, "失速速度"),
              ("airframe.eta_prop", "推进总效率", None, "能量模型"), ("airframe.p_avionics_w", "航电功率", "W", "能量模型"),
              ("airframe.p_max_w", "推进持续功率", "W", "FW-6"), ("airframe.v_min_mps", "最小空速", "m/s", "准入（110）"),
              ("airframe.v_loiter_mps", "盘旋空速", "m/s", "盘旋功率与半径"), ("airframe.v_cruise_mps", "巡航空速", "m/s", "巡航功率与续航"),
              ("airframe.v_max_mps", "最大空速", "m/s", "准入（110）"), ("airframe.phi_max_deg", "最大滚转", "°", "转弯半径"),
              ("airframe.climb_max_mps", "最大爬升率", "m/s", "fw_kin"), ("airframe.sink_max_mps", "最大下沉率", "m/s", "fw_kin"),
              ("airframe.r_loiter_default_m", "缺省盘旋半径", "m", "hover 映射为盘旋"),
              ("airframe.launch", "起飞方式", None, "takeoff 映射"), ("airframe.recovery", "回收方式", None, "land 映射"))),
    ("VTOL", (("vtol.transition_fw_s", "转固定翼过渡", "s", "VTOL 状态机"), ("vtol.transition_mc_s", "转多旋翼过渡", "s", "VTOL 状态机"),
             ("vtol.p_hover_w", "悬停功率", "W", "MC 模式与过渡段功率"), ("vtol.hover_max_s", "单架次悬停上限", "s", "524 VTOL_HOVER_LIMIT"),
             ("vtol.d_fw_min_m", "转固定翼最短航段", "m", "VTOL 状态机"))),
    ("签名", (("acoustic.lwa_hover_dba", "悬停声功率", "dB(A)", "被发现判据（声学）"),
            ("acoustic.lwa_cruise_dba", "巡航声功率", "dB(A)", "被发现判据（声学）"), ("acoustic.bpf_hz", "桨叶通过频率", "Hz", "谱形"),
            ("acoustic.bpf_cruise_hz", "巡航桨叶通过频率", "Hz", "谱形"), ("visual.area_m2", "可见截面", "m²", "被发现判据（视觉）"),
            ("visual.contrast_sky", "天空背景对比度", None, "被发现判据（视觉）"), ("rcs_m2", "雷达截面", "m²", "雷达检测"),
            ("ir.motor_delta_t_k", "电机温升", "K", "红外签名"))),
)


def _get(doc: dict, path: str) -> Any:
    cur: Any = doc
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _conf_of(doc: dict, path: str, raw: Any) -> str:
    if path.startswith("labels.") or path in ("compute_tops", "link.model", "battery.v_storage_v"):
        return "label"
    if isinstance(raw, dict) and raw.get("conf") in ("A", "B", "C", "D"):
        return str(raw["conf"])
    head = path.split(".", 1)[0]
    parent = doc.get(head)
    if isinstance(parent, dict) and parent.get("conf") in ("A", "B", "C", "D"):
        return str(parent["conf"])
    if (doc.get("catalog") or {}).get("reference"):
        return "D"
    return "D" if head in ("visual", "ir", "acoustic", "airframe", "vtol") else "B"


def params_table(doc: dict, derived: dict[str, Any], slots: list[dict]) -> list[dict]:
    """分组参数表（14 §20.6.2；AWR-04 §5.1 的分组）：`{group, key, label_zh, value, unit, confidence, use_zh}`。"""
    rows: list[dict] = []
    for group, fields in _GROUPS:
        for path, label, unit, use in fields:
            raw = _get(doc, path)
            if raw is None:
                continue
            val = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
            rows.append({"group": group, "key": path, "label_zh": label, "value": _clean(val), "unit": unit,
                         "confidence": _conf_of(doc, path, raw), "use_zh": use})
    for s in slots:
        if s.get("legacy"):
            continue
        rows.append({"group": "挂载", "key": f"sensors.{s['slot']}", "label_zh": f"{s['slot']}（{s.get('model_id') or '—'}）",
                     "value": {"default": s["default"], "removable": s["removable"], "mass_kg": s.get("mass_kg"),
                               "power_w": s.get("power_w")}, "unit": None, "confidence": s.get("conf") or "D",
                     "use_zh": "感知、质量与功率预算"})
    for k in sorted(derived):
        v = derived[k]
        if isinstance(v, (int, float)):
            rows.append({"group": "派生", "key": f"derived.{k}", "label_zh": k, "value": v, "unit": None, "confidence": "D",
                         "use_zh": "由参数计算（加载时与文件比对）"})
    return rows


def _slots(rig: Any) -> list[dict]:
    if rig is None:
        return []
    from awr.sim.sensors.mounts import mount_summary

    base = rig.base or rig
    ms = mount_summary(base, base.default_mask(sandbox=True) if base.d2 else base.all_mask)
    out = []
    for x in ms["slots"]:
        sp = base.by_name(x["slot"])
        out.append({"slot": x["slot"], "sensor_no": x["sensor_no"], "model_id": x["model_id"],
                    "type": (sp.schema_id.split(".")[2] if sp is not None and sp.schema_id else None),
                    "default": bool(x["default"]), "removable": bool(x["removable"]), "legacy": bool(x["legacy"]),
                    "mass_kg": x["mass_kg"], "power_w": x["power_w"], "conf": x["conf"],
                    "on_gimbal_of": sp.on_gimbal_of if sp is not None else None})
    return out


def model_detail(cat: Catalog, pid: str) -> dict:
    return detail_of(cat.docs[pid][0], cat.entry(pid) or {}, cat.model_dir(pid), cat.doc, model_id=pid)


def detail_of(doc: dict, e: dict, mdir: Path | None, cat_doc: dict, *, model_id: str | None = None, session_clone: bool = False,
              with_sensors: bool = True, checks: list[dict] | None = None, extra_w: float = 0.0) -> dict:
    """详情（SB-22；会话克隆的 SB-25、SB-26 回复）。`checks` 给出时不再重算（会话克隆取 M08 克隆时的检查结果）；`extra_w`
    为克隆挂载变化的附加电功率（固定翼功率与续航按 AWR-20 §5.5 计入）；`with_sensors = False` 时省略 M13 的传感器描述。"""
    pid = str(model_id or doc.get("id"))
    summ = summary_of(doc, e, mdir, model_id=pid, session_clone=session_clone)
    ft = PF.frame_type_of(doc)
    der: dict[str, Any] = {}
    if ft in PF.MULTIROTOR_FRAMES or ft == "vtol_quadplane":
        der.update({k: v for k, v in PF.derived_values(doc).items() if v is not None})
    der.update(D.fw_derived(doc, extra_w=extra_w))
    eu = D.e_use_wh(doc)
    if eu is not None:
        der["e_use_wh"] = eu
    ph = D.multirotor_hover_power_w(doc) if ft in PF.MULTIROTOR_FRAMES else D.num((doc.get("vtol") or {}).get("p_hover_w"))
    if ph is not None:
        der["p_hover_w"] = ph
    fw = D.fw_aero(doc)
    if fw is not None:
        der["r_min_m"] = fw.r_min(fw.v_loiter_mps)
        der["r_loiter_min_w10_m"] = fw.r_loiter_min(WIND_TABLE_REF_MPS)
        der["v_mp_mps"], der["v_md_mps"] = fw.v_mp, fw.v_md
    vp = D.vtol_profile(doc)
    if vp is not None:
        der["mission_total_s"] = vp["total_s"]
        der["e_vert_wh"] = vp["e_vert_wh"]
    derived = {k: _r(v, 4) for k, v in der.items() if isinstance(v, (int, float))}
    _dflt, _opt, rig = _sensor_lists(doc, mdir)
    slots = _slots(rig)
    if checks is None:
        checks = []
        if ft in PF.MULTIROTOR_FRAMES:
            checks += PF.run_checks(doc)
        elif ft == "vtol_quadplane":
            checks += [c for c in PF.run_checks(doc) if c["id"] in ("VH-1", "VH-2", "VH-3")]
        if ft in ("fixed_wing", "vtol_quadplane"):
            checks += fw_checks(doc)
        if ft == "vtol_quadplane":
            checks += vt_checks(doc)
        checks += sg_checks(doc) + mount_checks(doc, mdir) + [PF.mount_check(doc, mdir)]
    sensors: dict[str, Any] = {}
    if mdir is not None and with_sensors:
        from awr.sim.sensors.describe import describe_model

        sensors = describe_model(mdir)
    mounted = list((default_rig(doc, mdir) or _EmptyRig()).specs)
    bat = doc.get("battery") if isinstance(doc.get("battery"), dict) else None
    battery: dict[str, Any] | None = None
    if bat is not None:
        pack = next((p for p in cat_doc.get("packs") or [] if p.get("pack_id") == bat.get("pack_id")), None)
        battery = {"pack": pack, "chargers": []}
        if pack is not None:
            for ch in cat_doc.get("chargers") or []:
                t = pack_charge_time_s(pack, ch, 0.0, S_FULL)
                battery["chargers"].append({"model": ch["model"], "compatible": math.isfinite(t),
                                            "charge_time_s": _r(t, 1) if math.isfinite(t) else None})
    out = dict(summ)
    out.update({"profile": _clean(json.loads(json.dumps(doc, default=str))), "params_table": params_table(doc, derived, slots),
                "derived": derived, "frame_type": ft, "frame_kind": PF.frame_kind_of(doc),
                "catalog": {"reference": summ["reference"], "disclaimer_zh": DISCLAIMER_ZH if summ["reference"] else None,
                            "size_class": e.get("size_class"), "order": e.get("order"), "summary_zh": e.get("summary_zh"),
                            "limits_default": (doc.get("catalog") or {}).get("limits_default"), "visible": bool(e.get("visible"))},
                "limits": _limits_of(doc), "slots": slots, "signature": _clean(signature_summary(doc)),
                "capabilities": capabilities_of(doc, mounted), "checks": _clean(checks), "sensors": _clean(sensors),
                "battery": _clean(battery)})
    if fw is not None:
        out["fw"] = _clean({"airframe": doc.get("airframe"), "derived": {k: derived.get(k) for k in (
            "v_stall_mps", "aspect_ratio", "wing_loading_n_m2", "p_cruise_w", "p_loiter_w", "endurance_cruise_s", "endurance_loiter_s",
            "r_min_loiter_m", "ld_max", "endurance_mission_cruise_s", "r_loiter_min_w10_m") if derived.get(k) is not None},
            "loiter_radius_table": fw.loiter_radius_table(), "vtol": doc.get("vtol"),
            "mission": None if vp is None else {k: _r(v, 2) for k, v in vp.items()}})
    if PF.frame_type_of(doc) in PF.MULTIROTOR_FRAMES and str(pid).startswith("p600"):
        out["twin"] = PF.twin_table(doc, mdir)
    return out


class _EmptyRig:
    specs: tuple = ()


# ---------------------------------------------------------------- 传感器目录（SB-23）
def _sensor_spec(kind: str, sp: Any, d2: dict, doc: dict) -> dict:
    g = doc.get("gimbal") or {}
    gim = {"pitch_deg": [g.get("pitch_min_deg"), g.get("pitch_max_deg")], "yaw_deg": [g.get("yaw_min_deg"), g.get("yaw_max_deg")],
           "rate_dps": g.get("rate_max_deg_s")} if g else None
    if kind == "eo":
        z = d2.get("zoom") or {}
        sn = doc.get("sensor") or {}
        il = d2.get("illuminator")
        spec = {"sensor_w_mm": (z.get("sensor_mm") or [None, None])[0], "sensor_h_mm": (z.get("sensor_mm") or [None, None])[1],
                "pixel_um": sn.get("pixel_um"), "native_w_px": sn.get("width_px"), "native_h_px": sn.get("height_px"),
                "f_mm": z.get("f_mm"), "f_number": z.get("f_number"),
                "outputs": [{"name": o["name"], "w_px": o["w_px"], "h_px": o["h_px"], "fps": o["fps"], "crop_w_px": o["crop_w_px"],
                             "p_eff_um": o["p_eff_um"], "hfov_deg": o["hfov_deg"]} for o in z.get("outputs") or []],
                "gimbal": gim, "zoom_full_s": z.get("zoom_time_s"), "min_lux_color": (doc.get("night") or {}).get("e_min_color_lx"),
                "min_lux_mono": (doc.get("night") or {}).get("e_min_mono_lx"), "analysis_default": z.get("analysis_default"),
                "k4_requires": z.get("k4_requires"),
                "derived": {"hfov_deg": z.get("hfov_deg"), "vfov_deg": z.get("vfov_deg"), "dfov_deg": z.get("dfov_deg")}}
        if il:
            ill = (doc.get("night") or {}).get("illuminator") or {}
            spec["illuminator"] = {"lambda_nm": ill.get("wavelength_nm"), "power_w": ill.get("p_w"), "range_ref_m": ill.get("r_ref_m"),
                                   "divergence_ref_deg": ill.get("theta_ref_deg"), "r_nir_m": il.get("range_m")}
        return spec
    if kind == "nir_illuminator":
        il = d2.get("illuminator") or {}
        return {"lambda_nm": doc.get("wavelength_nm"), "power_w": doc.get("p_w"), "divergence_deg": doc.get("divergence_deg"),
                "range_ref_m": (il.get("range_m") or [None])[0], "ref": doc.get("ref"), "derived": {"r_nir_m": il.get("range_m")}}
    if kind == "lwir":
        det = doc.get("detector") or {}
        return {"w_px": det.get("width_px"), "h_px": det.get("height_px"), "pixel_um": det.get("pixel_um"), "band_um": det.get("band_um"),
                "netd_mk": det.get("netd_mk"), "fps": det.get("frame_hz"),
                "lenses": [{"f_mm": ln["f_mm"], "f_number": ln["f_number"], "hfov_deg": ln["hfov_deg"], "vfov_deg": ln["vfov_deg"],
                            "ifov_urad": ln["ifov_urad"], "mass_kg": ln["mass_kg"], "power_w": ln["power_w"]}
                           for ln in d2.get("lenses") or []],
                "digital_zoom": [1, doc.get("digital_zoom_max")], "palettes": doc.get("palettes"), "gimbal": gim,
                "gimbal_mass_kg": doc.get("gimbal_mass_kg")}
    if kind == "radar":
        fov = doc.get("fov") or {}
        rd = d2.get("radar") or {}
        return {"freq_ghz": doc.get("freq_ghz"), "az_fov_deg": 2 * float(fov.get("az_deg", 0)), "el_fov_deg": 2 * float(fov.get("el_deg", 0)),
                "r_ref_m": doc.get("r_ref_m"), "rcs_ref_m2": doc.get("sigma_ref_m2"), "snr_req_db": doc.get("snr_req_db"),
                "res_range_m": doc.get("range_res_m"), "res_az_deg": doc.get("az_res_deg"), "res_vel_mps": doc.get("vel_res_mps"),
                "acc_az_deg": doc.get("az_acc_deg"), "rate_hz": doc.get("rate_hz"), "r_max_m": doc.get("r_max_m"),
                "rain": doc.get("rain"), "derived": {"r_detect_m": rd.get("r_detect_m")}}
    if kind == "acoustic":
        return {"mics": doc.get("n_mics"), "aperture_m": doc.get("aperture_m"), "band_hz": doc.get("band_hz"),
                "gain_db": doc.get("array_gain_db"), "nr_db": doc.get("nr_db"), "r_ego_m": doc.get("r_ego_m"),
                "acc_bearing_deg": doc.get("az_acc_deg"), "rate_hz": doc.get("rate_hz"), "detect_snr_db": doc.get("detect_snr_db")}
    if kind == "lidar":
        rg = doc.get("range") or {}
        fov = doc.get("fov") or {}
        lr = d2.get("lidar_range") or {}
        return {"lambda_nm": 905, "range_10pct_m": rg.get("r_at_rho10_m"), "range_max_m": rg.get("hard_max_m"), "blind_m": rg.get("blind_m"),
                "fov": {"h_deg": fov.get("h_deg"), "v_deg": [fov.get("v_min_deg"), fov.get("v_max_deg")]},
                "points_per_s": (doc.get("pattern") or {}).get("point_rate_hz"), "frame_hz": doc.get("frame_rate_hz"),
                "range_exponent": rg.get("exponent"),
                "derived": {"r_max_m": lr.get("r_max_m"), "density_pts_sr": lr.get("density_pts_sr")}}
    return {}


def sensor_items(cat: Catalog) -> list[dict]:
    """`GET /catalog/sensors` 条目（SB-23）：目录 `sensor_models[]` 中 `visible` 的型号，按目录次序。"""
    from awr.sim.sensors.describe import d2_json
    from awr.sim.sensors.spec import build_spec

    out = []
    for sm in cat.doc.get("sensor_models") or []:
        if sm.get("visible") is False:
            continue
        hit = cat.sensor_docs.get(str(sm["id"]))
        if hit is None:
            continue
        doc, p = hit
        d = dict(doc)
        d.setdefault("mount", {"xyz_m": [0.0, 0.0, 0.0], "rpy_deg": [0, 0, 0]})
        sp = build_spec(str(sm["id"]), d, 0, str(p))
        d2 = d2_json(sp)
        kind = KIND_OF_SCHEMA.get(str(doc.get("schema")), "eo")
        spec = _sensor_spec(kind, sp, d2, d)
        spec.update({"model_version": str(doc.get("model_version")), "conf": doc.get("conf"), "src": doc.get("src"),
                     "env_limits": doc.get("env_limits"), "disclaimer_zh": DISCLAIMER_ZH if doc.get("reference") else None})
        out.append(_clean({"sensor_type": str(sm["id"]), "name_zh": str(doc.get("display_name") or sm["id"]), "kind": kind,
                           "reference": bool(doc.get("reference", False)), "mass_kg": D.num(doc.get("mass_kg")),
                           "power_w": D.num(doc.get("power_w")), "mount_requires_landed": True, "spec": spec}))
    return out


# ---------------------------------------------------------------- 视图
def build_view(cat: Catalog | None = None) -> dict:
    cat = cat or Catalog.load()
    vis = cat.visible_ids()
    view = {"schema": VIEW_SCHEMA, "catalog_version": cat.version, "disclaimer_zh": cat.disclaimer_zh,
            "models": {"items": [model_summary(cat, pid) for pid in vis]},
            "details": {pid: model_detail(cat, pid) for pid in vis},
            "sensors": {"items": sensor_items(cat)}}
    view = _clean(view)
    view["etag"] = {"models": etag_of(view["models"]), "sensors": etag_of(view["sensors"]),
                    "details": {pid: etag_of(d) for pid, d in view["details"].items()}}
    return view


def write_view(path: Path | None = None, view: dict | None = None) -> Path:
    path = Path(path or VIEW_PATH)
    view = view if view is not None else build_view()
    path.write_bytes(canonical_bytes(view) + b"\n")
    return path
