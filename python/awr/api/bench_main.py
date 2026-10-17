"""bench-api 入口（AWR-17 §18.4 第 2 条；API-FR-094）：`python -m awr.api.bench_main`，只挂载 `awr/bench/api/` 路由，
缺省绑定 127.0.0.1:18650（dmax，经鉴权反向代理暴露在 ZeroTier 内网）。D1、D2 api 不挂载 bench 路由。
"""

from __future__ import annotations

import argparse

from awr.bench.api.app import create_bench_app
from awr.bench.settings import BenchSettings

__all__ = ["create_app", "main"]


def create_app(settings: BenchSettings | None = None):
    return create_bench_app(settings or BenchSettings.from_env())


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m awr.api.bench_main")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    a = ap.parse_args(argv)
    s = BenchSettings.from_env()
    uvicorn.run(create_app(s), host=a.host or s.bind, port=a.port or s.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
