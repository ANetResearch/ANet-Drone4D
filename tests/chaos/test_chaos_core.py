"""chaos-core（D1-AC-11a；PERF-AC-044；M16-FR-057；M16 §6.12 前两行；AWR-18 §8.8）。

后端：ci profile 的 supervisor（sim-core + api，空闲端口），剧本 `free-shenzhen`（`gcs_loss_policy = ignore`，api 缺席期间
机体不进入 LINK_LOSS）。探针 `rtprobe` 保持连接并有 1 个在途调用（cid 固定）。

1. kill -9 api：StateRing `step_seq` 在 api 缺席期间继续递增（仿真不中断）；客户端 ≤ 3 s 重连；epoch 不变（插值环不清空）；
   以同一 cid 重发得到 `duplicate: true` 且结果为终态。
2. kill -9 sim-core（无 checkpoint）：supervisor ≤ 3 s 重启（`restarts` + 1、RUNNING）；剧本从起点重开；客户端先收到
   新 epoch 的 TIME，再收到该 epoch 的 SNAPSHOT。
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

import pytest
from awrproc import Backend, world_ready
from rtprobe import Probe

from awr.contracts import LAYOUT_ID
from awr.runtime.statering import StateRing

pytestmark = [pytest.mark.chaos, pytest.mark.perf]
RECONNECT_S = 3.0
RESTART_S = 3.0


@pytest.fixture
def backend():
    if not world_ready("shenzhen"):
        pytest.skip("worlds/shenzhen not built（make worlds）")
    b = Backend(world="shenzhen", scenario="free-shenzhen", profile="ci")
    b.start()
    yield b
    b.stop()


def _ring(b: Backend) -> StateRing:
    return StateRing.attach(Path("/dev/shm/awr") / b.run_id / "state.sim-core", expect_layout_id=LAYOUT_ID)


async def _reconnect(b: Backend, tok: str, resume: dict, deadline_s: float) -> Probe:
    delay, end = 0.2, time.monotonic() + deadline_s + 5.0
    last: Exception | None = None
    while time.monotonic() < end:
        try:
            return await Probe.open(b.base, tok, resume=resume, timeout=3.0)
        except Exception as e:
            last = e
            await asyncio.sleep(delay)
            delay = min(1.0, delay * 1.5)
    raise TimeoutError(f"reconnect failed: {last}")


def test_kill_api(backend: Backend) -> None:
    tok = backend.token("operator", "m16chaosapi")
    ring = _ring(backend)

    async def run() -> None:
        p = await Probe.open(backend.base, tok)
        info0 = p.server_info
        epoch0 = p.times[-1].epoch
        vid = next(iter(p.roster_ids()), "p600-01")
        cid = "m16-chaos-takeoff-1"
        await p.call(cid, f"uav/{vid}/cmd/takeoff", {"alt_m": 15})
        first = await p.result(cid, final=False, timeout=10)
        assert first.get("status") in ("accepted", "running", "succeeded"), first
        api_pid = backend.pid_of("api")
        s0 = ring.header().step_seq
        os.kill(api_pid, signal.SIGKILL)
        t_kill = time.monotonic()
        await asyncio.sleep(0.5)
        assert ring.header().step_seq > s0, "simulation stalled while api was down"
        p2 = await _reconnect(backend, tok, {"sessionId": info0.get("sessionId", ""), "lastEventSeq": 0}, RECONNECT_S)
        dt = time.monotonic() - t_kill
        assert dt <= RECONNECT_S, f"reconnect took {dt:.2f} s"
        assert p2.times[-1].epoch == epoch0, "epoch changed on api restart"
        assert p2.server_info.get("seat") == "held"
        await p2.call(cid, f"uav/{vid}/cmd/takeoff", {"alt_m": 15})
        r = await p2.result(cid, final=False, timeout=10)
        assert r.get("duplicate") is True, r
        fin = await p2.result(cid, final=True, timeout=30)
        assert fin.get("status") in ("succeeded", "failed", "rejected", "canceled", "timeout"), fin
        await p2.close()

    try:
        asyncio.run(run())
    finally:
        ring.close()


def test_kill_sim_core(backend: Backend) -> None:
    tok = backend.token("viewer", "m16chaossim")

    async def run() -> None:
        p = await Probe.open(backend.base, tok)
        await p.subscribe([{"id": 1, "topic": "swarm/uav/state", "rate": 10, "mode": "latest"}])
        await p.drain(1.0)
        epoch0 = p.times[-1].epoch
        procs0 = {x["name"]: x for x in backend.procs()}
        restarts0 = int(procs0["sim-core"].get("restarts", 0))
        os.kill(int(procs0["sim-core"]["pid"]), signal.SIGKILL)
        t_kill = time.monotonic()
        restarted = None
        while time.monotonic() - t_kill < RESTART_S + 5.0:
            await p.drain(0.2)
            try:
                cur = {x["name"]: x for x in backend.procs()}["sim-core"]
            except Exception:
                continue
            if int(cur.get("restarts", 0)) > restarts0 and cur.get("state") == "RUNNING":
                restarted = time.monotonic() - t_kill
                break
        assert restarted is not None and restarted <= RESTART_S, f"sim-core restart took {restarted}"
        await p.until(lambda k, x: k == "time" and x.epoch != epoch0, 10)
        await p.drain(2.0)
        new = [o for o in p.order if o[1] != epoch0]
        assert new and new[0][0] == "time", new[:5]                                        # TIME 先于 SNAPSHOT
        assert any(k == "snapshot" for k, _ in new), new[:10]
        ev, _ = backend.events(0)
        assert any(e.get("type") in ("scenario.loaded", "sim.started") and e.get("t_sim_ns", 1) < 5e9 for e in ev
                   if e.get("seq", 0) > 1), "scenario did not restart from its beginning"
        await p.close()

    asyncio.run(run())
