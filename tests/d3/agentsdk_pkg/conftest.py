"""tests/d3/agentsdk_pkg：夹具定义在 pkgfix.py（测试模块经 `from pkgfix import ...` 共用其常量与工具）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pkgfix import clean_venv, dist, synth_event

__all__ = ["clean_venv", "dist", "synth_event"]
