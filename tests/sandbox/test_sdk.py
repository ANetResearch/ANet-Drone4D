"""Python SDK（M17-FR-056 至 FR-058、AC-013 的功能部分；17 §17.13.1）：只经 SDK 创建、续期、时钟、快照、事件、实时流（WS 解码与
自动 ack）、速度控制（call velocity + advertise + CLIENT_DATA，<= 50 Hz）、排队长轮询与 slot_ready 回调后认领、结束后
SandboxGone；公开配额档下自动求解工作量证明。另检查单文件 <= 1500 行、Python 3.9 语法与依赖面。

D2.1（M17-FR-080、AC-033）：`park()` 后会话请求得到 515 时自动恢复一次再重试（token 换发）、满员时 `resume(wait=True)` 按恢复票
等到恢复、`capacity()`。"""

from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path

import pytest
from sbharness import SbxStack, make_sb

ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "tools" / "sdk"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

import awr_sandbox as A  # noqa: E402


def test_single_file_and_py39_syntax() -> None:
    src = (SDK / "awr_sandbox.py").read_text(encoding="utf-8")
    assert len(src.splitlines()) <= 1500
    tree = ast.parse(src, feature_version=(3, 9))
    assert any(isinstance(n, ast.ImportFrom) and n.module == "__future__" for n in tree.body)
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "awr" not in mods and {"httpx", "msgpack", "websockets"} <= mods | {"websockets"}
    assert A.__version__ == "1.1.0" and A.API_VERSION == "v1"
    for ex in sorted((SDK / "examples").glob("*.py")):
        ast.parse(ex.read_text(encoding="utf-8"), feature_version=(3, 9))


@pytest.fixture(scope="module")
def st():
    s = SbxStack(sb=make_sb(max_sessions=1, spawn={"timeout_s": 4}),
                 sb_api={"rest": {"rps": 200, "burst": 400, "clock_rps": 100, "entity_rps": 100}})
    s.pt.launcher.n_vehicles = 1
    yield s
    s.close()


def test_sdk_session_end_to_end(st: SbxStack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWR_SANDBOX_PRINCIPAL_FILE", str(tmp_path / "principal"))
    sb = A.Sandbox(st.base, auto_keepalive=False)
    assert (tmp_path / "principal").stat().st_mode & 0o777 == 0o600 and len(sb.principal_hint) == 32
    assert sb.worlds()[0]["world_id"] == "synthcity" and sb.templates("synthcity")
    s = sb.create("synthcity", "t5_area_guard")
    assert s.sid.startswith("sb-") and s.state()["state"] in ("READY", "ACTIVE") and s.limits["vehicles_max"] == 12
    k = s.keepalive()
    assert k["renewed"] is True and s.token == k["token"]
    c = s.clock("speed", rate=2)
    assert c["granted_rate"] <= 2 and c["status"] == "succeeded" and c["rate_cap"] > 2
    assert s.snapshot()["world_id"] == "synthcity"
    msgs = list(s.stream(["sandbox/session"], seconds=3.0))
    assert msgs and msgs[-1].topic == "sandbox/session" and msgs[-1].data["sid"] == s.sid
    sim = st.pt.launcher.sim(s.sid)
    n0 = len(sim.setpoints)
    for _ in range(20):
        s.velocity("f001", 1.0, 0.0, 0.0)
        time.sleep(0.025)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(sim.setpoints) - n0 < 5:
        time.sleep(0.05)
    assert len(sim.setpoints) - n0 >= 5
    s.velocity_stop("f001")
    assert any(m.get("op") == "velocity_stop" for m in sim.commands)
    ev = s.events(0)
    assert "items" in ev and ev["next_since_seq"] >= 1
    s.close()
    with pytest.raises(A.SandboxGone) as ei:
        for _ in range(50):
            s.state()
            time.sleep(0.1)
    assert ei.value.code == 506
    sb.close()


def test_sdk_queue_slot_ready_and_claim(st: SbxStack) -> None:
    a = A.Sandbox(st.base, principal_hint="a" * 32, auto_keepalive=False)
    b = A.Sandbox(st.base, principal_hint="b" * 32, auto_keepalive=False)
    first = a.create("synthcity", None)
    seen: list[str] = []
    out: dict = {}

    def run() -> None:
        out["s"] = b.create("synthcity", None, queue_timeout_s=60, on_queue=lambda kind, v: seen.append(kind))

    th = threading.Thread(target=run)
    th.start()
    time.sleep(1.5)
    assert "sandbox.queued" in seen
    first.close()
    th.join(30)
    assert "sandbox.slot_ready" in seen and out["s"].state()["state"] in ("READY", "ACTIVE")
    out["s"].close()
    a.close()
    b.close()


def test_sdk_auto_solves_pow() -> None:
    sb_cfg = make_sb(max_sessions=4, quota_profile="public", pow={"bits": 8, "prefix_creates_1h": 1},
                     prefix={"create_burst": 10, "create_per_h": 100}, spawn={"timeout_s": 4})
    st = SbxStack(sb=sb_cfg, sb_api={"quota_profile": "public"}, access_mode="public")
    try:
        sdk = A.Sandbox(st.base, principal_hint="c" * 32, auto_keepalive=False)
        s1 = sdk.create("synthcity", None)
        s1.close()
        st.pt.wait(lambda: not st.broker.live(), 10, "ended")
        s2 = sdk.create("synthcity", None)           # 1 h 内已创建 1 次：需要工作量证明，SDK 自动求解
        assert s2.sid != s1.sid
        assert st.pt.call(lambda: len(st.broker.pow.used)) == 1      # 第二次创建附了已验证的解
        s2.close()
        sdk.close()
    finally:
        st.close()


def test_sdk_park_auto_resume_and_capacity(st: SbxStack) -> None:
    sb = A.Sandbox(st.base, principal_hint="d" * 32, auto_keepalive=False)
    cap = sb.capacity()
    assert cap["schema"] == "awr.capacity.v1" and cap["sandbox"]["max"] == 1
    s = sb.create("synthcity", None)
    tok0 = s.token
    p = s.park("idle")
    assert p["state"] in ("PARKING", "PARKED")
    st.pt.wait(lambda: st.broker.sessions[s.sid].state == "PARKED", 8, "PARKED")
    assert s.state()["state"] == "PARKED"
    with pytest.raises(A.AwrError) as ei:
        s.keepalive()                                      # 续期不唤醒驻留会话
    assert ei.value.code == 515
    c = s.clock("pause")                                   # 515 → 自动恢复一次 → 重试成功
    assert c["status"] in ("succeeded", "accepted") and s.token != tok0
    assert st.broker.sessions[s.sid].state in ("READY", "ACTIVE")
    s.park("user")
    st.pt.wait(lambda: st.broker.sessions[s.sid].state == "PARKED", 8, "PARKED again")
    other = A.Sandbox(st.base, principal_hint="e" * 32, auto_keepalive=False)
    o = other.create("synthcity", None)                    # 槽位被占：恢复需排队
    out: dict = {}
    seen: list[str] = []

    def run() -> None:
        out["info"] = s.resume(wait=True, timeout_s=60, on_queue=lambda kind, v: seen.append(kind))

    th = threading.Thread(target=run)
    th.start()
    time.sleep(1.5)
    assert "sandbox.queued" in seen
    o.close()
    th.join(30)
    assert "sandbox.slot_ready" in seen and out["info"]["state"] in ("READY", "ACTIVE")
    s.close()
    sb.close()
    other.close()
