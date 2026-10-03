"""M07 env stage 行缓存写入与风场换算的 numba 核（M07-FR-017；ADR-060 摊销；FX2-R3，ADR-070）。

`EnvironmentServiceImpl.store_rows`、`store_wind_rows`（EnvSample32 行缓存，结构化数组的字段花式下标赋值）在 N = 1000 时
分别约 0.30、0.12 ms/次（store_rows 10 Hz，store_wind_rows 40 Hz）。
本模块的核对同一组数据一次遍历写完，运算与 numpy 实现逐项相同：

- 量化 `np.clip(np.round(x / q), lo, hi)` 再赋给整数字段：`np.round`（decimals = 0）即 `rint`（四舍六入五成双），clip 后的
  整数值经 C 截断转换写入；float64 -> float32 字段为同一舍入；float32 输入按 numpy 2 的弱标量规则在 float32 中运算。

输入假定有限（env 查询对非有限位置不走这里：融合路径返回 False 后 numpy 路径同样只写有限值）。`tests/environment/
test_env_rows_kernel.py` 与 numpy 实现逐字节对拍。numba 不可用或 `AWR_KERNEL=numpy` 时 `HAVE_NUMBA` 为 False，调用方保持
numpy 实现。
"""

from __future__ import annotations

import math
import os

import numpy as np

try:
    from numba import njit

    HAVE_NUMBA = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover
    HAVE_NUMBA = False

    def njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f

        return deco(a[0]) if a and callable(a[0]) else deco

__all__ = ["HAVE_NUMBA", "full_rest_nb", "store_rows_nb", "store_wind_rows_nb", "warmup"]


@njit(cache=True, fastmath=False, inline="always")
def _q(x, q, lo, hi):
    """float64 量化（wind_mean：float64 / 0.01）。"""
    v = np.rint(x / q)
    if v < lo:
        v = lo
    elif v > hi:
        v = hi
    return v


@njit(cache=True, fastmath=False, inline="always")
def _q32(x, q, lo, hi):
    """float32 量化：查询缓冲中的湍流标准差、阵风、雨量、密度为 float32，numpy 2 的弱标量规则下 `x / 0.01`、round 与 clip
    都在 float32 中完成（q、lo、hi 由调用方给 float32 常量）。"""
    v = np.rint(x / q)
    if v < lo:
        v = lo
    elif v > hi:
        v = hi
    return v


@njit(cache=True, fastmath=False)
def store_rows_nb(sl, n, wind, wmean, tsig, sig, rain, rho, flags, src, glong,
                  r_wind, r_wmean, r_ts, r_sig, r_rain, r_rho, r_flags, r_src, r_gust, row_valid):
    for k in range(n):
        s = sl[k]
        for d in range(3):
            r_wind[s, d] = np.float32(wind[k, d])
            r_wmean[s, d] = np.int16(_q(wmean[k, d], 0.01, -32768.0, 32767.0))
        r_ts[s, 0] = np.uint8(_q32(tsig[k, 0], np.float32(0.05), np.float32(0.0), np.float32(255.0)))
        r_ts[s, 1] = np.uint8(_q32(tsig[k, 2], np.float32(0.05), np.float32(0.0), np.float32(255.0)))
        r_sig[s] = np.float32(sig[k])
        r_rain[s] = np.uint16(_q32(rain[k], np.float32(0.01), np.float32(0.0), np.float32(65535.0)))
        r_rho[s] = np.uint16(_q32(rho[k], np.float32(3e-5), np.float32(0.0), np.float32(65535.0)))
        r_flags[s] = flags[k]
        r_src[s] = src[k]
        r_gust[s] = np.int16(_q32(glong[k], np.float32(0.01), np.float32(-32768.0), np.float32(32767.0)))
        row_valid[s] = True


@njit(cache=True, fastmath=False)
def store_wind_rows_nb(sl, n, wind, glong, r_wind, r_gust):
    for k in range(n):
        s = sl[k]
        for d in range(3):
            r_wind[s, d] = np.float32(wind[k, d])
        r_gust[s] = np.int16(_q32(glong[k], np.float32(0.01), np.float32(-32768.0), np.float32(32767.0)))


@njit(cache=True, fastmath=False)
def full_rest_nb(pos, zg, n, ground_z, h_anchor, sw, ft, haze0, h_haze, sig_fog, sig_precip, fog_top, base, k_mor,
                 flag_fog, flag_below, want_optics, want_turb, want_precip, want_thermo, rain, snow, dust, isa_dt, rh,
                 src_level, flags, sigma_ext, mor, sigma_precip_pm, turb_sigma, turb_l, rain_eff, snow_eff, dust_o, temp,
                 press, rho, rh_o, source_level):
    """env 全量求值中风以外的字段（`EnvironmentServiceImpl.query` 在风已走融合核之后的其余部分：MIL 湍流谱、光学、降水、
    ISA 热力、标志与来源级别）一次遍历写完（ADR-073 第 3 条）。逐项运算与 numpy 路径相同：`np.maximum(x, c)` 写作
    `x if x >= c else c`（输入有限），`**` 与 `math.exp` 与 numpy 同用 libm，float64 结果写入 float32 字段为同一舍入；
    `tests/environment/test_env_full_kernel.py` 与 numpy 路径逐字节对拍。N = 1000 时 numpy 路径约 0.5 ms（生产口径约 1 ms），
    是 env 全量 tick 与普通 tick 之差的主体。"""
    for k in range(n):
        z_agl = pos[k, 2] - zg[k]
        x = z_agl / ft
        hft = x if x >= 10.0 else 10.0
        fl = flags[k]
        if want_optics:
            z = pos[k, 2] - ground_z
            s = haze0 * math.exp(-z / h_haze)
            fog = z < fog_top
            s = s + (sig_fog if fog else 0.0)
            if fog:
                fl = fl | flag_fog
            below = z < base
            s = s + (sig_precip if below else 0.0)
            if sig_precip > 0 and below:
                fl = fl | flag_below
            sigma_ext[k] = s
            mor[k] = k_mor / (s if s >= 1e-12 else 1e-12)
            sigma_precip_pm[k] = sig_precip if below else 0.0
        flags[k] = fl
        source_level[k] = src_level
        if want_turb:
            su = sw / (0.177 + 0.000823 * hft) ** 0.4
            lu = hft / (0.177 + 0.000823 * hft) ** 1.2 * ft
            turb_sigma[k, 0] = su
            turb_sigma[k, 1] = su
            turb_sigma[k, 2] = sw
            turb_l[k, 0] = lu
            turb_l[k, 1] = lu
            turb_l[k, 2] = hft * ft
        if want_precip:
            rain_eff[k] = rain
            snow_eff[k] = snow
            dust_o[k] = dust
        if want_thermo:
            h = h_anchor + pos[k, 2]
            t_isa = 288.15 - 0.0065 * h
            t = t_isa + isa_dt
            p = 101325.0 * (t_isa / 288.15) ** 5.25588
            temp[k] = t - 273.15
            press[k] = p
            rho[k] = p / (287.053 * t)
            rh_o[k] = rh


def full_rest(o, pos, zg, n, ground_z, h_anchor, sw, ft, haze0, h_haze, sig_fog, sig_precip, fog_top, base, k_mor, flag_fog,
              flag_below, fields_optics, fields_turb, fields_precip, fields_thermo, rain, snow, dust, isa_dt, rh,
              src_level) -> None:
    """`full_rest_nb` 的包装（EnvSampleSoA 字段解包；标量统一为 float64、标志为 uint8，签名固定以命中预热缓存）。"""
    full_rest_nb(pos, zg, int(n), float(ground_z), float(h_anchor), float(sw), float(ft), float(haze0), float(h_haze),
                 float(sig_fog), float(sig_precip), float(fog_top), float(base), float(k_mor), np.uint8(flag_fog),
                 np.uint8(flag_below), bool(fields_optics), bool(fields_turb), bool(fields_precip), bool(fields_thermo),
                 float(rain), float(snow), float(dust), float(isa_dt), float(rh), np.uint8(src_level), o.flags,
                 o.sigma_ext_per_m, o.mor_m, o.sigma_precip_per_m, o.turb_sigma_mps, o.turb_l_m, o.rain_eff_mmh,
                 o.snow_eff_mmh, o.dust, o.temperature_c, o.pressure_pa, o.rho_kgm3, o.rh, o.source_level)


def warmup() -> None:
    """以运行期类型调用一次：slot 为 int64、查询缓冲为 C 连续 float64/uint8、行缓存为 EnvSample32 的字段视图。"""
    if not HAVE_NUMBA:
        return
    from awr.contracts.layouts import ENV_SAMPLE32

    from .query import EnvSampleSoA

    o = EnvSampleSoA.alloc(2)
    r = np.zeros(4, ENV_SAMPLE32)
    rv = np.zeros(4, np.bool_)
    sl = np.arange(2, dtype=np.int64)
    store_rows_nb(sl, 2, o.wind_mps, o.wind_mean_mps, o.turb_sigma_mps, o.sigma_ext_per_m, o.rain_eff_mmh, o.rho_kgm3,
                  o.flags, o.source_level, o.gust_long_mps, r["wind"], r["wind_mean"], r["turb_sigma_uw"], r["sigma_ext"],
                  r["rain_eff"], r["rho"], r["flags"], r["source_level"], r["gust"], rv)
    store_wind_rows_nb(sl, 2, o.wind_mps, o.gust_long_mps, r["wind"], r["gust"])
    full_rest(o, np.zeros((2, 3)), np.zeros(2), 2, 0.0, 0.0, 0.5, 0.3048, 1e-4, 1000.0, 0.0, 0.0, 0.0, 0.0, 3.912, 16, 32,
              True, True, True, True, 0.0, 0.0, 0.0, 0.0, 0.5, 1)
