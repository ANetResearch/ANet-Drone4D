#!/usr/bin/env python3
"""命令平面与事件平面基准（D1-AC-10；M11-AC-004、M11-AC-026；PERF-AC-037；AWR-18 §7.2、§7.6）。

sim-core 模拟（独立进程，同步固定步长 250 Hz）：经 `awr.runtime.bus.ZenohBus` 服务 `ctl/sim-core/cmd`（回调只入队，
步顶 drain 锁存并回复 Admission，随后发 `cmd.accepted`，200 ms 后发 `cmd.succeeded`）；背景事件按 `--events` 条/s 均匀
分布在各步，经 `EventPublisher` 按步合批（DROP）并自动服务 `evt/sim-core/_replay`；每 2 步向 StateRing 发布 N 行。
Gateway 模拟（本进程，uvloop）：以 `--cps` 条/s 串行 `bus.call(ctl/sim-core/cmd)`，统计准入 RTT 与失败；
`EventSubscriber` 在 60 Hz tick 中 pump，按生产者序交付；`--drop-every k` 人为丢弃每第 k 条事件消息，验证 1 s 内经
`_replay` 补齐且交付顺序不变。

丢弃注入的口径（FX2-R2-gateway 修正）：只在 60 s 测量窗口内注入；窗口结束后停止注入，tick 与 pump 继续运行 `--drain` 秒
（缺省 1.5 s，大于 1 s 补齐时限），之后仍未交付的丢弃事件才计为"未补齐"。此前注入一直持续到 pump 停止的那一刻，最后约
70 ms 内被丢弃的消息来不及补拉，被误计为未补齐（ACC-1 第 1 轮 3 次中 2 次各 2、3 条，均为同一条尾部消息内的事件）。
已经交付（例如被先到的补拉回复带回）的事件即使其原消息随后被丢弃，也不计入丢弃集合。

门禁：命令失败 0；准入 RTT p99 ≤ 25 ms；事件未补齐缺口 0、乱序 0；（丢弃注入时）补齐时延最大值 ≤ 1000 ms。
用法：python tools/bench/ipc/bench_cmd.py [--secs 60] [--cps 50] [--events 570] [--drop-every 0] [--drain 1.5] [--runs 1] [--out DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C

from awr.contracts import LAYOUT_ID, bus_keys
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32
from awr.runtime.bus import ZenohBus
from awr.runtime.events import EventPublisher, EventSubscriber
from awr.runtime.statering import StateRing

NS = "awr/bench/cmd"
WARMUP_S = 2.0


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------- sim-core 模拟（子进程）
def sim(a: argparse.Namespace) -> None:
    bus = ZenohBus.open("sim-core", namespace=NS, listen=["tcp/127.0.0.1:0"], connect=[a.endpoint])
    inbox: queue.SimpleQueue = queue.SimpleQueue()
    bus.serve(bus_keys.ctl_cmd("sim-core"), inbox.put)  # 回调线程只入队
    ev = EventPublisher(bus, "sim-core", epoch=1)
    ring_dir = Path(tempfile.mkdtemp(prefix="awr-bench-cmd-", dir="/dev/shm"))
    ring = StateRing.create(ring_dir / "state.sim-core", capacity=max(1024, a.n), layout_id=LAYOUT_ID)
    full, lite = np.zeros(a.n, DRONE_STATE64), np.zeros(a.n, SWARM_LITE32)
    bus.ready()
    print("READY", flush=True)
    hz = a.hz
    dt = 1.0 / hz
    t_end = time.perf_counter() + WARMUP_S + a.secs + max(3.0, a.drain + 1.5)  # 生产者持续到网关排空结束之后
    nxt = time.perf_counter()
    k = 0
    acc = 0.0
    pending: collections.deque = collections.deque()
    step_us, drain_s, flush_s, pub_s = [], 0.0, 0.0, 0.0
    cpu0 = t0 = None
    k0 = 0
    admitted = 0
    rss0 = C.rss_mb()
    while time.perf_counter() < t_end:
        nxt += dt
        sl = nxt - time.perf_counter()
        if sl > 0:
            time.sleep(sl)
        if cpu0 is None and time.perf_counter() > t_end - a.secs - max(3.0, a.drain + 1.5):
            cpu0, t0, k0 = time.process_time(), time.perf_counter(), k
            step_us.clear()
            drain_s = flush_s = pub_s = 0.0
        ts = time.perf_counter()
        k += 1
        t_sim = int(k * dt * 1e9)
        ring.heartbeat(t_sim, 1, 1000, step_seq=k)
        # ① 步顶锁存：drain inbox，回复 Admission（sim-core 永不阻塞：回复与事件都是 DROP）
        while True:
            try:
                req = inbox.get_nowait()
            except queue.Empty:
                break
            m = req.msg()
            req.reply_msg({"cid": m.get("cid"), "status": "accepted", "code": 0, "t_sim_ns": t_sim, "epoch": 1, "segment": 1})
            ev.emit("cmd.accepted", t_sim_ns=t_sim, cid=m.get("cid"), uav=m.get("uav"), op="goto")
            pending.append((k + int(0.2 * hz), m.get("cid"), m.get("uav")))
            admitted += 1
        td = time.perf_counter()
        # ② 发布状态（125 Hz）
        if k % 2 == 0:
            ring.publish(full, lite, t_sim, 1)
        tp = time.perf_counter()
        # ③ 事件：完成事件 + 背景事件（按步合批）
        while pending and pending[0][0] <= k:
            _, cid, uav = pending.popleft()
            ev.emit("cmd.succeeded", t_sim_ns=t_sim, cid=cid, uav=uav, op="goto")
        acc += a.events * dt
        while acc >= 1.0:
            acc -= 1.0
            ev.emit("safety.separation", t_sim_ns=t_sim, severity=2, uav=f"uav{(k % a.n) + 1:04d}", code=1, value=4.0,
                    threshold=5.0)
        ev.flush()
        te = time.perf_counter()
        step_us.append((te - ts) * 1e6)
        drain_s += td - ts
        pub_s += tp - td
        flush_s += te - tp
    wall = time.perf_counter() - t0
    sim_s = (k - k0) * dt
    res = {"cpu_core": (time.process_time() - cpu0) / wall, "step_us": C.stats3(step_us), "rtf": sim_s / wall,
           "stage_ms_per_s": {"inbox_drain": round(drain_s * 1e3 / sim_s, 3), "publish": round(pub_s * 1e3 / sim_s, 3),
                              "events_flush": round(flush_s * 1e3 / sim_s, 3)},
           "rss_mb": {"start": rss0, "end": C.rss_mb()}, "events_emitted": ev.stats["emitted"], "replays": ev.stats["replays"],
           "admitted": admitted}
    print("RESULT " + json.dumps(res), flush=True)
    time.sleep(0.3)
    ev.close()
    bus.close()
    ring.close()
    import shutil

    shutil.rmtree(ring_dir, ignore_errors=True)


# ---------------------------------------------------------------- Gateway 模拟
async def gateway_run(a: argparse.Namespace) -> dict:
    loop = asyncio.get_running_loop()
    ep = f"tcp/127.0.0.1:{_free_port()}"
    bus = ZenohBus.open("api", namespace=NS, listen=[ep], connect=[], loop=loop)
    proc = subprocess.Popen([sys.executable, __file__, "--role", "sim", "--endpoint", ep, "--secs", str(a.secs), "--events",
                             str(a.events), "--n", str(a.n), "--hz", str(a.hz), "--drain", str(a.drain)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "READY"
        order = {"delivered": 0, "reorders": 0, "last": {}}
        dropped: dict[tuple[str, int], float] = {}
        recover_ms: list[float] = []
        gaps: list[tuple] = []
        counting = [False]

        def on_events(producer: str, evs: list[dict]) -> None:
            now = time.monotonic()
            for e in evs:
                key = (producer, e["epoch"])
                last = order["last"].get(key, 0)
                if e["seq"] <= last:
                    order["reorders"] += 1
                order["last"][key] = e["seq"]
                if counting[0]:
                    order["delivered"] += 1
                t = dropped.pop((producer, e["seq"]), None)
                if t is not None:
                    recover_ms.append((now - t) * 1e3)

        def on_gap(producer: str, epoch: int, lo: int, hi: int) -> None:
            gaps.append((producer, epoch, lo, hi))

        sub = EventSubscriber(bus, on_events=on_events, on_gap=on_gap)
        n_msgs = [0]
        n_drops = [0]
        inject = [False]  # 只在测量窗口内注入丢弃；排空阶段不注入（见模块说明）
        if a.drop_every > 0:
            import msgpack

            def flt(key: str, raw: bytes) -> bool:
                n_msgs[0] += 1
                if inject[0] and n_msgs[0] % a.drop_every == 0:
                    n_drops[0] += 1
                    t = time.monotonic()
                    producer = key.split("/")[1]
                    for e in msgpack.unpackb(raw):
                        if e["seq"] > order["last"].get((producer, e["epoch"]), 0):  # 已交付的不算丢弃
                            dropped[(producer, e["seq"])] = t
                    return False
                return True

            sub.filter = flt

        stop = [False]
        lags: list[float] = []

        async def ticker() -> None:
            nxt = time.perf_counter()
            while not stop[0]:
                nxt += 1 / 60
                await asyncio.sleep(max(0.0, nxt - time.perf_counter()))
                lags.append((time.perf_counter() - nxt) * 1e3)
                sub.pump()

        tk = asyncio.ensure_future(ticker())
        await asyncio.sleep(WARMUP_S)
        lags.clear()
        counting[0] = True
        inject[0] = True
        rtts, fails = [], 0
        cpu0, t0 = time.process_time(), time.perf_counter()
        kc = 0
        while time.perf_counter() - t0 < a.secs:
            kc += 1
            ts = time.perf_counter()
            try:
                await bus.call(bus_keys.ctl_cmd("sim-core"), {"cid": f"c{kc}", "op": "goto", "uav": "uav0007",
                                                              "args": {"target_enu_m": [1.0, 2.0, 30.0]}})
                rtts.append((time.perf_counter() - ts) * 1e3)
            except Exception:
                fails += 1
            await asyncio.sleep(max(0.0, 1.0 / a.cps - (time.perf_counter() - ts)))
        cpu = (time.process_time() - cpu0) / (time.perf_counter() - t0)
        inject[0] = False
        await asyncio.sleep(a.drain)  # 排空：tick 与 pump 继续，让窗口末尾的丢弃完成补拉（≥ 1 s 补齐时限）
        counting[0] = False
        stop[0] = True
        await tk
        sub.pump()
        now = time.monotonic()
        oldest_unrec_ms = round(max((now - t for t in dropped.values()), default=0.0) * 1e3, 1)
        out = (await asyncio.to_thread(proc.communicate, timeout=60))[0]
        w = json.loads(next(line for line in out.splitlines() if line.startswith("RESULT "))[7:])
        res = {"cmds": len(rtts), "fails": fails, "rtt_ms": C.stats3(rtts) | {"p95": round(C.pct(rtts, 95), 3)},
               "api_cpu_core": cpu, "loop_lag_ms_p99": round(C.pct(lags, 99), 3),
               "events_delivered": order["delivered"], "events_per_s": round(order["delivered"] / (a.secs + a.drain), 1),
               "reorders": order["reorders"], "gaps_unrecovered": len(gaps), "gap_list": gaps[:20],
               "drops_injected": n_drops[0],
               "recover_ms": C.stats3(recover_ms) if recover_ms else None, "unrecovered_after_drop": len(dropped),
               "unrecovered_oldest_ms": oldest_unrec_ms, "drain_s": a.drain,
               "subscriber": dict(sub.stats), "sim": w}
        sub.close()
        return res
    finally:
        if proc.poll() is None:
            proc.kill()
        bus.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--role", default="gateway", choices=("gateway", "sim"))
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--secs", type=float, default=60.0)
    ap.add_argument("--cps", type=float, default=50.0)
    ap.add_argument("--events", type=float, default=570.0)
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--hz", type=float, default=250.0)
    ap.add_argument("--drop-every", type=int, default=0, help="人为丢弃每第 k 条事件消息（验证 _replay 补齐）")
    ap.add_argument("--drain", type=float, default=1.5, help="窗口结束后停止注入、继续 pump 的排空秒数（须 > 1 s 补齐时限）")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    a = ap.parse_args(argv)
    if a.role == "sim":
        sim(a)
        return 0
    try:
        lock = C.acquire_perf_lock(a.no_lock)
    except TimeoutError as e:
        sys.stderr.write(f"错误（退出码 14）：{e}\n修复：等待构建与测试结束后重试\n")
        return C.EXIT_ENV
    if not C.wait_load(a.no_load_wait):
        sys.stderr.write("错误（退出码 14）：开跑前 loadavg 10 min 内未降到 4 以下（PERF-E002）\n修复：等待负载下降\n")
        return C.EXIT_ENV
    try:
        import uvloop

        runner = uvloop.run
    except ImportError:
        runner = asyncio.run
    sampler = C.LoadSampler()
    runs = []
    for i in range(a.runs):
        r = runner(gateway_run(a))
        runs.append(r)
        sys.stderr.write(f"run {i + 1}/{a.runs}: {json.dumps({k: v for k, v in r.items() if k != 'gap_list'})}\n")
    load = sampler.stop()
    flat = [{"fails": r["fails"], "rtt_p99": r["rtt_ms"]["p99"], "rtt_p50": r["rtt_ms"]["p50"], "reorders": r["reorders"],
             "gaps": r["gaps_unrecovered"], "recover_max": (r["recover_ms"] or {}).get("max"),
             "unrecovered": r["unrecovered_after_drop"], "api_cpu": r["api_cpu_core"], "events_per_s": r["events_per_s"],
             "sim_cpu": r["sim"]["cpu_core"], "rtf": r["sim"]["rtf"], "step_p50": r["sim"]["step_us"]["p50"],
             "step_p99": r["sim"]["step_us"]["p99"], "step_max": r["sim"]["step_us"]["max"]} for r in runs]
    med = {k: C.median_of(flat, k) for k in flat[0]}
    worst = {"fails": max(f["fails"] for f in flat), "reorders": max(f["reorders"] for f in flat),
             "gaps": max(f["gaps"] for f in flat), "unrecovered": max(f["unrecovered"] for f in flat)}
    gates_in = [("cmd_fails", float(worst["fails"]), "==", 0.0), ("rtt_ms_p99", med["rtt_p99"], "<=", 25.0),
                ("event_reorders", float(worst["reorders"]), "==", 0.0), ("event_gaps_unrecovered", float(worst["gaps"]), "==", 0.0)]
    if a.drop_every:
        gates_in += [("event_recover_ms_max", med["recover_max"], "<=", 1000.0),
                     ("event_unrecovered_after_drop", float(worst["unrecovered"]), "==", 0.0)]
    gates, ok = C.judge(gates_in, load["mean"] < 6)
    r0 = runs[len(runs) // 2]["sim"]
    record = {"n": a.n, "rate": 1.0, "clients": 0, "with_flight60": False, "dur_s": a.secs, "rtf": med["rtf"],
              "cpu_core": med["sim_cpu"], "api_cpu_core": med["api_cpu"],
              "step_us": {"p50": med["step_p50"], "p99": med["step_p99"], "max": med["step_max"]}, "catchup_saturated": 0,
              "stage_ms_per_s": r0["stage_ms_per_s"], "tick_age_ms": None, "rss_mb": r0["rss_mb"], "kernel": "numpy",
              "load": load}
    params = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(a).items()}
    d = C.write_outputs("bench_cmd", a.out, record, {"params": params, "median": med, "worst": worst, "runs": runs,
                                                     "gates": gates, "ok": ok, "load": load})
    print(json.dumps({"tool": "bench_cmd", "ok": ok, "out": str(d), "median": med, "gates": gates}, ensure_ascii=False))
    del lock
    return C.EXIT_OK if ok else C.EXIT_FAIL


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    sys.exit(main())
