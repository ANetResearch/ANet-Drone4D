#!/usr/bin/env python3
"""tools/bench/sandbox/soak.py：S7 共享展示浸泡（WP-13，M16；D2-AC-39、M20-AC-031 的浸泡部分；ADR-094）。

cgroup 模拟（`_sbbench.Stack`）下运行共享展示（`--show-scenario`，缺省 `s7-synthcity-relay`）与 `ws_max_show` 个 viewer，连续
`--hours`（缺省 24 h，覆盖一次 04:00 软重置），1 Hz 采样。判据（D2-AC-39 原文；任一不满足即不通过，不判定项为 null）：

- `duration_h`：实际运行时长 ≥ 24 h（D2-AC-39 的硬口径；不足 24 h 的运行不能作为 D2-AC-39 的判定，报告记为不通过而不是外推）；
- sim-core 与 show-api 的 RSS 线性斜率 ≤ 2 MB/h（前 30 min 为预热，不计）；
- 非预期重启 0（supervisor 进程表的 restarts 增量）；
- 被发现 0（show-api `/api/events` 的 `target.discovered`，只计带我方机体的事件；日表行的 `discovered` 之和交叉核对）；
- 软重置：日历跨过当地 04:00 的次数（每分钟读 `GET /api/env/state` 的 `calendar.t_cal_unix_ns` 与 `tz`）≥ 1，且每次跨越都有
  一条 `relay.day_rollover` 事件（M20 dayroll，D2-FX-R2-other；第 1 轮工具从事件环取 `env.keyframe`，而该事件不进事件环，
  软重置无法确认）；
- 日表归档正确（M20-AC-031）：每条日切事件的 `day` 等于日界前一日的当地日期，各行监控时长不超过一日、覆盖率在 [0, 1]；
- OOM 0（单元与沙盒组 `memory.events`）。

附加观测（不属于 D2-AC-39 判据，`ok` 恒为 null）：共享展示 RTF 的 1 Hz 瞬时最小与 10 min 窗口最小（StateRing 的 t_sim 增量 / 墙钟），
单步 p99 最大。

运行中每 `--report-every-min`（缺省 60）写一次 `report.partial.json`（与最终报告同结构，`partial: true`）；收到 SIGTERM 或 SIGINT
时提前结束并写最终报告（`interrupted: true`，按实际时长判定）。

性能锁（ADR-033 PR-1）：`--lock-scope run`（缺省）在整个运行期间持有排他锁 `runs/.perf.lock`；`--lock-scope start` 只在起跑条件
（锁 + 负载 ≤ 4）与栈启动期间持有，栈就绪后释放，使 24 h 浸泡能按 D2-MS5"24 h 浸泡与其他用例并行"与其他阶段同时运行（D2-AC-39
的判据为 RSS 斜率、重启、被发现、软重置与 OOM，与负载无关；运行内负载照常记录在报告 `load`）。第 1 轮验收因浸泡独占锁 24 h
而缩短为 6 h（D2-FX-R2-other）。

用法：
    python tools/bench/sandbox/soak.py --hours 24                      # 排他锁、负载 ≤ 4
    python tools/bench/sandbox/soak.py --hours 0.02 --viewers 2 --smoke --show-scenario s0-synthcity-showcase   # 功能自检
    python tools/bench/sandbox/soak.py --hours 0.15 --viewers 2 --smoke --no-cgroup --calendar-start-local 03:57  # 日切链路自检
输出 `runs/perf/soak-<UTC>/`（`samples.jsonl`、`events.jsonl`、`calendar.jsonl`、`report.json`，`awr.bench.soak.v1`）；退出码 0 通过、
1 不通过、14 环境不满足。`--smoke` 不取锁、不等负载，RSS 斜率与时长不判定（其余判据照常求值，供功能自检）。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parents[2] / "tools" / "bench" / "ipc", _HERE.parents[2] / "tools" / "sandbox"):
    sys.path.insert(0, str(_p))
import _common as PERF  # noqa: E402
import _sbbench as B  # noqa: E402
from capacity import Viewers  # noqa: E402

SCHEMA = "awr.bench.soak.v1"
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14
RSS_SLOPE_MAX = 2.0
HOURS_MIN = 24.0
SOFT_RESET_HHMM = "04:00"
RTF_WINDOW_S = 600.0
ROLLOVER = "relay.day_rollover"


class EventTail:
    """show-api `/api/events` 的增量拉取（viewer token；按 seq 去重写入 events.jsonl）与 `GET /api/env/state` 的日历采样。"""

    def __init__(self, stack: B.Stack, out: Path) -> None:
        self.stack, self.out = stack, out
        self.since = 0
        self.token = stack.viewer_token(99)
        self.counts: dict[str, int] = {}
        self.discovered: list[dict] = []
        self.rollovers: list[dict] = []
        self.cal: list[tuple[int, str]] = []         # (t_cal_unix_ns, tz)

    def _get(self, path: str, **params: Any) -> Any:
        import httpx

        r = httpx.get(self.stack.show_base + path, params=params or None, headers={"Authorization": f"Bearer {self.token}"}, timeout=10)
        if r.status_code == 401:
            self.token = self.stack.viewer_token(99)
            return None
        if r.status_code != 200:
            return None
        return r.json()

    def poll(self) -> None:
        import httpx

        with contextlib.suppress(httpx.HTTPError, ValueError), (self.out / "events.jsonl").open("a", encoding="utf-8") as f:
            for _ in range(20):
                d = self._get("/api/events", since=self.since, limit=1000)
                if not isinstance(d, dict):
                    return
                items = d.get("items") or []
                for e in items:
                    k = str(e.get("type") or e.get("kind"))
                    self.counts[k] = self.counts.get(k, 0) + 1
                    if k == "target.discovered" and (e.get("data") or {}).get("uav"):
                        self.discovered.append(e)
                    if k == ROLLOVER:
                        self.rollovers.append(e)
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
                self.since = int(d.get("next_since") or self.since)
                if len(items) < 1000:
                    break
        self.poll_calendar()

    def poll_calendar(self) -> None:
        import httpx

        with contextlib.suppress(httpx.HTTPError, ValueError, TypeError):
            d = self._get("/api/env/state")
            c = (d or {}).get("calendar") or {}
            if c.get("t_cal_unix_ns") is not None:
                row = (int(c["t_cal_unix_ns"]), str(c.get("tz") or "Asia/Shanghai"))
                self.cal.append(row)
                with (self.out / "calendar.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"t_mono": round(time.monotonic(), 3), "t_cal_unix_ns": str(row[0]), "tz": row[1]}) + "\n")


def crossed_local(cal_ns: list[int], tz: str = "Asia/Shanghai", hhmm: str = SOFT_RESET_HHMM) -> int:
    """日历时刻序列（单调）跨过当地 HH:MM 的次数。"""
    z = ZoneInfo(tz)
    h, m = (int(x) for x in hhmm.split(":"))
    n = 0
    for a, b in pairwise(cal_ns):
        da = datetime.fromtimestamp(a / 1e9, UTC).astimezone(z)
        db = datetime.fromtimestamp(b / 1e9, UTC).astimezone(z)
        mark = da.replace(hour=h, minute=m, second=0, microsecond=0)
        if mark <= da:
            mark += timedelta(days=1)
        while mark <= db:
            n += 1
            mark += timedelta(days=1)
    return n


def expected_day(t_cal_ns: int, tz: str, hh: int = 4) -> str:
    """日界 t_cal（当地 hh:00）结束的仿真日的当地日期。"""
    loc = datetime.fromtimestamp(t_cal_ns / 1e9 - 1.0, UTC).astimezone(ZoneInfo(tz))
    if loc.hour < hh:
        loc -= timedelta(days=1)
    return loc.date().isoformat()


def judge_rollovers(rollovers: list[dict], cal: list[tuple[int, str]], hours: float, *, smoke: bool) -> list[dict]:
    """软重置与日表归档（D2-AC-39 的"含 1 次软重置"，M20-AC-031 的"日表归档正确"）。"""
    tz = cal[-1][1] if cal else "Asia/Shanghai"
    n_cross = crossed_local([c for c, _ in cal], tz)
    n_roll = len(rollovers)
    if n_cross >= 1:
        ok_reset: bool | None = n_roll == n_cross
    elif hours >= HOURS_MIN and not smoke:
        ok_reset = False            # 24 h 必然跨过一次当地 04:00：日历没有推进或没有采到
    else:
        ok_reset = None if n_roll == 0 else False
    bad: list[str] = []
    for e in rollovers:
        d = e.get("data") or {}
        try:
            t_cal = int(d.get("t_cal_unix_ns"))
        except (TypeError, ValueError):
            bad.append("t_cal_unix_ns")
            continue
        if d.get("day") != expected_day(t_cal, str(d.get("tz") or tz)):
            bad.append(f"day:{d.get('day')}")
        for r in d.get("rows") or []:
            mon = r.get("monitor_s")
            cov = r.get("coverage")
            if mon is None or not 0 <= float(mon) <= 86400.0 + 60.0:
                bad.append(f"{r.get('tid')}.monitor_s")
            if cov is not None and not 0.0 <= float(cov) <= 1.0:
                bad.append(f"{r.get('tid')}.coverage")
    ok_table = (not bad) if rollovers else None
    return [check("soft_reset_0400", ok_reset, {"crossings": n_cross, "rollovers": n_roll, "tz": tz},
                  ">= 1 crossing of local 04:00 in 24 h, one relay.day_rollover per crossing"),
            check("day_table", ok_table, {"rows": sum(len((e.get("data") or {}).get("rows") or []) for e in rollovers), "bad": bad[:20]},
                  "day label = local date before the boundary; monitor_s <= 1 day; coverage in [0, 1]")]


def rtf_windows(samples: list[dict], window_s: float = RTF_WINDOW_S, step_s: float = 60.0, skip_s: float = 0.0) -> dict[str, Any]:
    """共享展示 RTF 的窗口最小：每 step_s 一个起点，窗口内 t_sim 增量 / 墙钟增量（epoch 变化的窗口跳过）。"""
    pts = [(s["t"], (s.get("show_ring") or {}).get("t_sim_ns"), (s.get("show_ring") or {}).get("epoch")) for s in samples
           if "error" not in s]
    pts = [p for p in pts if p[1] is not None]
    if len(pts) < 2:
        return {"window_s": window_s, "min": None, "n": 0}
    t0 = pts[0][0] + skip_s
    out = []
    j = 0
    i = 0
    while i < len(pts):
        if pts[i][0] < t0:
            i += 1
            continue
        while j < len(pts) and pts[j][0] - pts[i][0] < window_s:
            j += 1
        if j >= len(pts):
            break
        a, b = pts[i], pts[j]
        if a[2] == b[2] and b[0] > a[0]:
            out.append((b[1] - a[1]) / 1e9 / (b[0] - a[0]))
        nxt = pts[i][0] + step_s
        while i < len(pts) and pts[i][0] < nxt:
            i += 1
    return {"window_s": window_s, "min": round(min(out), 4) if out else None, "n": len(out)}


def check(name: str, ok: bool | None, value: Any, expect: Any) -> dict:
    return {"check": name, "ok": ok, "value": value, "expect": expect}


def build_report(*, a: argparse.Namespace, ts: str, stack: B.Stack, samples: list[dict], ev: EventTail, viewers: dict, load: dict,
                 elapsed_h: float, partial: bool, interrupted: bool) -> dict:
    skip = min(1800.0, elapsed_h * 3600.0 / 4)
    summ = B.summarize(samples, skip_s=skip)
    slopes = {p: B.rss_slope_mb_per_h(samples, p, skip_s=skip) for p in ("sim-core", "api")}
    restarts = {n: p.get("restarts") for n, p in (summ.get("procs") or {}).items() if p.get("restarts")}
    disc_rows = sum(int(r.get("discovered") or 0) for e in ev.rollovers for r in (e.get("data") or {}).get("rows") or [])
    checks = [check("duration_h", (elapsed_h >= HOURS_MIN) if not a.smoke else None, round(elapsed_h, 3), f">= {HOURS_MIN} h")]
    checks += [check(f"rss_slope.{p}", (s is not None and s <= RSS_SLOPE_MAX) if not a.smoke else None, s, f"<= {RSS_SLOPE_MAX} MB/h")
               for p, s in slopes.items()]
    checks += [check("unexpected_restarts", not restarts, restarts, 0),
               check("discovered", not ev.discovered and disc_rows == 0, {"events": len(ev.discovered), "day_rows": disc_rows}, 0)]
    checks += judge_rollovers(ev.rollovers, ev.cal, elapsed_h, smoke=a.smoke)
    checks += [check("oom", ((summ.get("cgroup") or {}).get("oom_kill", 0) == 0) if summ.get("cgroup") else None,
                     (summ.get("cgroup") or {}).get("oom_kill"), 0)]
    show = summ.get("show") or {}
    info = {"show_rtf_min_1hz": show.get("rtf_min"), "show_rtf_window": rtf_windows(samples, skip_s=skip),
            "show_step_p99_us_max": show.get("step_p99_us_max"), "note": "not a D2-AC-39 criterion"}
    ok = all(c["ok"] is not False for c in checks)
    return {"schema": SCHEMA, "run_id": f"soak-{ts}", "git_sha": PERF.git_sha(), "host": PERF.host_info(), "hours": a.hours,
            "elapsed_h": round(elapsed_h, 4), "partial": partial, "interrupted": interrupted, "show_scenario": a.show_scenario,
            "calendar_start_local": a.calendar_start_local, "cgroup": stack.cgroup_mode, "load": load, "viewers": viewers,
            "events": ev.counts, "rollovers": [e.get("data") for e in ev.rollovers], "summary": summ, "rss_slopes_mb_per_h": slopes,
            "info": info, "checks": checks, "ok": ok, "smoke": a.smoke}


def release_lock(lock: Any) -> bool:
    """释放 `acquire_perf_lock` 返回的排他锁（`--lock-scope start`）。"""
    if lock is None:
        return False
    with contextlib.suppress(OSError, ValueError):
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    os.environ.pop("AWR_PERF_LOCK_HELD", None)
    return True


def calendar_start(local_hhmm: str, tz: str = "Asia/Shanghai", now: float | None = None) -> str:
    """今天（当地）的 HH:MM 对应的 UTC 时刻（`AWR_SHOW_CALENDAR_START`）；已过则取明天。"""
    z = ZoneInfo(tz)
    t = datetime.now(UTC) if now is None else datetime.fromtimestamp(now, UTC)
    loc = t.astimezone(z)
    hh, mm = (int(x) for x in local_hhmm.split(":"))
    d = datetime(loc.year, loc.month, loc.day, hh, mm, tzinfo=z)
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/bench/sandbox/soak.py")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--viewers", type=int, default=24)
    ap.add_argument("--show-scenario", default="s7-synthcity-relay")
    ap.add_argument("--cpu-quota", type=int, default=250)
    ap.add_argument("--no-cgroup", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    ap.add_argument("--report-every-min", type=float, default=60.0)
    ap.add_argument("--lock-scope", choices=("run", "start"), default="run",
                    help="run：整个运行期间持有性能排他锁；start：只在起跑与栈启动期间持有（D2-MS5 与其他用例并行）")
    ap.add_argument("--calendar-start-local", default=None,
                    help="功能自检：共享展示日历从当地（Asia/Shanghai）今天的 HH:MM 开始（AWR_SHOW_CALENDAR_START），例如 03:57")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    if not (B.ROOT / "worlds" / "synthcity" / "world.json").exists():
        print("worlds/synthcity not built", file=sys.stderr)
        return EXIT_ENV
    if a.calendar_start_local and not a.smoke:
        print("--calendar-start-local is a functional self-check option (requires --smoke)", file=sys.stderr)
        return EXIT_ENV
    lock = None if a.smoke else PERF.acquire_perf_lock(a.no_lock)
    if not a.smoke and not PERF.wait_load(a.no_load_wait):
        return EXIT_ENV
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(a.out) if a.out else B.ROOT / "runs" / "perf" / f"soak-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    env = {"AWR_SHOW_CALENDAR_START": calendar_start(a.calendar_start_local)} if a.calendar_start_local else {}
    stack = B.Stack(cgroup=not a.no_cgroup, cpu_quota_pct=a.cpu_quota, show_scenario=a.show_scenario, ws_max_show=max(a.viewers, 1),
                    env=env)
    load = PERF.LoadSampler()
    stop = {"sig": None}

    def _on_sig(signum: int, _frame: Any) -> None:
        stop["sig"] = signum

    old = {s: signal.signal(s, _on_sig) for s in (signal.SIGTERM, signal.SIGINT)}
    smp: B.Sampler | None = None
    vw: Viewers | None = None
    ev: EventTail | None = None
    v: dict = {}
    lock_released_s: float | None = None
    t0 = time.monotonic()
    t_end = t0 + a.hours * 3600.0
    try:
        stack.start()
        t0 = time.monotonic()
        t_end = t0 + a.hours * 3600.0
        smp = B.Sampler(stack, out, slim=True).start()
        vw = Viewers(stack, a.viewers).start()
        ev = EventTail(stack, out)
        if a.lock_scope == "start" and release_lock(lock):
            lock = None
            lock_released_s = round(time.monotonic() - t0, 1)
        next_rep = t0 + a.report_every_min * 60.0
        while time.monotonic() < t_end and stop["sig"] is None:
            ev.poll()
            now = time.monotonic()
            if now >= next_rep:
                next_rep = now + a.report_every_min * 60.0
                rep = build_report(a=a, ts=ts, stack=stack, samples=list(smp.samples), ev=ev, viewers={"frames": vw.frames, "errors": vw.errors},
                                   load={}, elapsed_h=(now - t0) / 3600.0, partial=True, interrupted=False)
                (out / "report.partial.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
            end_wait = time.monotonic() + min(60.0, max(0.5, t_end - time.monotonic()))
            while time.monotonic() < end_wait and stop["sig"] is None:
                time.sleep(0.5)
        ev.poll()
        v = vw.stop()
        samples = smp.stop()
    finally:
        if vw is not None and not v:
            v = vw.stop()
        stack.stop()
        for s, h in old.items():
            signal.signal(s, h)
    elapsed_h = (time.monotonic() - t0) / 3600.0
    ld = load.stop()
    report = build_report(a=a, ts=ts, stack=stack, samples=samples, ev=ev, viewers=v, load=ld, elapsed_h=elapsed_h, partial=False,
                          interrupted=stop["sig"] is not None)
    report["lock"] = {"scope": a.lock_scope, "released_after_s": lock_released_s, "smoke": a.smoke}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    ok = report["ok"]
    print(f"soak: {'PASS' if ok else 'FAIL'} ({elapsed_h:.2f} h of {a.hours} h, cgroup {stack.cgroup_mode}) report: {out / 'report.json'}")
    for c in report["checks"]:
        if c["ok"] is False:
            print(f"    FAIL {c['check']}: {c['value']} (expect {c['expect']})")
    del lock
    return EXIT_OK if ok else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
