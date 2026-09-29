#!/usr/bin/env python3
"""编码与装帧耗时（M11-AC-019 的"编码与装帧 p99 ≤ 0.3 ms"；M11 §9.3 r27 bench_encode 迁移；AWR-18 §7.6）。

只保留四种编码：raw Full64（`uav/{id}/state` 64 B 切片）、raw Lite32（N 架整群一条记录）、msgpack（state_ext 形状）与 JSON
（对照），并测每 tick 为 3 个客户端装帧（Tier S 默认订阅集：swarm + 32 架关注集 + 1 架选中 + roster + env）的耗时。
装帧走生产代码 `ClientSession.assemble()`（发送时装帧、编码缓存命中），不经网络。

门禁：装帧（含编码）每 tick p99 ≤ 0.3 ms（N = 1000、3 客户端）。
用法：python tools/bench/ipc/bench_encode.py [--n 1000] [--clients 3] [--ticks 3000] [--runs 3] [--no-lock] [--no-load-wait]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import msgpack
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C

from awr.api.ratelimit import RateLimiter
from awr.api.rt.channels import ChannelRegistry
from awr.api.rt.session import ClientSession
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32


class _Clock:
    global_epoch = 1


class _Gw:
    def __init__(self) -> None:
        self.registry = ChannelRegistry()
        self.clock = _Clock()
        self.limiter = RateLimiter({})
        self.frame_t_sim_ns = 0
        self.mode = "live"
        self.event_ch = self.registry.add_fixed(6, "event")

    def on_subchan_added(self, s, sc) -> None:
        pass

    def on_subs_changed(self, s) -> None:
        pass


class _Ws:
    pass


class _P:
    id = "p-bench"
    role = "viewer"


def micro(n: int, iters: int) -> dict:
    full = np.zeros(n, DRONE_STATE64)
    lite = np.zeros(n, SWARM_LITE32)
    ext = [[i, {"lifecycle": "READY", "battery": {"soc_pct": 90.0}, "accel_mps2": [0.0, 0.1, 9.8]}] for i in range(n)]
    out = {}
    for name, fn in (("full64_slice_us", lambda: bytes(memoryview(full.tobytes())[64:128])),
                     ("lite32_copy_us", lambda: lite.tobytes()),
                     ("msgpack_state_ext_us", lambda: msgpack.packb(ext[:32], use_bin_type=True)),
                     ("json_state_ext_us", lambda: json.dumps(ext[:32], separators=(",", ":")))):
        ts = []
        for _ in range(iters):
            t = time.perf_counter()
            fn()
            ts.append((time.perf_counter() - t) * 1e6)
        out[name] = C.stats3(ts)
    return out


def assemble_run(n: int, clients: int, ticks: int) -> dict:
    gw = _Gw()
    roster = gw.registry.add_fixed(1, "fleet/roster")
    swarm = gw.registry.add_fixed(2, "swarm/uav/state")
    env = gw.registry.add_fixed(3, "env/state")
    ids = [f"uav{i:04d}" for i in range(n)]
    per = {v: gw.registry.get_or_create(f"uav/{v}/state", entity={"kind": "uav", "id": v}, announce=False) for v in ids}
    sess = []
    for c in range(clients):
        s = ClientSession(gw, _Ws(), f"c-{c}", _P())
        s.hello = True
        subs = [{"id": 1, "topic": "fleet/roster", "rate": 10}, {"id": 2, "topic": "swarm/uav/state", "rate": 10},
                {"id": 3, "topic": "env/state", "rate": 10}]
        subs += [{"id": 10 + j, "topic": f"uav/{ids[(c * 32 + j) % n]}/state", "rate": 30} for j in range(32)]
        subs += [{"id": 99, "topic": f"uav/{ids[(c * 32 + 40) % n]}/state", "rate": 60}]
        s.subscribe([x | {"mode": "latest"} for x in subs])
        sess.append(s)
    lite = np.zeros(n, SWARM_LITE32).tobytes()
    full = np.zeros(n, DRONE_STATE64).tobytes()
    roster.publish(b"\x80" * 64, 0)
    env.publish(b"\x80" * 900, 0)
    ts = []
    mv = memoryview(full)
    for k in range(1, ticks + 1):
        gw.frame_t_sim_ns = k * 16_666_667
        t0 = time.perf_counter()
        swarm.publish(lite, gw.frame_t_sim_ns)
        for i, ch in enumerate(per.values()):
            if ch.subscribers:
                ch.publish(mv[i * 64:(i + 1) * 64], gw.frame_t_sim_ns)
        for s in sess:
            s.mark_due(k)
            if s.can_assemble():
                s.assemble()
                s.on_ack(s.frame_seq)
        ts.append((time.perf_counter() - t0) * 1e3)
    return {"assemble_ms": C.stats3(ts), "encodes": sum(c.encodes for c in gw.registry.by_id.values())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--clients", type=int, default=3)
    ap.add_argument("--ticks", type=int, default=3000)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-load-wait", action="store_true")
    a = ap.parse_args(argv)
    try:
        lock = C.acquire_perf_lock(a.no_lock)
    except TimeoutError as e:
        sys.stderr.write(f"错误（退出码 14）：{e}\n")
        return C.EXIT_ENV
    if not C.wait_load(a.no_load_wait):
        sys.stderr.write("错误（退出码 14）：开跑前 loadavg 未降到 4 以下（PERF-E002）\n")
        return C.EXIT_ENV
    sampler = C.LoadSampler()
    runs = [{"micro": micro(a.n, 2000), **assemble_run(a.n, a.clients, a.ticks)} for _ in range(a.runs)]
    load = sampler.stop()
    p99 = C.median_of([{"v": r["assemble_ms"]["p99"]} for r in runs], "v")
    gates, ok = C.judge([("assemble_ms_p99", p99, "<=", 0.3)], load["mean"] < 6)
    record = {"n": a.n, "rate": 1.0, "clients": a.clients, "with_flight60": False, "dur_s": a.ticks / 60.0, "rtf": 1.0,
              "cpu_core": 0.0, "step_us": {"p50": 0.0, "p99": 0.0, "max": 0.0}, "catchup_saturated": 0,
              "stage_ms_per_s": {}, "rss_mb": C.rss_mb(), "kernel": "numpy", "load": load,
              "ipc": {"assemble_p99_ms": p99}}
    d = C.write_outputs("bench_encode", a.out, record, {"params": {k: str(v) for k, v in vars(a).items()}, "runs": runs,
                                                        "gates": gates, "ok": ok, "load": load})
    print(json.dumps({"tool": "bench_encode", "ok": ok, "out": str(d), "assemble_ms_p99": p99, "gates": gates},
                     ensure_ascii=False))
    del lock
    return C.EXIT_OK if ok else C.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
