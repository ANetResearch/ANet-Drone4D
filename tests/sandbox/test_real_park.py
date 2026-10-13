"""真实沙盒 sim-core 的驻留与恢复（M17-FR-070、FR-073、NFR-016 的进程链路；D2-AC-45 的功能部分）：池经 zenoh 拉起真实的
`python -m awr.sim.runtime`，播放约 2 s 后驻留：暂停、checkpoint 落盘（M08 正式接口不在时走过渡路径：强制写一代并在停止时镜像），
进程退出、tmpfs 删除、驻留目录含 checkpoint 与配置快照；恢复后环头部的仿真时刻与驻留时刻相差 ≤ 0.1 s、机体仍在（checkpoint 生效，
epoch + 1）。需要已构建的 worlds/synthcity（缺失时 skip）。"""

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
from awr.sandbox.park import CKPT_NAME, SNAP_NAME
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


async def _until(pred, timeout: float, what: str) -> None:   # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"等待超时：{what}")


@pytest.mark.skipif(not (ROOT / "worlds" / "synthcity" / "world.json").exists(), reason="worlds/synthcity 未构建")
def test_real_park_and_resume(tmp_path: Path) -> None:
    rv = f"tcp/127.0.0.1:{_free_port()}"
    shm = Path("/dev/shm/awr") / f"t-rpark-{os.getpid()}"

    async def go() -> None:
        router = ZenohBus.open("rv", namespace="awr/_rv", listen=[rv], announce=False)
        s = PoolSettings(sb=make_sb(max_sessions=1, spawn={"timeout_s": 90}), state_dir=tmp_path / "state", shm_root=shm,
                         q_cores=2.5, delegated=False, rendezvous=rv, profile="ci", env=dict(os.environ),
                         identity_dir=tmp_path / "identity")
        pool = Pool(s, worlds=make_worlds(), templates=TemplateCatalog(HERE / "fixtures" / "templates"), planhost=False,
                    bus_kind="zenoh")
        await pool.start()
        try:
            r = await pool.dispatch("create", {"principal_id": "p-" + "b" * 32, "addr": "127.0.0.1",
                                               "req": {"world": "synthcity", "template": "t5_area_guard"}})
            sid = r["result"]["session"]["sid"]
            await _until(lambda: pool.broker.sessions[sid].state == "READY", 95, "READY")
            rec = pool.broker.sessions[sid]
            await pool.dispatch("activity", {"items": {sid: {"ws_n": 1, "api_calls": 1}}})
            await _until(lambda: rec.clock_state == "PLAYING" and rec.usage.get("rate_achieved", 0) > 0.5, 20, "PLAYING")
            await asyncio.sleep(2.0)
            pid, e0 = rec.pid, rec.epoch
            t0 = time.monotonic()
            assert (await pool.dispatch("park", {"sid": sid, "reason": "idle"}))["http"] == 202
            await _until(lambda: pool.broker.sessions[sid].state == "PARKED", 20, "PARKED")
            park_s = time.monotonic() - t0
            assert rec.resume_mode == "checkpoint", rec
            t_parked = int(rec.t_sim_ns_parked or 0)
            assert t_parked >= 1_500_000_000
            pd = s.parked_dir / sid
            assert (pd / CKPT_NAME).stat().st_size > 0 and (pd / SNAP_NAME).exists() and rec.park_bytes <= 8 * (1 << 20)
            assert not (shm / sid).exists()
            assert not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2]
            t1 = time.monotonic()
            assert (await pool.dispatch("resume", {"sid": sid}))["http"] == 201
            await _until(lambda: pool.broker.sessions[sid].state == "READY" and not rec.resuming, 95, "resumed")
            resume_s = time.monotonic() - t1
            b = pool.broker.binding(rec)
            t_now = pool.launcher.ring_t_sim(b)
            assert rec.resume_mode == "checkpoint", (rec.resume_mode, t_now, t_parked,
                                                     (s.runs_dir / sid / "logs" / "sim-core.log").read_text()[-3000:])
            assert t_now is not None and abs(t_now - t_parked) <= 100_000_000
            assert rec.epoch == e0 + 1 and not pd.exists()
            await pool.dispatch("activity", {"items": {sid: {"ws_n": 1, "api_calls": 1}}})
            await _until(lambda: rec.clock_state == "PLAYING", 15, "PLAYING after resume")
            await asyncio.sleep(1.0)
            assert rec.usage.get("vehicles") == 1
            print(f"park {park_s:.2f} s, resume {resume_s:.2f} s, t_sim {t_parked / 1e9:.3f} s")
            await pool.dispatch("stop", {"sid": sid})
            await _until(lambda: pool.broker.sessions[sid].state == "ENDED", 20, "ENDED")
        finally:
            await pool.stop(maintenance=True, grace_s=10)
            router.close()

    try:
        asyncio.run(go())
    finally:
        shutil.rmtree(shm, ignore_errors=True)
