"""Hungarian 分配（M10-FR-058；n03 §3.9；r26 §3.9）：`scipy.optimize.linear_sum_assignment` 的薄包装。

供 CAPT（成员 → 槽位）与覆盖分块（分块 → 机库）使用。n ≤ 200 时 ≤ 10 ms（本文实测 n = 200 为 8.2 ms）。

D2-FX-R2：`scipy.optimize.linear_sum_assignment` 即扩展模块 `scipy/optimize/_lsap` 的同名函数（scipy 原样再导出）；经包导入会执行
`scipy.optimize/__init__`，连带装载约 25 MB 匿名页（共享规划 worker 与会话 sim-core 的首个分配作业之后常驻，D2-AC-28/45）。这里直接
按文件装载该扩展模块（同一实现、同一结果，含并列代价时的选择），失败时退回包导入。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

__all__ = ["hungarian"]

_LSA: Callable[..., Any] | None = None


def _lsa() -> Callable[..., Any]:
    global _LSA
    if _LSA is not None:
        return _LSA
    fn: Callable[..., Any] | None = None
    try:
        import glob
        import importlib.util
        import os
        import sys

        opt = sys.modules.get("scipy.optimize")
        if opt is not None:                           # 已有进程完整导入了 scipy.optimize：直接用
            fn = opt.linear_sum_assignment
        else:
            import scipy

            so = sorted(glob.glob(os.path.join(os.path.dirname(scipy.__file__), "optimize", "_lsap*.so")))
            spec = importlib.util.spec_from_file_location("scipy.optimize._lsap", so[0]) if so else None
            if spec is not None and spec.loader is not None:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                fn = mod.linear_sum_assignment
    except Exception:  # pragma: no cover - scipy 布局变化：退回包导入
        fn = None
    if fn is None:
        from scipy.optimize import linear_sum_assignment as fn
    _LSA = fn
    return fn


def hungarian(C: np.ndarray) -> np.ndarray:
    """代价矩阵 C (n_rows, n_cols)，n_rows ≤ n_cols：返回每行分到的列号（int64，长度 n_rows）。"""
    linear_sum_assignment = _lsa()

    C = np.asarray(C, np.float64)
    if C.ndim != 2:
        raise ValueError("cost matrix must be 2-D")
    if C.shape[0] == 0:
        return np.zeros(0, np.int64)
    if not np.all(np.isfinite(C)):
        big = float(np.nanmax(np.where(np.isfinite(C), C, np.nan))) if np.isfinite(C).any() else 1.0
        C = np.where(np.isfinite(C), C, abs(big) * 1e6 + 1e6)
    r, c = linear_sum_assignment(C)
    out = np.full(C.shape[0], -1, np.int64)
    out[r] = c
    return out
