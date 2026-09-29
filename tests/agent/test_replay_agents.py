"""M14-AC-036（agent-runtime 侧）：证据记录全部以同名 `agent.*` 事件发出（录制源）；由录制的事件重建的任务序列与实时逐条相同；
回放模式下写操作（R45 submit、R46 cancel）返回 118，查询照常。"""

from __future__ import annotations

import asyncio

from fakes.s3 import build_s3, run, start

from awr.agent.runtime.core import EVENT_LEVELS
from awr.agent.runtime.publisher import fit_tasks_frame
from awr.agent.runtime.rpc import RuntimeRpc


def _s3_with_recording():
    sched, fake, core = build_s3()
    rec: list[tuple[int, str, dict, int]] = []
    frames: list[dict] = []
    core.emit_fn = lambda k, d, lvl: rec.append((sched.now_ns(), k, dict(d), lvl))
    core.tm.on_change = lambda t: frames.append(core.tm.tasks_frame())

    async def main():
        await start(core, fake)
        await run(sched, fake, t_end_s=300.0, until=lambda: bool(core.tm.tasks) and all(t.terminal for t in core.tm.tasks.values()))

    asyncio.run(main())
    return core, rec, frames


def test_every_evidence_row_is_an_event_and_replay_matches() -> None:
    core, rec, frames = _s3_with_recording()
    n_rows = sum(len(lg.rows) for lg in core.ledgers.values())
    ev_rows = [r for r in rec if r[1] in EVENT_LEVELS]
    assert len(ev_rows) == n_rows
    assert all({"chain", "seq", "id"} <= set(d) for _t, _k, d, _l in ev_rows)
    # 回放：只用录制的事件重建任务状态序列
    replay: dict[str, list[str]] = {}
    for _t, k, d, _l in rec:
        if k == "agent.task.state":
            replay.setdefault(d["task_id"], []).append(d["to"])
    live: dict[str, list[str]] = {}
    for r in core.ledger(core.coord_aid).rows:
        if r["type"] == "agent.task.state":
            live.setdefault(r["payload"]["task_id"], []).append(r["payload"]["to"])
    assert replay == live and replay["T-0001"][-1] == "completed"
    levels = {k: lvl for _t, k, _d, lvl in rec}
    assert levels["agent.task.accepted"] == 1 and levels["agent.lease.acquired"] == 1
    # agent/tasks 帧：单帧 ≤ 16 KiB，最后一帧的任务状态即实时状态
    last, raw = fit_tasks_frame(frames[-1])
    assert len(raw) <= 16 * 1024 and last["tasks"][0]["state"] == "completed"


def test_replay_mode_write_118() -> None:
    _sched, fake, core = build_s3()
    rpc = RuntimeRpc(core)

    async def main():
        await start(core, fake)
        core.session_state = "replay"
        spec = {"capability": "thermal.imaging", "target_enu_m": [1.0, 2.0, None], "args": {}}
        r1 = rpc.handle_task({"v": 1, "op": "submit", "spec": spec, "principal": {"principal_id": "p-x"}})
        r2 = rpc.handle_cancel({"v": 1, "task_id": "T-0001"})
        q = rpc.handle_query({"v": 1, "op": "agents"})
        return r1, r2, q

    r1, r2, q = asyncio.run(main())
    assert r1["code"] == 118 and r2["code"] == 118 and q["code"] == 0 and len(q["items"]) == 4


def test_tasks_frame_size_limit() -> None:
    frame = {"version": 1, "t_sim_ns": 0, "tasks": [
        {"task_id": f"T-{i:04d}", "state": "completed" if i % 2 else "working", "reason": "x" * 400} for i in range(64)]}
    f, raw = fit_tasks_frame(frame)
    assert len(raw) <= 16 * 1024 and 0 < len(f["tasks"]) < 64
    assert sum(1 for t in f["tasks"] if t["state"] == "working") == 32  # 先丢最旧的终态任务
