"""已弃用的容器入口：`python -m drone4d.serve` 与 `drone4d-agent` 转发到 `anet4d.serve`（ANet4D V0.3.2 保留一个版本）。

导入 `drone4d` 包时已发出 DeprecationWarning（缺省过滤器下命令行不显示），入口另在 stderr 打印一行改用提示；参数与行为同 `anet4d.serve`。"""

from __future__ import annotations

import sys

from anet4d.serve import main as _main

NOTICE = "drone4d: 'python -m drone4d.serve' and 'drone4d-agent' are deprecated; use 'python -m anet4d.serve' or 'anet4d-agent'"


def main(argv: list[str] | None = None) -> int:
    print(NOTICE, file=sys.stderr, flush=True)
    return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
