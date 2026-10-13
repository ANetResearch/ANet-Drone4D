"""D2.2-FX-world：`tools/sim/capture_city_watch.py` 录制后事件扫描的回归（纯函数，构造 WS 消息流做断言，不起真实
引擎）。覆盖两点：(1) 单条 `op=="event"` 与合并的 `op=="events"`（`items` 数组）都要被看到——原 `kinds` 统计只看了
前一种，会漏掉同一 tick 内与其他事件一起合并发送的 `task.state`；(2) `task.state → INFEASIBLE`/`FAILED` 被判为
“这次录制不能用”，`CANCELED` 不算（观看者连接本身不会取消任务）。"""

from __future__ import annotations

from tools.sim.capture_city_watch import _bad_task_states, _iter_events


def _task_state_event(seq: int, tid: str, to: str, *, frm: str = "RUNNING") -> dict:
    """实际 WS 事件对象的形状（`python/awr/api/rt/events.py` `EventRing.append`：
    `encode_event({..., "type": kind, "level": lv, ..., "data": data})`）：外层分类字段是 `type`，不是 `kind`；
    `data` 里的 `type` 是任务自己的类型（`relay_watch`），与外层 `type`（`task.state`）同名不同义。"""
    return {"seq": seq, "t_sim_ns": seq * 10**8, "t_wall_ns": "0", "type": "task.state", "level": 2,
            "producer": "sim-core", "uav": None, "cid": None,
            "data": {"tid": tid, "type": "relay_watch", "code": 581, "why": "los", "from": frm, "to": to}}


def test_iter_events_flattens_both_singular_and_plural_ops() -> None:
    singular = {"op": "event", **_task_state_event(1, "t-1", "RUNNING", frm="PLANNING")}
    plural = {"op": "events", "items": [_task_state_event(2, "t-2", "INFEASIBLE"), _task_state_event(3, "t-3", "DONE")]}
    other = {"op": "serverInfo", "world": {"id": "shenzhen"}}
    out = list(_iter_events([singular, plural, other]))
    assert [ev["seq"] for ev in out] == [1, 2, 3]


def test_bad_task_states_empty_when_everything_runs_normally() -> None:
    msgs = [{"op": "event", **_task_state_event(1, "t-1", "RUNNING", frm="PLANNING")},
            {"op": "events", "items": [_task_state_event(2, "t-2", "DONE")]}]
    assert _bad_task_states(msgs) == []


def test_bad_task_states_catches_infeasible_inside_a_batched_events_message() -> None:
    """关键回归：INFEASIBLE 这条和另一条事件被同一 tick 合并进 `op=="events"` 时，仍必须被发现——这正是六城录制里
    `relay_watch` 转为 INFEASIBLE 那一刻实际发生的情形（同一 tick 往往还有别的例行事件一起刷出）。"""
    msgs = [{"op": "events", "items": [_task_state_event(1, "t-1", "RUNNING", frm="PLANNING"),
                                       _task_state_event(2, "t-2", "INFEASIBLE")]}]
    bad = _bad_task_states(msgs)
    assert len(bad) == 1
    assert bad[0]["tid"] == "t-2"
    assert bad[0]["to"] == "INFEASIBLE"
    assert bad[0]["why"] == "los"


def test_bad_task_states_ignores_canceled() -> None:
    """CANCELED 不判为录制失败：观看者连接本身不会取消任务，出现也不代表生成或录制有问题。"""
    msgs = [{"op": "event", **_task_state_event(1, "t-1", "CANCELED")}]
    assert _bad_task_states(msgs) == []


def test_bad_task_states_catches_failed_too() -> None:
    msgs = [{"op": "event", **_task_state_event(1, "t-1", "FAILED")}]
    bad = _bad_task_states(msgs)
    assert len(bad) == 1 and bad[0]["to"] == "FAILED"
