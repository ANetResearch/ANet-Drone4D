"""supervisor 测试用的假子进程（M11-AC-005）：经 init_child 启动，按参数模拟正常心跳、崩溃、挂死、忽略 SIGTERM 等行为。

用法：python tests/runtime/fake_child.py --mode run|crash|hang|exit0|ignore-term|ring|spam [--after S] [--rc N]
       [--grandchild PIDFILE [--grandchild-ignore-term]]（派生一个同进程组的后代并把 pid 写入 PIDFILE，模拟 plan-pool 工作进程）
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from awr.runtime.child import init_child
from awr.runtime.heartbeat import Heartbeat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="run")
    ap.add_argument("--after", type=float, default=0.3)
    ap.add_argument("--rc", type=int, default=1)
    ap.add_argument("--hz", type=float, default=50.0)
    ap.add_argument("--lines", type=int, default=0)
    ap.add_argument("--name", default=os.environ.get("AWR_PROC", "fake"))
    ap.add_argument("--run", default=None)
    ap.add_argument("--segment", default=None)
    ap.add_argument("--bus", action="store_true", help="经 ctx 打开 ZenohBus 并声明 proc/<name>/ready")
    ap.add_argument("--grandchild", default=None, help="派生同进程组的后代（sleep），pid 写入该文件")
    ap.add_argument("--grandchild-ignore-term", action="store_true")
    a = ap.parse_args()
    if os.environ.get("AWR_STANDBY") == "1":  # supervisor 的热备用进程（ADR-070）：就绪后等接替指令
        import json

        print("AWR_STANDBY_READY", flush=True)
        line = sys.stdin.readline()
        if not line.strip():
            return 0
        os.environ.update({str(k): str(v) for k, v in (json.loads(line).get("env") or {}).items()})
        os.environ.pop("AWR_STANDBY", None)
        print(f"fake child promoted pid={os.getpid()}", flush=True)
    ctx = init_child(a.name)
    if a.grandchild:
        import subprocess

        code = ("import signal, time\n" + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if a.grandchild_ignore_term else "")
                + "time.sleep(120)\n")
        gc = subprocess.Popen([sys.executable, "-c", code])  # 不新建会话：与本进程同组，如同 multiprocessing spawn
        Path(a.grandchild).write_text(str(gc.pid))
    bus = None
    if a.bus:
        from awr.runtime.bus import ZenohBus

        bus = ZenohBus.open(a.name, ctx)
        bus.ready()
    if a.mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ring = None
    hb = None
    if a.mode == "ring":
        from awr.contracts import LAYOUT_ID
        from awr.runtime.statering import StateRing

        ring, _ = StateRing.open_or_create(ctx.run_dir / "state.sim-core", capacity=16, slots=16, layout_id=LAYOUT_ID)
    else:
        hb = Heartbeat(ctx.hb_path)
    for i in range(a.lines):
        sys.stderr.write(f'{{"lvl":"INFO","proc":"{a.name}","msg":"spam","kv":{{"i":{i},"pad":"{"x" * 200}"}}}}\n')
    sys.stderr.flush()
    print(f"fake child ready pid={os.getpid()} args={a.run},{a.segment}", flush=True)
    t0 = time.monotonic()
    k = 0
    while not ctx.stopping:
        el = time.monotonic() - t0
        if a.mode == "crash" and el > a.after:
            return a.rc
        if a.mode == "exit0" and el > a.after:
            return 0
        if a.mode == "hang" and el > a.after:
            while True:  # 主循环挂死：不再写心跳
                pass
        k += 1
        if ring is not None:
            ring.heartbeat(k * 4_000_000, 1, 1000, step_seq=k)
        elif hb is not None:
            hb.beat()
        time.sleep(1.0 / a.hz)
    Path(ctx.persist_dir / f"stopped.{a.name}").write_text(str(time.monotonic_ns()))
    if bus is not None:
        bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
