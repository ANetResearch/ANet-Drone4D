"""tests/sandbox 公共设置（M17）：把本目录与 tests/rt 加入 sys.path（--import-mode=importlib 下导入 sbharness、fakesim、
fakepool、rtc）。"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent / "rt"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
