"""测试用规划作业插件（`AWR_PLAN_PLUGINS=...,plan_test_kinds`；worker 进程经 PYTHONPATH 导入同一模块）。

| kind | 行为 |
|---|---|
| test.echo | 回显 `payload.x`（结果哈希含 x） |
| test.sleep | 睡眠 `payload.sleep_s` 后返回（预算 100 ms，用于超时与强杀） |
| test.long | 同 test.sleep，预算 3000 ms（长 query：超过总线 5 s 未回复清扫） |
| test.big | 返回 `payload.n` 个 float64（> 1 MB 时服务端外置） |
| test.quota | `validate_input` 按 `payload.measured` 与 `PLAN_QUOTA` 比较 |
| test.crash | 进程直接退出（`payload.once` 为标记文件：不存在时创建并退出，第二次正常返回） |
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np

from awr.sim.planning.jobs import PRIO_INTERACTIVE, PlanResult, quota_check, register_plan_kind, result_digest


def _ok(req, extra: dict) -> PlanResult:
    return PlanResult(req.job_id, "ok", (), None, {}, None, None, result_digest((), None, extra), 0, extra)


def _echo(req, world) -> PlanResult:
    return _ok(req, {"echo": req.payload.get("x"), "world": None if world is None else world.world_id})


def _sleep(req, world) -> PlanResult:
    time.sleep(float(req.payload.get("sleep_s", 0.0)))
    return _ok(req, {"slept": float(req.payload.get("sleep_s", 0.0))})


def _big(req, world) -> PlanResult:
    n = int(req.payload.get("n", 1000))
    return _ok(req, {"blob": np.arange(n, dtype=np.float64)})


def _quota(req, world) -> PlanResult:
    return _ok(req, {"measured": dict(req.payload.get("measured") or {})})


def _validate_quota(payload: dict):
    return quota_check(payload.get("measured") or {})


def _crash(req, world) -> PlanResult:
    mark = req.payload.get("once")
    if mark and Path(mark).exists():
        return _ok(req, {"crashed": False})
    if mark:
        Path(mark).write_text("1")
    os._exit(9)


register_plan_kind("test.echo", _echo, owner="test", budget_ms=200, priority_default=PRIO_INTERACTIVE)
register_plan_kind("test.sleep", _sleep, owner="test", budget_ms=100)
register_plan_kind("test.long", _sleep, owner="test", budget_ms=3000)
register_plan_kind("test.big", _big, owner="test", budget_ms=2000)
register_plan_kind("test.quota", _quota, owner="test", budget_ms=200, validate_input=_validate_quota)
register_plan_kind("test.crash", _crash, owner="test", budget_ms=500)
