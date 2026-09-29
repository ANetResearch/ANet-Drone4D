"""M14-AC-011：两轴与钳制（16 组 (status, verify_trust, simulated, predicate, scope) 组合；service 模块 V1 形态不能 completed）。"""

from __future__ import annotations

import itertools

import pytest
from fakes.schemas import errors

from awr.agent.runtime.effect import clamp_trust, completed_ok, effect_record, legal_combination
from awr.agent.runtime.types import Artifact, Effect, EffectStatus, TaskState

COMBOS = [*itertools.product([EffectStatus.OK, EffectStatus.UNVERIFIED], [1, 2], [False, True], [True, False], [True]),
          (EffectStatus.OK, 4, True, True, False), (EffectStatus.FAILED, 4, True, True, True)]


@pytest.mark.parametrize("status,vt,sim,pred,scope", COMBOS)
def test_verified_and_completed(status: EffectStatus, vt: int, sim: bool, pred: bool, scope: bool) -> None:
    e = Effect(status, verify_trust=vt, simulated=sim)
    want_verified = status is EffectStatus.OK and (vt >= 2 or sim)
    assert e.verified() is want_verified
    assert completed_ok(e, pred, scope) is (want_verified and pred and scope)


def test_service_module_v1_cannot_complete() -> None:
    e = Effect(EffectStatus.OK, verify_trust=1, simulated=False, protocol="anet.service")
    assert not e.verified()
    assert not completed_ok(e, True, True)
    assert not legal_combination(TaskState.COMPLETED, e, predicate_ok=True, scope_ok=True)


def test_clamp_never_raises() -> None:
    e = Effect(EffectStatus.OK, verify_trust=4, simulated=True)
    assert clamp_trust(e, verify_max=1, simulated_allowed=False).verify_trust == 1
    assert not clamp_trust(e, verify_max=1, simulated_allowed=False).simulated
    low = Effect(EffectStatus.OK, verify_trust=1)
    assert clamp_trust(low, verify_max=4, simulated_allowed=True).verify_trust == 1


def test_legal_table() -> None:
    ok = Effect(EffectStatus.OK, verify_trust=4, simulated=True)
    unv = Effect(EffectStatus.UNVERIFIED)
    assert legal_combination(TaskState.SUBMITTED, None)
    assert not legal_combination(TaskState.SUBMITTED, unv)
    assert legal_combination(TaskState.WORKING, unv) and not legal_combination(TaskState.WORKING, ok)
    assert legal_combination(TaskState.COMPLETED, ok, predicate_ok=True, scope_ok=True)
    assert not legal_combination(TaskState.COMPLETED, ok, predicate_ok=False, scope_ok=True)
    assert legal_combination(TaskState.INPUT_REQUIRED, ok, predicate_ok=False, scope_ok=True)
    assert legal_combination(TaskState.FAILED, unv, reason_code=477) and not legal_combination(TaskState.FAILED, unv)
    assert legal_combination(TaskState.REJECTED, None, reason_code=474)
    assert not legal_combination(TaskState.REJECTED, None, reason_code=478)
    assert legal_combination(TaskState.CANCELED, unv) and not legal_combination(TaskState.CANCELED, ok)


def test_effect_schema_and_record() -> None:
    e = Effect(EffectStatus.OK, verify_trust=4, auth_trust=1, simulated=True, metrics={"confidence": 0.9},
               artifacts=(Artifact("thermal/T-0001/7.pgm", 19215, "sha256:" + "0" * 64, "image/x-portable-graymap"),))
    assert errors("agent/effect.schema.json", e.to_dict()) == []
    assert Effect.from_dict(e.to_dict()) == e
    rec = effect_record(e, tests={"station_reached": True}, resources=[{"kind": 102, "id": "uav/p600-b1"}])
    assert rec.tests == [{"id": "station_reached", "status": 1}] and rec.artifacts[0]["size_bytes"] == 19215
    with pytest.raises(ValueError):
        Effect(EffectStatus.OK, metrics={"x": float("nan")})
    with pytest.raises(ValueError):
        Effect(EffectStatus.OK, verify_trust=5)
