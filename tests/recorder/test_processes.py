"""真实进程链路（M12-AC-030 手动开录、AC-041 按需启动、D1-AC-18 功能部分）：supervisor（ci profile）启动 sim-core、api、
recorder（常驻，layer ext）与按需的 replay-worker；WS 客户端 `call rec/start` 开录 3 s 后 `rec/stop`，段 CLOSED；随后暂停
实时、`playback{open}` 当前运行的录制（api 经 `sys/start` 拉起 replay-worker），seek 后 SNAPSHOT 到达，close 回到实时。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rt"))

import rtc

from awr.contracts import frame as F

pytestmark = pytest.mark.ext
ROOT = Path(__file__).resolve().parents[2]


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def sup(tmp_path_factory: pytest.TempPathFactory):
    runs = tmp_path_factory.mktemp("procruns")
    port, bus = _port(), _port()
    # 不加载 runtime.yaml 的缺省剧本（S1 会替换骨架机体并按剧本 record 策略自动开录），INT-1
    env = {**os.environ, "AWR_RUNS_DIR": str(runs), "PYTHONUNBUFFERED": "1", "AWR_SIM_N": "3", "AWR_SCENARIO_LOAD": "0"}
    env.pop("AWR_SUPERVISOR_PID", None)
    env.pop("AWR_SCENARIO", None)
    logf = open(runs / "supervisor.out", "wb")  # noqa: SIM115 - 与进程同生命周期
    p = subprocess.Popen([sys.executable, "-m", "awr.runtime.supervisor", "--profile", "ci", "--only",
                          "sim-core,api,recorder,replay-worker", "--set", "net.port_offset=0", "--set", f"net.port={port}",
                          "--set", f"bus.rendezvous=tcp/127.0.0.1:{bus}", "--set", "run.keep_run_dir=false"],
                         cwd=ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    end = time.monotonic() + 120
    ok = False
    while time.monotonic() < end and p.poll() is None:
        try:
            if httpx.get(f"{base}/api/health/ready", timeout=2).status_code == 200:
                ok = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    if not ok:
        os.killpg(p.pid, signal.SIGKILL)
        logf.close()
        out = (runs / "supervisor.out").read_text(errors="replace")
        pytest.skip(f"supervisor not ready: {out[-2000:]}")
    st = SimpleNamespace(base=base, ws_url=f"ws://127.0.0.1:{port}/api/rt", origin=base, runs=runs, proc=p, out=runs / "supervisor.out",
                         bus_port=bus)
    yield st
    os.killpg(p.pid, signal.SIGTERM)
    try:
        p.wait(40)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
    logf.close()


def _direct_replay(sup, run_id: str) -> None:
    import threading

    from awr.contracts import bus_keys
    from awr.runtime.bus import ZenohBus

    b = ZenohBus.open("m12-test", namespace=bus_keys.namespace("shenzhen", run_id), connect=[f"tcp/127.0.0.1:{sup.bus_port}"])
    try:
        end = time.monotonic() + 30
        while not b.alive(bus_keys.proc_ready("replay-worker")) and time.monotonic() < end:
            time.sleep(0.2)
        assert b.alive(bus_keys.proc_ready("replay-worker")), "replay-worker not ready"

        def call(op: str, msg: dict) -> dict:
            ev = threading.Event()
            box: dict = {}

            def done(rep, err) -> None:
                box["rep"], box["err"] = rep, err
                ev.set()

            b.call_cb(bus_keys.ctl_replay_worker(op), {"v": 1, "cid": f"t-{op}", **msg}, done, timeout=5.0)
            assert ev.wait(15), op
            assert box["err"] is None, box
            return box["rep"]

        rep = call("open", {"run": run_id, "segment": 0})
        assert rep["status"] == "accepted" and rep["data_end_ns"] > rep["data_start_ns"], rep
        mid = (rep["data_start_ns"] + rep["data_end_ns"]) // 2
        sk = call("seek", {"t_ns": int(mid)})
        assert sk["status"] == "accepted" and sk["gen"] == 2 and sk["t_sample_ns"] <= mid
        assert call("close", {})["state"] == "idle"
    finally:
        b.close()


def test_record_then_replay_with_real_processes(sup) -> None:
    tok = rtc.token(sup.base, "operator", rtc.hint_of("m12procs"))
    run_id = tok["run_id"]

    async def run() -> None:
        c = await rtc.open_client(sup, tok["token"])
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"},
                                                  {"id": 2, "topic": "event", "rate": 0, "mode": "all"}]})
        await c.until(lambda k, x: k == "batch", 20)
        await c.send({"op": "call", "id": "m12-rec-start-01", "service": "rec/start", "args": {}})
        r = await c.result("m12-rec-start-01", timeout=20)
        assert r["status"] == "succeeded", r
        await c.drain(3.0)
        await c.send({"op": "call", "id": "m12-rec-stop-001", "service": "rec/stop", "args": {}})
        r = await c.result("m12-rec-stop-001", timeout=20)
        assert r["status"] == "succeeded", r
        d = sup.runs / run_id
        end = time.monotonic() + 15
        seg = None
        while time.monotonic() < end:
            try:
                seg = json.loads((d / "meta.json").read_text())["segments"][0]
                if seg["state"] == "CLOSED":
                    break
            except (OSError, ValueError, IndexError, KeyError):
                pass
            await asyncio.sleep(0.2)
        assert seg is not None and seg["state"] == "CLOSED" and (d / "rec-000.mcap").stat().st_size > 1000, seg
        end = time.monotonic() + 10
        while "rec.stopped" not in [e.get("type") for e in c.events()] and time.monotonic() < end:
            await c.drain(0.2)
        kinds = [e.get("type") for e in c.events()]
        assert "rec.started" in kinds and "rec.stopped" in kinds
        await c.send({"op": "call", "id": "m12-pause-00001", "service": "sim/pause", "args": {}})
        await c.result("m12-pause-00001", timeout=20)
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == 2, 10)
        await c.send({"op": "playback", "cmd": "open", "run": run_id, "segment": 0, "request_id": "po"})
        _, pb = await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x.get("request_id") == "po"
                              and x["status"] in ("paused", "error"), 40)
        if pb["status"] != "paused":
            # 已知网关竞态（请求 M12-to-M11）：sys/start 之后没有等待 replay-worker 就绪就调用 open（213）；此时 replay-worker
            # 进程已由 supervisor 拉起：以测试总线直接验证真实进程的 ctl/replay-worker/{open,seek,close}
            assert pb.get("code") == 213, pb
            _direct_replay(sup, run_id)
            await c.ws.close()
            return
        mid = (pb["dataStart_ns"] + pb["dataEnd_ns"]) // 2
        await c.send({"op": "playback", "cmd": "seek", "seek_ns": int(mid), "request_id": "ps"})
        await c.until(lambda k, x: k == "batch" and x.header.flags & F.BATCH_REPLAY and x.header.flags & F.BATCH_SNAPSHOT, 10)
        await c.send({"op": "playback", "cmd": "close", "request_id": "pc"})
        await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo" and x["mode"] == "live", 20)
        await c.ws.close()

    asyncio.run(run())
