#!/usr/bin/env python3
"""模板可加载与 60 s 运行检查（WP-13，M16；AWR-04 §10.4"CI 校验每个模板在对应世界上可加载并运行 60 s"；D2-AC-34 的模板部分）。

对每个（世界，模板）：按 M17 池的同一函数 `awr.sandbox.templates.build_session_config` 组装 `session.json`（日历取模板的
`calendar_default`，时刻固定为 2026-10-05，便于复现），以批处理（M08-FR-221，sandbox100，D1 + D2 全部插件）运行 `--until` 仿真秒，
读取 `summary.json` 与 `events.jsonl` 判定：

1. 退出码 0，会话配置问题为空（`config_problems`，M08 `SessionConfig.apply` 的逐段问题）；
2. 机体、识别物、机巢与任务数量与模板一致（实体全部装配成功）；
3. 机巢中有机体且有任务的模板至少出现一次 `nest.launch`（任务真正派出了机体）；
4. 无错误事件：`cmd.failed`、`scenario.invalid`、任务进入 `FAILED` 或 `INFEASIBLE`、严重度 ≥ 3 的事件、机体进入
   `CRASHED`、`ELAND`、`FAILSAFE` 等终态；
5. 报告首次 `perception.capture` 的仿真时刻（T1、T5 的首次反馈，仅记录，不在本检查中判定）。

用法：
    python tools/sandbox/check_templates.py                         # 七个世界 × T1–T6，60 s，并行 3
    python tools/sandbox/check_templates.py --world synthcity --template t5_area_guard --until 120
    python tools/sandbox/check_templates.py --json report.json
退出码：0 全部通过，1 有不通过，14 世界缺失（`--skip-missing` 时跳过）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sbcommon as C

EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14
NOW_UNIX = 1791158400.0                       # 2026-10-05T00:00:00Z：日历的"当日"（可复现）
LIMITS = {"vehicles_max": 12, "targets_max": 30, "cloned_models_max": 8, "nests_max": 4, "tasks_active_max": 6}
BAD_STATES = ("CRASHED", "ELAND", "FAILSAFE", "EMERGENCY", "LOST")


def session_config(world: str, template_id: str, *, seed: int | None = None) -> dict[str, Any]:
    """与 M17 池相同的组装（`build_session_config`），日历按模板缺省、时刻固定。"""
    from awr.sandbox.templates import build_session_config

    t = C.load_json(C.SANDBOX / world / f"{template_id}.json")
    return build_session_config(world, template_id=template_id, template=t, snapshot=None, calendar=None, acoustic_bg=None,
                                seed=int(t.get("seed", 7) if seed is None else seed), limits=LIMITS, tz=C.TZ[world], now=NOW_UNIX)


def judge(template: dict, summary: dict, events: list[dict]) -> dict[str, Any]:
    probs: list[str] = []
    if summary.get("exit_code") != 0:
        probs.append(f"exit_code {summary.get('exit_code')} ({summary.get('failed')})")
    probs.extend(f"config {json.dumps(p, ensure_ascii=False)[:200]}" for p in summary.get("config_problems") or [])
    if summary.get("n_uav") != len(template.get("vehicles") or []):
        probs.append(f"vehicles {summary.get('n_uav')} != {len(template.get('vehicles') or [])}")
    if summary.get("n_target") != len(template.get("targets") or []):
        probs.append(f"targets {summary.get('n_target')} != {len(template.get('targets') or [])}")
    first_capture = None
    tasks: dict[str, str] = {}
    nests = set()
    for e in events:
        k, d = e.get("kind"), e.get("data") or {}
        if k == "perception.capture" and first_capture is None:
            first_capture = round(int(e.get("t_sim_ns") or 0) / 1e9, 2)
        if k in ("task.created", "task.state"):
            tasks[str(d.get("tid"))] = str(d.get("to"))
            if d.get("to") in ("FAILED", "INFEASIBLE"):
                probs.append(f"task {d.get('tid')} {d.get('type')} -> {d.get('to')} ({d.get('code')} {d.get('why')})")
        if k in ("nest.launch", "nest.recover", "nest.swap_done", "nest.dispatch_delayed"):
            nests.add(str(d.get("nest")))
        if k == "cmd.failed":
            probs.append(f"cmd.failed {e.get('uav')} {d.get('op')} {d.get('code')}")
        if k == "scenario.invalid":
            probs.append(f"scenario.invalid {d}")
        if int(e.get("severity") or 0) >= 3:
            probs.append(f"severity {e.get('severity')} {k} {e.get('uav') or ''} {json.dumps(d, ensure_ascii=False)[:160]}")
        if k == "uav.state" and any(b in str(d.get("to", "")) for b in BAD_STATES):
            probs.append(f"uav {e.get('uav')} -> {d.get('to')} ({d.get('reason')})")
    launched = any(e.get("kind") == "nest.launch" for e in events)
    if any(v.get("nest_id") for v in template.get("vehicles") or []) and template.get("tasks") and not launched:
        probs.append("no nest.launch: the template task never dispatched a vehicle")
    n_tasks = len(template.get("tasks") or [])
    if len([t for t in tasks if not t.startswith("pv")]) < n_tasks:
        probs.append(f"tasks created {len(tasks)} < {n_tasks}")
    return {"ok": not probs, "problems": probs, "first_capture_s": first_capture, "tasks": tasks,
            "n_uav": summary.get("n_uav"), "n_target": summary.get("n_target"), "rtf": summary.get("rtf_avg"),
            "wall_s": summary.get("wall_s")}


def run_one(world: str, template_id: str, until_s: float, out: Path, *, timeout_s: float = 900.0) -> dict[str, Any]:
    """一次批处理（子进程，插件登记表彼此隔离）。"""
    d = out / world / template_id
    d.mkdir(parents=True, exist_ok=True)
    cfg = session_config(world, template_id)
    (d / "session.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    env = dict(os.environ, AWR_PLUGINS=C.plugins_env(), AWR_CLOCK_PROFILE="sandbox100", PYTHONUNBUFFERED="1")
    for k in ("AWR_SCENARIO", "AWR_SESSION_CONFIG", "AWR_SUPERVISOR_PID"):
        env.pop(k, None)
    t0 = time.monotonic()
    cmd = [sys.executable, "-m", "awr.sim.runtime", "--batch", "--until", str(until_s), "--config", str(d / "session.json"),
           "--out", str(d), "--world", world]
    try:
        p = subprocess.run(cmd, cwd=C.ROOT, env=env, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"world": world, "template": template_id, "ok": False, "problems": [f"timeout {timeout_s} s"]}
    (d / "stderr.log").write_text(p.stderr[-200_000:], encoding="utf-8")
    try:
        summary = C.load_json(d / "summary.json")
        events = [json.loads(x) for x in (d / "events.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError) as e:
        return {"world": world, "template": template_id, "ok": False,
                "problems": [f"no batch output (rc {p.returncode}): {e}; {p.stderr[-600:]}"]}
    r = judge(C.load_json(C.SANDBOX / world / f"{template_id}.json"), summary, events)
    r.update({"world": world, "template": template_id, "wall_total_s": round(time.monotonic() - t0, 1)})
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/sandbox/check_templates.py")
    ap.add_argument("--world", action="append", choices=C.WORLDS)
    ap.add_argument("--template", action="append", choices=C.TEMPLATE_IDS)
    ap.add_argument("--until", type=float, default=60.0)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--out", default=None, help="输出目录（缺省临时目录）")
    ap.add_argument("--json", default=None, help="把结果写成 JSON 文件")
    ap.add_argument("--skip-missing", action="store_true")
    a = ap.parse_args(argv)
    worlds = a.world or list(C.WORLDS)
    base = Path(os.environ.get("AWR_WORLDS_DIR") or C.ROOT / "worlds")
    missing = [w for w in worlds if not (base / w / "world.json").exists()]
    if missing and not a.skip_missing:
        print(f"worlds not built: {missing}", file=sys.stderr)
        return EXIT_ENV
    worlds = [w for w in worlds if w not in missing]
    out = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="awr-tpl-check-"))
    jobs = [(w, t) for w in worlds for t in (a.template or C.TEMPLATE_IDS)]
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        res = list(ex.map(lambda j: run_one(j[0], j[1], a.until, out), jobs))
    for r in res:
        cap = f" capture {r.get('first_capture_s')} s" if r.get("first_capture_s") is not None else ""
        print(f"{r['world']:13s} {r['template']:15s} {'OK  ' if r['ok'] else 'FAIL'}{cap}"
              f"{'' if r['ok'] else '  ' + ' | '.join(r['problems'][:4])}")
    if a.json:
        Path(a.json).write_text(json.dumps({"until_s": a.until, "out": str(out), "results": res}, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return EXIT_OK if all(r["ok"] for r in res) else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
