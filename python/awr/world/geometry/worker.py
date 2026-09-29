"""V0.2 geo-worker（Open3D RaycastingScene，独立进程）。D1 为桩：启动即报"V0.2 提供"（M04 §2.1）。"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    print("geo-worker（Open3D RaycastingScene）在 V0.2 提供；D1 只有 sim-core 的 DSM 探针服务", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
