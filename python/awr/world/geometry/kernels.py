"""M04 热核：`los_batch` 的 numba 实现（D2，AWR-04 §4.2 第 4 条、§6.3；D2-MS2 必做项）。

与 numpy 参考实现 `DsmWorldQuery.los_batch_np` **逐位一致**（同一组样点、同一运算顺序、`fastmath = False`）：

    L  = sqrt((dx·dx + dy·dy) + dz·dz)                     （与 numpy `linalg.norm(axis=1)` 的求和顺序相同，已对拍）
    n  = max(2, ceil(max(L − 2·eps, 0) / step) + 1)，e = eps / L（L > 0）
    s_j = e + (1 − 2e)·j / (n − 1)，q_j = A + (B − A)·s_j，j = 0 … n − 1
    遮挡当且仅当存在 q_j 落在 DSM 范围内且 z_j ≤ dsm_eff[格]；2·eps ≥ L 时可见

差别只在于命中第一个遮挡样点即停，并且不分配 O(样点数) 的中间数组。每条射线的 `≤ 64` 对上限、步长与 eps 的语义不变
（M04-FR-014）。

装载约定（避免在 sim-core 主循环内编译，D1 验收第 1 轮 4.1 的教训）：

- 本模块只在 `los_batch` 首次需要或 `warmup()` 时导入；`warmup()` 以运行期的唯一签名（A、B 为 C 连续 float64 二维，
  DSM 为只读 C 连续 float32 二维，输出 bool 一维）编译或读取缓存（`cache=True`，目录随 `NUMBA_CACHE_DIR`）；
- `los_batch()` 在核未预热时返回 None，调用方退回 numpy 参考实现（结果相同）；因此任何进程都不会在查询路径上编译；
- `DsmWorldQuery` 构造时若进程已导入 numba（sim-core、plan-pool worker）即同步预热，numba 未导入的进程（api 之外的
  工具、World 构建）不为此付出导入成本；
- 环境变量 `AWR_KERNEL=numpy` 关闭 numba 路径（与 M09 热核相同的开关）；输入含非有限值、步长或 eps 非有限、单条射线
  样点数超过 `MAX_SAMPLES` 时也退回 numpy 路径（保持其既有行为）。
"""

from __future__ import annotations

import math
import os
import time

import numpy as np

try:  # numba 0.67.0（ADR-038）
    from numba import njit

    HAVE_NUMBA = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover - 取决于环境
    HAVE_NUMBA = False

    def njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f
        return deco if not (a and callable(a[0])) else a[0]

__all__ = ["HAVE_NUMBA", "MAX_SAMPLES", "is_ready", "los_batch", "readonly", "warmup"]

MAX_SAMPLES = 50_000_000      # 单条射线样点数上限（5000 m 射线在 1 m 步长下为 5000；超出时交给 numpy 路径）
_STATE = {"ready": False, "warm_ms": None, "error": None}


@njit(cache=True, fastmath=False, nogil=True)
def _los_kernel(A, B, dsm, x0, y0, cell, step, eps, out):  # pragma: no cover - numba 编译执行（numpy 对拍覆盖）
    """返回 0 成功；1 输入含非有限值；2 样点数超限（调用方退回 numpy 路径）。"""
    H = dsm.shape[0]
    W = dsm.shape[1]
    n_pairs = A.shape[0]
    for i in range(n_pairs):
        for k in range(3):
            if not (math.isfinite(A[i, k]) and math.isfinite(B[i, k])):
                return 1
    for i in range(n_pairs):
        ax = A[i, 0]
        ay = A[i, 1]
        az = A[i, 2]
        dx = B[i, 0] - ax
        dy = B[i, 1] - ay
        dz = B[i, 2] - az
        L = math.sqrt((dx * dx + dy * dy) + dz * dz)
        if not (2.0 * eps < L):
            out[i] = True
            continue
        span = L - 2.0 * eps
        if span < 0.0:
            span = 0.0
        q = span / step
        if not (q < MAX_SAMPLES):
            return 2
        n = math.ceil(q) + 1
        if n < 2:
            n = 2
        e = eps / L if L > 0.0 else 0.0
        c1 = 1.0 - 2.0 * e
        den = n - 1
        vis = True
        for j in range(n):
            s = e + c1 * j / den
            x = ax + dx * s
            y = ay + dy * s
            z = az + dz * s
            fx = (x - x0) / cell
            fy = (y - y0) / cell
            if fx >= 0.0 and fx < W and fy >= 0.0 and fy < H and z <= dsm[int(fy), int(fx)]:
                vis = False
                break
        out[i] = vis
    return 0


def readonly(a: np.ndarray) -> np.ndarray:
    """只读视图（不复制）：使 DSM 的 numba 类型恒为 `readonly array(float32, 2d, C)`，与预热签名一致。"""
    v = np.ascontiguousarray(a, np.float32).view()
    v.flags.writeable = False
    return v


def warmup() -> bool:
    """以运行期签名编译或读取缓存；返回核是否可用（失败只记录，查询退回 numpy 路径）。"""
    if _STATE["ready"] or not HAVE_NUMBA:
        return bool(_STATE["ready"])
    t0 = time.perf_counter()
    try:
        dsm = readonly(np.array([[0.0, 0.0, 0.0, 0.0], [0.0, 30.0, 30.0, 0.0], [0.0, 0.0, 0.0, 0.0]], np.float32))
        A = np.array([[1.0, 1.0, 10.0], [1.0, 3.0, 50.0]])
        B = np.array([[7.0, 3.0, 10.0], [7.0, 3.0, 50.0]])
        out = np.empty(2, np.bool_)
        st = _los_kernel(A, B, dsm, 0.0, 0.0, 2.0, 1.0, 0.5, out)
        if st != 0 or bool(out[0]) or not bool(out[1]):
            raise RuntimeError(f"los kernel self-check failed: status {st}, out {out.tolist()}")
        _STATE.update(ready=True, warm_ms=round((time.perf_counter() - t0) * 1000.0, 1), error=None)
    except Exception as e:  # pragma: no cover - 取决于环境
        _STATE.update(ready=False, error=f"{type(e).__name__}: {e}")
    return bool(_STATE["ready"])


def is_ready() -> bool:
    return bool(_STATE["ready"])


def state() -> dict:
    return dict(_STATE, have_numba=HAVE_NUMBA)


def los_batch(A: np.ndarray, B: np.ndarray, dsm_ro: np.ndarray, x0: float, y0: float, cell: float, step_m: float,
              eps_m: float) -> np.ndarray | None:
    """numba 路径；核未预热、输入不适用时返回 None（调用方用 numpy 参考实现，结果相同）。"""
    if not _STATE["ready"]:
        return None
    step = float(step_m)
    eps = float(eps_m)
    if not (math.isfinite(step) and math.isfinite(eps)) or step <= 0.0:
        return None
    A = np.ascontiguousarray(A, np.float64)
    B = np.ascontiguousarray(B, np.float64)
    out = np.empty(A.shape[0], np.bool_)
    st = _los_kernel(A, B, dsm_ro, float(x0), float(y0), float(cell), step, eps, out)
    return out if st == 0 else None
