"""批处理基准插件（WP-13，M16）：按仿真时刻注入天气，并在运行中探测 M20 的 dry-run 与可持续性评估。

sim-core 组合根插件（`AWR_PLUGINS` 追加 `bench_probe`，PYTHONPATH 含 `tools/bench/sandbox`），导入即登记一个慢任务
`bench_probe`（仿真时钟 1 Hz，M08 `register_slow_task`；批处理保留 owner 为 plugin 的慢任务）：

- `AWR_BENCH_ENV_SCHEDULE`：JSON 列表 `[{at_s, patch, mode?, duration_s?}]`，到时以内部 principal（role scenario）提交
  `env/set`（D2-AC-19 的"3 km 霾时段"：`{"atmosphere": {"mor_bg_m": 3000}}`，结束时改回）；批处理会话配置没有定时事件，
  M08 的 `--events` 见 d2-WP-13-to-M08 第 2 条，合入后本功能可改用它；
- `AWR_BENCH_PROBE_AT_S` 与 `AWR_BENCH_PROBE_OUT`：到时对第一个任务做 M20 dry-run（`queries.dry_run`，与 SB-53 `dry_run`
  同一函数）并对接力与值守任务求可持续性（`sustain_sync`，`AWR_BENCH_PROBE_HORIZON_H` 小时），预览完成后把
  `{t_s, task, dry_run, sustainability, relay_precheck}` 写成 JSON。

本模块只在基准与验收工具中加载，不进入任何部署形态的插件清单。
"""

from __future__ import annotations

import json
import os
from typing import Any

from awr.sim.fleet.stages import registry as R

PRINCIPAL = {"principal_id": "bench:relay_batch", "role": "scenario", "entry": "scenario", "seat": False, "source": "SCENARIO"}
SCHEDULE: list[dict[str, Any]] = sorted(json.loads(os.environ.get("AWR_BENCH_ENV_SCHEDULE") or "[]"), key=lambda e: float(e["at_s"]))
PROBE_AT_S = float(os.environ.get("AWR_BENCH_PROBE_AT_S") or -1.0)
PROBE_OUT = os.environ.get("AWR_BENCH_PROBE_OUT") or ""
PROBE_HORIZON_H = int(os.environ.get("AWR_BENCH_PROBE_HORIZON_H") or 24)

_st: dict[str, Any] = {"next": 0, "done": False, "pid": None, "rep": None, "applied": []}


def _env_events(ctx: Any, t: float) -> None:
    calls = getattr(ctx, "calls", None)
    while _st["next"] < len(SCHEDULE) and t >= float(SCHEDULE[_st["next"]]["at_s"]):
        ev = SCHEDULE[_st["next"]]
        _st["next"] += 1
        args = {"patch": ev["patch"], "mode": ev.get("mode", "smooth"), "duration_s": float(ev.get("duration_s", 120.0))}
        r = calls.submit_internal({"cid": f"bench-env-{_st['next']:03d}", "op": "env/set", "args": args}, PRINCIPAL) if calls else None
        _st["applied"].append({"t_s": round(t, 1), "args": args, "status": (r or {}).get("status"), "code": (r or {}).get("code")})


def _probe(t: float) -> None:
    from awr.sim.tasking import plugin as kp
    from awr.sim.tasking import queries as Q

    rt = kp.runtime()
    if rt is None:
        _st["done"] = True
        return
    tasks = [T for T in rt.tasks.values() if not T.parent]
    if not tasks:
        return
    T = tasks[0]
    if _st["rep"] is None:
        rep: dict[str, Any] = {"t_s": round(t, 1), "task": {"tid": T.tid, "type": T.type, "state": T.state,
                                                            "last_error": getattr(T, "last_error", None)}}
        try:
            rep["sustainability"] = Q.sustain_sync(rt, T, PROBE_HORIZON_H)
        except Exception as e:  # 评估失败如实记录
            rep["sustainability"] = {"verdict": "error", "error": f"{type(e).__name__}: {e}"}
        if T.relay is not None:
            r = T.relay
            rep["relay_precheck"] = {"state": r.state, "infeasible_periods": [
                {"band": p["band"], "t_from_s": round(p["t_from"], 1), "t_to_s": round(p["t_to"], 1)} for p in r.infeasible_periods]}
        spec = {"type": T.type, "label": None, "params": T.params, "common": T.common}
        try:
            d = Q.dry_run(rt, spec)
            _st["pid"] = (d.get("item") or {}).get("preview_id") or d.get("preview_id")
            rep["dry_run"] = d
        except Exception as e:
            rep["dry_run"] = {"error": f"{type(e).__name__}: {getattr(e, 'code', '')} {getattr(e, 'detail', e)}"}
        _st["rep"] = rep
    pid = _st["pid"]
    if pid:
        pv = rt.previews.get(pid)
        if pv is not None and pv.get("state") == "done":
            _st["rep"]["dry_run"] = {"state": "done", "item": Q.preview_wire(pv)}
            _st["pid"] = None
        elif pv is not None:
            return
    _st["done"] = True
    _st["rep"]["env_events"] = list(_st["applied"])
    if PROBE_OUT:
        with open(PROBE_OUT, "w", encoding="utf-8") as f:
            json.dump(_st["rep"], f, ensure_ascii=False, default=str, indent=1)


def _task(ctx: Any) -> None:
    t = int(getattr(ctx, "t_ns", 0) or 0) / 1e9
    _env_events(ctx, t)
    if PROBE_AT_S >= 0 and not _st["done"] and t >= PROBE_AT_S:
        _probe(t)


if not any(s.name == "bench_probe" for s in R.registry().slow):
    R.register_slow_task("bench_probe", _task, period_sim_s=1.0, budget_us=5000)
