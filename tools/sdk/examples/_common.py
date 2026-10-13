"""示例公共参数：服务地址取命令行第一个参数或环境变量 AWR_SANDBOX_URL（缺省本机 sandbox-api）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awr_sandbox import Sandbox

BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AWR_SANDBOX_URL", "http://127.0.0.1:8002")


def sandbox() -> Sandbox:
    return Sandbox(BASE)
