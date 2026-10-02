"""cmd_watch 大机群路径与逐行实现等价（ADR-060；M08-FR-057）。

在途调用 ≥ `VEC_MIN_ROWS` 时，`watch_tick` 的完成判据（`_done_np`）与停滞、截止、进度段（`_watch_rows_np`）改为向量预筛，
产生事件的行仍按行号升序逐行处理。同一命令脚本（36 架：takeoff、goto、有限圈与持续 orbit、follow_path、hover、rtl、
land、pause/resume）分别强制走逐行路径与向量路径，`cmd.*` 事件序列（种类、cid、原因码、effect、进度）、调用表的判据计时
与机群状态逐位一致。另验证进度限流：单次 cmd_watch 至多发出 `PROGRESS_MAX_PER_TICK` 条 `cmd.progress`。
"""

from __future__ import annotations

import numpy as np
import pytest
from simlib import CoreHarness

from awr.sim.core import command as CE
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import registry as R

N = 36
T_END_S = 75.0


def _script(ids: list[str]) -> dict[float, list[tuple[str, dict, str]]]:
    s: dict[float, list[tuple[str, dict, str]]] = {1.0: [("takeoff", {"alt_m": 12.0 + (k % 3)}, v) for k, v in enumerate(ids)]}
    work = []
    for k, v in enumerate(ids):
        x0, y0 = 12.0 * k, 0.0
        kind = k % 6
        if kind == 0:
            work.append(("goto", {"pos": [x0 + 6.0, y0 + 25.0, 14.0]}, v))
        elif kind == 1:
            work.append(("orbit", {"center": [x0, y0 + 10.0, 14.0], "radius_m": 4.0, "turns": 1, "speed_mps": 2.0}, v))
        elif kind == 2:
            work.append(("orbit", {"center": [x0, y0 + 10.0, 14.0], "radius_m": 4.0, "turns": 0, "speed_mps": 2.0}, v))
        elif kind == 3:
            work.append(("follow_path", {"waypoints": [[x0, 15.0, 14.0], [x0 + 4.0, 25.0, 15.0], [x0, 30.0, 14.0]]}, v))
        elif kind == 4:
            work.append(("hover", {}, v))
        else:
            work.append(("goto", {"pos": [x0, y0 + 40.0, 16.0]}, v))
    s[20.0] = work
    s[24.0] = [("pause", {}, ids[5]), ("pause", {}, ids[11])]
    s[30.0] = [("resume", {}, ids[5]), ("resume", {}, ids[11])]
    s[55.0] = [("rtl", {}, v) for v in ids[0:6]] + [("land", {}, v) for v in ids[6:12]]
    return s


def _run(vec_min: int, monkeypatch: pytest.MonkeyPatch, kernel: bool = False) -> tuple[list[tuple], dict[str, bytes], int]:
    monkeypatch.setattr(CE, "VEC_MIN_ROWS", vec_min)
    with R.isolated_registry() as reg:
        h = CoreHarness(n=N, reg=reg, spacing=12.0)
        try:
            h.core.engine.watch_kernel = kernel  # numpy 向量实现或 numba 融合核（kernels_watch，FX2-R2）
            ev: list[tuple] = []
            orig = h.core.events.emit
            per_tick: dict[int, int] = {}

            def emit(kind: str, **kw):
                if kind.startswith("cmd."):
                    ev.append((kind, kw.get("cid"), kw.get("code"), repr(kw.get("effect")), repr(kw.get("progress"))))
                    if kind == "cmd.progress":
                        t = int(kw.get("t_sim_ns") or 0)
                        per_tick[t] = per_tick.get(t, 0) + 1
                return orig(kind, **kw)

            h.core.events.emit = emit  # type: ignore[method-assign]
            ids = h.ids()
            script = _script(ids)
            done: set[float] = set()
            hacked = False
            tb = h.core.engine.table
            while h.t < T_END_S:
                for ts, cmds in script.items():
                    if ts not in done and h.t >= ts:
                        done.add(ts)
                        for op, args, uav in cmds:
                            h.cmd(op, args, uav=uav)
                if not hacked and h.t >= 22.0:
                    hacked = True
                    # 两条运行中的 goto：一条构造停滞（参照点置为当前位置、计时回拨 6 s → 203），一条截止已过（202）
                    r = int(tb.by_slot[h.slot(ids[17]), 0])
                    tb.stall_ref[r] = h.S.enu.pos[h.slot(ids[17])]
                    tb.stall_t[r] -= 6_000_000_000
                    tb.deadline[int(tb.by_slot[h.slot(ids[23]), 0])] = 0
                h.W[0] += 2 * TICK_NS
                h.core.iterate()
            tb = h.core.engine.table
            state = {k: np.ascontiguousarray(getattr(tb, k)).tobytes()
                     for k in ("live", "status", "hold_since", "stall_ref", "stall_t", "stall_rref", "stall_rmove",
                               "deadline", "max_dev", "prog_wall", "seen_landed")}
            S = h.S
            state |= {"p": S.p.tobytes(), "v": S.v.tobytes(), "ctrl_mode": S.ctrl_mode.tobytes()}
            return ev, state, max(per_tick.values(), default=0)
        finally:
            h.close()


@pytest.mark.parametrize("kernel", [False, True], ids=["numpy", "numba"])
def test_vector_path_equals_row_path(monkeypatch: pytest.MonkeyPatch, kernel: bool) -> None:
    from awr.sim.fleet import kernels_l1 as K

    if kernel and not K.HAVE_NUMBA:
        pytest.skip("numba unavailable")
    ev_row, st_row, peak_row = _run(10**9, monkeypatch)
    ev_vec, st_vec, peak_vec = _run(0, monkeypatch, kernel=kernel)
    kinds = {k for k, *_ in ev_row}
    assert {"cmd.accepted", "cmd.running", "cmd.succeeded", "cmd.progress", "cmd.failed"} <= kinds, kinds
    codes = {code for k, _c, code, *_ in ev_row if k == "cmd.failed"}
    assert {int(CE.Reason.STALLED), int(CE.Reason.PROGRESS_TIMEOUT)} <= codes, codes
    assert sum(1 for k, *_ in ev_row if k == "cmd.succeeded") >= N, "script should complete most calls"
    assert len(ev_row) == len(ev_vec)
    for a, b in zip(ev_row, ev_vec, strict=True):
        assert a == b
    for k in st_row:
        assert st_row[k] == st_vec[k], k
    assert peak_row == peak_vec and peak_row <= CE.PROGRESS_MAX_PER_TICK


@pytest.mark.parametrize("kernel", [False, True])
def test_progress_pick_caps_and_prefers_oldest(kernel: bool) -> None:
    """numpy 实现与 numba 实现（kernels_watch.progress_pick，FX2-R3）同一选择。"""
    from awr.sim.core.calls import ST_RUNNING, Call, CallTable

    class _Eng:
        pass

    eng = _Eng()
    eng.watch_kernel = kernel
    eng.table = CallTable(capacity=64, n_slots=64)
    tb = eng.table
    for k in range(40):
        c = Call(f"c{k}", "goto", k, f"u{k}", None, None, "test", {}, 0, 0)
        r = tb.alloc(c, t_ns=0, deadline_ns=10**12)
        tb.status[r] = ST_RUNNING
        tb.prog_wall[r] = 1000 - k  # 行号越大越久未发
    rows = tb.rows()
    pick = CE.CommandEngine._progress_pick(eng, rows, 10**10)  # type: ignore[arg-type]
    assert len(pick) == CE.PROGRESS_MAX_PER_TICK
    assert pick == frozenset(range(40 - CE.PROGRESS_MAX_PER_TICK, 40))
    tb.meta[39].batch_id = "b1"  # 批量调用不发进度（batch_id 在创建时确定；此处改写后按 meta 同步标志）
    tb.sync_flags(39)
    tb.paused[38] = True
    pick = CE.CommandEngine._progress_pick(eng, rows, 10**10)  # type: ignore[arg-type]
    assert 39 not in pick and 38 not in pick and len(pick) == CE.PROGRESS_MAX_PER_TICK
