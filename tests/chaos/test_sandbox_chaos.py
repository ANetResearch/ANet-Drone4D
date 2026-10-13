"""沙盒混沌（WP-13；D2-AC-03 隔离与故障恢复、M11-AC-206、M17-AC-003/004 的进程级部分；AWR-04 §4.9）。

后端：`sbstack.SandboxStack`（真实 supervisor `sandbox` profile：共享展示 sim-core、show-api、sandbox-pool、sandbox-api；池以真实
sim-core 拉起会话，N_max = 2）。两个会话 A、B（模板 T2，各 1 架 P600）。只经 SDK 与 REST 观测。

1. 隔离：A 的 token 读写 B 与共享 run 一律拒绝（302 或 503 类原因码，HTTP 401/403）；
2. kill -9 A 的 sim-core：A ≤ 6 s 恢复（同一 sid 与 token，epoch + 1），B 的仿真时间持续推进、epoch 不变，共享展示不受影响；
3. kill -9 sandbox-pool：两个会话的 sim-core 继续运行（pid 不变、仿真时间推进），supervisor 重启池后 ≤ 10 s 重新接管（health 中
   sessions 不变），会话与 token 仍有效；
4. kill -9 sandbox-api：supervisor 重启后客户端 ≤ 5 s 回到原 sid，token 仍有效；
5. kill -9 show-api：沙盒会话不受影响（sandbox-api 健康、仿真时间推进）。

全部用例标 `chaos` 与 `perf`（带时间阈值，只经 `make chaos` 在排他性能锁下执行；G1 跳过）。
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
SIM_RECOVER_S = 6.0
POOL_ADOPT_S = 10.0
API_BACK_S = 5.0


@pytest.fixture(scope="module")
def env():
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    st = SandboxStack(max_sessions=2).start()
    a = A.Sandbox(st.base, principal_hint="MSIXCHAOSAHINTAAAAAAAAAA", auto_keepalive=False, timeout_s=30.0).create("synthcity", "t2_polyline")
    b = A.Sandbox(st.base, principal_hint="MSIXCHAOSBHINTAAAAAAAAAA", auto_keepalive=False, timeout_s=30.0).create("synthcity", "t2_polyline")
    for s in (a, b):
        s.wait_ready(90)
        s.clock("speed", rate=1)
    try:
        yield st, a, b
    finally:
        for s in (a, b):
            with contextlib.suppress(A.AwrError, httpx.HTTPError):
                s.close()
        st.stop()


def t_sim(s: A.Session) -> int:
    return int(s.vehicles.state("p600-01")["t_sim_ns"])


def advancing(s: A.Session, wait_s: float = 1.5) -> bool:
    t0 = t_sim(s)
    time.sleep(wait_s)
    return t_sim(s) > t0


def wait_until(pred, timeout_s: float, poll_s: float = 0.1) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        try:
            if pred():
                return time.monotonic() - t0
        except (A.AwrError, httpx.HTTPError, OSError):
            pass
        time.sleep(poll_s)
    raise TimeoutError(f"condition not met within {timeout_s} s")


def test_token_isolation(env) -> None:
    st, a, b = env
    for method, path in (("GET", b.path()), ("GET", b.path("vehicles")), ("POST", b.path("keepalive"))):
        r = httpx.request(method, st.base + path, headers={"Authorization": f"Bearer {a.token}"}, json={}, timeout=10)
        assert r.status_code in (401, 403), (method, path, r.status_code, r.text[:200])
        assert r.json().get("code") in (302, 503), r.text[:200]
    r = httpx.post(st.show_base + "/api/sim/speed", headers={"Authorization": f"Bearer {a.token}"}, json={"rate": 2}, timeout=10)
    assert r.status_code in (401, 403, 404), r.text[:200]


def test_kill_session_simcore(env) -> None:
    st, a, b = env
    pid = st.session_pid(a.sid)
    assert pid is not None
    epoch_a, epoch_b = a.state()["epoch"], b.state()["epoch"]
    os.kill(pid, signal.SIGKILL)
    dt = wait_until(lambda: a.state()["state"] in ("ACTIVE", "READY", "IDLE") and a.info["epoch"] == epoch_a + 1
                    and st.session_pid(a.sid) not in (None, pid), 20.0)
    assert dt <= SIM_RECOVER_S, dt
    assert advancing(b) and b.state()["epoch"] == epoch_b
    assert a.vehicles.list()["items"], "A lost its configuration after the restart"


def test_kill_pool_sessions_keep_running(env) -> None:
    st, a, b = env
    pids = {s.sid: st.session_pid(s.sid) for s in (a, b)}
    pool = st.pid_of("sandbox-pool")
    os.kill(pool, signal.SIGKILL)
    assert advancing(a) and advancing(b)                         # sandbox-api 直连会话上下文，池缺席不影响
    dt = wait_until(lambda: st.pid_of("sandbox-pool") != pool and st.health().get("pool") == "up"
                    and st.health().get("sessions") == 2, 30.0, poll_s=0.25)
    assert dt <= POOL_ADOPT_S, dt
    assert {s.sid: st.session_pid(s.sid) for s in (a, b)} == pids   # 接管而非重启
    for s in (a, b):
        assert s.state()["state"] in ("ACTIVE", "IDLE") and s.keepalive().get("session")


def test_kill_sandbox_api_client_returns(env) -> None:
    st, a, _b = env
    api = st.pid_of("sandbox-api")
    os.kill(api, signal.SIGKILL)
    dt = wait_until(lambda: st.pid_of("sandbox-api") != api and a.state()["sid"] == a.sid, 30.0, poll_s=0.25)
    assert dt <= API_BACK_S, dt
    assert advancing(a)


def test_kill_show_api_does_not_touch_sandbox(env) -> None:
    st, a, b = env
    show = st.pid_of("api")
    os.kill(show, signal.SIGKILL)
    assert st.health().get("pool") == "up"
    assert advancing(a) and advancing(b)
    wait_until(lambda: st.pid_of("api") != show, 30.0, poll_s=0.5)
