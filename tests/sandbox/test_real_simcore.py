"""真实沙盒 sim-core 的联调（M17-FR-004、FR-007、FR-025、FR-029 的进程链路；M17-AC-001 的功能部分）：池经 zenoh 汇合点拉起
`python -m awr.sim.runtime`（sandbox100、`AWR_SESSION_CONFIG`、`AWR_DETACHED=1`、`AWR_SIM_AUTOPLAY=0`），StateRing 心跳判 READY，
首个活动后以会话 principal 登记席位并 `play`（perf 的实际倍速约 1），导出配置快照，结束后进程退出、目录删除、快照留存。
需要已构建的 worlds/synthcity（缺失时 skip）。"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import time
from pathlib import Path

import pytest
from sbharness import make_sb, make_worlds

from awr.runtime.bus import ZenohBus
from awr.sandbox.config import PoolSettings
from awr.sandbox.pool import Pool
from awr.sandbox.templates import TemplateCatalog

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

pytestmark = pytest.mark.needs_data


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.mark.skipif(not (ROOT / "worlds" / "synthcity" / "world.json").exists(), reason="worlds/synthcity 未构建")
def test_real_sandbox_simcore(tmp_path: Path) -> None:
    rv = f"tcp/127.0.0.1:{_free_port()}"
    shm = Path("/dev/shm/awr") / f"t-real-{os.getpid()}"

    async def go() -> None:
        router = ZenohBus.open("rv", namespace="awr/_rv", listen=[rv], announce=False)
        s = PoolSettings(sb=make_sb(max_sessions=1, spawn={"timeout_s": 90}), state_dir=tmp_path / "state", shm_root=shm,
                         q_cores=2.5, delegated=False, rendezvous=rv, profile="ci", env=dict(os.environ),
                         identity_dir=tmp_path / "identity")
        pool = Pool(s, worlds=make_worlds(), templates=TemplateCatalog(HERE / "fixtures" / "templates"), planhost=False,
                    bus_kind="zenoh")
        await pool.start()
        try:
            r = await pool.dispatch("create", {"principal_id": "p-" + "a" * 32, "addr": "127.0.0.1",
                                               "req": {"world": "synthcity", "template": "t5_area_guard"}})
            sid = r["result"]["session"]["sid"]
            t0 = time.monotonic()
            while pool.broker.sessions[sid].state == "SPAWNING" and time.monotonic() - t0 < 95:
                await asyncio.sleep(0.2)
            rec = pool.broker.sessions[sid]
            assert rec.state == "READY", (rec.state, (s.runs_dir / sid / "logs" / "sim-core.log").read_text()[-2000:])
            pid = rec.pid
            await pool.dispatch("activity", {"items": {sid: {"ws_n": 1, "api_calls": 1}}})
            end = time.monotonic() + 15
            while time.monotonic() < end and not (rec.clock_state == "PLAYING" and rec.usage.get("rate_achieved", 0) > 0.5):
                await asyncio.sleep(0.2)
            assert rec.clock_state == "PLAYING" and rec.usage.get("rate_achieved", 0) > 0.5 and rec.anon_mb > 0
            assert rec.usage.get("vehicles") == 1          # 模板中的 1 架 p600_mid360 经 session.json 装配
            snap = await pool.dispatch("snapshot", {"sid": sid})
            assert snap["http"] == 200 and snap["result"]["world_id"] == "synthcity" and len(snap["result"]["vehicles"]) == 1
            await pool.dispatch("stop", {"sid": sid})
            end = time.monotonic() + 20
            while time.monotonic() < end and pool.broker.sessions[sid].state != "ENDED":
                await asyncio.sleep(0.2)
            assert pool.broker.sessions[sid].state == "ENDED" and Path(pool.broker.sessions[sid].snapshot_path).exists()
            assert not (s.runs_dir / sid).exists() and not (shm / sid).exists()
            assert not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2]
        finally:
            await pool.stop(maintenance=True, grace_s=10)
            router.close()

    try:
        asyncio.run(go())
    finally:
        shutil.rmtree(shm, ignore_errors=True)
