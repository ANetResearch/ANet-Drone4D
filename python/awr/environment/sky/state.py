"""SkyModel：sim-core 内的日历、太阳、照度、热背景与声学背景状态（AWR-04 §6.5、§7.3；ADR-102；M18-M19 PRD §6.11）。

- 太阳：日历已设置时 `mode = clock`，按 NOAA 由 `t_cal` 与世界锚点经纬度求位置（方位换算到网格北：
  `az_grid = az_true − trueNorth.yawOffsetDeg`）；未设置时 `mode = fixed`，取关键帧 `config.sun` 的固定方位与高度。
- 采样 `sample(t)` 以 1 s【仿真】量化（与"1 Hz 计算"一致，同一秒内的感知查询得到同一值，确定且与订阅无关）。
- 下发：`wire(sample)` 给出 `config.sun` 与顶层 D2 字段；`changed()` 判定相对上次下发是否需要新版本（太阳方向变化 ≥ 0.5°、
  照度档、热交叉、背景级 ≥ 0.5 dB、地表温度 ≥ 0.5 K、湿度 ≥ 1%、照度对数变化 ≥ 0.1）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from ..atmosphere.optics import water_vapour_gm3
from . import acoustic as AC
from .calendar import Calendar
from .illum import BAND_NAMES, illum_band, illuminance_lx
from .solar import solar_position, sun_dir_enu
from .thermal import DEFAULT_SURFACE, SURFACE_CLASSES, air_temp_c, k_cross_of, sky_temp_c, surface_temp_c, t_mean_c

__all__ = ["DEFAULT_LATLON", "PUBLISH_SUN_DEG", "SEC_NS", "SkyInputs", "SkyModel", "SkySample"]

SEC_NS = 1_000_000_000
DEFAULT_LATLON = (30.0, 120.0)  # synthcity 示意锚点（ADR-077）
PUBLISH_SUN_DEG = 0.5
PUBLISH_DB = 0.5
PUBLISH_K = 0.5
PUBLISH_RH_PCT = 1.0
PUBLISH_LG_E = 0.1
DEFAULT_ACOUSTIC = "rural"


@dataclass(frozen=True, slots=True)
class SkyInputs:
    """天气输入（来自 eval_env 与 derive 在采样时刻的值）。"""

    cover: float = 0.0
    rain_eff_mmh: float = 0.0
    rh: float = 0.5
    isa_dt_c: float = 0.0
    cloud_base_m: float = 1500.0
    mor_m: float = 30000.0
    wetness: float = 0.0


@dataclass(frozen=True, slots=True)
class SkySample:
    """某一仿真秒的环境感知量（M19 EnvSample 的超集，字段名与 `tools/fake/perception_stub.EnvSample` 一致）。"""

    t_ns: int
    mode: str                     # fixed | clock
    sun_az_deg: float             # 网格北顺时针
    sun_el_deg: float             # 几何高度
    illum_lx: float
    band_code: int                # 0 day, 1 low, 2 night（LightBand）
    mor_m: float
    rho_v_gm3: float
    humidity_pct: float
    t_air_c: float
    t_bg_c: float
    eps_bg: float
    t_sky_c: float
    k_cross: float
    rain_mmph: float
    cloud_cover: float
    acoustic_profile: str
    l_bg_dba: float
    l_bg_bands_dba: NDArray[np.float64] = field(repr=False)
    t_cal_unix_ns: int | None = None
    tst_min: float | None = None

    @property
    def band(self) -> str:
        return BAND_NAMES[self.band_code]


def _r(x: float, nd: int) -> float:
    v = round(float(x), nd)
    return 0.0 if v == 0 else v


def _sig(x: float, n: int = 4) -> float:
    x = float(x)
    if x == 0 or not math.isfinite(x):
        return 0.0
    return float(f"{x:.{n}g}")


class SkyModel:
    def __init__(self, *, lat_deg: float = DEFAULT_LATLON[0], lon_deg: float = DEFAULT_LATLON[1], yaw_offset_deg: float = 0.0,
                 h_ground_msl_m: float = 0.0) -> None:
        self.lat = float(lat_deg)
        self.lon = float(lon_deg)
        self.yaw_off = float(yaw_offset_deg)
        self.h_ground_msl = float(h_ground_msl_m)
        self.calendar: Calendar | None = None
        self.acoustic = DEFAULT_ACOUSTIC
        self.surface = DEFAULT_SURFACE
        self.moon_lx = 0.0
        self.enabled = False      # 关键帧是否携带 D2 字段（日历或声学背景被配置后为真；D1 帧逐字节不变）
        self.rev = 0              # 配置修订号（缓存键）
        self.published: dict[str, Any] | None = None
        self._cache: dict[tuple, SkySample] = {}

    # ------------------------------------------------------------ 配置
    def _bump(self) -> None:
        self.rev += 1
        self._cache.clear()

    def set_calendar(self, cal: Calendar | None) -> None:
        self.calendar = cal
        self.enabled = self.enabled or cal is not None
        self._bump()

    def set_acoustic(self, profile: str) -> None:
        if profile not in AC.PROFILES:
            raise ValueError(profile)
        self.acoustic = profile
        self.enabled = True
        self._bump()

    def set_surface(self, cls: str) -> None:
        if cls not in SURFACE_CLASSES:
            raise ValueError(cls)
        self.surface = cls
        self._bump()

    def state(self) -> dict[str, Any]:
        cal = self.calendar
        return {"enabled": self.enabled, "calendar": None if cal is None else [cal.cal0_unix_ms, cal.tz], "acoustic": self.acoustic,
                "surface": self.surface, "moon_lx": self.moon_lx, "published": self.published}

    def restore(self, st: dict[str, Any] | None) -> None:
        if not st:
            return
        cal = st.get("calendar")
        self.calendar = None if not cal else Calendar(int(cal[0]), str(cal[1]))
        self.acoustic = str(st.get("acoustic", DEFAULT_ACOUSTIC))
        self.surface = str(st.get("surface", DEFAULT_SURFACE))
        self.moon_lx = float(st.get("moon_lx", 0.0))
        self.enabled = bool(st.get("enabled", False))
        self.published = st.get("published")
        self._bump()

    # ------------------------------------------------------------ 太阳
    def sun_at(self, t_ns: int, fixed: tuple[float, float]) -> tuple[float, float, float | None]:
        """-> (az_grid_deg, el_deg, tst_min | None)。"""
        cal = self.calendar
        if cal is None:
            return float(fixed[0]) % 360.0, float(fixed[1]), None
        p = solar_position(cal.t_cal_ms(t_ns), self.lat, self.lon)
        return (p.az_deg - self.yaw_off) % 360.0, p.el_deg, p.tst_min

    def el_at_cal_ms(self, cal_ms: float) -> float:
        return solar_position(cal_ms, self.lat, self.lon).el_deg

    # ------------------------------------------------------------ 采样
    def peek(self, t_ns: int, key: Any = None) -> SkySample | None:
        """缓存命中时直接返回（调用方可省去天气求值）。"""
        return self._cache.get(((int(t_ns) // SEC_NS) * SEC_NS, key, self.rev))

    def sample(self, t_ns: int, inp: SkyInputs, fixed: tuple[float, float], key: Any = None) -> SkySample:
        tq = (int(t_ns) // SEC_NS) * SEC_NS
        ck = (tq, key, self.rev)
        hit = self._cache.get(ck)
        if hit is not None:
            return hit
        az, el, tst = self.sun_at(tq, fixed)
        cal = self.calendar
        illum = illuminance_lx(el, inp.cover, self.moon_lx)
        band = illum_band(el)
        tm = t_mean_c(self.h_ground_msl, inp.isa_dt_c)
        t_air = tm if tst is None else air_temp_c(tm, tst, inp.cover)
        cls = SURFACE_CLASSES[self.surface]
        t_bg = surface_temp_c(t_air, el, inp.cover, inp.wetness, cls)
        t_sky = sky_temp_c(t_air, inp.rh, inp.cover, inp.cloud_base_m)
        if cal is None:
            kc = 1.0
        else:
            c_ms = cal.t_cal_ms(tq)
            kc = k_cross_of(el, self.el_at_cal_ms(c_ms - 3_600_000.0), self.el_at_cal_ms(c_ms + 3_600_000.0))
        rho_v = water_vapour_gm3(inp.rh, tm)
        l_bg = AC.level_dba(self.acoustic, el, inp.rain_eff_mmh)
        smp = SkySample(t_ns=tq, mode="fixed" if cal is None else "clock", sun_az_deg=az, sun_el_deg=el, illum_lx=illum, band_code=band,
                        mor_m=inp.mor_m, rho_v_gm3=rho_v, humidity_pct=100.0 * min(1.0, max(0.0, inp.rh)), t_air_c=t_air, t_bg_c=t_bg,
                        eps_bg=cls.eps, t_sky_c=t_sky, k_cross=kc, rain_mmph=inp.rain_eff_mmh, cloud_cover=inp.cover,
                        acoustic_profile=self.acoustic, l_bg_dba=l_bg, l_bg_bands_dba=AC.band_levels_dba(self.acoustic, l_bg),
                        t_cal_unix_ns=None if cal is None else cal.t_cal_unix_ns(tq), tst_min=tst)
        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[ck] = smp
        return smp

    # ------------------------------------------------------------ 下发
    @staticmethod
    def wire(smp: SkySample) -> tuple[dict[str, Any], dict[str, Any]]:
        """-> (config.sun, 顶层 D2 字段 humidity_pct、surface、acoustic_bg)；calendar 由关键帧按 t_ns 生成。"""
        sun = {"azimuth_deg": _r(smp.sun_az_deg, 3), "elevation_deg": _r(smp.sun_el_deg, 3), "mode": smp.mode,
               "illum_lx": _sig(smp.illum_lx, 4), "band": smp.band}
        top = {"humidity_pct": _r(smp.humidity_pct, 2),
               "surface": {"t_c": _r(smp.t_bg_c, 2), "emissivity": smp.eps_bg, "k_cross": smp.k_cross},
               "acoustic_bg": {"profile": smp.acoustic_profile, "l_bg_dba": _r(smp.l_bg_dba, 2)}}
        return sun, top

    def changed(self, sun: dict[str, Any], top: dict[str, Any]) -> list[str]:
        """相对上次下发的变化项（空表示不必发新版本）。"""
        p = self.published
        if p is None:
            return ["initial"]
        ps, pt = p["sun"], p["top"]
        why: list[str] = []
        a = sun_dir_enu(sun["azimuth_deg"], sun["elevation_deg"])
        b = sun_dir_enu(ps["azimuth_deg"], ps["elevation_deg"])
        cosang = min(1.0, max(-1.0, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]))
        if math.degrees(math.acos(cosang)) >= PUBLISH_SUN_DEG - 1e-9:
            why.append("sun")
        if sun["band"] != ps["band"] or sun["mode"] != ps["mode"]:
            why.append("band")
        e1, e0 = max(sun["illum_lx"], 1e-6), max(ps["illum_lx"], 1e-6)
        if abs(math.log10(e1) - math.log10(e0)) >= PUBLISH_LG_E:
            why.append("illum")
        if top["surface"]["k_cross"] != pt["surface"]["k_cross"] or top["surface"]["emissivity"] != pt["surface"]["emissivity"]:
            why.append("k_cross")
        if abs(top["surface"]["t_c"] - pt["surface"]["t_c"]) >= PUBLISH_K:
            why.append("surface")
        if (top["acoustic_bg"]["profile"] != pt["acoustic_bg"]["profile"]
                or abs(top["acoustic_bg"]["l_bg_dba"] - pt["acoustic_bg"]["l_bg_dba"]) >= PUBLISH_DB):
            why.append("acoustic")
        if abs(top["humidity_pct"] - pt["humidity_pct"]) >= PUBLISH_RH_PCT:
            why.append("humidity")
        return why

    def mark_published(self, sun: dict[str, Any], top: dict[str, Any]) -> None:
        self.published = {"sun": dict(sun), "top": {k: (dict(v) if isinstance(v, dict) else v) for k, v in top.items()}}
