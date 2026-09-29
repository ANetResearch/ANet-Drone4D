"""ReconstructionJob state machine as a pure function (M01 §6.7.1 R01-R15; M01-FR-013; AWR-12 §4.12 J01-J08). D1-core.

`transition(state, event, guards)` returns the target state, the actions the caller must perform and a reason code:
0 on success, 348 JOB_STATE_CONFLICT for an event that is not allowed in `state`, and the registered guard codes for
a rejected submit (115, 330, 331, 332, 123, 124, 333, 346). Pausing for the performance lock does not change the state
(R13, R14). Only FAILED leaves a terminal state (retry, R11).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..types import JOB_STATES, RECON_STAGES, RESUMABLE_CODES, TERMINAL_STATES

__all__ = ["EVENTS", "FATAL_CODES", "Guards", "Transition", "next_stage", "transition"]

EVENTS: tuple[str, ...] = ("submit", "start", "stage_done", "stage_skipped", "ir_invalid", "deep_invalid",
                           "error_recoverable", "error_fatal", "crash_detected", "retry", "cancel", "perf_lock", "perf_unlock",
                           "expire")
FATAL_CODES = frozenset({330, 334, 336, 337, 338, 339, 340, 341, 343})
WORKING = frozenset(RECON_STAGES)
QUEUE_MAX = 16


@dataclass(frozen=True)
class Guards:
    role: str = "operator"                 # actor role for submit / cancel / retry
    is_submitter: bool = True              # actor submitted the job (or is admin)
    params_ok: bool = True
    engine_available: bool = True
    target_ok: bool = True                 # id legal, not an existing or built-in world
    source_ready: bool = True
    target_conflict: bool = False          # another active job targets the same world
    queue_len: int = 0
    disk_ok: bool = True
    perf_locked: bool = False
    resume_stage: str = "PREPARING"        # first stage without a valid completion marker
    needs_segmentation: bool = False       # SEGMENTING cannot be skipped (real engines, V0.5)
    ir_ok: bool = True                     # validate_session(deep=False) clean at the end of GEOREFERENCING
    deep_ok: bool = True                   # validate_world(deep=True) clean at the end of PACKAGING
    error_code: int = 342
    resumable: bool = False                # job record flag (retry)
    expired: bool = False                  # resumable retention (7 days) elapsed


@dataclass(frozen=True)
class Transition:
    state: str | None
    actions: tuple[str, ...] = ()
    code: int = 0
    error: dict | None = field(default=None)

    @property
    def ok(self) -> bool:
        return self.code == 0


def next_stage(stage: str) -> str:
    i = RECON_STAGES.index(stage)
    return RECON_STAGES[i + 1] if i + 1 < len(RECON_STAGES) else "SUCCEEDED"


def _reject(state: str | None, code: int = 348) -> Transition:
    return Transition(state, (), code, None)


def _fail(stage: str, code: int, resumable: bool) -> Transition:
    cleanup = ("keep_staging", "keep_workdir") if resumable else ("cleanup_staging",)
    return Transition("FAILED", ("record_error", *cleanup, "emit_state"), 0,
                      {"code": code, "stage": stage, "resumable": resumable})


def transition(state: str | None, event: str, guards: Guards | None = None) -> Transition:
    g = guards or Guards()
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}")
    if state is not None and state not in JOB_STATES:
        raise ValueError(f"unknown state {state!r}")

    # R01 submit: only for a job that does not exist yet
    if event == "submit":
        if state is not None:
            return _reject(state)
        for bad, code in ((g.role not in ("operator", "admin"), 115), (not g.params_ok, 330), (not g.engine_available, 331),
                          (not g.target_ok, 332), (not g.source_ready, 123), (g.target_conflict, 124),
                          (g.queue_len >= QUEUE_MAX, 333), (not g.disk_ok, 346)):
            if bad:
                return _reject(None, code)
        return Transition("QUEUED", ("create_job", "assign_session_id", "enqueue", "emit_state"))
    if state is None:
        return _reject(None)

    # R12 cancel: QUEUED or any working stage, by the submitter or an admin
    if event == "cancel":
        if state in TERMINAL_STATES:
            return _reject(state)
        if not (g.is_submitter or g.role == "admin"):
            return _reject(state, 115)
        if state == "QUEUED":
            return Transition("CANCELLED", ("set_cancel_flag", "dequeue", "emit_state"))
        return Transition("CANCELLED", ("set_cancel_flag", "stop_at_checkpoint", "cleanup_staging", "cleanup_workdir_keep_log",
                                        "emit_state"))

    # R11 retry / R15 expire
    if state == "FAILED":
        if event == "retry":
            if not (g.is_submitter or g.role == "admin"):
                return _reject(state, 115)
            if not g.resumable or g.expired:
                return _reject(state)
            return Transition("QUEUED", ("attempt_plus_one", "enqueue", "emit_state"))
        if event == "expire" and g.resumable:
            return Transition("FAILED", ("cleanup_staging", "mark_not_resumable"), 0,
                              {"code": 344, "resumable": False, "detail": "EXPIRED"})
        return _reject(state)
    if state in TERMINAL_STATES:
        return _reject(state)

    # R02 start
    if state == "QUEUED":
        if event != "start" or g.perf_locked:
            return _reject(state)
        target = g.resume_stage if g.resume_stage in RECON_STAGES else "PREPARING"
        return Transition(target, ("make_dirs", "emit_state"))

    # working stages
    if event == "stage_done":
        if state == "GEOREFERENCING" and not g.ir_ok:                    # R06
            return _fail(state, 340, False)
        if state == "PACKAGING":
            if not g.deep_ok:
                return _fail(state, 343, False)
            return Transition("SUCCEEDED", ("write_marker", "publish", "emit_world_added", "emit_state"))   # R07
        return Transition(next_stage(state), ("write_marker", "emit_state"))                                # R03, R05
    if event == "stage_skipped":                                                                            # R04
        if state != "SEGMENTING" or g.needs_segmentation:
            return _reject(state)
        return Transition("INFERRING", ("write_marker_skipped", "emit_state"))
    if event == "ir_invalid":
        return _fail(state, 340, False) if state == "GEOREFERENCING" else _reject(state)
    if event == "deep_invalid":
        return _fail(state, 343, False) if state == "PACKAGING" else _reject(state)
    if event == "error_recoverable":                                                                        # R08
        code = g.error_code if g.error_code in RESUMABLE_CODES or g.error_code == 342 else 342
        return _fail(state, code, True)
    if event == "error_fatal":                                                                              # R09
        code = g.error_code if g.error_code in FATAL_CODES or g.error_code == 342 else 342
        return _fail(state, code, False)
    if event == "crash_detected":                                                                           # R10
        return _fail(state, 344, True)
    if event == "perf_lock":                                                                                # R13
        return Transition(state, ("pause_at_checkpoint", "emit_progress_paused"))
    if event == "perf_unlock":                                                                              # R14
        return Transition(state, ("resume",))
    return _reject(state)
