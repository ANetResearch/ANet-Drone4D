"""SimClock：单一世界时钟（M08-FR-001、FR-002、FR-008、FR-009；D2：M08-FR-201、FR-220；M08 §6.8、§15.6.1；ADR-045、ADR-090）。

主时钟 tick 由时钟档给出（`clock_profile`：d1_250 为 4 ms，sandbox100 为 10 ms），`t_sim_ns = tick × tick_ns`（int64）；
模块常量 `TICK_NS` 与 `MAX_STEP_TICKS` 保留为 d1_250 的值（D1 代码与测试的兼容名），实例上以 `tick_ns`、`max_step_ticks`
为准。状态 STOPPED、PLAYING、PAUSED、STEPPING、LIVE，按 AWR-03 §5.2
第 6 条映射到 TimeState（0、1、2、3、9）。固定步长追帧：每轮至多 `max_batch = max(5, ⌈5·rate⌉)` 个 tick，超出时把墙钟锚点
前移（不跳步、不累积欠账）并置 `rtf_limited`。`paused_total_ns()` 为 PAUSED、STEPPING 期间按墙钟累加的单调量（M09 墙钟链路
判据据此冻结计时）。墙钟函数可注入（测试用假时钟）；墙钟只允许在 `awr/sim/runtime` 内读取（ADR-049 不变量 5）。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from awr.contracts.enums import TimeState
from awr.contracts.reasons import Reason

from . import clock_profile as CP

__all__ = ["MANUAL_UNLOCK_NS", "TICK_NS", "ClockReply", "SimClock"]

TICK_NS = CP.D1_250.tick_ns  # d1_250 的兼容名；实例的 tick 长度见 SimClock.tick_ns
RATES = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0)
MAX_STEP_TICKS = CP.D1_250.max_step_ticks
MANUAL_UNLOCK_NS = 2_000_000_000  # 手动锁解锁延迟【墙钟】（M08 §15.4.4）
MANUAL_FREE, MANUAL_LOCKED, MANUAL_UNLOCKING = 0, 1, 2


@dataclass
class ClockReply:
    code: int
    state: int
    rate: float
    tick: int
    detail: dict | None = None


class SimClock:
    TICK_NS = TICK_NS

    def __init__(self, *, wall_ns: Callable[[], int] = time.monotonic_ns, max_batch_base: int = 5,
                 max_speed: float = 10.0, pausable: bool = True, steppable: bool = True, live: bool = False,
                 profile: CP.ClockProfile | str | None = None) -> None:
        self.profile = CP.get(profile)
        self.tick_ns = int(self.profile.tick_ns)
        self.TICK_NS = self.tick_ns  # 实例覆盖类属性（D1 代码以 clock.TICK_NS 读取）
        self.max_step_ticks = int(self.profile.max_step_ticks)
        self._wall = wall_ns
        self.perf_ns = time.perf_counter_ns
        self.tick = 0
        self.state = int(TimeState.LIVE if live else TimeState.STOPPED)
        self.rate = 1.0
        self.rtf_limited = False
        self.max_batch_base = max_batch_base
        self.max_speed = max_speed
        self.pausable = pausable
        self.steppable = steppable
        self.wall0 = self._wall()
        self.tick0 = 0
        self._pause_start: int | None = None
        self._paused_total = 0
        self._step_left = 0
        self.catchup_saturated = 0
        self._limited_reported = False
        self._step_from = int(TimeState.PAUSED)
        self.step_done = False
        # 内部保持（ADR-068 第 4 条、ADR-073 第 7 条，剧本开局屏障）：reason -> (墙钟截止, 就绪判据)。保持期间 steps_due 返回 0
        # 并重锚（不欠账）；对外状态仍为 PLAYING（不是 PAUSED，不写输入日志），主循环照常迭代、写心跳、处理 bus 与慢任务
        self._holds: dict[str, tuple[int, Callable[[], bool] | None]] = {}
        self.hold_log: list[tuple[str, str, int, int]] = []  # (reason, ready | timeout, 保持开始 tick, 墙钟时长 ns)
        self._hold_t0: dict[str, tuple[int, int]] = {}
        # 设置期：主循环逐 tick 推进并在每个 tick 后检查保持（保持在置下它的 tick 之后立即生效，×1 与 ×10 相同）
        self.per_tick = False
        # 手动控制时钟锁（M08-FR-220，§15.6.1；只在 profile.manual_lock 的档位生效）：FREE → LOCKED → UNLOCKING → FREE
        self.manual = MANUAL_FREE
        self._manual_unlock_at = 0

    # ------------------------------------------------------------ 查询
    @property
    def t_ns(self) -> int:
        return self.tick * self.tick_ns

    def wall_mono_ns(self) -> int:
        return self._wall()

    def paused_total_ns(self) -> int:
        extra = (self._wall() - self._pause_start) if self._pause_start is not None else 0
        return self._paused_total + extra

    @property
    def rate_milli(self) -> int:
        return round(self.rate * 1000)

    @property
    def advancing(self) -> bool:
        return self.state in (TimeState.PLAYING, TimeState.LIVE)

    # ------------------------------------------------------------ 内部保持（剧本开局屏障）
    def hold(self, reason: str, *, timeout_s: float = 10.0, ready: Callable[[], bool] | None = None) -> None:
        """置内部保持：下一次 `steps_due` 起不再推进，直到 `ready()` 为真、`release(reason)` 或墙钟超过 timeout_s（超时放行，
        记入 `hold_log` 供调用方告警）。同名保持已存在时不改截止。"""
        if reason in self._holds:
            return
        now = self._wall()
        self._holds[reason] = (now + int(timeout_s * 1e9), ready)
        self._hold_t0[reason] = (self.tick, now)

    def release(self, reason: str, how: str = "release") -> None:
        if self._holds.pop(reason, None) is not None:
            t0 = self._hold_t0.pop(reason, (self.tick, self._wall()))
            self.hold_log.append((reason, how, t0[0], self._wall() - t0[1]))

    @property
    def held(self) -> bool:
        return bool(self._holds)

    def _holding(self, now: int) -> bool:
        for reason, (deadline, ready) in list(self._holds.items()):
            ok = False
            if ready is not None:
                try:
                    ok = bool(ready())
                except Exception:
                    ok = True  # 判据本身出错：放行，不让仿真停在开局
            if ok:
                self.release(reason, "ready")
            elif now >= deadline:
                self.release(reason, "timeout")
        return bool(self._holds)

    # ------------------------------------------------------------ 推进
    def _anchor(self, now: int) -> None:
        self.wall0, self.tick0 = now, self.tick

    def steps_due(self, now_ns: int | None = None) -> int:
        """本轮应执行的 tick 数（C10：到期 tick 超过 max_batch 时锚点前移并置 rtf_limited）。"""
        now = self._wall() if now_ns is None else now_ns
        if self.state == TimeState.STEPPING:
            n = min(self._step_left, max(self.max_batch_base, 50))
            return n
        if not self.advancing:
            return 0
        if self._holds:
            held = self._holding(now)
            self._anchor(now)  # 保持期间与刚放行时重锚：保持的墙钟时长不计入追帧（不欠账）
            if held:
                self.rtf_limited = False
                return 0
        tick_ns = self.tick_ns
        target = self.tick0 + int((now - self.wall0) * self.rate) // tick_ns
        due = target - self.tick
        max_batch = max(self.max_batch_base, math.ceil(self.max_batch_base * self.rate))
        if due > max_batch:
            self.wall0 = now - int((self.tick + max_batch - self.tick0) * tick_ns / self.rate)
            self.rtf_limited = True
            self.catchup_saturated += 1
            return max_batch
        self.rtf_limited = False
        return max(due, 0)

    def advanced(self, n: int) -> None:
        """执行 n 个 tick 后调用（STEPPING 计数，C05）。tick 本身由 pipeline 调用方逐个推进。"""
        if self.state == TimeState.STEPPING:
            self._step_left -= n
            if self._step_left <= 0:
                self._step_left = 0
                self.state = int(TimeState.PAUSED)
                self.step_done = True

    def next_deadline_ns(self) -> int:
        return self.wall0 + int((self.tick + 1 - self.tick0) * self.tick_ns / self.rate)

    def set_live(self, live: bool) -> bool:
        """C07/C08：世界中出现 `slaved_realtime` 或 `live` 后端时进入 LIVE（rate 锁 1、重锚）；最后一个此类机体移除后回到 PLAYING。
        返回状态是否改变。"""
        now = self._wall()
        if live and self.state != TimeState.LIVE:
            if self.state == TimeState.PAUSED and self._pause_start is not None:
                self._paused_total += now - self._pause_start
                self._pause_start = None
            self.rate = 1.0
            self.state = int(TimeState.LIVE)
            self._anchor(now)
            return True
        if not live and self.state == TimeState.LIVE:
            self.state = int(TimeState.PLAYING)
            self._anchor(now)
            return True
        return False

    def apply_caps(self, caps_clock: list) -> bool:
        """`caps.clock` 汇总：取所有在场后端中最严者（M08-FR-009）。"""
        live = any(getattr(c, "mode", "lockstep") in ("slaved_realtime", "live") for c in caps_clock)
        self.pausable = all(getattr(c, "pausable", True) for c in caps_clock) if caps_clock else True
        self.steppable = all(getattr(c, "steppable", True) for c in caps_clock) if caps_clock else True
        self.max_speed = min([float(getattr(c, "max_speed", 10.0)) for c in caps_clock] or [10.0])
        return self.set_live(live)

    # ------------------------------------------------------------ 操作（ctl/sim-core/clock，C01–C09）
    def apply(self, op: str, args: dict | None = None) -> ClockReply:
        a = args or {}
        now = self._wall()
        live = self.state == TimeState.LIVE
        if op == "play":
            if live:
                return self._reply(0)
            if self.state == TimeState.PAUSED and self._pause_start is not None:
                self._paused_total += now - self._pause_start
                self._pause_start = None
            self.state = int(TimeState.PLAYING)
            self._anchor(now)
            return self._reply(0)
        if op == "pause":
            if live or not self.pausable:
                return self._reply(int(Reason.CLOCK_CONSTRAINT))
            if self.state != TimeState.PAUSED:
                self._pause_start = now
            self.state = int(TimeState.PAUSED)
            return self._reply(0)
        if op == "step":
            ticks = int(a.get("ticks", 25))
            if live or not self.steppable:
                return self._reply(int(Reason.CLOCK_CONSTRAINT))
            if not 1 <= ticks <= self.max_step_ticks:
                return self._reply(int(Reason.PARAM_OUT_OF_RANGE))
            if self.state not in (TimeState.PAUSED, TimeState.STOPPED):
                return self._reply(int(Reason.STATE))
            if self._pause_start is None:
                self._pause_start = now
            self._step_from = int(self.state)
            self.state = int(TimeState.STEPPING)
            self._step_left = ticks
            return self._reply(0)
        if op == "speed":
            if live:
                return self._reply(int(Reason.CLOCK_CONSTRAINT))
            try:
                rate = float(a.get("rate"))
            except (TypeError, ValueError):
                return self._reply(int(Reason.PARAM_OUT_OF_RANGE))
            if rate not in RATES:
                return self._reply(int(Reason.PARAM_OUT_OF_RANGE))
            if rate > self.max_speed:
                return self._reply(int(Reason.CLOCK_CONSTRAINT))
            if self.manual_locked and rate != self.rate:
                return self._reply(int(Reason.CLOCK_CONSTRAINT), {"why": "MANUAL_CONTROL", "rate": self.rate})
            self.rate = rate
            self._anchor(now)
            return self._reply(0)
        if op == "reset":
            for r in list(self._holds):
                self.release(r, "reset")
            if self._pause_start is not None:
                self._paused_total += now - self._pause_start
            self.tick = 0
            self.state = int(TimeState.LIVE if live else TimeState.STOPPED)
            self._pause_start = None
            self.rtf_limited = False
            self._anchor(now)
            return self._reply(0)
        return self._reply(int(Reason.BAD_REQUEST))

    def _reply(self, code: int, detail: dict | None = None) -> ClockReply:
        return ClockReply(code, self.state, self.rate, self.tick, detail)

    # ------------------------------------------------------------ 手动控制时钟锁（M08-FR-220，§15.6.1）
    @property
    def manual_locked(self) -> bool:
        """LOCKED 或 UNLOCKING：`sim/speed{rate ≠ 当前}` 返回 117（MANUAL_CONTROL）。"""
        return self.manual != MANUAL_FREE

    def manual_enter(self) -> bool:
        """某 slot 进入 OPERATOR 的 VELOCITY（调用 running 或收到 setpoint）：倍率置 1 并锁定（档位不支持时无操作）。
        返回是否由 FREE 进入 LOCKED（调用方据此发 `sim.clock{reason: manual_lock}`）。"""
        if not self.profile.manual_lock:
            return False
        if self.manual == MANUAL_LOCKED:
            return False
        was_free = self.manual == MANUAL_FREE
        self.manual = MANUAL_LOCKED
        if self.rate != 1.0 and self.state != TimeState.LIVE:
            self.rate = 1.0
            self._anchor(self._wall())
        return was_free

    def manual_poll(self, any_velocity: bool) -> str | None:
        """每轮主循环调用：`any_velocity` 为是否仍有 slot 处于 OPERATOR 的 VELOCITY（或 1 s【墙钟】内收到过 setpoint）。
        返回 `manual_lock`、`manual_unlock` 或 None（状态转移，调用方发 `sim.clock` 事件）。"""
        if not self.profile.manual_lock:
            return None
        now = self._wall()
        if any_velocity:
            return "manual_lock" if self.manual_enter() else None
        if self.manual == MANUAL_LOCKED:
            self.manual = MANUAL_UNLOCKING
            self._manual_unlock_at = now + MANUAL_UNLOCK_NS
        elif self.manual == MANUAL_UNLOCKING and now >= self._manual_unlock_at:
            self.manual = MANUAL_FREE
            return "manual_unlock"
        return None
