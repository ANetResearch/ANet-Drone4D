"""tests/agent 公共夹具（M14）：`--import-mode=importlib` 下经 sys.path 垫片导入 `fakes`（fake SimBridge、S3 装配）。"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
