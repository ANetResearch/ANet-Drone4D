"""`python -m awr.sim.runtime`：sim-core 进程入口（supervisor 以此启动，M11-R-to-M08 第 1 条；M08 §9.1）。"""

import sys

from .main import main

sys.exit(main())
