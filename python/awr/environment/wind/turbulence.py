"""湍流：共享冻结 von Kármán 湍流盒（默认）与 Dryden 回归模式（M07-FR-012、FR-013；M07 §6.3.8、§6.3.9；g06 §5.5；g08 §8）。

湍流盒 `vk_box(N=64, dx=4, L=30, seed)`：修正波数 p = sin(k*dx)/dx 的 Helmholtz 投影（离散中心差分无散），三分量合并
按单一 std 归一化（不得逐分量，§6.3.8），存为 AWRV kind 2（RGB = u, v, w，A = 0，f16）。采样"平移坐标、旋转输出"：
Dq = (f_adv*D_enu) mod 256，b = trilerp_periodic(box, (p − Dq)/4 − 0.5)（格心约定），物理与前端读同一 f16 资产。
MIL-F-8785C 高度缩放：sigma_w = 0.5295*sigma_ref，sigma_u = sigma_v = sigma_w / (0.177 + 0.000823*hft)^0.4，hft = max(z_agl/0.3048, 10)。

DrydenBank：每 slot 一份标准化状态（平稳分布方差为 I），精确离散，与 dt 无关；V = max(|U_mean − v|, 0.5)；RNG 流 1
（`dryden`）每个 env tick 按 slot 升序一次抽 (n, 5)，新 slot 以 N(0, I) 平稳起步。输出在平均风坐标系 (e, n, z_hat) 中，旋转回 ENU。
"""

from __future__ import annotations

import math

import numpy as np

from ..weather.presets import C

__all__ = ["SIGMA_W_OVER_REF", "DrydenBank", "TurbBox", "mil_sigma_arr", "vk_box"]

TURB_N = int(C["turb_n"])
TURB_DX = float(C["turb_dx_m"])
TURB_L = float(C["turb_l_m"])
TURB_PERIOD = float(C["turb_period_m"])
GROUND_FADE_M = float(C["ground_fade_m"])
DRYDEN_VMIN = float(C["dryden_min_airspeed_mps"])
FT = 0.3048
SIGMA_W_OVER_REF = (0.177 + 0.000823 * (10.0 / FT)) ** 0.4  # 0.5295：10 m 处 sigma_u 恰为 sigma_ref


def vk_box(n: int = TURB_N, dx: float = TURB_DX, L: float = TURB_L, seed: int = 7) -> np.ndarray:
    """-> float64 (3, n, n, n)，轴顺序 (分量, x, y, z)；三分量合并 std = 1。"""
    rng = np.random.default_rng(seed)  # PCG64(SeedSequence(seed))
    k1 = 2.0 * np.pi * np.fft.fftfreq(n, dx)
    KX, KY, KZ = np.meshgrid(k1, k1, k1, indexing="ij")
    K = np.sqrt(KX**2 + KY**2 + KZ**2)
    Ks = K.copy()
    Ks[0, 0, 0] = 1.0
    E = (K * L) ** 4 / (1.0 + (K * L) ** 2) ** (17.0 / 6.0)
    amp = np.sqrt(E / (4.0 * np.pi * Ks**2))
    amp[0, 0, 0] = 0.0
    PX, PY, PZ = np.sin(KX * dx) / dx, np.sin(KY * dx) / dx, np.sin(KZ * dx) / dx
    P2 = PX**2 + PY**2 + PZ**2
    P2[P2 == 0] = 1.0
    xi = rng.standard_normal((3, n, n, n)) + 1j * rng.standard_normal((3, n, n, n))
    kd = (PX * xi[0] + PY * xi[1] + PZ * xi[2]) / P2
    a = np.stack([xi[0] - PX * kd, xi[1] - PY * kd, xi[2] - PZ * kd]) * amp
    u = np.real(np.fft.ifftn(a, axes=(1, 2, 3)))
    u /= u.std()
    return u


def box_to_rgba(u: np.ndarray) -> np.ndarray:
    """(3, x, y, z) -> (z, y, x, 4) f16，A = 0（AWRV layout 0）。"""
    n = u.shape[1]
    out = np.zeros((n, n, n, 4), np.float16)
    for c in range(3):
        out[..., c] = np.transpose(u[c], (2, 1, 0)).astype(np.float16)
    return out


def mil_sigma_arr(z_agl: np.ndarray, sigma_ref: float) -> tuple[np.ndarray, float]:
    """-> (sigma_u(z) (N,), sigma_w)。"""
    sw = SIGMA_W_OVER_REF * sigma_ref
    hft = np.maximum(np.asarray(z_agl, np.float64) / FT, 10.0)
    return sw / (0.177 + 0.000823 * hft) ** 0.4, sw


def ground_fade_arr(z_agl: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(z_agl, np.float64) / GROUND_FADE_M, 0.0, 1.0)


class TurbBox:
    """f16 资产解码后的 float64 镜像，周期三线性采样（flat gather 8 角点）。"""

    def __init__(self, rgba: np.ndarray, dx: float = TURB_DX) -> None:
        nz, ny, nx = rgba.shape[:3]
        if not (nx == ny == nz):
            raise ValueError("turbulence box must be cubic")
        self.n = nx
        self.dx = float(dx)
        self.period = self.n * self.dx
        # f16 values are exact in float32 (half the gather bandwidth); products and sums are float64
        self.flat = np.ascontiguousarray(rgba[..., :3].astype(np.float32).reshape(-1, 3))

    @classmethod
    def from_volume(cls, vol) -> TurbBox:
        return cls(np.asarray(vol.data), float(vol.cell_m[0]))

    def sample(self, q: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
        """q (N, 3) 采样坐标（m，已减去 Dq）-> (N, 3)。8 角点一次 flat gather（g06 `g06_query_flat.py`），按 z、y、x 顺序加权求和。"""
        q = np.asarray(q, np.float64)
        m = q.shape[0]
        if out is None:
            out = np.empty((m, 3))
        n = self.n
        g = q / self.dx - 0.5
        f0 = np.floor(g)
        fr = g - f0
        i0 = np.mod(f0.astype(np.int64), n)
        i1 = i0 + 1
        i1[i1 == n] = 0
        ix = np.stack((i0[:, 0], i1[:, 0]), 1)
        iy = np.stack((i0[:, 1], i1[:, 1]), 1) * n
        iz = np.stack((i0[:, 2], i1[:, 2]), 1) * (n * n)
        idx = (iz[:, :, None, None] + iy[:, None, :, None] + ix[:, None, None, :]).reshape(m, 8)
        wx = np.stack((1.0 - fr[:, 0], fr[:, 0]), 1)
        wy = np.stack((1.0 - fr[:, 1], fr[:, 1]), 1)
        wz = np.stack((1.0 - fr[:, 2], fr[:, 2]), 1)
        w = (wz[:, :, None, None] * wy[:, None, :, None] * wx[:, None, None, :]).reshape(m, 8)
        G = np.take(self.flat, idx.ravel(), axis=0).reshape(m, 8, 3).astype(np.float64)
        np.copyto(out, np.matmul(w[:, None, :], G)[:, 0, :])
        return out


class DrydenBank:
    """每 slot 的 Dryden 标准化状态（z_u ∈ R，z_v、z_w ∈ R^2）。"""

    def __init__(self, capacity: int, rng: np.random.Generator) -> None:
        self.capacity = int(capacity)
        self.rng = rng
        self.zu = np.zeros(self.capacity)
        self.zv = np.zeros((self.capacity, 2))
        self.zw = np.zeros((self.capacity, 2))
        self.live = np.zeros(self.capacity, bool)
        self.out = np.zeros((self.capacity, 3))  # 最近一次 step 的 ENU 湍流（m/s，未乘近地淡入）

    def spawn(self, slots: np.ndarray) -> None:
        s = np.sort(np.asarray(slots, np.int64))
        if s.size == 0:
            return
        z = self.rng.standard_normal((s.size, 5))
        self.zu[s] = z[:, 0]
        self.zv[s] = z[:, 1:3]
        self.zw[s] = z[:, 3:5]
        self.live[s] = True
        self.out[s] = 0.0

    def despawn(self, slots: np.ndarray) -> None:
        s = np.asarray(slots, np.int64)
        self.live[s] = False
        self.out[s] = 0.0

    def sync(self, active_slots: np.ndarray) -> None:
        """活动集合变化：新 slot 平稳起步，离开的 slot 清除。"""
        want = np.zeros(self.capacity, bool)
        want[np.asarray(active_slots, np.int64)] = True
        gone = np.flatnonzero(self.live & ~want)
        if gone.size:
            self.despawn(gone)
        new = np.flatnonzero(want & ~self.live)
        if new.size:
            self.spawn(new)

    @staticmethod
    def _step2(z: np.ndarray, r: np.ndarray, nrm: np.ndarray) -> None:
        er = np.exp(-r)
        z1, z2 = z[:, 0].copy(), z[:, 1].copy()
        z[:, 0] = er * ((1.0 + r) * z1 + r * z2)
        z[:, 1] = er * (-r * z1 + (1.0 - r) * z2)
        e2 = np.exp(-2.0 * r)
        q11 = np.maximum(-np.expm1(-2.0 * r) - e2 * (2.0 * r + 2.0 * r * r), 0.0)
        q12 = 2.0 * r * r * e2
        q22 = np.maximum(-np.expm1(-2.0 * r) + e2 * (2.0 * r - 2.0 * r * r), 0.0)
        l11 = np.sqrt(q11)
        l21 = np.where(l11 > 0, q12 / np.where(l11 > 0, l11, 1.0), 0.0)
        l22 = np.sqrt(np.maximum(q22 - l21 * l21, 0.0))
        z[:, 0] += l11 * nrm[:, 0]
        z[:, 1] += l21 * nrm[:, 0] + l22 * nrm[:, 1]

    def step(self, slots: np.ndarray, z_agl: np.ndarray, v_rel: np.ndarray, sigma_ref: float, dt: float,
             e_mean: tuple[float, float]) -> None:
        """slots 升序；v_rel = U_mean − v (n, 3)；结果写 self.out[slots]（ENU）。"""
        s = np.asarray(slots, np.int64)
        if s.size == 0:
            return
        nrm = self.rng.standard_normal((s.size, 5))
        V = np.maximum(np.linalg.norm(v_rel, axis=1), DRYDEN_VMIN)
        hft = np.maximum(np.asarray(z_agl, np.float64) / FT, 10.0)
        Lw = hft * FT
        Lu = hft / (0.177 + 0.000823 * hft) ** 1.2 * FT
        su, sw = mil_sigma_arr(z_agl, sigma_ref)
        a = np.exp(-V * dt / Lu)
        zu = self.zu[s] * a + np.sqrt(np.maximum(1.0 - a * a, 0.0)) * nrm[:, 0]
        self.zu[s] = zu
        zv = self.zv[s]
        self._step2(zv, dt * V / Lu, nrm[:, 1:3])
        self.zv[s] = zv
        zw = self.zw[s]
        self._step2(zw, dt * V / Lw, nrm[:, 3:5])
        self.zw[s] = zw
        up = su * zu
        vp = 0.5 * su * (zv[:, 0] + math.sqrt(3.0) * zv[:, 1])
        wp = 0.5 * sw * (zw[:, 0] + math.sqrt(3.0) * zw[:, 1])
        ex, ey = e_mean
        nx, ny = -ey, ex
        self.out[s, 0] = up * ex + vp * nx
        self.out[s, 1] = up * ey + vp * ny
        self.out[s, 2] = wp
        self.live[s] = True

    def state(self) -> dict:
        return {"zu": self.zu.copy(), "zv": self.zv.copy(), "zw": self.zw.copy(), "live": self.live.copy(), "out": self.out.copy(),
                "rng": self.rng.bit_generator.state}

    def restore(self, st: dict) -> None:
        for k in ("zu", "zv", "zw", "live", "out"):
            np.copyto(getattr(self, k), np.asarray(st[k]))
        if st.get("rng") is not None:
            self.rng.bit_generator.state = st["rng"]


_ = math
