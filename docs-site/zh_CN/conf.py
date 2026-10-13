"""ANet Drone4D 文档站（zh_CN）的 Sphinx 配置；公共部分见 docs-site/_shared/conf_common.py。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))

from conf_common import configure

globals().update(configure("zh_CN"))
