"""计数器 RNG（M13-FR-030；M13 §6.5.4；ADR-049 补充条款）。

键按（seed、流、agent_no、tick、通道）逐级 SplitMix64 派生：`k = sm(seed); k = sm(k ^ stream); k = sm(k ^ agent);
k = sm(k ^ tick); k = sm(k ^ ch)`。每级对下一个分量都是双射，分量互换不会碰撞；结果只取决于键，与抽样子集、兴趣集、
观看者无关（规则 ①）。正态用 Box–Muller（u1 取 (k >> 11 + 0.5)/2^53，u2 取 sm(k ^ C3) >> 11），均匀为 [0, 1)。

通道登记表 `CHANNELS`（规则 ③）：同一流内的区间互不重叠，`check_channels()` 在测试与 CI 中校验。
只依赖 numpy。
"""

from __future__ import annotations

import itertools

import numpy as np

__all__ = ["CHANNELS", "check_channels", "key", "normal", "normal_elem", "sm", "u64", "uniform", "uniform_elem"]

U = np.uint64
C1, C2, C3 = U(0x9E3779B97F4A7C15), U(0xBF58476D1CE4E5B9), U(0x94D049BB133111EB)
INV53 = 1.0 / 9007199254740992.0
MASK64 = (1 << 64) - 1

# (流, 名称) -> [lo, hi]（闭区间）；流 3 = sensor_noise，流 4 = detector（rt/rng_streams.json）
CHANNELS: dict[tuple[int, str], tuple[int, int]] = {
    (3, "gnss_white"): (0, 2),
    (3, "imu_white"): (3, 8),
    (3, "imu_turn_on"): (9, 14),
    (3, "gnss_z0"): (15, 17),
    (3, "imu_zb0"): (18, 23),
    (3, "baro"): (24, 25),
    (3, "battery_meas"): (26, 31),
    (4, "detect_draw"): (256, 511),        # 256 + idx·2 + d（idx < 64 为目标表行号；d = 0 相机、1 热成像）
    (4, "detect_extra"): (1024, 1535),     # 1024 + idx·8 + j（首检 conf、位置误差 3、热成像帧种子）
    (4, "thermal_pixel"): (1 << 20, (1 << 20) + 160 * 120 - 1),  # 热成像帧 NETD 噪声（以像素下标为通道，流取 4）
}


def check_channels() -> list[str]:
    """同一流内通道区间两两不重叠；返回冲突描述（空为通过）。"""
    bad = []
    items = sorted(CHANNELS.items(), key=lambda kv: (kv[0][0], kv[1][0]))
    for (a, ra), (b, rb) in itertools.pairwise(items):
        if a[0] == b[0] and rb[0] <= ra[1]:
            bad.append(f"stream {a[0]}: {a[1]} {ra} overlaps {b[1]} {rb}")
    return bad


def u64(x) -> np.ndarray:
    """任意整数（含负数、> 2^63 的 Python int）按 2^64 取模转 uint64。"""
    if isinstance(x, (int, np.integer)):
        return U(int(x) & MASK64)
    a = np.asarray(x)
    if a.dtype == np.uint64:
        return a
    return (a.astype(np.int64)).astype(np.uint64)


def sm(z):
    """SplitMix64 终混（uint64 回绕，双射）。"""
    with np.errstate(over="ignore"):
        z = z + C1
        z = (z ^ (z >> U(30))) * C2
        z = (z ^ (z >> U(27))) * C3
        return z ^ (z >> U(31))


def key(seed, stream, agent, tick, ch):
    """逐级派生键；各参数可为标量或可广播的数组（agent、ch 常为 (n, 1) 与 (1, m)）。"""
    k = sm(u64(seed))
    k = sm(k ^ u64(stream))
    k = sm(k ^ u64(agent))
    k = sm(k ^ u64(tick))
    return sm(k ^ u64(ch))


def normal(seed, stream, agent, tick, ch) -> np.ndarray:
    """N(0, 1)；agent (n,)、ch (m,) 时返回 (n, m)（二者都为标量时返回 0 维数组）。"""
    a = np.asarray(agent)
    c = np.asarray(ch)
    if a.ndim == 1 and c.ndim == 1:
        a, c = a[:, None], c[None, :]
    k = key(seed, stream, a, tick, c)
    b = sm(k ^ C3)
    u1 = ((k >> U(11)).astype(np.float64) + 0.5) * INV53
    u2 = (b >> U(11)).astype(np.float64) * INV53
    return np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)


def uniform(seed, stream, agent, tick, ch) -> np.ndarray:
    """U[0, 1)；广播规则同 `normal`。"""
    a = np.asarray(agent)
    c = np.asarray(ch)
    if a.ndim == 1 and c.ndim == 1:
        a, c = a[:, None], c[None, :]
    return (key(seed, stream, a, tick, c) >> U(11)).astype(np.float64) * INV53


def normal_elem(seed, stream, agent, tick, ch) -> np.ndarray:
    """逐元素版（不做外积）：agent、tick、ch 按 numpy 规则广播。"""
    k = key(seed, stream, np.asarray(agent), np.asarray(tick), np.asarray(ch))
    b = sm(k ^ C3)
    u1 = ((k >> U(11)).astype(np.float64) + 0.5) * INV53
    u2 = (b >> U(11)).astype(np.float64) * INV53
    return np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)


def uniform_elem(seed, stream, agent, tick, ch) -> np.ndarray:
    """逐元素版 U[0, 1)。"""
    return (key(seed, stream, np.asarray(agent), np.asarray(tick), np.asarray(ch)) >> U(11)).astype(np.float64) * INV53
