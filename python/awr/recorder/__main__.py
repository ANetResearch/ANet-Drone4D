"""`python -m awr.recorder`：recorder 进程入口（supervisor 常驻启动，layer ext；configs/runtime.yaml `procs.recorder`）。"""

import sys

from .app import main

sys.exit(main())
