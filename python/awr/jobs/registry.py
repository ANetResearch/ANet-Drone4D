"""任务注册表（M03 §6.14；扩展点：`@register_job`，M01 注册 "recon"）。

`emits_events = True` 的任务自行经 `JobContext.event_sink` 发 `job.state`、`job.progress`、`job.log`（M01 的 recon 任务，
M01 §6.7.3）；其余任务（world_build）由 job-worker 与 `JobContext` 代发。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobKind:
    name: str
    stages: tuple[str, ...]
    params_schema: dict
    runner: Callable[[Any, dict], Any]
    emits_events: bool = False


_REGISTRY: dict[str, JobKind] = {}


def register_job(name: str, stages: tuple[str, ...], params_schema: dict | None = None, *, emits_events: bool = False):
    """装饰器：`@register_job("world_build", STAGES, schema)` 注册 `runner(ctx, params) -> output`。"""

    def deco(fn):
        if name in _REGISTRY and _REGISTRY[name].runner is not fn:
            raise ValueError(f"job kind {name!r} already registered")
        _REGISTRY[name] = JobKind(name, tuple(stages), params_schema or {"type": "object"}, fn, bool(emits_events))
        return fn

    return deco


def get_job(name: str) -> JobKind:
    try:
        return _REGISTRY[name]
    except KeyError as e:
        raise KeyError(f"unknown job kind {name!r}; registered: {sorted(_REGISTRY)}") from e


def job_kinds() -> list[str]:
    return sorted(_REGISTRY)
