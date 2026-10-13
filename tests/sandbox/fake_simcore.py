"""假沙盒 sim-core 进程（M17 PRD §9.4 第 1 周：`tests/sandbox/fake_simcore.py`）：打开真实的 StateRing（`$AWR_RUN_DIR/state.sim-core`），
以 50 Hz 写心跳；可配置挂死（`FAKE_HANG_AFTER_S`：停止心跳但进程存活）、退出（`FAKE_EXIT_AFTER_S`）、不建环（`FAKE_NO_RING=1`）。
SIGTERM 时正常退出。用于 spawner、adopt 与 cgroup 用例。"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from awr.contracts import LAYOUT_ID
from awr.runtime.statering import StateRing

stop = False


def _term(*_a: object) -> None:
    global stop
    stop = True


def main() -> int:
    signal.signal(signal.SIGTERM, _term)
    rd = Path(os.environ["AWR_RUN_DIR"])
    rd.mkdir(parents=True, exist_ok=True)
    hang = float(os.environ.get("FAKE_HANG_AFTER_S") or 0)
    die = float(os.environ.get("FAKE_EXIT_AFTER_S") or 0)
    ring = None
    if os.environ.get("FAKE_NO_RING") != "1":
        ring, _ = StateRing.open_or_create(rd / "state.sim-core", capacity=16, slots=16, layout_id=LAYOUT_ID)
    t0 = time.monotonic()
    t_sim = 0
    while not stop:
        el = time.monotonic() - t0
        if die and el >= die:
            return 7
        if ring is not None and not (hang and el >= hang):
            t_sim += 20_000_000
            ring.heartbeat(t_sim, 1)
        time.sleep(0.02)
    return 0


if __name__ == "__main__":
    sys.exit(main())
