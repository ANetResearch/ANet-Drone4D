"""容器入口：`python -m drone4d.serve pkg.module:Factory [--sock PATH]`。Factory 为无参可调用对象（DroneAgent 子类或工厂函数），
每个容器服务一架机；也可以 `--baseline track_share` 直接服务内置基线。"""

from __future__ import annotations

import argparse
import importlib
import sys

from drone4d.wire import serve


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m drone4d.serve")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("factory", nargs="?", help="pkg.module:Factory")
    g.add_argument("--baseline", help="built-in baseline policy name")
    ap.add_argument("--sock", help="default $DRONE4D_AGENT_SOCK or /run/drone4d/agent.sock")
    a = ap.parse_args(argv)
    if a.baseline:
        from drone4d.baselines import BASELINES

        factory = BASELINES[a.baseline]
    else:
        mod, _, attr = a.factory.partition(":")
        factory = getattr(importlib.import_module(mod), attr or "Agent")
    serve(factory, a.sock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
