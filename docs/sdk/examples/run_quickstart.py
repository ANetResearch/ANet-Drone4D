#!/usr/bin/env python3
"""快速上手的运行脚本（平台侧，需要 ANet4D 仓库环境：`awr` 包、世界包与已编译事件；Realistic 档另需 gz-sim 与
本仓库的 Gazebo 原生插件，即 d3-gz 镜像）。docs/sdk/quickstart.zh-CN.md 第 4 节、quickstart.en.md section 4。

以 `anet4d.bench.make(..., physics=...)` 构造本地开发 Env（不排名），两种 Agent 接入方式：
- 进程内：`--agent plaza_watch:PlazaWatch`，每架机一个实例，经 `anet4d.runner.run_episode` 驱动；
- 套接字：`--sock-root DIR`，为每架机监听 `DIR/<uav>/agent.sock`，等待外部进程（研究者主机上 `pip install anet4d` 后的
  `python -m anet4d.serve plaza_watch:PlazaWatch --sock DIR/<uav>/agent.sock`，或 Agent 容器）接入，经线协议
  `drone4d.bench.wire.v1` 交换帧。两种方式下同一种子的动作流逐字节一致。

结束后在标准输出打印一行 JSON 摘要（`drone4d.quickstart.summary.v1`）：步数、墙钟、逐机超时与动作流 sha256、report 数。

用法：
    python docs/sdk/examples/run_quickstart.py --spec configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json \\
        --worlds-root worlds --events-root d3data/events --physics realistic --gz-launcher local \\
        --duration-s 60 --seed 1 --out runs/quickstart-1 --agent plaza_watch:PlazaWatch
    python docs/sdk/examples/run_quickstart.py ... --sock-root /tmp/d4d-sock          # 等待外部 Agent 接入
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path

SUMMARY = "drone4d.quickstart.summary.v1"


def load_factory(ref: str) -> Callable:
    """`pkg.module:Class` → 无参工厂（当前目录在导入路径上，便于直接引用同目录的 Agent 文件）。"""
    if "" not in sys.path:
        sys.path.insert(0, "")
    mod, _, attr = ref.partition(":")
    return getattr(importlib.import_module(mod), attr or "Agent")


def host_sockets(env, sock_root: Path, *, hello_timeout_s: float = 300.0, step_budget_s: float | None = None,
                 on_listen: Callable[[dict[str, Path]], None] | None = None) -> tuple[dict, dict]:
    """为每架机监听一个 Unix 套接字，等待外部 Agent 的 HELLO，然后运行一回合；→ (运行清单, 每机线协议统计)。

    `on_listen(paths)` 在全部套接字就绪后调用（测试与脚本用它启动 Agent 进程）。"""
    from awr.agentsdk.env.remote import SocketAgentHost, run_remote_episode

    hosts = {u: SocketAgentHost(u, Path(sock_root) / u / "agent.sock", hello_timeout_s=hello_timeout_s) for u in env.possible_agents}
    paths = {u: h.path for u, h in hosts.items()}
    if on_listen is not None:
        on_listen(paths)
    else:
        for u, p in paths.items():
            print(f"{u}: waiting for an agent on {p}", file=sys.stderr, flush=True)
    try:
        for h in hosts.values():
            h.accept()
    except BaseException:
        for h in hosts.values():
            h.close("shutdown")
        raise
    return run_remote_episode(env, hosts, step_budget_s=step_budget_s)


def summarize(manifest: dict, out_dir: Path, stats: dict | None = None) -> dict:
    """运行清单与运行目录 → 摘要；`acts_sha256` 为每机 `agents/<uav>/acts.jsonl` 的 sha256（动作流逐字节比对用）。"""
    out = Path(out_dir)
    acts, reports = {}, 0
    for u in sorted(manifest["agents"]):
        p = out / "agents" / u / "acts.jsonl"
        acts[u] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    rp = out / "reports.jsonl"
    if rp.exists():
        reports = sum(1 for line in rp.read_text(encoding="utf-8").splitlines() if line.strip())
    s = {"schema": SUMMARY, "run_id": manifest["run_id"], "world": manifest["world"], "event": manifest["event"]["id"],
         "physics": manifest["physics"], "tier": manifest["tier"], "obs_level": manifest["obs_level"], "n_ticks": manifest["n_ticks"],
         "duration_s": manifest["duration_s"], "wall_s": manifest["wall_s"],
         "timeouts": {u: a["timeouts"] for u, a in sorted(manifest["agents"].items())}, "reports": reports, "acts_sha256": acts}
    if stats is not None:
        s["wire"] = {u: {"protocol_errors": st.get("protocol_errors", 0), "exited": st.get("exited")} for u, st in sorted(stats.items())}
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run_quickstart.py", description="Run one local development episode (not ranked).")
    ap.add_argument("--spec", required=True, type=Path, help="bench/run_spec.v1 JSON file")
    ap.add_argument("--worlds-root", default="worlds", type=Path)
    ap.add_argument("--events-root", default="d3data/events", type=Path)
    ap.add_argument("--physics", default="realistic", help="realistic (gz-sim lockstep) | fast (P0b) | kinematic (development only)")
    ap.add_argument("--obs-level", help="override the run specification: L1 | L2 | L2-depth")
    ap.add_argument("--gz-launcher", default="docker", help="realistic only: docker (sibling d3-gz container) | local (inside d3-gz)")
    ap.add_argument("--gz-export-root", type=Path, help="realistic only: writable directory for the gz world export (default: inside the world)")
    ap.add_argument("--seed", default=1, type=int)
    ap.add_argument("--duration-s", type=float, help="override the run specification (the quickstart uses 60 s)")
    ap.add_argument("--out", required=True, type=Path, help="run directory")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--agent", help="in-process agent factory pkg.module:Class")
    g.add_argument("--sock-root", type=Path, help="listen on DIR/<uav>/agent.sock for external agents")
    ap.add_argument("--hello-timeout-s", default=300.0, type=float)
    a = ap.parse_args(argv)

    from anet4d.local import make  # 即 anet4d.bench.make；平台镜像未装 bench-api 客户端依赖 httpx 时也可导入
    from anet4d.runner import run_episode

    rs = json.loads(a.spec.read_text(encoding="utf-8"))
    rs["ranked"] = False                                   # 本地开发运行不排名
    if a.duration_s is not None:
        rs["duration_s"] = a.duration_s
    if a.obs_level:
        rs["obs_level"] = a.obs_level
    gz = None
    if a.physics == "realistic":
        gz = {"launcher": a.gz_launcher, **({"export_root": str(a.gz_export_root)} if a.gz_export_root else {})}
    env = make(rs, run_id=f"quickstart-{a.seed}", seed=a.seed, worlds_root=a.worlds_root, events_root=a.events_root, out_dir=a.out,
               physics=a.physics, gz=gz)
    if a.agent:
        factory = load_factory(a.agent)
        manifest, stats = run_episode(env, lambda _uav: factory()), None
    else:
        manifest, stats = host_sockets(env, a.sock_root, hello_timeout_s=a.hello_timeout_s)
    print(json.dumps(summarize(manifest, a.out, stats), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
