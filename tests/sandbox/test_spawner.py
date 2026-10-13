"""拉起、就绪、停止与清理（M17-FR-003、FR-007、FR-025、AC-001、AC-005 的进程部分）：真实子进程（tests/sandbox/fake_simcore.py），
真实 StateRing、setsid、日志文件、run 目录与 tmpfs 目录的创建与删除、SIGTERM 与 SIGKILL。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest
from sbharness import make_settings

from awr.sandbox.cgroups import CgroupManager
from awr.sandbox.model import SessionRecord, new_sid
from awr.sandbox.spawner import ProcessLauncher, default_plugins

FAKE = [sys.executable, str(Path(__file__).resolve().parent / "fake_simcore.py")]


def _rec(world: str = "synthcity") -> SessionRecord:
    return SessionRecord(sid=new_sid(), state="SPAWNING", world=world, world_class="S", template=None, principal_id="p-x",
                         prefix_h="h", agg_prefix_h="a")


def _launcher(tmp_path: Path, **env: str):
    s = make_settings(tmp_path)
    s.simcore_argv = tuple(FAKE)
    s.env = {**os.environ, **env, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "python"),
             "AWR_SCENARIO": "s7-synthcity-relay", "AWR_SUPERVISOR_PID": "1"}
    return s, ProcessLauncher(s, CgroupManager(s.sb, delegated=False))


def _binding(s, rec):
    from awr.runtime.binding import RunBinding

    return RunBinding.make(rec.sid, rec.world, kind="sandbox", run_dir=s.shm_root / rec.sid,
                           secret_path=s.runs_dir / rec.sid / "secret", persist_dir=s.runs_dir / rec.sid)


def test_launch_ready_terminate_cleanup(tmp_path: Path) -> None:
    s, ln = _launcher(tmp_path)
    rec = _rec()
    b = _binding(s, rec)

    async def go() -> None:
        h = await ln.launch(rec, b, {"world_id": "synthcity", "vehicles": []}, restart=False)
        assert h.pid > 0 and h.starttime is not None and os.getpgid(h.pid) == h.pid     # setsid
        assert await ln.wait_ready(rec, b, h, 15.0) is None
        rd = s.runs_dir / rec.sid
        assert (rd / "secret").stat().st_mode & 0o777 == 0o600 and len((rd / "secret").read_bytes()) == 32
        assert (rd / "session.json").exists() and (rd / "logs" / "sim-core.log").exists()
        env = Path(f"/proc/{h.pid}/environ").read_bytes().split(b"\0")
        for want in (b"AWR_RUN=" + rec.sid.encode(), b"AWR_DETACHED=1", b"AWR_CLOCK_PROFILE=sandbox100",
                     b"AWR_PLAN_POOL=remote", b"AWR_SIM_AUTOPLAY=0"):
            assert want in env, want
        assert not any(x.startswith((b"AWR_SUPERVISOR_PID=", b"AWR_SCENARIO=")) for x in env)
        age, hdr = ln.health(b, h.spawn_mono_ns)
        assert age is not None and age < 1000 and hdr is not None
        assert await ln.terminate(h.pid, grace_s=3.0)
        await h.proc.wait()
        ln.cleanup(rec, b)
        assert not rd.exists() and not b.run_dir.exists()

    try:
        asyncio.run(go())
    finally:
        shutil.rmtree(s.shm_root, ignore_errors=True)


def test_not_ready_and_exit_detected(tmp_path: Path) -> None:
    s, ln = _launcher(tmp_path, FAKE_NO_RING="1")
    rec = _rec()
    b = _binding(s, rec)

    async def go() -> None:
        h = await ln.launch(rec, b, None, restart=False)
        assert await ln.wait_ready(rec, b, h, 0.8) == "timeout"
        ln.kill(h.pid)
        await h.proc.wait()
        s2, ln2 = _launcher(tmp_path, FAKE_EXIT_AFTER_S="0.2", FAKE_NO_RING="1")
        rec2 = _rec()
        b2 = _binding(s2, rec2)
        h2 = await ln2.launch(rec2, b2, None, restart=False)
        why = await ln2.wait_ready(rec2, b2, h2, 10.0)
        assert why is not None and why.startswith("exit")
        ln.cleanup(rec, b)
        ln2.cleanup(rec2, b2)
        shutil.rmtree(s2.shm_root, ignore_errors=True)

    try:
        asyncio.run(go())
    finally:
        shutil.rmtree(s.shm_root, ignore_errors=True)


def test_hung_process_heartbeat_ages(tmp_path: Path) -> None:
    s, ln = _launcher(tmp_path, FAKE_HANG_AFTER_S="0.3")
    rec = _rec()
    b = _binding(s, rec)

    async def go() -> None:
        h = await ln.launch(rec, b, None, restart=False)
        assert await ln.wait_ready(rec, b, h, 15.0) is None
        await asyncio.sleep(1.0)
        age, _ = ln.health(b, h.spawn_mono_ns)
        assert age is not None and age > 500
        ln.kill(h.pid, dump=True)
        assert await asyncio.wait_for(h.proc.wait(), 5) != 0
        ln.cleanup(rec, b)

    try:
        asyncio.run(go())
    finally:
        shutil.rmtree(s.shm_root, ignore_errors=True)


def test_default_plugins_only_importable() -> None:
    p = default_plugins()
    assert "awr.sim.safety" in p and all("." in x for x in p)


@pytest.mark.parametrize("big", [True])
def test_log_truncated_when_oversized(tmp_path: Path, big: bool) -> None:
    s, ln = _launcher(tmp_path)
    log = tmp_path / "x.log"
    log.write_bytes(b"x" * (int(s.sb.disk.log_mb * (1 << 20)) + 10))
    ln._truncate_log(log)
    assert log.stat().st_size == 0
