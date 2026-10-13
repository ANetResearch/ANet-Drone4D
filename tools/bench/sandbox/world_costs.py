#!/usr/bin/env python3
"""tools/bench/sandbox/world_costs.py：七个世界的沙盒成本表与单会话内存（WP-13，M16；D2-AC-28、AWR-04 §4.4.2、M17-FR-015）。

对每个世界：真实栈（`_sbbench.Stack`，`sandbox` profile）中创建一个满载会话（`capacity.capacity_config`：10 架 P600 + 30 识别物 +
巡查、接力与巡线任务，另每 30 s 一次 GSD 扫描 dry-run 触发共享规划），×1 运行 `--seconds`（缺省 120 s，前 30 s 预热不计），1 Hz 采样：

| 列 | 含义 | 口径 |
|---|---|---|
| `anon_mb` | 会话 sim-core 匿名页 + shmem（RssAnon + RssShmem）峰值 | D2-AC-28：S 级 ≤ 150 MB、sanfrancisco ≤ 170 MB |
| `ws_mb` | 会话 sim-core 映射的世界文件（`worlds/<id>/**` 与几何派生缓存）的驻留页之和（`/proc/<pid>/smaps`） | 世界加权准入的 `ws(w)` |
| `c0` | 会话 sim-core ×1 的 CPU（核）均值 | 每会话 c_i 的先验 |
| `grid25_in_anon` | Grid25 是否落在匿名页：派生缓存中 grid25 数组的映射为文件页（smaps 中 `Anonymous` 为 0） | D2-AC-28 第 1 条后半 |

另报共享规划 worker（sandbox-pool 的非会话子进程）的 PSS 峰值（D2-AC-28：≤ 90 MB）。输出 `awr.sandbox.world_costs.v1`
（`configs/sandbox/world_costs.json` 同一格式，`measured: true`，`src` 记录主机与 run id）与 `report.json`；`--write` 时覆盖
`configs/sandbox/world_costs.json`（该文件由 M17 读取，本工具是其生成器，AWR-04 §4.4.2；MS5 实测后提交并以 ADR 冻结）。

用法：
    python tools/bench/sandbox/world_costs.py --all                    # 排他锁、负载 ≤ 4
    python tools/bench/sandbox/world_costs.py --world synthcity --seconds 60 --smoke
退出码：0 通过（D2-AC-28 的单会话阈值）、1 不通过、14 环境不满足。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parents[2] / "tools" / "bench" / "ipc", _HERE.parents[2] / "tools" / "sandbox"):
    sys.path.insert(0, str(_p))
import _common as PERF  # noqa: E402
import _sbbench as B  # noqa: E402
import sbcommon as C  # noqa: E402
from capacity import capacity_config, sandbox  # noqa: E402

SCHEMA = "awr.sandbox.world_costs.v1"
ANON_GATE_MB = {"S": 150.0, "L": 170.0}
PLAN_PSS_GATE_MB = 90.0
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14
CONFIG = B.ROOT / "configs" / "sandbox" / "world_costs.json"


def smaps_world(pid: int, roots: tuple[str, ...]) -> dict[str, float]:
    """映射路径位于 roots 之下的区域：Rss 与 Anonymous（MB）；grid25 数组单列。"""
    out = {"rss_mb": 0.0, "anon_mb": 0.0, "grid25_rss_mb": 0.0, "grid25_anon_mb": 0.0}
    cur: str | None = None
    try:
        lines = Path(f"/proc/{pid}/smaps").read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if parts and "-" in parts[0] and len(parts) >= 5 and ":" not in parts[0]:
            path = parts[5] if len(parts) >= 6 else ""
            cur = path if any(path.startswith(r) for r in roots) else None
            continue
        if cur is None or not parts:
            continue
        if parts[0] in ("Rss:", "Anonymous:"):
            mb = int(parts[1]) / 1024.0
            key = "rss_mb" if parts[0] == "Rss:" else "anon_mb"
            out[key] += mb
            if "grid25" in cur:
                out["grid25_" + key] += mb
    return {k: round(v, 2) for k, v in out.items()}


def mem_total_mb() -> float:
    with contextlib.suppress(OSError, ValueError, IndexError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024.0, 1)
    return 0.0


def pss_mb(pid: int) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return round(int(line.split()[1]) / 1024.0, 2)
    except (OSError, ValueError, IndexError):
        return None
    return None


def plan_workers(pool_pid: int, sessions: set[int]) -> list[int]:
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) in sessions:
            continue
        try:
            ppid = int(Path(f"/proc/{d.name}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if ppid == pool_pid:
            out.append(int(d.name))
    return out


def measure_world(world: str, seconds: float, out: Path) -> dict[str, Any]:
    stack = B.Stack(world=world, cgroup=False)
    roots = (str(B.ROOT / "worlds" / world), str(B.ROOT / "worlds" / ".geo-cache"), str(B.ROOT / "worlds" / "_shared"))
    res: dict[str, Any] = {"world": world}
    try:
        stack.start()
        s = sandbox(stack, 0).create(world, snapshot=capacity_config(world))
        s.wait_ready(180)
        s.clock("speed", rate=1)
        t0 = time.monotonic()
        a = C.load_json(C.ANCHORS / f"{world}.json")
        anon, ws, cpu, g25 = [], [], [], []
        plan_pss: list[float] = []
        last_plan = 0.0
        prev_cpu: tuple[float, float] | None = None
        tok = stack.admin_token()
        pool = next((p.get("pid") for p in stack.procs(tok) if p.get("name") == "sandbox-pool"), None)
        while time.monotonic() - t0 < seconds:
            now = time.monotonic()
            pids = B.session_pids(stack.tag)
            pid = pids.get(s.sid)
            if now - last_plan >= 30.0:
                last_plan = now
                with contextlib.suppress(Exception):
                    s.tasks.create({"type": "area_scan_gsd", "params": {"polygon": a["aoi_scan"], "gsd_cm": 3.0}}, dry_run=True)
            if pid and now - t0 >= min(30.0, seconds / 4):
                m = B.proc_mem_mb(pid)
                anon.append((m.get("RssAnon") or 0) + (m.get("RssShmem") or 0))
                sm = smaps_world(pid, roots)
                ws.append(sm["rss_mb"])
                g25.append(sm["grid25_anon_mb"])
                c = B.proc_cpu_s(pid)
                if c is not None and prev_cpu is not None:
                    cpu.append((c - prev_cpu[1]) / (now - prev_cpu[0]))
                prev_cpu = (now, c) if c is not None else prev_cpu
                if pool:
                    plan_pss += [x for x in (pss_mb(w) for w in plan_workers(int(pool), set(pids.values()))) if x is not None]
            time.sleep(1.0)
        res.update({"anon_mb": round(max(anon or [0.0]), 1), "ws_mb": round(max(ws or [0.0]), 1),
                    "c0": round(sum(cpu) / len(cpu), 4) if cpu else None, "grid25_anon_mb": round(max(g25 or [0.0]), 2),
                    "plan_worker_pss_mb": round(max(plan_pss), 1) if plan_pss else None, "samples": len(anon),
                    "usage": s.state().get("usage")})
        with contextlib.suppress(Exception):
            s.close()
    except Exception as e:
        import traceback

        res["error"] = f"{type(e).__name__}: {e}"
        (out / f"{world}.error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        stack.stop()
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/bench/sandbox/world_costs.py")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--world", action="append", choices=C.WORLDS)
    g.add_argument("--all", action="store_true")
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--write", action="store_true", help="覆盖 configs/sandbox/world_costs.json")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    worlds = list(C.WORLDS) if a.all else list(a.world)
    missing = [w for w in worlds if not (B.ROOT / "worlds" / w / "world.json").exists()]
    if missing:
        print(f"worlds not built: {missing}", file=sys.stderr)
        return EXIT_ENV
    lock = None if a.smoke else PERF.acquire_perf_lock(a.no_lock)
    if not a.smoke and not PERF.wait_load(a.no_load_wait):
        return EXIT_ENV
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(a.out) if a.out else B.ROOT / "runs" / "perf" / f"world-costs-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    prior = {w["world"]: w for w in C.load_json(CONFIG).get("worlds", [])} if CONFIG.exists() else {}
    load = PERF.LoadSampler()
    rows, checks = [], []
    for w in worlds:
        r = measure_world(w, a.seconds, out)
        cls = (prior.get(w) or {}).get("world_class") or ("L" if (r.get("ws_mb") or 0) > 100 else "S")
        rows.append({"world": w, "world_class": cls, "anon_mb": r.get("anon_mb") or 0.0, "ws_mb": r.get("ws_mb") or 0.0,
                     "c0": r.get("c0") if r.get("c0") is not None else (prior.get(w) or {}).get("c0", 0.15),
                     "available": "error" not in r, "measured": "error" not in r and not a.smoke,
                     "src": f"tools/bench/sandbox/world_costs.py run world-costs-{ts}"})
        gate = 170.0 if w == "sanfrancisco" else ANON_GATE_MB["S"] if cls == "S" else ANON_GATE_MB["L"]
        checks.append({"check": f"{w}.anon_shmem_mb", "ok": None if a.smoke else (r.get("anon_mb") or 1e9) <= gate,
                       "value": r.get("anon_mb"), "expect": f"<= {gate}"})
        checks.append({"check": f"{w}.grid25_not_in_anon", "ok": None if a.smoke else (r.get("grid25_anon_mb") or 0.0) == 0.0,
                       "value": r.get("grid25_anon_mb"), "expect": 0})
        if r.get("plan_worker_pss_mb") is not None:
            checks.append({"check": f"{w}.plan_worker_pss_mb", "ok": None if a.smoke else r["plan_worker_pss_mb"] <= PLAN_PSS_GATE_MB,
                           "value": r["plan_worker_pss_mb"], "expect": f"<= {PLAN_PSS_GATE_MB}"})
        print(f"{w:13s} anon {r.get('anon_mb')} MB  ws {r.get('ws_mb')} MB  c0 {r.get('c0')}  plan PSS {r.get('plan_worker_pss_mb')}"
              f"{'  ERROR ' + r['error'] if 'error' in r else ''}")
    ld = load.stop()
    table = {"schema": SCHEMA, "generator": f"tools/bench/sandbox/world_costs.py (M16, run world-costs-{ts})",
             "generated_unix_ns": str(time.time_ns()), "host": {"cpu_model": PERF.host_info().get("cpu_model", ""), "cores": PERF.host_info().get("cores", 0),
                                                                "mem_mb": mem_total_mb()},
             "worlds": rows + [v for k, v in prior.items() if k not in worlds]}
    (out / "world_costs.json").write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "report.json").write_text(json.dumps({"schema": "awr.bench.world_costs.v1", "load": ld, "checks": checks, "rows": rows},
                                                ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if a.write and not a.smoke:
        CONFIG.write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"written {CONFIG.relative_to(B.ROOT)}")
    print(f"report: {out / 'report.json'}")
    del lock
    return EXIT_OK if all(c["ok"] is not False for c in checks) else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
