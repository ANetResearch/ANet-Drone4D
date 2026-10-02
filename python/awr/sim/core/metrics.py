"""剧本度量注册表（M08-FR-088；M08 §7.1.1；AWR-03 §4.3）。

M09、M10、M13、M14 以 `register_metric(name, fn, owner=...)` 登记剧本度量（`min_separation_m`、`guard_events`、
`pos_err_max_m` 等），M10 剧本导演以 `metric(name, **kw)` 读取。名称全局唯一，重名登记失败；未登记名称抛 KeyError。
度量函数在 sim-core 主线程调用，应为 O(1) 或读取已缓存的聚合值。

外部度量（M14-FR-043；M14-to-M08 第 3 条；ADR-058）：进程外生产者（agent-runtime）经 `ctl/sim-core/cmd` 的
`scenario/metric{name, args, value}` 写入的度量先以 `register_external_metric(name, owner=, keys=)` 声明；写入在
apply_tick 生效（CommandEngine），按 `args` 的规范 JSON 分键保存最新值，读取时键不存在抛 LookupError（剧本谓词为假）。
剧本重置时清空；checkpoint 经 `external_snapshot()` / `external_restore()` 保存与恢复。
"""

from __future__ import annotations

import contextlib
import json
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

__all__ = ["MetricSpec", "clear_external", "external_restore", "external_snapshot", "is_external", "isolated_metrics",
           "list_metrics", "metric", "register_external_metric", "register_metric", "set_external", "unregister_metric"]


@dataclass(frozen=True)
class MetricSpec:
    name: str
    fn: Callable[..., float]
    owner: str | None


_METRICS: dict[str, MetricSpec] = {}
_EXTERNAL: dict[str, tuple[str, ...]] = {}  # name -> 允许的 args 键
_VALUES: dict[str, dict[str, float]] = {}  # name -> {规范 JSON 键: 最新值}


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
    _EXTERNAL.pop(name, None)
    _VALUES.pop(name, None)


def _key(name: str, kw: dict[str, Any]) -> str:
    allowed = _EXTERNAL[name]
    bad = [k for k in kw if k not in allowed]
    if bad:
        raise ValueError(f"度量 {name!r} 不接受参数 {bad}（允许 {list(allowed)}）")
    norm = {k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v) for k, v in kw.items()}
    return json.dumps(norm, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def register_external_metric(name: str, *, owner: str | None = None, keys: tuple[str, ...] = ()) -> None:
    """声明一个由进程外生产者写入的剧本度量（例如 M14 的 `target_confidence{target_id}`、`t_conf_s{target_id, threshold}`）。"""

    def read(**kw: Any) -> float:
        try:
            return _VALUES[name][_key(name, kw)]
        except KeyError:
            raise LookupError(f"外部度量 {name!r}{kw} 尚无取值") from None

    register_metric(name, read, owner=owner)
    _EXTERNAL[name] = tuple(keys)
    _VALUES.setdefault(name, {})


def is_external(name: str) -> bool:
    return name in _EXTERNAL


def set_external(name: str, kw: dict[str, Any], value: float) -> None:
    """写入外部度量的最新值；未声明的名称 KeyError，参数键不符或值非有限数 ValueError。"""
    if name not in _EXTERNAL:
        raise KeyError(f"未声明的外部度量：{name!r}")
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"外部度量 {name!r} 的值不是有限数")
    _VALUES.setdefault(name, {})[_key(name, dict(kw))] = v


def clear_external() -> None:
    for d in _VALUES.values():
        d.clear()


def external_snapshot() -> dict[str, dict[str, float]]:
    return {k: dict(v) for k, v in _VALUES.items() if v}


def external_restore(snap: dict[str, dict[str, float]] | None) -> None:
    clear_external()
    for k, v in (snap or {}).items():
        if k in _EXTERNAL:
            _VALUES[k] = {str(kk): float(vv) for kk, vv in v.items()}


def list_metrics() -> list[MetricSpec]:
    return sorted(_METRICS.values(), key=lambda m: m.name)


@contextlib.contextmanager
def isolated_metrics() -> Iterator[dict[str, MetricSpec]]:
    """测试用：临时的空度量表。"""
    global _METRICS, _EXTERNAL, _VALUES
    saved = _METRICS, _EXTERNAL, _VALUES
    _METRICS, _EXTERNAL, _VALUES = {}, {}, {}
    try:
        yield _METRICS
    finally:
        _METRICS, _EXTERNAL, _VALUES = saved
