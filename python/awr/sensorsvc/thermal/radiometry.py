"""热成像辐射换算的 CPU 参考实现（M25 §15.6 管线第 4 步与第 5 步 `B⁻¹`；AWR-05 §5.4.10；AWR-20 §15.16.2）。

- 波段积分 Planck 辐亮度 `B(T) = ∫_{8 µm}^{14 µm} 2hc²/λ⁵ / (exp(hc/(λkT)) − 1) dλ`（W·m⁻²·sr⁻¹）。解析值以
  `x = hc/(λkT)` 换元后的不完全积分级数求得：`∫_x^∞ t³/(eᵗ − 1) dt = Σ_n e^(−nx)·(x³/n + 3x²/n² + 6x/n³ + 6/n⁴)`，
  `B(T) = 2k⁴T⁴/(h³c²)·[F(x₁₄) − F(x₈)]`；`dB/dT` 取同一级数的闭式导数。常数取 SI 2019 精确值。
- 查找表 `PlanckTable`：200–400 K、步长 0.01 K 共 20001 点，float64 解析值转 float32（构建期生成，`sha256` 为表字节摘要，
  进入运行清单）；正变换为表内线性插值，反变换为同一折线的精确反函数（二分定位 + 线性插值），故往返误差只来自 float32
  舍入。超出 [200, 400] K 的温度与对应辐亮度饱和于端点。
- 辐射换算：`L_refl = w_sky·B(T_sky) + (1 − w_sky)·B(T_air)`，`w_sky = clamp(n_z, 0, 1)`；`L_obj = ε·B(T_s) + (1 − ε)·L_refl`；
  `L_sensor = τ(d)·L_obj + (1 − τ(d))·B(T_air)`，`τ(d) = exp(−β·d)`；无几何像素直接取 `B(T_sky)`（等效天空温度已含整层
  大气路径，不再叠加透过项）。天空有效温度 `T_sky = T_air − 25·(1 − 0.8·cloud)` K。
- 大气消光（km⁻¹，系数暂定，形式固定）：`β = β_clear(rh) + β_fog + β_rain`，`β_clear = 0.02 + 0.06·rh`，
  `β_fog = 0.5·3.912/V_km`（只在给出雾能见度 V 时计入），`β_rain = 0.25·R^0.63`（R 为雨强 mm/h）。
- 降雨时发射率 +0.02（上限 0.99，雨强 ≥ 0.5 mm/h 视为湿润）；距离 d 为斜距（沿视线），由平面深度按像素视线长度换算。

只依赖 numpy；GPU 后处理着色器以本模块为参照做一致性测试（容差见 `tests/d3/sensors/test_thermal_core.py`）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

__all__ = ["BAND_UM", "PLANCK_TABLE_VERSION", "T_MAX_K", "T_MIN_K", "T_STEP_K", "WET_RAIN_MMH", "PlanckTable", "ThermalEnv",
           "band_dbdt_exact", "band_radiance_exact", "band_temperature_exact", "extinction_beta_per_km", "object_radiance",
           "planck_table", "range_from_depth", "reflected_radiance", "sensor_radiance", "sky_temperature_k", "transmittance",
           "wet_emissivity"]

PLANCK_TABLE_VERSION = "planck-band-lut@1"
BAND_UM = (8.0, 14.0)
T_MIN_K, T_MAX_K, T_STEP_K = 200.0, 400.0, 0.01
N_TABLE = 20001                                   # (400 − 200)/0.01 + 1
WET_RAIN_MMH = 0.5                                # 雨强 ≥ 0.5 mm/h 视为湿润（AWR-05 §5.4.10 静态表面温度与发射率）

_H = 6.62607015e-34                               # 普朗克常数 J·s（SI 2019 精确值）
_C = 299792458.0                                  # 光速 m/s
_K = 1.380649e-23                                 # 玻尔兹曼常数 J/K
_A = _H * _C / _K                                 # 第二辐射常数 c₂ = hc/k（m·K）
_CB = 2.0 * _K ** 4 / (_H ** 3 * _C ** 2)         # B(T) = _CB·T⁴·G(T)
_N_SERIES = 40                                    # x ≥ 2.57（14 µm、400 K）时 e^(−40x) < 1e−44，级数截断误差可忽略


def _tail(x: np.ndarray) -> np.ndarray:
    """F(x) = ∫_x^∞ t³/(eᵗ − 1) dt（x > 0），按 n = 1..40 级数逐项累加（float64）。"""
    x = np.asarray(x, np.float64)
    out = np.zeros_like(x)
    for n in range(_N_SERIES, 0, -1):                                    # 由小项到大项累加，减少舍入
        out += np.exp(-n * x) * (x ** 3 / n + 3.0 * x ** 2 / n ** 2 + 6.0 * x / n ** 3 + 6.0 / n ** 4)
    return out


def _f(x: np.ndarray) -> np.ndarray:
    return x ** 3 / np.expm1(x)


def _xs(t_k, band_um: tuple[float, float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    t = np.asarray(t_k, np.float64)
    x_short = _A / (band_um[0] * 1e-6 * t)                               # 短波端（8 µm）：x 大
    x_long = _A / (band_um[1] * 1e-6 * t)                                # 长波端（14 µm）：x 小
    return t, x_short, x_long


def band_radiance_exact(t_k, band_um: tuple[float, float] = BAND_UM) -> np.ndarray:
    """波段积分 Planck 辐亮度解析值（W·m⁻²·sr⁻¹，float64）。"""
    t, xs, xl = _xs(t_k, band_um)
    return _CB * t ** 4 * (_tail(xl) - _tail(xs))


def band_dbdt_exact(t_k, band_um: tuple[float, float] = BAND_UM) -> np.ndarray:
    """dB/dT 解析值（W·m⁻²·sr⁻¹·K⁻¹，float64）：`_CB·T³·[4G + x_l·f(x_l) − x_s·f(x_s)]`，`dx/dT = −x/T`。"""
    t, xs, xl = _xs(t_k, band_um)
    g = _tail(xl) - _tail(xs)
    return _CB * t ** 3 * (4.0 * g + xl * _f(xl) - xs * _f(xs))


def band_temperature_exact(radiance, band_um: tuple[float, float] = BAND_UM) -> np.ndarray:
    """B⁻¹ 解析值（float64，牛顿迭代至收敛）：用于构造用例的解析解，不进入渲染路径。"""
    lv = np.asarray(radiance, np.float64)
    t = np.full_like(lv, 300.0)
    for _ in range(60):
        step = (band_radiance_exact(t, band_um) - lv) / band_dbdt_exact(t, band_um)
        t = np.clip(t - step, 50.0, 2000.0)
        if float(np.max(np.abs(step), initial=0.0)) < 1e-12:
            break
    return t


@dataclass(frozen=True, eq=False)
class PlanckTable:
    """波段积分 Planck 正反查找表（float32，200–400 K，0.01 K 步长）。"""

    band_um: tuple[float, float]
    radiance_lut: np.ndarray                     # (20001,) float32，B(T_MIN + i·T_STEP)
    dbdt_lut: np.ndarray                         # (20001,) float32，dB/dT 同网格
    sha256: str                                  # radiance_lut 与 dbdt_lut 字节（小端）的 sha256
    version: str = PLANCK_TABLE_VERSION

    def _locate_t(self, t_k) -> tuple[np.ndarray, np.ndarray]:
        u = (np.clip(np.asarray(t_k, np.float64), T_MIN_K, T_MAX_K) - T_MIN_K) / T_STEP_K
        i = np.clip(np.floor(u).astype(np.int64), 0, N_TABLE - 2)
        return i, u - i

    def radiance(self, t_k) -> np.ndarray:
        """B(T)：表内线性插值（float32 输出）。"""
        i, f = self._locate_t(t_k)
        lut = self.radiance_lut.astype(np.float64)
        return (lut[i] + f * (lut[i + 1] - lut[i])).astype(np.float32)

    def dbdt(self, t_k) -> np.ndarray:
        """dB/dT：表内线性插值（float32 输出）。"""
        i, f = self._locate_t(t_k)
        lut = self.dbdt_lut.astype(np.float64)
        return (lut[i] + f * (lut[i + 1] - lut[i])).astype(np.float32)

    def temperature(self, radiance) -> np.ndarray:
        """B⁻¹(L)：同一折线的精确反函数（二分定位 + 线性插值，float32 输出），越界饱和于 200 K 与 400 K。"""
        lut = self.radiance_lut.astype(np.float64)
        lv = np.asarray(radiance, np.float64)
        i = np.clip(np.searchsorted(lut, lv, side="right") - 1, 0, N_TABLE - 2)
        f = np.clip((lv - lut[i]) / (lut[i + 1] - lut[i]), 0.0, 1.0)
        return (T_MIN_K + (i + f) * T_STEP_K).astype(np.float32)


@lru_cache(maxsize=4)
def planck_table(band_um: tuple[float, float] = BAND_UM) -> PlanckTable:
    """构建（并缓存）波段 Planck 表；同一波段在任何主机上逐字节相同，摘要写入运行清单。"""
    band = (float(band_um[0]), float(band_um[1]))
    if not 0.0 < band[0] < band[1]:
        raise ValueError(f"invalid band {band_um!r}")
    t = T_MIN_K + np.arange(N_TABLE, dtype=np.float64) * T_STEP_K
    rad = band_radiance_exact(t, band).astype("<f4")
    der = band_dbdt_exact(t, band).astype("<f4")
    rad.setflags(write=False)
    der.setflags(write=False)
    digest = hashlib.sha256(rad.tobytes() + der.tobytes()).hexdigest()
    return PlanckTable(band, rad, der, digest)


@dataclass(frozen=True)
class ThermalEnv:
    """一帧的热成像环境量（取自 M07 环境时间线）。"""

    t_air_k: float
    cloud: float = 0.0                           # 云量 0–1
    rh: float = 0.5                              # 相对湿度 0–1
    fog_visibility_km: float | None = None       # 雾能见度；None 表示无雾
    rain_mmh: float = 0.0                        # 雨强 mm/h
    t_sky_k: float | None = None                 # 显式天空有效温度（缺省按云量公式）

    @property
    def sky_k(self) -> float:
        return sky_temperature_k(self.t_air_k, self.cloud) if self.t_sky_k is None else float(self.t_sky_k)

    @property
    def beta_per_km(self) -> float:
        return extinction_beta_per_km(self.rh, self.fog_visibility_km, self.rain_mmh)


def sky_temperature_k(t_air_k: float, cloud: float) -> float:
    """天空有效温度 `T_sky = T_air − 25·(1 − 0.8·cloud)` K（晴空 T_air − 25 K，全云 T_air − 5 K）。"""
    c = min(1.0, max(0.0, float(cloud)))
    return float(t_air_k) - 25.0 * (1.0 - 0.8 * c)


def extinction_beta_per_km(rh: float, fog_visibility_km: float | None = None, rain_mmh: float = 0.0) -> float:
    """长波红外消光系数 β（km⁻¹）= 晴空（湿度）+ 雾 + 雨。"""
    beta = 0.02 + 0.06 * min(1.0, max(0.0, float(rh)))
    if fog_visibility_km is not None:
        if fog_visibility_km <= 0:
            raise ValueError(f"fog visibility must be positive, got {fog_visibility_km!r}")
        beta += 0.5 * 3.912 / float(fog_visibility_km)
    if rain_mmh > 0:
        beta += 0.25 * float(rain_mmh) ** 0.63
    return beta


def transmittance(range_m, beta_per_km: float) -> np.ndarray:
    """τ(d) = exp(−β·d)，d 为斜距（m），β 为 km⁻¹（float64）。"""
    return np.exp(-float(beta_per_km) * np.asarray(range_m, np.float64) * 1e-3)


def wet_emissivity(eps, rain_mmh: float) -> np.ndarray:
    """降雨（≥ 0.5 mm/h）时发射率 +0.02，上限 0.99；否则原值。"""
    e = np.asarray(eps, np.float64)
    return np.minimum(e + 0.02, 0.99) if rain_mmh >= WET_RAIN_MMH else e


def range_from_depth(depth_m: np.ndarray, fx: float, fy: float, cx: float, cy: float) -> np.ndarray:
    """平面深度 (H, W) → 斜距：`d = z·sqrt(1 + x² + y²)`，x、y 为像素中心的归一化坐标（与 `camera.effects._cos4` 同一约定）。"""
    z = np.asarray(depth_m, np.float64)
    h, w = z.shape
    x = (np.arange(w, dtype=np.float64) + 0.5 - cx) / fx
    y = (np.arange(h, dtype=np.float64) + 0.5 - cy) / fy
    return z * np.sqrt(1.0 + x[None, :] ** 2 + y[:, None] ** 2)


def reflected_radiance(table: PlanckTable, n_z, t_sky_k: float, t_air_k: float) -> np.ndarray:
    """反射环境辐射 `L_refl = w_sky·B(T_sky) + (1 − w_sky)·B(T_air)`，`w_sky = clamp(n_z, 0, 1)`（float64）。"""
    w = np.clip(np.asarray(n_z, np.float64), 0.0, 1.0)
    b_sky = float(table.radiance(t_sky_k))
    b_air = float(table.radiance(t_air_k))
    return w * b_sky + (1.0 - w) * b_air


def object_radiance(table: PlanckTable, t_s_k, eps, n_z, t_sky_k: float, t_air_k: float) -> np.ndarray:
    """表面出射辐亮度 `L_obj = ε·B(T_s) + (1 − ε)·L_refl`（float64）。"""
    e = np.clip(np.asarray(eps, np.float64), 0.0, 1.0)
    return e * table.radiance(t_s_k).astype(np.float64) + (1.0 - e) * reflected_radiance(table, n_z, t_sky_k, t_air_k)


def sensor_radiance(table: PlanckTable, t_s_k, eps, n_z, range_m, env: ThermalEnv) -> np.ndarray:
    """入瞳辐亮度（float32，单个子帧）：`τ(d)·L_obj + (1 − τ(d))·B(T_air)`；斜距非有限或 ≤ 0 的像素视为无几何，取 B(T_sky)。

    各输入为可广播数组（逐像素表面温度、发射率、法向竖直分量、斜距）；子帧加权、光学模糊与 `B⁻¹` 由 `noise.detector` 完成。
    """
    t_sky, t_air = env.sky_k, float(env.t_air_k)
    d = np.asarray(range_m, np.float64)
    geom = np.isfinite(d) & (d > 0)
    tau = transmittance(np.where(geom, d, 0.0), env.beta_per_km)
    l_obj = object_radiance(table, t_s_k, eps, n_z, t_sky, t_air)
    l_sen = tau * l_obj + (1.0 - tau) * float(table.radiance(t_air))
    return np.where(geom, l_sen, float(table.radiance(t_sky))).astype(np.float32)

