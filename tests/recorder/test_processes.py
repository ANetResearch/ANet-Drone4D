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

pytestmark = [pytest.mark.ext, pytest.mark.needs_data]
ROOT = Path(__file__).resolve().parents[2]
WORLDS = Path(os.environ.get("AWR_WORLDS_DIR") or ROOT / "worlds")


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def sup(tmp_path_factory: pytest.TempPathFactory):
    # supervisor 的默认世界是深圳，未构建时回退到 synthcity（ADR-077）；两者都没有时 api 永远不就绪，不必等满 120 s（SHOW-CI）
    if not any((WORLDS / w / "world.json").exists() for w in ("shenzhen", "synthcity")):
        pytest.skip("worlds/shenzhen 与 worlds/synthcity 都未构建（make worlds 或 make demo-world）")
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
        # FX-GW：网关在 sys/start 之后等待 proc/replay-worker/ready 再 open（此前冷启动期间稳定 213）
        assert pb["status"] == "paused", pb
        mid = (pb["dataStart_ns"] + pb["dataEnd_ns"]) // 2
        e_open = pb["epoch"]
        await c.send({"op": "playback", "cmd": "seek", "seek_ns": int(mid), "request_id": "ps"})
        # playback 命令串行（17 §6.11）：等本次 seek 的终态 did_seek 与新纪元的 SNAPSHOT，再发 close（否则 busy 105）
        await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x.get("request_id") == "ps"
                      and (x.get("did_seek") or x["status"] == "error"), 15)
        assert [m for m in c.texts if m["op"] == "playbackState" and m.get("request_id") == "ps"][-1].get("did_seek")
        await c.until(lambda k, x: k == "batch" and x.header.flags & F.BATCH_REPLAY and x.header.flags & F.BATCH_SNAPSHOT
                      and x.header.epoch != e_open, 10)
        # 回放名册来自 replay-worker（open 时在途的实时 roster 回复被丢弃，FX-GW）
        assert any(ch["topic"].startswith("uav/") and ch["topic"].endswith("/state") for ch in c.channels.values())
        await c.send({"op": "playback", "cmd": "close", "request_id": "pc"})
        await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo" and x["mode"] == "live", 20)
        await c.ws.close()

    asyncio.run(run())
