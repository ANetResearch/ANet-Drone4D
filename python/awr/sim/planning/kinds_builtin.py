"""D1 内置规划作业的登记（M10-FR-201；ADR-113 第 ② 条）。

把 `worker.py` 中 D1 的 11 种作业（原 `worker_main` 的 if/elif 分派）经 `register_plan_kind` 登记；处理函数本身不变，
因此同一输入的 `result_sha256` 与 D1 逐字节相同（M10-AC-201，`tests/planning/test_registry.py` 对拍 D1 黄金数据）。
预算取 `jobs.BUDGET_MS`；输入校验只检查 D2 新增的禁入体数（≤ 64，M10-FR-212）。本模块在 sim-core、plan-pool worker
与共享规划服务中都会被导入（`jobs.ensure_plan_kinds`），导入只做登记。
"""

from __future__ import annotations

import time
from typing import Any

from . import worker as W
from .jobs import (
    BUDGET_MS,
    PRIO_BACKGROUND,
    PRIO_INTERACTIVE,
    PRIO_START,
    PlanRequest,
    PlanResult,
    quota_check,
    register_plan_kind,
    result_digest,
)

__all__ = ["validate_keepouts"]


def _t0() -> float:
    t = W.job_t0()
    return t if t is not None else time.perf_counter()


def validate_keepouts(payload: dict) -> tuple[int, str, dict] | None:
    """D1 类别的输入配额（M10 §15.3.3 最后一行）：`payload.keepouts` ≤ 64。"""
    ko = payload.get("keepouts") if isinstance(payload, dict) else None
    if not ko:
        return None
    return quota_check({"keepouts": len(ko)})


def _warm(req: PlanRequest, world: Any) -> PlanResult:
    import numpy as np

    from . import smooth as SM

    SM.make_trajectory(np.array([[0, 0, 10.0], [10, 0, 10.0], [10, 10, 12.0]]), SM.Limits(), None)
    if req.payload.get("grid25"):
        W.grid25_for(world, float(req.payload.get("alt_min_agl_m", 20.0)))
    return PlanResult(req.job_id, "ok", (), None, {}, None, None, result_digest((), None))


def _path_valid(req: PlanRequest, world: Any) -> PlanResult:
    return W._do_path_valid(req, world)


def _transit(req: PlanRequest, world: Any) -> PlanResult:
    return W._do_transit(req, world, _t0())


def _follow_path(req: PlanRequest, world: Any) -> PlanResult:
    return W._do_follow_path(req, world, _t0())


def _generator(req: PlanRequest, world: Any) -> PlanResult:
    return W._do_generator(req, world, _t0())


def _preview(req: PlanRequest, world: Any) -> PlanResult:
    return W._do_generator(req, world, _t0(), preview=True)


def _formation(req: PlanRequest, world: Any) -> PlanResult:
    if req.payload.get("op") == "assemble":
        return W._do_assemble(req, world, _t0())
    return W._do_generator(req, world, _t0())


def _resume(req: PlanRequest, world: Any) -> PlanResult:
    return W._do_resume(req, world, _t0())


def _deconflict(req: PlanRequest, world: Any) -> PlanResult:
    return W._do_deconflict(req, world)


_PRIO = {"warm": PRIO_BACKGROUND, "path_valid": PRIO_INTERACTIVE, "preview": PRIO_INTERACTIVE}
_FN = {"warm": _warm, "path_valid": _path_valid, "safe_transit": _transit, "astar25": _transit,
       "follow_path": _follow_path, "generator": _generator, "coverage": _generator, "formation": _formation,
       "deconflict": _deconflict, "preview": _preview, "resume": _resume}

for _k, _f in _FN.items():
    register_plan_kind(_k, _f, owner="M10", budget_ms=BUDGET_MS[_k], validate_input=validate_keepouts,
                       priority_default=_PRIO.get(_k, PRIO_START))
