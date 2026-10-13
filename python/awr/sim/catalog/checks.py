"""机型自洽检查：固定翼 FW-1 至 FW-8、VTOL VT-1 至 VT-4、签名 SG-1 至 SG-3、挂载 MT-2、MT-5、传感器 SN-7、SN-9（CAT-FR-005、
FR-007、FR-014、FR-021；AWR-20 §8.2；ADR-096、ADR-115）。

检查函数 `fn(doc) -> list[{id, value, range, pass, msg?}]` 经 M08 `register_profile_check(frame_types, check_id, fn)` 登记
（`register(R)`），在 ProfileTable 加载（失败 353 拒绝启动）与会话克隆（CL-3，失败 522）时执行。派生值比对（≤ 1%）只针对文件：
会话克隆（id 形如 `<源 id>_c<n>`）的派生值由 M21 在克隆后重算（`clone.py`），不与源文件的 `derived` 比对。

需要挂载文件的检查（MT-2 机体残余质量、SN-9 工作温度交集、SN-7 LWIR 衍射告警）按 `id` 在当前机型目录（`AWR_VEHICLES_DIR`
或 `vehicles/`，含 `catalog.json` 的 `dir`）中定位机型包，经 M13 `spec.load_rig` 读取有效规格；定位不到时（临时测试目录）给出
通过的说明行。纯函数，不抽随机数。
"""

from __future__ import annotations

import math
import re
from functools import cache
from pathlib import Path
from typing import Any

from ..fleet import profiles as PF
from . import derive as D
from .signature import sg_checks

__all__ = ["CLONE_ID_RE", "DERIVED_TOL", "FRAMES_ALL", "FRAMES_FW", "fw_checks", "is_clone_id", "model_dir_for", "mount_checks",
           "register", "vt_checks"]

DERIVED_TOL = 0.01  # 派生值相对差 ≤ 1%
FRAMES_FW = frozenset({"fixed_wing", "vtol_quadplane"})
FRAMES_ALL = frozenset({"quad_x", "hex_x", "fixed_wing", "vtol_quadplane"})
CLONE_ID_RE = re.compile(r"^(?P<base>[a-z0-9_]+?)_c[0-9]+$")
PHI_RANGE_DEG = (15.0, 45.0)
HOVER_MAX_RANGE_S = (60.0, 600.0)
VT1_TWR_MIN = 1.6


def _row(cid: str, value: Any, rng: Any, ok: bool, msg: str | None = None) -> dict:
    r: dict[str, Any] = {"id": cid, "value": value, "range": rng, "pass": bool(ok)}
    if msg:
        r["msg"] = msg
    return r


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None or not math.isfinite(float(x)) else round(float(x), nd)


def is_clone_id(pid: str) -> bool:
    return bool(CLONE_ID_RE.match(str(pid)))


def _cmp(cid: str, doc: dict, calc: dict[str, float], keys: tuple[str, ...]) -> list[dict]:
    """文件 `derived` 与派生值的比对（≤ 1%）；克隆与文件未给出的键跳过。"""
    if is_clone_id(str(doc.get("id"))):
        return []
    der = doc.get("derived") or {}
    out = []
    for k in keys:
        v = der.get(k)
        c = calc.get(k)
        if v is None or c is None or not isinstance(v, (int, float)):
            continue
        ok = abs(float(v) - c) <= DERIVED_TOL * max(abs(c), 1e-12)
        out.append(_row(cid, {k: float(v), "computed": _r(c)}, [_r(c * (1 - DERIVED_TOL)), _r(c * (1 + DERIVED_TOL))], ok,
                        None if ok else f"derived.{k} 与派生值相差超过 1%"))
    return out


# ---------------------------------------------------------------- FW-1 至 FW-8（固定翼与 VTOL 的固定翼段）
def fw_checks(doc: dict) -> list[dict]:
    try:
        fw = D.fw_aero(doc)
    except KeyError as e:
        return [_row("FW-1", None, None, False, f"airframe 缺少字段 {e}")]
    if fw is None:
        return [_row("FW-1", None, None, False, "fixed_wing、vtol_quadplane 须有 airframe")]
    calc = D.fw_derived(doc)
    out: list[dict] = []
    out.append(_row("FW-1", {"aspect_ratio": _r(fw.aspect_ratio), "wing_loading_n_m2": _r(fw.wing_loading_n_m2, 2)}, None,
                    fw.wing_area_m2 > 0 and fw.span_m > 0))
    out += _cmp("FW-1", doc, calc, ("aspect_ratio", "wing_loading_n_m2"))
    vs_mtow = fw.v_stall(fw.mtow_kg)
    vs_m = fw.v_stall()
    out.append(_row("FW-2", {"v_min_mps": fw.v_min_mps, "v_stall_mtow_mps": _r(vs_mtow, 3)}, [_r(1.1 * vs_mtow, 3), None],
                    fw.v_min_mps >= 1.1 * vs_mtow - 1e-9, None if fw.v_min_mps >= 1.1 * vs_mtow - 1e-9 else
                    f"v_min {fw.v_min_mps} < 1.1·V_s(mtow) = {1.1 * vs_mtow:.2f} m/s"))
    out.append(_row("MT-5", {"v_min_mps": fw.v_min_mps, "v_stall_mps": _r(vs_m, 3)}, [_r(1.1 * vs_m, 3), None],
                    fw.v_min_mps >= 1.1 * vs_m - 1e-9, None if fw.v_min_mps >= 1.1 * vs_m - 1e-9 else
                    f"v_min {fw.v_min_mps} < 1.1·V_s(m) = {1.1 * vs_m:.2f} m/s"))
    out += _cmp("FW-2", doc, calc, ("v_stall_mps",))
    ok3 = fw.v_min_mps <= fw.v_loiter_mps <= fw.v_cruise_mps < fw.v_max_mps
    out.append(_row("FW-3", {"v_min": fw.v_min_mps, "v_loiter": fw.v_loiter_mps, "v_cruise": fw.v_cruise_mps,
                             "v_max": fw.v_max_mps}, None, ok3, None if ok3 else "须 v_min ≤ v_loiter ≤ v_cruise < v_max"))
    phi_ok = PHI_RANGE_DEG[0] <= fw.phi_max_deg <= PHI_RANGE_DEG[1]
    lim4 = 1.05 * vs_m / math.sqrt(math.cos(math.radians(min(fw.phi_max_deg, 89.0))))
    ok4 = phi_ok and fw.v_min_mps >= lim4 - 1e-9
    out.append(_row("FW-4", {"phi_max_deg": fw.phi_max_deg, "v_min_mps": fw.v_min_mps}, [_r(lim4, 3), None], ok4,
                    None if ok4 else f"须 φ_max ∈ [15°, 45°] 且 v_min ≥ 1.05·V_s/√cos φ_max = {lim4:.2f} m/s"))
    out += _cmp("FW-5", doc, calc, ("p_cruise_w", "p_loiter_w", "endurance_cruise_s", "endurance_loiter_s", "ld_max"))
    p_vmax = fw.p_elec(fw.v_max_mps)
    p_climb = fw.p_elec(fw.v_cruise_mps) + fw.mass_kg * D.G * fw.climb_max_mps / fw.eta_prop
    ok6 = p_vmax <= fw.p_max_w + 1e-9 and p_climb <= fw.p_max_w + 1e-9
    out.append(_row("FW-6", {"p_vmax_w": _r(p_vmax, 1), "p_cruise_climb_w": _r(p_climb, 1)}, [None, fw.p_max_w], ok6,
                    None if ok6 else "P(v_max) 或 P(v_cruise) + 爬升功率超过 p_max_w"))
    wr = D.num(doc.get("wind_rating_mps"))
    ok7 = wr is None or wr <= fw.v_cruise_mps - D.LOITER_WIND_MARGIN_MPS + 1e-9
    out.append(_row("FW-7", wr, [None, fw.v_cruise_mps - D.LOITER_WIND_MARGIN_MPS], ok7,
                    None if ok7 else "wind_rating_mps 须 ≤ v_cruise − 3"))
    r0 = fw.r_loiter_min(0.0)
    ok8 = fw.r_loiter_default_m >= r0 - 1e-9
    out.append(_row("FW-8", {"r_loiter_default_m": fw.r_loiter_default_m, "r_loiter_min_m": _r(r0, 2)}, [_r(r0, 2), None], ok8,
                    None if ok8 else "r_loiter_default_m 须 ≥ R_loiter,min(0)"))
    out += _cmp("FW-8", doc, calc, ("r_min_loiter_m",))
    return out


# ---------------------------------------------------------------- VT-1 至 VT-4（VTOL）
def vt_checks(doc: dict) -> list[dict]:
    vt = doc.get("vtol")
    if not isinstance(vt, dict):
        return [_row("VT-2", None, None, False, "vtol_quadplane 须有 vtol")]
    out: list[dict] = []
    mot = doc.get("motor") or {}
    mtow = D.num(doc.get("mtow_kg"), D.num(doc.get("mass_kg")))
    tmax = D.num(mot.get("t_max_n"))
    n = int(mot.get("n") or 0)
    twr = (n * tmax) / (mtow * D.G) if tmax and mtow else None
    out.append(_row("VT-1", _r(twr, 3), [VT1_TWR_MIN, None], twr is not None and twr >= VT1_TWR_MIN,
                    None if twr is not None and twr >= VT1_TWR_MIN else "升力系统 MTOW 时推重比须 ≥ 1.6"))
    t_fw, t_mc = D.num(vt.get("transition_fw_s"), 0.0), D.num(vt.get("transition_mc_s"), 0.0)
    hmax = D.num(vt.get("hover_max_s"), 0.0)
    v_tr = D.num(vt.get("v_transition_mps"))
    v_min = D.num((doc.get("airframe") or {}).get("v_min_mps"))
    ok2 = t_fw > 0 and t_mc > 0 and HOVER_MAX_RANGE_S[0] <= hmax <= HOVER_MAX_RANGE_S[1] and \
        (v_tr is None or v_min is None or abs(v_tr - v_min) <= 1e-9)
    out.append(_row("VT-2", {"transition_fw_s": t_fw, "transition_mc_s": t_mc, "hover_max_s": hmax, "v_transition_mps": v_tr},
                    list(HOVER_MAX_RANGE_S), ok2, None if ok2 else "过渡时长须 > 0、v_transition = v_min、hover_max_s ∈ [60, 600]"))
    jv = (doc.get("inertia_kgm2") or {}).get("value") if isinstance(doc.get("inertia_kgm2"), dict) else None
    ok3 = isinstance(jv, dict) and float(jv["zz"]) > max(float(jv["xx"]), float(jv["yy"]))
    out.append(_row("VT-3", jv, None, ok3, None if ok3 else "须 J_zz > max(J_xx, J_yy)"))
    calc = D.fw_derived(doc)
    out += _cmp("VT-4", doc, calc, ("endurance_mission_cruise_s",))
    return out


# ---------------------------------------------------------------- 需要挂载文件的检查（MT-2、SN-9、SN-7）
def _stamp(vdir: Path) -> tuple:
    files = sorted(vdir.glob("*/params.yaml"))
    cat = vdir / "catalog.json"
    if cat.exists():
        files.append(cat)
    return tuple((str(p), p.stat().st_mtime_ns) for p in files)


@cache
def _id_dirs(vdir: str, stamp: tuple) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(Path(vdir).glob("*/params.yaml")):
        try:
            d = PF.yaml_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if isinstance(d, dict) and d.get("id"):
            out[str(d["id"])] = str(p.parent)
            for v in (d.get("variants") or {}):
                out[str(v)] = str(p.parent)
    return out


def model_dir_for(doc_or_id: dict | str) -> Path | None:
    """机型（或会话克隆、存档版本）所在的机型包目录；当前机型目录中找不到时 None。"""
    pid = str(doc_or_id.get("id") if isinstance(doc_or_id, dict) else doc_or_id).split("@", 1)[0]
    vdir = PF.vehicles_dir()
    if not vdir.is_dir():
        return None
    m = _id_dirs(str(vdir), _stamp(vdir))
    if pid in m:
        return Path(m[pid])
    c = CLONE_ID_RE.match(pid)
    if c and c.group("base") in m:
        return Path(m[c.group("base")])
    return None


def default_rig(doc: dict, mdir: Path | None = None) -> Any:
    """缺省挂载的有效传感器组（M13 `load_rig` 的子集，不含 D1 字符串槽位）；目录未知、映射为空或纯 D1 映射时 None。"""
    from awr.sim.sensors.spec import load_rig

    mdir = mdir or model_dir_for(doc)
    smap = doc.get("sensors") or {}
    if mdir is None or not smap:
        return None
    rig = load_rig(mdir, dict(smap))
    return rig.subset(rig.default_mask(sandbox=True)) if rig.d2 else None  # 纯 D1 映射：没有 D2 挂载（质量在 payload[]）


def mount_checks(doc: dict, mdir: Path | None = None) -> list[dict]:
    """MT-2（机体残余质量 > 0，并与文件 `derived.airframe_residual_kg` 比对）、SN-9（机体与全部缺省挂载的工作温度交集非空）、
    SN-7（LWIR `λ·F# > 1.2·像元` 告警，只给 msg）。"""
    from awr.sim.sensors.spec import SensorSpecError

    out: list[dict] = []
    try:
        rig = default_rig(doc, mdir)
    except SensorSpecError as e:
        return [_row("MT-2", None, None, False, f"挂载文件无效：{'; '.join(e.problems)[:200]}")]
    m = D.num(doc.get("mass_kg"), 0.0)
    bat = doc.get("battery") if isinstance(doc.get("battery"), dict) else {}
    bm = D.num(bat.get("mass_kg"), 0.0) or 0.0
    specs = list(rig.specs) if rig is not None else []
    mounted_ids = {s.model_id for s in specs if s.model_id}
    mm = sum(float(s.mass_kg) for s in specs if math.isfinite(float(s.mass_kg)))
    # payload[] 中与已挂载型号同名的条目不重复计入（P600 的 eo_gx40、mid360 在映射切换为对象形式后由槽位计入）
    pl = sum(float(p.get("mass_kg") or 0.0) for p in doc.get("payload") or [] if str(p.get("name")) not in mounted_ids)
    if rig is None and not bat:
        out.append(_row("MT-2", None, None, True, "没有电池与 D2 挂载，跳过"))
    else:
        res = m - bm - mm - pl
        ok = res > 0.0
        out.append(_row("MT-2", _r(res), [0.0, None], ok, None if ok else "机体残余质量须 > 0"))
        if not is_clone_id(str(doc.get("id"))):
            fv = (doc.get("derived") or {}).get("airframe_residual_kg")
            if isinstance(fv, (int, float)) and abs(float(fv) - res) > max(DERIVED_TOL * abs(res), 1e-3):
                out.append(_row("MT-2", {"airframe_residual_kg": float(fv), "computed": _r(res)}, None, False,
                                "derived.airframe_residual_kg 与派生值相差超过 1%"))
    tc = (doc.get("env_limits") or {}).get("temp_c") or [None, None]
    lo, hi = D.num(tc[0]), D.num(tc[1])
    if rig is not None:
        lims = [(lo, hi)] if lo is not None and hi is not None else []
        lims += [s.env_limits for s in specs if s.env_limits is not None]
        if lims:
            a, b = max(x[0] for x in lims), min(x[1] for x in lims)
            out.append(_row("SN-9", [a, b], None, a <= b, None if a <= b else "机体与缺省挂载的工作温度交集为空"))
        warn = []
        for s in specs:
            if s.schema_id == "awr.sensor.lwir.v1" and s.lenses:
                ln = next((x for x in s.lenses if abs(x.f_mm - s.lens_mm) < 1e-9), None)
                pix = float((s.doc.get("detector") or {}).get("pixel_um", 12.0))
                if ln is not None and 10.0 * ln.f_number > 1.2 * pix:
                    warn.append(f"{s.name}: λ·F# {10.0 * ln.f_number:.1f} µm > 1.2·像元 {1.2 * pix:.1f} µm，按衍射像元计算")
        out.append(_row("SN-7", len(warn), None, True, "; ".join(warn) if warn else None))
    return out


def _mount_checks_registered(doc: dict) -> list[dict]:
    return mount_checks(doc)


def register(R: Any) -> None:
    """向 M08 登记表登记（`R` 为 `awr.sim.fleet.stages.registry`）；对当前登记表幂等。"""
    have = {c.check_id for c in R.registry().profile_checks}
    for frames, cid, fn in ((FRAMES_FW, "M21.FW", fw_checks), (frozenset({"vtol_quadplane"}), "M21.VT", vt_checks),
                            (FRAMES_ALL, "M21.SG", sg_checks), (FRAMES_ALL, "M21.MT", _mount_checks_registered)):
        if cid not in have:
            R.register_profile_check(frames, cid, fn, owner="M21")
