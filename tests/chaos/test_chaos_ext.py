"""混沌 ext（D1-AC-11b；PERF-AC-046；M16 §6.12 第 3–6 行；P1，失败走豁免流程）。

- 主循环挂死：`sys/inject{fault: hang}` 等价于对 sim-core 发 SIGSTOP（supervisor 的注入钩子即如此实现，M11-R）；
  ≤ 2.5 s 检出（supervisor 标记不健康并重启），≤ 4 s 恢复出帧（StateRing 由新进程继续写入）。
- 熔断：60 s 内连续 6 次 kill → sim-core 进入 FAILED；`POST /api/sys/restart {reset_breaker: true}` 可恢复。
- checkpoint 恢复与毒性 checkpoint：需要 ext 进程集与 checkpoint（M11 D1-ext），未交付时 skip。
"""

from __future__ import annotations

import contextlib
import os
import signal
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
    ring_path = Path("/dev/shm/awr") / backend.run_id / "state.sim-core"
    pid0 = backend.pid_of("sim-core")
    os.kill(pid0, signal.SIGSTOP)
    t0 = time.monotonic()
    detected = recovered = None
    try:
        while time.monotonic() - t0 < 12.0:
            try:
                cur = _state(backend, "sim-core")
            except Exception:
                time.sleep(0.1)
                continue
            if detected is None and (cur.get("state") != "RUNNING" or int(cur.get("pid") or 0) != pid0):
                detected = time.monotonic() - t0
            if int(cur.get("pid") or 0) not in (0, pid0) and cur.get("state") == "RUNNING":
                ring = StateRing.attach(ring_path, expect_layout_id=LAYOUT_ID)
                s = ring.header().step_seq
                time.sleep(0.3)
                if ring.header().step_seq > s:
                    recovered = time.monotonic() - t0
                ring.close()
                if recovered is not None:
                    break
            time.sleep(0.1)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid0, signal.SIGCONT)
    assert detected is not None and detected <= 2.5, detected
    assert recovered is not None and recovered <= 4.0, recovered


def test_restart_breaker(backend: Backend) -> None:
    import httpx

    for _ in range(6):
        t = time.monotonic()
        while time.monotonic() - t < 10.0:
            try:
                cur = _state(backend, "sim-core")
            except Exception:
                time.sleep(0.1)
                continue
            if cur.get("state") == "RUNNING" and cur.get("pid"):
                os.kill(int(cur["pid"]), signal.SIGKILL)
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
    r = httpx.post(backend.base + "/api/sys/restart", json={"name": "sim-core", "reset_breaker": True}, timeout=10,
                   headers={"Authorization": "Bearer " + backend.token("admin", "m16chaosadmin")})
    assert r.status_code == 202, r.text


def test_checkpoint_recovery() -> None:
    pytest.skip("checkpoint（ADR-019，D1-ext）尚未在 runtime.yaml 与 sim-core 中交付；交付后按 M16 §6.12 第 3、5 行补齐")
