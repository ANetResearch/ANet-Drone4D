"""均匀三次 B-spline 轨迹契约 `awr.traj.bspline.v1`（M10 §6.3.2；ADR-039；r25 §3.4、§4.1）。

    区间 i = floor(τ/ts)，u = τ/ts − i ∈ [0, 1)
    p(τ) = [1 u u² u³] · M · [Q_i, Q_{i+1}, Q_{i+2}, Q_{i+3}]ᵀ，M = 1/6·[[1,4,1,0],[−3,0,3,0],[3,−6,3,0],[−1,3,−3,1]]
    v 取 [0,1,2u,3u²]/ts；a 取 [0,0,2,6u]/ts²；定义域 τ ∈ [0, (N − 3)·ts]
    导数控制点：V_i = (Q_{i+1} − Q_i)/ts，A_i = (Q_{i+2} − 2Q_{i+1} + Q_i)/ts²（凸包：max‖v‖ ≤ max‖V_i‖）
    边界（Fast-Planner states2pts）：Q0 = p0 − ts·v0 + ts²/3·a0；Q1 = p0 − ts²/6·a0；Q2 = p0 + ts·v0 + ts²/3·a0
    EGO 映射：knots u_k = (k − 3)·ts；start_time = t0；pos_pts = ctrl_pts；yaw_pts 为空

内部 float64；线上 `ctrl_pts` 为 float32。本模块另提供 `uav/{id}/path` 的 `awr.blob.polyline4.v1` 编码（17 §6.5）。
"""

from __future__ import annotations

import hashlib
import math
import struct

import numpy as np

__all__ = ["M3", "MAX_CTRL_PTS", "SCHEMA", "TS_DEFAULT", "blob_polyline4", "ctrl_hull", "deriv_ctrl", "duration",
           "eval_bspline", "sample_adaptive", "split_ctrl", "states2pts", "to_ego", "traj_bytes", "traj_sha256"]

SCHEMA = "awr.traj.bspline.v1"
TS_DEFAULT = 0.5
MAX_CTRL_PTS = 4096
M3 = np.array([[1, 4, 1, 0], [-3, 0, 3, 0], [3, -6, 3, 0], [-1, 3, -3, 1]], np.float64) / 6.0
BLOB_MAGIC = b"AWRB"
BLOB_F32 = 2
BLOB_COMP_POLY4 = 4
POLY4_MAX = 4096


def duration(Q: np.ndarray, ts: float) -> float:
    return max(len(Q) - 3, 0) * float(ts)


def eval_bspline(Q: np.ndarray, ts: float, tq, deriv: int = 0) -> np.ndarray:
    """在 τ = tq（标量或 (k,)）处求值；deriv 0/1/2 为位置、速度、加速度。越界钳到定义域。"""
    Q = np.asarray(Q, np.float64)
    nseg = len(Q) - 3
    T = nseg * ts
    t = np.clip(np.atleast_1d(np.asarray(tq, np.float64)), 0.0, T)
    i = np.minimum((t / ts).astype(np.int64), nseg - 1)
    u = t / ts - i
    if deriv == 0:
        S = np.stack([np.ones_like(u), u, u * u, u ** 3], 1)
    elif deriv == 1:
        S = np.stack([np.zeros_like(u), np.ones_like(u), 2 * u, 3 * u * u], 1) / ts
    else:
        S = np.stack([np.zeros_like(u), np.zeros_like(u), 2 * np.ones_like(u), 6 * u], 1) / (ts * ts)
    Wt = S @ M3
    idx = i[:, None] + np.arange(4)[None, :]
    return np.einsum("tk,tkd->td", Wt, Q[idx])


def deriv_ctrl(Q: np.ndarray, ts: float) -> tuple[np.ndarray, np.ndarray]:
    Q = np.asarray(Q, np.float64)
    return np.diff(Q, axis=0) / ts, np.diff(Q, 2, axis=0) / (ts * ts)


def ctrl_hull(Q: np.ndarray, ts: float) -> dict:
    """导数控制点凸包上界：水平速度、上升/下降速度、加速度模长。"""
    V, A = deriv_ctrl(Q, ts)
    return {"v_xy": float(np.linalg.norm(V[:, :2], axis=1).max()) if len(V) else 0.0,
            "vz_up": float(max(V[:, 2].max(), 0.0)) if len(V) else 0.0,
            "vz_dn": float(max(-V[:, 2].min(), 0.0)) if len(V) else 0.0,
            "a": float(np.linalg.norm(A, axis=1).max()) if len(A) else 0.0}


def states2pts(p0, v0, a0, ts: float) -> np.ndarray:
    p0, v0, a0 = (np.asarray(x, np.float64) for x in (p0, v0, a0))
    return np.stack([p0 - ts * v0 + ts * ts / 3.0 * a0, p0 - ts * ts / 6.0 * a0, p0 + ts * v0 + ts * ts / 3.0 * a0])


def split_ctrl(Q: np.ndarray, max_pts: int = MAX_CTRL_PTS) -> list[np.ndarray]:
    """控制点超过上限时按段切分：第 k 块取 Q[i0 : i1 + 3]（相邻块重叠 3 个控制点），切点处 p、v、a 精确连续。"""
    Q = np.asarray(Q, np.float64)
    if len(Q) <= max_pts:
        return [Q]
    nseg = len(Q) - 3
    per = max_pts - 3
    out = []
    for i0 in range(0, nseg, per):
        i1 = min(nseg, i0 + per)
        out.append(Q[i0:i1 + 3].copy())
    return out


def to_ego(Q: np.ndarray, ts: float, t0_ns: int = 0, traj_id: int = 0) -> dict:
    """EGO `Bspline.msg` 映射（r25 §4.1）：order 3、knots u_k = (k − 3)·ts、pos_pts = ctrl_pts、yaw_pts 为空。"""
    Q = np.asarray(Q, np.float64)
    n = len(Q)
    return {"order": 3, "traj_id": int(traj_id), "start_time_ns": int(t0_ns),
            "knots": [(k - 3) * float(ts) for k in range(n + 4)], "pos_pts": Q.tolist(), "yaw_pts": [], "yaw_dt": 0.0}


def traj_bytes(Q: np.ndarray, ts: float) -> bytes:
    return struct.pack("<d", float(ts)) + np.ascontiguousarray(np.asarray(Q, "<f8")).tobytes()


def traj_sha256(Q: np.ndarray, ts: float) -> str:
    return hashlib.sha256(traj_bytes(Q, ts)).hexdigest()


def sample_adaptive(Q: np.ndarray, ts: float, *, max_ds_m: float = 5.0, max_turn_deg: float = 5.0,
                    max_pts: int = POLY4_MAX, dt_s: float = 0.1) -> np.ndarray:
    """按弧长 ≤ 5 m、转角 ≤ 5° 自适应抽样（17 §6.5 `uav/{id}/path`）：返回 (k, 4) float64 `(x, y, z, t_rel_s)`。"""
    T = duration(Q, ts)
    if T <= 0:
        p = np.asarray(Q, np.float64)[:1]
        return np.c_[p, [0.0]]
    t = np.arange(0.0, T + 1e-9, dt_s)
    if t[-1] < T - 1e-9:
        t = np.r_[t, T]
    P = eval_bspline(Q, ts, t)
    keep = [0]
    last = 0
    cos_lim = math.cos(math.radians(max_turn_deg))
    d_last = None
    acc = 0.0
    for k in range(1, len(P)):
        seg = P[k] - P[k - 1]
        L = float(np.linalg.norm(seg))
        acc += L
        turn = False
        if L > 1e-6:
            u = seg / L
            if d_last is not None and float(u @ d_last) < cos_lim:
                turn = True
        if acc >= max_ds_m or turn or k == len(P) - 1:
            keep.append(k)
            if L > 1e-6:
                d_last = (P[k] - P[last]) / max(float(np.linalg.norm(P[k] - P[last])), 1e-9)
            last = k
            acc = 0.0
    idx = np.asarray(sorted(set(keep)), np.int64)
    out = np.c_[P[idx], t[idx]]
    if len(out) > max_pts:
        sel = np.unique(np.r_[np.linspace(0, len(out) - 1, max_pts).round().astype(np.int64)])
        out = out[sel]
    return out


def blob_polyline4(pts4: np.ndarray) -> bytes:
    """`awr.blob.polyline4.v1`：16 B 头 `AWRB | u16 version=1 | u16 dtype=2 (f32) | u32 count | u32 comp=4`，
    其后为 count × 4 个 f32（x, y, z, t_rel_s）；空数组表示取消或结束。"""
    a = np.ascontiguousarray(np.asarray(pts4, "<f4").reshape(-1, 4))
    return BLOB_MAGIC + struct.pack("<HHII", 1, BLOB_F32, int(a.shape[0]), BLOB_COMP_POLY4) + a.tobytes()
