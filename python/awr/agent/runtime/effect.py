"""效果构造、聚合、钳制与两轴合法组合（M14 §6.6、§6.7.3；M14-FR-016、FR-017、FR-031）。

- `verified()` 是唯一判定（`Effect.verified`）；quirk、提供方自报、`service` 模块 V1 不得抬升（`clamp_trust`）。
- 两轴合法组合表（§6.6）：其余组合视为缺陷，`legal_combination()` 供单测与 TaskManager 自检断言。
- EffectRecord 构造（§6.7.3）：metrics、artifacts 取自效果；tests、resources、effects 取自处理器记录的执行过程。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from .tsir import EffectRecord
from .types import Effect, EffectStatus, TaskState

__all__ = ["LEGAL", "clamp_trust", "completed_ok", "effect_record", "legal_combination", "unavailable", "unverified"]

# 任务状态 → 允许的效果状态（None 表示效果为空）
LEGAL: dict[TaskState, frozenset] = {
    TaskState.SUBMITTED: frozenset({None}),
    TaskState.WORKING: frozenset({None, EffectStatus.UNVERIFIED}),
    TaskState.INPUT_REQUIRED: frozenset({None, EffectStatus.UNVERIFIED, EffectStatus.FAILED, EffectStatus.UNAVAILABLE,
                                         EffectStatus.OK}),
    TaskState.COMPLETED: frozenset({EffectStatus.OK}),
    TaskState.FAILED: frozenset({EffectStatus.FAILED, EffectStatus.UNVERIFIED, EffectStatus.UNAVAILABLE, EffectStatus.OK}),
    TaskState.CANCELED: frozenset({EffectStatus.UNVERIFIED, None}),
    TaskState.REJECTED: frozenset({None, EffectStatus.UNAVAILABLE}),
}
REJECT_CODES = frozenset({471, 473, 474, 475})


def legal_combination(state: TaskState, effect: Effect | None, *, predicate_ok: bool | None = None,
                      scope_ok: bool | None = None, reason_code: int = 0) -> bool:
    st = None if effect is None else effect.status
    if st not in LEGAL[state]:
        return False
    if state is TaskState.COMPLETED:
        return effect is not None and effect.verified() and predicate_ok is True and scope_ok is True
    if state is TaskState.INPUT_REQUIRED and st is EffectStatus.OK:
        # OK 只出现在谓词为假、范围违反或未验证且重试耗尽时（保留最近一次委派的效果）
        return not completed_ok(effect, predicate_ok, scope_ok)
    if state is TaskState.FAILED:
        return reason_code != 0
    if state is TaskState.REJECTED:
        return reason_code in REJECT_CODES
    return True


def completed_ok(effect: Effect | None, predicate_ok: bool | None, scope_ok: bool | None) -> bool:
    """completed 当且仅当 verified(effect) ∧ 谓词为真 ∧ 负向范围未违反（FR-016）。"""
    return effect is not None and effect.verified() and predicate_ok is True and scope_ok is True


def clamp_trust(e: Effect, *, verify_max: int, simulated_allowed: bool) -> Effect:
    """请求方钳制：verify_trust 不超过声明上限；非仿真通道不得自称 simulated（不抬升，只降低）。"""
    vt = min(int(e.verify_trust), int(verify_max))
    sim = bool(e.simulated) and simulated_allowed
    if vt == e.verify_trust and sim == e.simulated:
        return e
    return replace(e, verify_trust=vt, simulated=sim)


def unavailable(message: str, **kw: Any) -> Effect:
    return Effect(EffectStatus.UNAVAILABLE, message=message, **kw)


def unverified(**kw: Any) -> Effect:
    return Effect(EffectStatus.UNVERIFIED, **kw)


def effect_record(effect: Effect, *, tests: Mapping[str, bool] | None = None,
                  resources: Sequence[Mapping[str, Any]] = (), effects: Sequence[Mapping[str, Any]] = ()) -> EffectRecord:
    """§6.7.3：由效果与执行过程构造验收记录。tests 为 {test_id: passed}。"""
    return EffectRecord(
        metrics=dict(effect.metrics),
        artifacts=[a.to_dict() for a in effect.artifacts],
        tests=[{"id": k, "status": 1 if v else 2} for k, v in sorted((tests or {}).items())],
        resources=[dict(r) for r in resources],
        effects=[dict(x) for x in effects],
    )
