"""规则分类（AWR-16 §5 第 4 条；M03-FR-015；x01 §3.7）→ `anet-classes@1` 紧凑索引。

地面 `hag < 1 ∧ |n_z| > 0.9` → 1；立面 `hag ≥ 1 ∧ |n_z| < 0.3` → 6；屋顶 `hag ≥ 2.5 ∧ |n_z| > 0.9` → 5；
低矮物 `1 ≤ hag < 2.5 ∧ |n_z| > 0.9` → 7；其余 → 0。四个条件两两不相交。
CSF 语义（`--semantic csf`）为 P2，D1 只有桩。
"""

from __future__ import annotations

import numpy as np

from .types import ConfigError

C_UNCLASSIFIED, C_GROUND, C_ROOF, C_FACADE, C_LOW_OBJECT = 0, 1, 5, 6, 7


def classify(N: np.ndarray, hag: np.ndarray) -> np.ndarray:
    nz = np.abs(N[:, 2])
    cls = np.full(len(hag), C_UNCLASSIFIED, np.uint8)
    cls[(hag >= 1.0) & (nz < 0.3)] = C_FACADE
    cls[(hag >= 1.0) & (hag < 2.5) & (nz > 0.9)] = C_LOW_OBJECT
    cls[(hag >= 2.5) & (nz > 0.9)] = C_ROOF
    cls[(hag < 1.0) & (nz > 0.9)] = C_GROUND
    return cls


def classify_csf(*_args, **_kwargs) -> np.ndarray:
    """CSF 语义（M03-FR-019，P2，V0.2 实现）。"""
    raise ConfigError("--semantic csf 为 P2（V0.2 提供），D1 默认使用规则语义")
