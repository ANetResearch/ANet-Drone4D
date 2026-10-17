"""相机效果链与 ISP 的参考实现（M25 §6.5；AWR-20 §15.6.5 噪声档；P0a-1 基础档：无雾雨、无运动模糊、无畸变）。

顺序（M25 §6.5.1 去掉尚未落地的项）：曝光增益（固定或自动曝光）→ 渐晕 → Poisson–Gaussian 噪声 → sRGB 编码 → 8 位量化。
L2-depth 深度传感器模型（§6.5.6）与云台编码器噪声（§15.6.5）同在本模块。全部计算为 numpy float32，随机数只取决于
（run_seed、uav_no、帧序号、像素、通道），按 `awr.sim.sensors.cbrng` 逐级派生（M25 §6.17.2，流 22，通道表 `CHANNELS_V2`）；
GPU 着色器版本以本模块为参照做一致性测试。

本实现设定（M25 未规定处）：
- 逐像素正态：k = sm(K_frame ^ (pix·m + c))，b = sm(k ^ C3)；u1 = ((k >> 40) + 0.5)·2⁻²⁴，u2 = (b >> 40)·2⁻²⁴（float32 可精确表示，
  便于 GLSL 复现），n = sqrt(−2 ln u1)·cos(2π u2)。pix 为交付图像行主序下标（第 0 行在上），RGB 的 m = 3、深度的 m = 1。
- 自动曝光测光：1/8 降采样（取每 8 × 8 块左上像素），中心 50% 区域（各边 25% 内缩）权重 2，log2 Y 在 [−12, 0] 等分 64 档，
  m = 2^(加权平均档中心)。测光对象为施加基础增益、未施加曝光误差的图像，使误差保持为显式的 ±ae_error_amp 调制而不被反馈抵消。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

from awr.sensorsvc.camera.model import CameraModel, IspConfig, NoiseConfig
from awr.sim.sensors.cbrng import C3, key, sm, uniform

__all__ = ["CHANNELS_V2", "FX_VERSION", "NOISE_TABLE_VERSION", "PROFILES", "STREAM_M25", "AeState", "DepthSensorConfig", "FxParams",
           "NoiseProfile", "apply_fx_reference", "apply_rgb", "depth_sensor", "finish_frame", "frame_params", "gimbal_reading", "meter",
           "meter_sub", "pixel_normals", "profile", "with_profile"]

FX_VERSION = "fx-basic@1"
NOISE_TABLE_VERSION = "d3-noise-1-draft"
STREAM_M25 = 22
# M25 §6.17.2 通道表（流 22）：基址 + sensor_no；区间互不重叠
CHANNELS_V2: dict[str, tuple[int, int]] = {
    "cam_noise": (0x1000, 0x10FF), "cam_ae": (0x1100, 0x11FF), "cam_rain": (0x1200, 0x12FF), "cam_gimbal": (0x1300, 0x13FF),
    "depth_noise": (0x1400, 0x14FF), "lidar": (0x2000, 0x20FF), "imu": (0x3000, 0x37FF), "gnss": (0x3800, 0x3FFF), "nav": (0x4000, 0x40FF),
}
_U = np.uint64
_INV24 = np.float32(1.0 / 16777216.0)
AE_BINS = 64
AE_LOG2_RANGE = (-12.0, 0.0)


@dataclass(frozen=True)
class DepthSensorConfig:
    a: float = 0.0                        # sigma(d) = a·(0.005·d + 0.0005·d²)
    d_min_m: float = 0.3
    d_max_m: float = 200.0


@dataclass(frozen=True)
class NoiseProfile:
    name: str
    isp: IspConfig
    noise: NoiseConfig
    depth: DepthSensorConfig
    gimbal_sigma_deg: float


PROFILES: dict[str, NoiseProfile] = {
    "clean": NoiseProfile("clean", IspConfig(ae="fixed", ev=0.0), NoiseConfig(0.0, 0.0), DepthSensorConfig(0.0, 0.3, 200.0), 0.0),
    "realistic": NoiseProfile("realistic", IspConfig(ae="auto", ae_error_amp=0.2), NoiseConfig(2.0, 0.2), DepthSensorConfig(1.0, 0.3, 60.0), 0.05),
    "harsh": NoiseProfile("harsh", IspConfig(ae="auto", ae_error_amp=0.4), NoiseConfig(4.0, 0.4), DepthSensorConfig(2.0, 0.3, 40.0), 0.2),
}


def profile(name: str) -> NoiseProfile:
    if name not in PROFILES:
        raise ValueError(f"unknown noise profile {name!r} (clean, realistic, harsh)")
    return PROFILES[name]


def with_profile(cm: CameraModel, p: NoiseProfile) -> CameraModel:
    """把噪声档写入相机模型（保留调用方的 JPEG 质量与渐晕系数）。"""
    return replace(cm, isp=replace(p.isp, jpeg_quality=cm.isp.jpeg_quality, vignette_k=cm.isp.vignette_k), noise=p.noise)


@dataclass
class AeState:
    """每 (uav, sensor) 一份，按 seq 顺序推进（M25 §6.5.3）。"""
    gain_prev: float = 1.0
    meter_prev: float | None = None
    frames: int = 0
    history: list[float] = field(default_factory=list)


def _normals(K: np.uint64, idx: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore"):
        k = sm(_U(K) ^ idx)
        b = sm(k ^ C3)
    u1 = ((k >> _U(40)).astype(np.float32) + np.float32(0.5)) * _INV24
    u2 = (b >> _U(40)).astype(np.float32) * _INV24
    return np.sqrt(np.float32(-2.0) * np.log(u1)) * np.cos(np.float32(2.0 * math.pi) * u2)


def pixel_normals(seed: int, uav_no: int, frame_index: int, ch: int, n: int, m: int) -> np.ndarray:
    """(n, m) float32 标准正态；元素 (pix, c) 的计数器为 pix·m + c。"""
    K = key(seed, STREAM_M25, uav_no, frame_index, ch)
    return _normals(K, np.arange(n * m, dtype=np.uint64)).reshape(n, m)


def meter(lin: np.ndarray) -> float:
    """测光：1/8 降采样的中心加权 64 档 log2 直方图的加权平均（整数计数，确定）。"""
    return meter_sub(lin[::8, ::8])


def meter_sub(sub: np.ndarray) -> float:
    """`meter` 的直方图部分；`sub` 为已按 1/8 降采样的线性图像（GPU 路径读回的小图）。"""
    s = np.asarray(sub, np.float32)
    Y = 0.2126 * s[..., 0] + 0.7152 * s[..., 1] + 0.0722 * s[..., 2]
    lo, hi = AE_LOG2_RANGE
    b = np.clip(((np.log2(np.maximum(Y, 2.0 ** lo)) - lo) / (hi - lo) * AE_BINS).astype(np.int64), 0, AE_BINS - 1)
    h, w = Y.shape
    wgt = np.ones((h, w), np.int64)
    wgt[h // 4:h - h // 4, w // 4:w - w // 4] = 2
    hist = np.bincount(b.ravel(), weights=wgt.ravel(), minlength=AE_BINS).astype(np.int64)
    centre = lo + (np.arange(AE_BINS) + 0.5) * (hi - lo) / AE_BINS
    return float(2.0 ** (float((hist * centre).sum()) / max(int(hist.sum()), 1)))


def exposure_gain(state: AeState, isp: IspConfig, t_mid_s: float, *, seed: int, uav_no: int, sensor_no: int) -> tuple[float, float]:
    """返回 (基础增益 g, 含误差的增益 g·err)。"""
    if isp.ae == "fixed":
        g = 2.0 ** isp.ev
    else:
        m = state.meter_prev if state.meter_prev is not None else isp.ae_target
        g_prev = state.gain_prev if state.frames > 0 else 1.0
        g = float(np.clip(g_prev * (isp.ae_target / max(m, 1e-4)) ** isp.ae_alpha, *isp.ae_gain_range))
    phi = float(uniform(seed, STREAM_M25, uav_no, 0, CHANNELS_V2["cam_ae"][0] + sensor_no)) * 2.0 * math.pi
    err = 1.0 + isp.ae_error_amp * math.sin(2.0 * math.pi * t_mid_s / isp.ae_error_period_s + phi)
    return g, g * err


def _cos4(cm: CameraModel) -> np.ndarray:
    x = (np.arange(cm.width, dtype=np.float32) + np.float32(0.5) - np.float32(cm.cx)) / np.float32(cm.fx)
    y = (np.arange(cm.height, dtype=np.float32) + np.float32(0.5) - np.float32(cm.cy)) / np.float32(cm.fy)
    r2 = x[None, :] ** 2 + y[:, None] ** 2
    return (np.float32(1.0) / (np.float32(1.0) + r2)) ** 2          # cos⁴θ = 1 / (1 + r²)²


def _srgb_q(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, np.float32(0.0), np.float32(1.0))
    y = np.where(x <= np.float32(0.0031308), x * np.float32(12.92),
                 np.float32(1.055) * np.power(x, np.float32(1.0 / 2.4)) - np.float32(0.055))
    return np.floor(y * np.float32(255.0) + np.float32(0.5)).astype(np.uint8)


@dataclass(frozen=True)
class FxParams:
    """一帧效果链的全部参数（CPU 计算，GPU 着色器与参考实现共用）。"""
    gain: float                           # 含曝光误差的增益
    gain_base: float                      # 测光用的基础增益
    vignette_k: float
    read_sigma_dn: float
    shot_k_dn: float
    key_noise: int                        # 像素噪声帧键（uint64）
    depth: DepthSensorConfig | None       # None：不输出观测深度
    key_depth: int
    meter: bool                           # 自动曝光：本帧需要测光


def frame_params(cm: CameraModel, state: AeState, *, seed: int, uav_no: int, sensor_no: int, frame_index: int, t_mid_s: float,
                 depth: DepthSensorConfig | None = None) -> FxParams:
    """计算一帧的效果链参数并推进自动曝光状态（测光结果由 `finish_frame` 写回）。"""
    g, ge = exposure_gain(state, cm.isp, t_mid_s, seed=seed, uav_no=uav_no, sensor_no=sensor_no)
    state.gain_prev = g
    state.frames += 1
    state.history.append(ge)
    kn = int(key(seed, STREAM_M25, uav_no, frame_index, CHANNELS_V2["cam_noise"][0] + sensor_no))
    kd = int(key(seed, STREAM_M25, uav_no, frame_index, CHANNELS_V2["depth_noise"][0] + sensor_no))
    return FxParams(ge, g, cm.isp.vignette_k, cm.noise.read_sigma_dn, cm.noise.shot_k_dn, kn, depth, kd, cm.isp.ae == "auto")


def finish_frame(state: AeState, p: FxParams, sub: np.ndarray | None) -> None:
    """自动曝光：以本帧 1/8 降采样线性图像（未乘增益）乘基础增益测光，供下一帧使用。"""
    if p.meter and sub is not None:
        state.meter_prev = meter_sub(np.asarray(sub, np.float32) * np.float32(p.gain_base))


def _rgb_fx(lin: np.ndarray, cm: CameraModel, p: FxParams) -> np.ndarray:
    img = np.asarray(lin, np.float32) * np.float32(p.gain)
    if p.vignette_k:
        img = img * (np.float32(1.0) - np.float32(p.vignette_k) * (np.float32(1.0) - _cos4(cm)))[..., None]
    if p.read_sigma_dn > 0 or p.shot_k_dn > 0:
        h, w = img.shape[:2]
        x = np.float32(255.0) * np.clip(img, np.float32(0.0), np.float32(1.0))
        sigma = np.sqrt(np.float32(p.shot_k_dn) * x + np.float32(p.read_sigma_dn) ** 2)
        n = _normals(_U(p.key_noise), np.arange(h * w * 3, dtype=np.uint64)).reshape(h, w, 3)
        img = (x + sigma * n) / np.float32(255.0)
    return _srgb_q(img)


def _depth_fx(depth_m: np.ndarray, cfg: DepthSensorConfig, key_depth: int) -> np.ndarray:
    d = np.asarray(depth_m, np.float32)
    ok = (d >= np.float32(cfg.d_min_m)) & (d <= np.float32(cfg.d_max_m))
    out = np.where(ok, d, np.float32(0.0)).astype(np.float32)
    if cfg.a > 0:
        n = _normals(_U(key_depth), np.arange(d.size, dtype=np.uint64)).reshape(d.shape)
        sig = np.float32(cfg.a) * (np.float32(0.005) * d + np.float32(0.0005) * d * d)
        out = np.where(ok, np.maximum(out + sig * n, np.float32(cfg.d_min_m)), np.float32(0.0)).astype(np.float32)
    return out


def apply_fx_reference(lin: np.ndarray, depth_m: np.ndarray, cm: CameraModel, p: FxParams) -> tuple[np.ndarray, np.ndarray | None]:
    """参考实现：线性渲染与平面深度 → (交付 sRGB8, 观测深度或 None)；GPU 编码遍与之逐值一致（容差见一致性测试）。"""
    return _rgb_fx(lin, cm, p), (None if p.depth is None else _depth_fx(depth_m, p.depth, p.key_depth))


def apply_rgb(lin: np.ndarray, cm: CameraModel, state: AeState, *, seed: int, uav_no: int, sensor_no: int, frame_index: int,
              t_mid_s: float) -> np.ndarray:
    """线性渲染 (H, W, 3) → 交付 sRGB8；推进 `state`（自动曝光）。"""
    lin = np.asarray(lin, np.float32)
    p = frame_params(cm, state, seed=seed, uav_no=uav_no, sensor_no=sensor_no, frame_index=frame_index, t_mid_s=t_mid_s)
    finish_frame(state, p, lin[::8, ::8])
    return _rgb_fx(lin, cm, p)


def depth_sensor(depth_m: np.ndarray, cfg: DepthSensorConfig, *, seed: int, uav_no: int, sensor_no: int, frame_index: int) -> np.ndarray:
    """L2-depth（M25 §6.5.6）：越界、无几何为 0；其余加 sigma(d)·n，加噪后不低于 d_min（不产生负深度或 0 以外的越界值）。"""
    return _depth_fx(depth_m, cfg, int(key(seed, STREAM_M25, uav_no, frame_index, CHANNELS_V2["depth_noise"][0] + sensor_no)))


def gimbal_reading(g_true: tuple[float, float, float], sigma_deg: float, *, seed: int, uav_no: int, sensor_no: int,
                   frame_index: int) -> tuple[float, float, float]:
    """云台编码器读数：每帧每轴白噪声 sigma（度）。"""
    if sigma_deg <= 0:
        return tuple(float(v) for v in g_true)  # type: ignore[return-value]
    n = pixel_normals(seed, uav_no, frame_index, CHANNELS_V2["cam_gimbal"][0] + sensor_no, 3, 1).ravel()
    return tuple(float(v) + sigma_deg * float(e) for v, e in zip(g_true, n, strict=True))  # type: ignore[return-value]
