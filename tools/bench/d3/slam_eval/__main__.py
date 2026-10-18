"""`python tools/bench/d3/slam_eval <子命令>` 或 `PYTHONPATH=python:tools/bench/d3 python -m slam_eval <子命令>`。"""

import sys
from pathlib import Path

if not __package__:                                                         # 以目录方式运行：把 tools/bench/d3 放到导入路径
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    _repo_py = Path(__file__).resolve().parents[4] / "python"
    if _repo_py.is_dir() and str(_repo_py) not in sys.path:
        sys.path.insert(1, str(_repo_py))

from slam_eval.cli import main

sys.exit(main())
