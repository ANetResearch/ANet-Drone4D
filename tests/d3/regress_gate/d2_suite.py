#!/usr/bin/env python3
"""tests/d3/regress_gate/d2_suite.py：回归门第 1 条"D2 Python 测试全集"的运行器（D3-AC-01；封板任务 K14；docs/05 F-114）。

gate.json 的 pytest 步骤以本脚本运行 D2 测试全集：先完整运行一次 `python -m pytest <参数>`，再对失败用例逐个隔离重跑。

判定规则（全部满足才通过，退出码 0；否则退出码 1）：
  1. 首轮会话正常结束：pytest 退出码为 0 或 1；收集错误、会话级错误（例如会话收尾 `OSError: Too many open files`）、
     中断或超时不做重跑，直接不通过；
  2. 首轮失败的用例数 ≤ `--max-reruns-cases`（缺省 8）：失败面大时视为回归，不做重跑；
  3. 每个首轮失败的用例在独立的 pytest 进程中单独重跑，至多 `--reruns` 次（缺省 3），其中一次通过即判为"负载敏感"并计入
     报告的 `load_sensitive` 列表；任一用例全部重跑都失败即不通过。

隔离重跑针对 D2 套件中依赖墙钟的功能用例（异步超时、去抖窗口、性能锁让步计数）：dmax 共享主机的 CPU 池容器
（`--cpus` 配额）中这些用例偶发失败，同一用例在同一容器配方下单独运行通过，且 D2 标签源码表现相同（K14 记录：
`tests/reconstruction/test_perf_lock.py` 在 v0.2.2 源码上单独运行 3 次失败 2 次，当前树 3 次失败 1 次；重跑次数取 3）。
用例本身属于 D2，不在 d3-dev 上修改（需要改用例时登记为 v0.3 之后的 D2 补丁候选）。

报告：标准输出末尾一行 `D2-SUITE-REPORT <json>`（`awr.d3.regress_gate.d2_suite.v1`：首轮计数、失败用例、每个用例的重跑结果、
判定与原因）；`--report <path>` 另写同一 JSON 文件。回归门把标准输出写入 `<out>/pytest.log`。

用法：
    python tests/d3/regress_gate/d2_suite.py [--reruns 3] [--max-reruns-cases 8] [--report r.json] -- -q -m "not perf" ... tests
`--` 之后为首轮 pytest 参数；重跑沿用其中的 `-p` 插件开关（例如 `-p no:cacheprovider`），只换成单个用例 id。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = "awr.d3.regress_gate.d2_suite.v1"
HERE = Path(__file__).resolve().parent
PLUGIN = "d2_suite"                                  # 本模块同时是首轮会话的 pytest 插件（-p d2_suite，见 pytest_runtest_logreport）
ENV_FAILED = "AWR_D2_SUITE_FAILED"                   # 插件写失败用例 id 的文件（每行一个）
ENV_COLLECT = "AWR_D2_SUITE_COLLECT_ERRORS"          # 插件写收集错误的文件（每行一个）


# ---------------------------------------------------------------------------------------------------------------------
# pytest 插件钩子（只在首轮子进程中由 -p d2_suite 加载）


def _append(env: str, line: str) -> None:
    p = os.environ.get(env)
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def pytest_runtest_logreport(report) -> None:  # pytest 钩子：setup、call、teardown 任一阶段失败即记该用例
    if report.failed:
        _append(ENV_FAILED, report.nodeid)


def pytest_collectreport(report) -> None:  # pytest 钩子
    if report.failed:
        _append(ENV_COLLECT, report.nodeid or "<session>")


# ---------------------------------------------------------------------------------------------------------------------
# 运行器


def plugin_flags(args: list[str]) -> list[str]:
    """首轮参数中的插件开关（`-p X` 与 `-pX`），重跑沿用"""
    out: list[str] = []
    for i, a in enumerate(args):
        if a == "-p" and i + 1 < len(args):
            out += ["-p", args[i + 1]]
        elif a.startswith("-p") and len(a) > 2 and not a.startswith("-p="):
            out.append(a)
    return out


def _read_lines(p: Path) -> list[str]:
    if not p.is_file():
        return []
    seen: dict[str, None] = {}
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            seen.setdefault(ln.strip(), None)
    return list(seen)


def _pytest(argv: list[str], env: dict[str, str], cwd: Path, timeout_s: float | None) -> tuple[int, float]:
    t0 = time.monotonic()
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", *argv], cwd=cwd, env=env, check=False, timeout=timeout_s)
        code = r.returncode
    except subprocess.TimeoutExpired:
        code = 124
    return code, round(time.monotonic() - t0, 1)


def run_suite(pytest_args: list[str], *, reruns: int = 3, max_cases: int = 8, cwd: Path | None = None,
              timeout_s: float | None = None, rerun_timeout_s: float | None = 900.0) -> dict:
    """首轮 + 隔离重跑 → 报告（`awr.d3.regress_gate.d2_suite.v1`）"""
    cwd = cwd or Path.cwd()
    with tempfile.TemporaryDirectory(prefix="awr-d2-suite-") as td:
        failed_p, coll_p = Path(td) / "failed.txt", Path(td) / "collect.txt"
        env = {**os.environ, ENV_FAILED: str(failed_p), ENV_COLLECT: str(coll_p),
               "PYTHONPATH": os.pathsep.join([str(HERE), *filter(None, [os.environ.get("PYTHONPATH")])])}
        code, dt = _pytest([*pytest_args, "-p", PLUGIN], env, cwd, timeout_s)
        failed, coll = _read_lines(failed_p), _read_lines(coll_p)
    rep: dict = {"schema": SCHEMA, "args": pytest_args, "first": {"exit": code, "seconds": dt, "failed": failed, "collect_errors": coll},
                 "reruns_per_case": reruns, "max_rerun_cases": max_cases, "reruns": [], "load_sensitive": [], "pass": False, "reason": ""}
    if code == 0 and not failed and not coll:
        rep.update({"pass": True, "reason": "first run passed"})
        return rep
    if coll:
        rep["reason"] = f"collection errors ({len(coll)}): no reruns"
        return rep
    if code != 1 or not failed:
        rep["reason"] = f"pytest session ended with exit {code} ({len(failed)} failed cases recorded): no reruns"
        return rep
    if len(failed) > max_cases:
        rep["reason"] = f"{len(failed)} failed cases > {max_cases}: treated as a regression, no reruns"
        return rep
    base_env = {k: v for k, v in os.environ.items() if k not in (ENV_FAILED, ENV_COLLECT)}
    flags = plugin_flags(pytest_args)
    still: list[str] = []
    for nid in failed:
        tries = []
        for _ in range(max(0, reruns)):
            c, t = _pytest(["-q", *flags, nid], base_env, cwd, rerun_timeout_s)
            tries.append({"exit": c, "seconds": t})
            if c == 0:
                break
        ok = any(x["exit"] == 0 for x in tries)
        rep["reruns"].append({"nodeid": nid, "tries": tries, "passed": ok})
        (rep["load_sensitive"] if ok else still).append(nid)
    rep["pass"] = not still
    rep["reason"] = (f"{len(failed)} failed in the first run, all passed in isolation" if not still
                     else f"{len(still)} of {len(failed)} failed again in every isolated rerun: {still}")
    return rep


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    own, rest = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else ([], argv)
    ap = argparse.ArgumentParser(description="D2 Python test suite with isolated reruns of failed cases (D3-AC-01)")
    ap.add_argument("--reruns", type=int, default=3, help="isolated reruns per failed case (one pass suffices)")
    ap.add_argument("--max-reruns-cases", type=int, default=8, help="more failed cases than this: no reruns, fail")
    ap.add_argument("--rerun-timeout", type=float, default=900.0, help="seconds per isolated rerun")
    ap.add_argument("--report", type=Path, help="also write the report JSON here")
    a = ap.parse_args(own)
    if not rest:
        ap.error("pytest arguments go after --")
    rep = run_suite(rest, reruns=a.reruns, max_cases=a.max_reruns_cases, rerun_timeout_s=a.rerun_timeout)
    for r in rep["reruns"]:
        print(f"[d2-suite] rerun {r['nodeid']}: {'PASS' if r['passed'] else 'FAIL'} ({len(r['tries'])} tries)", flush=True)
    print(f"[d2-suite] {'PASS' if rep['pass'] else 'FAIL'}: {rep['reason']}", flush=True)
    print("D2-SUITE-REPORT " + json.dumps(rep, ensure_ascii=False, sort_keys=True), flush=True)
    if a.report:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps(rep, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
