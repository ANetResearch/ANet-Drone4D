"""S7 日切与软重置（M20-FR-074；AWR-04 §4.10、ADR-094；D2-AC-39、M20-AC-031）。

共享展示的仿真日历与北京时间同步、×1 连续运行；每个仿真日的当地 04:00（会话日历 `calendar.tz`，S7 为 Asia/Shanghai）做一次软重置：

1. **日表归档**：每个接力任务把当日（上一次日切到本次日切）的统计归档到 `relay_stats` 日表（`day_table`，保留最近 7 天）：监控时长、
   覆盖率、按原因的间隙次数与当日最长间隙、被发现、交接次数与当日重叠（最短、平均）、架次、机时、能耗、能量 RTL。累计统计
   （`awr.relay.stats.v1`）不清零：交接重叠改由运行中的计数、和与最小值给出，因此清理当日交接记录后累计值不变。
2. **清理累积结构**：当日交接记录（重叠明细）；接力任务中已结束（DONE、CANCELED）且不再被任何架次引用的工作项与其分配记录
   （每次接替新建一个站位工作项，此前从不删除，`task/{tid}/status` 的 `items[]` 与逐项遍历随运行时间线性增长）。
   任务、站位、在岗与接替、机巢与识别物状态不变；被发现累计（M18 `discovered_n`）不清零（17 §17.8）。
3. **可观测**：发一条 `relay.day_rollover` 事件（level 0）：`day`（被归档的当地日期）、`tz`、`t_cal_unix_ns`（日界的日历时刻，十进制
   字符串）、`rows[]`（各接力任务的日表行）、`pruned_items`、`pruned_assignments`、`n`（本会话第几次日切）。浸泡工具
   （`tools/bench/sandbox/soak.py`）据此确认软重置与日表归档（第 1 轮验收只能从日历推断，且日界误按 UTC 04:00 计算）。

日界按会话日历计算（`t_cal = cal0 + t_sim`）：首次求值或日历被 `env/calendar` 改写（cal0 或 tz 变化）时只重定基准、不归档；
`sim/reset` 与 checkpoint 恢复时由 `RelayController.reset()` 清空。没有日历（M07 未装配）时不做日切。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["DAY_TABLE_MAX", "SOFT_RESET_HH", "DayRoll", "boundary_after", "day_label"]

log = logging.getLogger("awr.sim.tasking.dayroll")

SOFT_RESET_HH = 4            # 当地 04:00（AWR-04 §4.10）
DAY_TABLE_MAX = 7            # 日表保留天数
TERMINAL = ("DONE", "CANCELED")
_COUNTERS = ("t_monitor", "captured_t", "discovered", "handovers", "sorties", "flight_s", "energy_wh", "energy_rtl", "cfg_entries")


@lru_cache(maxsize=16)
def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Shanghai")


def boundary_after(t_cal_ns: int, tz: str, hh: int = SOFT_RESET_HH) -> int:
    """严格晚于 t_cal 的下一个当地 hh:00（UNIX ns）。按当地日期加一日再取墙上时刻，夏令时切换日同样只有一个日界。"""
    z = _zone(tz)
    loc = datetime.fromtimestamp(int(t_cal_ns) / 1e9, UTC).astimezone(z)
    d: date = loc.date()
    if (loc.hour, loc.minute, loc.second, loc.microsecond) >= (hh, 0, 0, 0):
        d = d + timedelta(days=1)
    mark = datetime(d.year, d.month, d.day, hh, 0, tzinfo=z)
    return int(mark.timestamp()) * 1_000_000_000


def day_label(mark_ns: int, tz: str, hh: int = SOFT_RESET_HH) -> str:
    """日界 mark 结束的那个仿真日（当地日期，`YYYY-MM-DD`）：mark 前一刻所在的当地日期再按 hh 偏移。"""
    z = _zone(tz)
    loc = datetime.fromtimestamp(int(mark_ns) / 1e9 - 1.0, UTC).astimezone(z)
    if loc.hour < hh:
        loc = loc - timedelta(days=1)
    return loc.date().isoformat()


def _calendar(rt: Any) -> Any:
    env = getattr(getattr(rt, "ctx", None), "env", None)
    return getattr(getattr(env, "sky", None), "calendar", None)


def _totals(r: Any) -> dict[str, Any]:
    out: dict[str, Any] = {k: float(getattr(r, k, 0) or 0) for k in _COUNTERS}
    out["gaps_n"] = {k: int(v) for k, v in (getattr(r, "gaps_n", None) or {}).items()}
    return out


class DayRoll:
    """会话级日切（每 1 s【仿真】由 `RelayController.step()` 调用一次；只比较一个整数，日界处才做日期运算）。"""

    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.key: tuple[int, str] | None = None
        self.next_ns: int | None = None
        self.n = 0
        self.last: dict | None = None

    def reset(self) -> None:
        self.key = None
        self.next_ns = None
        self.last = None

    def tick(self) -> dict | None:
        cal = _calendar(self.rt)
        if cal is None:
            return None
        try:
            t_cal = int(cal.t_cal_unix_ns(self.rt.t_ns))
            key = (int(cal.cal0_unix_ms), str(cal.tz))
        except Exception:
            return None
        if key != self.key or self.next_ns is None:
            # 首次求值或日历被改写（env/calendar）：只重定日界，不归档
            self.key = key
            self.next_ns = boundary_after(t_cal, key[1])
            return None
        if t_cal < self.next_ns:
            return None
        mark = self.next_ns
        self.next_ns = boundary_after(t_cal, key[1])
        try:
            return self.roll(mark, key[1])
        except Exception:
            # 日切失败不影响接力（下一日再试）；日志可见
            log.exception("relay day rollover failed", extra={"kv": {"t_cal_unix_ns": mark}})
            return None

    def roll(self, mark_ns: int, tz: str) -> dict:
        rt = self.rt
        day = day_label(mark_ns, tz)
        rows = []
        for T in sorted(rt.tasks.values(), key=lambda x: x.tid):
            r = T.relay
            if r is None:
                continue
            row = self.archive(r, day, mark_ns)
            if row is not None:
                rows.append({"tid": T.tid, "target": r.target, **row})
        n_items, n_asg = self.prune()
        self.n += 1
        ev = {"day": day, "tz": tz, "t_cal_unix_ns": str(int(mark_ns)), "n": self.n, "rows": rows,
              "pruned_items": n_items, "pruned_assignments": n_asg}
        self.last = ev
        if rt.tasks or rt.nests:
            rt.emit("relay.day_rollover", 0, **ev)
        log.info("relay day rollover", extra={"kv": {"day": day, "tz": tz, "tasks": len(rows), "pruned_items": n_items}})
        return ev

    def archive(self, r: Any, day: str, mark_ns: int) -> dict | None:
        """当日统计（与上一次日切的累计值之差）写入日表；清理当日交接记录。任务在当日没有任何活动时不写行。"""
        cur = _totals(r)
        base = r.day_base or {}
        d = {k: cur[k] - float(base.get(k, 0.0)) for k in _COUNTERS}
        g0 = base.get("gaps_n") or {}
        gaps = {k: int(v) - int(g0.get(k, 0)) for k, v in cur["gaps_n"].items()}
        ov = list(r.overlaps or [])
        active = d["t_monitor"] > 0 or any(gaps.values()) or d["sorties"] > 0 or d["handovers"] > 0 or d["discovered"] > 0
        r.day_base = cur
        r.overlaps = []
        gmax, r.gap_max_day = float(r.gap_max_day or 0.0), 0.0
        if not active:
            return None
        mon = d["t_monitor"]
        row = {"day": day, "t_cal_end_unix_ns": str(int(mark_ns)), "monitor_s": round(mon, 1),
               "coverage": round(min(max(d["captured_t"] / mon, 0.0), 1.0), 4) if mon > 0 else None,
               "gaps": gaps, "gap_max_s": round(gmax, 2), "discovered": int(d["discovered"]), "handovers": int(d["handovers"]),
               "overlap_min_s": round(min(ov), 2) if ov else None, "overlap_mean_s": round(sum(ov) / len(ov), 2) if ov else None,
               "config_entries": int(d["cfg_entries"]), "sorties": int(d["sorties"]), "flight_h": round(d["flight_s"] / 3600.0, 4),
               "energy_wh": round(d["energy_wh"], 2), "energy_rtl": int(d["energy_rtl"])}
        r.day_table = [*list(r.day_table or []), row][-DAY_TABLE_MAX:]
        return row

    def prune(self) -> tuple[int, int]:
        """接力任务中已结束且不再被架次引用的工作项及其分配记录（交接记录）。"""
        rt = self.rt
        live = {(so.tid, so.wid) for so in rt.sorties.values() if getattr(so, "tid", None) and getattr(so, "wid", None)}
        n_items = n_asg = 0
        for T in sorted(rt.tasks.values(), key=lambda x: x.tid):
            if T.relay is None:
                continue
            drop = [wid for wid, it in T.items.items() if it.state in TERMINAL and (T.tid, wid) not in live]
            if not drop:
                continue
            for wid in drop:
                del T.items[wid]
            before = len(T.assignments)
            T.assignments = [a for a in T.assignments if a.get("wid") in T.items]
            n_items += len(drop)
            n_asg += before - len(T.assignments)
            T.revision += 1        # 分配着色变化：客户端据 revision 重取计划几何（17 §17.7.4）
        return n_items, n_asg
