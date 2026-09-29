"""M10 测试公共设置：本目录的辅助模块可直接 import；每个用例内 plan-pool 缺省 inline（提交即执行，结果仍在下一次
drain 生效）、不自动加载剧本。环境变量只在用例期间经 monkeypatch 设置，不泄漏到同一 pytest 进程中其他模块的用例
（例如 e2e 启动的 sim-core 子进程）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


@pytest.fixture(autouse=True)
def _m10_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWR_PLAN_POOL", "inline")
    monkeypatch.setenv("AWR_SCENARIO_LOAD", "0")
