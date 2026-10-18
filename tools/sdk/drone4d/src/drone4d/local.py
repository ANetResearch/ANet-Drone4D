"""`drone4d.bench.make`（本地开发 Env）：在含 `awr` 的仓库环境中构造 lockstep Env（M26 §7.2）。

正式评测由 M27 作业驱动，Agent 在隔离容器中经线协议交换帧，不使用本函数。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["make"]


def make(run_spec: dict, *, run_id: str = "local-dev", seed: int = 0, worlds_root: str | Path = "worlds", events_root: str | Path = "data/d3/events",
         out_dir: str | Path | None = None, physics: str | None = None, gz: dict | None = None):
    try:
        from awr.agentsdk.env.core import BenchEnv
        from awr.agentsdk.env.spec import EnvSpec
    except ImportError as e:                                   # pragma: no cover
        raise RuntimeError("drone4d.bench.make needs the Drone4D repository environment (awr package)") from e
    spec = EnvSpec.from_run_spec(run_spec, run_id=run_id, seed=seed, worlds_root=Path(worlds_root), events_root=Path(events_root),
                                 out_dir=Path(out_dir) if out_dir else None, physics=physics, gz=gz)
    return BenchEnv(spec)
