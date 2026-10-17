"""bench-api 与 bench 工具的设置（AWR-17 §18.3、§18.20；M27 §7.6）：数据根、密钥目录、会话库、Origin 白名单。

环境变量：`AWR_BENCH_DATA_ROOT`（缺省 /data/d3）、`AWR_BENCH_ALLOWED_ORIGINS`（逗号分隔）、`AWR_BENCH_INSECURE_COOKIE=1`
（只用于本机开发与测试，去掉 Cookie 的 Secure 属性）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["BenchSettings"]


@dataclass(frozen=True)
class BenchSettings:
    data_root: Path = Path("/data/d3")
    allowed_origins: tuple[str, ...] = ()
    secure_cookie: bool = True
    session_ttl_s: int = 43_200
    short_ttl_s: int = 600
    bind: str = "127.0.0.1"
    port: int = 18650
    extra: dict = field(default_factory=dict)

    @property
    def keys_dir(self) -> Path:
        return self.data_root / "keys"

    @property
    def db_path(self) -> Path:
        return self.data_root / "bench.sqlite"

    @property
    def dataset_dir(self) -> Path:
        return self.data_root / "dataset"

    @property
    def worlds_dir(self) -> Path:
        return self.data_root / "worlds"

    @classmethod
    def from_env(cls, **overrides) -> BenchSettings:
        env = os.environ
        kw: dict = {}
        if env.get("AWR_BENCH_DATA_ROOT"):
            kw["data_root"] = Path(env["AWR_BENCH_DATA_ROOT"])
        if env.get("AWR_BENCH_ALLOWED_ORIGINS"):
            kw["allowed_origins"] = tuple(o.strip() for o in env["AWR_BENCH_ALLOWED_ORIGINS"].split(",") if o.strip())
        if env.get("AWR_BENCH_INSECURE_COOKIE") == "1":
            kw["secure_cookie"] = False
        kw.update(overrides)
        return cls(**kw)
