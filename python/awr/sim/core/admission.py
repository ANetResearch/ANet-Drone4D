"""生产者本地准入 ④–⑧（M08-FR-054、FR-055；M08 §6.10.2；ADR-016；AWR-12 §5.1.2）。

执行顺序即原因码优先级：④ 状态（生命周期 108、准入矩阵 101/105/106、登记在第 4 步的检查，例如 M09 的锁与预检）→
⑤ 租约（115、116、100）→ ⑥ 参数边界（110）→ ⑦ 后端能力（109）→ ⑧ 围栏粗校验（登记在第 8 步的检查，102）。
M08 不 import M09、M10：它们的检查经 `register_admission_check(step, name, fn)` 登记，`fn(req, ctx) -> AdmitResult | int | None`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from awr.contracts.enums import Lifecycle
from awr.contracts.reasons import Reason

from . import state_model as SM

__all__ = ["ADMIT_NAME_CODE", "MOTION_OPS", "NAV_OPS", "AdmitCtx", "AdmitReq", "AdmitResult", "run_registered"]

ADMIT_NAME_CODE = {"STATE": int(Reason.STATE), "SAFETY_ACTIVE": int(Reason.SAFETY_ACTIVE),
                   "DUPLICATE": int(Reason.DUPLICATE), "UNSUPPORTED": int(Reason.BACKEND_UNSUPPORTED)}
NAV_OPS = frozenset({"goto", "follow_path", "orbit", "velocity"})
MOTION_OPS = frozenset({"takeoff", "land", "goto", "follow_path", "orbit", "hover", "rtl", "velocity", "safety_stop",
                        "pause", "kill"})  # AWR-17 §7.6 的 10 个命令 + kill：第 ⑦ 步按 caps 检查


@dataclass
class AdmitReq:
    cid: str
    op: str
    slot: int
    uav: str
    args: dict[str, Any]
    principal: dict[str, Any]
    fs: int = 0
    sub: int = 0
    flags: int = 0
    polyline_enu_m: np.ndarray | None = None  # ⑧ 围栏粗校验的折线 [p, p_stop, goal]


@dataclass
class AdmitCtx:
    tick: int
    t_ns: int
    S: Any
    world: Any = None
    profiles: Any = None


@dataclass
class AdmitResult:
    code: int = 0
    detail: Any = None
    needs_fine: bool = False
    warnings: list[str] = field(default_factory=list)


def lifecycle_ok(lc: int) -> bool:
    return lc == Lifecycle.READY


def matrix_code(op: str, fs: int, sub: int, flags: int, agl: float) -> int:
    name = SM.admit(op, SM.FS(fs), sub, flags, has_task=False, agl=agl)
    if name is None:
        return 0
    return ADMIT_NAME_CODE.get(name, int(Reason.STATE))


def run_registered(checks, req: AdmitReq, ctx: AdmitCtx) -> AdmitResult:
    """依次执行登记的检查，返回第一个失败项；全部通过时合并 needs_fine 与 warnings。"""
    out = AdmitResult()
    for c in checks:
        r = c.fn(req, ctx)
        if r is None:
            continue
        if isinstance(r, int):
            r = AdmitResult(code=r)
        if r.code:
            return r
        out.needs_fine |= r.needs_fine
        out.warnings.extend(r.warnings)
    return out


def finite_vec(v: Any, n: int) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == n and all(isinstance(x, (int, float)) and math.isfinite(x) for x in v)
