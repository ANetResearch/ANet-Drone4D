"""生成物一致性：`apps/web/src/engine/sensors/specs.gen.ts` 与 golden 文件与当前 yaml/实现一致（`--check`）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def test_web_specs_up_to_date():
    subprocess.run([sys.executable, str(HERE / "gen_web_specs.py"), "--check"], check=True, timeout=120)


def test_golden_up_to_date():
    subprocess.run([sys.executable, str(HERE / "gen_golden.py"), "--check"], check=True, timeout=120)
