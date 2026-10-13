"""等级迟滞与明确捕获状态机（M19-FR-013、FR-014；ADR-100；AWR-04 §6.3 第 6 步、§6.4；M18-M19 PRD §6.10）。

对每个（机体，成像传感器，识别物）维护当前等级 L 与捕获态；评估间隔为该机的感知周期（5 Hz，0.2 s）。

| 状态 | 事件 | 守卫 | 动作 |
|---|---|---|---|
| L | 测得 L_m > L | 持续 ≥ 1.0 s | L ← 窗口内 L_m 的最小值，发 level |
| L | 测得 L_m < L | 持续 ≥ 2.0 s | L ← 窗口内 L_m 的最大值，发 level |
| L | L_m = L 或 deferred | — | 清零（相等）或冻结（deferred）升降计时 |
| IDLE → ARMING → CAPTURED（持续 1.0 s，发 capture）→ HOLDING（条件不满足）→ IDLE（持续 2.0 s，发 capture_lost） |

捕获条件 = 成像传感器 ∧ 视锥内 ∧ k_los ≥ 2/3 ∧ N_eff ≥ N_req；视线延期（deferred）的对冻结全部计时（不增不减：计时起点
后移一个评估间隔）。时刻均为仿真秒；纯 Python，无随机数。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .params import LEVELS

__all__ = ["ARMING", "CAPTURED", "CAP_NAMES", "HOLDING", "IDLE", "PairState", "Transition", "step"]

IDLE, ARMING, CAPTURED, HOLDING = 0, 1, 2, 3
CAP_NAMES = ("IDLE", "ARMING", "CAPTURED", "HOLDING")
_CAP = LEVELS["capture"]
T_UP = float(_CAP["t_up_s"])
T_DOWN = float(_CAP["t_down_s"])
T_HOLD = float(_CAP["t_hold_s"])
T_LOST = float(_CAP["t_lost_s"])
EPS = 1e-6


@dataclass
class PairState:
    level: int = 0
    up_t0: float | None = None
    up_min: int = 0
    down_t0: float | None = None
    down_max: int = 0
    cap: int = IDLE
    t_ok: float = 0.0          # ARMING 起点
    t_bad: float = 0.0         # HOLDING 起点
    since: float = 0.0         # 进入 CAPTURED 的时刻
    t_last_ok: float | None = None   # 最近一次满足捕获条件的时刻（capture_lost.t_last_ok_ns）
    reason: str | None = None
    t_eval: float | None = None      # 上次评估时刻（deferred 冻结用）
    n_req: float = 14.0
    level_req: str = "R"
    last: dict = field(default_factory=dict)   # 最近一次测量（帧与事件字段）

    @property
    def active(self) -> bool:
        return self.level > 0 or self.cap != IDLE or self.up_t0 is not None

    def hold_s(self, t: float) -> float:
        if self.cap == ARMING:
            return max(0.0, t - self.t_ok)
        if self.cap == CAPTURED:
            return max(0.0, t - self.since)
        if self.cap == HOLDING:
            return max(0.0, t - self.t_bad)
        return 0.0


@dataclass(frozen=True)
class Transition:
    kind: str                  # level | capture | capture_lost
    level_from: int = 0
    level_to: int = 0
    reason: str | None = None
    captured_s: float = 0.0
    t_last_ok: float | None = None


def step(ps: PairState, t: float, level_m: int, cond: bool, *, deferred: bool = False, reason: str | None = None) -> list[Transition]:
    """一次评估：测得等级 `level_m`、捕获条件 `cond`（视线延期时 `deferred`，冻结计时）；返回本次发生的转移。"""
    out: list[Transition] = []
    if deferred:
        if ps.t_eval is not None:
            dt = max(0.0, t - ps.t_eval)
            if ps.up_t0 is not None:
                ps.up_t0 += dt
            if ps.down_t0 is not None:
                ps.down_t0 += dt
            if ps.cap == ARMING:
                ps.t_ok += dt
            elif ps.cap == HOLDING:
                ps.t_bad += dt
        ps.t_eval = t
        return out
    ps.t_eval = t
    # ---- 等级迟滞
    if level_m > ps.level:
        ps.down_t0 = None
        if ps.up_t0 is None:
            ps.up_t0, ps.up_min = t, level_m
        else:
            ps.up_min = min(ps.up_min, level_m)
        if t - ps.up_t0 >= T_UP - EPS:
            out.append(Transition("level", ps.level, ps.up_min))
            ps.level, ps.up_t0 = ps.up_min, None
    elif level_m < ps.level:
        ps.up_t0 = None
        if ps.down_t0 is None:
            ps.down_t0, ps.down_max = t, level_m
        else:
            ps.down_max = max(ps.down_max, level_m)
        if t - ps.down_t0 >= T_DOWN - EPS:
            out.append(Transition("level", ps.level, ps.down_max))
            ps.level, ps.down_t0 = ps.down_max, None
    else:
        ps.up_t0 = ps.down_t0 = None
    # ---- 明确捕获
    if cond:
        ps.t_last_ok = t
    if ps.cap == IDLE:
        if cond:
            ps.cap, ps.t_ok = ARMING, t
    elif ps.cap == ARMING:
        if not cond:
            ps.cap = IDLE
        elif t - ps.t_ok >= T_HOLD - EPS:
            ps.cap, ps.since = CAPTURED, t
            out.append(Transition("capture", ps.level, ps.level))
    elif ps.cap == CAPTURED:
        if not cond:
            ps.cap, ps.t_bad, ps.reason = HOLDING, t, reason
    elif ps.cap == HOLDING:
        if cond:
            ps.cap, ps.reason = CAPTURED, None
        elif t - ps.t_bad >= T_LOST - EPS:
            out.append(Transition("capture_lost", ps.level, ps.level, ps.reason or reason, max(0.0, ps.t_bad - ps.since), ps.t_last_ok))
            ps.cap, ps.reason = IDLE, None
    return out
