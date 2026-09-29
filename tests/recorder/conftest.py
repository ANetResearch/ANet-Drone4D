"""tests/recorder 公共夹具（M12 §10.1）：合成录制（awr.recorder.synth）与记录遍历辅助。

合成录制按模块共享（一段 N = 60、30 s、2 架剧本标记机、每秒 5 条事件、每 7 s 一条严重事件的录制约 1.2 s），辅助函数在
rechelp.py（--import-mode=importlib 下经 sys.path 垫片导入）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rechelp

RUN = rechelp.RUN


@pytest.fixture(scope="module")
def runs_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("runs")


@pytest.fixture(scope="module")
def std_run(runs_dir: Path) -> dict:
    """标准合成录制：runs/<RUN>/rec-000.mcap（CLOSED）。"""
    from awr.recorder.synth import synthesize

    return synthesize(runs_dir / RUN, n=60, sim_s=30, marked=["sim-0001", "sim-0002"], critical_every_s=7)
