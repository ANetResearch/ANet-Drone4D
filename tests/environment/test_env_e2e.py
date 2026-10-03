"""端到端（M07-AC-009、M07-AC-010 网关部分、M07-AC-031 部分；API-AC-033）：进程内栈（LocalBus + LocalRing + 后台 sim-core
线程 + 真实 api 与 WS），插件 `awr.environment.stage` 经组合根导入。

- WS 订阅 env/state 得到自包含关键帧（presets_sha256 与契约一致）；`call env/preset` 经 ctl/sim-core/cmd → CommandEngine →
  登记的 `env/preset` 处理者，准入 accepted 后下一个 20 ms 网格生效，新版本经 evt/sim-core/env 到达客户端；
- `call env/query` 经 ctl/sim-core/query 返回 SoA；越界 110；viewer 写 115；
- REST：GET /api/env/state、/api/env/presets（ETag），POST /api/env/query。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import msgpack
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rt"))

import rtc

from awr.contracts.presets import PRESETS_SHA256


class EnvStack:
    def __init__(self) -> None:
        import uvicorn

        from awr.api.inproc import InprocSim, inproc_settings
        from awr.api.main import create_app
        from awr.runtime.statering import LocalRing

        self.settings = replace(inproc_settings(rtc.WORLD), hello_timeout_s=2.0)
        self.sim = InprocSim(self.settings, n=1, plugins=("awr.environment.stage",)).start()
        self.app = create_app(self.settings, ring_cls=LocalRing)
        self.port = rtc.free_port()
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, ws="websockets", log_level="warning", lifespan="on",
                             ws_per_message_deflate=False, ws_max_size=262144)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, name="uvicorn-env", daemon=True)
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


pytestmark = pytest.mark.needs_data  # stack 加载已构建的深圳（AWR-18 §8.2，SHOW-CI）


@pytest.fixture(scope="module")
def stack():
    if not rtc.world_ready():
        pytest.skip(f"worlds/{rtc.WORLD} not built")
    s = EnvStack()
    yield s
    s.close()


def test_keyframe_preset_and_query_over_ws(stack):
    async def run():
        tok = rtc.token(stack.base, "operator", hint=rtc.hint_of("envoperator"))["token"]
        c = await rtc.open_client(stack, tok)
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "env/state", "rate": 10, "mode": "latest"}]})
        eid = None

        def got_env(k, x):
            nonlocal eid
            eid = eid or c.topic_ids.get("env/state")
            return k == "batch" and eid is not None and x.by_channel(eid) is not None

        await c.until(got_env, 30)  # 上限只防挂死（负载敏感，INT-1 §7.11）
        kf = c.latest_msgpack(eid)
        assert kf["schema"] == "awr.env.keyframe.v1" and kf["config"]["presets_sha256"] == PRESETS_SHA256
        v0 = kf["version"]
        await c.send({"op": "call", "id": "c-env-1", "service": "env/preset", "args": {"name": "fog", "duration_s": 5}})
        r = await c.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "c-env-1", 30)
        assert r[1]["status"] in ("accepted", "succeeded"), r[1]
        fin = await c.result("c-env-1", timeout=30)            # ADR-058：插件命令 running → succeeded（终态，effect OK）
        assert fin["status"] == "succeeded" and fin["effect"]["status"] == "OK", fin

        def newer(k, x):
            if k != "batch" or x.by_channel(eid) is None:
                return False
            f = msgpack.unpackb(x.payload(x.by_channel(eid)), raw=False, strict_map_key=False)
            return f["version"] > v0 and f["to_preset"] == "fog"

        await c.until(newer, 30)
        await c.send({"op": "call", "id": "c-env-q", "service": "env/query", "args": {"points": [[0, 0, 40], [30, 20, 80]]}})
        q = await c.result("c-env-q", timeout=30)  # env/query 是 atomic 慢任务，负载下可顺延（STARVE_NS 兜底）
        assert q["status"] == "succeeded" and q["data"]["n"] == 2 and len(q["data"]["wind_mps"]) == 2
        await c.send({"op": "call", "id": "c-env-bad", "service": "env/set", "args": {"patch": {"wind": {"speed_ref_mps": 99}}}})
        bad = await c.result("c-env-bad", timeout=30)
        assert bad["status"] == "rejected" and bad["code"] in (110, 300)
        await c.ws.close()

    asyncio.run(run())


def test_rest(stack):
    tok = rtc.token(stack.base, "viewer")["token"]
    h = {"authorization": f"Bearer {tok}", "origin": stack.origin}
    end = time.monotonic() + 30
    while True:
        r = httpx.get(f"{stack.base}/api/env/state", headers=h, timeout=10)
        if r.status_code == 200 or time.monotonic() > end:
            break
        time.sleep(0.3)
    assert r.status_code == 200 and r.json()["schema"] == "awr.env.keyframe.v1"
    assert httpx.get(f"{stack.base}/api/env/state?expand=1", headers=h, timeout=10).json()["to"]["wind"]["speed_ref_mps"] > 0
    p = httpx.get(f"{stack.base}/api/env/presets", headers=h, timeout=10)
    assert p.status_code == 200 and p.headers["etag"] == f'"{PRESETS_SHA256}"'
    q = httpx.post(f"{stack.base}/api/env/query", json={"points": [[0, 0, 50]], "fields": ["WIND", "THERMO"]}, headers=h, timeout=10)
    # ADR-057（FX-SIM2）：不可分片慢任务按积分与借贷启动，`ctl/sim-core/query` 有界时延，不再挂起到 202
    assert q.status_code == 200, q.text
    assert q.json()["data"]["n"] == 1
    w = httpx.post(f"{stack.base}/api/env/preset", json={"name": "rain"}, headers=h, timeout=10)
    assert w.status_code == 403
