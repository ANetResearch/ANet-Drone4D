"""轨迹指标（M27-FR-106、FR-108、§6.7.7 `traj_metrics`；AWR-05 §5.4.6 RPE100 定义；r1 §4.4 `eval_sim.py`）。

坐标约定：真值为世界系（ENU）下的 IMU 位姿 `T_W_I(t)`（`gt_imu.tum`）；FAST-LIVO2 估计为其世界系 F（首帧 IMU 系）下的 IMU 位姿
`T_F_I(t)`（`Log/result/<seq>.txt`，TUM：`t x y z qx qy qz qw`）。

- 锚定：`T_W_F = T_W_I(t_e0) · T_F_I(t_e0)⁻¹`，即用首个估计时刻的真值位姿把估计放到世界系；锚定后的误差是纯里程计漂移
  （`drift_final_m`、`drift_max_m`）。
- 对齐 ATE：锚定后的估计位置与真值位置做 SE(3) Umeyama 刚体对齐（不估尺度），取 RMSE（`ate_aligned_rmse_m`，D3-AC-14 的"对齐 ATE"）。
- RPE100：沿真值路程 s（估计时刻上的真值位置累积弧长）取段起点 i（`stride_m > 0` 时每 stride_m 米取首个越过的样本，
  `stride_m = 0` 时每个估计样本，即 r1 口径），终点 j 为首个 `s[j] − s[i] ≥ seg_m` 的样本；在各自段起点的机体系比较相对平移
  （`R_g,i ᵀ Δp_g` 与 `R_w,i ᵀ Δp_w`，去除航向漂移），误差除以实际段长 `s[j] − s[i]`（r1 除以 100 m，差异 < 1%，M27 §20），
  以百分比表示。单次运行标量取段均值 `rpe100_pct`（与 D3-AC-14 阈值来源 r1 `rpe100_mean_pct` 一致），另报段中位数与最大值。
  缺省 `stride_m = 0`（逐估计样本起点）：r1 `s2c_livo_a`（1587 个 10 Hz 估计）上逐样本起点与 r1 相差 −0.32%（仅分母差异），
  M27 §6.7.7 原型的 1 m 步长相差 +2.4%（起点子集抽样造成），超出 M27-AC-024 的 1%。
- 真机回归（`positions_ate`）：只有 RTK 位置时，把估计位置按时间线性插值到 RTK 时刻（只取估计覆盖区间内的 RTK 样本），
  再做不估尺度的 Umeyama 对齐（r1 §3.3 `analyze_real.py`）。

本模块只依赖 numpy（作业宿主与评测容器共用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = [
    "R_to_quat",
    "TrajInterp",
    "Tum",
    "load_tum",
    "positions_ate",
    "quat_to_R",
    "save_tum",
    "segment_starts",
    "slerp",
    "traj_metrics",
    "umeyama",
]


# ---------------------------------------------------------------------------------------------------------------- 四元数
def quat_to_R(q: np.ndarray) -> np.ndarray:
    """[x, y, z, w]（n, 4）→ 旋转矩阵（n, 3, 3）；输入先归一化。"""
    q = np.asarray(q, np.float64).reshape(-1, 4)
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    R = np.empty((len(q), 3, 3))
    R[:, 0, 0] = 1 - 2 * (y * y + z * z)
    R[:, 0, 1] = 2 * (x * y - z * w)
    R[:, 0, 2] = 2 * (x * z + y * w)
    R[:, 1, 0] = 2 * (x * y + z * w)
    R[:, 1, 1] = 1 - 2 * (x * x + z * z)
    R[:, 1, 2] = 2 * (y * z - x * w)
    R[:, 2, 0] = 2 * (x * z - y * w)
    R[:, 2, 1] = 2 * (y * z + x * w)
    R[:, 2, 2] = 1 - 2 * (x * x + y * y)
    return R


def R_to_quat(R: np.ndarray) -> np.ndarray:
    """旋转矩阵（n, 3, 3）→ [x, y, z, w]（n, 4，w ≥ 0 半球）；Shepperd 法按最大对角分量分支。"""
    R = np.asarray(R, np.float64).reshape(-1, 3, 3)
    out = np.empty((len(R), 4))
    tr = R[:, 0, 0] + R[:, 1, 1] + R[:, 2, 2]
    diag = np.stack([R[:, 0, 0], R[:, 1, 1], R[:, 2, 2]], 1)
    for n in range(len(R)):
        m = R[n]
        if tr[n] > diag[n].max():
            s = 2.0 * np.sqrt(1.0 + tr[n])
            out[n] = [(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s]
        else:
            k = int(np.argmax(diag[n]))
            i, j = (k + 1) % 3, (k + 2) % 3
            s = 2.0 * np.sqrt(max(1.0 + m[k, k] - m[i, i] - m[j, j], 1e-300))
            v = np.empty(4)
            v[k] = 0.25 * s
            v[i] = (m[i, k] + m[k, i]) / s
            v[j] = (m[j, k] + m[k, j]) / s
            v[3] = (m[j, i] - m[i, j]) / s
            out[n] = v
    out /= np.linalg.norm(out, axis=1, keepdims=True)
    out[out[:, 3] < 0] *= -1
    return out


def slerp(q0: np.ndarray, q1: np.ndarray, u: np.ndarray) -> np.ndarray:
    """逐行球面插值（[x, y, z, w]）：q1 先翻到 q0 同半球，点积 > 0.9995 时线性插值后归一化。"""
    q0 = np.asarray(q0, np.float64).reshape(-1, 4)
    q1 = np.asarray(q1, np.float64).reshape(-1, 4)
    u = np.asarray(u, np.float64).reshape(-1)
    d = np.einsum("ij,ij->i", q0, q1)
    q1 = np.where((d < 0)[:, None], -q1, q1)
    d = np.abs(d)
    out = np.empty_like(q0)
    lin = d > 0.9995
    if lin.any():
        q = q0[lin] + u[lin, None] * (q1[lin] - q0[lin])
        out[lin] = q / np.linalg.norm(q, axis=1, keepdims=True)
    s = ~lin
    if s.any():
        th = np.arccos(np.minimum(d[s], 1.0))
        sn = np.sin(th)
        out[s] = (np.sin((1 - u[s]) * th) / sn)[:, None] * q0[s] + (np.sin(u[s] * th) / sn)[:, None] * q1[s]
    return out


# ---------------------------------------------------------------------------------------------------------------- TUM
@dataclass
class Tum:
    """TUM 轨迹：t（s，float64）、p（n, 3）、q（n, 4，[x, y, z, w]）。"""

    t: np.ndarray
    p: np.ndarray
    q: np.ndarray

    def __post_init__(self) -> None:
        self.t = np.asarray(self.t, np.float64).reshape(-1)
        self.p = np.asarray(self.p, np.float64).reshape(-1, 3)
        self.q = np.asarray(self.q, np.float64).reshape(-1, 4)
        if not len(self.t) == len(self.p) == len(self.q):
            raise ValueError("TUM columns have different lengths")

    def __len__(self) -> int:
        return len(self.t)

    def T(self) -> np.ndarray:
        """逐样本 4 × 4 位姿。"""
        T = np.tile(np.eye(4), (len(self), 1, 1))
        T[:, :3, :3] = quat_to_R(self.q)
        T[:, :3, 3] = self.p
        return T


def load_tum(path: Path | str) -> Tum:
    """读取 TUM 文本（空白分隔、`#` 注释）；按时刻稳定排序并去掉重复时刻（保留首次出现）。"""
    a = np.loadtxt(path, ndmin=2, comments="#")
    if a.size == 0:
        raise ValueError(f"{path}: empty trajectory")
    if a.shape[1] < 8:
        raise ValueError(f"{path}: TUM needs 8 columns, got {a.shape[1]}")
    order = np.argsort(a[:, 0], kind="stable")
    a = a[order]
    _, first = np.unique(a[:, 0], return_index=True)
    a = a[np.sort(first)]
    return Tum(a[:, 0], a[:, 1:4], a[:, 4:8])


def save_tum(path: Path | str, tr: Tum) -> Path:
    path = Path(path)
    np.savetxt(path, np.column_stack([tr.t, tr.p, tr.q]), fmt=["%.9f"] + ["%.6f"] * 3 + ["%.9f"] * 4)
    return path


class TrajInterp:
    """真值轨迹插值：位置逐轴线性、姿态球面插值；查询时刻夹到样本区间（r1 `Traj.at` 同口径）。"""

    def __init__(self, tr: Tum) -> None:
        if len(tr) < 2 or (np.diff(tr.t) <= 0).any():
            raise ValueError("truth trajectory needs >= 2 strictly increasing samples")
        self.t, self.p = tr.t, tr.p
        q = tr.q / np.linalg.norm(tr.q, axis=1, keepdims=True)
        for k in range(1, len(q)):                                        # 同半球连续
            if q[k] @ q[k - 1] < 0:
                q[k] = -q[k]
        self.q = q

    @property
    def span(self) -> tuple[float, float]:
        return float(self.t[0]), float(self.t[-1])

    def at(self, ts) -> tuple[np.ndarray, np.ndarray]:
        ts = np.clip(np.asarray(ts, np.float64).reshape(-1), self.t[0], self.t[-1])
        n = np.clip(np.searchsorted(self.t, ts, side="right") - 1, 0, len(self.t) - 2)
        u = (ts - self.t[n]) / (self.t[n + 1] - self.t[n])
        p = self.p[n] * (1 - u)[:, None] + self.p[n + 1] * u[:, None]
        return quat_to_R(slerp(self.q[n], self.q[n + 1], u)), p


# ---------------------------------------------------------------------------------------------------------------- 对齐
def umeyama(src: np.ndarray, dst: np.ndarray, with_scale: bool = False) -> tuple[np.ndarray, np.ndarray, float]:
    """最小二乘 `dst ≈ s · R · src + t`（Umeyama 1991）；返回 (R, t, s)，不估尺度时 s = 1。"""
    src = np.asarray(src, np.float64).reshape(-1, 3)
    dst = np.asarray(dst, np.float64).reshape(-1, 3)
    if len(src) < 3:
        raise ValueError("umeyama needs >= 3 point pairs")
    mu_s, mu_d = src.mean(0), dst.mean(0)
    S, D = src - mu_s, dst - mu_d
    U, sig, Vt = np.linalg.svd(D.T @ S / len(src))
    E = np.eye(3)
    if np.linalg.det(U @ Vt) < 0:
        E[2, 2] = -1
    R = U @ E @ Vt
    s = float((sig * np.diag(E)).sum() / ((S ** 2).sum() / len(src))) if with_scale else 1.0
    return R, mu_d - s * R @ mu_s, s


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)))


# ---------------------------------------------------------------------------------------------------------------- RPE
def segment_starts(s: np.ndarray, stride_m: float) -> np.ndarray:
    """段起点下标：`stride_m ≤ 0` 时每个样本；否则每 stride_m 米取首个路程 ≥ k·stride_m 的样本（去重、升序）。"""
    s = np.asarray(s, np.float64)
    if stride_m <= 0:
        return np.arange(len(s))
    marks = np.arange(0.0, s[-1] + 1e-9, float(stride_m))
    idx = np.searchsorted(s, marks - 1e-9, side="left")
    return np.unique(idx[idx < len(s)])


def traj_metrics(est: Tum, gt: Tum | TrajInterp, *, seg_m: float = 100.0, stride_m: float = 0.0) -> dict:
    """单次运行轨迹指标（§6.7.7）。返回值另含 `_T_W_F`（锚定变换）与 `_align`（Umeyama R、t），供地图评测使用。"""
    g = gt if isinstance(gt, TrajInterp) else TrajInterp(gt)
    lo, hi = g.span
    keep = (est.t >= lo) & (est.t <= hi)
    if keep.sum() < 3:
        raise ValueError("estimate does not overlap the truth trajectory")
    te, Te = est.t[keep], est.T()[keep]
    Rg, pg = g.at(te)
    T_W_I0 = np.eye(4)
    T_W_I0[:3, :3], T_W_I0[:3, 3] = Rg[0], pg[0]
    T_W_F = T_W_I0 @ np.linalg.inv(Te[0])
    Tw = T_W_F @ Te
    Rw, pw = Tw[:, :3, :3], Tw[:, :3, 3]
    e_anchor = np.linalg.norm(pw - pg, axis=1)
    R_al, t_al, _ = umeyama(pw, pg)
    e_al = np.linalg.norm(pw @ R_al.T + t_al - pg, axis=1)
    s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(pg, axis=0), axis=1))])
    rpe, seg_len = [], []
    j = 0
    for i in segment_starts(s, stride_m):
        j = max(j, int(i))
        while j < len(s) and s[j] - s[i] < seg_m:
            j += 1
        if j >= len(s):
            break
        L = s[j] - s[i]
        dg = Rg[i].T @ (pg[j] - pg[i])
        de = Rw[i].T @ (pw[j] - pw[i])
        rpe.append(float(np.linalg.norm(dg - de) / L * 100.0))
        seg_len.append(float(L))
    dR = np.einsum("nji,njk->nik", Rg, Rw)                                 # R_gᵀ R_w
    ang = np.degrees(np.arccos(np.clip((np.trace(dR, axis1=1, axis2=2) - 1.0) / 2.0, -1.0, 1.0)))
    yaw_g = np.unwrap(np.arctan2(Rg[:, 1, 0], Rg[:, 0, 0]))
    yaw_w = np.unwrap(np.arctan2(Rw[:, 1, 0], Rw[:, 0, 0]))
    out = {
        "n": len(te), "duration_s": float(te[-1] - te[0]), "length_m": float(s[-1]),
        "drift_final_m": float(e_anchor[-1]), "drift_max_m": float(e_anchor.max()),
        "drift_final_pct": float(e_anchor[-1] / s[-1] * 100.0) if s[-1] > 0 else None,
        "ate_aligned_rmse_m": _rms(e_al), "ate_aligned_max_m": float(e_al.max()), "ate_unaligned_rmse_m": _rms(e_anchor),
        "rot_err_final_deg": float(ang[-1]), "yaw_drift_final_deg": float(np.degrees(abs(yaw_w[-1] - yaw_g[-1]))),
        "rpe100_pct": float(np.mean(rpe)) if rpe else None,
        "rpe100_median_pct": float(np.median(rpe)) if rpe else None,
        "rpe100_max_pct": float(np.max(rpe)) if rpe else None,
        "n_segments": len(rpe), "seg_m": float(seg_m), "stride_m": float(stride_m),
        "seg_len_mean_m": float(np.mean(seg_len)) if seg_len else None,
        "_T_W_F": T_W_F, "_align": (R_al, t_al),
    }
    return out


def positions_ate(est: Tum, ref_t: np.ndarray, ref_p: np.ndarray) -> dict:
    """真机回归 ATE：估计位置线性插值到参考（RTK）时刻，不估尺度的 Umeyama 对齐（r1 §3.3）。"""
    ref_t = np.asarray(ref_t, np.float64).reshape(-1)
    ref_p = np.asarray(ref_p, np.float64).reshape(-1, 3)
    m = (ref_t >= est.t[0]) & (ref_t <= est.t[-1])
    if m.sum() < 3:
        raise ValueError("estimate does not overlap the reference positions")
    tq, pr = ref_t[m], ref_p[m]
    pi = np.stack([np.interp(tq, est.t, est.p[:, k]) for k in range(3)], 1)
    R, t, _ = umeyama(pi, pr)
    err = np.linalg.norm(pi @ R.T + t - pr, axis=1)
    length = float(np.linalg.norm(np.diff(pr, axis=0), axis=1).sum())
    return {"n_assoc": int(m.sum()), "duration_s": float(tq[-1] - tq[0]), "length_m": length, "ate_rmse_m": _rms(err),
            "ate_mean_m": float(err.mean()), "ate_max_m": float(err.max()), "final_err_m": float(err[-1]),
            "ate_rmse_pct_of_length": float(_rms(err) / length * 100.0) if length > 0 else None}
