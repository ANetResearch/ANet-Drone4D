"""tests/perception 公共夹具（M19）：插件只在隔离登记表中装配（`plib.PBench`），不影响其他目录的用例。"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "contracts", _HERE.parent / "targets", _HERE.parent / "sim"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
