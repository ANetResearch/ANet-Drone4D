"""tests/chaos 公共设置（M16 §6.12；AWR-18 §8.8；M16-NFR-011）。

- 全部用例同时标 `chaos` 与 `perf`（带时间阈值，G1 跳过，经 `make chaos-core` / `make chaos` 在排他性能锁下执行）；
  D1-ext 的用例另标 `ext`。`make chaos-core` = `-m "chaos and not ext"`，`make chaos` = `-m chaos`。
- 只在 ci profile 的自起后端上注入故障；检测到本机有 demo profile 的运行时以退出码 11 拒绝（M16-NFR-011）。
- 后端辅助复用 `tests/e2e/awrproc.py`（sys.path 垫片）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for p in (str(_HERE), str(_HERE.parent / "e2e")):
    if p not in sys.path:
        sys.path.insert(0, p)


def demo_runs() -> list[int]:
    """本用户运行中的 demo profile supervisor（读 /proc/<pid>/cmdline 与 environ）。"""
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().split(b"\0")
            if b"awr.runtime.supervisor" not in b" ".join(cmd):
                continue
            env = (d / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        prof = None
        if b"--profile" in cmd:
            i = cmd.index(b"--profile")
            prof = cmd[i + 1] if i + 1 < len(cmd) else None
        if prof is None:
            prof = next((e.split(b"=", 1)[1] for e in env if e.startswith(b"AWR_PROFILE=")), b"dev")
        if prof == b"demo":
            out.append(int(d.name))
    return out


@pytest.fixture(autouse=True, scope="session")
def _refuse_demo_runtime():
    """只在真正执行混沌用例时检查（G1 的 `-m "not perf"` 不会触发）。"""
    if os.environ.get("AWR_CHAOS_ALLOW_DEMO") != "1" and demo_runs():
        pytest.exit(f"demo profile runtime is active (pids {demo_runs()}); chaos refuses to run (M16-NFR-011); "
                    f"修复：make stop", returncode=11)
    yield
