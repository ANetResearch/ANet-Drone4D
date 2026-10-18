"""本地运行（开发用，不排名）：以 PettingZoo Parallel 风格的 Env 驱动每架机一个 DroneAgent。"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from drone4d.agent import DroneAgent

__all__ = ["run_episode"]


def run_episode(env, agents: Mapping[str, DroneAgent] | Callable[[str], DroneAgent]) -> dict:
    """env 为 `drone4d.bench.make(...)` 或 `awr.agentsdk.env.core.BenchEnv`；→ env.close() 的运行清单。"""
    obs, infos = env.reset()
    pool = {u: (agents(u) if callable(agents) else agents[u]) for u in env.possible_agents}
    for u, ag in pool.items():
        ag.reset(infos[u]["config"])
    try:
        while env.agents:
            acts = {u: pool[u].step(o["obs"], o["inbox"]) for u, o in obs.items() if u in env.agents}
            obs, _r, _t, _tr, _i = env.step(acts)
    finally:
        manifest = env.close()
    return manifest
