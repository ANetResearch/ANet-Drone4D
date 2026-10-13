"""运维 CLI（M17-FR-065、AC-023 的功能部分）：status、list、accept off 后创建 503 `502{maintenance}`、limits、drain 先告知后结束、
stop；签名错误的 pool.secret 被拒绝。"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from brokerkit import hint
from sbharness import PoolThread, make_sb, make_settings

from awr.runtime.bus import LocalBus
from awr.sandbox import ctl


@pytest.fixture
def pt(tmp_path: Path):
    s = make_settings(tmp_path, make_sb(max_sessions=2, spawn={"timeout_s": 3}, notice_s=2))
    p = PoolThread(s)
    yield p
    p.stop()
    p.launcher.close()
    shutil.rmtree(s.shm_root, ignore_errors=True)


def _ctl(pt: PoolThread, *argv: str, state_dir: Path | None = None) -> int:
    async def go() -> int:
        bus = LocalBus.open("ctl-test", namespace=pt.namespace, announce=False)
        try:
            return await ctl.run(["--state-dir", str(state_dir or pt.state_dir), "--json", *argv], bus=bus)
        finally:
            bus.close()

    return asyncio.run(go())


def test_ctl_commands(pt: PoolThread, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    assert _ctl(pt, "status") == 0 and '"n_max": 2' in capsys.readouterr().out
    assert _ctl(pt, "accept", "off") == 0
    r = pt.op("create", {"principal_id": "p-" + hint(1), "addr": "198.51.100.1", "req": {"world": "synthcity"}})
    assert r["http"] == 503 and r["code"] == 502 and r["detail"]["why"] == "maintenance"
    assert _ctl(pt, "accept", "on") == 0
    assert _ctl(pt, "limits", "--max-sessions", "1") == 0 and pt.broker.n_max() == 1
    assert _ctl(pt, "limits", "--max-sessions", "auto") == 0 and pt.broker.n_max() == 2
    sid = pt.op("create", {"principal_id": "p-" + hint(2), "addr": "198.51.100.2", "req": {"world": "synthcity"}})["result"]["session"]["sid"]
    pt.wait(lambda: pt.broker.sessions[sid].state == "READY", 8, "READY")
    capsys.readouterr()
    assert _ctl(pt, "list") == 0 and sid in capsys.readouterr().out
    assert _ctl(pt, "drain", "--notice", "1") == 0
    pt.wait(lambda: pt.broker.sessions[sid].drain_at is not None, 3, "notice")
    pt.wait(lambda: pt.broker.sessions[sid].state == "ENDED", 10, "drained")
    assert pt.broker.sessions[sid].end_reason == "maintenance"
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "pool.secret").write_bytes(b"x" * 32)
    assert _ctl(pt, "status", state_dir=bad) == 1
    assert _ctl(pt, "status", state_dir=tmp_path / "none") == 2
