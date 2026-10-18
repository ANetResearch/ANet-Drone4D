"""门 3：语义事件验证器（M23 §6.9、§6.10.1、§15.3；D3-AC-06）。

公共入口 `verify_event(plan, frames, table, ...) -> Gate3`；检测器、一致性检查与类型验证器经 `registry` 登记，本包子模块在首次
调用时按模块名次序自动导入（新增事件类型只需在本目录新增文件，见 `registry`）。
"""

from __future__ import annotations

from awr.sim.events.verify.base import CheckResult, Context, Detection
from awr.sim.events.verify.engine import INCOMPLETE_KINDS, SALIENCE_MIN, Gate3, load_quality, verify_event
from awr.sim.events.verify.registry import CHECKS, DETECTORS, VALIDATORS, check, collect, detector, validator

__all__ = ["CHECKS", "DETECTORS", "INCOMPLETE_KINDS", "SALIENCE_MIN", "VALIDATORS", "CheckResult", "Context", "Detection", "Gate3", "check",
           "collect", "detector", "load_quality", "validator", "verify_event"]
