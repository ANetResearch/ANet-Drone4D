"""IMU 合成的 float64 参考实现（M25 §6.9.1 一致性判据；AWR-05 §5.4.6 合同测试；D3-AC-13）。

与 `awr.sensorsvc.imu.synth_imu` 同一算法、独立书写（逐物理步显式循环，不调用 scipy 与 numpy 插值）：
1. 逐物理步 IMU 帧真值：ω̇ 为后向差分（首步取 0），f_imu = R_biᵀ (f_b + ω̇ × r + ω × (ω × r))，w_imu = R_biᵀ ω；
2. 一阶抗混叠 y[n] = y[n−1] + a·(x[n] − y[n−1])，a = 1 − exp(−2π f_c Δt)，y[0] = x[0]；
3. 线性插值到 200 Hz 绝对网格 t_j = j·5 ms（取包围 t_j 的两个物理步）。
`unfiltered` 跳过第 2 步，二者之差即"抗混叠理论误差"（另行报告，不作判据）。

`strapdown` 为 clean 档 5 s 纯惯导回代（C7）：姿态按相邻样本角速度均值的旋转向量更新，速度与位置按梯形积分，世界系 ENU、重力 −g·e_z。
"""

from __future__ import annotations

import bisect
import math

import numpy as np

G = 9.80665


def _imu_frame_truth(t_ns, omega_b, f_b, R_bi, r_imu_b) -> tuple[list[np.ndarray], list[np.ndarray]]:
    R = np.asarray(R_bi, np.float64)
    r = np.asarray(r_imu_b, np.float64)
    w_all = np.asarray(omega_b, np.float64)
    f_all = np.asarray(f_b, np.float64)
    gyro, acc = [], []
    for n in range(len(t_ns)):
        w = w_all[n]
        wdot = np.zeros(3) if n == 0 else (w_all[n] - w_all[n - 1]) / ((int(t_ns[n]) - int(t_ns[n - 1])) * 1e-9)
        f_lever = np.cross(wdot, r) + np.cross(w, np.cross(w, r))
        acc.append(R.T @ (f_all[n] + f_lever))
        gyro.append(R.T @ w)
    return gyro, acc


def _first_order(x: list[np.ndarray], a: float) -> list[np.ndarray]:
    y = [x[0].copy()]
    for n in range(1, len(x)):
        y.append(y[n - 1] + a * (x[n] - y[n - 1]))
    return y


def _grid(t_ns, t_from_ns: int, t_to_ns: int, rate_hz: int) -> list[int]:
    step = 1_000_000_000 // rate_hz
    j = -(-int(t_from_ns) // step)
    out = []
    while j * step < int(t_to_ns):
        if int(t_ns[0]) <= j * step <= int(t_ns[-1]):
            out.append(j * step)
        j += 1
    return out


def _interp(t: list[int], y: list[np.ndarray], tj: int) -> np.ndarray:
    k = bisect.bisect_right(t, tj) - 1
    if k >= len(t) - 1:
        return y[-1].copy()
    u = (tj - t[k]) / (t[k + 1] - t[k])
    return y[k] + u * (y[k + 1] - y[k])


def reference_imu(t_ns, omega_b, f_b, *, R_bi, r_imu_b, t_from_ns: int, t_to_ns: int, rate_hz: int = 200, aa_cutoff_hz: float = 80.0,
                  unfiltered: bool = False) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (t_ns (n,), gyro (n, 3) rad/s, acc (n, 3) m/s²)；物理步须等间隔（gz 与 Fast 档的固定步长）。"""
    t = [int(v) for v in t_ns]
    dts = {t[n] - t[n - 1] for n in range(1, len(t))}
    if len(dts) != 1:
        raise ValueError("reference implementation expects a fixed physics step")
    dt = dts.pop() * 1e-9
    gyro, acc = _imu_frame_truth(t, omega_b, f_b, R_bi, r_imu_b)
    if not unfiltered:
        a = 1.0 - math.exp(-2.0 * math.pi * aa_cutoff_hz * dt)
        gyro, acc = _first_order(gyro, a), _first_order(acc, a)
    tj = _grid(t, t_from_ns, t_to_ns, rate_hz)
    G_ = np.array([_interp(t, gyro, x) for x in tj]).reshape(-1, 3)
    A_ = np.array([_interp(t, acc, x) for x in tj]).reshape(-1, 3)
    return np.array(tj, np.int64), G_, A_


def _exp_so3(v: np.ndarray) -> np.ndarray:
    th = float(np.linalg.norm(v))
    K = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
    if th < 1e-12:
        return np.eye(3) + K
    return np.eye(3) + math.sin(th) / th * K + (1.0 - math.cos(th)) / th**2 * (K @ K)


def strapdown(t_ns, gyro_rps, acc_mps2, *, R0: np.ndarray, v0: np.ndarray, p0: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """IMU 帧读数 → IMU 原点的世界位置、速度与姿态序列（与 `t_ns` 对齐）。"""
    t = np.asarray(t_ns, np.int64)
    w = np.asarray(gyro_rps, np.float64)
    f = np.asarray(acc_mps2, np.float64)
    g = np.array([0.0, 0.0, G])
    R, v, p = np.asarray(R0, np.float64).copy(), np.asarray(v0, np.float64).copy(), np.asarray(p0, np.float64).copy()
    Rs, vs, ps = [R.copy()], [v.copy()], [p.copy()]
    for k in range(len(t) - 1):
        dt = (int(t[k + 1]) - int(t[k])) * 1e-9
        a0 = R @ f[k] - g
        R1 = R @ _exp_so3(0.5 * (w[k] + w[k + 1]) * dt)
        a1 = R1 @ f[k + 1] - g
        v1 = v + 0.5 * (a0 + a1) * dt
        p = p + v * dt + (2.0 * a0 + a1) * dt * dt / 6.0
        R, v = R1, v1
        Rs.append(R.copy())
        vs.append(v.copy())
        ps.append(p.copy())
    return np.array(ps), np.array(vs), np.array(Rs)
