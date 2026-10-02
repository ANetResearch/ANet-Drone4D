#!/usr/bin/env python3
"""状态平面基准：StateRing 写入与 Gateway 读环（M11-AC-002；PERF-AC-034；D1-AC-08 的读环部分；AWR-18 §7.2、§7.6）。

写者（sim-core 模拟，独立进程）：固定频率（默认 125 Hz）主循环，每次迭代 heartbeat 并发布 N 行 Full64 + Lite32，
交替使用 `publish()` 与零拷贝 `begin_publish()/commit_publish()` 两条路径，分别统计耗时。
读者（Gateway 模拟，本进程，uvloop）：60 Hz tick，每 tick `header()` 一致性读 + `read_latest()`，每 60 tick `identity()`；
统计 tick 时数据年龄（now − 最新帧 t_pub_ns）、读者 CPU、tick 耗时与事件循环延迟。

门禁：publish（两条路径）p99 ≤ 300 µs；无客户端时读环 CPU ≤ 3% 核；tick 数据年龄 p99 ≤ 15 ms。

客户端口径（D1-AC-08、PERF-AC-035 完整口径；FX2-R2-gateway 补齐，此前只解析参数不执行）：`--clients K` 时不用合成写者，
而是以真实后端运行：supervisor `--profile perf --only sim-core,api`（PR-6 钉核：api core0、sim-core core1），机群为
`--scenario ladder-shenzhen --scenario-profile n1000`（剧本标记 `ladder.steady` 之后开始，与 gw-10clients 同一负载），
`--scenario none` 时改为 `AWR_SIM_N=--n` 骨架布设；就绪后启动客户端：
- `--with-flight60`：`flight60_clients.mjs` 启动 K 个 headless Chromium（C1 标志），各自跑 flight60（持续 Range 流式，
  并按默认订阅集订阅 WS）；查询串缺省 `scene=full&n=1000`（18 §8.7(2) MS5 起的口径；MS4 的 `scene=pc&rt=1` 前端未实现
  `rt=1`，`scene=pc` 下不订阅仿真 channel，不能代表网关负载）；测量窗口从 K 个客户端全部开始飞行起算，`--secs`（缺省 60 s）；
- 否则：`rt_client.mjs` 启动 K 个轻量协议客户端（18 §8.7(2)），窗口 `--secs`。
窗口内每 1 s 读 `/proc/<pid>/stat`（pid 取自 `GET /api/sys/procs`）得 api 与 sim-core CPU（18 §9.4 第 3 条，权威来源），
窗口结束时取 `GET /api/sys/perf?window_s=<窗口>` 的 `api.tick_age_p99_ms`（各秒 p99 的窗口 p99，与 harness 同一口径）、
`api.loop_lag_p99_ms` 与 sim 字段；窗口内 api 或 sim-core 重启则本次无效（PERF-E008）。
门禁：api ≤ 0.35 核（CPU 类，运行内 load 均值 ≥ 6 时不判定，PR-5）；tick 数据年龄 p99 ≤ 15 ms；事件循环延迟 p99 ≤ 10 ms（P1，
只报告）。超标时按 ADR-013 拆分静态服务后复测。

用法：python tools/bench/ipc/bench_state.py [--n 1000] [--hz 125] [--secs 15] [--runs 3] [--out DIR] [--no-lock] [--no-load-wait]
      python tools/bench/ipc/bench_state.py --clients 3 --with-flight60 [--secs 60] [--runs 3] [--client-query Q]
        [--scenario ladder-shenzhen --scenario-profile n1000 --wait-mark ladder.steady | --scenario none --n 1000]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C

from awr.contracts import LAYOUT_ID
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32, lite_from_full
from awr.runtime.statering import LOSSY, StateRing

WARMUP_S = 2.0


# ---------------------------------------------------------------- 写者（子进程）
def writer(a: argparse.Namespace) -> None:
    n, hz = a.n, a.hz
    ring = StateRing.create(Path(a.path), capacity=max(1024, n), slots=32, layout_id=LAYOUT_ID)
    full = np.zeros(n, DRONE_STATE64)
    lite = np.zeros(n, SWARM_LITE32)
    full["agent_no"] = np.arange(n)
    idx = np.arange(n)
    r = 50.0 + (idx % 10) * 20.0
    work = np.random.default_rng(0).random(n * 16)
    print("READY", flush=True)
    dt = 1.0 / hz
    nxt = time.perf_counter()
    t_end = nxt + WARMUP_S + a.secs + 1.0
    pub = {"publish": [], "begin": []}
    step, misses, k = [], 0, 0
    cpu0 = t0 = None
    t_sim0 = 0
    rss0 = C.rss_mb()
    synth_s = pub_s = 0.0
    while time.perf_counter() < t_end:
        nxt += dt
        slack = nxt - time.perf_counter()
        if slack > 0:
            time.sleep(slack)
        elif slack < -dt:
            misses += 1
        if cpu0 is None and time.perf_counter() > t_end - a.secs - 1.0:
            cpu0, t0, t_sim0 = time.process_time(), time.perf_counter(), k
            pub = {"publish": [], "begin": []}
            step.clear()
            misses = 0
            synth_s = pub_s = 0.0
        ts = time.perf_counter()
        k += 1
        t_sim = int(k * dt * 1e9)
        ring.heartbeat(t_sim, 1, 1000, step_seq=k)
        if a.load == "synthetic":
            ang = 2 * math.pi * idx / n + 0.04 * k * dt
            full["pos"][:, 0] = r * np.cos(ang)
            full["pos"][:, 1] = r * np.sin(ang)
            full["q"][:, 3] = 1.0
            # 模拟 sim-core 的步进计算（FleetSim 1000 架每步约 2–4 ms，g05 §3.2），使写者像真实主循环一样持续占用 CPU
            w_end = ts + a.work_us * 1e-6
            while time.perf_counter() < w_end:
                np.sin(work, out=work)
            lite_from_full(full, out=lite)
        tp = time.perf_counter()
        if k % 2:
            ring.publish(full, lite, t_sim, 1)
            path = "publish"
        else:
            fv, lv, tk = ring.begin_publish()
            fv[:n] = full
            lv[:n] = lite
            ring.commit_publish(tk, n, t_sim, 1)
            path = "begin"
        te = time.perf_counter()
        pub[path].append((te - tp) * 1e6)
        step.append((te - ts) * 1e6)
        synth_s += tp - ts
        pub_s += te - tp
    wall = time.perf_counter() - t0
    res = {"cpu_core": (time.process_time() - cpu0) / wall, "publish_us": C.stats3(pub["publish"]),
           "begin_commit_us": C.stats3(pub["begin"]), "step_us": C.stats3(step), "deadline_misses": misses,
           "frames": len(step), "rtf": (k - t_sim0) * dt / wall, "rss_mb": {"start": rss0, "end": C.rss_mb()},
           "stage_ms_per_s": {"synth": round(synth_s * 1e3 / max(1e-9, (k - t_sim0) * dt), 3),
                              "publish": round(pub_s * 1e3 / max(1e-9, (k - t_sim0) * dt), 3)}}
    print("RESULT " + json.dumps(res), flush=True)
    time.sleep(0.3)
    ring.close()


# ---------------------------------------------------------------- 读者（Gateway 模拟）
async def reader_run(a: argparse.Namespace) -> dict:
    path = Path(tempfile.mkdtemp(prefix="awr-bench-", dir="/dev/shm")) / "state.sim-core"
    proc = subprocess.Popen([sys.executable, __file__, "--role", "writer", "--n", str(a.n), "--hz", str(a.hz), "--secs",
                             str(a.secs), "--path", str(path), "--load", a.load, "--work-us", str(a.work_us)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "READY"
        ring = None
        for _ in range(200):
            try:
                ring = StateRing.attach(path, expect_layout_id=LAYOUT_ID)
                break
            except Exception:
                await asyncio.sleep(0.01)
        assert ring is not None
        ring.register(LOSSY, "bench-api")
        period = 1 / 60
        await asyncio.sleep(WARMUP_S)
        ages, lags, tick_us = [], [], []
        fresh = 0
        cpu0, t0 = time.process_time(), time.perf_counter()
        nxt = t0
        last = 0
        k = 0
        while time.perf_counter() - t0 < a.secs:
            nxt += period
            await asyncio.sleep(max(0.0, nxt - time.perf_counter()))
            lags.append((time.perf_counter() - nxt) * 1e3)
            ts = time.perf_counter()
            k += 1
            ring.header()
            f = ring.read_latest(last)
            if f is not None:
                last = f.frame_seq
                fresh += 1
                t_pub = f.t_pub_ns
            if k % 60 == 0:
                ring.identity()
            if last:
                ages.append((time.monotonic_ns() - t_pub) / 1e6)
            tick_us.append((time.perf_counter() - ts) * 1e6)
        cpu = (time.process_time() - cpu0) / (time.perf_counter() - t0)
        out = proc.communicate(timeout=60)[0]
        w = json.loads(next(line for line in out.splitlines() if line.startswith("RESULT "))[7:])
        ring.close()
        return {"reader_cpu_core": cpu, "tick_age_ms": {"p50": round(C.pct(ages, 50), 3), "p99": round(C.pct(ages, 99), 3)},
                "tick_us": C.stats3(tick_us), "loop_lag_ms_p99": round(C.pct(lags, 99), 3),
                "fresh_ticks_pct": round(100 * fresh / max(1, k), 1), "writer": w}
    finally:
        if proc.poll() is None:
            proc.kill()
        import shutil

        shutil.rmtree(path.parent, ignore_errors=True)


# ---------------------------------------------------------------- 客户端口径（真实后端 + K 个客户端，D1-AC-08）
VIEWER_HINT = "BENCHSTATEVIEWERAAAAAAAA"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _http(url: str, *, method: str = "GET", body: dict | None = None, token: str | None = None,
          timeout: float = 5.0) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def _cpu_s(pid: int) -> float | None:
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


class _Backend:
    """supervisor（perf profile，sim-core + api）；stdout 由后台线程排空（管道写满会阻塞 supervisor 的日志）。"""

    def __init__(self, a: argparse.Namespace) -> None:
        self.runs = Path(tempfile.mkdtemp(prefix="awr-bench-gw-"))
        port, bport = _free_port(), _free_port()
        env = dict(os.environ, AWR_RUNS_DIR=str(self.runs), AWR_WORLD=a.world, PYTHONUNBUFFERED="1")
        env.pop("AWR_SUPERVISOR_PID", None)
        if a.scenario and a.scenario != "none":
            env["AWR_SCENARIO"] = a.scenario
            if a.scenario_profile:
                env["AWR_SCENARIO_PROFILE"] = a.scenario_profile
        else:
            env.update(AWR_SIM_N=str(a.n), AWR_SCENARIO_LOAD="0", AWR_SIM_AUTOPLAY="1")
        cmd = [sys.executable, "-m", "awr.runtime.supervisor", "--profile", "perf", "--only", "sim-core,api",
               "--set", "net.port_offset=0", "--set", f"net.port={port}", "--set", f"bus.rendezvous=tcp/127.0.0.1:{bport}",
               "--set", "run.keep_run_dir=false"]
        self.diagnostic = bool(os.environ.get("AWR_BENCH_RUNTIME_CONFIG"))
        if self.diagnostic:  # 仅诊断（与 fleet_ladder 相同）：另一份 runtime.yaml，结果标记 diagnostic，不用于判定
            cmd += ["-c", os.environ["AWR_BENCH_RUNTIME_CONFIG"]]
        self.base = f"http://127.0.0.1:{port}"
        self.log: list[str] = []
        self.proc = subprocess.Popen(cmd, cwd=C.ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     start_new_session=True)
        self.token: str | None = None
        self.pids: dict[str, int] = {}
        self.restarts0: dict[str, int] = {}

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.log.append(line.rstrip("\n"))
            del self.log[:-200]

    def start(self, ready_timeout_s: float = 120.0) -> None:
        assert self.proc.stdout is not None
        end = time.monotonic() + ready_timeout_s
        ready = False
        while time.monotonic() < end:
            line = self.proc.stdout.readline()
            if not line:
                break
            self.log.append(line.rstrip("\n"))
            if re.search(r"READY run=", line):
                ready = True
                break
        if not ready:
            raise RuntimeError("supervisor 未就绪：" + " | ".join(self.log[-5:]))
        threading.Thread(target=self._pump, daemon=True).start()
        while time.monotonic() < end:
            if _http(f"{self.base}/api/health/ready")[0] == 200:
                break
            time.sleep(0.25)
        st, tok = _http(f"{self.base}/api/auth/token", method="POST", body={"role": "viewer", "principal_hint": VIEWER_HINT})
        if st != 200 or not isinstance(tok, dict):
            raise RuntimeError(f"viewer token 失败：HTTP {st}")
        self.token = tok["token"]
        while time.monotonic() < end:
            items = self.procs()
            core = [x for x in items if x.get("name") in ("api", "sim-core")]
            if len(core) == 2 and all(x.get("state") == "RUNNING" for x in core):
                self.pids = {x["name"]: int(x["pid"]) for x in core if x.get("pid")}
                self.restarts0 = {x["name"]: int(x.get("restarts") or 0) for x in core}
                return
            time.sleep(0.5)
        raise RuntimeError(f"进程未全部 RUNNING：{[(x.get('name'), x.get('state')) for x in self.procs()]}")

    def procs(self) -> list[dict]:
        st, j = _http(f"{self.base}/api/sys/procs", token=self.token)
        return list((j or {}).get("items") or []) if st == 200 else []

    def clients_summary(self) -> list[dict]:
        """`GET /api/rt/inspect`（perf profile 下 viewer 可读）：各连接的订阅集与下行速率，核对客户端确实订阅了默认订阅集。"""
        st, j = _http(f"{self.base}/api/rt/inspect", token=self.token)
        if st != 200 or not isinstance(j, dict):
            return []
        return [{"role": c.get("role"), "tier": c.get("tier"), "kbps": c.get("kbps"), "credit_skips": c.get("credit_skips"),
                 "subs": sorted({f"{x.get('topic')}@{x.get('rate')}" for x in c.get("subs") or []})[:24],
                 "n_subs": len(c.get("subs") or [])} for c in j.get("clients") or [] if isinstance(c, dict)]

    def perf_window(self, window_s: int) -> dict:
        st, j = _http(f"{self.base}/api/sys/perf?window_s={max(5, min(600, int(window_s)))}", token=self.token)
        return j if st == 200 and isinstance(j, dict) else {}

    def wait_fleet(self, a: argparse.Namespace, timeout_s: float) -> float:
        """剧本：等 `--wait-mark` 事件；骨架布设：等 sim.n_active ≥ N。返回等待秒数。"""
        t0 = time.monotonic()
        end = t0 + timeout_s
        since = 0
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise RuntimeError("supervisor 已退出")
            if a.scenario and a.scenario != "none" and a.wait_mark:
                st, j = _http(f"{self.base}/api/events?since={since}&limit=1000", token=self.token)
                if st == 200 and isinstance(j, dict):
                    for e in j.get("items") or []:
                        d = e.get("data") or {}
                        if d.get("label") == a.wait_mark or e.get("type") == a.wait_mark:
                            return time.monotonic() - t0
                        if e.get("type") == "scenario.invalid":
                            raise RuntimeError(f"scenario.invalid {d}")
                    since = j.get("next_since", since)
            else:
                f = self.perf_window(5).get("fields", {}).get("sim.n_active", {})
                if f and f.get("max", 0) >= a.n:
                    return time.monotonic() - t0
            time.sleep(1.0)
        raise RuntimeError(f"机群未在 {timeout_s:.0f} s 内就绪（{a.wait_mark or 'n_active'}）")

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
        shutil.rmtree(self.runs, ignore_errors=True)


def _clients_cmd(a: argparse.Namespace, base: str) -> list[str]:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("找不到 node（Node 22）：客户端编排需要 PATH 中有 node")
    if a.with_flight60:
        # --hold：先飞完的客户端重新载入再飞，窗口内始终 K 个客户端在流式加载（遮罩揭开时刻在负载下可差 20 s）
        return [node, str(C.ROOT / "tools" / "bench" / "ipc" / "flight60_clients.mjs"), "--base", base, "--clients",
                str(a.clients), "--city", a.world, "--query", a.client_query, "--timeout", str(int(a.secs + 120)),
                "--hold", str(int(a.secs + 2))]
    return [node, str(C.ROOT / "tools" / "bench" / "ipc" / "rt_client.mjs"), "--base", base, "--clients", str(a.clients),
            "--dur", str(int(a.secs)), "--warmup", "0"]


def clients_run(a: argparse.Namespace) -> dict:
    be = _Backend(a)
    cl: subprocess.Popen | None = None
    try:
        be.start()
        fleet_wait_s = be.wait_fleet(a, a.fleet_timeout)
        rss0 = {k: C.rss_mb(p) for k, p in be.pids.items()}
        cl = subprocess.Popen(_clients_cmd(a, be.base), cwd=C.ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert cl.stdout is not None
        lines: list[str] = []
        t_start = None
        end = time.monotonic() + 240
        if a.with_flight60:
            while time.monotonic() < end:  # 全部客户端开始飞行（遮罩揭开 + 1 s 预热）
                line = cl.stdout.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
                if line.startswith("FLIGHT_START"):
                    t_start = time.monotonic()
                    break
            if t_start is None:
                raise RuntimeError("客户端未能开始 flight60：" + " | ".join(lines[-5:]))
        else:
            time.sleep(1.0)  # rt_client 握手与订阅
            t_start = time.monotonic()
        rest: list[str] = []
        threading.Thread(target=lambda: rest.extend(x.rstrip("\n") for x in cl.stdout), daemon=True).start()  # type: ignore[union-attr]
        c0 = {k: _cpu_s(p) for k, p in be.pids.items()}
        w0 = time.monotonic()
        sampler_load = C.LoadSampler()
        while time.monotonic() - w0 < a.secs:
            time.sleep(1.0)
            if any(x.startswith("FLIGHT_DONE") for x in rest):
                break
        w1 = time.monotonic()
        c1 = {k: _cpu_s(p) for k, p in be.pids.items()}
        win = be.perf_window(round(w1 - w0))
        client_subs = be.clients_summary()
        load = sampler_load.stop()
        procs = be.procs()
        restarted = [x.get("name") for x in procs if x.get("name") in be.restarts0
                     and (int(x.get("restarts") or 0) != be.restarts0[x["name"]] or x.get("state") != "RUNNING")]
        try:  # 窗口（缺省 60 s）与 flight60 的 60 s 飞行基本同时结束；更短的窗口（冒烟）不等飞行结束
            cl.wait(timeout=30.0)
        except subprocess.TimeoutExpired:
            cl.kill()
        summary = next((json.loads(x) for x in reversed(rest) if x.startswith("{")), None)
        f = win.get("fields", {})

        def fv(key: str, stat: str) -> float | None:
            v = (f.get(key) or {}).get(stat)
            return float(v) if isinstance(v, (int, float)) else None

        dur = w1 - w0
        cpu = {k: (None if c0.get(k) is None or c1.get(k) is None else (c1[k] - c0[k]) / dur) for k in be.pids}
        stage = {k.split(".", 2)[2]: float(v["p50"]) for k, v in f.items() if k.startswith("sim.stage_ms_per_s.") and "p50" in v}
        return {"api_cpu_core": cpu.get("api"), "sim_cpu_core": cpu.get("sim-core"), "window_s": round(dur, 2),
                "tick_age_ms": {"p50": fv("api.tick_age_p50_ms", "p50"), "p99": fv("api.tick_age_p99_ms", "p99"),
                                "max": fv("api.tick_age_p99_ms", "max")},
                "loop_lag_ms_p99": fv("api.loop_lag_p99_ms", "p99"), "api_cpu_pct_server": fv("api.cpu_pct", "p50"),
                "encodes_per_s": fv("api.encodes_per_s", "p50"),
                "sim": {"rtf": fv("sim.rtf", "p50"), "step_p50_us": fv("sim.step_p50_us", "p50"),
                        "step_p99_us": fv("sim.step_p99_us", "p99"), "step_max_us": fv("sim.step_max_us", "max"),
                        "n_active": fv("sim.n_active", "max"), "catchup_saturated": fv("sim.catchup_saturated", "max"),
                        "stage_ms_per_s": stage},
                "rss_mb": {k: {"start": rss0.get(k), "end": C.rss_mb(p)} for k, p in be.pids.items()},
                "restarted": restarted, "fleet_wait_s": round(fleet_wait_s, 1), "diagnostic": be.diagnostic,
                "clients": summary, "client_subs": client_subs, "load": load}
    finally:
        if cl is not None and cl.poll() is None:
            cl.kill()
        be.close()


def clients_main(a: argparse.Namespace) -> int:
    runs = []
    sampler = C.LoadSampler()
    for i in range(a.runs):
        try:
            r = clients_run(a)
        except RuntimeError as e:  # 后端或机群起不来（例如 N = 1000 布设超时）：本次无效，不产出指标
            sys.stderr.write(f"run {i + 1}/{a.runs}: 无效：{e}\n")
            continue
        runs.append(r)
        sys.stderr.write(f"run {i + 1}/{a.runs}: {json.dumps(r, ensure_ascii=False)}\n")
    load = sampler.stop()
    if not runs:
        sys.stderr.write("错误（退出码 1）：全部运行无效（后端、机群或客户端没有就绪），见上方原因\n")
        return C.EXIT_FAIL
    valid = [r for r in runs if not r["restarted"]]
    flat = [{"api_cpu_core": r["api_cpu_core"], "sim_cpu_core": r["sim_cpu_core"], "tick_age_p50": r["tick_age_ms"]["p50"],
             "tick_age_p99": r["tick_age_ms"]["p99"], "loop_lag_p99": r["loop_lag_ms_p99"], "rtf": r["sim"]["rtf"],
             "step_p50": r["sim"]["step_p50_us"], "step_p99": r["sim"]["step_p99_us"], "step_max": r["sim"]["step_max_us"],
             "run_load_mean": r["load"]["mean"]} for r in (valid or runs)]
    med = {k: C.median_of(flat, k) for k in flat[0]}
    cpu_valid = (med["run_load_mean"] or 99.0) < 6
    gates, ok = C.judge([("cpu_api_core", med["api_cpu_core"], "<=", 0.35),
                         ("tick_age_ms_p99", med["tick_age_p99"], "<=", 15.0)], cpu_valid)
    report = [{"metric": "loop_lag_ms_p99", "value": med["loop_lag_p99"], "op": "<=", "threshold": 10.0, "result": "只报告（P1）"}]
    if len(valid) < len(runs):
        ok = False
        gates.append({"metric": "valid_runs", "value": len(valid), "op": "==", "threshold": len(runs),
                      "result": "无效（PERF-E008：窗口内 api 或 sim-core 重启）"})
    if any(r["diagnostic"] for r in runs):
        gates.append({"metric": "diagnostic", "value": 1, "op": "==", "threshold": 0, "result": "不判定（AWR_BENCH_RUNTIME_CONFIG）"})
        ok = False
    r0 = runs[len(runs) // 2]
    record = {"n": a.n, "rate": 1.0, "clients": a.clients, "with_flight60": a.with_flight60, "dur_s": r0["window_s"],
              "rtf": med["rtf"] if med["rtf"] is not None else 0.0, "cpu_core": med["sim_cpu_core"] or 0.0,
              "api_cpu_core": med["api_cpu_core"],
              "step_us": {"p50": med["step_p50"] or 0.0, "p99": med["step_p99"] or 0.0, "max": med["step_max"] or 0.0},
              "catchup_saturated": int(r0["sim"]["catchup_saturated"] or 0), "stage_ms_per_s": r0["sim"]["stage_ms_per_s"],
              "tick_age_ms": ({"p50": med["tick_age_p50"], "p99": med["tick_age_p99"]}
                              if med["tick_age_p50"] is not None and med["tick_age_p99"] is not None else None),
              "rss_mb": {"start": float(r0["rss_mb"].get("api", {}).get("start") or 0.0),
                         "end": float(r0["rss_mb"].get("api", {}).get("end") or 0.0)},
              "kernel": "numba", "load": load}
    params = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(a).items()}
    d = C.write_outputs("bench_state", a.out, record, {"params": params, "median": med, "runs": runs, "gates": gates,
                                                       "report": report, "ok": ok, "load": load})
    print(json.dumps({"tool": "bench_state", "mode": "clients", "ok": ok, "out": str(d), "median": med, "gates": gates},
                     ensure_ascii=False))
    return C.EXIT_OK if ok else C.EXIT_FAIL


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--role", default="reader", choices=("reader", "writer"))
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--hz", type=float, default=125.0)
    ap.add_argument("--secs", type=float, default=None,
                    help="测量窗口秒数；缺省：客户端口径（--clients K）60 s（18 §8.7(2)、D1-AC-08），合成写者口径 15 s")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--load", default="synthetic", choices=("synthetic", "none"))
    ap.add_argument("--work-us", type=float, default=2500.0, help="synthetic 负载下每步模拟的仿真计算耗时（µs）")
    ap.add_argument("--clients", type=int, default=0, help="K > 0：真实后端 + K 个客户端（D1-AC-08 口径）")
    ap.add_argument("--with-flight60", action="store_true", help="客户端为 headless Chromium flight60（否则 rt_client.mjs）")
    ap.add_argument("--client-query", default="scene=full&n=1000", help="flight60 查询串（18 §8.7(2) MS5 起的口径）")
    ap.add_argument("--world", default="shenzhen")
    ap.add_argument("--scenario", default="ladder-shenzhen", help="机群剧本；none 时用 AWR_SIM_N=--n 骨架布设")
    ap.add_argument("--scenario-profile", default="n1000")
    ap.add_argument("--wait-mark", default="ladder.steady")
    ap.add_argument("--fleet-timeout", type=float, default=240.0, help="等机群就绪（剧本标记或 n_active）的上限秒数")
    ap.add_argument("--path", default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    a = ap.parse_args(argv)
    if a.secs is None:  # D1 验收第 2 轮 4.4：客户端口径此前缺省 15 s，与文档的 60 s 不一致（FX2-R3-sim）
        a.secs = 60.0 if a.clients > 0 else 15.0
    if a.role == "writer":
        writer(a)
        return 0
    if a.with_flight60 and a.clients <= 0:
        sys.stderr.write("错误（退出码 2）：--with-flight60 需要 --clients K（K ≥ 1）\n")
        return 2
    try:
        lock = C.acquire_perf_lock(a.no_lock)
    except TimeoutError as e:
        sys.stderr.write(f"错误（退出码 14）：{e}\n修复：等待构建与测试结束后重试\n")
        return C.EXIT_ENV
    if not C.wait_load(a.no_load_wait):
        sys.stderr.write("错误（退出码 14）：开跑前 loadavg 10 min 内未降到 4 以下（PERF-E002）\n修复：等待负载下降\n")
        return C.EXIT_ENV
    if a.clients > 0:
        rc = clients_main(a)
        del lock
        return rc
    try:
        import uvloop

        runner = uvloop.run
    except ImportError:
        runner = asyncio.run
    sampler = C.LoadSampler()
    runs = []
    for i in range(a.runs):
        r = runner(reader_run(a))
        runs.append(r)
        sys.stderr.write(f"run {i + 1}/{a.runs}: {json.dumps(r)}\n")
    load = sampler.stop()
    flat = [{"reader_cpu_core": r["reader_cpu_core"], "tick_age_p50": r["tick_age_ms"]["p50"],
             "tick_age_p99": r["tick_age_ms"]["p99"], "publish_p99": r["writer"]["publish_us"]["p99"],
             "begin_p99": r["writer"]["begin_commit_us"]["p99"], "writer_cpu": r["writer"]["cpu_core"],
             "rtf": r["writer"]["rtf"], "step_p50": r["writer"]["step_us"]["p50"], "step_p99": r["writer"]["step_us"]["p99"],
             "step_max": r["writer"]["step_us"]["max"], "loop_lag_p99": r["loop_lag_ms_p99"]} for r in runs]
    med = {k: C.median_of(flat, k) for k in flat[0]}
    cpu_valid = load["mean"] < 6
    gates, ok = C.judge([("publish_us_p99", med["publish_p99"], "<=", 300.0),
                         ("begin_commit_us_p99", med["begin_p99"], "<=", 300.0),
                         ("cpu_reader_core", med["reader_cpu_core"], "<=", 0.03),
                         ("tick_age_ms_p99", med["tick_age_p99"], "<=", 15.0)], cpu_valid)
    w0 = runs[len(runs) // 2]["writer"]
    record = {"n": a.n, "rate": 1.0, "clients": a.clients, "with_flight60": a.with_flight60, "dur_s": a.secs,
              "rtf": med["rtf"], "cpu_core": med["writer_cpu"], "api_cpu_core": med["reader_cpu_core"],
              "step_us": {"p50": med["step_p50"], "p99": med["step_p99"], "max": med["step_max"]}, "catchup_saturated": 0,
              "stage_ms_per_s": w0["stage_ms_per_s"], "tick_age_ms": {"p50": med["tick_age_p50"], "p99": med["tick_age_p99"]},
              "rss_mb": w0["rss_mb"], "kernel": "numpy", "load": load}
    d = C.write_outputs("bench_state", a.out, record, {"params": {k: v for k, v in vars(a).items() if k != "out"} | {
        "out": str(a.out) if a.out else None}, "median": med, "runs": runs, "gates": gates, "ok": ok, "load": load})
    print(json.dumps({"tool": "bench_state", "ok": ok, "out": str(d), "median": med, "gates": gates}, ensure_ascii=False))
    del lock
    return C.EXIT_OK if ok else C.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
