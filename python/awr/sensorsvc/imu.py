"""IMU 合成（M25 §6.9.1、§6.9.2）：由位姿历史得到 IMU 帧比力与角速度，一阶抗混叠，线性插值到 200 Hz 绝对网格，加噪声。

- 物理档的 PoseHistory 直接给出 `omega_b` 与 `f_b`（步内常值）；脚本位姿（采集作业、P0a-1/2）没有这两列，
  `kinematics_from_poses` 以样本差分求得：速度与加速度为中心差分（端点单侧），角速度为相邻姿态的相对旋转向量 / Δt（后向，
  首样本取第 1 段），比力 f_b = R_wbᵀ (a + g·e_z)。
- 杆臂：f_imu = R_biᵀ (f_b + ω̇ × r + ω × (ω × r))，ω̇ 为后向差分；w_imu = R_biᵀ ω。
- 抗混叠：y[n] = y[n−1] + a·(x[n] − y[n−1])，a = 1 − exp(−2π f_c Δt)，初值取首样本（scipy lfilter，与逐步递推逐位一致）。
- 噪声参数：MID-360 内置 IMU 型号 `imu_icm40609_livox`（`vehicles/d3/sensor_models/`，AWR-20 §15.6.5 单一真源，
  `awr.sim.catalog.loader.imu_noise_tier` 解析）。clean 档无噪声；realistic 档为型号文件取值；harsh 档为 realistic 加振动
  （白噪声与随机游走不放大）。每样本（Δt = 5 ms）：白噪声为每样本 sigma；偏置纯随机游走 b(j+1) = b(j) + rw·sqrt(Δt)·n，
  b(0) = 0，j < 0（预滚窗口）向负方向延伸 b(j) = b(j+1) − rw·sqrt(Δt)·n_j；上电偏置 turn_on_sigma·n（j = 0 通道）；
  振动 v = A·sin(2π f_v t_j + φ_axis) 只加在加速度三轴，f_v 为机型 `acoustic.bpf_hz`。随机数
  key(run_seed, 22, uav_no, j, 0x3000 + sensor_no·32 + c)：c 0–5 白噪声、6–11 偏置步进、12–17 上电偏置、18–20 振动相位
  （U[0, 1) × 2π，j = 0）。量程饱和 32 g、2000 °/s，flags bit0。
- 动力学断言（M25-FR-092，D3-AC-13 C8）：`check_dynamics` / `assert_dynamics` 对 PoseHistory 检查 |ω| < 5 rad/s 与
  比力模长 |a| < 30 m/s²，违例时 SLAM 导出返回 642 并标注时间段。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from awr.sensorsvc.errors import SensorSvcError
from awr.sim.sensors import cbrng

__all__ = ["DEFAULT_BPF_HZ", "DYN_ACC_MAX_MPS2", "DYN_OMEGA_MAX_RPS", "MID360_IMU_IN_LIDAR", "TIERS", "BiasWalk", "DynamicsReport", "G", "ImuModel",
           "ImuSamples", "assert_dynamics", "check_dynamics", "kinematics_from_poses", "load_imu_model", "synth_imu"]

G = 9.80665
STREAM_M25 = 22
CH_IMU = 0x3000
TIERS = ("clean", "realistic", "harsh")
ACC_SAT = 32 * G
GYRO_SAT = math.radians(2000.0)
MID360_IMU_IN_LIDAR = (0.011, 0.02329, -0.04412)     # mid360.yaml imu.lidar_in_imu.t 取反（旋转为单位阵）
DEFAULT_MODEL = Path(__file__).resolve().parents[3] / "vehicles" / "d3" / "sensor_models" / "imu_icm40609_livox.yaml"
DEFAULT_BPF_HZ = 150.0                               # 缺省 SLAM 机型 p600_mid360_inv 的 acoustic.bpf_hz（vehicles/p600/params.yaml）
DYN_OMEGA_MAX_RPS = 5.0                              # C8：|ω| < 5 rad/s
DYN_ACC_MAX_MPS2 = 30.0                              # C8：|a| < 30 m/s²（比力模长，与 slam.rig 的初始化窗口判据同口径）
SEC_DYN_JOIN_NS = 50_000_000                         # 违例样本间隔 ≤ 50 ms 时并入同一时间段


@dataclass(frozen=True)
class ImuModel:
    """内置 IMU 型号的 realistic 档参数（SI 单位，每样本离散；AWR-20 §15.6.5）。clean 档全部取 0，harsh 档另加振动。
    缺省值与 `imu_icm40609_livox@1.0.0` 相同（运行镜像只挂载 python/ 时使用）。"""

    rate_hz: int = 200
    gyro_sigma: float = 7.0e-4                       # rad/s，每样本白噪声 sigma（200 Hz）
    gyro_rw: float = 1.0e-5                          # rad/s/√s，偏置随机游走
    gyro_turn_on: float = math.radians(0.5)          # rad/s，上电偏置 sigma（每运行抽一次）
    acc_sigma: float = 1.0e-3 * G                    # m/s²，每样本白噪声 sigma
    acc_rw: float = 1.0e-5 * G                       # m/s²/√s
    acc_turn_on: float = 0.196                       # m/s²
    aa_cutoff_hz: float = 80.0
    vib_hz: float = DEFAULT_BPF_HZ                   # harsh 振动频率（机型 acoustic.bpf_hz）
    vib_amp_mps2: float = 1.5                        # harsh 振动幅值（三轴）
    model_id: str = "imu_icm40609_livox"
    noise_table_version: str = "d3-noise-1"

    @property
    def gyro_nd(self) -> float:
        """等效噪声密度 rad/s/√Hz（sigma / sqrt(rate)）。"""
        return self.gyro_sigma / math.sqrt(self.rate_hz)

    @property
    def acc_nd(self) -> float:
        """等效噪声密度 m/s²/√Hz。"""
        return self.acc_sigma / math.sqrt(self.rate_hz)

    @classmethod
    def from_noise(cls, noise: Mapping[str, Any], *, vib_hz: float | None = None, **kw: Any) -> ImuModel:
        """`imu_noise_tier(doc, "realistic" | "harsh")` 的字典（即 rig.json `noise.imu`）→ 型号参数；harsh 字典的 `vib_hz` 优先。
        clean 档字典全部为 0，不能还原 realistic 参数，拒绝。"""
        if noise.get("tier") == "clean":
            raise ValueError("clean-tier noise parameters are all zero; build the model from the realistic or harsh tier")
        hz = noise.get("vib_hz") or vib_hz or DEFAULT_BPF_HZ
        if "vib_amp_mps2" not in kw and noise.get("tier") == "harsh":
            kw["vib_amp_mps2"] = float(noise.get("vib_amp_mps2", 0.0))
        return cls(rate_hz=int(noise.get("rate_hz", 200)), gyro_sigma=float(noise["gyro_sigma_sample_rps"]),
                   gyro_rw=float(noise["gyro_bias_rw_rps_rts"]), gyro_turn_on=math.radians(float(noise["turn_on_gyro_dps"])),
                   acc_sigma=float(noise["acc_sigma_sample_g"]) * G, acc_rw=float(noise["acc_bias_rw_g_rts"]) * G,
                   acc_turn_on=float(noise["turn_on_acc_mps2"]), aa_cutoff_hz=float(noise.get("aa_cutoff_hz", 80.0)), vib_hz=float(hz),
                   **kw)

    def table(self, tier: str) -> dict[str, Any]:
        """噪声档 `tier` 下的参数，键与单位同 `imu_noise_tier`（g、rad/s、g/√s、rad/s/√s、m/s²、°/s），用于与参数表逐项对照。"""
        if tier not in TIERS:
            raise ValueError(f"noise tier {tier!r} not in {TIERS}")
        z = tier == "clean"
        out: dict[str, Any] = {
            "acc_sigma_sample_g": 0.0 if z else self.acc_sigma / G, "gyro_sigma_sample_rps": 0.0 if z else self.gyro_sigma,
            "acc_bias_rw_g_rts": 0.0 if z else self.acc_rw / G, "gyro_bias_rw_rps_rts": 0.0 if z else self.gyro_rw,
            "turn_on_acc_mps2": 0.0 if z else self.acc_turn_on, "turn_on_gyro_dps": 0.0 if z else math.degrees(self.gyro_turn_on),
            "vib_hz": self.vib_hz if tier == "harsh" else None, "vib_amp_mps2": self.vib_amp_mps2 if tier == "harsh" else 0.0,
        }
        out.update({"rate_hz": self.rate_hz, "acc_unit_export": "g", "aa_cutoff_hz": self.aa_cutoff_hz, "tier": tier})
        return out


@dataclass
class ImuSamples:
    t_ns: np.ndarray                  # (n,) int64，200 Hz 绝对网格
    acc_mps2: np.ndarray              # (n, 3) IMU 帧比力
    gyro_rps: np.ndarray              # (n, 3)
    flags: np.ndarray                 # (n,) u8，bit0 饱和
    acc_true: np.ndarray              # 无噪声值（真值）
    gyro_true: np.ndarray


def load_imu_model(path: Path | str | None = None, *, bpf_hz: float | None = None) -> ImuModel:
    """读取内置 IMU 型号文件（`awr.sensor.imu_mems.v1`，缺省 `imu_icm40609_livox`）；`bpf_hz` 为机型 `acoustic.bpf_hz`
    （harsh 振动频率，缺省取 p600 的 150 Hz）。缺省路径不存在时（运行镜像只挂载 python/）返回内置缺省。"""
    import yaml

    from awr.sim.catalog.loader import imu_noise_tier

    p = Path(path or DEFAULT_MODEL)
    hz = float(bpf_hz) if bpf_hz is not None else DEFAULT_BPF_HZ
    if path is None and not p.exists():
        return ImuModel(vib_hz=hz)
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    if d.get("schema") != "awr.sensor.imu_mems.v1":
        raise ValueError(f"{p}: built-in IMU model must be awr.sensor.imu_mems.v1, got {d.get('schema')!r}")
    kw: dict[str, Any] = {} if d.get("vibration_harsh") else {"vib_amp_mps2": 0.0}
    n = imu_noise_tier(d, "harsh" if d.get("vibration_harsh") else "realistic", bpf_hz=hz)
    return ImuModel.from_noise(n, vib_hz=hz, model_id=str(d.get("model_id", p.stem)), noise_table_version=str(d.get("noise_table_version", "")),
                               **kw)


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


def _turn_on(seed: int, uav_no: int, sensor_no: int) -> np.ndarray:
    return np.array([float(_key_normal(seed, uav_no, 0, sensor_no, 12 + c)) for c in range(6)])


def _bias_walk(seed: int, uav_no: int, sensor_no: int, j: np.ndarray, rw: np.ndarray) -> np.ndarray:
    """偏置随机游走 b(j)（6 通道），b(0) = 0：j > 0 为 Σ_{0≤m<j} s_m，j < 0 为 −Σ_{j≤m<0} s_m，s_m = rw·sqrt(Δt)·n_m。
    两个方向各自从 0 顺序累加，结果只取决于 j，与分批调用的区间无关（逐位一致）。"""
    out = np.zeros((len(j), 6))
    hi, lo = int(j.max(initial=0)), int(j.min(initial=0))
    if hi > 0:
        m = np.arange(0, hi, dtype=np.int64)[:, None]
        steps = np.hstack([_key_normal(seed, uav_no, m, sensor_no, 6 + c) for c in range(6)]) * rw
        cum = np.vstack([np.zeros((1, 6)), np.cumsum(steps, axis=0)])
        pos = j > 0
        out[pos] = cum[j[pos]]
    if lo < 0:
        m = np.arange(-1, lo - 1, -1, dtype=np.int64)[:, None]                       # −1, −2, …, lo
        steps = np.hstack([_key_normal(seed, uav_no, m, sensor_no, 6 + c) for c in range(6)]) * rw
        cum = np.vstack([np.zeros((1, 6)), -np.cumsum(steps, axis=0)])                # cum[k] = b(−k)
        neg = j < 0
        out[neg] = cum[-j[neg]]
    return out


class BiasWalk:
    """`_bias_walk` 的增量缓存（逐 tick 调用的在线路径用）：正方向累加和按需向后延伸并保留，延伸段以已缓存的末值为首元素重新
    `cumsum`，逐次加法与整段从 0 顺序累加相同，结果逐位一致；每次调用的开销只与新增网格点数成正比（整段重算随仿真时长线性
    增长，360 s 运行的末段每次约 7 万点 × 6 通道）。负方向（预滚窗口）与键变化时退回整段计算。一个对象只服务一个
    (seed, uav_no, sensor_no, rw) 键；键变化时清空重建。"""

    def __init__(self) -> None:
        self._key: tuple | None = None
        self._cum = np.zeros((0, 6))
        self._n = 0                                               # 有效行数：行 r 为 b(r)，r < _n
        self._on: dict[tuple[int, int, int], np.ndarray] = {}     # 上电偏置的标准正态抽样（j = 0 通道 12–17，只取决于键）

    def turn_on(self, seed: int, uav_no: int, sensor_no: int) -> np.ndarray:
        """上电偏置通道的 6 个标准正态值（与 `synth_imu` 逐次计算相同，按键缓存）。"""
        key = (int(seed), int(uav_no), int(sensor_no))
        if key not in self._on:
            self._on[key] = _turn_on(seed, uav_no, sensor_no)
        return self._on[key].copy()

    def __call__(self, seed: int, uav_no: int, sensor_no: int, j: np.ndarray, rw: np.ndarray) -> np.ndarray:
        j = np.asarray(j, np.int64)
        if len(j) and int(j.min()) < 0:
            return _bias_walk(seed, uav_no, sensor_no, j, rw)
        key = (int(seed), int(uav_no), int(sensor_no), np.asarray(rw, np.float64).tobytes())
        if key != self._key:
            self._key, self._cum, self._n = key, np.zeros((0, 6)), 0
        hi = int(j.max(initial=0))
        if hi >= self._n:
            self._extend(seed, uav_no, sensor_no, hi, rw)
        return self._cum[j] if len(j) else np.zeros((0, 6))

    def _extend(self, seed: int, uav_no: int, sensor_no: int, hi: int, rw: np.ndarray) -> None:
        """有效行延伸到 b(hi)（含）。首次与整段计算相同（0 行之后接 `cumsum(steps)`）。"""
        if self._n == 0:
            new = _bias_walk(seed, uav_no, sensor_no, np.arange(0, hi + 1, dtype=np.int64), rw)
        else:
            m = np.arange(self._n - 1, hi, dtype=np.int64)[:, None]          # s_{n−1} … s_{hi−1}
            steps = np.hstack([_key_normal(seed, uav_no, m, sensor_no, 6 + c) for c in range(6)]) * rw
            new = np.cumsum(np.vstack([self._cum[self._n - 1:self._n], steps]), axis=0)[1:]
        n1 = self._n + len(new)
        if n1 > len(self._cum):                                       # 容量倍增，避免每 tick 复制整段
            buf = np.zeros((max(n1, 2 * len(self._cum), 1024), 6))
            buf[:self._n] = self._cum[:self._n]
            self._cum = buf
        self._cum[self._n:n1] = new
        self._n = n1


def synth_imu(t_ns: np.ndarray, omega_b: np.ndarray, f_b: np.ndarray, *, R_bi: np.ndarray, r_imu_b: np.ndarray, model: ImuModel,
              t_from_ns: int, t_to_ns: int, tier: str = "realistic", seed: int = 0, uav_no: int = 0, sensor_no: int = 0,
              bias: BiasWalk | None = None) -> ImuSamples:
    """`t_ns` 为物理步时刻；输出 [t_from, t_to) 内的 200 Hz 绝对网格样本（网格原点 0，预滚窗口为负时刻）。噪声状态以 j = 0 为
    原点递推，与分批调用无关。`bias`：偏置随机游走的增量缓存（`BiasWalk`，每个 (uav, sensor) 一个；结果与不传时逐位一致）。"""
    from scipy.signal import lfilter

    if tier not in TIERS:
        raise ValueError(f"noise tier {tier!r} not in {TIERS}")
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
    gyro, acc = gyro_t.copy(), acc_t.copy()
    if tier != "clean" and len(j):
        dts = 1.0 / model.rate_hz
        jj = j[:, None]
        white = np.hstack([_key_normal(seed, uav_no, jj, sensor_no, c) for c in range(6)])
        on = bias.turn_on(seed, uav_no, sensor_no) if bias is not None else _turn_on(seed, uav_no, sensor_no)
        on *= np.array([model.gyro_turn_on] * 3 + [model.acc_turn_on] * 3)
        rw = np.array([model.gyro_rw] * 3 + [model.acc_rw] * 3) * math.sqrt(dts)
        bias = (bias(seed, uav_no, sensor_no, j, rw) if bias is not None else _bias_walk(seed, uav_no, sensor_no, j, rw)) + on
        gyro = gyro_t + bias[:, :3] + model.gyro_sigma * white[:, :3]
        acc = acc_t + bias[:, 3:] + model.acc_sigma * white[:, 3:]
        if tier == "harsh" and model.vib_amp_mps2 > 0:            # 抗混叠与重采样之后按输出采样时刻叠加（含采样混叠）
            ph = 2.0 * math.pi * np.array([float(cbrng.uniform_elem(seed, STREAM_M25, uav_no, 0, CH_IMU + sensor_no * 32 + 18 + c))
                                           for c in range(3)])
            acc = acc + model.vib_amp_mps2 * np.sin(2.0 * math.pi * model.vib_hz * (tj.astype(np.float64) / 1e9)[:, None] + ph)
    sat = (np.abs(acc) > ACC_SAT).any(1) | (np.abs(gyro) > GYRO_SAT).any(1)
    acc = np.clip(acc, -ACC_SAT, ACC_SAT)
    gyro = np.clip(gyro, -GYRO_SAT, GYRO_SAT)
    return ImuSamples(tj, acc, gyro, sat.astype(np.uint8), acc_t, gyro_t)


# ---------------------------------------------------------------------------------------------------------------- 动力学断言
@dataclass
class DynamicsReport:
    """PoseHistory 动力学断言（M25-FR-092，C8）：全程最大 |ω| 与比力模长，违例时间段 `[{t0_ns, t1_ns, max_omega_rps, max_acc_mps2}]`。"""

    n: int
    max_omega_rps: float
    max_acc_mps2: float
    segments: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.segments

    def to_dict(self) -> dict:
        return {"n": self.n, "max_omega_rps": round(self.max_omega_rps, 6), "max_acc_mps2": round(self.max_acc_mps2, 6),
                "segments": self.segments, "ok": self.ok}


def check_dynamics(t_ns, omega_b, f_b, *, omega_max: float = DYN_OMEGA_MAX_RPS, acc_max: float = DYN_ACC_MAX_MPS2) -> DynamicsReport:
    """逐样本 |ω| < `omega_max`、|f_b| < `acc_max`；相距 ≤ 50 ms 的违例样本并为一个时间段。"""
    t = np.asarray(t_ns, np.int64)
    wn = np.linalg.norm(np.asarray(omega_b, np.float64).reshape(-1, 3), axis=1)
    fn = np.linalg.norm(np.asarray(f_b, np.float64).reshape(-1, 3), axis=1)
    if not len(t) == len(wn) == len(fn):
        raise ValueError("t_ns, omega_b and f_b must have the same length")
    bad = np.flatnonzero(~((wn < omega_max) & (fn < acc_max)))
    groups = np.split(bad, np.flatnonzero(np.diff(t[bad]) > SEC_DYN_JOIN_NS) + 1) if len(bad) else []
    segs = [{"t0_ns": int(t[g[0]]), "t1_ns": int(t[g[-1]]), "max_omega_rps": round(float(wn[g].max()), 6),
             "max_acc_mps2": round(float(fn[g].max()), 6)} for g in groups]
    return DynamicsReport(len(t), float(wn.max(initial=0.0)), float(fn.max(initial=0.0)), segs)


def assert_dynamics(t_ns, omega_b, f_b, **kw: float) -> DynamicsReport:
    """SLAM 导出前的动力学断言：违例时抛 642（`extra.segments` 标注时间段），否则返回报告。"""
    rep = check_dynamics(t_ns, omega_b, f_b, **kw)
    if not rep.ok:
        s = rep.segments
        raise SensorSvcError(642, f"pose history violates |omega| < {kw.get('omega_max', DYN_OMEGA_MAX_RPS)} rad/s or "
                                  f"|a| < {kw.get('acc_max', DYN_ACC_MAX_MPS2)} m/s^2 in {len(s)} segment(s), first "
                                  f"[{s[0]['t0_ns']}, {s[0]['t1_ns']}] ns", segments=s, dynamics=rep.to_dict())
    return rep
