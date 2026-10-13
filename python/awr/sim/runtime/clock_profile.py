"""时钟档（M08-FR-201；ADR-090；AWR-04 §4.5；M08 §15.3.1）。

`d1_250`：D1 主时钟 250 Hz（4 ms），L1 125 Hz（every 2），共享展示与全部 D1 剧本；`sandbox100`：D2 沙盒主时钟 100 Hz（10 ms），
L1 every 1。档位在进程生命周期内不可变：sim-core 启动时按 `AWR_CLOCK_PROFILE`（sandbox-pool 拉起沙盒 sim-core 时置
`sandbox100`；缺省 `d1_250`）选定一次，之后 `current()` 返回同一对象。依赖 tick 长度的常量一律经本模块读取（`tick_ns`、
`tick_hz`、`dt_tick`），stage 的 `(every, phase)` 由 `fleet/stages/rates.py` 的按档调度表给出（M08-FR-202）。

本模块不读墙钟；`use()` 只供测试在一段代码内切换档位。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

__all__ = ["D1_250", "PROFILES", "SANDBOX100", "ClockProfile", "current", "from_env", "get", "set_current", "use"]

ProfileName = Literal["d1_250", "sandbox100"]


@dataclass(frozen=True)
class ClockProfile:
    """一个时钟档的全部常量（M08 §15.3.1；数值见 AWR-04 §4.5 与 M08 §15.4）。"""

    name: str
    tick_hz: int                 # 主时钟频率
    tick_ns: int                 # 4 000 000 | 10 000 000
    max_step_ticks: int          # sim/step 单次上限：仿真时长 10 s（2500 | 1000）
    budget_total_core: float     # 构建期 Σbudget 上限（d1_250 只计 stage，sandbox100 含主循环项，M08-FR-203）
    checkpoint_period_s: float   # checkpoint 周期【仿真】
    l1_every: int                # 多旋翼 L1 物理与控制的 every（125 Hz | 100 Hz）
    tap_every: int               # StateRing 发布的 every（125 Hz | 50 Hz）
    tap_idle_opt: bool           # 空载与无读者时的 tap 降频（M08-FR-223 ②③；只在沙盒档启用，D1 行为不变）
    manual_lock: bool            # 手动控制时钟锁（M08-FR-220；d1_250 保持 ADR-045 第 ④ 条：倍率 ≠ 1 时 velocity 117）
    loop_in_budget: bool         # 构建期预算是否含主循环项（drain、flush、慢任务）
    targets_period_ns: int = 100_000_000  # state/sim-core/targets 10 Hz【仿真】
    pub_min_wall_ns: int = 50_000_000     # 快进时识别物发布的墙钟最小间隔（≥ 50 ms）
    # ×1 下奇偶 tick 成对推进（主循环在偶数 tick 到期时唤醒并连推两个 tick，ADR-070）：d1_250 为 L1 与 tap 所在的偶数 tick
    # （125 Hz 唤醒）；sandbox100 为 tap（StateRing 50 Hz）所在的偶数 tick（50 Hz 唤醒；D2-AC-08：每次唤醒的固定开销与冷缓存
    # 减半，sim-core 实测约 −8%；奇数 tick 的 stage 晚 10 ms【墙钟】执行，发布时刻与仿真结果不变）
    pair_wake: bool = True
    # FleetState 容量（槽数；0 为 D1 的 1024）：sandbox100 会话 ≤ 12 架（AWR-04 §4.4.3 limits.vehicles），容量 64 时机群状态与各插件
    # 状态块的整容量遍历与缓存占用约为 1024 的 1/16（D2-AC-08）；`AWR_FLEET_CAPACITY` 可覆盖（fleet_ladder 的大机群档）
    fleet_capacity: int = 0

    @property
    def dt_tick(self) -> float:
        """一个主时钟 tick 的秒数（0.004 | 0.01）。"""
        return 1.0 / self.tick_hz

    def ticks_for(self, seconds: float) -> int:
        """把仿真秒数换算为 tick 数（四舍五入）。"""
        return round(seconds * 1e9 / self.tick_ns)

    def every_for_hz(self, hz: float) -> int:
        """按频率声明 stage 的 every（`tick_hz / hz` 必须为整数，否则 ValueError；M08-FR-202）。"""
        if hz <= 0:
            raise ValueError(f"hz={hz} 必须 > 0")
        q = self.tick_hz / float(hz)
        e = round(q)
        if e < 1 or abs(q - e) > 1e-9:
            raise ValueError(f"hz={hz} 不整除 {self.name} 的 tick_hz={self.tick_hz}（M08-FR-202）")
        return int(e)

    def to_json(self) -> dict:
        return {"name": self.name, "tick_hz": self.tick_hz, "tick_ns": self.tick_ns, "max_step_ticks": self.max_step_ticks,
                "budget_total_core": self.budget_total_core, "checkpoint_period_s": self.checkpoint_period_s}


D1_250 = ClockProfile("d1_250", 250, 4_000_000, 2500, 0.40, 1.0, 2, 2, False, False, False)
SANDBOX100 = ClockProfile("sandbox100", 100, 10_000_000, 1000, 0.16, 5.0, 1, 2, True, True, True, fleet_capacity=64)
PROFILES: dict[str, ClockProfile] = {p.name: p for p in (D1_250, SANDBOX100)}

_CURRENT: ClockProfile | None = None


def get(name: str | ClockProfile | None) -> ClockProfile:
    """按名取档位；None 取 `current()`；未知名 ValueError。"""
    if name is None:
        return current()
    if isinstance(name, ClockProfile):
        return name
    p = PROFILES.get(str(name).strip())
    if p is None:
        raise ValueError(f"未知时钟档 {name!r}（可选 {sorted(PROFILES)}）")
    return p


def from_env(env: Mapping[str, str] | None = None) -> ClockProfile:
    """`AWR_CLOCK_PROFILE`（缺省 d1_250）。"""
    e = os.environ if env is None else env
    return get(e.get("AWR_CLOCK_PROFILE") or "d1_250")


def current() -> ClockProfile:
    """进程内的当前档位（首次访问时按环境变量确定）。"""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = from_env()
    return _CURRENT


def set_current(p: str | ClockProfile) -> ClockProfile:
    """设置进程档位（sim-core 启动时调用一次）；返回此前的档位。"""
    global _CURRENT
    prev = current()
    _CURRENT = get(p)
    return prev


@contextlib.contextmanager
def use(p: str | ClockProfile) -> Iterator[ClockProfile]:
    """测试用：在一段代码内使用指定档位，结束后恢复。"""
    prev = set_current(p)
    try:
        yield current()
    finally:
        set_current(prev)
