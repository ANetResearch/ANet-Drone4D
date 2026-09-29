"""`python -m awr.datasets.demo {check,card}`（M16 §7.1：`make demo`、`make demo-check`）。

- `check [--json] [--force] [--quick]`：演示前检查（7 项 + 2 项），不通过时以 19 §16.2 的退出码返回，最后一行打印修复命令；
  `--quick` 只做世界的浅校验；`--force` 时负载告警不阻止演示（不通过项仍阻止）。
- `card`：打印 D0–D7 提示卡。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .check import ROOT_HINT, exit_code, ext_status, run_checks
from .script import card_lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.datasets.demo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--json", action="store_true")
    c.add_argument("--force", action="store_true")
    c.add_argument("--quick", action="store_true")
    c.add_argument("--no-doctor", action="store_true")
    sub.add_parser("card")
    a = ap.parse_args(argv)
    repo = ROOT_HINT
    worlds = Path(os.environ.get("AWR_WORLDS_DIR") or repo / "worlds")
    runs = Path(os.environ.get("AWR_RUNS_DIR") or repo / "runs")
    off = int(os.environ.get("AWR_PORT_OFFSET", "0") or 0)
    if a.cmd == "card":
        items = run_checks(repo, worlds, deep=False, runs_dir=runs, with_doctor=False)
        for line in card_lines(items, port=8000 + 10 * off, ext=ext_status(runs)):
            print(line)
        return 0
    items = run_checks(repo, worlds, force=a.force, deep=not a.quick, runs_dir=runs, with_doctor=not a.no_doctor)
    code = exit_code(items)
    if a.json:
        print(json.dumps({"schema": "awr.demo_check.v1", "exit_code": code, "items": [it.to_json() for it in items]},
                         ensure_ascii=False, indent=1))
    else:
        for it in items:
            mark = {"pass": "通过", "fail": "不通过", "warn": "告警", "manual": "人工"}[it.status]
            print(f"[demo-check] {it.id:9s} {mark:4s} {it.title_zh}：{it.detail}" + (f"（{it.code}）" if it.code else ""))
    if code:
        fix = next((it.fix for it in items if it.status == "fail" and it.fix), "make demo-check")
        print(f"错误（退出码 {code}）：演示前检查未通过", file=sys.stderr)
        print(f"修复：{fix}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
