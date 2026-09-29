"""MID-360 参数化扫描花样（M13-FR-050；M13 §6.5.10；r04 §3.1.3；D1 桩）。

r04 参考公式（第 i 点，k = i & 3 为光束号，n = i >> 2 为束样本号）：
`el = EC(n) + S_EL[k]·cos(θ + 1.7°)`，`az = 270.08 + AZSTEP·n + S_AZ[k]·sin θ / cos EC(n)`（mod 360），
`EC(n) = C0 + Σ_{j=1..6} Cj·cos(2π j n / 5000)`，`θ = 2π n / 274.1`。

表驱动实现：载波周期正好是一帧（5000 束样本 = 0.1 s），EC 与 1/cos EC 每帧相同，预计算一次；每帧只用 float64 约化
扇相位 θ0 = 2π·(n0 mod ROT)/ROT 与方位起点 az0，再用和角公式展开（TS `mid360Pattern.ts` 与 TSL 移植必须沿用
"每帧 float64 约化，着色器只算帧内偏移"；未约化的 f32 俯仰误差 p99 为 5.4°）。与参考公式逐点误差 ≤ 1e-6°。
只依赖 numpy。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["AZSTEP", "NPF", "ROT", "S_AZ", "S_EL", "C", "mid360_pattern", "mid360_reference", "offset_time_ns"]

C = np.array([20.4535, 23.5232, 1.1247, 2.3002, 0.2232, 0.1565, 0.0330])
S_EL = np.array([2.765, 0.921, -0.921, -2.765])
S_AZ = np.array([1.248, 0.617, -0.617, -1.248])
ROT = 274.1
AZSTEP = -1.31341
CARRIER = 5000
NPF = 20000  # 每帧点数（200 kHz × 0.1 s）
AZ0 = 270.08
FAN_PHASE_DEG = 1.7
POINT_DT_NS = 5000

_i = np.arange(NPF)
_K = _i & 3
_N = _i >> 2
_PH = 2.0 * np.pi * _N / CARRIER
EC = C[0] + sum(C[j] * np.cos(j * _PH) for j in range(1, 7))
SEC = 1.0 / np.cos(np.radians(EC))
_DTH = 2.0 * np.pi * _N / ROT
_CD = np.cos(_DTH + math.radians(FAN_PHASE_DEG))
_SD = np.sin(_DTH + math.radians(FAN_PHASE_DEG))
_CD0 = np.cos(_DTH)
_SD0 = np.sin(_DTH)
_SEL = S_EL[_K]
_SAZ_SEC = S_AZ[_K] * SEC
_AZL = AZSTEP * _N
for _a in (EC, SEC, _CD, _SD, _CD0, _SD0, _SEL, _SAZ_SEC, _AZL):
    _a.flags.writeable = False


def mid360_pattern(frame_idx: int, out_az_deg: np.ndarray, out_el_deg: np.ndarray) -> None:
    """第 frame_idx 帧的 20 000 条射线方向（度，雷达系 FLU：方位从 +X 逆时针，俯仰向上为正），写入预分配的 f64 数组。"""
    n0 = int(frame_idx) * CARRIER
    th0 = 2.0 * math.pi * ((n0 % ROT) / ROT)
    c0, s0 = math.cos(th0), math.sin(th0)
    az0 = (AZ0 + (AZSTEP * n0) % 360.0) % 360.0
    np.multiply(_SEL, _CD * c0 - _SD * s0, out=out_el_deg)
    np.add(out_el_deg, EC, out=out_el_deg)
    np.multiply(_SAZ_SEC, _SD0 * c0 + _CD0 * s0, out=out_az_deg)
    np.add(out_az_deg, _AZL, out=out_az_deg)
    np.add(out_az_deg, az0, out=out_az_deg)
    np.remainder(out_az_deg, 360.0, out=out_az_deg)


def mid360_reference(frame_idx: int) -> tuple[np.ndarray, np.ndarray]:
    """r04 §3.1.3 参考公式（逐点，无约化；只用于 golden 与测试）。"""
    n = int(frame_idx) * CARRIER + _N
    ph = 2.0 * np.pi * (n % CARRIER) / CARRIER
    ec = C[0] + sum(C[j] * np.cos(j * ph) for j in range(1, 7))
    th = 2.0 * np.pi * (n % ROT) / ROT
    el = ec + S_EL[_K] * np.cos(th + math.radians(FAN_PHASE_DEG))
    az = (AZ0 + (AZSTEP * n) % 360.0 + S_AZ[_K] * np.sin(th) / np.cos(np.radians(ec))) % 360.0
    return az, el


def offset_time_ns() -> np.ndarray:
    """帧内逐点时间偏移（ns）：i·5000。"""
    return (_i * POINT_DT_NS).astype(np.uint32)


def directions_flu(az_deg: np.ndarray, el_deg: np.ndarray) -> np.ndarray:
    a, e = np.radians(az_deg), np.radians(el_deg)
    ce = np.cos(e)
    return np.stack([ce * np.cos(a), ce * np.sin(a), np.sin(e)], axis=1)
