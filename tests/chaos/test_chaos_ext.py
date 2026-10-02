"""混沌 ext（D1-AC-11b；PERF-AC-046；M16 §6.12 第 3–6 行；P1，失败走豁免流程）。

- 主循环挂死：`sys/inject{fault: hang}` 等价于对 sim-core 发 SIGSTOP（supervisor 的注入钩子即如此实现，M11-R）；
  ≤ 2.5 s 检出（supervisor 判定挂死即转 STOPPING，转储栈后 SIGKILL 并重启，ADR-065），≤ 4 s 恢复出帧（StateRing 由新进程
  继续写入）。
- 熔断：60 s 内连续 6 次 kill → sim-core 进入 FAILED；`POST /api/sys/restart {reset_breaker: true}` 可恢复。
- checkpoint 恢复与毒性 checkpoint（FX-SIM2 补齐）：sim-core 在监管下默认挂接 checkpoint（每 1 s【仿真】一代，扩展段带 M09、M07
  内部状态与外部度量，ADR-019）；kill -9 后回滚 ≤ 1 s、新进程 RUNNING 后新 epoch 首帧 ≤ 1.5 s、在途调用到达终态；
  恢复后 5 s 内再崩改用上一代。
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from awrproc import Backend, world_ready

from awr.contracts import LAYOUT_ID
from awr.runtime.statering import StateRing

pytestmark = [pytest.mark.chaos, pytest.mark.perf, pytest.mark.ext]


@pytest.fixture
def backend():
    if not world_ready("shenzhen"):
        pytest.skip("worlds/shenzhen not built（make worlds）")
    b = Backend(world="shenzhen", scenario="free-shenzhen", profile="ci")
    b.start()
    yield b
    b.stop()


def _state(b: Backend, name: str) -> dict:
    return {x["name"]: x for x in b.procs()}[name]


def test_main_loop_hang(backend: Backend) -> None:
    """检出：supervisor 判定挂死即转 STOPPING（ADR-065），以 `/api/sys/procs` 的状态或 pid 变化计；恢复：StateRing 中出现新进程
    写入的帧（最新帧的 epoch 不同于挂死前的 epoch）的时刻，10 ms 采样。

    第 2 轮验收以"新 pid 出现后 attach，`step_seq` 超过 attach 时读到的值"判恢复：attach 若早于新进程从 checkpoint 恢复，读到的
    是旧进程的 `step_seq`，而新进程从 checkpoint 回滚（≤ 1 s 仿真）后要再走完这段才"超过"它，恢复时刻被多计至多约 1 s
    （FX2-R3-sim 自测：同一次挂死，新 epoch 首帧 4.5 s，原判据 5.3 s）。另外 ring 文件在重启时复用（epoch 加 1）；inode 变化时
    重新 attach。"""
    ring_path = Path("/dev/shm/awr") / backend.run_id / "state.sim-core"
    ring = StateRing.attach(ring_path, expect_layout_id=LAYOUT_ID)
    epoch0 = ring.header().epoch
    ino0 = ring.identity()[0]
    pid0 = backend.pid_of("sim-core")
    os.kill(pid0, signal.SIGSTOP)
    t0 = time.monotonic()
    detected = recovered = None
    next_poll = 0.0
    try:
        while time.monotonic() - t0 < 12.0:
            now = time.monotonic()
            if detected is None and now >= next_poll:
                next_poll = now + 0.05
                with contextlib.suppress(Exception):
                    cur = _state(backend, "sim-core")
                    if cur.get("state") != "RUNNING" or int(cur.get("pid") or 0) != pid0:
                        detected = time.monotonic() - t0
            with contextlib.suppress(Exception):
                ino = ring.identity()[0]
                if ino != ino0:
                    ring.close()
                    ring = StateRing.attach(ring_path, expect_layout_id=LAYOUT_ID)
                    ino0 = ino
            fr = ring.read_latest()
            if fr is not None and fr.epoch != epoch0:
                recovered = time.monotonic() - t0
                break
            time.sleep(0.01)
    finally:
        ring.close()
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid0, signal.SIGCONT)
    assert detected is not None and detected <= 2.5, detected
    assert recovered is not None and recovered <= 4.0, recovered


def test_restart_breaker(backend: Backend) -> None:
    import httpx

    # admin 令牌在熔断之前取得：签发 operator/admin 令牌要经 sim-core 申领席位，sim-core 处于 FAILED 时 `/api/auth/token`
    # 回 503（FX2-R2 自测发现，已转 gateway 区域）
    admin = backend.token("admin", "m16chaosadmin")
    killed: set[int] = set()
    for _ in range(6):
        t = time.monotonic()
        while time.monotonic() - t < 20.0:  # 第 5 次重启的退避为 8 s，再加启动约 2–3 s：10 s 不够等到 RUNNING
            try:
                cur = _state(backend, "sim-core")
            except Exception:
                time.sleep(0.1)
                continue
            pid = int(cur.get("pid") or 0)
            # 只杀新进程：kill 之后 supervisor 处理退出之前，列表仍报告旧 pid 为 RUNNING（D1 验收第 1 轮的竞态）
            if cur.get("state") == "RUNNING" and pid and pid not in killed:
                killed.add(pid)
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                break
            if cur.get("state") == "FAILED":
                break
            time.sleep(0.1)
    end = time.monotonic() + 20.0
    state = None
    while time.monotonic() < end:
        try:
            state = _state(backend, "sim-core").get("state")
        except Exception:
            state = None
        if state == "FAILED":
            break
        if backend.proc is not None and backend.proc.poll() == 20:     # ci profile：核心进程熔断时 supervisor 以 20 退出
            pytest.skip("ci profile exits with 20 on core breaker (19 §16.2); FAILED state not observable over REST")
        time.sleep(0.2)
    assert state == "FAILED"

    def ci_exit() -> bool:
        # FAILED 已在 REST 上观察到，随后 ci profile 的 supervisor 按核心进程熔断以 20 退出（19 §16.2），api 随之停止
        with contextlib.suppress(subprocess.TimeoutExpired):
            return backend.proc is not None and backend.proc.wait(timeout=10) == 20
        return False

    try:
        r = httpx.post(backend.base + "/api/sys/restart", json={"name": "sim-core", "reset_breaker": True}, timeout=10,
                       headers={"Authorization": "Bearer " + admin})
    except httpx.TransportError:
        if ci_exit():
            pytest.skip("ci profile exited with 20 after the core breaker (19 §16.2); reset_breaker not reachable")
        raise
    if r.status_code != 202 and ci_exit():
        pytest.skip("ci profile exited with 20 after the core breaker (19 §16.2); reset_breaker not reachable")
    assert r.status_code == 202, r.text


def _ring(b: Backend) -> StateRing:
    return StateRing.attach(Path("/dev/shm/awr") / b.run_id / "state.sim-core", expect_layout_id=LAYOUT_ID)


def _wait_restart(b: Backend, restarts0: int, timeout_s: float = 10.0) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        try:
            cur = _state(b, "sim-core")
        except Exception:
            time.sleep(0.05)
            continue
        if int(cur.get("restarts", 0)) > restarts0 and cur.get("state") == "RUNNING":
            return time.monotonic()
        time.sleep(0.05)
    return None


def _restarted(b: Backend) -> list[dict]:
    ev, _ = b.events(0)
    return [e for e in ev if e.get("type") == "sim.restarted"]


def test_checkpoint_recovery(backend: Backend) -> None:
    """M16 §6.12 第 3 行（D1-AC-11b）：kill -9 sim-core 后从 checkpoint 恢复（ADR-019；sim-core 在监管下默认挂接 checkpoint，
    每 1 s【仿真】一代）：回滚 ≤ 1 s（`sim.restarted.restored_t_sim_ns` 与 kill 前的仿真时刻之差）；新进程 RUNNING 后新 epoch
    的首个 TIME ≤ 1.5 s；恢复带回 M09、M07 扩展段；恢复前在途的调用到达终态。"""
    import asyncio

    from rtprobe import Probe

    tok = backend.token("operator", "m16chaosckpt")
    ring = _ring(backend)

    async def run() -> None:
        p = await Probe.open(backend.base, tok)
        await p.subscribe([{"id": 1, "topic": "swarm/uav/state", "rate": 10, "mode": "latest"}])
        vid = next(iter(p.roster_ids()), "p600-01")
        cid = "m16-chaos-ckpt-takeoff"
        await p.call(cid, f"uav/{vid}/cmd/takeoff", {"alt_m": 25})
        first = await p.result(cid, final=False, timeout=10)
        assert first.get("status") in ("accepted", "running"), first
        await p.drain(2.5)                                       # 至少两代 checkpoint
        epoch0 = p.times[-1].epoch
        restarts0 = int(_state(backend, "sim-core").get("restarts", 0))
        t_sim_kill = int(ring.header().t_sim_ns)
        os.kill(backend.pid_of("sim-core"), signal.SIGKILL)
        t_run = None
        while t_run is None:
            await p.drain(0.1)
            t_run = _wait_restart(backend, restarts0, 0.2)
        await p.until(lambda k, x: k == "time" and x.epoch != epoch0, 10)
        t_frame = time.monotonic()
        rs = _restarted(backend)
        assert rs, "no sim.restarted event (checkpoint not restored)"
        d = rs[-1].get("data") or {}
        rollback_s = (t_sim_kill - int(d.get("restored_t_sim_ns", 0))) / 1e9
        assert -0.05 <= rollback_s <= 1.0 + 0.05, rollback_s
        assert {"safety", "env"} <= set(d.get("ext") or []), d
        assert t_frame - t_run <= 1.5, t_frame - t_run
        fin = await p.result(cid, final=True, timeout=60)
        assert fin.get("status") in ("succeeded", "failed", "canceled", "timeout"), fin
        await p.close()

    try:
        asyncio.run(run())
    finally:
        ring.close()


def test_poison_checkpoint(backend: Backend) -> None:
    """M16 §6.12 第 5 行：恢复后 5 s 内再次 kill -9，该代标记为毒性，改用上一代（`restored_t_sim_ns` 不晚于前一次恢复点）。"""
    time.sleep(3.0)                                              # 攒几代 checkpoint
    restarts0 = int(_state(backend, "sim-core").get("restarts", 0))
    os.kill(backend.pid_of("sim-core"), signal.SIGKILL)
    assert _wait_restart(backend, restarts0) is not None
    first = None
    end = time.monotonic() + 10
    while first is None and time.monotonic() < end:
        rs = _restarted(backend)
        first = rs[-1] if rs else None
        time.sleep(0.2)
    assert first is not None
    os.kill(backend.pid_of("sim-core"), signal.SIGKILL)         # 恢复后 5 s 内再崩
    assert _wait_restart(backend, restarts0 + 1) is not None
    second = None
    end = time.monotonic() + 10
    while second is None and time.monotonic() < end:
        rs = _restarted(backend)
        second = rs[-1] if len(rs) >= 2 else None
        time.sleep(0.2)
    assert second is not None
    t1 = int((first.get("data") or {}).get("restored_t_sim_ns", 0))
    t2 = int((second.get("data") or {}).get("restored_t_sim_ns", 0))
    assert t2 <= t1, (t1, t2)
