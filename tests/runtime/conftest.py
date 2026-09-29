"""tests/runtime 公共夹具（M11 运行时库）。辅助函数在 rtlib.py（--import-mode=importlib 下经 sys.path 垫片导入）。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def shm_dir():
    d = Path(tempfile.mkdtemp(prefix="awr-test-", dir="/dev/shm"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def namespace() -> str:
    return f"awr/test/r{uuid.uuid4().hex[:12]}"


@pytest.fixture(params=["local", "zenoh"])
def bus_kind(request) -> str:
    return request.param
