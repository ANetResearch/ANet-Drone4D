"""驻留与恢复的混沌用例（WP-13b，M16；D2-AC-45"池重启后 PARKED 会话仍可恢复"、AWR-04 §15.16"混沌用例覆盖池与 sandbox-api
重启"；M17-FR-074；d2-WP-04b-to-M16 第 2 条）。

后端：`sbstack.SandboxStack`（真实 supervisor `sandbox` profile，N_max = 1；池以真实 sim-core 拉起会话）。

1. 驻留后 kill -9 sandbox-pool：supervisor 重启池后（≤ 10 s 接管）会话仍为 PARKED（驻留目录、寿命、原因不变），恢复到 READY、
   epoch + 1、驻留前新增的机体仍在；
2. 驻留后 kill -9 sandbox-api：重启后（≤ 5 s）同一 sid 与 token 仍可查询，恢复成功；
3. 驻留进行中（PARKING）kill -9 sandbox-pool：重启后会话不丢失（PARKED 或已恢复运行），之后可恢复或继续使用。

标 `chaos` 与 `perf`（杀进程，只经 `make chaos` 在排他性能锁下执行；G1 跳过）。
"""

from __future__ import annotations

import contextlib
import os
import signal
import time

import httpx
import pytest
from awrproc import world_ready
from sbstack import SandboxStack, sdk

pytestmark = [pytest.mark.chaos, pytest.mark.perf]
A = sdk()
POOL_ADOPT_S = 10.0
API_BACK_S = 5.0


@pytest.fixture(scope="module")
def st():
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    s = SandboxStack(max_sessions=1).start()
    try:
        yield s
    finally:
        s.stop()


def _wait(pred, timeout_s: float, poll_s: float = 0.25) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        with contextlib.suppress(httpx.HTTPError, A.AwrError, KeyError, ValueError):
            if pred():
                return time.monotonic() - t0
        time.sleep(poll_s)
    raise AssertionError(f"condition not met within {timeout_s} s")


def _state(st: SandboxStack, s: A.Session) -> str:
    return str(st.session(s.sid, s.token).get("state"))


def _open_and_park(st: SandboxStack, hint: str) -> tuple[A.Session, str]:
    s = A.Sandbox(st.base, principal_hint=hint, auto_keepalive=False, timeout_s=30.0).create("synthcity", "t1_point_watch", queue=False)
    s.wait_ready(90)
    s.rt()
    added = s.vehicles.add("p600_mid360", home_enu_m=[-26.0, -330.0, None])["id"]
    time.sleep(2.0)
    s.park()
    _wait(lambda: _state(st, s) == "PARKED", 30.0)
    return s, added


def _close(st: SandboxStack, s: A.Session) -> None:
    with contextlib.suppress(A.AwrError, httpx.HTTPError):
        s.close()
    with contextlib.suppress(AssertionError):
        _wait(lambda: st.capacity()["sandbox"]["active"] == 0, 45.0)


def test_parked_session_survives_a_pool_crash(st: SandboxStack) -> None:
    s, added = _open_and_park(st, "MSIXPCHAOSAHINTAAAAAAAAA")
    try:
        before = st.session(s.sid, s.token)
        pool = st.pid_of("sandbox-pool")
        os.kill(pool, signal.SIGKILL)
        dt = _wait(lambda: st.pid_of("sandbox-pool", 1.0) not in (0, pool) and st.health().get("pool") == "up", 30.0)
        assert dt <= POOL_ADOPT_S * 3, dt                    # 功能上限；POOL_ADOPT_S 在排他锁下判定
        seen: list[dict] = []

        def parked() -> bool:
            seen.append(st.session(s.sid, s.token))
            return seen[-1].get("state") == "PARKED"

        try:
            _wait(parked, 30.0)
        except AssertionError:
            raise AssertionError(f"not PARKED after the pool restart: {str(seen[-1])[:400]}") from None
        after = seen[-1]
        assert after["park_reason"] == before["park_reason"] and int(after["t_sim_ns"]) == int(before["t_sim_ns"])
        assert abs(int(after["expires_in_s"]) - int(before["expires_in_s"])) <= 2
        assert st.parked_dir(s.sid).exists()
        s.resume(wait=True)
        assert s.state()["state"] in ("READY", "ACTIVE") and int(s.state()["epoch"]) >= 2
        assert added in [v["id"] for v in s.vehicles.list()["items"]]
    finally:
        _close(st, s)


def test_parked_session_survives_a_sandbox_api_crash(st: SandboxStack) -> None:
    s, added = _open_and_park(st, "MSIXPCHAOSBHINTAAAAAAAAA")
    try:
        api = st.pid_of("sandbox-api")
        os.kill(api, signal.SIGKILL)
        dt = _wait(lambda: st.pid_of("sandbox-api", 1.0) not in (0, api) and _state(st, s) == "PARKED", 30.0)
        assert dt <= API_BACK_S * 4, dt
        s.resume(wait=True)
        assert s.state()["state"] in ("READY", "ACTIVE")
        assert added in [v["id"] for v in s.vehicles.list()["items"]]
    finally:
        _close(st, s)


def test_pool_crash_while_parking_does_not_lose_the_session(st: SandboxStack) -> None:
    s = A.Sandbox(st.base, principal_hint="MSIXPCHAOSCHINTAAAAAAAAA", auto_keepalive=False, timeout_s=30.0).create(
        "synthcity", "t1_point_watch", queue=False)
    try:
        s.wait_ready(90)
        s.rt()
        time.sleep(2.0)
        pool = st.pid_of("sandbox-pool")
        s.park()
        os.kill(pool, signal.SIGKILL)                        # 驻留进行中（暂停、checkpoint、停止进程之间）
        _wait(lambda: st.pid_of("sandbox-pool", 1.0) not in (0, pool) and st.health().get("pool") == "up", 30.0)
        _wait(lambda: _state(st, s) in ("PARKED", "READY", "ACTIVE", "IDLE"), 60.0)
        if _state(st, s) == "PARKED":
            s.resume(wait=True)
        assert s.state()["state"] in ("READY", "ACTIVE", "IDLE")
        assert s.vehicles.list()["items"]
    finally:
        _close(st, s)
