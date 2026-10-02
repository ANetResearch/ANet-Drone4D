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
import os
from typing import Any

import numpy as np

from ..weather.presets import C

try:  # 可选：小机群 env stage 的固定开销（M07 §9.4 numba 行；FX-SIM1）。不可用或 AWR_KERNEL=numpy 时走 numpy 路径
    from numba import njit as _njit

    _HAVE_NB = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover - 取决于环境
    _HAVE_NB = False

    def _njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f
        return deco if not (a and callable(a[0])) else a[0]


@_njit(cache=True, fastmath=False)
def _sample_nb(q, dx, n, flat, out):
    """周期三线性采样（与 `TurbBox.sample` 的 numpy 路径同一公式与求和顺序：角点按 z、y、x 展开的 8 项顺序累加）。"""
    for k in range(q.shape[0]):
        gx = q[k, 0] / dx - 0.5
        gy = q[k, 1] / dx - 0.5
        gz = q[k, 2] / dx - 0.5
        fx0 = np.floor(gx)
        fy0 = np.floor(gy)
        fz0 = np.floor(gz)
        rx = gx - fx0
        ry = gy - fy0
        rz = gz - fz0
        ix0 = np.int64(fx0) % n
        iy0 = np.int64(fy0) % n
        iz0 = np.int64(fz0) % n
        ix1 = ix0 + 1
        if ix1 == n:
            ix1 = 0
        iy1 = iy0 + 1
        if iy1 == n:
            iy1 = 0
        iz1 = iz0 + 1
        if iz1 == n:
            iz1 = 0
        a0 = 0.0
        a1 = 0.0
        a2 = 0.0
        for c in range(8):
            bz = c >> 2
            by = (c >> 1) & 1
            bx = c & 1
            wz = rz if bz else 1.0 - rz
            wy = ry if by else 1.0 - ry
            wx = rx if bx else 1.0 - rx
            w = wz * wy * wx
            j = (iz1 if bz else iz0) * (n * n) + (iy1 if by else iy0) * n + (ix1 if bx else ix0)
            a0 += w * np.float64(flat[j, 0])
            a1 += w * np.float64(flat[j, 1])
            a2 += w * np.float64(flat[j, 2])
        out[k, 0] = a0
        out[k, 1] = a1
        out[k, 2] = a2

__all__ = ["SIGMA_W_OVER_REF", "DrydenBank", "TurbBox", "bilinear_cc", "mil_sigma_arr", "vk_box", "warmup", "wind_fused"]


@_njit(cache=True, fastmath=False)
def _wind_fused_nb(pos, use_dtm, dtm, w, h, x0, y0, cell, ground_z, kind, z_ref, z0, d, alpha, den, spd, ex, ey, w_mean,
                   turb_mode, su_num, sw, fade_m, ft, dq, dx, nbox, flat, dry, agent, wm, wg, wt, wo, flags, valid, calm,
                   calm_mps, q_tmp, b_tmp):
    """env 查询"只求风"（WIND_PARTS、GLOBAL、无阵风事件）的融合实现：地面高（DTM 格心双线性，float32 舍入）、风廓线、
    MIL-F-8785C 高度缩放、近地衰减、湍流盒采样或 Dryden 输出、三项合成与 CALM 标志一次遍历完成。每一步与
    `EnvironmentServiceImpl.query` 的 numpy 路径同一公式与运算次序（逐位相同，tests/environment/test_wind_fused.py 对拍）。
    kind：0 log、1 power、2 uniform；turb_mode：0 无湍流、1 湍流盒、2 Dryden。"""
    n = pos.shape[0]
    hx = float(max(w - 1, 0))
    hy = float(max(h - 1, 0))
    cmax = max(w - 2, 0)
    rmax = max(h - 2, 0)
    dc = 1 if w >= 2 else 0
    dr = w if h >= 2 else 0
    for k in range(n):
        if use_dtm:  # 同 `_bilinear_cc_nb`，结果按 M04 `ground_dtm` 舍入为 float32 再转 float64
            gx = (pos[k, 0] - x0) / cell - 0.5
            gy = (pos[k, 1] - y0) / cell - 0.5
            gx = 0.0 if gx < 0.0 else (hx if gx > hx else gx)
            gy = 0.0 if gy < 0.0 else (hy if gy > hy else gy)
            c0 = min(int(gx), cmax)
            r0 = min(int(gy), rmax)
            tx = gx - c0
            ty = gy - r0
            i = r0 * w + c0
            v00 = dtm[i]
            v01 = dtm[i + dc]
            v10 = dtm[i + dr]
            v11 = dtm[i + dr + dc]
            zg = np.float64(np.float32((v00 * (1 - tx) + v01 * tx) * (1 - ty) + (v10 * (1 - tx) + v11 * tx) * ty))
        else:
            zg = ground_z
        z = pos[k, 2] - zg
        if kind == 2:
            f = 1.0 if z > 0 else 0.0
        elif kind == 1:
            f = (max(z, 0.0) / z_ref) ** alpha
        else:
            f = math.log((z - d) / z0) / den if z > d + z0 else 0.0
        m0 = spd * f * ex
        m1 = spd * f * ey
        wm[k, 0] = m0
        wm[k, 1] = m1
        wm[k, 2] = w_mean
        wg[k, 0] = 0.0
        wg[k, 1] = 0.0
        wg[k, 2] = 0.0
        t0 = 0.0
        t1 = 0.0
        t2 = 0.0
        if turb_mode != 0:
            hft = max(z / ft, 10.0)
            su = su_num / (0.177 + 0.000823 * hft) ** 0.4
            g = min(max(z / fade_m, 0.0), 1.0)
            if turb_mode == 1:
                q_tmp[0, 0] = pos[k, 0] - dq[0]
                q_tmp[0, 1] = pos[k, 1] - dq[1]
                q_tmp[0, 2] = pos[k, 2] - dq[2]
                _sample_nb(q_tmp, dx, nbox, flat, b_tmp[k:k + 1])
                t0 = su * b_tmp[k, 0] * g
                t1 = su * b_tmp[k, 1] * g
                t2 = sw * b_tmp[k, 2] * g
            else:
                a = agent[k]
                t0 = dry[a, 0] * g
                t1 = dry[a, 1] * g
                t2 = dry[a, 2] * g
        wt[k, 0] = t0
        wt[k, 1] = t1
        wt[k, 2] = t2
        wo[k, 0] = (m0 + 0.0) + t0
        wo[k, 1] = (m1 + 0.0) + t1
        wo[k, 2] = (w_mean + 0.0) + t2
        fl = valid
        if math.hypot(m0, m1) < calm_mps:
            fl = fl | calm
        flags[k] = fl


def wind_fused(pos: np.ndarray, dtm: tuple | None, ground_z: float, prof: dict, spd: float, ex: float, ey: float, w_mean: float,
               turb_mode: int, sigma_ref: float, dq: np.ndarray | None, box: TurbBox | None, dry: np.ndarray | None,
               agent: np.ndarray | None, o: Any, valid: int, calm: int, calm_mps: float) -> bool:
    """`_wind_fused_nb` 的包装：numba 不可用、位置非有限或廓线类型未知时返回 False（调用方走 numpy 路径）。"""
    if not _HAVE_NB:
        return False
    kind = {"log": 0, "power": 1, "uniform": 2}.get(str(prof["kind"]))
    if kind is None:
        return False
    pos = np.ascontiguousarray(pos, np.float64)
    if not np.isfinite(pos).all():
        return False
    n = pos.shape[0]
    d, z0 = float(prof["d_m"]), float(prof["z0_m"])
    z_ref = float(prof["z_ref_m"])
    den = math.log((z_ref - d) / z0) if kind == 0 else 1.0
    if dtm:
        f64, w, h, x0, y0, cell = dtm
        use_dtm = True
    else:
        f64, w, h, x0, y0, cell = _EMPTY_F64, 1, 1, 0.0, 0.0, 1.0
        use_dtm = False
    sw = SIGMA_W_OVER_REF * float(sigma_ref)
    if turb_mode == 1 and box is not None:
        dqa, dxb, nb, flat = np.ascontiguousarray(dq, np.float64), float(box.dx), np.int64(box.n), box.flat
    else:
        dqa, dxb, nb, flat = _ZERO3, 1.0, np.int64(1), _FLAT1
    dr = dry if (turb_mode == 2 and dry is not None) else _DRY0
    ag = np.ascontiguousarray(agent, np.int64) if (turb_mode == 2 and agent is not None) else _AG0
    tmp = np.empty((n, 3))
    _wind_fused_nb(pos, use_dtm, f64, int(w), int(h), float(x0), float(y0), float(cell), float(ground_z), kind, z_ref, z0, d,
                   float(prof["alpha"]), den, float(spd), float(ex), float(ey), float(w_mean), int(turb_mode), sw, sw,
                   GROUND_FADE_M, FT, dqa, dxb, nb, flat, dr, ag, o.wind_mean_mps, o.wind_gust_mps, o.wind_turb_mps,
                   o.wind_mps, o.flags, np.uint8(valid), np.uint8(calm), float(calm_mps), np.empty((1, 3)), tmp)
    return True


@_njit(cache=True, fastmath=False)
def _bilinear_cc_nb(x, y, f, w, h, x0, y0, cell, out):
    """格心双线性（界外钳制）：与 M04 `Grid.bilinear`（`_bilinear_scalar` 与 numpy 路径）逐位相同的运算次序。"""
    hx = float(max(w - 1, 0))
    hy = float(max(h - 1, 0))
    cmax = max(w - 2, 0)
    rmax = max(h - 2, 0)
    dc = 1 if w >= 2 else 0
    dr = w if h >= 2 else 0
    for k in range(x.shape[0]):
        gx = (x[k] - x0) / cell - 0.5
        gy = (y[k] - y0) / cell - 0.5
        gx = 0.0 if gx < 0.0 else (hx if gx > hx else gx)
        gy = 0.0 if gy < 0.0 else (hy if gy > hy else gy)
        c0 = min(int(gx), cmax)
        r0 = min(int(gy), rmax)
        tx = gx - c0
        ty = gy - r0
        i = r0 * w + c0
        v00 = f[i]
        v01 = f[i + dc]
        v10 = f[i + dr]
        v11 = f[i + dr + dc]
        out[k] = (v00 * (1 - tx) + v01 * tx) * (1 - ty) + (v10 * (1 - tx) + v11 * tx) * ty


def bilinear_cc(xy: np.ndarray, f64: np.ndarray, w: int, h: int, x0: float, y0: float, cell: float) -> np.ndarray | None:
    """DTM 格心双线性（float64 结果；与 M04 `ground_dtm` 未转 float32 之前的值逐位相同）。numba 不可用或输入非有限时
    返回 None（调用方走 M04 路径）。env stage 50 Hz 对全部机体求地面高，此前 M04 numpy 路径约 0.2 ms（N = 1000）。"""
    if not _HAVE_NB:
        return None
    x = np.ascontiguousarray(xy[:, 0], np.float64)
    y = np.ascontiguousarray(xy[:, 1], np.float64)
    if not (np.isfinite(x).all() and np.isfinite(y).all()):
        return None
    out = np.empty(x.shape[0])
    _bilinear_cc_nb(x, y, f64, int(w), int(h), float(x0), float(y0), float(cell), out)
    return out


def warmup() -> bool:
    """湍流盒采样核的 numba 预热（运行期签名：q float64 C、dx float64、n int64、flat float32 C、out float64 C）。
    sim-core 装配 env 插件时调用，使编译或读缓存发生在 ready 之前，而不是在主循环的首个 env tick 内（D1 验收第 1 轮 4.1）。"""
    if not _HAVE_NB:
        return False
    q = np.full((2, 3), 1.0)
    flat = np.zeros((8, 3), np.float32)
    _sample_nb(q, 4.0, np.int64(2), flat, np.empty((2, 3)))
    bilinear_cc(np.zeros((2, 2)), np.zeros(4), 2, 2, 0.0, 0.0, 10.0)
    from ..query import EnvSampleSoA

    o = EnvSampleSoA.alloc(2)
    prof = {"kind": "log", "z_ref_m": 10.0, "z0_m": 0.5, "d_m": 0.0, "alpha": 0.25}
    for mode in (0, 1, 2):
        for dtm in ((np.zeros(4), 2, 2, 0.0, 0.0, 10.0), None):
            wind_fused(np.full((2, 3), 1.0), dtm, 0.0, prof, 1.0, 1.0, 0.0, 0.0, mode, 1.0, np.zeros(3),
                       TurbBox(np.zeros((2, 2, 2, 4), np.float16), 4.0), np.zeros((4, 3)), np.arange(2), o, 1, 2, 1e-6)
    return True

TURB_N = int(C["turb_n"])
TURB_DX = float(C["turb_dx_m"])
TURB_L = float(C["turb_l_m"])
TURB_PERIOD = float(C["turb_period_m"])
GROUND_FADE_M = float(C["ground_fade_m"])
DRYDEN_VMIN = float(C["dryden_min_airspeed_mps"])
FT = 0.3048
_EMPTY_F64 = np.zeros(1)
_ZERO3 = np.zeros(3)
_FLAT1 = np.zeros((1, 3), np.float32)
_DRY0 = np.zeros((1, 3))
_AG0 = np.zeros(1, np.int64)
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
        if _HAVE_NB and np.isfinite(q).all():
            _sample_nb(np.ascontiguousarray(q), self.dx, self.n, self.flat, out)
            return out
        return self._sample_np(q, out)

    def _sample_np(self, q: np.ndarray, out: np.ndarray) -> np.ndarray:
        m = q.shape[0]
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
        # 8 个角点按固定顺序累加（与 numba 路径逐位相同；不用 matmul：BLAS 的求和顺序随实现而变）
        acc = np.zeros((m, 3))
        for c in range(8):
            acc += w[:, c, None] * G[:, c, :]
        np.copyto(out, acc)
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
