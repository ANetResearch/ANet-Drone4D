"""S3 纽约港搜救 thermal 复核的锁步装配（M16 §6.4.5 定稿几何；M14 §6.11.2、§7.4）。

机体：a1 搜索（rgb.zoom，扩展方形基准 (−64, −1283)、z 60、首腿 55 m 向东、逆时针、5 m/s）；b1、b2 候选（thermal.imaging，
MISSION 租约沿 follow_path 飞往待命点 (86, −1133, 80)、(−314, −1033, 80)，3 m/s）；b3（thermal.imaging，SOC 0.26 留在地面，
覆盖 119）；c1 中继（150 m，不是 agents 成员）。目标 t1 (−24, −1253) conf_first 0.42、conf_confirm 0.9；t2、t3 为干扰目标。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from awr.agent.runtime.clock import SimScheduler
from awr.agent.runtime.core import RuntimeCore
from awr.agent.runtime.scenario import AgentsBlock
from awr.runtime.principal import derive_key

from .fake_sim import FakeSim

WORLD = "newyork"
RUN = "s3-lockstep"
SECRET = b"s3-test-secret-0123456789abcdef!"
T1 = (-24.0, -1253.0, 0.0)

ACCEPT = {"op": 1, "children": [
    {"op": 12, "thresh": {"metric": "confidence", "op": 4, "value": 0.8}},
    {"op": 10, "artifact": {"path_glob": "thermal/**", "min_size_bytes": 1024}},
    {"op": 11, "test": {"test_id": "station_reached", "expect": 1}}]}

S3_AGENTS: dict[str, Any] = {
    "network": "mock",
    "members": [
        {"vehicle_id": "p600-a1", "capabilities": ["rgb.zoom"], "role": "searcher"},
        {"vehicle_id": "p600-b1", "capabilities": ["thermal.imaging"], "role": "verifier"},
        {"vehicle_id": "p600-b2", "capabilities": ["thermal.imaging"], "role": "verifier"},
        {"vehicle_id": "p600-b3", "capabilities": ["thermal.imaging"], "role": "verifier"}],
    "tasks": [{
        "template_id": "thermal-verify",
        "trigger": {"on": "detection", "from_roles": ["searcher"], "capability": "rgb.*", "state": "suspect", "conf_lt": 0.8,
                    "merge_radius_m": 30},
        "capability": "thermal.imaging",
        "args": {"dwell_s": 10, "alt_agl_m": 60, "orbit_radius_m": 20},
        "accept": ACCEPT,
        "strategy": "auction", "max_retries": 2}],
}


def expanding_square(base: tuple[float, float], z: float, leg0: float, legs: int) -> list[tuple[float, float, float]]:
    """首腿向东、逆时针，腿长 55、55、110、110、…（M10 §6.5.10）。"""
    pts = [(base[0], base[1], z)]
    x, y = base
    dirs = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    for i in range(legs):
        d = leg0 * (i // 2 + 1)
        dx, dy = dirs[i % 4]
        x, y = x + dx * d, y + dy * d
        pts.append((x, y, z))
    return pts


def build_s3(*, seed: int = 7, world_seed: int = 11, strict: bool = True, agents: dict[str, Any] | None = None,
             sim_kw: dict[str, Any] | None = None) -> tuple[SimScheduler, FakeSim, RuntimeCore]:
    sched = SimScheduler(0, epoch=1, segment=0)
    k_entry = derive_key(SECRET, RUN, "entry")
    fake = FakeSim(sched, k_entry=k_entry, seed=seed, **(sim_kw or {}))
    fake.add_vehicle("p600-a1", 1, (-58.0, -1515.0, 0.0), soc=1.00, sensors=("camera",))
    fake.add_vehicle("p600-b1", 2, (-52.0, -1515.0, 0.0), soc=0.80, sensors=("camera", "thermal"))
    fake.add_vehicle("p600-b2", 3, (-46.0, -1515.0, 0.0), soc=0.95, sensors=("camera", "thermal"))
    fake.add_vehicle("p600-b3", 4, (-40.0, -1515.0, 0.0), soc=0.26, sensors=("camera", "thermal"))
    fake.add_vehicle("p600-c1", 5, (-34.0, -1515.0, 0.0), soc=1.00, sensors=())
    fake.add_target("t1", T1)
    fake.add_target("t2", (160.0, -1060.0, 0.0))
    fake.add_target("t3", (-290.0, -1500.0, 0.0))
    fake.mission("p600-a1", expanding_square((-64.0, -1283.0), 60.0, 55.0, 12), speed=5.0)
    fake.mission("p600-b1", [(-52.0, -1515.0, 80.0), (86.0, -1133.0, 80.0)], speed=3.0)
    fake.mission("p600-b2", [(-46.0, -1515.0, 80.0), (-314.0, -1033.0, 80.0)], speed=3.0)
    fake.mission("p600-c1", [(-34.0, -1515.0, 150.0), (-64.0, -1283.0, 150.0)], speed=3.0)
    block = AgentsBlock.from_doc(agents or S3_AGENTS)
    core = RuntimeCore(world_id=WORLD, run_id=RUN, secret=SECRET, sched=sched, bridge=fake, block=block, world_seed=world_seed,
                       strict=strict)
    return sched, fake, core


async def start(core: RuntimeCore, fake: FakeSim) -> None:
    await core.register_coordinator()
    await core.register_from_roster(fake.roster())
    core.triggers.arm_time_triggers()


async def run(sched: SimScheduler, fake: FakeSim, *, t_end_s: float, chunk_ns: int = 20_000_000,
              until: Callable[[], bool] | None = None, hook: Callable[[int], Any] | None = None) -> None:
    """锁步推进：每个 chunk 先让替身推进仿真，再推进调度器（到期回调与被唤醒的协程在同一仿真时刻继续）。"""
    t = sched.now_ns()
    t_end = int(t_end_s * 1e9)
    while t < t_end:
        t = min(t + chunk_ns, t_end)
        fake.step_to(t)
        await sched.advance_to(t)
        if hook is not None:
            r = hook(t)
            if asyncio.iscoroutine(r):
                await r
        if until is not None and until():
            break


def run_s3(*, t_end_s: float = 300.0, chunk_ns: int = 20_000_000, stop_on_done: bool = True, **kw: Any) -> tuple[FakeSim, RuntimeCore]:
    sched, fake, core = build_s3(**kw)

    async def main() -> None:
        await start(core, fake)
        done = (lambda: core.tm.target_confidence("t1") is not None and (core.tm.target_confidence("t1") or 0) >= 0.9
                and all(t.terminal for t in core.tm.tasks.values())) if stop_on_done else None
        await run(sched, fake, t_end_s=t_end_s, chunk_ns=chunk_ns, until=done)
        # 收尾：让交还租约与最后的投递落定
        await run(sched, fake, t_end_s=sched.now_s() + 3.0, chunk_ns=chunk_ns)

    asyncio.run(main())
    return fake, core
