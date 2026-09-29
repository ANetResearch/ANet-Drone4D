#!/usr/bin/env python3
"""M14 基准脚本（M14-AC-032、AC-033；M14 §10；路径所有权 AWR-03 §4.3：tools/bench/agent 属 M14）。

两种模式（性能验收由 M16 harness 在 ADR-033 性能运行协议下调度：排他锁、开跑前 load ≤ 4、3 次取中位）：

- `--scenario s3-newyork-sar --dur 60`（AC-032）：采样运行中的 agent-runtime 进程（`--pid`，缺省按命令行
  `awr.agent.runtime` 查找）的 CPU（核）与 RSS；阈值 S3 期间 1 min 均值 ≤ 0.05 核、RSS ≤ 120 MB；`--idle` 时空闲 ≤ 0.01 核。
- `--stress`（AC-033）：进程内 latency 0、200 个 agent、64 个并发任务、每分钟 1000 次委派，持续 `--dur` 秒（缺省 600）：
  RSS 增长 ≤ 10%、决策 p99 ≤ 5 ms【墙钟】、`agent.*` 事件 ≤ 50 条/s、`agent/tasks` 帧 ≤ 16 KiB。

`--smoke` 只做功能自检（短时长，不判阈值、不持锁）。结果写 `--out`（缺省 runs/bench/agent/<ts>/bench-result.json）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python"))

THRESH = {"cpu_s3_cores": 0.05, "cpu_idle_cores": 0.01, "rss_s3_mb": 120.0, "rss_growth_frac": 0.10, "decision_p99_ms": 5.0,
          "events_per_s": 50.0, "tasks_frame_bytes": 16 * 1024}


# ---------------------------------------------------------------- 进程采样（AC-032）
def _find_pid() -> int | None:
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if "awr.agent.runtime" in cmd and "bench" not in cmd:
            return int(d.name)
    return None


def _proc_cpu_s(pid: int) -> float:
    f = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    tck = os.sysconf("SC_CLK_TCK")
    return (int(f[11]) + int(f[12])) / tck


def _proc_rss_mb(pid: int) -> float:
    for line in (Path("/proc") / str(pid) / "status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024.0
    return 0.0


def sample_process(pid: int, dur: float, period: float = 1.0) -> dict[str, Any]:
    c0, t0 = _proc_cpu_s(pid), time.monotonic()
    rss = []
    while time.monotonic() - t0 < dur:
        rss.append(_proc_rss_mb(pid))
        time.sleep(period)
    cores = (_proc_cpu_s(pid) - c0) / max(1e-9, time.monotonic() - t0)
    return {"pid": pid, "dur_s": round(time.monotonic() - t0, 2), "cpu_cores": round(cores, 4), "rss_mb_max": round(max(rss or [0]), 1),
            "load1": os.getloadavg()[0]}


# ---------------------------------------------------------------- 进程内压测（AC-033）
class _Provider:
    coordinator = False

    def __init__(self, aid: str, no: int, sched: Any) -> None:
        self.aid = self.id = aid
        self.agent_no = no
        self.vehicle_id = self.name = f"uav{no:04d}"
        self.profile_id = "p600_mid360"
        self.sched = sched

    def capabilities(self) -> list[str]:
        return ["thermal.imaging", "agent.describe", "agent.state", "task.quote"]

    def describe(self) -> dict[str, Any]:
        return {"manifest_sha256": "0" * 64}

    async def invoke(self, call: Any, sink: Any) -> AsyncIterator[Any]:
        from awr.agent.runtime.types import Effect, EffectStatus, Phase

        if call.capability == "task.quote":
            yield Effect(EffectStatus.OK, verify_trust=4, simulated=True, metrics={
                "eta_s": 30.0 + self.agent_no % 50, "energy_wh": 10.0, "soc_after_pct": 60.0, "feasible": 1.0, "code": 0.0,
                "conf_expected": 0.8, "load": 0.0, "wind_mps": 5.0, "rain_mmh": 0.0, "mor_m": 20000.0})
            return
        yield Effect(EffectStatus.UNVERIFIED, metrics={"accepted": 1.0, "eta_s": 5.0})
        sink.phase(Phase.EXECUTING)
        sink.test("station_reached", True)
        await self.sched.sleep_s(5.0)
        yield Effect(EffectStatus.OK, verify_trust=4, simulated=True, metrics={"confidence": 0.9},
                     artifacts=({"path": "thermal/x/1.pgm", "size_bytes": 19215},))


async def _stress(n_agents: int, n_tasks: int, per_min: int, dur: float) -> dict[str, Any]:
    from awr.agent.anet_mock import identity as ID
    from awr.agent.anet_mock.network import MockNetwork
    from awr.agent.runtime.allocator import ContractNetAllocator
    from awr.agent.runtime.blackboard import Blackboard
    from awr.agent.runtime.clock import SimScheduler
    from awr.agent.runtime.evidence import EvidenceLog
    from awr.agent.runtime.publisher import fit_tasks_frame
    from awr.agent.runtime.scoring import Limits, ScoreNorm, Weights
    from awr.agent.runtime.tasks import TaskManager
    from awr.agent.runtime.types import TaskSpec

    sched = SimScheduler(0)
    net = MockNetwork(sched, zero_latency=True)
    coord = ID.coordinator_aid("bench")
    events = [0]
    ev = EvidenceLog(coord, None, sched, on_append=lambda _l: events.__setitem__(0, events[0] + 1), keep=5000)
    board = Blackboard(now_ms=sched.now_ms)
    tm = TaskManager(sched=sched, net=net, coordinator_aid=coord, evidence_log=ev, board=board, archive_s=1.0)
    tm.allocator = ContractNetAllocator(tm, weights=Weights(), norms_of=lambda v: ScoreNorm(200.0, 18.87), limits_of=lambda v, c: Limits())
    for i in range(n_agents):
        await net.register(_Provider(ID.aid("bench", f"uav{i:04d}"), i + 1, sched))
    acc = {"op": 1, "children": [{"op": 12, "thresh": {"metric": "confidence", "op": 4, "value": 0.8}},
                                 {"op": 11, "test": {"test_id": "station_reached", "expect": 1}}]}
    t0 = time.monotonic()
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    step_ns = 50_000_000
    lat: list[float] = []
    submitted = 0
    interval_s = 60.0 * (n_agents + 1) / max(1, per_min)  # 每个任务 = n_agents 次报价委派 + 1 次执行委派（墙钟节奏）
    next_submit = t0
    max_frame = 0
    k = 0
    while time.monotonic() - t0 < dur:
        now = time.monotonic()
        while now >= next_submit and tm.active_count() < n_tasks:
            submitted += 1
            spec = TaskSpec("thermal.imaging", {"dwell_s": 1}, (float(submitted % 1000) * 40.0, float(submitted // 1000) * 40.0, None),
                            acc, None, "auction", None, 0)
            tm.submit(spec, merge_radius_m=0.0)
            next_submit += interval_s
        a = time.perf_counter()
        await sched.advance_to(sched.now_ns() + step_ns)
        lat.append((time.perf_counter() - a) * 1000.0)
        k += 1
        if k % 20 == 0:
            max_frame = max(max_frame, len(fit_tasks_frame(tm.tasks_frame())[1]))
            for tid in [t.task_id for t in tm.tasks.values() if t.terminal][:-64]:
                tm.tasks.pop(tid, None)
        await asyncio.sleep(0)
    wall = time.monotonic() - t0
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    lat.sort()
    return {"agents": n_agents, "active_max": n_tasks, "submitted": submitted, "delegations": net.stats["delegations"],
            "dur_s": round(wall, 2), "decision_step_p99_ms": round(lat[int(0.99 * (len(lat) - 1))], 3) if lat else None,
            "events_per_s": round(events[0] / max(wall, 1e-9), 1), "tasks_frame_bytes_max": max_frame,
            "rss_mb_start": round(rss0, 1), "rss_mb_end": round(rss1, 1), "rss_growth_frac": round((rss1 - rss0) / max(rss0, 1.0), 4)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/bench/agent/run.py", description="M14 agent-runtime 基准（M14-AC-032、033）")
    ap.add_argument("--scenario", default=None, help="采样运行中的 agent-runtime（例如 s3-newyork-sar）")
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--idle", action="store_true", help="空闲判定（≤ 0.01 核）")
    ap.add_argument("--stress", action="store_true")
    ap.add_argument("--agents", type=int, default=200)
    ap.add_argument("--tasks", type=int, default=64)
    ap.add_argument("--per-min", type=int, default=1000)
    ap.add_argument("--dur", type=float, default=None)
    ap.add_argument("--smoke", action="store_true", help="功能自检：短时长，不判阈值")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    res: dict[str, Any] = {"schema": "awr.bench.agent.v1", "t_wall_unix": int(time.time()), "smoke": a.smoke, "thresholds": THRESH}
    ok = True
    if a.stress or a.smoke:
        dur = a.dur if a.dur is not None else (5.0 if a.smoke else 600.0)
        r = asyncio.run(_stress(20 if a.smoke else a.agents, 16 if a.smoke else a.tasks, a.per_min, dur))
        res["stress"] = r
        if not a.smoke:
            ok &= r["rss_growth_frac"] <= THRESH["rss_growth_frac"] and (r["decision_step_p99_ms"] or 0) <= THRESH["decision_p99_ms"] \
                and r["events_per_s"] <= THRESH["events_per_s"] and r["tasks_frame_bytes_max"] <= THRESH["tasks_frame_bytes"]
    if a.scenario:
        pid = a.pid or _find_pid()
        if pid is None:
            print("agent-runtime process not found", file=sys.stderr)
            return 2
        r = sample_process(pid, a.dur if a.dur is not None else 60.0)
        r["scenario"] = a.scenario
        res["process"] = r
        lim = THRESH["cpu_idle_cores"] if a.idle else THRESH["cpu_s3_cores"]
        ok &= r["cpu_cores"] <= lim and r["rss_mb_max"] <= THRESH["rss_s3_mb"]
    res["pass"] = bool(ok) if not a.smoke else None
    out = Path(a.out) if a.out else ROOT / "runs" / "bench" / "agent" / time.strftime("%Y%m%d-%H%M%S") / "bench-result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False))
    return 0 if (ok or a.smoke) else 1


if __name__ == "__main__":
    sys.exit(main())
