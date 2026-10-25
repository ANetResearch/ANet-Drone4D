"""Shared D3 episode helpers; never import a collected test module by basename."""

from __future__ import annotations

from pathlib import Path

from awr.agentsdk.env.core import BenchEnv
from awr.agentsdk.env.spec import EnvSpec, FleetEntry

FLEET = (FleetEntry("uav-01", (-10.0, 50.0, 0.0)), FleetEntry("uav-02", (10.0, 50.0, 0.0)))


def _spec(synth_event, out: Path | None, seed: int = 5, dur: float = 30.0, **kw) -> EnvSpec:
    w, ev = synth_event
    return EnvSpec(f"t-{seed}", seed, w, ev, kw.pop("fleet", FLEET), physics="kinematic", duration_s=dur, out_dir=out, **kw)


def _run(spec: EnvSpec, make_agent, check=None) -> dict:
    env = BenchEnv(spec)
    obs, infos = env.reset()
    ags = {u: make_agent(u) for u in env.possible_agents}
    for u, a in ags.items():
        a.reset(infos[u]["config"])
        if check:
            check("config", u, infos[u]["config"])
    while env.agents:
        acts = {}
        for u, o in obs.items():
            if check:
                check("obs", u, o)
            acts[u] = ags[u].step(o["obs"], o["inbox"])
        obs, *_ = env.step(acts)
    m = env.close()
    m["_env"] = env
    return m
