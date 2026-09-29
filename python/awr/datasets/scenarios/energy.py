"""编写期离线能量复核（M16 §6.4.3、§9.2、§9.3；AWR-12 §5.8.3–§5.8.5）。

两套模型（门禁以 M10 能量预检与 M09 估价为准，这里只做编写期复核与回归预言）：

1. **风阻模型**（移植 `.cache/research/biz12/s1_check.py`，AWR-12 §5.8.4 的复核脚本）：
   `P = P_hover·(T/T_hover)^1.5 + m·g·v_z/η`（爬升），`T = sqrt(T_H² + (k·r_x)² + (k·r_y)²)`，
   `k = ½ρ·CdA·|r| + Σω_hover·c_rd`，`r = v_ground − w(z)`，风廓线 `w(z) = w_ref·(AGL/10)^0.25`。
   返航按 AWR-12 §5.8.3：`z_rtl = max(z_now, z_home + 30, H_top(p→home) + 5)`，原地 2 m/s 爬升、巡航、1.5 m/s 下降到
   home + 10 m、末段 1.0 m/s。地形取 World Package 的 DSM/DTM（`WorldTerrain`），或任何实现 `Terrain` 的对象。
2. **1-D 悬停功率模型**（移植 `.cache/research/m16/scenario_energy.py` 的 `Sortie`，M09 `s1_energy_check.py` 加返航段）：
   1 s 步进、不计风阻、连续 ENERGY_RTL 判据 `t_rem_usable / (1.3·t_rtl)`（< 1 触发），给出落地 SOC 与最小余量比。

常量从 `vehicles/p600/params.yaml` 读取（`EnergyModel.from_vehicle()`），可用能量口径 E_use = 0.85 × 222 = 188.7 Wh
（ADR-052）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import numpy as np

__all__ = [
    "S1_PLAN",
    "SX_LEGS",
    "EnergyModel",
    "SortieResult",
    "Terrain",
    "WindProfile",
    "WorldTerrain",
    "helix_sortie",
    "s1_segment_1d",
    "simulate_sortie",
]

ROOT = Path(__file__).resolve().parents[4]
G = 9.81


@dataclass(frozen=True)
class EnergyModel:
    p_hover_w: float = 515.0
    mass_kg: float = 3.5
    eta_climb: float = 0.5
    v_climb_mps: float = 3.0            # 起飞与转场爬升（px4_default）
    v_rtl_up_mps: float = 2.0           # 12 §5.8.3（g04 §6.3 Mock RTL CLIMB）
    v_dn_mps: float = 1.5
    v_final_mps: float = 1.0            # 风阻模型的末段 10 m
    v_land_mps: float = 0.7             # 1-D 模型的末段 10 m（M09 s1_energy_check）
    v_rtl_mps: float = 5.0
    e_use_wh: float = 0.85 * 222.0
    wind_drag: bool = True
    cda_m2: float = 0.035
    c_rd: float = 1.05e-4
    k_f: float = 8.54858e-6
    rho: float = 1.20
    emerg: float = 0.05                 # ENERGY_RTL 判据中保留的应急电量
    reserve: float = 0.20               # 能量预检落地 SOC 下限（12 §5.8.4）
    rtl_margin: float = 1.3

    @property
    def t_hover_n(self) -> float:
        return self.mass_kg * G

    @property
    def omega_sum(self) -> float:
        return 4 * math.sqrt(self.t_hover_n / 4 / self.k_f)

    @classmethod
    def from_vehicle(cls, path: Path | None = None) -> EnergyModel:
        """从 Vehicle Package 读取质量、电池与阻力参数（缺字段时保留缺省值）。"""
        import yaml

        p = path or ROOT / "vehicles" / "p600" / "params.yaml"
        try:
            d = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return cls()

        def val(node: Any, default: float) -> float:
            if isinstance(node, dict):
                node = node.get("value", default)
            try:
                return float(node)
            except (TypeError, ValueError):
                return default

        bat = d.get("battery") or {}
        aero = d.get("aero") or d.get("drag") or {}
        prop = d.get("prop") or {}

        def find(key: str, default: float) -> float:
            for blk in (d, aero, prop, bat):
                if isinstance(blk, dict) and key in blk:
                    return val(blk[key], default)
            return default

        m = cls()
        cap = val(bat.get("capacity_wh"), 222.0)
        frac = val(bat.get("usable_frac"), 0.85)
        return replace(m, p_hover_w=val(bat.get("p_hover_w"), m.p_hover_w), mass_kg=find("mass_kg", m.mass_kg),
                       e_use_wh=cap * frac, cda_m2=find("cda_m2", m.cda_m2), c_rd=find("c_rd", m.c_rd),
                       k_f=find("k_f", m.k_f))

    def scaled(self, p_scale: float) -> EnergyModel:
        return replace(self, p_hover_w=self.p_hover_w * p_scale)


@dataclass(frozen=True)
class WindProfile:
    speed_ref_mps: float = 0.0
    dir_from_deg: float = 0.0
    z_ref_m: float = 10.0
    alpha: float = 0.25

    @classmethod
    def from_scenario(cls, doc: dict) -> WindProfile:
        w = (((doc.get("env") or {}).get("patch") or {}).get("wind") or {})
        return cls(float(w.get("speed_ref_mps", 0.0) or 0.0), float(w.get("dir_from_deg", 0.0) or 0.0))

    def at(self, agl_m: float) -> tuple[float, float]:
        """去向风（ENU，m/s）；廓线按 AGL（最低 0.5 m）。"""
        s = self.speed_ref_mps * (max(agl_m, 0.5) / self.z_ref_m) ** self.alpha
        to = math.radians(self.dir_from_deg + 180.0)
        return s * math.sin(to), s * math.cos(to)


class Terrain(Protocol):
    def ground(self, x: float, y: float) -> float: ...          # DTM，world z
    def surface(self, x: float, y: float) -> float: ...         # DSM（出生点落在屋顶或地面），world z
    def top_along(self, a: tuple[float, float], b: tuple[float, float]) -> float: ...   # 线段上缓冲障碍顶，world z


class WorldTerrain:
    """World Package 地形（M04 `WorldQuery`）：障碍顶取 6 m 半径内的 DSM 最大值（12 §5.8.5 的缓冲口径）。"""

    def __init__(self, wq: Any, buffer_m: float = 6.0) -> None:
        self.wq = wq
        self.buffer_m = buffer_m

    def ground(self, x: float, y: float) -> float:
        return float(np.asarray(self.wq.ground_dtm(np.array([[x, y]], np.float64)))[0])

    def surface(self, x: float, y: float) -> float:
        return float(np.asarray(self.wq.height_dsm(np.array([[x, y]], np.float64)))[0])

    def tops(self, xy: np.ndarray) -> np.ndarray:
        return np.asarray(self.wq.column_max_within(np.asarray(xy, np.float64), radius_m=self.buffer_m), np.float64)

    def top_along(self, a: tuple[float, float], b: tuple[float, float]) -> float:
        n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1])) + 1)
        t = np.linspace(0.0, 1.0, n)
        xy = np.stack([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t], axis=1)
        return float(self.tops(xy).max())


@dataclass
class SortieResult:
    label: str
    soc_land: float
    t_total_s: float
    min_margin: float = math.inf
    energy_rtl_fired: dict | None = None
    precheck_soc: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def precheck_ok(self) -> bool:
        return (self.precheck_soc if self.precheck_soc is not None else self.soc_land) >= 0.20


# ---------------------------------------------------------------- 风阻模型（12 §5.8.4）
class _DragModel:
    def __init__(self, m: EnergyModel, wind: WindProfile, ground_z: float) -> None:
        self.m, self.wind, self.gz = m, wind, ground_z

    def power(self, vg: tuple[float, float], z: float, vz: float = 0.0) -> float:
        m = self.m
        climb = m.mass_kg * G * vz / m.eta_climb if vz > 0 else 0.0
        if not m.wind_drag:
            return m.p_hover_w + climb
        wx, wy = self.wind.at(z - self.gz)
        rx, ry = vg[0] - wx, vg[1] - wy
        vr = math.hypot(rx, ry)
        k = 0.5 * m.rho * m.cda_m2 * vr + m.omega_sum * m.c_rd
        th = m.t_hover_n
        t = math.sqrt(th ** 2 + (k * rx) ** 2 + (k * ry) ** 2)
        return m.p_hover_w * (t / th) ** 1.5 + climb

    def vertical(self, z0: float, z1: float, v: float, n: int = 60) -> tuple[float, float]:
        if abs(z1 - z0) < 1e-9:
            return 0.0, 0.0
        t = abs(z1 - z0) / v
        vz = v if z1 > z0 else 0.0
        e = sum(self.power((0.0, 0.0), z0 + (z1 - z0) * (k + 0.5) / n, vz) for k in range(n)) * t / n / 3600
        return t, e

    def horizontal(self, p, q, z: float, v: float, headwind: bool) -> tuple[float, float]:
        d = math.hypot(q[0] - p[0], q[1] - p[1])
        if d < 1e-9:
            return 0.0, 0.0
        ux, uy = (q[0] - p[0]) / d, (q[1] - p[1]) / d
        wx, wy = self.wind.at(z - self.gz)
        vc = max(1.0, v - max(0.0, -(wx * ux + wy * uy))) if headwind else v
        t = d / vc
        return t, self.power((vc * ux, vc * uy), z) * t / 3600


def helix_sortie(terrain: Terrain, center: tuple[float, float], r_m: float, home: tuple[float, float], z_start: float,
                 z_end: float, dz_nom_m: float, v_mps: float, wind: WindProfile, m: EnergyModel | None = None,
                 label: str = "helix") -> SortieResult:
    """一个螺旋扫描架次（起飞 → 在 home 上空爬升到转场高度 → 进入点 → 螺旋（整圈）→ 返航 → 着陆）。

    入场方位指向 home（`entry_azimuth_deg = auto`），整圈数 `ceil(|Δz| / dz_nom)`，扫描终点与入场点同方位（M10 §6.5.10）。
    返回落地 SOC（按实飞：巡航不减逆风）、预检 SOC（按估价：v_c = max(1, v − w_head)）与 ENERGY_RTL 判据。
    """
    m = m or EnergyModel()
    gz = terrain.ground(*center)
    dm = _DragModel(m, wind, gz)
    az0 = math.atan2(home[1] - center[1], home[0] - center[0])
    entry = (center[0] + r_m * math.cos(az0), center[1] + r_m * math.sin(az0))
    zh = terrain.surface(*home)
    zc = max(terrain.top_along(home, entry) + 5.0, z_start, zh + 2.5)
    t1, e1 = dm.vertical(zh, zc, m.v_climb_mps)
    t2, e2 = dm.horizontal(home, entry, zc, v_mps, False)
    t3, e3 = dm.vertical(zc, z_start, m.v_dn_mps) if zc > z_start else (0.0, 0.0)
    revs = max(1, math.ceil(abs(z_end - z_start) / dz_nom_m - 1e-9))
    dz = abs(z_end - z_start) / revs
    length = revs * 2 * math.pi * r_m
    th = length / v_mps
    vz = (z_end - z_start) / th
    n = revs * 360
    eh = 0.0
    for k in range(n):
        f = (k + 0.5) / n
        a = az0 + 2 * math.pi * revs * f
        eh += dm.power((-v_mps * math.sin(a), v_mps * math.cos(a)), z_start + (z_end - z_start) * f, max(vz, 0.0))
    eh = eh * th / n / 3600
    t_scan_end = t1 + t2 + t3 + th
    soc_scan_end = 1.0 - (e1 + e2 + e3 + eh) / m.e_use_wh

    def rtl(headwind: bool) -> tuple[float, float, float]:
        z_rtl = max(z_end, zh + 30.0, terrain.top_along(entry, home) + 5.0)
        a1, b1 = dm.vertical(z_end, z_rtl, m.v_rtl_up_mps)
        a2, b2 = dm.horizontal(entry, home, z_rtl, v_mps, headwind)
        a3, b3 = dm.vertical(z_rtl, zh + 10.0, m.v_dn_mps) if z_rtl > zh + 10 else (0.0, 0.0)
        a4, b4 = dm.vertical(min(z_rtl, zh + 10.0), zh, m.v_final_mps)
        return a1 + a2 + a3 + a4, b1 + b2 + b3 + b4, z_rtl

    tf, ef, z_rtl = rtl(True)
    ta, ea, _ = rtl(False)
    p_scan = eh * 3600 / th
    t_rem = (soc_scan_end - m.emerg) * m.e_use_wh / p_scan * 3600
    margin = t_rem / (m.rtl_margin * tf)
    return SortieResult(label, soc_scan_end - ea / m.e_use_wh, t_scan_end + ta, margin,
                        None if margin >= 1.0 else {"t_s": round(t_scan_end, 1)}, soc_scan_end - ef / m.e_use_wh,
                        {"revs": revs, "dz_m": dz, "length_m": length, "entry": entry, "z_rtl_m": z_rtl,
                         "t_scan_end_s": t_scan_end, "p_scan_w": p_scan, "home_z_m": zh, "transit_z_m": zc})


# ---------------------------------------------------------------- 1-D 悬停功率模型（M09 + 返航段）
class _Sortie1D:
    def __init__(self, m: EnergyModel) -> None:
        self.m = m
        self.soc, self.t, self.z, self.d_home = 1.0, 0, 0.0, 0.0
        self.worst = math.inf
        self.fired: dict | None = None
        self.ztrace: list[float] = []

    def _t_rtl(self) -> float:
        m = self.m
        z_rtl = max(self.z, 30.0)
        return (self.d_home / m.v_rtl_mps + max(0.0, z_rtl - self.z) / m.v_rtl_up_mps
                + max(0.0, z_rtl - 10.0) / m.v_dn_mps + 10.0 / m.v_land_mps)

    def step(self, dz: float = 0.0, dd: float = 0.0) -> None:
        m = self.m
        self.soc -= (m.p_hover_w / 3600 + m.mass_kg * G * max(dz, 0.0) / m.eta_climb / 3600) / m.e_use_wh
        self.z += dz
        self.d_home += dd
        self.t += 1
        self.ztrace.append(self.z)
        tre = (self.soc - m.emerg) * m.e_use_wh * 3600 / m.p_hover_w
        mg = tre / (m.rtl_margin * self._t_rtl())
        self.worst = min(self.worst, mg)
        if mg < 1.0 and self.fired is None:
            self.fired = {"t_s": self.t, "z_m": round(self.z, 1), "soc": round(self.soc, 3)}

    def run(self, legs: list[tuple]) -> _Sortie1D:
        m = self.m
        for leg in legs:
            k = leg[0]
            if k in ("up", "rtl_up", "dn"):
                rem = float(leg[1])
                v = {"up": m.v_climb_mps, "rtl_up": m.v_rtl_up_mps, "dn": m.v_dn_mps}[k]
                sgn = -1.0 if k == "dn" else 1.0
                while rem > 1e-9:
                    d = min(v, rem)
                    self.step(dz=sgn * d)
                    rem -= d
            elif k == "h":                       # ('h', dist, v, d_end)：离家距离线性过渡到 d_end
                dist, v = float(leg[1]), float(leg[2])
                d_end = float(leg[3]) if len(leg) > 3 else self.d_home + dist
                n = max(1, math.ceil(dist / v))
                dd = (d_end - self.d_home) / n
                for _ in range(n):
                    self.step(dd=dd)
            elif k == "hover":
                for _ in range(int(leg[1])):
                    self.step()
            elif k == "helix":                  # ('helix', z0, z1, v, dz_rev, r)
                z0, z1, v, dzr = map(float, leg[1:5])
                r = float(leg[5]) if len(leg) > 5 else 57.0
                rate = dzr / (2 * math.pi * r / v)
                s = rate if z1 > z0 else -rate
                while (self.z < z1) if s > 0 else (self.z > z1):
                    self.step(dz=s)
            else:
                raise ValueError(f"unknown leg {k!r}")
        return self

    def rtl_and_land(self) -> _Sortie1D:
        m = self.m
        z_rtl = max(self.z, 30.0)
        legs: list[tuple] = [("rtl_up", z_rtl - self.z)] if z_rtl > self.z else []
        legs += [("h", self.d_home, m.v_rtl_mps, 0.0), ("dn", max(0.0, z_rtl - 10.0))]
        self.run(legs)
        for _ in range(int(10 / m.v_land_mps)):
            self.step()
        return self


def simulate_sortie(legs: list[tuple], m: EnergyModel | None = None, label: str = "") -> SortieResult:
    """1-D 模型的一个架次（任务段后自动返航并着陆）：落地 SOC、最小余量比、ENERGY_RTL 是否触发。"""
    s = _Sortie1D(m or EnergyModel(wind_drag=False)).run(legs).rtl_and_land()
    return SortieResult(label, s.soc, float(s.t), s.worst, s.fired)


def s1_segment_1d(z0: float, z1: float, v: float = 6.0, overlap: float = 0.2, r_m: float = 57.0, d_home: float = 80.0,
                  m: EnergyModel | None = None) -> tuple[SortieResult, list[float]]:
    """S1 一段（1-D）：爬升到 z0 → 转场 → 螺旋到 z1 → 返航；返回结果与扫描期间高度序列（间距复核用）。"""
    dz_rev = 2 * 30.0 * math.tan(math.radians(42.1) / 2) * (1 - overlap)
    s = _Sortie1D(m or EnergyModel(wind_drag=False))
    s.run([("up", z0), ("h", d_home, v)])
    t0 = s.t
    s.run([("helix", z0, z1, v, dz_rev, r_m)])
    t1 = s.t
    trace = [math.nan] * t0 + s.ztrace[t0:t1]
    s.rtl_and_land()
    return SortieResult(f"S1 {z0:.0f}->{z1:.0f}", s.soc, float(s.t), s.worst, s.fired, None,
                        {"dz_rev_m": dz_rev, "scan_end_s": t1}), trace


# ---------------------------------------------------------------- 定稿参数与架次（M16 §6.4）
S1_PLAN = {
    "center": (-162.2, 77.3), "radius_m": 57.0, "dz_nom_m": 18.47, "speed_mps": 6.0,
    "p600-01": {"home": (-230.0, 20.0), "z": (252.0, 50.0)},
    "p600-02": {"home": (-230.0, 40.0), "z": (391.0, 248.0)},
}

# S2–S6 的保守 1-D 架次（`.cache/research/m16/scenario_final.py`，M16 §6.4.4–§6.4.7 的能量行；预言值为可用口径）
SX_LEGS: dict[str, dict] = {
    "s2-shanghai-formation#A": {"legs": [("up", 250), ("h", 120, 6, 120), ("hover", 30), ("h", 1100, 6, 820),
                                         ("h", 1100, 6, 120)], "soc_land": 0.444, "margin": 2.05},
    "s2-shanghai-formation#B": {"legs": [("up", 130), ("h", 150, 5, 150), ("h", 800, 5, 200)],
                                "soc_land": 0.708, "margin": 5.74},
    "s3-newyork-sar#a1": {"legs": [("up", 60), ("h", 232, 5, 232), ("h", 2310, 5, 300)], "soc_land": 0.510},
    "s3-newyork-sar#c1": {"legs": [("up", 150), ("h", 232, 5, 232), ("hover", 300)], "soc_land": 0.566},
    "s4-chicago-lakeshore#A": {"legs": [("up", 150), ("h", 216, 8, 216), ("h", 1800, 8, 1810), ("h", 1800, 8, 216)],
                               "soc_land": 0.470, "margin": 1.52},
    "s4-chicago-lakeshore#B": {"legs": [("up", 460), ("h", 120, 5, 120), ("h", 730, 5, 300)],
                               "soc_land": 0.424, "margin": 1.78},
    "s5-sanfrancisco-terrain": {"legs": [("up", 150), ("h", 260, 5, 260), ("h", 3000, 5, 300)],
                                "soc_land": 0.325, "margin": 2.44},
    "s6-suzhou-corridor#i": {"legs": [("up", 165), ("h", 1000, 10, 1000), ("h", 2000, 10, 1000), ("h", 1000, 10, 0)],
                             "soc_land": 0.548, "margin": 2.12},
    "s6-suzhou-corridor#r": {"legs": [("up", 170), ("h", 100, 5, 100), ("hover", 480)], "soc_land": 0.454},
}
