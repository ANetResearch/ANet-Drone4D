"""SafetyEvent 代码索引（M09 §6.13；FR-100、FR-102）。

代码目录的唯一真源是契约 `packages/contracts/rt/safety_codes.json`（生成物 `awr.contracts.safety_codes`，M00 生成器）；
本模块只在其上建立 u16 下标（SoA 的 `reason` 字段）与分级映射，另追加若干"转移原因"伪代码（`SYS.*`、`OP.*`），
它们只出现在 `uav.state` 事件的 `reason` 与 safety 行 `fsm.reason` 中，不作为 SafetyEvent 发布。

线上 level（17 §6.12；FR-102）：info → 1、warn → 2、action → 2、critical → 3。
"""

from __future__ import annotations

from awr.contracts.safety_codes import SAFETY_CODES, SafetyCode

__all__ = [
    "CLS_ACTION",
    "CLS_CRITICAL",
    "CLS_INFO",
    "CLS_WARN",
    "CODES",
    "CODE_INDEX",
    "LEVEL",
    "PSEUDO",
    "code_of",
    "idx",
    "info",
    "is_event",
    "level_of_cls",
]

CLS_INFO, CLS_WARN, CLS_ACTION, CLS_CRITICAL = "info", "warn", "action", "critical"
_CLS_LEVEL = {CLS_INFO: 1, CLS_WARN: 2, CLS_ACTION: 2, CLS_CRITICAL: 3}

# 转移原因伪代码（不发布为 SafetyEvent）
PSEUDO: tuple[str, ...] = (
    "NONE",
    "SYS.SPAWN", "SYS.LIFECYCLE", "SYS.PREFLIGHT_OK", "SYS.SPOOLUP", "SYS.CLIMB", "SYS.ARRIVED", "SYS.PHASE",
    "SYS.TOUCHDOWN", "SYS.LANDED_DISARM", "SYS.RTL_PHASE", "SYS.LOC_LOST_LAND",
    "OP.TAKEOFF", "OP.ARM", "OP.DISARM", "OP.GOTO", "OP.FOLLOW_PATH", "OP.ORBIT", "OP.VELOCITY", "OP.HOVER", "OP.PAUSE",
    "OP.LAND", "OP.RTL", "OP.RESUME", "OP.CANCEL", "OP.TRAJ", "OP.OFFBOARD", "OP.KILL", "OP.ESCALATE",
)

CODES: tuple[str, ...] = PSEUDO + tuple(SAFETY_CODES)
CODE_INDEX: dict[str, int] = {c: i for i, c in enumerate(CODES)}
LEVEL: tuple[int, ...] = tuple(_CLS_LEVEL[SAFETY_CODES[c].cls] if c in SAFETY_CODES else 0 for c in CODES)


def level_of_cls(cls: str) -> int:
    return _CLS_LEVEL[cls]


def idx(code: str) -> int:
    """代码 -> u16 下标（未知代码抛 KeyError：代码必须先登记到契约）。"""
    return CODE_INDEX[code]


def code_of(i: int) -> str:
    return CODES[int(i)] if 0 <= int(i) < len(CODES) else "NONE"


def is_event(i: int) -> bool:
    return CODES[int(i)] in SAFETY_CODES


def info(i: int) -> SafetyCode:
    return SAFETY_CODES[CODES[int(i)]]
