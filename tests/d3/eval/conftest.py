"""评测测试复用 Env 测试的合成事件夹具（tests/d3/env/conftest.py）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_p = Path(__file__).resolve().parents[1] / "env" / "conftest.py"
_spec = importlib.util.spec_from_file_location("d3_env_conftest", _p)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
synth_event = _m.synth_event
