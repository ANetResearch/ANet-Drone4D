#!/usr/bin/env python3
"""tools/deploy/dmax/d3_tests_summary.py：D3 测试统一入口的计数合并（D3 封板 I01；由 run_d3_tests.sh 在容器内调用）。

输入为各阶段的 `summary<后缀>.json`（tests/d3/conftest.py 写出）与 `rc<后缀>.txt`（pytest 退出码），输出 `counts.json` 与 `meta.json`：

- 同一用例在任一阶段实际运行即取运行结果（多阶段运行取最差：error > failed > xpassed > xfailed > passed），全部阶段跳过才计跳过，
  跳过层取首个阶段的跳过记录；如 gz 锁步用例在 sensorsvc 阶段为 platform 跳过、在 gz 阶段通过，合并后计通过；
- 只合并本次运行的阶段（`--stages`；入口在运行前清除同一标签目录中的旧摘要与退出码）；
- 运行元数据取自环境变量 `D3T_<KEY>`（键名转小写并入 counts.json 与 meta.json，如 D3T_TAG、D3T_GPU_NAME）；
- `ok` 为全部阶段退出码为 0 且摘要齐全；退出码 0 / 1 对应 ok 与否，参数错误为 2。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

SCHEMA = "awr.d3.test_counts.v1"
TIERS = ("dep", "gpu", "optin", "data", "platform", "other")
RANK = {"passed": 0, "xfailed": 1, "xpassed": 2, "failed": 3, "error": 4}
COUNT_KEYS = ("passed", "failed", "error", "skipped", "xfailed", "xpassed")


def merge(out: Path, stages: list[tuple[str, str]]) -> dict:
    """stages：(后缀, 镜像) 列表，后缀 "" 为主阶段。返回合并结果（不含运行元数据）。"""
    st: dict[str, dict] = {}
    merged: dict[str, str] = {}
    skip_of: dict[str, dict] = {}
    collect_errors: list[str] = []
    for sfx, image in stages:
        key = sfx.lstrip("-") or "main"
        rcp, sp = out / f"rc{sfx}.txt", out / f"summary{sfx}.json"
        rc = int(rcp.read_text().strip() or "-1") if rcp.exists() else None
        if not sp.exists():
            st[key] = {"image": image, "rc": rc, "summary": None}
            continue
        s = json.loads(sp.read_text())
        st[key] = {"image": image, "rc": rc, "counts": s["counts"], "skipped_by_tier": s["skipped_by_tier"],
                   "missing_deps": s["missing_deps"], "required_tiers": s["required_tiers"],
                   "required_skip_failures": s["required_skip_failures"], "duration_s": s["duration_s"], "python": s["python"]}
        collect_errors += [f"{key}:{n}" for n in s["collect_errors"]]
        for sk in s["skips"]:
            if not sk["required"]:
                skip_of.setdefault(sk["nodeid"], sk)
        for nid, cat in s["tests"].items():
            prev = merged.get(nid)
            if prev is None or prev == "skipped" or (cat != "skipped" and RANK.get(cat, 0) > RANK.get(prev, 0)):
                merged[nid] = cat
    counts = Counter(merged.values())
    tiers: Counter[str] = Counter()
    deps: Counter[str] = Counter()
    skipped = []
    for nid, cat in sorted(merged.items()):
        if cat != "skipped":
            continue
        sk = skip_of.get(nid, {"tier": "other", "detail": None, "reason": ""})
        tiers[sk["tier"]] += 1
        if sk["tier"] == "dep" and sk.get("detail"):
            deps[sk["detail"]] += 1
        skipped.append({"nodeid": nid, "tier": sk["tier"], "reason": sk["reason"]})
    ok = bool(st) and all(v.get("rc") == 0 and v.get("counts") is not None for v in st.values())
    return {
        "schema": SCHEMA,
        "ok": ok,
        "counts": {k: counts.get(k, 0) for k in COUNT_KEYS} | {"total": len(merged), "collect_errors": len(collect_errors)},
        "skipped_by_tier": {t: tiers.get(t, 0) for t in TIERS},
        "missing_deps": dict(sorted(deps.items())),
        "collect_errors": collect_errors,
        "not_passed": sorted(n for n, c in merged.items() if c in ("failed", "error", "xpassed")),
        "stages": st,
        "skipped": sorted(skipped, key=lambda x: (x["tier"], x["nodeid"])),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--stages", required=True, help="逗号分隔 <后缀>=<镜像>，主阶段后缀为空，如 '=d3-sensorsvc:dev,-gz=d3-gz:dev'")
    a = ap.parse_args(argv)
    stages = [(x.partition("=")[0], x.partition("=")[2]) for x in a.stages.split(",") if x]
    if not stages or any(not img for _, img in stages):
        print(f"d3_tests_summary: bad --stages {a.stages!r}", file=sys.stderr)
        return 2
    res = merge(a.out, stages)
    meta = {k[4:].lower(): v for k, v in sorted(os.environ.items()) if k.startswith("D3T_")}
    meta |= {"finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    res = meta | res
    (a.out / "counts.json").write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n")
    (a.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n")
    c = res["counts"]
    print(f"[d3-tests] {meta.get('tag', '-')}: total {c['total']}  passed {c['passed']}  failed {c['failed']}  error {c['error']}  "
          f"skipped {c['skipped']}  xfailed {c['xfailed']}  collect_errors {c['collect_errors']}  ok {res['ok']}")
    print("[d3-tests] skipped by tier: " + "  ".join(f"{t} {n}" for t, n in res["skipped_by_tier"].items()))
    if res["missing_deps"]:
        print("[d3-tests] missing deps: " + "  ".join(f"{m} {n}" for m, n in res["missing_deps"].items()))
    for n in res["not_passed"] + res["collect_errors"]:
        print(f"[d3-tests] not passed: {n}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
