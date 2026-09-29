"""tests/sim/backends 的 sys.path 垫片（pytest 以 importlib 模式运行，测试替身 fake_px4、prom_codec 按模块名导入）。"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
