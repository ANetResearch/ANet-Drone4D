"""可见格覆盖校验核（M20-FR-022；M20 §6.6 第 5 步；M20-NFR-002：1 km²、250 000 格 ≤ 2 s）。

对每个格 x（2 m）：候选帧 = 每条航段（条带或补拍鼓包的直线段，段两端与段内每隔 b 触发）上沿航迹最近的触发点；PRD 写"最近条带及其
两侧条带上 ≤ 3 帧"，对平行条带两者等价，补拍鼓包的斜段使"按横向距离取 3 条"漏判，因此逐段判定（几何不过即跳过，视线只对几何
通过者计算，任一帧通过即停止）。帧 k 覆盖 x 当且仅当

    h = z_cam − z_x > 0，|cross| ≤ h·tan(θ_c)，|along| ≤ h·tan(φ_a)，离轴角 atan(√(cross² + along²)/h) ≤ θ_max，
    GSD_k(x) = h·p'/f ≤ g，且 DSM 视线（水平 2 m 步长，≤ 17 个样点，0.5 m 容差）无遮挡。

`cover_cells` 在 numba 可用时用 njit 核（`cache=True`，首次调用编译），否则用分块的 numpy 实现（逐格逐条带向量化）；两者结果一致
（`tests/tasking/test_scan.py` 对拍）。`frame_cover` 为单帧（补拍视点）对给定格集合的覆盖矩阵（numpy）。
"""

from __future__ import annotations

import math
import os

import numpy as np

__all__ = ["CoverParams", "cover_cells", "cover_cells_numpy", "frame_cover", "have_numba"]

LOS_EPS_M = 0.5
MAX_SAMPLES = 16


class CoverParams:
    """覆盖判据参数：tan_c（横向半视场或 θ_max 的较小者）、tan_a（航向半视场）、tan_t（离轴上限）、gsd_k = p'/f、g、触发间距 b。"""

    __slots__ = ("b", "g", "gsd_k", "tan_a", "tan_c", "tan_t")

    def __init__(self, tan_c: float, tan_a: float, tan_t: float, gsd_k: float, g: float, b: float) -> None:
        self.tan_c, self.tan_a, self.tan_t, self.gsd_k, self.g, self.b = float(tan_c), float(tan_a), float(tan_t), float(gsd_k), float(g), float(b)


def have_numba() -> bool:
    if os.environ.get("AWR_KERNEL", "").lower() == "numpy":
        return False
    try:
        import numba  # noqa: F401
    except Exception:
        return False
    return True


_KERNEL = None


def _get_kernel():
    global _KERNEL
    if _KERNEL is not None:
        return _KERNEL
    import numba

    @numba.njit(cache=True, nogil=True)
    def kern(cx, cy, cz, seg, b, tan_c, tan_a, tan_t, gsd_k, g, dsm, x0, y0, res, out):  # pragma: no cover - numba
        n = cx.shape[0]
        S = seg.shape[0]
        H, W = dsm.shape
        for i in range(n):
            x = cx[i]
            y = cy[i]
            z = cz[i]
            ok = False
            for s in range(S):
                zc = seg[s, 2]
                h = zc - z
                if h <= 0.0 or h * gsd_k > g * (1.0 + 1e-9):
                    continue
                ux = seg[s, 5]
                uy = seg[s, 6]
                L = seg[s, 4]
                dx = x - seg[s, 0]
                dy = y - seg[s, 1]
                reach = h * tan_t
                al = dx * ux + dy * uy
                if al < -reach or al > L + reach or abs(dx * uy - dy * ux) > reach:
                    continue
                t = math.floor(al / b + 0.5) * b
                if t < 0.0:
                    t = 0.0
                if t > L:
                    t = L
                if abs(L - al) < abs(al - t):
                    t = L
                fx = seg[s, 0] + ux * t
                fy = seg[s, 1] + uy * t
                ax = seg[s, 7]
                ay = seg[s, 8]
                ex = x - fx
                ey = y - fy
                d_al = ex * ax + ey * ay
                cr = ex * ay - ey * ax
                if abs(cr) > h * tan_c or abs(d_al) > h * tan_a:
                    continue
                hd = math.sqrt(cr * cr + d_al * d_al)
                if hd > reach:
                    continue
                m = int(math.ceil(hd / 2.0))  # noqa: RUF046 - numba 核内显式整型
                if m > MAX_SAMPLES:
                    m = MAX_SAMPLES
                if m < 1:
                    m = 1
                blocked = False
                for k in range(1, m):
                    f = k / m
                    px = fx + (x - fx) * f
                    py = fy + (y - fy) * f
                    pz = zc + (z - zc) * f
                    c = int((px - x0) / res)
                    r = int((py - y0) / res)
                    if c < 0:
                        c = 0
                    if c >= W:
                        c = W - 1
                    if r < 0:
                        r = 0
                    if r >= H:
                        r = H - 1
                    if dsm[r, c] > pz + LOS_EPS_M:
                        blocked = True
                        break
                if not blocked:
                    ok = True
                    break
            out[i] = ok

    _KERNEL = kern
    return kern


def _segs(strips: np.ndarray) -> np.ndarray:
    """航段 S×5（x0, y0, x1, y1, z）或 S×7（另加画幅轴 ax, ay：云台偏航锁定在所属条带方向）→ S×9
    （x0, y0, z, _, L, ux, uy, ax, ay）。"""
    A = np.asarray(strips, np.float64)
    A = A.reshape(-1, A.shape[-1] if A.ndim == 2 else 5)
    d = A[:, 2:4] - A[:, 0:2]
    L = np.hypot(d[:, 0], d[:, 1])
    u = d / np.maximum(L, 1e-9)[:, None]
    ax = A[:, 5:7] if A.shape[1] >= 7 else u
    return np.c_[A[:, 0], A[:, 1], A[:, 4], np.zeros(len(A)), L, u[:, 0], u[:, 1], ax[:, 0], ax[:, 1]]


def cover_cells(cells: np.ndarray, strips: np.ndarray, p: CoverParams, dsm: np.ndarray, geo: tuple[float, float, float]) -> np.ndarray:
    """cells: M×3（x, y, z 顶面）；strips: S×5；dsm: 局部 DSM 栅格（第 0 行在南），geo = (x0, y0, res)。返回 bool[M]。"""
    cells = np.ascontiguousarray(cells, np.float64)
    seg = np.ascontiguousarray(_segs(strips))
    out = np.zeros(len(cells), np.bool_)
    if len(cells) == 0 or len(seg) == 0:
        return out
    if have_numba():
        kern = _get_kernel()
        kern(cells[:, 0].copy(), cells[:, 1].copy(), cells[:, 2].copy(), seg, float(p.b), p.tan_c, p.tan_a, p.tan_t, p.gsd_k, p.g,
             np.ascontiguousarray(dsm, np.float64), float(geo[0]), float(geo[1]), float(geo[2]), out)
        return out
    return cover_cells_numpy(cells, strips, p, dsm, geo)


def _los_ok(F: np.ndarray, X: np.ndarray, dsm: np.ndarray, geo: tuple[float, float, float]) -> np.ndarray:
    """帧位置 F（n×3）到格顶 X（n×3）的 DSM 视线（同核的采样口径）。"""
    hd = np.hypot(X[:, 0] - F[:, 0], X[:, 1] - F[:, 1])
    m = np.clip(np.ceil(hd / 2.0), 1, MAX_SAMPLES).astype(np.int64)
    ok = np.ones(len(F), bool)
    H, W = dsm.shape
    x0, y0, res = geo
    for k in range(1, MAX_SAMPLES):
        act = (k < m) & ok
        if not act.any():
            break
        f = k / m[act]
        P = F[act] + (X[act] - F[act]) * f[:, None]
        c = np.clip(((P[:, 0] - x0) / res).astype(np.int64), 0, W - 1)
        r = np.clip(((P[:, 1] - y0) / res).astype(np.int64), 0, H - 1)
        blocked = dsm[r, c] > P[:, 2] + LOS_EPS_M
        idx = np.flatnonzero(act)
        ok[idx[blocked]] = False
    return ok


def cover_cells_numpy(cells: np.ndarray, strips: np.ndarray, p: CoverParams, dsm: np.ndarray, geo: tuple[float, float, float],
                      chunk: int = 2048) -> np.ndarray:
    """与 numba 核同一判据：逐格对全部航段的最近触发帧做几何判定，几何通过者按横向距离升序做视线，任一通过即覆盖。"""
    seg = _segs(strips)
    M = len(cells)
    out = np.zeros(M, bool)
    S = len(seg)
    if M == 0 or S == 0:
        return out
    for s0 in range(0, M, chunk):
        X = cells[s0:s0 + chunk]
        n = len(X)
        dx = X[:, None, 0] - seg[None, :, 0]
        dy = X[:, None, 1] - seg[None, :, 1]
        al = dx * seg[None, :, 5] + dy * seg[None, :, 6]
        L = np.broadcast_to(seg[None, :, 4], al.shape)
        h = seg[None, :, 2] - X[:, None, 2]
        t = np.clip(np.floor(al / p.b + 0.5) * p.b, 0.0, L)
        t = np.where(np.abs(L - al) < np.abs(al - t), L, t)
        ex = X[:, None, 0] - (seg[None, :, 0] + seg[None, :, 5] * t)
        ey = X[:, None, 1] - (seg[None, :, 1] + seg[None, :, 6] * t)
        d_al = ex * seg[None, :, 7] + ey * seg[None, :, 8]
        cr = ex * seg[None, :, 8] - ey * seg[None, :, 7]
        geo_ok = (h > 0) & (h * p.gsd_k <= p.g * (1 + 1e-9)) & (np.abs(cr) <= h * p.tan_c) & (np.abs(d_al) <= h * p.tan_a)
        geo_ok &= np.hypot(cr, d_al) <= h * p.tan_t
        key = np.where(geo_ok, np.abs(cr), np.inf)
        order = np.argsort(key, axis=1, kind="stable")
        done = np.zeros(n, bool)
        rows = np.arange(n)
        for q in range(S):
            s = order[:, q]
            act = np.isfinite(key[rows, s]) & ~done
            if not act.any():
                if not np.isfinite(key[rows, s]).any():
                    break
                continue
            idx = np.flatnonzero(act)
            ss = s[idx]
            F = np.c_[seg[ss, 0] + seg[ss, 5] * t[idx, ss], seg[ss, 1] + seg[ss, 6] * t[idx, ss], seg[ss, 2]]
            los = _los_ok(F, X[idx], dsm, geo)
            done[idx[los]] = True
        out[s0:s0 + n] = done
    return out


def frame_cover(cells: np.ndarray, frames: np.ndarray, axis_rad: float, p: CoverParams, dsm: np.ndarray, geo: tuple[float, float, float]
                ) -> np.ndarray:
    """单帧（补拍视点，云台天底、画幅沿 axis 方向）覆盖矩阵 bool[n_frames, n_cells]。"""
    F = np.asarray(frames, np.float64).reshape(-1, 3)
    X = np.asarray(cells, np.float64).reshape(-1, 3)
    out = np.zeros((len(F), len(X)), bool)
    if len(F) == 0 or len(X) == 0:
        return out
    ux, uy = math.cos(axis_rad), math.sin(axis_rad)
    for k, f in enumerate(F):
        dx = X[:, 0] - f[0]
        dy = X[:, 1] - f[1]
        al = dx * ux + dy * uy
        cr = dx * uy - dy * ux
        h = f[2] - X[:, 2]
        ok = (h > 0) & (h * p.gsd_k <= p.g * (1 + 1e-9)) & (np.abs(cr) <= h * p.tan_c) & (np.abs(al) <= h * p.tan_a)
        ok &= np.hypot(cr, al) <= h * p.tan_t
        idx = np.flatnonzero(ok)
        if idx.size:
            los = _los_ok(np.repeat(f[None, :], idx.size, axis=0), X[idx], dsm, geo)
            out[k, idx[los]] = True
    return out


def pairs_cover(F: np.ndarray, X: np.ndarray, pf: np.ndarray, pc: np.ndarray, axis_rad: float, p: CoverParams, dsm: np.ndarray,
                geo: tuple[float, float, float], chunk: int = 200_000) -> np.ndarray:
    """成对判定：帧 F[pf[k]]（云台天底、画幅沿 axis）是否覆盖格 X[pc[k]]（补拍候选的批量覆盖，向量化 numpy）。"""
    out = np.zeros(len(pf), bool)
    if len(pf) == 0:
        return out
    ux, uy = math.cos(axis_rad), math.sin(axis_rad)
    for s0 in range(0, len(pf), chunk):
        a = pf[s0:s0 + chunk]
        c = pc[s0:s0 + chunk]
        Fa = F[a]
        Xc = X[c]
        dx = Xc[:, 0] - Fa[:, 0]
        dy = Xc[:, 1] - Fa[:, 1]
        al = dx * ux + dy * uy
        cr = dx * uy - dy * ux
        h = Fa[:, 2] - Xc[:, 2]
        ok = (h > 0) & (h * p.gsd_k <= p.g * (1 + 1e-9)) & (np.abs(cr) <= h * p.tan_c) & (np.abs(al) <= h * p.tan_a)
        ok &= np.hypot(cr, al) <= h * p.tan_t
        idx = np.flatnonzero(ok)
        if idx.size:
            los = _los_ok(Fa[idx], Xc[idx], dsm, geo)
            sub = np.zeros(len(a), bool)
            sub[idx[los]] = True
            out[s0:s0 + len(a)] = sub
    return out


def warmup() -> bool:
    """按运行期签名编译覆盖核（`cover_cells` 内全部转为连续 float64，签名唯一）：`make numba-warm`（`awr.sim.runtime.warm`）调用，
    避免首个 GSD 扫描作业在规划进程内编译（D2-ACC-1 第 4.7 节：运行期新增签名 `kernels_cover._get_kernel.<locals>.kern`）。"""
    if not have_numba():
        return False
    cells = np.array([[1.0, 1.0, 0.0]])
    strips = np.array([[0.0, 0.0, 10.0, 0.0, 40.0]])
    cover_cells(cells, strips, CoverParams(0.5, 0.4, 0.8, 1e-4, 0.05, 5.0), np.zeros((4, 4)), (-2.0, -2.0, 2.0))
    return True
