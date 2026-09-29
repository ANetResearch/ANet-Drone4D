"""天气图与噪声的确定性烘焙（M07 §7.4、§9.1；16 §8.4；r16 §3.4.1）。

天气图 `weather_s{seed}_512.awrv`：AWRV kind 6（noise_field），512^2、nz = 1、RGBA8、可平铺（周期 = 地图边长，
`config.weather_map.scale_m` = 24 km）。R = 覆盖度 fbm（整数哈希值噪声，4 个八度），G = 云单体结构（Worley fbm，2 个八度），
B = 细节值噪声（1 个八度），A = 255。只用 uint32 整数哈希（不用 sin 哈希），两端与任何平台逐字节一致。
`field_version` 参数串 `"weather|seed=<seed>|n=<n>|v=1"`。
"""

from __future__ import annotations

import numpy as np

__all__ = ["WEATHER_VERSION", "hash32_arr", "weather_map", "weather_params"]

WEATHER_VERSION = 1
_M = np.uint32(0xFFFFFFFF)


def hash32_arr(x: np.ndarray, y: np.ndarray, salt: int) -> np.ndarray:
    """uint32 格点哈希（murmur3 finalizer 混合）。"""
    with np.errstate(over="ignore"):
        h = (x.astype(np.uint32) * np.uint32(0x8DA6B343)) ^ (y.astype(np.uint32) * np.uint32(0xD8163841)) ^ np.uint32(salt & 0xFFFFFFFF)
        h ^= h >> np.uint32(16)
        h *= np.uint32(0x85EBCA6B)
        h ^= h >> np.uint32(13)
        h *= np.uint32(0xC2B2AE35)
        h ^= h >> np.uint32(16)
    return h


def _u01(h: np.ndarray) -> np.ndarray:
    return (h >> np.uint32(8)).astype(np.float64) / float(1 << 24)


def _value_noise(n: int, cells: int, salt: int) -> np.ndarray:
    """可平铺值噪声：cells × cells 格点，五次平滑插值 -> (n, n) ∈ [0, 1)。"""
    t = (np.arange(n) + 0.5) * cells / n
    i0 = np.floor(t).astype(np.int64)
    f = t - i0
    f = f * f * f * (f * (f * 6.0 - 15.0) + 10.0)
    i1 = (i0 + 1) % cells
    i0 %= cells
    X0, Y0 = np.meshgrid(i0, i0, indexing="xy")
    X1, Y1 = np.meshgrid(i1, i1, indexing="xy")
    FX, FY = np.meshgrid(f, f, indexing="xy")
    v00 = _u01(hash32_arr(X0, Y0, salt))
    v10 = _u01(hash32_arr(X1, Y0, salt))
    v01 = _u01(hash32_arr(X0, Y1, salt))
    v11 = _u01(hash32_arr(X1, Y1, salt))
    a = v00 + (v10 - v00) * FX
    b = v01 + (v11 - v01) * FX
    return a + (b - a) * FY


def _worley(n: int, cells: int, salt: int) -> np.ndarray:
    """可平铺 Worley F1（每格一个特征点）-> (n, n) ∈ [0, 1]，1 为特征点处。"""
    t = (np.arange(n) + 0.5) * cells / n
    X, Y = np.meshgrid(t, t, indexing="xy")
    cx, cy = np.floor(X).astype(np.int64), np.floor(Y).astype(np.int64)
    best = np.full(X.shape, 9.0)
    for oy in (-1, 0, 1):
        for ox in (-1, 0, 1):
            gx, gy = cx + ox, cy + oy
            hx, hy = gx % cells, gy % cells
            px = gx + _u01(hash32_arr(hx, hy, salt))
            py = gy + _u01(hash32_arr(hx, hy, salt ^ 0x5BD1E995))
            best = np.minimum(best, np.hypot(X - px, Y - py))
    return 1.0 - np.clip(best, 0.0, 1.0)


def weather_params(seed: int, n: int = 512) -> str:
    return f"weather|seed={int(seed)}|n={int(n)}|v={WEATHER_VERSION}"


def weather_map(seed: int, n: int = 512) -> np.ndarray:
    """-> (1, n, n, 4) uint8（AWRV layout 0：z, y, x, comp）。"""
    s = int(seed) & 0xFFFFFFFF
    cov = np.zeros((n, n))
    amp, tot = 1.0, 0.0
    for o in range(4):
        cov += amp * _value_noise(n, 4 << o, s ^ (0x9E3779B9 * (o + 1) & 0xFFFFFFFF))
        tot += amp
        amp *= 0.5
    cov /= tot
    lo, hi = np.percentile(cov, 1.0), np.percentile(cov, 99.0)
    cov = np.clip((cov - lo) / max(hi - lo, 1e-9), 0.0, 1.0)
    cell = 0.65 * _worley(n, 12, s ^ 0x27D4EB2F) + 0.35 * _worley(n, 24, s ^ 0x165667B1)
    det = _value_noise(n, 64, s ^ 0x7FEB352D)
    out = np.empty((1, n, n, 4), np.uint8)
    out[0, ..., 0] = np.round(cov * 255.0).astype(np.uint8)
    out[0, ..., 1] = np.round(np.clip(cell, 0.0, 1.0) * 255.0).astype(np.uint8)
    out[0, ..., 2] = np.round(det * 255.0).astype(np.uint8)
    out[0, ..., 3] = 255
    return out
