#!/usr/bin/env python3
"""fleet_ladder：机群阶梯基准（M08-FR-078；M08 §6.14；AWR-18 §7.4、§7.6、§8.7；ADR-033 性能运行协议）。

对每个 N ∈ `--n`（缺省 10,50,100,200,500,1000；100 只作表征点）以真实 supervisor + sim-core 进程运行 `--dur` 秒：
- 剧本（缺省，M08-FR-078、M16 §6.4.8：`--scenario ladder-shenzhen`，profile 缺省 `n<N>`）：`AWR_SIM_N=0`，剧本加载器布设并
  驱动机群，订阅剧本标记，见到 `--wait-mark`（缺省 `ladder.steady`）后开始测量；
- 骨架布设（`--scenario none`）：`AWR_SIM_N=N`、`AWR_SCENARIO_LOAD=0`（不加载 `run.scenario` 的缺省剧本，D1 验收第 1 轮中
  S1 剧本把机群换成 2 架，负载只发出 4 条命令）；负载（`ladder_load.py`）：GCS 信标 2 Hz，按层批量起飞到 4 层交错高度，
  ≥ 95% 机体 FLYING 后逐架小半径环绕，再稳定 `--settle` 秒开始测量；`--churn` 随机 goto；
- 并发口径（D1-AC-28，18 §7.4）：`--clients K` 同时启动 api 并以 `tools/bench/ipc` 的客户端编排连接 K 个客户端（`--with-flight60`
  时为 headless Chromium flight60，否则为 rt_client 协议客户端），窗口结束时取 api 的 tick 数据年龄（`/api/sys/perf`）与 api CPU；
  `--with-recorder` 同时启动 recorder（CPU 与 `rec-*.mcap` 写入速率）；`--with-checkpoint` 记录 checkpoint 状态（监管下的 sim-core
  恒挂接每 1 s【仿真】一代的 checkpoint，ADR-019，本参数只作口径声明）。
采集：sim-core CPU（`/proc/<pid>/stat` utime + stime 在窗口内的增量，核·秒/墙钟秒）、RTF（StateRing 头部 t_sim 与墙钟之比）、
单步 p50/p99/max 与追帧饱和（头部 1 Hz 自报，窗口内各秒取最大）、逐 stage ms/仿真 s（`state/sim-core/perf`）、RSS、内核、
loadavg。输出 `bench-result.json`（schema `awr.bench.result.v1`）与 `fleet-ladder.json`（各次运行明细：机群状态、拒绝统计、
失败原因）到 `--out`。
协议（ADR-033）：`flock runs/.perf.lock`；开跑前 1 分钟 loadavg ≤ 4（最长等 10 min）；每档 `--runs` 次取中位；运行期间 load
均值 ≥ 6 时 CPU 阈值不判定。`--smoke`：跳过锁与负载等待、单次短跑，只做功能自检。
失败（supervisor 未就绪、sim-core 未运行、机群未就绪、窗口内重启）时该规模点记为失败、不写 NaN，退出码 1（D1 验收第 1 轮 4.4）。
门禁（N = 1000）：RTF ≥ 0.99、CPU ≤ 0.6 核（> 0.40 告警）、单步 p99 ≤ 3 ms、最大 ≤ 12 ms、追帧饱和 0；并发口径另判 tick 数据
年龄 p99 ≤ 15 ms、recorder ≤ 0.1 核、写入 ≤ 60 MB/min。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
GATES_CONCURRENT = [("tick_age_p99_ms", "<=", 15.0), ("cpu_recorder_core", "<=", 0.1), ("rec_write_mb_per_min", "<=", 60.0)]


class LadderError(RuntimeError):
    pass


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _cpu_s(pid: int | None) -> float | None:
    if not pid:
        return None
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def _proc_pid(module: bytes, run_id: str) -> int | None:
    """按命令行模块名与环境中的运行 id 找子进程（sim-core：awr.sim.runtime；recorder：awr.recorder）。跳过热备用进程
    （启动环境含 `AWR_STANDBY=1`，命令行与主进程相同，AWR-19 §4.2、ADR-070）。接替后的进程在 /proc 中仍显示启动时的
    环境（含该变量），因而不会被找到：测量窗口内发生接替即 sim-core 重启，本来就按 PERF-E008 判本次无效。"""
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes().split(b"\0")
            env = (p / "environ").read_bytes()
        except OSError:
            continue
        if module in cmd and f"AWR_RUN={run_id}".encode() in env.split(b"\0") and b"AWR_STANDBY=1" not in env.split(b"\0"):
            return int(p.name)
    return None


def _rec_bytes(persist: Path) -> int:
    try:
        return sum(p.stat().st_size for p in persist.glob("rec-*.mcap"))
    except OSError:
        return 0


class _Supervisor:
    """supervisor 子进程（perf profile）；stdout 由后台线程排空（管道写满会阻塞 supervisor 的日志）。"""

    def __init__(self, env: dict, only: list[str]) -> None:
        self.port, self.bport = _free_port(), _free_port()
        cmd = [sys.executable, "-m", "awr.runtime.supervisor", "--profile", "perf", "--only", ",".join(only),
               "--set", "net.port_offset=0", "--set", f"net.port={self.port}", "--set",
               f"bus.rendezvous=tcp/127.0.0.1:{self.bport}", "--set", "run.keep_run_dir=false"]
        self.diagnostic = bool(os.environ.get("AWR_BENCH_RUNTIME_CONFIG"))
        if self.diagnostic:  # 仅诊断（ACC-1）：另一份 runtime.yaml（例如放宽 sim-core 启动宽限），不用于判定
            cmd += ["-c", os.environ["AWR_BENCH_RUNTIME_CONFIG"]]
        self.base = f"http://127.0.0.1:{self.port}"
        self.log: list[str] = []
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     start_new_session=True)

    def wait_ready(self, timeout_s: float = 120.0) -> str:
        assert self.proc.stdout is not None
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            line = self.proc.stdout.readline()
            if not line:
                break
            self.log.append(line.rstrip("\n"))
            m = re.search(r"READY run=(\S+)", line)
            if m:
                threading.Thread(target=self._pump, daemon=True).start()
                return m.group(1)
        raise LadderError("supervisor 未就绪：" + " | ".join(self.log[-5:]))

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.log.append(line.rstrip("\n"))
            del self.log[:-300]

    def close(self) -> None:
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(10)


def run_one(n: int, dur: float, a: argparse.Namespace, *, rate: float, runs_dir: Path) -> dict:
    """单次运行；失败抛 LadderError（不写 NaN 记录）。返回 {record, detail}。"""
    scenario = a.scenario if a.scenario and a.scenario != "none" else None
    env = dict(os.environ, AWR_KERNEL=a.kernel, AWR_SIM_AUTOPLAY="1", AWR_RUNS_DIR=str(runs_dir), PYTHONUNBUFFERED="1")
    env.pop("AWR_SUPERVISOR_PID", None)
    if scenario:
        env.update(AWR_SCENARIO=scenario, AWR_SIM_N="0", AWR_SCENARIO_LOAD="1")
        if a.profile:
            env["AWR_SCENARIO_PROFILE"] = a.profile
    else:
        env.update(AWR_SIM_N=str(n), AWR_SCENARIO_LOAD="0")
        env.pop("AWR_SCENARIO_PROFILE", None)
    only = ["sim-core"] + (["api"] if a.clients > 0 else []) + (["recorder"] if a.with_recorder else [])
    if a.with_recorder:
        env["AWR_REC_AUTOSTART"] = "1"  # 骨架布设没有剧本的 record 开关：recorder 就绪即开始录制（M12）
    sup = _Supervisor(env, only)
    bus = load = cl = guard = None
    detail: dict[str, Any] = {"n": n, "rate": rate, "scenario": scenario, "procs": only}
    try:
        run_id = sup.wait_ready()
        detail["run_id"] = run_id
        from awr.runtime.bus import ZenohBus
        from awr.runtime.statering import RingNotReady, StateRing

        world = env.get("AWR_WORLD", "shenzhen")
        secret = (runs_dir / run_id / "secret").read_bytes()
        bus = ZenohBus.open("fleet-ladder", namespace=f"awr/{world}/{run_id}", connect=[f"tcp/127.0.0.1:{sup.bport}"],
                            listen=[], announce=False)
        perf: list[dict] = []
        bus.subscribe(bus_keys.STATE_PERF, lambda k, raw: perf.append(msgpack.unpackb(raw, raw=False)))
        ring = None
        t_end = time.monotonic() + a.boot_timeout
        while ring is None:  # sim-core 就绪（StateRing 可附着且心跳在走）
            if sup.proc.poll() is not None:
                raise LadderError("supervisor 已退出：" + " | ".join(sup.log[-5:]))
            try:
                ring = StateRing.attach(Path("/dev/shm/awr") / run_id / "state.sim-core", expect_layout_id=LAYOUT_ID)
            except (RingNotReady, FileNotFoundError, OSError):
                if time.monotonic() > t_end:
                    raise LadderError(f"sim-core 未在 {a.boot_timeout:.0f} s 内就绪："
                                      + " | ".join(sup.log[-5:])) from None
                time.sleep(0.5)
        load = LadderLoad(bus, run_id, secret)
        if scenario:
            load.watch_marks()
        while not load.seat():  # StateRing 先于总线服务就绪（插件装配、世界加载期间不应答）
            if time.monotonic() > t_end:
                raise LadderError("席位申领失败（sim-core 未应答 ctl/sim-core/lease）")
            time.sleep(0.5)
        ids: list[str] = []
        homes: dict[str, list[float]] = {}
        t_fleet = time.monotonic()
        if scenario:
            if a.wait_mark:
                w = load.wait_mark(a.wait_mark, a.fleet_timeout)
                if w is None:
                    raise LadderError(f"剧本标记 {a.wait_mark} 未在 {a.fleet_timeout:.0f} s 内出现")
            ids = [e["id"] for e in load.roster()]
        else:
            ros: list[dict] = []
            while time.monotonic() - t_fleet < a.fleet_timeout:
                ros = [e for e in load.roster() if e.get("lifecycle") == "READY"]
                if len(ros) >= n:
                    break
                time.sleep(0.5)
            ids = [e["id"] for e in ros]
            if len(ids) < n:
                raise LadderError(f"机群未就绪：READY {len(ids)}/{n}")
            load.takeoff_layers(ids)
            fly = load.wait_flying(ids, timeout_s=a.fleet_timeout)
            detail["takeoff"] = fly
            if fly["flying"] < 0.9 * n:
                raise LadderError(f"起飞未完成：{fly}")
            homes = load.positions()
            load.orbit_all(ids, homes)
            time.sleep(a.settle)
        detail["fleet_ready_s"] = round(time.monotonic() - t_fleet, 1)
        if rate != 1.0:
            cid = "fl-speed"
            load._call(bus_keys.CTL_CLOCK, {"v": 1, "cid": cid, "op": "speed", "args": {"rate": rate},
                                            "principal": load.principal(cid)})
        pid = _proc_pid(b"awr.sim.runtime", run_id)
        rec_pid = _proc_pid(b"awr.recorder", run_id) if a.with_recorder else None
        if pid is None:
            raise LadderError("找不到 sim-core 进程")
        api_pid = None
        token = None
        if a.clients > 0:
            import bench_state as BS

            st, tok = BS._http(f"{sup.base}/api/auth/token", method="POST",
                               body={"role": "viewer", "principal_hint": BS.VIEWER_HINT})
            if st != 200 or not isinstance(tok, dict):
                raise LadderError(f"viewer token 失败：HTTP {st}")
            token = tok["token"]
            st, j = BS._http(f"{sup.base}/api/sys/procs", token=token)
            api_pid = next((int(x["pid"]) for x in (j or {}).get("items") or [] if x.get("name") == "api" and x.get("pid")),
                           None)
            ns = SimpleNamespace(with_flight60=a.with_flight60, clients=a.clients, world=world, client_query=a.client_query,
                                 secs=dur)
            cl = subprocess.Popen(BS._clients_cmd(ns, sup.base), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True)
            guard = C.AffinityGuard(cl.pid).start()  # PR-6：SwiftShader 线程不得逃出客户端的核（ADR-073 第 6 条）
            lines: list[str] = []
            if a.with_flight60:
                end = time.monotonic() + 240
                started = False
                while time.monotonic() < end and cl.stdout is not None:
                    line = cl.stdout.readline()
                    if not line:
                        break
                    lines.append(line.rstrip("\n"))
                    if line.startswith("FLIGHT_START"):
                        started = True
                        break
                if not started:
                    raise LadderError("客户端未能开始 flight60：" + " | ".join(lines[-5:]))
            else:
                time.sleep(1.0)
            threading.Thread(target=lambda: [None for _ in cl.stdout], daemon=True).start()  # type: ignore[union-attr]
        rss0 = C.rss_mb(pid)
        sampler = C.LoadSampler()
        h0 = ring.header()
        c0, rc0, ac0 = _cpu_s(pid), _cpu_s(rec_pid), _cpu_s(api_pid)
        b0 = _rec_bytes(runs_dir / run_id)
        w0 = time.monotonic()
        steps = []
        t_churn = time.monotonic()
        while time.monotonic() - w0 < dur:
            time.sleep(1.0)
            h = ring.header()
            steps.append((h.step_p50_us, h.step_p99_us, h.step_max_us))
            if a.churn and not scenario and time.monotonic() - t_churn >= a.churn / max(rate, 1e-3):
                t_churn = time.monotonic()
                load.churn(ids, homes)
        h1 = ring.header()
        c1, rc1, ac1 = _cpu_s(pid), _cpu_s(rec_pid), _cpu_s(api_pid)
        w1 = time.monotonic()
        b1 = _rec_bytes(runs_dir / run_id)
        load_stats = sampler.stop()
        if c1 is None or c0 is None or _proc_pid(b"awr.sim.runtime", run_id) != pid:
            raise LadderError("测量窗口内 sim-core 重启或退出（PERF-E008）")
        win = w1 - w0
        tick_age = None
        if a.clients > 0 and token:
            import bench_state as BS

            st, j = BS._http(f"{sup.base}/api/sys/perf?window_s={max(5, min(600, round(win)))}", token=token)
            f = (j or {}).get("fields", {}) if st == 200 and isinstance(j, dict) else {}
            p50 = (f.get("api.tick_age_p50_ms") or {}).get("p50")
            p99 = (f.get("api.tick_age_p99_ms") or {}).get("p99")
            if isinstance(p50, (int, float)) and isinstance(p99, (int, float)):
                tick_age = {"p50": float(p50), "p99": float(p99)}
        stage: dict[str, float] = {}
        tail = perf[-max(1, int(dur)):]
        for p in tail:
            for k, v in (p.get("stage_ms_per_s") or {}).items():
                stage[k] = stage.get(k, 0.0) + v / len(tail)
        kern = perf[-1].get("kernel", a.kernel) if perf else a.kernel
        rec = {"n": n, "rate": rate, "clients": a.clients, "with_flight60": bool(a.with_flight60), "dur_s": round(win, 2),
               "rtf": round((h1.t_sim_ns - h0.t_sim_ns) / 1e9 / win, 4),
               "cpu_core": round((c1 - c0) / win, 4),
               "api_cpu_core": None if ac0 is None or ac1 is None else round((ac1 - ac0) / win, 4),
               "recorder_cpu_core": None if rc0 is None or rc1 is None else round((rc1 - rc0) / win, 4),
               "rec_write_mb_per_min": round((b1 - b0) / 1e6 / win * 60.0, 3) if a.with_recorder else None,
               "step_us": {"p50": float(max(s[0] for s in steps)), "p99": float(max(s[1] for s in steps)),
                           "max": float(max(s[2] for s in steps))},
               "catchup_saturated": int(h1.catchup_saturated - h0.catchup_saturated),
               "stage_ms_per_s": {k: round(v, 3) for k, v in stage.items()}, "tick_age_ms": tick_age,
               "rss_mb": {"start": rss0, "end": C.rss_mb(pid)}, "kernel": kern if kern in ("numba", "numpy") else "numba",
               "load": load_stats, "ipc": {"cmd_failed": load.failed, "cmd_sent": load.n_cmd,
                                           "fleet_ready_s": detail["fleet_ready_s"], "n_vehicles": len(ids)}}
        detail.update({"ok": True, "rejected": dict(load.rejected), "step_series_us": steps[-int(dur):],
                       "diagnostic": sup.diagnostic, "with_checkpoint": True,
                       "client_affinity": guard.stop() if guard is not None else None})
        return {"record": rec, "detail": detail}
    except LadderError as e:
        detail.update({"ok": False, "error": str(e), "log_tail": sup.log[-20:]})
        raise LadderError(json.dumps(detail, ensure_ascii=False)) from None
    finally:
        if guard is not None:
            guard.stop()
        if cl is not None and cl.poll() is None:
            cl.kill()
        if load is not None:
            load.close()
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.close()
        sup.close()


def _median(runs: list[dict]) -> dict:
    """逐指标取中位（bool、字符串与 None 取第一次的值）。"""
    out: dict[str, Any] = {}
    for k, v in runs[0].items():
        vals = [r.get(k) for r in runs]
        if isinstance(v, bool) or v is None or isinstance(v, str):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _median(vals) if all(isinstance(x, dict) for x in vals) else v
        elif isinstance(v, (int, float)):
            xs = [float(x) for x in vals if isinstance(x, (int, float))]
            med = statistics.median(xs) if xs else None
            out[k] = None if med is None else (round(med) if isinstance(v, int) else round(med, 4))
        else:
            out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", default="10,50,100,200,500,1000")
    ap.add_argument("--dur", type=float, default=60.0)
    ap.add_argument("--rate", default="1")
    ap.add_argument("--kernel", default="numba", choices=("numba", "numpy"))
    ap.add_argument("--scenario", default="ladder-shenzhen", help="剧本 id（缺省 ladder-shenzhen）；none 为骨架布设")
    ap.add_argument("--profile", default=None, help="剧本 profile（例如 n1000，M16 §6.4.8）；缺省为 n<N>")
    ap.add_argument("--wait-mark", default="ladder.steady", help="剧本口径的测量起点标记")
    ap.add_argument("--churn", type=float, default=None)
    ap.add_argument("--with-recorder", action="store_true")
    ap.add_argument("--with-checkpoint", action="store_true", help="口径声明：监管下的 sim-core 恒挂接 checkpoint（ADR-019）")
    ap.add_argument("--clients", type=int, default=0)
    ap.add_argument("--with-flight60", action="store_true")
    ap.add_argument("--client-query", default="scene=full&n=1000", help="flight60 查询串（同 bench_state）")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--settle", type=float, default=5.0, help="环绕下发后进入测量前的墙钟秒数")
    ap.add_argument("--warm", type=float, default=None, help="（兼容旧参数，等同 --settle）")
    ap.add_argument("--boot-timeout", type=float, default=90.0)
    ap.add_argument("--fleet-timeout", type=float, default=240.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--smoke", action="store_true", help="功能自检：不持锁、不等负载、单次")
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    ap.add_argument("--keep-runs", default=None, help="诊断：运行目录放在此处并保留（含崩溃转储与日志）")
    a = ap.parse_args(argv)
    if a.warm is not None:
        a.settle = a.warm
    if a.with_flight60 and a.clients <= 0:
        sys.stderr.write("错误（退出码 2）：--with-flight60 需要 --clients K（K ≥ 1）\n")
        return 2
    lock = None if a.smoke else C.acquire_perf_lock(a.no_lock)
    _ = lock
    runs_dir = Path(a.keep_runs) if a.keep_runs else Path(tempfile.mkdtemp(prefix="awr-ladder-"))
    runs_dir.mkdir(parents=True, exist_ok=True)
    records, details, ok = [], [], True
    try:
        for rate in [float(x) for x in a.rate.split(",")]:
            for n in [int(x) for x in a.n.split(",")]:
                if not a.smoke and not C.wait_load(a.no_load_wait):
                    print(json.dumps({"n": n, "result": "环境不满足（PERF_ENV_NOT_READY）"}, ensure_ascii=False))
                    return C.EXIT_ENV
                prof0 = a.profile
                if a.scenario and a.scenario != "none" and prof0 is None:
                    a.profile = f"n{n}"
                runs = []
                for _ in range(1 if a.smoke else a.runs):
                    try:
                        r = run_one(n, a.dur, a, rate=rate, runs_dir=runs_dir)
                    except LadderError as e:
                        try:
                            details.append(json.loads(str(e)))
                        except ValueError:
                            details.append({"n": n, "rate": rate, "ok": False, "error": str(e)})
                        print(json.dumps({"n": n, "rate": rate, "result": "失败", "error": details[-1].get("error")},
                                         ensure_ascii=False))
                        continue
                    runs.append(r["record"])
                    details.append(r["detail"])
                a.profile = prof0
                if not runs:
                    ok = False
                    continue
                rec = runs[0] if len(runs) == 1 else _median(runs)
                records.append(rec)
                print(json.dumps({"n": n, "rate": rate, "rtf": rec["rtf"], "cpu_core": rec["cpu_core"], "step_us": rec["step_us"],
                                  "catchup_saturated": rec["catchup_saturated"], "runs": len(runs)}, ensure_ascii=False))
    finally:
        if not a.keep_runs:
            shutil.rmtree(runs_dir, ignore_errors=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(a.out) if a.out else ROOT / "runs" / "perf" / f"fleet_ladder-{ts}-{secrets.token_hex(2)}"
    out.mkdir(parents=True, exist_ok=True)
    result = {"schema": "awr.bench.result.v1", "tool": "fleet_ladder", "run_id": out.name, "git_sha": C.git_sha(),
              "host": C.host_info(), "records": records}
    (out / "bench-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "fleet-ladder.json").write_text(json.dumps({"schema": "awr.bench.fleet_ladder.v1", "runs": details},
                                                      ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for rec in records:
        if rec["n"] == 1000 and rec.get("rate", 1.0) == 1.0 and not a.smoke:
            vals = {"rtf": rec["rtf"], "cpu_core": rec["cpu_core"], "step_p99_us": rec["step_us"]["p99"],
                    "step_max_us": rec["step_us"]["max"], "catchup_saturated": rec["catchup_saturated"]}
            gates = [(k, vals[k], op, thr) for k, op, thr in GATES_1000]
            if a.clients > 0 or a.with_recorder:
                cv = {"tick_age_p99_ms": (rec.get("tick_age_ms") or {}).get("p99") if a.clients > 0 else None,
                      "cpu_recorder_core": rec.get("recorder_cpu_core"), "rec_write_mb_per_min": rec.get("rec_write_mb_per_min")}
                gates += [(k, cv[k], op, thr) for k, op, thr in GATES_CONCURRENT if cv[k] is not None]
            res, ok1 = C.judge(gates, rec["load"]["mean"] < 6)
            ok &= ok1
            print(json.dumps(res, ensure_ascii=False))
    print(f"bench-result: {out / 'bench-result.json'}")
    return C.EXIT_OK if ok else C.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
