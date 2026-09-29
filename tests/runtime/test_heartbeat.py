"""主循环心跳文件（M11-FR-010；AWR-17 §9.6）。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from awr.runtime import heartbeat as H
from awr.runtime.heartbeat import Heartbeat


def test_beat_and_age(shm_dir: Path) -> None:
    p = shm_dir / "hb.api"
    assert Heartbeat.age_ms(p) is None
    hb = Heartbeat(p)
    try:
        assert os.path.getsize(p) == 16 and os.stat(p).st_mode & 0o777 == 0o600
        assert Heartbeat.age_ms(p) is None  # 首次 beat 之前视为未就绪
        hb.beat()
        mono, pid, beat = H.read(p)
        assert pid == os.getpid() and beat == 1 and mono > 0
        time.sleep(0.05)
        age = Heartbeat.age_ms(p)
        assert age is not None and 40 <= age < 1000
        hb.beat()
        assert Heartbeat.age_ms(p) < 40 and H.read(p)[2] == 2
    finally:
        hb.close()


def test_suppress_hook(shm_dir: Path) -> None:
    hb = Heartbeat(shm_dir / "hb.x")
    try:
        hb.beat()
        before = H.read(shm_dir / "hb.x")
        H.suppress_heartbeats()
        hb.beat()
        assert H.read(shm_dir / "hb.x") == before
    finally:
        H.resume_heartbeats()
        hb.close()


def test_short_file_is_none(shm_dir: Path) -> None:
    (shm_dir / "hb.bad").write_bytes(b"\0" * 8)
    assert H.read(shm_dir / "hb.bad") is None and Heartbeat.age_ms(shm_dir / "hb.bad") is None
