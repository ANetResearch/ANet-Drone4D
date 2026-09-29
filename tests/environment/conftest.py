"""tests/environment 公共设置：把本目录加入 sys.path 以导入 `envfix`（pytest 以 importlib 模式运行）。"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
