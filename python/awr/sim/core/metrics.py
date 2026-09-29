"""剧本度量注册表（M08-FR-088；M08 §7.1.1；AWR-03 §4.3）。

M09、M10、M13、M14 以 `register_metric(name, fn, owner=...)` 登记剧本度量（`min_separation_m`、`guard_events`、
`pos_err_max_m` 等），M10 剧本导演以 `metric(name, **kw)` 读取。名称全局唯一，重名登记失败；未登记名称抛 KeyError。
度量函数在 sim-core 主线程调用，应为 O(1) 或读取已缓存的聚合值。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass

__all__ = ["MetricSpec", "isolated_metrics", "list_metrics", "metric", "register_metric", "unregister_metric"]


@dataclass(frozen=True)
class MetricSpec:
    name: str
    fn: Callable[..., float]
    owner: str | None


_METRICS: dict[str, MetricSpec] = {}


def register_metric(name: str, fn: Callable[..., float], *, owner: str | None = None) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("度量名必须是非空字符串")
    if name in _METRICS:
        raise ValueError(f"度量重名：{name!r}（已由 {_METRICS[name].owner} 登记）")
    if not callable(fn):
        raise TypeError(f"度量 {name!r} 的 fn 不可调用")
    _METRICS[name] = MetricSpec(name, fn, owner)


def metric(name: str, **kw) -> float:
    try:
        spec = _METRICS[name]
    except KeyError:
        raise KeyError(f"未登记的度量：{name!r}") from None
    return float(spec.fn(**kw))


def unregister_metric(name: str) -> None:
    _METRICS.pop(name, None)


def list_metrics() -> list[MetricSpec]:
    return sorted(_METRICS.values(), key=lambda m: m.name)


@contextlib.contextmanager
def isolated_metrics() -> Iterator[dict[str, MetricSpec]]:
    """测试用：临时的空度量表。"""
    global _METRICS
    saved = _METRICS
    _METRICS = {}
    try:
        yield _METRICS
    finally:
        _METRICS = saved
