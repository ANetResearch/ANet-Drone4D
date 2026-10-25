"""热成像探测器模型的 CPU 参考实现（M25 §15.6 管线第 5 步；AWR-05 §5.4.10 探测器；AWR-20 §15.6.5、§15.16.2 噪声档）。

顺序：子帧时间常数加权（辐亮度）→ 高斯光学模糊（辐亮度）→ `T_app = B⁻¹(L)` → 固定图案噪声 → NETD 时间噪声 → 0.01 K 量化为 u16。

- 子帧加权：`w_j ∝ exp(−(t_end − t_j)/τ_b)`，归一化后对各子帧入瞳辐亮度加权求和（子帧时刻由渲染侧给出）。
- 光学模糊：可分离高斯核，半径 `ceil(3sigma)`，权重归一化，边缘复制；sigma ≤ 0 时不做模糊。
- 时间噪声：`sigma_T(T) = NETD_300·(dB/dT|300 K)/(dB/dT|T)`，T 为无噪表观温度；每像素每帧一个标准正态，
  键 `key(run_seed, 22, uav_no, frame_index, therm_temporal + sensor_no)`，像素计数器为行主序下标。
- 固定图案噪声（非均匀性校正残差）：每像素偏置 `o ~ N(0, sigma_off^2)` 与增益 `g ~ N(0, sigma_gain^2)`，键
  `key(run_seed, 22, uav_no, 0, therm_fpn + sensor_no)`，计数器为 `pix·2 + c`（c = 0 偏置、1 增益），运行内恒定、换种子改变。
  本实现设定（M25 未规定处）：增益残差作用于相对校正点 `T_NUC = 300 K` 的温差，`T' = T + g·(T − T_NUC) + o`
  （两点校正后的增益残差在校正点处为 0，随场景偏离校正点线性增长）。
- 量化：`temp_ck = floor(T·100 + 0.5)`，饱和于 [0, 65535]（与 `camera.effects._srgb_q` 同一舍入约定，GLSL 可逐值复现）。
- 标准正态与相机像素噪声同一派生（`camera.effects.pixel_normals`：24 位 u1、u2，float32 Box–Muller），GPU 着色器可逐位复现。

全部随机量只取决于 (run_seed, uav_no, sensor_no, frame_index, 像素)：计数器式，无状态，任一帧可单独重算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from awr.sensorsvc.camera.effects import CHANNELS_V2, STREAM_M25, pixel_normals
from awr.sensorsvc.thermal.radiometry import PlanckTable

__all__ = ["CK_PER_K", "PROFILES", "STREAM_M25", "THERMAL_NOISE_VERSION", "THERM_CHANNELS", "T_NUC_K", "T_REF_NETD_K",
           "ThermalNoiseProfile", "apply_noise", "blend_subframes", "check_channels", "decode_ck", "detector", "fpn_maps",
           "netd_sigma_k", "optics_blur", "profile", "quantize_ck", "subframe_weights", "temporal_normals"]

THERMAL_NOISE_VERSION = "therm-noise@1"
# 流 22 热成像通道（M25 §15.6 随机流；与 `camera.effects.CHANNELS_V2` 的区间互不重叠，登记表合并见 M25 §6.17.2）
THERM_CHANNELS: dict[str, tuple[int, int]] = {"therm_temporal": (0x1500, 0x15FF), "therm_fpn": (0x1600, 0x16FF)}
T_REF_NETD_K = 300.0                          # NETD 标称场景温度
T_NUC_K = 300.0                               # 非均匀性校正点（增益残差在此为 0）
CK_PER_K = 100                                # u16 量化：0.01 K/LSB


@dataclass(frozen=True)
class ThermalNoiseProfile:
    """热成像噪声档（数值以 AWR-20 §15.6.5 为单一真源）。"""

    name: str
    netd_mk_300k: float                       # 时间噪声 NETD（300 K 场景，F1.0）
    fpn_offset_k: float                       # 固定图案噪声偏置 sigma
    fpn_gain_frac: float                      # 固定图案噪声增益 sigma（相对值）
    optics_sigma_px: float                    # 光学模糊高斯 sigma
    tau_bolometer_s: float                    # 微测辐射热计时间常数


PROFILES: dict[str, ThermalNoiseProfile] = {
    "clean": ThermalNoiseProfile("clean", 0.0, 0.0, 0.0, 0.6, 0.010),
    "realistic": ThermalNoiseProfile("realistic", 50.0, 0.10, 0.005, 0.6, 0.010),
    "harsh": ThermalNoiseProfile("harsh", 80.0, 0.25, 0.010, 0.8, 0.012),
}


def profile(name: str) -> ThermalNoiseProfile:
    if name not in PROFILES:
        raise ValueError(f"unknown thermal noise profile {name!r} (clean, realistic, harsh)")
    return PROFILES[name]


def _check_ids(uav_no: int, sensor_no: int) -> None:
    span = THERM_CHANNELS["therm_temporal"][1] - THERM_CHANNELS["therm_temporal"][0]
    if not 0 <= sensor_no <= span or uav_no < 0:
        raise ValueError(f"uav_no {uav_no} / sensor_no {sensor_no} out of range (sensor_no 0..{span})")


def netd_sigma_k(table: PlanckTable, t_k, netd_mk_300k: float) -> np.ndarray:
    """时间噪声标准差（K，float32）：`NETD_300·(dB/dT|300 K)/(dB/dT|T)`。"""
    ref = float(table.dbdt(T_REF_NETD_K))
    return (np.float32(netd_mk_300k * 1e-3 * ref) / table.dbdt(t_k)).astype(np.float32)


def temporal_normals(seed: int, uav_no: int, sensor_no: int, frame_index: int, h: int, w: int) -> np.ndarray:
    """(h, w) float32 标准正态，通道 therm_temporal；像素计数器为行主序下标。"""
    _check_ids(uav_no, sensor_no)
    ch = THERM_CHANNELS["therm_temporal"][0] + sensor_no
    return pixel_normals(seed, uav_no, frame_index, ch, h * w, 1).reshape(h, w)


def fpn_maps(seed: int, uav_no: int, sensor_no: int, h: int, w: int, prof: ThermalNoiseProfile) -> tuple[np.ndarray, np.ndarray]:
    """固定图案噪声 (偏置 K, 增益残差)，各 (h, w) float32；只取决于 (run_seed, uav_no, sensor_no)，运行内恒定。"""
    _check_ids(uav_no, sensor_no)
    ch = THERM_CHANNELS["therm_fpn"][0] + sensor_no
    n = pixel_normals(seed, uav_no, 0, ch, h * w, 2)
    off = (np.float32(prof.fpn_offset_k) * n[:, 0]).reshape(h, w)
    gain = (np.float32(prof.fpn_gain_frac) * n[:, 1]).reshape(h, w)
    return off.astype(np.float32), gain.astype(np.float32)


def subframe_weights(t_sub_s, t_end_s: float, tau_b_s: float) -> np.ndarray:
    """子帧权重 `w_j ∝ exp(−(t_end − t_j)/τ_b)`，归一化（float64）；τ_b ≤ 0 时只取最后一个子帧。"""
    t = np.asarray(t_sub_s, np.float64)
    if t.ndim != 1 or t.size == 0:
        raise ValueError("t_sub_s must be a non-empty 1-D sequence")
    if np.any(t > t_end_s + 1e-12):
        raise ValueError("sub-frame instants must not exceed t_end")
    if tau_b_s <= 0:
        w = np.zeros_like(t)
        w[int(np.argmax(t))] = 1.0
        return w
    w = np.exp(-(t_end_s - t) / tau_b_s)
    return w / w.sum()


def blend_subframes(l_sub: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """(S, H, W) 子帧入瞳辐亮度按权重求和 → (H, W) float32。"""
    ls = np.asarray(l_sub, np.float32)
    wv = np.asarray(weights, np.float64)
    if ls.ndim != 3 or ls.shape[0] != wv.size:
        raise ValueError(f"sub-frame stack {ls.shape} does not match {wv.size} weights")
    return np.tensordot(wv.astype(np.float32), ls, axes=1).astype(np.float32)


def _gauss_kernel(sigma_px: float) -> np.ndarray:
    r = max(1, math.ceil(3.0 * sigma_px))
    x = np.arange(-r, r + 1, dtype=np.float64)
    k = np.exp(-0.5 * (x / sigma_px) ** 2)
    return (k / k.sum()).astype(np.float32)


def optics_blur(img: np.ndarray, sigma_px: float) -> np.ndarray:
    """可分离高斯光学模糊（float32，边缘复制）；sigma ≤ 0 时原样返回副本。"""
    a = np.asarray(img, np.float32)
    if sigma_px <= 0:
        return a.copy()
    k = _gauss_kernel(sigma_px)
    r = (k.size - 1) // 2
    p = np.pad(a, ((0, 0), (r, r)), mode="edge")
    tmp = np.zeros_like(a)
    for j, kj in enumerate(k):
        tmp += kj * p[:, j:j + a.shape[1]]
    p = np.pad(tmp, ((r, r), (0, 0)), mode="edge")
    out = np.zeros_like(a)
    for j, kj in enumerate(k):
        out += kj * p[j:j + a.shape[0], :]
    return out


def apply_noise(t_app_k: np.ndarray, table: PlanckTable, prof: ThermalNoiseProfile, *, seed: int, uav_no: int, sensor_no: int,
                frame_index: int, fpn: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
    """无噪表观温度 (H, W) → 含固定图案噪声与时间噪声的表观温度（float32）。

    `fpn` 可传入运行开始时预先求得的 `fpn_maps`（每运行每传感器一次）；缺省时就地按键重算，结果相同。
    """
    t = np.asarray(t_app_k, np.float32)
    h, w = t.shape
    out = t.copy()
    if prof.fpn_offset_k > 0 or prof.fpn_gain_frac > 0:
        off, gain = fpn if fpn is not None else fpn_maps(seed, uav_no, sensor_no, h, w, prof)
        out = out + gain * (t - np.float32(T_NUC_K)) + off
    if prof.netd_mk_300k > 0:
        out = out + netd_sigma_k(table, t, prof.netd_mk_300k) * temporal_normals(seed, uav_no, sensor_no, frame_index, h, w)
    return out.astype(np.float32)


def quantize_ck(t_k: np.ndarray) -> np.ndarray:
    """表观温度（K）→ u16 厘开尔文：`floor(T·100 + 0.5)`，饱和于 [0, 65535]。"""
    x = np.floor(np.asarray(t_k, np.float64) * CK_PER_K + 0.5)
    return np.clip(x, 0, 65535).astype(np.uint16)


def decode_ck(temp_ck: np.ndarray) -> np.ndarray:
    """u16 厘开尔文 → 表观温度（K，float32）。"""
    return (np.asarray(temp_ck, np.uint16).astype(np.float32) / np.float32(CK_PER_K)).astype(np.float32)


def detector(l_sub: np.ndarray, t_sub_s, t_end_s: float, table: PlanckTable, prof: ThermalNoiseProfile, *, seed: int, uav_no: int,
             sensor_no: int, frame_index: int, fpn: tuple[np.ndarray, np.ndarray] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """探测器参考实现：(S, H, W) 子帧入瞳辐亮度 → (表观温度 float32, temp_ck u16)。"""
    ls = np.asarray(l_sub, np.float32)
    if ls.ndim == 2:
        ls = ls[None]
    lv = blend_subframes(ls, subframe_weights(t_sub_s, t_end_s, prof.tau_bolometer_s))
    lv = optics_blur(lv, prof.optics_sigma_px)
    t_app = apply_noise(table.temperature(lv), table, prof, seed=seed, uav_no=uav_no, sensor_no=sensor_no, frame_index=frame_index,
                        fpn=fpn)
    return t_app, quantize_ck(t_app)


def check_channels() -> list[str]:
    """热成像通道与相机通道表区间互不重叠；返回冲突描述（空为通过）。"""
    bad = []
    for name, (lo, hi) in THERM_CHANNELS.items():
        for other, (olo, ohi) in CHANNELS_V2.items():
            if not (hi < olo or ohi < lo):
                bad.append(f"{name} {lo:#x}-{hi:#x} overlaps {other} {olo:#x}-{ohi:#x}")
    return bad
