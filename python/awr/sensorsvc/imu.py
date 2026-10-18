"""IMU 合成（M25 §6.9.1、§6.9.2）：由位姿历史得到 IMU 帧比力与角速度，一阶抗混叠，线性插值到 200 Hz 绝对网格，加噪声。

- 物理档的 PoseHistory 直接给出 `omega_b` 与 `f_b`（步内常值）；脚本位姿（采集作业、P0a-1/2）没有这两列，
  `kinematics_from_poses` 以样本差分求得：速度与加速度为中心差分（端点单侧），角速度为相邻姿态的相对旋转向量 / Δt（后向，
  首样本取第 1 段），比力 f_b = R_wbᵀ (a + g·e_z)。
- 杆臂：f_imu = R_biᵀ (f_b + ω̇ × r + ω × (ω × r))，ω̇ 为后向差分；w_imu = R_biᵀ ω。
- 抗混叠：y[n] = y[n−1] + a·(x[n] − y[n−1])，a = 1 − exp(−2π f_c Δt)，初值取首样本（scipy lfilter，与逐步递推逐位一致）。
- 噪声（per-sample，Δt = 5 ms）：白噪声 sigma = ND·sqrt(200)；偏置随机游走 b(j+1) = b(j) + rw·sqrt(Δt)·n（MID-360 内置 IMU 规则）；
  上电偏置 turn_on_sigma·n（j = 0 通道）。随机数 key(run_seed, 22, uav_no, j, 0x3000 + sensor_no·32 + c)：c 0–5 白噪声、
  6–11 偏置步进、12–17 上电偏置。clean 档无噪声；harsh 档白噪声与随机游走 × 1.5。量程饱和 32 g、2000 °/s，flags bit0。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from awr.sim.sensors import cbrng

__all__ = ["MID360_IMU_IN_LIDAR", "G", "ImuModel", "ImuSamples", "kinematics_from_poses", "load_imu_model", "synth_imu"]

G = 9.80665
STREAM_M25 = 22
CH_IMU = 0x3000
TIER = {"clean": 0.0, "realistic": 1.0, "harsh": 1.5}
ACC_SAT = 32 * G
GYRO_SAT = math.radians(2000.0)
MID360_IMU_IN_LIDAR = (0.011, 0.02329, -0.04412)     # mid360.yaml imu.lidar_in_imu.t 取反（旋转为单位阵）
DEFAULT_MODEL = Path(__file__).resolve().parents[3] / "vehicles" / "awr_q9" / "sensors" / "imu.yaml"


@dataclass(frozen=True)
class ImuModel:
    rate_hz: int = 200
    gyro_nd: float = 3.39e-4          # rad/s/√Hz
    gyro_rw: float = 3.88e-5          # rad/s²/√Hz
    gyro_turn_on: float = 8.73e-3     # rad/s
    acc_nd: float = 4.0e-3            # m/s²/√Hz
    acc_rw: float = 6.0e-3            # m/s³/√Hz
    acc_turn_on: float = 0.196        # m/s²
    aa_cutoff_hz: float = 80.0


@dataclass
class ImuSamples:
    t_ns: np.ndarray                  # (n,) int64，200 Hz 绝对网格
    acc_mps2: np.ndarray              # (n, 3) IMU 帧比力
    gyro_rps: np.ndarray              # (n, 3)
    flags: np.ndarray                 # (n,) u8，bit0 饱和
    acc_true: np.ndarray              # 无噪声值（真值）
    gyro_true: np.ndarray


def load_imu_model(path: Path | str | None = None) -> ImuModel:
    import yaml

    p = Path(path or DEFAULT_MODEL)
    if path is None and not p.exists():                  # 运行镜像只挂载 python/：取与 imu.yaml 相同的内置缺省
        return ImuModel()
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    g, a = d["gyro"], d["accel"]
    return ImuModel(int(d.get("rate_hz", 200)), float(g["noise_density"]), float(g["random_walk"]), float(g["turn_on_sigma"]),
                    float(a["noise_density"]), float(a["random_walk"]), float(a["turn_on_sigma"]))


def _quat_R(q: np.ndarray) -> np.ndarray:
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    return np.stack([np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
                     np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
                     np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1)], 1)


def _log_so3(R: np.ndarray) -> np.ndarray:
    """(n, 3, 3) → 旋转向量 (n, 3)。"""
    c = np.clip((np.trace(R, axis1=1, axis2=2) - 1.0) / 2.0, -1.0, 1.0)
    th = np.arccos(c)
    v = np.stack([R[:, 2, 1] - R[:, 1, 2], R[:, 0, 2] - R[:, 2, 0], R[:, 1, 0] - R[:, 0, 1]], 1)
    s = np.sin(th)
    k = np.where(s > 1e-9, th / (2 * np.where(s > 1e-9, s, 1.0)), 0.5)
    return v * k[:, None]


def kinematics_from_poses(t_ns: np.ndarray, pos: np.ndarray, q_xyzw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (omega_b (n, 3) rad/s，f_b (n, 3) m/s²)；机体 FLU、世界 ENU。"""
    t = np.asarray(t_ns, np.float64) / 1e9
    p = np.asarray(pos, np.float64)
    R = _quat_R(np.asarray(q_xyzw, np.float64))
    v = np.gradient(p, t, axis=0)
    a = np.gradient(v, t, axis=0)
    dR = np.einsum("nji,njk->nik", R[:-1], R[1:])                  # R_k−1ᵀ R_k（机体系增量）
    w = _log_so3(dR) / np.diff(t)[:, None]
    omega = np.vstack([w[:1], w])
    f_w = a + np.array([0.0, 0.0, G])
    f_b = np.einsum("nji,nj->ni", R, f_w)
    return omega, f_b


def _key_normal(seed: int, uav_no: int, j: np.ndarray, sensor_no: int, c: int) -> np.ndarray:
    return cbrng.normal_elem(seed, STREAM_M25, uav_no, j, CH_IMU + sensor_no * 32 + c)


def synth_imu(t_ns: np.ndarray, omega_b: np.ndarray, f_b: np.ndarray, *, R_bi: np.ndarray, r_imu_b: np.ndarray, model: ImuModel,
              t_from_ns: int, t_to_ns: int, tier: str = "realistic", seed: int = 0, uav_no: int = 0, sensor_no: int = 0) -> ImuSamples:
    """`t_ns` 为物理步时刻；输出 [t_from, t_to) 内的 200 Hz 绝对网格样本（网格原点 0）。噪声状态从 j = 0 递推，与分批调用无关。"""
    from scipy.signal import lfilter

    t = np.asarray(t_ns, np.int64)
    w = np.asarray(omega_b, np.float64)
    fb = np.asarray(f_b, np.float64)
    tt = t.astype(np.float64) / 1e9
    wdot = np.vstack([np.zeros((1, 3)), np.diff(w, axis=0) / np.diff(tt)[:, None]])
    r = np.asarray(r_imu_b, np.float64)
    lever = np.cross(wdot, r) + np.cross(w, np.cross(w, r))
    f_imu = (fb + lever) @ R_bi                                   # R_biᵀ x（行向量形式）
    w_imu = w @ R_bi
    dt = float(np.median(np.diff(tt))) if len(tt) > 1 else 1.0 / 250
    a = 1.0 - math.exp(-2.0 * math.pi * model.aa_cutoff_hz * dt)
    x = np.hstack([w_imu, f_imu])
    y = np.empty_like(x)
    for k in range(6):
        y[:, k], _ = lfilter([a], [1.0, -(1.0 - a)], x[:, k], zi=[(1.0 - a) * x[0, k]])
    step = 1_000_000_000 // model.rate_hz
    j0 = -(-int(t_from_ns) // step)
    j1 = -(-int(t_to_ns) // step)
    j = np.arange(j0, j1, dtype=np.int64)
    tj = j * step
    ok = (tj >= t[0]) & (tj <= t[-1])
    j, tj = j[ok], tj[ok]
    yi = np.stack([np.interp(tj.astype(np.float64), t.astype(np.float64), y[:, k]) for k in range(6)], 1)
    gyro_t, acc_t = yi[:, :3], yi[:, 3:]
    sc = TIER[tier]
    gyro, acc = gyro_t.copy(), acc_t.copy()
    if sc > 0 and len(j):
        dts = 1.0 / model.rate_hz
        sg, sa = model.gyro_nd * math.sqrt(model.rate_hz) * sc, model.acc_nd * math.sqrt(model.rate_hz) * sc
        jj = j[:, None]
        white = np.hstack([_key_normal(seed, uav_no, jj, sensor_no, c) for c in range(6)])
        on = np.array([float(_key_normal(seed, uav_no, 0, sensor_no, 12 + c)) for c in range(6)])
        on *= np.array([model.gyro_turn_on] * 3 + [model.acc_turn_on] * 3)
        rw = np.array([model.gyro_rw] * 3 + [model.acc_rw] * 3) * math.sqrt(dts) * sc
        jall = np.arange(0, int(j[-1]), dtype=np.int64)[:, None]      # 偏置从 j = 0 递推：b(j) = Σ_{m<j} rw·n_m
        steps = np.hstack([_key_normal(seed, uav_no, jall, sensor_no, 6 + c) for c in range(6)]) * rw if len(jall) else np.zeros((0, 6))
        cum = np.vstack([np.zeros((1, 6)), np.cumsum(steps, axis=0)])
        bias = cum[j] + on
        gyro = gyro_t + bias[:, :3] + sg * white[:, :3]
        acc = acc_t + bias[:, 3:] + sa * white[:, 3:]
    sat = (np.abs(acc) > ACC_SAT).any(1) | (np.abs(gyro) > GYRO_SAT).any(1)
    acc = np.clip(acc, -ACC_SAT, ACC_SAT)
    gyro = np.clip(gyro, -GYRO_SAT, GYRO_SAT)
    return ImuSamples(tj, acc, gyro, sat.astype(np.uint8), acc_t, gyro_t)
