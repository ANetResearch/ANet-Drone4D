"""门 3 验证器注册表（M23 §6.9、§15.3，M23-FR-070）。

三类条目，均以装饰器登记在本包的子模块中，`collect()` 按模块名次序导入 `awr.sim.events.verify` 下全部子模块（`base`、
`registry`、`engine` 与下划线开头者除外），因此新增事件类型只需在本目录新增文件（E06 `rendezvous.py`、E07 `reid.py`、
E08 `search.py`、E09 `vehicle_track.py`）：

- 检测器 `@detector(type, defaults=...)`：按注释类型（`annotations[].type`，§6.9.1）从帧数据计算全部候选事件，签名
  `fn(ctx, params) -> list[Detection]`；`params` 为缺省参数与注释 `params` 的合并结果；
- 一致性检查 `@check(code)`：§6.9.4 的逐项检查，签名 `fn(ctx, params) -> CheckResult`；
- 类型验证器 `@validator(event_type)`：§15.3 第 3 列，签名 `fn(ctx, gate3) -> None`，可向 `gate3` 追加事件、错误、告警与
  `extra`（写入编译报告）。

同一键重复登记抛 `RuntimeError`（避免并行新增的文件静默覆盖）；登记次序不影响结果（门 3 按类型名与代码排序执行）。
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["CHECKS", "DETECTORS", "VALIDATORS", "Check", "Detector", "check", "collect", "detector", "validator"]

_CORE = frozenset({"base", "registry", "engine"})


@dataclass(frozen=True)
class Detector:
    type: str
    fn: Callable
    defaults: dict = field(default_factory=dict)
    instant: bool = False                          # 瞬时事件（区间 [t, t]）
    anchor_param: str | None = None                # 注释参与者中的锚点 id 归入的参数名（anchor 或 zone）
    needs: tuple[str, ...] = ()                    # 依赖的上下文资源（mesh、stage）；缺失时不执行，注释记为未验证
    phase: str = "P0a"


@dataclass(frozen=True)
class Check:
    code: str
    fn: Callable
    defaults: dict = field(default_factory=dict)


DETECTORS: dict[str, Detector] = {}
CHECKS: dict[str, Check] = {}
VALIDATORS: dict[str, Callable] = {}
_collected = False


def _put(table: dict, key: str, val, what: str) -> None:
    if key in table:
        raise RuntimeError(f"duplicate gate-3 {what} registration: {key}")
    table[key] = val


def detector(type_: str, *, defaults: dict | None = None, instant: bool = False, anchor_param: str | None = None,
             needs: tuple[str, ...] = (), phase: str = "P0a"):
    def deco(fn: Callable) -> Callable:
        _put(DETECTORS, type_, Detector(type_, fn, dict(defaults or {}), instant, anchor_param, tuple(needs), phase), "detector")
        return fn
    return deco


def check(code: str, *, defaults: dict | None = None):
    def deco(fn: Callable) -> Callable:
        _put(CHECKS, code, Check(code, fn, dict(defaults or {})), "check")
        return fn
    return deco


def validator(event_type: str):
    def deco(fn: Callable) -> Callable:
        _put(VALIDATORS, event_type, fn, "validator")
        return fn
    return deco


def collect() -> None:
    """导入本包全部验证器子模块（幂等）。"""
    global _collected
    if _collected:
        return
    import awr.sim.events.verify as pkg

    for m in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if m.name in _CORE or m.name.startswith("_"):
            continue
        importlib.import_module(f"{pkg.__name__}.{m.name}")
    _collected = True
