#!/usr/bin/env python3
"""tools/bench/d3/regress_gate.py：D3 回归门（AWR-05 §4.8、D3-AC-01；WP-D3-00）。每个 D3 工作包合入 `d3-dev` 之前运行。

步骤与命令登记在 `tests/d3/regress_gate/gate.json`（第 1–6 条）：
  1. D2 Python 测试全集（`pytest -m "not perf"`）、契约测试与前端单测；
  2. 零影响对拍（builtin `zero-impact`）：`AWR_D3_ENABLED = 0` 与 `= 1`（不加载事件）两种条件下各运行一次
     `relay_batch.py --case b --hours 1 --determinism`，`relay_stats.json` 与 `events.jsonl` 的 sha256 两两相同，且与
     `tests/d3/regress_gate/baseline.json`（D2 基线，`--record-baseline` 在 `d2` 分支上生成）相同；两种条件下
     `tests/targets/test_determinism.py` 都通过；relay_batch 只以运行性检查（`RELAY_RUN_CHECKS`）与 determinism 为门槛，
     间隙等场景质量检查随字节对拍一并比较；
  3. D2 性能用例子集（builtin `perf`，缺省不运行：需要独占主机，ADR-033 运行协议）；
  4. `make lint`（含 lint-m06 与 import 方向规则）；5. import 方向；6. 公开演示构建与 K9 生产包扫描。

用法：
    python tools/bench/d3/regress_gate.py                       # 缺省步骤
    python tools/bench/d3/regress_gate.py --steps zero-impact   # 只跑指定步骤（逗号分隔）
    python tools/bench/d3/regress_gate.py --steps zero-impact --record-baseline   # 在 d2 分支上记录基线
输出 `runs/perf/d3-gate-<UTC>/report.json`（`awr.d3.regress_gate.report.v1`）；退出码 0 通过、1 不通过、2 参数错误。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "tests" / "d3" / "regress_gate" / "gate.json"
BASELINE = ROOT / "tests" / "d3" / "regress_gate" / "baseline.json"
PY = sys.executable


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load_gate(path: Path = GATE) -> dict:
    g = json.loads(path.read_text(encoding="utf-8"))
    ids = [s["id"] for s in g["steps"]]
    if len(ids) != len(set(ids)):
        raise ValueError("gate.json: duplicate step ids")
    return g


def select_steps(g: dict, only: list[str] | None) -> list[dict]:
    if only:
        known = {s["id"] for s in g["steps"]}
        bad = [x for x in only if x not in known]
        if bad:
            raise ValueError(f"unknown steps {bad}; known {sorted(known)}")
        return [s for s in g["steps"] if s["id"] in only]
    return [s for s in g["steps"] if s.get("default", True)]


def compare_digests(runs: list[dict[str, str]], baseline: dict[str, str] | None) -> list[str]:
    """runs: 每种开关条件下 {文件名: sha256}；返回不一致的说明（空表示一致）。"""
    out = []
    if not runs:
        return ["no runs"]
    ref = runs[0]
    for i, r in enumerate(runs[1:], 1):
        out.extend(f"{k}: flags #{i} {r.get(k)} != flags #0 {ref[k]}" for k in ref if r.get(k) != ref[k])
    if baseline is not None:
        out.extend(f"{k}: {ref.get(k)} != D2 baseline {v}" for k, v in baseline.items() if ref.get(k) != v)
    return out


# relay_batch 用例检查中的运行性检查；其余（间隙、交接等场景质量检查）由字节对拍覆盖，不单独作为门槛
RELAY_RUN_CHECKS = ("summary.exit_code", "summary.config_problems")


def relay_verdict(report: dict, case: str, hours: float) -> tuple[str, list[str]]:
    """从 relay_batch 的 report.json 取 r1 用例目录名与不通过项（运行性检查与 determinism 逐字节一致）。"""
    name = f"relay-{case}-{hours:g}h"
    cases = {c["case"]: c for c in report.get("cases", [])}
    problems = []
    for r in ("-r1", "-r2"):
        c = cases.get(name + r)
        if c is None:
            problems.append(f"{name}{r}: missing")
            continue
        problems += [f"{name}{r}: {k['check']}" for k in c.get("checks", []) if k["check"] in RELAY_RUN_CHECKS and not k["ok"]]
    det = cases.get(name + "-determinism")
    if det is None or not det.get("ok"):
        problems.append(f"{name}-determinism: {'missing' if det is None else 'not byte identical'}")
    return name + "-r1", problems


def _run(argv: list[str], cwd: Path, timeout_s: float, env: dict | None = None, log: Path | None = None) -> tuple[int, float]:
    t0 = time.monotonic()
    e = {**os.environ, **(env or {})}
    with open(log, "w", encoding="utf-8") if log else open(os.devnull, "w") as f:
        try:
            r = subprocess.run(argv, cwd=cwd, env=e, stdout=f, stderr=subprocess.STDOUT, timeout=timeout_s, check=False)
            code = r.returncode
        except subprocess.TimeoutExpired:
            code = 124
    return code, round(time.monotonic() - t0, 1)


def step_cmd(s: dict, out: Path) -> dict:
    argv = [PY if a == "{py}" else a for a in s["argv"]]
    code, dt = _run(argv, ROOT / s.get("cwd", "."), float(s.get("timeout_s", 3600)), log=out / f"{s['id']}.log")
    return {"pass": code == 0, "exit": code, "seconds": dt, "argv": argv}


def step_zero_impact(g: dict, out: Path, record: bool) -> dict:
    z = g["zero_impact"]
    rb = z["relay_batch"]
    runs, detail = [], []
    for k, flags in enumerate(z["flags"]):
        d = out / f"zero-{k}"
        argv = [PY, "tools/bench/sandbox/relay_batch.py", "--case", rb["case"], "--hours", str(rb["hours"]), "--seed", str(rb["seed"]),
                "--no-lock", "--no-load-wait", "--determinism", "--out", str(d)]
        code, dt = _run(argv, ROOT, 7200, env=flags, log=out / f"zero-{k}.log")
        rep = json.loads((d / "report.json").read_text(encoding="utf-8")) if (d / "report.json").exists() else {}
        case_name, problems = relay_verdict(rep, rb["case"], float(rb["hours"]))
        case_dir = d / case_name
        digests = {f: sha256(case_dir / f) for f in z["files"] if (case_dir / f).exists()}
        pcode, pdt = _run([PY, "-m", "pytest", "-q", *z["pytest"]], ROOT, 1800, env=flags, log=out / f"zero-{k}-pytest.log")
        runs.append(digests)
        detail.append({"flags": flags, "relay_batch_exit": code, "relay_problems": problems, "seconds": dt, "digests": digests,
                       "pytest_exit": pcode,
                       "pytest_seconds": pdt})
    missing = [f for f in z["files"] for r in runs if f not in r]
    if record:
        BASELINE.write_text(json.dumps({"schema": "awr.d3.regress_gate.baseline.v1", "relay_batch": rb, "digests": runs[0]},
                                       indent=1) + "\n", encoding="utf-8")
    base = json.loads(BASELINE.read_text(encoding="utf-8"))["digests"] if BASELINE.exists() else None
    diffs = compare_digests(runs, base)
    ok = not missing and not diffs and all(not d["relay_problems"] and d["pytest_exit"] == 0 for d in detail)
    return {"pass": ok, "runs": detail, "diffs": diffs, "missing": missing, "baseline": str(BASELINE.relative_to(ROOT)) if base else None}


def step_perf(g: dict, out: Path) -> dict:
    p = g["perf"]
    code, dt = _run([PY, "-m", "pytest", "-q", "-m", "perf", *p["pytest"]], ROOT, 3600, log=out / "perf.log")
    return {"pass": code == 0, "exit": code, "seconds": dt}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--steps", default=None, help="comma separated step ids (default: all default steps)")
    ap.add_argument("--record-baseline", action="store_true", help="write the zero-impact digests as the D2 baseline")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    try:
        g = load_gate()
        steps = select_steps(g, a.steps.split(",") if a.steps else None)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out) if a.out else ROOT / "runs" / "perf" / f"d3-gate-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    res = {}
    for s in steps:
        print(f"[d3-gate] {s['id']} ...", flush=True)
        if s["kind"] == "builtin":
            r = step_zero_impact(g, out, a.record_baseline) if s["id"] == "zero-impact" else step_perf(g, out)
        else:
            r = step_cmd(s, out)
        r["item"] = s["item"]
        res[s["id"]] = r
        print(f"[d3-gate] {s['id']}: {'PASS' if r['pass'] else 'FAIL'}", flush=True)
    rep = {"schema": "awr.d3.regress_gate.report.v1", "utc": stamp, "steps": res, "pass": all(r["pass"] for r in res.values())}
    (out / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[d3-gate] report {out / 'report.json'}: {'PASS' if rep['pass'] else 'FAIL'}")
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
