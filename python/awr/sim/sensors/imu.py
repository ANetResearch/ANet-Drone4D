"""ImuBank：IMU 偏置 GM 与摘要（M13-FR-033；M13 §6.5.6；D1-ext）。

比力 `f_b = R_wbᵀ·(a_w − g_w)`，`g_w = (0, 0, −9.80665)`（ENU），悬停时 `f_b ≈ (0, 0, +9.80665)`；
测量 `f̃ = f_b + sigma_ba·z_ba + b_a0 + ND_a·√200·n_a`，`ω̃ = ω_b + sigma_bg·z_bg + b_g0 + ND_g·√200·n_g`（报告"一个 200 Hz 样本"）。
偏置 GM：`sigma_b = RW·√(τ/2)`，φ = e^{−1 s/τ}；sensors stage 在 c ≡ 2 (mod 5) 的调用推进 `slot % 10 == part` 的 IMU slot
（每 slot 1 Hz），PCG64 流 3、slot 升序；上电偏置 `b0 = turn_on_sigma·n`（计数器 RNG，tick = spawn tick，通道 9–14）。
线上单位 m/s² 与 rad/s；MID-360 内置 IMU 的 g 只在 lidar_frame 侧与网关换算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from .enums import SensorKind
from .gm import sigma_from_rw
from .spec import SensorSpec

if TYPE_CHECKING:
    from .runtime import SensorRuntime

__all__ = ["G0", "ImuBank", "ImuTable", "imu_table", "specific_force"]

G0 = 9.80665
DT_BIAS_S = 1.0
RATE_SAMPLE_HZ = 200.0


@dataclass(frozen=True)
class ImuTable:
    nd: np.ndarray      # (6,) gyro 3、acc 3 的噪声密度
    sigma_b: np.ndarray  # (6,) 偏置稳态 sigma
    phi: np.ndarray      # (6,) 1 s 的 φ
    turn_on: np.ndarray  # (6,)


_TABLES: dict[int, ImuTable] = {}


def imu_table(spec: SensorSpec) -> ImuTable:
    t = _TABLES.get(id(spec))
    if t is not None:
        return t
    g, a = spec.noise["gyro"], spec.noise["accel"]
    tau = np.array([float(g["bias_tau_s"])] * 3 + [float(a["bias_tau_s"])] * 3)
    sb = np.array([sigma_from_rw(float(g["random_walk"]), float(g["bias_tau_s"]))] * 3
                  + [sigma_from_rw(float(a["random_walk"]), float(a["bias_tau_s"]))] * 3)
    nd = np.array([float(g["noise_density"])] * 3 + [float(a["noise_density"])] * 3)
    on = np.array([float(g["turn_on_sigma"])] * 3 + [float(a["turn_on_sigma"])] * 3)
    t = _TABLES[id(spec)] = ImuTable(nd, sb, np.exp(-DT_BIAS_S / tau), on)
    return t


def specific_force(q_xyzw: np.ndarray, acc_enu: np.ndarray) -> np.ndarray:
    """(n,4)、(n,3) -> 机体 FLU 比力 (n,3)。"""
    from .frames import quat_to_R

    R = quat_to_R(np.asarray(q_xyzw, np.float64).reshape(-1, 4))
    a = np.asarray(acc_enu, np.float64).reshape(-1, 3).copy()
    a[:, 2] += G0
    return np.einsum("nji,nj->ni", R, a)


class ImuBank:
    def __init__(self, rt: SensorRuntime) -> None:
        self.rt = rt
        self.stats = {"steps": 0}

    def on_spawn(self, slot: int, spec: SensorSpec, agent_no: int, tick: int) -> None:
        from . import cbrng

        rt = self.rt
        tb = imu_table(spec)
        b0 = cbrng.normal(rt.seed, 3, np.array([agent_no]), int(tick), np.arange(9, 15))[0]
        rt.blk["im_b0"][slot] = (tb.turn_on * b0).astype(np.float32)
        rt.blk["im_zb"][slot] = cbrng.normal(rt.seed, 3, np.array([agent_no]), int(tick), np.arange(18, 24))[0]

    def bias_part(self, S: Any, ctx: Any, part: int) -> int:
        rt = self.rt
        slots = rt.part_slots(SensorKind.IMU, part, 10)
        if slots.size == 0:
            return 0
        n = rt.rng3(ctx).standard_normal((slots.size, 6))
        b = rt.blk
        for rig_i, sel in rt.group_by_rig(slots):
            tb = imu_table(rt.rig_list[rig_i].by_kind(SensorKind.IMU))
            ss = slots[sel]
            b["im_zb"][ss] = tb.phi * b["im_zb"][ss] + np.sqrt(1.0 - tb.phi * tb.phi) * n[sel]
        self.stats["steps"] += 1
        return int(slots.size)

    def bias(self, slots: np.ndarray) -> np.ndarray:
        """当前偏置（含上电偏置）(n, 6)：gyro 3（rad/s）、acc 3（m/s²）。"""
        rt = self.rt
        out = np.zeros((slots.size, 6))
        for rig_i, sel in rt.group_by_rig(slots):
            tb = imu_table(rt.rig_list[rig_i].by_kind(SensorKind.IMU))
            ss = slots[sel]
            out[sel] = tb.sigma_b * rt.blk["im_zb"][ss] + rt.blk["im_b0"][ss].astype(np.float64)
        return out

    def sample(self, S: Any, slots: np.ndarray, agent_no: np.ndarray, tick: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """一个 200 Hz 样本：(acc_meas (n,3), gyro_meas (n,3), bias (n,6))；白噪声为计数器 RNG 通道 3–8。"""
        from . import cbrng

        rt = self.rt
        if slots.size == 0:
            z = np.zeros((0, 3))
            return z, z, np.zeros((0, 6))
        PQ = S.enu.pose_enu_flu(slots)
        f = specific_force(PQ[:, 3:], S.enu.acc_enu(slots))
        w = S.enu.omega_flu_slots(slots)
        bias = self.bias(slots)
        nw = cbrng.normal(rt.seed, 3, agent_no.astype(np.int64), int(tick), np.arange(3, 9))
        nd = np.zeros((slots.size, 6))
        for rig_i, sel in rt.group_by_rig(slots):
            nd[sel] = imu_table(rt.rig_list[rig_i].by_kind(SensorKind.IMU)).nd
        k = math.sqrt(RATE_SAMPLE_HZ)
        gyro = w + bias[:, :3] + nd[:, :3] * k * nw[:, :3]
        acc = f + bias[:, 3:] + nd[:, 3:] * k * nw[:, 3:]
        return acc, gyro, bias
