"""线协议 `drone4d.bench.wire.v1`（AWR-17 §18.9）：黄金向量（宿主与 SDK 编解码逐字节一致）、帧校验，以及容器 Agent 经 Unix
套接字运行与进程内运行逐字节一致（同一 Run 的位姿摘要与 report 相同）。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from awr.agentsdk.wire import T, WireError, WireSocket, decode_body, encode_frame

ROOT = Path(__file__).resolve().parents[3]
GOLD = ROOT / "packages/contracts/bench/golden/wire"
SDK = ROOT / "tools/sdk/drone4d/src"
sys.path.insert(0, str(SDK))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _lines(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _gold(name: str) -> bytes:
    return bytes.fromhex((GOLD / f"{name}.hex").read_text().replace(" ", "").strip())


def test_golden_vectors_host_and_sdk() -> None:
    from drone4d import wire as sdk

    hello = {"protocol": "drone4d.bench.wire.v1", "sdk_version": "1.0.0", "prefs": {"camera": "latest", "lidar": "all", "max_per_sensor": 4}}
    for enc in (encode_frame, sdk.encode_frame):
        assert enc(T.HELLO, hello) == _gold("hello")
        assert enc(T.READY, {}) == _gold("ready")
        assert enc(T.ERROR, {"code": 661, "why": "wall_budget"}) == _gold("error_661")
    assert decode_body(T.HELLO, _gold("hello")[8:]) == hello


def test_frame_checks() -> None:
    a, b = socket.socketpair()
    ws = WireSocket(b)
    a.sendall(bytes([1, 0, 0, 0, 0x03, 1, 1, 0, 0x80]))                 # flags ≠ 0
    with pytest.raises(WireError) as e:
        ws.recv(time.monotonic() + 1)
    assert (e.value.code, e.value.why) == (666, "frame")
    a.sendall(bytes([2, 0, 0, 0, 0x03, 0, 1, 0, 0xc1, 0x00]))           # 0xc1 不是合法 msgpack
    with pytest.raises(WireError) as e:
        ws.recv(time.monotonic() + 1)
    assert e.value.why == "decode"
    a.sendall(encode_frame(T.ACT, {"k": 3}))
    assert ws.recv(time.monotonic() + 1) == (T.ACT, {"k": 3})
    with pytest.raises(TimeoutError):
        ws.recv(time.monotonic() + 0.05)
    a.close()
    b.close()


def test_socket_agents_match_in_process(synth_event, tmp_path: Path) -> None:
    from drone4d.baselines import BASELINES
    from test_env import _run, _spec

    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.remote import SocketAgentHost, run_remote_episode

    m0 = _run(_spec(synth_event, tmp_path / "local", dur=40.0), lambda u: BASELINES["track_share"]())
    spec = _spec(synth_event, tmp_path / "remote", dur=40.0)
    uavs = [f.uav for f in spec.fleet]
    hosts = {u: SocketAgentHost(u, tmp_path / "sock" / u / "agent.sock") for u in uavs}
    env_ = {**os.environ, "PYTHONPATH": f"{SDK}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"}
    procs = [subprocess.Popen([sys.executable, "-m", "drone4d.serve", "--baseline", "track_share", "--sock", str(h.path)], env=env_)
             for h in hosts.values()]
    try:
        for h in hosts.values():
            assert h.accept()["protocol"] == "drone4d.bench.wire.v1"
        m1, stats = run_remote_episode(BenchEnv(spec), hosts, step_budget_s=30.0)
    finally:
        for p in procs:
            p.wait(30)
    assert all(p.returncode == 0 for p in procs)
    assert all(s["exited"] is None and s["timeouts"] == 0 and s["protocol_errors"] == 0 for s in stats.values())
    assert m1["pose_sha256"] == m0["pose_sha256"]
    assert _lines(tmp_path / "local/reports.jsonl") == _lines(tmp_path / "remote/reports.jsonl")
    for u in uavs:
        assert (tmp_path / f"local/agents/{u}/acts.jsonl").read_bytes() == (tmp_path / f"remote/agents/{u}/acts.jsonl").read_bytes()


def test_hello_timeout_and_version(tmp_path: Path) -> None:
    from awr.agentsdk.env.remote import SocketAgentHost

    h = SocketAgentHost("uav-01", tmp_path / "a.sock", hello_timeout_s=0.2)
    with pytest.raises(WireError) as e:
        h.accept()
    assert (e.value.code, e.value.why) == (665, "hello_timeout")
    h.close()
    h = SocketAgentHost("uav-01", tmp_path / "b.sock", hello_timeout_s=2.0)
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(str(h.path))
    c.sendall(encode_frame(T.HELLO, {"protocol": "drone4d.bench.wire.v2", "sdk_version": "9", "prefs": {}}))
    with pytest.raises(WireError) as e:
        h.accept()
    assert e.value.why == "version"
    t, body = WireSocket(c).recv(time.monotonic() + 1)
    assert t == T.ERROR and body == {"code": 666, "why": "version"}
    c.close()
    h.close()
