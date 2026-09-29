"""M10 提供的剧本度量（M10-FR-064、FR-067、FR-068；AWR-16 §12.3 指标登记表；AWR-12 §7.1.3）。

经 M08 度量注册表 `register_metric(name, fn, owner="M10")` 登记，剧本导演以 `metric(name, **kw)` 读取。度量尚未产出时抛
`MetricMissing`（LookupError），导演判该谓词为假（失败即关闭）。`link_quality_min` 在 D1 中恒为缺失（V0.2 定义链路模型）。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime import M10Runtime

__all__ = ["MetricMissing", "provided"]


class MetricMissing(LookupError):
    pass


def _mids(rt: M10Runtime, kw: dict) -> list[str]:
    ids = kw.get("mission_ids")
    if ids is None and kw.get("mission_id") is not None:
        ids = [kw["mission_id"]]
    if ids is None:
        return list(rt.missions.order)
    return [str(x) for x in ids]


def provided(rt: M10Runtime) -> dict[str, Callable[..., float]]:
    def missions_done(**kw: Any) -> float:
        mids = _mids(rt, kw)
        if not mids:
            raise MetricMissing("no missions")
        ms = [rt.missions.missions.get(m) for m in mids]
        if any(m is None for m in ms):
            raise MetricMissing("unknown mission")
        return 1.0 if all(m.state == "DONE" and not m.incomplete for m in ms) else 0.0

    def mission_progress(**kw: Any) -> float:
        m = rt.missions.missions.get(str(kw.get("mission_id")))
        if m is None:
            raise MetricMissing("unknown mission")
        return rt.missions.progress(m) / 100.0

    def facade_coverage(**kw: Any) -> float:
        cov = rt.coverage
        v = cov.facade_ratio(_mids(rt, kw)) if cov is not None else None
        if v is None:
            raise MetricMissing("facade grid not started")
        return float(v)

    def area_coverage(**kw: Any) -> float:
        cov = rt.coverage
        v = cov.area_ratio(_mids(rt, kw)) if cov is not None else None
        if v is None:
            raise MetricMissing("coverage grid not started")
        return float(v)

    def formation_err_rms_m(**kw: Any) -> float:
        st = rt.coverage.form.get(str(kw.get("mission_id"))) if rt.coverage is not None else None
        if not st or st[1] <= 0:
            raise MetricMissing("no formation samples")
        return math.sqrt(st[0] / st[1])

    def agl_min_m(**kw: Any) -> float:
        st = rt.coverage.agl.get(str(kw.get("mission_id"))) if rt.coverage is not None else None
        if not st or st[2] <= 0:
            raise MetricMissing("no terrain-follow samples")
        return float(st[0])

    def agl_rms_err_m(**kw: Any) -> float:
        st = rt.coverage.agl.get(str(kw.get("mission_id"))) if rt.coverage is not None else None
        if not st or st[2] <= 0:
            raise MetricMissing("no terrain-follow samples")
        return math.sqrt(st[1] / st[2])

    def link_quality_min(**kw: Any) -> float:
        raise MetricMissing("link model is V0.2")

    return {"missions_done": missions_done, "mission_progress": mission_progress, "facade_coverage": facade_coverage,
            "area_coverage": area_coverage, "formation_err_rms_m": formation_err_rms_m, "agl_min_m": agl_min_m,
            "agl_rms_err_m": agl_rms_err_m, "link_quality_min": link_quality_min}
