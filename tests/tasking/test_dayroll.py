"""S7 日切与软重置（M20-FR-074；AWR-04 §4.10、ADR-094；D2-AC-39、M20-AC-031；D2-FX-R2-other）。

- 日界按会话日历的当地 04:00（S7：Asia/Shanghai = UTC 20:00），不是 UTC 04:00（第 1 轮实现的日界落在北京 12:00）；
- 会话级：每个日界只日切一次，与是否有运行中的接力无关；日历被改写（cal0 或 tz 变化）时只重定日界、不归档；
- 日表行是当日值（与上一次日切的累计值之差），累计统计（`awr.relay.stats.v1`）不因清理当日交接记录而变化；
- 清理已结束且不再被架次引用的接力工作项与分配记录，在岗与接替的工作项保留；
- `relay.day_rollover` 事件给出被归档的当地日期、日界日历时刻与各任务的日表行；
- needs_data + slow：S7 批处理（sandbox100）从北京时间 03:58 开始跑 240 s，事件流中恰有一条日切事件。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from awr.environment.sky.calendar import Calendar
from awr.sim.tasking.book import ItemRT, SortieRT, TaskRT
from awr.sim.tasking.dayroll import DayRoll, boundary_after, day_label
from awr.sim.tasking.relay import RelayController, RelayRT
from awr.tasking.model import WorkItem

ROOT = Path(__file__).resolve().parents[2]
SH = "Asia/Shanghai"
S = 1_000_000_000


def _ms(y: int, mo: int, d: int, h: int, mi: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


class _Rt:
    """DayRoll 与 RelayController.stats_wire 用到的最小运行时。"""

    def __init__(self, cal0_ms: int, tz: str = SH) -> None:
        self.ctx = SimpleNamespace(env=SimpleNamespace(sky=SimpleNamespace(calendar=Calendar(cal0_ms, tz))))
        self.t_ns = 0
        self.tasks: dict[str, TaskRT] = {}
        self.nests: dict = {}
        self.sorties: dict[str, SortieRT] = {}
        self.events: list[tuple[str, dict]] = []

    @property
    def t(self) -> float:
        return self.t_ns * 1e-9

    def emit(self, kind: str, level: int = 0, uav: str | None = None, **data) -> None:
        self.events.append((kind, data))

    def rollovers(self) -> list[dict]:
        return [d for k, d in self.events if k == "relay.day_rollover"]


def _relay_task(rt: _Rt, tid: str = "t2") -> TaskRT:
    T = TaskRT(tid, {"type": "relay_watch", "params": {"target": "person-02"}}, state="RUNNING")
    T.relay = RelayRT(tid, "person-02")
    rt.tasks[tid] = T
    return T


def _item(T: TaskRT, k: int, state: str, uav: str | None = None) -> ItemRT:
    wid = f"{T.tid}.r{k}"
    it = ItemRT(WorkItem(wid, T.tid, "relay_station", pos=(0.0, 0.0, 60.0)), state=state, uav=uav)
    T.items[wid] = it
    if uav:
        T.assignments.append({"wid": wid, "uav": uav})
    return it


def _run(rt: _Rt, d: DayRoll, seconds: int, step_s: int = 1) -> None:
    for _ in range(0, seconds, step_s):
        rt.t_ns += step_s * S
        d.tick()


def test_boundary_is_local_0400_not_utc() -> None:
    t = _ms(2026, 10, 11, 17, 31) * 1_000_000                        # 北京 10-12 01:31
    assert boundary_after(t, SH) == _ms(2026, 10, 11, 20, 0) * 1_000_000   # 北京 10-12 04:00 = UTC 20:00
    assert day_label(boundary_after(t, SH), SH) == "2026-10-11"
    t2 = _ms(2026, 10, 11, 20, 0) * 1_000_000                        # 恰在日界：下一个日界为次日
    assert boundary_after(t2, SH) == _ms(2026, 10, 12, 20, 0) * 1_000_000
    la = "America/Los_Angeles"                                        # 夏令时结束日（11-01）：仍只有一个当地 04:00
    t3 = int(datetime(2026, 11, 1, 6, 0, tzinfo=UTC).timestamp() * 1e9)      # 洛杉矶 10-31 23:00（PDT）
    b3 = boundary_after(t3, la)
    assert datetime.fromtimestamp(b3 / 1e9, UTC).hour == 12 and day_label(b3, la) == "2026-10-31"   # 04:00 PST = 12:00 UTC


def test_rollover_once_per_local_day_and_not_at_utc_0400() -> None:
    rt = _Rt(_ms(2026, 10, 11, 19, 58))                                # 北京 03:58
    _relay_task(rt)
    d = DayRoll(rt)
    _run(rt, d, 3 * 60)                                               # 跨过北京 04:00
    (ev,) = rt.rollovers()
    assert ev["day"] == "2026-10-11" and ev["tz"] == SH and ev["n"] == 1
    assert ev["t_cal_unix_ns"] == str(_ms(2026, 10, 11, 20, 0) * 1_000_000)
    _run(rt, d, 9 * 3600, step_s=5)                                   # 跨过 UTC 04:00（北京 12:00）：不再日切
    assert len(rt.rollovers()) == 1
    _run(rt, d, 15 * 3600, step_s=5)                                  # 次日北京 04:00
    assert [e["day"] for e in rt.rollovers()] == ["2026-10-11", "2026-10-12"]


def test_calendar_rewrite_rebases_without_archiving() -> None:
    rt = _Rt(_ms(2026, 10, 11, 10, 0))
    _relay_task(rt)
    d = DayRoll(rt)
    _run(rt, d, 10)
    rt.ctx.env.sky.calendar = Calendar(_ms(2026, 10, 13, 22, 0), SH)   # env/calendar 跳到另一天的 06:00：不是 04:00 跨越
    _run(rt, d, 10)
    assert rt.rollovers() == []
    d.reset()                                                         # sim/reset 与恢复：重定日界
    _run(rt, d, 10)
    assert rt.rollovers() == []


def test_day_rows_are_daily_and_cumulative_stats_unchanged() -> None:
    rt = _Rt(_ms(2026, 10, 11, 19, 0))                                 # 北京 03:00
    T = _relay_task(rt)
    rc = RelayController(rt)
    r = T.relay
    r.t_monitor, r.captured_t, r.handovers, r.sorties = 3600.0, 3564.0, 2, 3
    r.gaps_n["battery"] = 1
    for ov in (17.0, 21.0):
        r.overlaps.append(ov)
        r.ov_n, r.ov_sum = r.ov_n + 1, r.ov_sum + ov
        r.ov_min = ov if r.ov_min is None else min(r.ov_min, ov)
    r.gap_max_day = 12.5
    before = rc.stats_wire(T)
    _run(rt, rc.days, 3601)                                          # 跨过北京 04:00
    (ev,) = rt.rollovers()
    (row,) = ev["rows"]
    assert row["tid"] == "t2" and row["day"] == "2026-10-11" and row["monitor_s"] == 3600.0 and row["coverage"] == 0.99
    assert row["handovers"] == 2 and row["overlap_min_s"] == 17.0 and row["overlap_mean_s"] == 19.0 and row["gap_max_s"] == 12.5
    assert row["gaps"]["battery"] == 1 and row["sorties"] == 3 and row["discovered"] == 0
    assert r.overlaps == [] and r.gap_max_day == 0.0
    assert r.day_table == [{k: v for k, v in row.items() if k not in ("tid", "target")}]
    after = rc.stats_wire(T)
    assert after["handovers"] == before["handovers"] == {"count": 2, "mean_overlap_s": 19.0, "min_overlap_s": 17.0}
    assert after["coverage"] == before["coverage"] and after["gaps"] == before["gaps"]
    # 第二天：只计当日增量
    r.t_monitor += 1000.0
    r.captured_t += 500.0
    r.handovers += 1
    r.overlaps.append(30.0)
    r.ov_n, r.ov_sum, r.ov_min = r.ov_n + 1, r.ov_sum + 30.0, 17.0
    _run(rt, rc.days, 86400, step_s=10)
    row2 = rt.rollovers()[-1]["rows"][0]
    assert row2["day"] == "2026-10-12" and row2["monitor_s"] == 1000.0 and row2["coverage"] == 0.5 and row2["handovers"] == 1
    assert row2["overlap_min_s"] == 30.0 and row2["gaps"]["battery"] == 0
    assert [x["day"] for x in r.day_table] == ["2026-10-11", "2026-10-12"]
    assert rc.stats_wire(T)["handovers"] == {"count": 3, "mean_overlap_s": round(68.0 / 3, 2), "min_overlap_s": 17.0}


def test_idle_relay_writes_no_row_but_event_is_emitted() -> None:
    rt = _Rt(_ms(2026, 10, 11, 19, 59))
    _relay_task(rt)
    d = DayRoll(rt)
    _run(rt, d, 120)
    (ev,) = rt.rollovers()
    assert ev["rows"] == [] and rt.tasks["t2"].relay.day_table == []


def test_prune_finished_items_not_referenced_by_sorties() -> None:
    rt = _Rt(_ms(2026, 10, 11, 19, 59))
    T = _relay_task(rt)
    _item(T, 1, "DONE", "p600-01")                                    # 已交接、架次已结束
    _item(T, 2, "DONE", "p600-02")                                    # 已交接、返航途中（架次仍引用）
    _item(T, 3, "CANCELED")                                           # 取消的接替工作项
    _item(T, 4, "ACTIVE", "p600-03")                                  # 在岗
    _item(T, 5, "PENDING")                                            # 待分配的接替
    rt.sorties["p600-02"] = SortieRT("p600-02", 1, "nest-a", "s7", tid="t2", wid="t2.r2", state="RTL")
    rev = T.revision
    d = DayRoll(rt)
    _run(rt, d, 120)
    (ev,) = rt.rollovers()
    assert sorted(T.items) == ["t2.r2", "t2.r4", "t2.r5"]
    assert [a["wid"] for a in T.assignments] == ["t2.r2", "t2.r4"]
    assert ev["pruned_items"] == 2 and ev["pruned_assignments"] == 1 and T.revision == rev + 1


def test_no_calendar_no_rollover() -> None:
    rt = _Rt(0)
    rt.ctx = SimpleNamespace(env=None)
    d = DayRoll(rt)
    _run(rt, d, 10)
    assert d.tick() is None and rt.events == []


@pytest.mark.needs_data
@pytest.mark.slow
def test_s7_batch_rolls_over_at_beijing_0400(tmp_path: Path) -> None:
    if not (ROOT / "worlds" / "synthcity" / "world.json").exists():
        pytest.skip("worlds/synthcity not built")
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "sandbox" / "s7_check.py"), "--until", "240", "--start-local", "03:58",
                        "--horizon-h", "24", "--out", str(tmp_path)], cwd=ROOT, capture_output=True, text=True, timeout=1500)
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines() if (tmp_path / "events.jsonl").exists() else []
    evs = [json.loads(x) for x in lines]
    roll = [e for e in evs if (e.get("kind") or e.get("type")) == "relay.day_rollover"]
    assert len(roll) == 1, p.stdout[-2000:] + p.stderr[-2000:]
    d = roll[0]["data"]
    t_cal = int(d["t_cal_unix_ns"])
    assert d["tz"] == SH and datetime.fromtimestamp(t_cal / 1e9, UTC).strftime("%H:%M:%S") == "20:00:00"
    assert d["day"] == day_label(t_cal, SH) and roll[0]["t_sim_ns"] // S in range(115, 125)
