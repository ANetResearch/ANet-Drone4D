"""机型派生值：固定翼与 VTOL 的气动与能量、盘旋空速与半径、VTOL 任务剖面、挂载与质量变化的能量修正（CAT-FR-005、FR-006、
FR-014、FR-015；AWR-20 §4.3、§5.1–§5.5；AWR-04 §5.3；ADR-052、ADR-096）。

记 `W = m·g`、`AR = b²/S`、`a = ½rho·S·C_D0`、`k = 2W²/(rho·S·π·e·AR)`，rho = 1.225 kg/m³：

    P_aero(V, n) = a·V³ + n²·k/V                  （n = 1/cos φ，盘旋与转弯时的载荷因数）
    P_elec       = P_aero/η + P_av + m·g·max(ḣ, 0)/η
    V_s = √(2W/(rho·S·C_Lmax))，V_mp = (k/(3a))^(1/4)，V_md = (k/a)^(1/4)，(L/D)_max = ½·√(π·e·AR/C_D0)
    续航 = E_use / P_elec，E_use = usable_frac·capacity_wh（ADR-052）

盘旋（§5.3）：`loiter_airspeed(w) = clamp(max(v_loiter, w + 3), v_min, v_cruise)`，`φ_max(V_a) = min(φ_max, acos((V_s/V_a)²))`，
`R_loiter,min(w) = 1.2·(V_a + w)²/(g·tan φ_max(V_a))`；无风最小转弯半径 `R_min(V) = V²/(g·tan φ_max)`。

纯函数，不抽随机数（CAT-NFR-002）。值对象 `{value, conf, src}` 与纯数值两种写法都接受。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "LOITER_WIND_MARGIN_MPS",
    "RHO0",
    "RLOITER_FACTOR",
    "TABLE_WINDS_MPS",
    "FwAero",
    "G",
    "e_use_wh",
    "endurance_summary",
    "fw_aero",
    "fw_derived",
    "mass_energy_correction",
    "multirotor_hover_power_w",
    "num",
    "vtol_profile",
]

G = 9.80665
RHO0 = 1.225
LOITER_WIND_MARGIN_MPS = 3.0  # 盘旋空速至少比风大 3 m/s（AWR-04 §5.3）
RLOITER_FACTOR = 1.2  # 对地盘旋半径下限的余量系数
TABLE_WINDS_MPS = (0.0, 5.0, 10.0, 12.0, 14.0, 15.0)


def num(x: Any, default: float | None = None) -> float | None:
    """值对象或纯数值 -> float；缺失为 default。"""
    v = x.get("value") if isinstance(x, dict) and "value" in x else x
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return float(v)


def _frame(doc: dict) -> str:
    return str((doc.get("frame") or {}).get("type") or "quad_x")


def e_use_wh(doc: dict) -> float | None:
    """可用能量 `usable_frac·capacity_wh`（缺省 usable_frac 0.85）；没有电池为 None。"""
    bat = doc.get("battery")
    if not isinstance(bat, dict):
        return None
    cap = num(bat.get("capacity_wh"))
    if cap is None:
        return None
    return float(num(bat.get("usable_frac"), 0.85)) * cap


@dataclass(frozen=True)
class FwAero:
    """固定翼（与 VTOL 固定翼段）的气动与运行参数（AWR-20 §5.1；单位见字段后缀）。"""

    mass_kg: float
    mtow_kg: float
    span_m: float
    wing_area_m2: float
    cd0: float
    oswald_e: float
    cl_max: float
    eta_prop: float
    p_avionics_w: float
    p_max_w: float
    v_min_mps: float
    v_loiter_mps: float
    v_cruise_mps: float
    v_max_mps: float
    phi_max_deg: float
    roll_rate_max_deg_s: float = 45.0
    tau_v_s: float = 2.0
    tau_phi_s: float = 0.5
    accel_max_mps2: float = 1.5
    decel_max_mps2: float = 2.0
    climb_max_mps: float = 4.0
    sink_max_mps: float = 3.0
    r_loiter_default_m: float = 80.0
    length_m: float = 1.0
    launch: dict = field(default_factory=dict)
    recovery: dict = field(default_factory=dict)
    rho: float = RHO0

    # ---- 基本量
    @property
    def weight_n(self) -> float:
        return self.mass_kg * G

    @property
    def aspect_ratio(self) -> float:
        return self.span_m ** 2 / self.wing_area_m2

    @property
    def wing_loading_n_m2(self) -> float:
        return self.weight_n / self.wing_area_m2

    @property
    def a_coef(self) -> float:
        return 0.5 * self.rho * self.wing_area_m2 * self.cd0

    @property
    def k_coef(self) -> float:
        return 2.0 * self.weight_n ** 2 / (self.rho * self.wing_area_m2 * math.pi * self.oswald_e * self.aspect_ratio)

    def v_stall(self, mass_kg: float | None = None) -> float:
        m = self.mass_kg if mass_kg is None else float(mass_kg)
        return math.sqrt(2.0 * m * G / (self.rho * self.wing_area_m2 * self.cl_max))

    @property
    def v_mp(self) -> float:
        return (self.k_coef / (3.0 * self.a_coef)) ** 0.25

    @property
    def v_md(self) -> float:
        return (self.k_coef / self.a_coef) ** 0.25

    @property
    def ld_max(self) -> float:
        return 0.5 * math.sqrt(math.pi * self.oswald_e * self.aspect_ratio / self.cd0)

    # ---- 功率与推力
    def p_aero(self, v_mps: float, n: float = 1.0) -> float:
        v = max(float(v_mps), 0.1)
        return self.a_coef * v ** 3 + n * n * self.k_coef / v

    def p_elec(self, v_mps: float, n: float = 1.0, climb_mps: float = 0.0, extra_w: float = 0.0) -> float:
        return self.p_aero(v_mps, n) / self.eta_prop + self.p_avionics_w + self.mass_kg * G * max(float(climb_mps), 0.0) / \
            self.eta_prop + float(extra_w)

    def thrust_n(self, v_mps: float, n: float = 1.0) -> float:
        return self.p_aero(v_mps, n) / max(float(v_mps), 0.1)

    # ---- 转弯与盘旋（§5.3）
    def phi_max_at(self, va_mps: float) -> float:
        """空速 V_a 下可用的最大滚转（rad）：`min(φ_max, acos((V_s/V_a)²))`。"""
        r = self.v_stall() / max(float(va_mps), 1e-6)
        lim = math.acos(min(1.0, r * r)) if r < 1.0 else 0.0
        return min(math.radians(self.phi_max_deg), lim)

    def r_min(self, va_mps: float) -> float:
        """空气系内最小转弯半径 `V²/(g·tan φ_max)`（无风）。"""
        return float(va_mps) ** 2 / (G * math.tan(math.radians(self.phi_max_deg)))

    def loiter_airspeed(self, w_max_mps: float) -> float:
        """盘旋指令空速：`clamp(max(v_loiter, w + 3), v_min, v_cruise)`。"""
        return min(max(max(self.v_loiter_mps, float(w_max_mps) + LOITER_WIND_MARGIN_MPS), self.v_min_mps), self.v_cruise_mps)

    def loiter_holdable(self, w_max_mps: float) -> bool:
        """`w + 3 ≤ v_cruise` 时盘旋可保持（否则发 fw.wind_exceeds，改逆风折线等待）。"""
        return float(w_max_mps) + LOITER_WIND_MARGIN_MPS <= self.v_cruise_mps + 1e-9

    def r_loiter_min(self, w_max_mps: float, va_mps: float | None = None) -> float:
        """对地盘旋半径下限 `1.2·(V_a + w)²/(g·tan φ_max(V_a))`。"""
        va = self.loiter_airspeed(w_max_mps) if va_mps is None else float(va_mps)
        phi = self.phi_max_at(va)
        return RLOITER_FACTOR * (va + float(w_max_mps)) ** 2 / (G * math.tan(max(phi, 1e-6)))

    def loiter_radius(self, w_max_mps: float, cmd_radius_m: float | None = None) -> float:
        """实际盘旋半径 `max(R_loiter,min, r_loiter_default, 指令半径)`。"""
        r = max(self.r_loiter_min(w_max_mps), self.r_loiter_default_m)
        if cmd_radius_m is not None:
            r = max(r, float(cmd_radius_m))
        return r

    def loiter_radius_table(self, winds_mps: tuple[float, ...] = TABLE_WINDS_MPS) -> list[dict]:
        return [{"w_max_mps": w, "airspeed_mps": round(self.loiter_airspeed(w), 3),
                 "r_loiter_min_m": round(self.r_loiter_min(w), 2), "holdable": self.loiter_holdable(w)} for w in winds_mps]

    def with_mass(self, mass_kg: float) -> FwAero:
        return FwAero(**{**self.__dict__, "mass_kg": float(mass_kg)})


def fw_aero(doc: dict, *, mass_kg: float | None = None) -> FwAero | None:
    """profile 文档的 `airframe` -> FwAero；没有 airframe 时 None。"""
    af = doc.get("airframe")
    if not isinstance(af, dict):
        return None
    m = num(doc.get("mass_kg"), 0.0) if mass_kg is None else float(mass_kg)
    mtow = num(doc.get("mtow_kg"), m)

    def f(key: str, default: float | None = None) -> float:
        v = num(af.get(key), default)
        if v is None:
            raise KeyError(f"airframe.{key}")
        return float(v)

    return FwAero(mass_kg=float(m), mtow_kg=float(mtow), span_m=f("span_m", 2.0), wing_area_m2=f("wing_area_m2"), cd0=f("cd0"),
                  oswald_e=f("oswald_e"), cl_max=f("cl_max"), eta_prop=f("eta_prop"), p_avionics_w=f("p_avionics_w", 0.0),
                  p_max_w=f("p_max_w", math.inf), v_min_mps=f("v_min_mps"), v_loiter_mps=f("v_loiter_mps", f("v_min_mps")),
                  v_cruise_mps=f("v_cruise_mps"), v_max_mps=f("v_max_mps"), phi_max_deg=f("phi_max_deg"),
                  roll_rate_max_deg_s=f("roll_rate_max_deg_s", 45.0), tau_v_s=f("tau_v_s", 2.0), tau_phi_s=f("tau_phi_s", 0.5),
                  accel_max_mps2=f("accel_max_mps2", 1.5), decel_max_mps2=f("decel_max_mps2", 2.0),
                  climb_max_mps=f("climb_max_mps", 4.0), sink_max_mps=f("sink_max_mps", 3.0),
                  r_loiter_default_m=f("r_loiter_default_m", 80.0), length_m=f("length_m", 1.0),
                  launch=dict(af.get("launch") or {}), recovery=dict(af.get("recovery") or {}))


def vtol_profile(doc: dict, *, extra_w: float = 0.0) -> dict[str, float] | None:
    """VTOL 标准任务剖面（AWR-20 §5.4）：垂直起飞 → 过渡 → 巡航 → 过渡 → 垂直降落，悬停累计 hover_max_s。
    垂直段能量 `p_hover·(hover_max + t_fw + t_mc)`，巡航段时长 `(E_use − E_vert)/P(v_cruise)`。"""
    vt = doc.get("vtol")
    fw = fw_aero(doc)
    eu = e_use_wh(doc)
    if not isinstance(vt, dict) or fw is None or eu is None:
        return None
    p_h = float(num(vt.get("p_hover_w"), 0.0)) + float(extra_w)
    t_vert = float(num(vt.get("hover_max_s"), 0.0)) + float(num(vt.get("transition_fw_s"), 0.0)) + \
        float(num(vt.get("transition_mc_s"), 0.0))
    e_vert = p_h * t_vert / 3600.0
    p_cr = fw.p_elec(fw.v_cruise_mps, extra_w=extra_w)
    cruise_s = max(eu - e_vert, 0.0) * 3600.0 / p_cr
    return {"e_vert_wh": e_vert, "t_vert_s": t_vert, "cruise_s": cruise_s, "total_s": cruise_s + t_vert, "p_hover_w": p_h}


def fw_derived(doc: dict, *, extra_w: float = 0.0) -> dict[str, float]:
    """固定翼与 VTOL 的派生值（`derived` 新键，AWR-20 §7.3）；没有 airframe 时空表。`extra_w` 为挂载变化的附加电功率（§5.5）。"""
    fw = fw_aero(doc)
    if fw is None:
        return {}
    out = {"v_stall_mps": fw.v_stall(), "aspect_ratio": fw.aspect_ratio, "wing_loading_n_m2": fw.wing_loading_n_m2,
           "p_cruise_w": fw.p_elec(fw.v_cruise_mps, extra_w=extra_w), "p_loiter_w": fw.p_elec(fw.v_loiter_mps, extra_w=extra_w),
           "ld_max": fw.ld_max,
           "r_min_loiter_m": fw.r_min(fw.v_loiter_mps)}
    eu = e_use_wh(doc)
    if eu is not None:
        out["endurance_cruise_s"] = eu * 3600.0 / out["p_cruise_w"]
        out["endurance_loiter_s"] = eu * 3600.0 / out["p_loiter_w"]
    vp = vtol_profile(doc, extra_w=extra_w)
    if vp is not None:
        out["endurance_mission_cruise_s"] = vp["cruise_s"]
    return out


def multirotor_hover_power_w(doc: dict) -> float | None:
    """多旋翼悬停功率：文件 `battery.p_hover_w`，否则按 ADR-052 由 `E_use ÷ 悬停续航` 反推。"""
    bat = doc.get("battery")
    if not isinstance(bat, dict):
        return None
    p = num(bat.get("p_hover_w"))
    if p is not None:
        return p
    eu = e_use_wh(doc)
    t = num(bat.get("hover_endurance_s"))
    return eu * 3600.0 / t if eu is not None and t else None


def endurance_summary(doc: dict) -> dict[str, float]:
    """`{hover_s?, cruise_s?, loiter_s?, mission_s?}`（口径 E_use ÷ 功率，不含任务余量；17 §17.6.2）。"""
    out: dict[str, float] = {}
    ft = _frame(doc)
    eu = e_use_wh(doc)
    if ft in ("quad_x", "hex_x"):
        p = multirotor_hover_power_w(doc)
        if p and eu is not None:
            out["hover_s"] = eu * 3600.0 / p
    d = fw_derived(doc)
    if "endurance_cruise_s" in d:
        out["cruise_s"] = d["endurance_cruise_s"]
        out["loiter_s"] = d["endurance_loiter_s"]
    if "endurance_mission_cruise_s" in d:
        out["mission_s"] = d["endurance_mission_cruise_s"]
    return out


def mass_energy_correction(doc: dict, dm_kg: float = 0.0, dp_w: float = 0.0, *, d_wh: float = 0.0) -> dict[str, Any]:
    """挂载与质量变化的能量修正（AWR-20 §5.5，MT-4）：

        多旋翼与 VTOL 悬停：P_hover' = P_hover·((m + Δm)/m)^1.5 + ΔP_e
        固定翼与 VTOL 巡航：P'(V)    = (a·V³ + ((m + Δm)/m)²·k/V)/η + P_av + ΔP_e；V_s' = V_s·√((m + Δm)/m)
        电池容量变化：      Δm_bat = ΔWh / e_spec（e_spec = capacity_wh / battery.mass_kg），计入 Δm
        续航：              E_use' / P'

    返回 `{mass_kg, p_hover_w?, hover_s?, p_cruise_w?, p_loiter_w?, cruise_s?, loiter_s?, v_stall_mps?}`。"""
    m0 = float(num(doc.get("mass_kg"), 0.0))
    bat = doc.get("battery") if isinstance(doc.get("battery"), dict) else {}
    cap = num(bat.get("capacity_wh"))
    bm = num(bat.get("mass_kg"))
    dm_bat = 0.0
    if d_wh and cap and bm:
        dm_bat = float(d_wh) / (cap / bm)
    m1 = m0 + float(dm_kg) + dm_bat
    eu = e_use_wh(doc)
    eu1 = None if eu is None else eu + float(num(bat.get("usable_frac"), 0.85)) * float(d_wh)
    out: dict[str, Any] = {"mass_kg": m1, "dm_kg": m1 - m0, "dp_w": float(dp_w)}
    ft = _frame(doc)
    if ft in ("quad_x", "hex_x", "vtol_quadplane"):
        ph = multirotor_hover_power_w(doc)
        if ft == "vtol_quadplane":
            ph = num((doc.get("vtol") or {}).get("p_hover_w"), ph)
        if ph:
            p1 = ph * (m1 / m0) ** 1.5 + float(dp_w)
            out["p_hover_w"] = p1
            if eu1 is not None and ft != "vtol_quadplane":
                out["hover_s"] = eu1 * 3600.0 / p1
    fw = fw_aero(doc)
    if fw is not None:
        f1 = fw.with_mass(m1)
        pc = f1.p_elec(f1.v_cruise_mps, extra_w=dp_w)
        pl = f1.p_elec(f1.v_loiter_mps, extra_w=dp_w)
        out.update({"p_cruise_w": pc, "p_loiter_w": pl, "v_stall_mps": f1.v_stall()})
        if eu1 is not None:
            out["cruise_s"] = eu1 * 3600.0 / pc
            out["loiter_s"] = eu1 * 3600.0 / pl
    return out
