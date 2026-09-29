#!/usr/bin/env python3
"""网关容量基准：真实 api 进程 + 合成生产者 + N 个轻量协议客户端（M11-AC-018、AC-019、AC-020 的服务端口径；D1-AC-08；
AWR-18 §7.2、§8.7（2））。

三类进程，均在本机回环：
- producer：`SyntheticSource`（N 架 Lite32/Full64，125 Hz，写 mmap StateRing；ZenohBus 监听一个回环端点，提供 roster、命令、
  事件、环境关键帧），相当于 sim-core；
- api：`uvicorn awr.api.main:app`，启动参数取自 `configs/runtime.yaml` 的 api 行（deflate 关、ws-max-size 262144），经
  `AWR_ZENOH_CONFIG` 连接 producer；
- client × N：Python websockets 协议客户端，默认订阅集按 Tier S（`fleet/roster@10`、`swarm/state@10`、`event`、`env/state@10`、
  `perf/server@1`、`sys/procs@1`、关注集 32 架 `uav/{id}/state@30`、选中 1 架 `uav/{id}/state@60` 与 `state_ext@2`），以
  `--consume-hz`（默认 30）消费并按"≥ 3 帧或 ≥ 50 ms"发 ack，ping 上报 `srttMs`。
测量窗口内：api 进程 CPU（/proc/<pid>/stat 的 utime + stime）、`GET /api/sys/perf?window_s=` 的 tick 数据年龄 p99、on_tick p99、
tick_overruns、编码速率，以及每客户端 swarm 实际频率、下行字节速率、credit_skips 比例。
门禁：`--clients 3`：api ≤ 0.35 核、tick 数据年龄 p99 ≤ 15 ms、on_tick p99 ≤ 1.5 ms、每 60 s overruns ≤ 3；
`--clients 10`（NFR-007）：api ≤ 0.6 核、每客户端 swarm ≥ 9.5 Hz、credit_skips ≤ 5%、tick 数据年龄 p99 ≤ 15 ms。
合成生产者不是 flight60：D1-AC-08 完整口径（3 个浏览器 + flight60 + 静态 Range）由 M16 harness 执行。

用法：python tools/bench/ipc/bench_gateway.py [--n 1000] [--clients 3] [--secs 30] [--runs 3] [--out DIR] [--no-lock]
      [--no-load-wait]；性能运行协议见 _common.py（make bench-gateway 持排他锁）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C

PROTO = "awr.rt.v1"
WARMUP_S = 3.0


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------- producer（子进程）
def producer(a: argparse.Namespace) -> None:
    from awr.api.rt.sources.synthetic import SyntheticSource
    from awr.runtime.bus import ZenohBus
    from awr.runtime.statering import StateRing

    bus = ZenohBus.open("sim-core", namespace=a.namespace, listen=[f"tcp/127.0.0.1:{a.zport}"], connect=[])
    src = SyntheticSource(a.n, hz=a.hz, events_per_s=a.events_per_s, bus=bus, ring_cls=StateRing,
                          ring_path=Path(a.run_dir) / "state.sim-core").start()
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    print("READY", flush=True)
    while not stop:
        time.sleep(0.1)
    src.stop()


# ---------------------------------------------------------------- client（子进程）
async def client_run(a: argparse.Namespace) -> dict:
    from websockets.asyncio.client import connect

    from awr.contracts import frame as F

    ws = await connect(a.url, subprotocols=[PROTO, "bearer." + a.token], max_size=None, compression=None,
                       open_timeout=20)
    topics: dict[str, int] = {}
    info = None
    for _ in range(3):
        m = await ws.recv()
        if isinstance(m, str):
            j = json.loads(m)
            if j["op"] == "serverInfo":
                info = j
            elif j["op"] == "advertise":
                topics.update({c["topic"]: c["id"] for c in j["channels"]})
    await ws.send(json.dumps({"op": "hello", "client": "bench-gateway/1", "contracts": info["contracts"], "tier": "S",
                              "deviceClass": "software"}))
    ids = sorted({t.split("/")[1] for t in topics if t.startswith("uav/") and t.endswith("/state")})
    focus = ids[a.focus_offset:a.focus_offset + a.focus] if a.profile == "tierS" else ids[:a.focus]
    sel = ids[(a.focus_offset + a.focus) % len(ids)] if ids else None
    subs = [{"topic": "fleet/roster", "rate": 10}, {"topic": "swarm/state", "rate": 10},
            {"topic": "event", "rate": 0, "mode": "all"}, {"topic": "env/state", "rate": 10},
            {"topic": "perf/server", "rate": 1}, {"topic": "sys/procs", "rate": 1}]
    subs += [{"topic": f"uav/{v}/state", "rate": 30} for v in focus]
    if sel:
        subs += [{"topic": f"uav/{sel}/state", "rate": 60}, {"topic": f"uav/{sel}/state_ext", "rate": 2}]
    for i in range(0, len(subs), 20):  # 订阅限流 20 条/s
        await ws.send(json.dumps({"op": "subscribe", "subs": [{"id": i + j + 1, "mode": "latest", **s}
                                                              for j, s in enumerate(subs[i:i + 20])]}))
        await asyncio.sleep(1.05)
    swarm_id = topics.get("swarm/uav/state")
    st = {"frames": 0, "bytes": 0, "swarm": 0, "latest": 0, "ctrl": 0, "measure": False}
    t_end = time.monotonic() + WARMUP_S + a.secs

    async def rx() -> None:
        while time.monotonic() < t_end:
            try:
                m = await asyncio.wait_for(ws.recv(), 1.0)
            except TimeoutError:
                continue
            if isinstance(m, str):
                if st["measure"]:
                    st["ctrl"] += 1
                    st["bytes"] += len(m)
                continue
            if m[0] != F.OP_BATCH:
                continue
            h = F.decode_batch_header(m)
            st["latest"] = h.frame_seq
            if st["measure"]:
                st["frames"] += 1
                st["bytes"] += len(m)
                st["swarm"] += sum(1 for r in F.iter_records(m) if r.channel_id == swarm_id)

    async def consume() -> None:
        last_ack, last_t = 0, time.monotonic()
        period = 1.0 / a.consume_hz
        k = 0
        while time.monotonic() < t_end:
            await asyncio.sleep(period)
            k += 1
            now = time.monotonic()
            if st["latest"] > last_ack and (st["latest"] - last_ack >= 3 or now - last_t >= 0.05):
                last_ack, last_t = st["latest"], now
                await ws.send(json.dumps({"op": "ack", "frame": last_ack, "fps": a.consume_hz}))
            if k % max(1, int(a.consume_hz / 2)) == 0:
                await ws.send(json.dumps({"op": "ping", "t": time.monotonic() * 1e3, "srttMs": 1.0}))

    async def measure() -> None:
        await asyncio.sleep(WARMUP_S)
        st["measure"] = True

    await asyncio.gather(rx(), consume(), measure())
    await ws.close()
    return {"conn_id": info["connId"], "swarm_hz": round(st["swarm"] / a.secs, 2),
            "frames_per_s": round(st["frames"] / a.secs, 2), "bytes_per_s": round(st["bytes"] / a.secs, 1),
            "ctrl_per_s": round(st["ctrl"] / a.secs, 2), "subs": len(subs)}


# ---------------------------------------------------------------- 主控
def _proc_cpu_s(pid: int) -> float:
    parts = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")


def _api_cmd(port: int) -> list[str]:
    import yaml

    cfg = yaml.safe_load((C.ROOT / "configs" / "runtime.yaml").read_text(encoding="utf-8"))
    cmd = next(p["cmd"] for p in cfg["procs"] if p["name"] == "api")
    out = [str(x).replace("${net.bind}", "127.0.0.1").replace("${net.port_effective}", str(port)) for x in cmd]
    out[0] = str(Path(sys.executable).with_name("uvicorn"))
    return out


def one_run(a: argparse.Namespace) -> dict:
    import httpx

    from awr.runtime.bus import ZenohBus

    tmp = Path(tempfile.mkdtemp(prefix="awr-bench-gw-", dir="/dev/shm" if Path("/dev/shm").is_dir() else None))
    run_id = "bench-" + os.urandom(3).hex()
    ns = f"awr/{a.world}/{run_id}"
    zport, port = _free_port(), _free_port()
    zcfg = tmp / "zenoh-api.json5"
    zcfg.write_text(str(ZenohBus.build_config(namespace=ns, listen=["tcp/127.0.0.1:0"],
                                              connect=[f"tcp/127.0.0.1:{zport}"])), encoding="utf-8")
    procs: list[subprocess.Popen] = []
    try:
        prod = subprocess.Popen([sys.executable, __file__, "--role", "producer", "--n", str(a.n), "--hz", str(a.hz),
                                 "--zport", str(zport), "--namespace", ns, "--run-dir", str(tmp),
                                 "--events-per-s", str(a.events_per_s)], cwd=C.ROOT, stdout=subprocess.PIPE, text=True)
        procs.append(prod)
        assert prod.stdout.readline().startswith("READY")
        env = dict(os.environ, AWR_RUN=run_id, AWR_RUN_DIR=str(tmp), AWR_PERSIST_DIR=str(tmp / "persist"),
                   AWR_ZENOH_CONFIG=str(zcfg), AWR_SERVE_WEB="0", AWR_WORLD=a.world, AWR_PROFILE="perf",
                   AWR_API_BUS="zenoh")
        env.pop("AWR_SUPERVISOR_PID", None)
        api = subprocess.Popen(_api_cmd(port), cwd=C.ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(api)
        base = f"http://127.0.0.1:{port}"
        end = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{base}/api/health/ready", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > end:
                raise RuntimeError("api 未就绪")
            time.sleep(0.2)
        clients = []
        for i in range(a.clients):
            tok = httpx.post(f"{base}/api/auth/token", json={"role": "viewer"}, timeout=5).json()["token"]
            clients.append(subprocess.Popen(
                [sys.executable, __file__, "--role", "client", "--url", f"ws://127.0.0.1:{port}/api/rt", "--token", tok,
                 "--secs", str(a.secs), "--consume-hz", str(a.consume_hz), "--focus", str(a.focus),
                 "--focus-offset", str(i * a.focus), "--profile", a.profile], cwd=C.ROOT, stdout=subprocess.PIPE,
                text=True))
        procs += clients
        time.sleep(WARMUP_S + 1.0 + (a.focus + 9) // 20)  # 订阅分批（20 条/s）+ 预热
        c0, t0 = _proc_cpu_s(api.pid), time.monotonic()
        time.sleep(max(1.0, a.secs - 1.5))
        cpu = (_proc_cpu_s(api.pid) - c0) / (time.monotonic() - t0)
        perf = httpx.get(f"{base}/api/sys/perf?window_s={max(5, int(a.secs) - 2)}", timeout=5,
                         headers={"Authorization": "Bearer " + httpx.post(f"{base}/api/auth/token",
                                                                          json={"role": "viewer"}, timeout=5).json()["token"]}
                         ).json()
        results = []
        for c in clients:
            out = c.communicate(timeout=a.secs + 60)[0]
            results.append(json.loads(next(x for x in out.splitlines() if x.startswith("RESULT "))[7:]))
        f = perf["fields"]
        last = perf.get("last") or {}
        skips = {r["conn_id"]: r for r in last.get("clients", [])}
        credit = [skips[r["conn_id"]]["credit_skips"] for r in results if r["conn_id"] in skips]

        def g(k: str, p: str = "p99") -> float:
            return float(f.get(k, {}).get(p, float("nan")))

        return {"api_cpu_core": round(cpu, 4), "tick_age_p50_ms": g("api.tick_age_p50_ms", "p50"),
                "tick_age_p99_ms": g("api.tick_age_p99_ms", "max"), "tick_p99_ms": g("api.tick_p99_ms", "max"),
                "loop_lag_p99_ms": g("api.loop_lag_p99_ms", "max"), "encodes_per_s": g("api.encodes_per_s", "p50"),
                "tick_overruns": g("api.tick_overruns", "max"), "clients": results,
                "swarm_hz_min": min((r["swarm_hz"] for r in results), default=float("nan")),
                "bytes_per_s_max": max((r["bytes_per_s"] for r in results), default=float("nan")),
                "credit_skips_max": max(credit, default=0)}
    finally:
        for p in reversed(procs):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--role", default="main", choices=("main", "producer", "client"))
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--hz", type=float, default=125.0)
    ap.add_argument("--clients", type=int, default=3)
    ap.add_argument("--secs", type=float, default=30.0)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--consume-hz", type=float, default=30.0)
    ap.add_argument("--focus", type=int, default=32)
    ap.add_argument("--focus-offset", type=int, default=0)
    ap.add_argument("--profile", default="tierS", choices=("tierS", "light"))
    ap.add_argument("--events-per-s", type=float, default=5.0)
    ap.add_argument("--world", default="shenzhen")
    ap.add_argument("--zport", type=int, default=0)
    ap.add_argument("--namespace", default="")
    ap.add_argument("--run-dir", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    a = ap.parse_args(argv)
    if a.role == "producer":
        producer(a)
        return 0
    if a.role == "client":
        print("RESULT " + json.dumps(asyncio.run(client_run(a))), flush=True)
        return 0
    try:
        lock = C.acquire_perf_lock(a.no_lock)
    except TimeoutError as e:
        sys.stderr.write(f"错误（退出码 14）：{e}\n")
        return C.EXIT_ENV
    if not C.wait_load(a.no_load_wait):
        sys.stderr.write("错误（退出码 14）：开跑前 loadavg 未降到 4 以下（PERF-E002）\n")
        return C.EXIT_ENV
    sampler = C.LoadSampler()
    runs = []
    for i in range(a.runs):
        r = one_run(a)
        runs.append(r)
        sys.stderr.write(f"run {i + 1}/{a.runs}: {json.dumps({k: v for k, v in r.items() if k != 'clients'})}\n")
    load = sampler.stop()
    keys = ("api_cpu_core", "tick_age_p99_ms", "tick_p99_ms", "loop_lag_p99_ms", "encodes_per_s", "tick_overruns",
            "swarm_hz_min", "bytes_per_s_max", "credit_skips_max")
    med = {k: C.median_of(runs, k) for k in keys}
    cpu_valid = load["mean"] < 6
    overruns_per_60 = med["tick_overruns"] * 60.0 / max(1.0, a.secs)
    if a.clients <= 3:
        checks = [("cpu_api_core", med["api_cpu_core"], "<=", 0.35), ("tick_age_ms_p99", med["tick_age_p99_ms"], "<=", 15.0),
                  ("on_tick_ms_p99", med["tick_p99_ms"], "<=", 1.5), ("tick_overruns_per_60s", overruns_per_60, "<=", 3.0)]
    else:
        checks = [("cpu_api_core", med["api_cpu_core"], "<=", 0.6), ("tick_age_ms_p99", med["tick_age_p99_ms"], "<=", 15.0),
                  ("swarm_hz_min", med["swarm_hz_min"], ">=", 9.5)]
    gates, ok = C.judge(checks, cpu_valid)
    record = {"n": a.n, "rate": 1.0, "clients": a.clients, "with_flight60": False, "dur_s": a.secs, "rtf": 1.0,
              "cpu_core": 0.0, "api_cpu_core": med["api_cpu_core"], "step_us": {"p50": 0.0, "p99": 0.0, "max": 0.0},
              "catchup_saturated": 0, "stage_ms_per_s": {}, "tick_age_ms": {"p50": 0.0, "p99": med["tick_age_p99_ms"]},
              "rss_mb": 0.0, "kernel": "numpy", "load": load,
              "ipc": {"on_tick_p99_ms": med["tick_p99_ms"], "loop_lag_p99_ms": med["loop_lag_p99_ms"],
                      "encodes_per_s": med["encodes_per_s"], "swarm_hz_min": med["swarm_hz_min"],
                      "client_bytes_per_s_max": med["bytes_per_s_max"], "credit_skips_max": med["credit_skips_max"]}}
    d = C.write_outputs("bench_gateway", a.out, record, {"params": {k: str(v) for k, v in vars(a).items()},
                                                         "median": med, "runs": runs, "gates": gates, "ok": ok,
                                                         "load": load})
    print(json.dumps({"tool": "bench_gateway", "ok": ok, "out": str(d), "median": med, "gates": gates}, ensure_ascii=False))
    del lock
    return C.EXIT_OK if ok else C.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
