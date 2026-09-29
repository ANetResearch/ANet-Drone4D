"""ReconstructionJob state machine (M01-AC-007; M01-FR-013; M01 §6.7.1 R01-R15).

11 states x 14 events exhaustively against the transition table; disallowed transitions return 348; 100,000 random
events never reach an unknown state; terminal states are only left by retry from FAILED.
"""

from __future__ import annotations

import numpy as np
import pytest

from awr.contracts.enums import JOB_STATE
from awr.reconstruction.jobs.state import EVENTS, Guards, next_stage, transition
from awr.reconstruction.types import JOB_STATES, RECON_STAGES, TERMINAL_STATES

WORK = RECON_STAGES


def expected(state: str, event: str) -> str | None:
    """Target state under default guards (None = rejected with 348)."""
    if event == "submit":
        return None
    if event == "cancel":
        return None if state in TERMINAL_STATES else "CANCELLED"
    if state == "FAILED":
        return None                                   # default guards: not resumable -> retry and expire rejected
    if state in TERMINAL_STATES:
        return None
    if state == "QUEUED":
        return "PREPARING" if event == "start" else None
    return {"stage_done": "SUCCEEDED" if state == "PACKAGING" else next_stage(state),
            "stage_skipped": "INFERRING" if state == "SEGMENTING" else None,
            "ir_invalid": "FAILED" if state == "GEOREFERENCING" else None,
            "deep_invalid": "FAILED" if state == "PACKAGING" else None,
            "error_recoverable": "FAILED", "error_fatal": "FAILED", "crash_detected": "FAILED",
            "perf_lock": state, "perf_unlock": state}.get(event)


def test_states_match_contract_enum():
    assert tuple(JOB_STATE["recon"]) == JOB_STATES and len(JOB_STATES) == 11 and len(EVENTS) == 14


@pytest.mark.parametrize("state", JOB_STATES)
@pytest.mark.parametrize("event", EVENTS)
def test_exhaustive_table(state, event):
    tr = transition(state, event)
    want = expected(state, event)
    if want is None:
        assert tr.code == 348 and tr.state == state and tr.actions == (), (state, event, tr)
    else:
        assert tr.ok and tr.state == want, (state, event, tr)


def test_submit_guards_and_codes():
    assert transition(None, "submit").state == "QUEUED"
    assert transition(None, "start").code == 348
    cases = [(Guards(role="viewer"), 115), (Guards(params_ok=False), 330), (Guards(engine_available=False), 331),
             (Guards(target_ok=False), 332), (Guards(source_ready=False), 123), (Guards(target_conflict=True), 124),
             (Guards(queue_len=16), 333), (Guards(disk_ok=False), 346)]
    for g, code in cases:
        tr = transition(None, "submit", g)
        assert tr.code == code and tr.state is None
    assert transition(None, "submit", Guards(queue_len=15)).state == "QUEUED"


def test_failure_semantics():
    tr = transition("GEOREFERENCING", "stage_done", Guards(ir_ok=False))
    assert tr.state == "FAILED" and tr.error == {"code": 340, "stage": "GEOREFERENCING", "resumable": False}
    assert "cleanup_staging" in tr.actions
    tr = transition("PACKAGING", "stage_done", Guards(deep_ok=False))
    assert tr.state == "FAILED" and tr.error["code"] == 343
    tr = transition("TILING", "error_recoverable", Guards(error_code=335))
    assert tr.error == {"code": 335, "stage": "TILING", "resumable": True} and "keep_staging" in tr.actions
    tr = transition("INFERRING", "error_fatal", Guards(error_code=336))
    assert tr.error["code"] == 336 and not tr.error["resumable"]
    tr = transition("TILING", "crash_detected")
    assert tr.error == {"code": 344, "stage": "TILING", "resumable": True}
    assert transition("PACKAGING", "stage_done").actions[:3] == ("write_marker", "publish", "emit_world_added")


def test_retry_expire_cancel_start_guards():
    assert transition("FAILED", "retry", Guards(resumable=True)).state == "QUEUED"
    assert transition("FAILED", "retry", Guards(resumable=True, expired=True)).code == 348
    assert transition("FAILED", "retry", Guards(resumable=True, is_submitter=False, role="operator")).code == 115
    assert transition("FAILED", "retry", Guards(resumable=True, is_submitter=False, role="admin")).state == "QUEUED"
    ex = transition("FAILED", "expire", Guards(resumable=True))
    assert ex.state == "FAILED" and ex.error["detail"] == "EXPIRED" and "mark_not_resumable" in ex.actions
    assert transition("INFERRING", "cancel", Guards(is_submitter=False, role="operator")).code == 115
    assert transition("QUEUED", "cancel").actions[0] == "set_cancel_flag"
    assert transition("QUEUED", "start", Guards(perf_locked=True)).code == 348
    assert transition("QUEUED", "start", Guards(resume_stage="TILING")).state == "TILING"
    assert transition("SEGMENTING", "stage_skipped", Guards(needs_segmentation=True)).code == 348
    with pytest.raises(ValueError):
        transition("QUEUED", "explode")
    with pytest.raises(ValueError):
        transition("RUNNING", "start")


def test_random_sequences_stay_legal():
    rng = np.random.default_rng(7)
    events = 0
    while events < 100_000:
        state = transition(None, "submit").state
        for _ in range(int(rng.integers(1, 40))):
            ev = EVENTS[int(rng.integers(len(EVENTS)))]
            g = Guards(ir_ok=bool(rng.random() < 0.9), deep_ok=bool(rng.random() < 0.9), resumable=bool(rng.random() < 0.5),
                       expired=bool(rng.random() < 0.1), perf_locked=bool(rng.random() < 0.2),
                       resume_stage=WORK[int(rng.integers(len(WORK)))], error_code=int(rng.choice([335, 336, 338, 342, 344])))
            tr = transition(state, ev, g)
            events += 1
            assert tr.state in JOB_STATES
            if not tr.ok:
                assert tr.state == state
            if state in ("SUCCEEDED", "CANCELLED"):
                assert tr.state == state and not tr.ok
            if state == "FAILED" and tr.state != "FAILED":
                assert ev == "retry" and tr.state == "QUEUED"
            state = tr.state
