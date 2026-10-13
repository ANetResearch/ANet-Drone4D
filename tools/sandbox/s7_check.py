#!/usr/bin/env python3
"""S7 装配与可持续性检查（WP-13，M16；ADR-094；M20-AC-031 的配置前提"`GET .../sustainability` 为是"）。

在独立进程中以批处理（sandbox100，D1 + D2 全部插件）装配 `scenarios/s7-synthcity-relay.json` 并运行 `--until` 仿真秒，然后对区域值守
任务调用 M20 的可持续性评估（`tasking/sustainability` 同一函数 `sustain_sync`，`--horizon-h` 小时，缺省 168），输出 JSON：

    {"config_problems": [...], "n_uav": 8, "n_target": 3, "nests": 2, "guard": {"tid", "state", "children"},
     "sustainability": {"verdict": "yes" | "no", "bottleneck": ..., ...}, "calendar": {...}}

日历：缺省把文件中的占位日历改为"今日北京时间 `--start-local`（缺省 08:00）"，与共享展示启动时的同步口径一致（d2-WP-13-to-M11）。
退出码：0 装配无问题、任务 RUNNING 且可持续性为"是"；1 不满足；2 输入错误。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sbcommon as C


def calendar_today(start_local: str = "08:00", tz: str = "Asia/Shanghai", now: float | None = None) -> dict[str, str]:
    z = ZoneInfo(tz)
    t = datetime.now(UTC) if now is None else datetime.fromtimestamp(now, UTC)
    loc = t.astimezone(z)
    hh, mm = (int(x) for x in start_local.split(":"))
    d = datetime(loc.year, loc.month, loc.day, hh, mm, tzinfo=z)
    return {"start_utc": d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "tz": tz}


def run(until_s: float = 30.0, *, horizon_h: int = 168, calendar: dict | None = None, config: dict | None = None,
        out: Path | None = None) -> dict[str, Any]:
    from awr.sim.runtime.batch import run_batch

    cfg = copy.deepcopy(config if config is not None else C.load_json(C.S7_PATH))
    if calendar is not None:
        cfg["calendar"] = dict(calendar)
    os.environ["AWR_CLOCK_PROFILE"] = "sandbox100"
    s = run_batch(until_s, cfg, seed=int(cfg.get("seed", 7)), out=out, clock="sandbox100", plugins=C.SANDBOX_PLUGINS)
    from awr.sim.tasking import plugin as kp
    from awr.sim.tasking.queries import sustain_sync

    rt = kp.runtime()
    rep: dict[str, Any] = {"config_problems": s.get("config_problems"), "n_uav": s.get("n_uav"), "n_target": s.get("n_target"),
                           "exit_code": s.get("exit_code"), "calendar": cfg.get("calendar"), "nests": 0, "guard": None,
                           "sustainability": None}
    if rt is None:
        rep["error"] = "tasking runtime not installed"
        return rep
    rep["nests"] = len(rt.nests)
    guards = [T for T in rt.tasks.values() if T.type == "area_guard"]
    if guards:
        T = guards[0]
        rep["guard"] = {"tid": T.tid, "state": T.state, "children": list(T.children)}
        try:
            rep["sustainability"] = sustain_sync(rt, T, int(horizon_h))
        except Exception as e:  # 评估本身失败：如实报告
            rep["sustainability"] = {"verdict": "error", "error": f"{type(e).__name__}: {e}"}
    return rep


def ok(rep: dict[str, Any]) -> bool:
    return (not rep.get("config_problems") and rep.get("n_uav") == 8 and rep.get("n_target") == 3 and rep.get("nests") == 2
            and (rep.get("guard") or {}).get("state") == "RUNNING" and (rep.get("sustainability") or {}).get("verdict") == "yes")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/sandbox/s7_check.py")
    ap.add_argument("--until", type=float, default=30.0)
    ap.add_argument("--horizon-h", type=int, default=168)
    ap.add_argument("--start-local", default="08:00", help="日历：今日北京时间 HH:MM（as-is 时沿用文件中的日历）")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    cal = None if a.start_local == "as-is" else calendar_today(a.start_local)
    out = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="awr-s7-"))
    rep = run(a.until, horizon_h=a.horizon_h, calendar=cal, out=out)
    rep["ok"] = ok(rep)
    print(json.dumps(rep, ensure_ascii=False, default=str))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
