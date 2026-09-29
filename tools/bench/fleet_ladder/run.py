#!/usr/bin/env python3
"""fleet_ladder：机群阶梯基准（M08-FR-078；M08 §6.14；AWR-18 §7.6、§8.7；ADR-033 性能运行协议）。

对每个 N ∈ `--n`（缺省 10,50,100,200,500,1000；100 只作表征点）以真实 supervisor + sim-core 进程运行 `--dur` 秒：
`AWR_SIM_N=N` 布设（出生点 6 m 间距）→ 负载（`ladder_load.py`：4 层交错起飞后环绕；`--churn` 随机 goto；`--scenario` 给出时
由剧本加载器驱动）→ 采集：sim-core CPU（`/proc/<pid>/stat` utime + stime 在窗口内的增量，核·秒/墙钟秒）、RTF（StateRing
头部 t_sim 与墙钟之比）、单步 p50/p99/max 与追帧饱和（头部 1 Hz 自报）、逐 stage ms/仿真 s（`state/sim-core/perf`）、RSS、
内核、loadavg。输出 `bench-result.json`（schema `awr.bench.result.v1`，每个 N 与倍率一条记录）到 `--out`。
协议（ADR-033）：`flock runs/.perf.lock`；开跑前 1 分钟 loadavg ≤ 4（最长等 10 min）；每档 `--runs` 次取中位；
运行期间 load 均值 ≥ 6 时 CPU 阈值不判定。`--smoke`：跳过锁与负载等待、单次短跑，只做功能自检（并行开发阶段用）。
门禁（N = 1000）：RTF ≥ 0.99、CPU ≤ 0.6 核（> 0.40 告警）、单步 p99 ≤ 3 ms、最大 ≤ 12 ms、追帧饱和 0。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import msgpack

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "tools" / "bench" / "ipc"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as C  # noqa: E402  （tools/bench/ipc：性能运行协议与 bench-result 输出）
from ladder_load import LadderLoad  # noqa: E402

from awr.contracts import LAYOUT_ID, bus_keys  # noqa: E402

GATES_1000 = [("rtf", ">=", 0.99), ("cpu_core", "<=", 0.6), ("step_p99_us", "<=", 3000.0), ("step_max_us", "<=", 12000.0),
              ("catchup_saturated", "==", 0)]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _cpu_s(pid: int) -> float:
    f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def _sim_pid(run_dir: Path) -> int | None:
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes().split(b"\0")
            env = (p / "environ").read_bytes()
        except OSError:
            continue
        if b"awr.sim.runtime" in cmd and run_dir.name.encode() in env:
            return int(p.name)
    return None


def run_one(n: int, dur: float, *, rate: float, kernel: str, churn: float | None, scenario: str | None,
            warm_s: float, runs_dir: Path | None = None) -> dict:
    port, bport = _free_port(), _free_port()
    env = dict(os.environ, AWR_SIM_N=str(n), AWR_KERNEL=kernel, AWR_SIM_AUTOPLAY="1")
    if runs_dir is not None:  # --smoke：运行目录放临时目录，不改动仓库 runs/current
        env["AWR_RUNS_DIR"] = str(runs_dir)
    if scenario:
        env["AWR_SCENARIO"] = scenario
    cmd = [sys.executable, "-m", "awr.runtime.supervisor", "--profile", "perf", "--only", "sim-core",
           "--set", "net.port_offset=0", "--set", f"net.port={port}", "--set", f"bus.rendezvous=tcp/127.0.0.1:{bport}",
           "--set", "run.keep_run_dir=false"]
    sup = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    run_id = None
    t_end = time.monotonic() + 120
    while time.monotonic() < t_end:
        line = sup.stdout.readline()
        if not line:
            break
        m = re.search(r"READY run=(\S+)", line)
        if m:
            run_id = m.group(1)
            break
    if run_id is None:
        sup.terminate()
        raise RuntimeError("supervisor 未就绪")
    try:
        from awr.runtime.bus import ZenohBus
        from awr.runtime.statering import StateRing

        world = env.get("AWR_WORLD", "shenzhen")
        runs = Path(env.get("AWR_RUNS_DIR", ROOT / "runs"))
        secret = (runs / run_id / "secret").read_bytes()
        bus = ZenohBus.open("fleet-ladder", namespace=f"awr/{world}/{run_id}", connect=[f"tcp/127.0.0.1:{bport}"],
                            listen=[], announce=False)
        perf: list[dict] = []
        bus.subscribe(bus_keys.STATE_PERF, lambda k, raw: perf.append(msgpack.unpackb(raw, raw=False)))
        ring = StateRing.attach(Path("/dev/shm/awr") / run_id / "state.sim-core", expect_layout_id=LAYOUT_ID)
        load = LadderLoad(bus, run_id, secret)
        load.seat()
        ros = []
        for _ in range(100):
            ros = [e for e in load.roster() if e.get("lifecycle") == "READY"]
            if len(ros) >= n:
                break
            time.sleep(0.2)
        ids = [e["id"] for e in ros]
        homes: dict[str, list[float]] = {}
        if not scenario:
            load.takeoff_layers(ids)
            time.sleep(warm_s)
            homes = load.positions()
            load.orbit_all(ids, homes)
        if rate != 1.0:
            cid = "fl-speed"
            load._call(bus_keys.CTL_CLOCK, {"v": 1, "cid": cid, "op": "speed", "args": {"rate": rate},
                                            "principal": load.principal(cid)})
        pid = _sim_pid(Path("/dev/shm/awr") / run_id)
        rss0 = C.rss_mb(pid)
        sampler = C.LoadSampler()
        h0 = ring.header()
        c0 = _cpu_s(pid) if pid else float("nan")
        w0 = time.monotonic()
        steps = []
        t_churn = time.monotonic()
        while time.monotonic() - w0 < dur:
            time.sleep(1.0)
            h = ring.header()
            steps.append((h.step_p50_us, h.step_p99_us, h.step_max_us))
            if churn and time.monotonic() - t_churn >= churn / max(rate, 1e-3):
                t_churn = time.monotonic()
                load.churn(ids, homes)
        h1 = ring.header()
        c1 = _cpu_s(pid) if pid else float("nan")
        w1 = time.monotonic()
        load_stats = sampler.stop()
        stage: dict[str, float] = {}
        for p in perf[-int(dur):]:
            for k, v in (p.get("stage_ms_per_s") or {}).items():
                stage[k] = stage.get(k, 0.0) + v / max(1, len(perf[-int(dur):]))
        kern = perf[-1].get("kernel", kernel) if perf else kernel
        rec = {"n": n, "rate": rate, "clients": 0, "with_flight60": False, "dur_s": round(w1 - w0, 2),
               "rtf": round((h1.t_sim_ns - h0.t_sim_ns) / 1e9 / (w1 - w0), 4),
               "cpu_core": round((c1 - c0) / (w1 - w0), 4),
               "api_cpu_core": None, "recorder_cpu_core": None, "rec_write_mb_per_min": None,
               "step_us": {"p50": float(max(s[0] for s in steps) if steps else 0),
                           "p99": float(max(s[1] for s in steps) if steps else 0),
                           "max": float(max(s[2] for s in steps) if steps else 0)},
               "catchup_saturated": int(h1.catchup_saturated - h0.catchup_saturated),
               "stage_ms_per_s": {k: round(v, 3) for k, v in stage.items()}, "tick_age_ms": None,
               "rss_mb": {"start": rss0, "end": C.rss_mb(pid)}, "kernel": kern if kern in ("numba", "numpy") else "numba",
               "load": load_stats, "ipc": {"cmd_failed": load.failed, "cmd_sent": load.n_cmd}}
        bus.close()
        return rec
    finally:
        sup.terminate()
        try:
            sup.wait(timeout=15)
        except subprocess.TimeoutExpired:
            sup.kill()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", default="10,50,100,200,500,1000")
    ap.add_argument("--dur", type=float, default=60.0)
    ap.add_argument("--rate", default="1")
    ap.add_argument("--kernel", default="numba", choices=("numba", "numpy"))
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--profile", default=None, help="剧本 profile（例如 n1000，M16 §6.4.8）")
    ap.add_argument("--churn", type=float, default=None)
    ap.add_argument("--with-recorder", action="store_true")
    ap.add_argument("--with-checkpoint", action="store_true")
    ap.add_argument("--clients", type=int, default=0)
    ap.add_argument("--with-flight60", action="store_true")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--warm", type=float, default=15.0, help="起飞后进入环绕前的墙钟秒数")
    ap.add_argument("--out", default=None)
    ap.add_argument("--smoke", action="store_true", help="功能自检：不持锁、不等负载、单次")
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    a = ap.parse_args(argv)
    if a.profile:
        os.environ["AWR_SCENARIO_PROFILE"] = a.profile
    lock = None if a.smoke else C.acquire_perf_lock(a.no_lock)
    _ = lock
    smoke_runs = Path(tempfile.mkdtemp(prefix="awr-ladder-smoke-")) if a.smoke else None
    records = []
    for rate in [float(x) for x in a.rate.split(",")]:
        for n in [int(x) for x in a.n.split(",")]:
            if not a.smoke and not C.wait_load(a.no_load_wait):
                print(json.dumps({"n": n, "result": "环境不满足（PERF_ENV_NOT_READY）"}))
                return C.EXIT_ENV
            runs = [run_one(n, a.dur, rate=rate, kernel=a.kernel, churn=a.churn, scenario=a.scenario, warm_s=a.warm,
                            runs_dir=smoke_runs) for _ in range(1 if a.smoke else a.runs)]
            rec = runs[0] if len(runs) == 1 else {k: C.median_of(runs, k) for k in runs[0]}
            records.append(rec)
            print(json.dumps({"n": n, "rate": rate, "rtf": rec["rtf"], "cpu_core": rec["cpu_core"],
                              "step_us": rec["step_us"], "catchup_saturated": rec["catchup_saturated"]}))
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(a.out) if a.out else ROOT / "runs" / "perf" / f"fleet_ladder-{ts}-{secrets.token_hex(2)}"
    out.mkdir(parents=True, exist_ok=True)
    result = {"schema": "awr.bench.result.v1", "tool": "fleet_ladder", "run_id": out.name, "git_sha": C.git_sha(),
              "host": C.host_info(), "records": records}
    (out / "bench-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if smoke_runs is not None:
        shutil.rmtree(smoke_runs, ignore_errors=True)
    ok = True
    for rec in records:
        if rec["n"] == 1000 and rec.get("rate", 1.0) == 1.0:
            vals = {"rtf": rec["rtf"], "cpu_core": rec["cpu_core"], "step_p99_us": rec["step_us"]["p99"],
                    "step_max_us": rec["step_us"]["max"], "catchup_saturated": rec["catchup_saturated"]}
            res, ok1 = C.judge([(k, vals[k], op, thr) for k, op, thr in GATES_1000], rec["load"]["mean"] < 6)
            ok &= ok1
            print(json.dumps(res, ensure_ascii=False))
    print(f"bench-result: {out / 'bench-result.json'}")
    return C.EXIT_OK if ok else C.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
