"""M13 传感器位姿链端到端（FX-SIM2；M13-to-M08 第 1、2 条，M13-to-M00 第 3 条；M13-FR-013、FR-034）：进程内 sim-core（装配
M13 插件）+ 真实 api 与 WS：

- roster 条目带 `sensors[]`（p600：camera 0、thermal 1、mid360 2），网关据此广告 `uav/{id}/sensor/{name}/pose`；
- 订阅 `uav/{id}/sensor/camera/pose` 后兴趣集驱动 SensorPose48 行下发（agent_no、sensor_no 与 roster 一致）；
- `uav/{id}/state_ext` 带 M13 经 state_ext 钩子合并的 `loc.gnss_fix/sats/hdop` 与 `sens.gimbal`（schema 已登记）。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rt"))

import rtc

from awr.contracts.layouts import SENSOR_POSE48


class Stack:
    def __init__(self) -> None:
        import uvicorn

        from awr.api.inproc import InprocSim, inproc_settings
        from awr.api.main import create_app
        from awr.runtime.statering import LocalRing

        self.settings = replace(inproc_settings(rtc.WORLD), hello_timeout_s=2.0)
        self.sim = InprocSim(self.settings, n=1, plugins=("awr.sim.sensors.plugin",)).start()
        self.app = create_app(self.settings, ring_cls=LocalRing)
        self.port = rtc.free_port()
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, ws="websockets", log_level="warning", lifespan="on",
                             ws_per_message_deflate=False, ws_max_size=262144)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, name="uvicorn-m13", daemon=True)
        self.thread.start()
        end = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > end or not self.thread.is_alive():
                raise RuntimeError("uvicorn 未启动")
            time.sleep(0.02)
        self.base = f"http://127.0.0.1:{self.port}"
        self.ws_url = f"ws://127.0.0.1:{self.port}/api/rt"
        self.origin = self.base

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.sim.stop()
        shutil.rmtree(self.settings.run_dir, ignore_errors=True)
        from awr.sim.sensors import plugin

        plugin.uninstall()


@pytest.fixture(scope="module")
def stack():
    if not rtc.world_ready():
        pytest.skip(f"worlds/{rtc.WORLD} not built")
    s = Stack()
    yield s
    s.close()


def test_roster_sensors_pose_topic_and_state_ext(stack) -> None:
    async def run() -> tuple[dict, np.ndarray, dict]:
        tok = rtc.token(stack.base, "viewer")["token"]
        c = await rtc.open_client(stack, tok)
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "fleet/roster", "rate": 10, "mode": "latest"}]})
        await c.until(lambda k, x: "fleet/roster" in c.topic_ids and k == "batch"
                      and x.by_channel(c.topic_ids["fleet/roster"]) is not None, 10)
        e = c.latest_msgpack(c.topic_ids["fleet/roster"])["entries"][0]
        pose_t = f"uav/{e['id']}/sensor/camera/pose"
        ext_t = f"uav/{e['id']}/state_ext"
        await c.until(lambda k, x: pose_t in c.topic_ids and ext_t in c.topic_ids, 10)
        await c.send({"op": "subscribe", "subs": [{"id": 2, "topic": pose_t, "rate": 10, "mode": "latest"},
                                                   {"id": 3, "topic": ext_t, "rate": 2, "mode": "latest"}]})
        pid, xid = c.topic_ids[pose_t], c.topic_ids[ext_t]

        def got_both(k, x) -> bool:
            return any(b.by_channel(pid) is not None for b in c.batches) and \
                ((c.latest_msgpack(xid) or {}).get("loc") or {}).get("gnss_fix") is not None

        await c.until(got_both, 20)
        b = next(b for b in reversed(c.batches) if b.by_channel(pid) is not None)
        rows = np.frombuffer(b.payload(b.by_channel(pid)), SENSOR_POSE48)
        ext = c.latest_msgpack(xid)
        await c.ws.close()
        return e, rows, ext

    e, rows, ext = asyncio.run(run())
    names = {s["name"]: s["sensor_no"] for s in e["sensors"]}
    assert names.get("camera") == 0 and names.get("thermal") == 1, e["sensors"]
    assert rows.size >= 1 and int(rows["agent_no"][0]) == e["agent_no"] and int(rows["sensor_no"][0]) == 0
    loc = ext["loc"]
    assert loc["gnss_fix"] is not None and loc["sats"] is not None, loc
    assert ext.get("sens", {}).get("gimbal"), ext
    assert rtc.schema_errors("rt/payloads/uav_state_ext.schema.json", ext) == []
