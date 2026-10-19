"""预滚窗口 `[−t_pre, 0)`（M26-FR-004、§6.3 PREROLL 状态；AWR-05 §5.4.6 初始化条款；M26-AC-022；D3-AC-13）。

- 时间轴：物理档从打开时刻起以自身时钟计时（PoseHistory 首样本为档内 0）；预滚后 Env 一律使用事件时间，档内时间 = 事件时间 +
  `t_pre`。`EventTimeTier` 是物理档的事件时间视图：PoseHistory 读取、接触事件与 TickResult 的时刻减去 `t_pre`，指令的
  `apply_tick` 加上预滚 tick 数，其余属性（`tracker`、`caps()`、`cp`、`n_steps` 等）透传。因此 Env 主循环、L2 采样与 Agent
  看到的时刻不变（k = 0 观测时刻为 0），事件零点之前的物理样本在 PoseHistory 中以负时刻出现。
- 窗口：地面起飞（`FleetEntry.airborne = False`）为静止窗口，不下发指令；空中出生为悬停保持窗口，在档内 tick 0 下发 `hover`。
  预滚期间物理、PoseHistory 正常推进；磁带、环境、L1 与 L2 观测不参与（磁带与环境取 t = 0 的状态），不调用 Agent、不交付观测。
- 判定：逐机以 `awr.sensorsvc.slam.rig.check_init_window` 统计窗口（时长、以事件零点结束的静止段、比力均值、|ω| 与 |a| 上界、
  悬停窗口末速度 ≤ 0.1 m/s），结果写入物理档关闭信息的 `preroll` 块（运行清单 `tier_info.preroll`）；`gravity_align_en` 在机队
  含空中出生时为真（M25 按出生方式写入 rig.json）。窗口判定不中止运行，SLAM 导出前由 M25 `require_init_window` 转为 642；
  预滚期间机体坠毁或失联按物理档失败处理（630）。
- 缺省 `t_pre = 2 s`（M26 §9 配置表），须为控制步（100 ms）的整数倍；`0` 关闭预滚（开发夹具），其余低于合同下限 0.5 s 的值被拒。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from awr.phys import PhysError
from awr.phys.adapter import ContactEvent, TickResult, VehicleStatus
from awr.phys.pose_history import COLS, PoseSample, PoseSlice
from awr.sensorsvc.slam.rig import PREROLL_DEFAULT_S, PREROLL_MIN_S, check_init_window

__all__ = ["PREROLL_DEFAULT_S", "PREROLL_MIN_S", "EventTimeTier", "PrerollPlan", "plan_preroll", "run_preroll"]

PHYSICS_TIER_UNAVAILABLE = 630


@dataclass(frozen=True)
class PrerollPlan:
    t_pre_s: float
    tick_ms: int
    n_ticks: int
    modes: dict[str, str]                       # uav -> static | hover_hold（按机队顺序）

    @property
    def t_pre_ns(self) -> int:
        return self.n_ticks * self.tick_ms * 1_000_000

    @property
    def gravity_align_en(self) -> bool:
        return any(m == "hover_hold" for m in self.modes.values())

    def start_of(self, uav: str) -> str:
        """rig.json `init.start`：空中出生为 `pose`，地面起飞为 `ground`。"""
        return "pose" if self.modes[uav] == "hover_hold" else "ground"


def plan_preroll(fleet: Iterable[tuple[str, bool]], t_pre_s: float = PREROLL_DEFAULT_S, tick_ms: int = 100) -> PrerollPlan:
    """`fleet`：(uav, airborne) 序列 → 预滚计划；`t_pre_s` 为 0 时关闭，否则须 ≥ 0.5 s 且为 `tick_ms` 的整数倍。"""
    t = float(t_pre_s)
    n = round(t * 1000 / tick_ms)
    if t < 0 or abs(n * tick_ms - t * 1000) > 1e-6:
        raise ValueError(f"pre-roll {t_pre_s} s must be a non-negative multiple of the {tick_ms} ms control step")
    if 0 < t < PREROLL_MIN_S:
        raise ValueError(f"pre-roll {t_pre_s} s is below the {PREROLL_MIN_S} s contract minimum (0 disables it)")
    return PrerollPlan(t, int(tick_ms), int(n), {str(u): ("hover_hold" if air else "static") for u, air in fleet})


class _EventTimeHistory:
    """PoseHistoryBuffer 的事件时间视图（时刻 = 档内时刻 − off_ns）。"""

    def __init__(self, inner, off_ns: int) -> None:
        self.inner, self.off_ns = inner, int(off_ns)

    def __getattr__(self, k: str):
        return getattr(self.inner, k)

    def slice(self, uav: str, t_from: int, t_to: int) -> PoseSlice:
        sl = self.inner.slice(uav, int(t_from) + self.off_ns, int(t_to) + self.off_ns)
        return PoseSlice(sl.uav, sl.t_ns - self.off_ns, sl.state)

    def last_sample(self, uav: str) -> PoseSample:
        s = self.inner.last_sample(uav)
        return replace(s, t_ns=int(s.t_ns) - self.off_ns)

    def release_before(self, t_ns: int) -> None:
        self.inner.release_before(int(t_ns) + self.off_ns)

    def set_gimbal(self, uav: str, sensor_no: int, t_ns: int, *a, **kw) -> None:
        self.inner.set_gimbal(uav, sensor_no, int(t_ns) + self.off_ns, *a, **kw)

    def gimbal_samples(self, uav: str) -> list:
        return [replace(g, t_ns=int(g.t_ns) - self.off_ns) for g in self.inner.gimbal_samples(uav)]


def _shift_contact(c: ContactEvent, off_ns: int) -> ContactEvent:
    return replace(c, t_ns=int(c.t_ns) - off_ns)


class EventTimeTier:
    """物理档的事件时间视图（模块说明）：Env 的 tick k 对应档内 tick k + n_pre。"""

    def __init__(self, inner, plan: PrerollPlan, report: dict | None = None) -> None:
        self.inner, self.plan = inner, plan
        self.n_pre, self.off_ns = plan.n_ticks, plan.t_pre_ns
        self.history = _EventTimeHistory(inner.history, self.off_ns)
        self.report = report or {}
        self.samples: dict[str, tuple[np.ndarray, np.ndarray]] = {}  # uav -> 预滚窗口样本（事件时间 t_ns、16 列状态）

    def preroll_samples(self, uav: str) -> tuple[np.ndarray, np.ndarray]:
        """预滚窗口 `[−t_pre, 0]` 的 PoseHistory 样本（事件时间）；物理档按 `retain_s` 释放后仍可取（供 L2 IMU 预热与 SLAM 导出）。"""
        if uav in self.samples:
            return self.samples[uav]
        s = self.history.last_sample(uav)
        return np.array([s.t_ns], np.int64), np.concatenate([s.pos_enu_m, s.q_ws_xyzw, s.vel_enu_mps, s.omega_b_rps, s.f_b])[None, :]

    @property
    def name(self) -> str:
        return self.inner.name

    def __getattr__(self, k: str):
        return getattr(self.inner, k)

    def submit(self, cmds: Sequence) -> list:
        return self.inner.submit([replace(c, apply_tick=int(c.apply_tick) + self.n_pre) for c in cmds])

    def advance(self, k: int) -> TickResult:
        r = self.inner.advance(int(k) + self.n_pre)
        return replace(r, k=int(k), t_start_ns=int(r.t_start_ns) - self.off_ns, t_end_ns=int(r.t_end_ns) - self.off_ns,
                       contacts=tuple(_shift_contact(c, self.off_ns) for c in r.contacts))

    def status(self):
        return self.inner.status()

    def close(self) -> dict:
        info = dict(self.inner.close())
        if "t_end_ns" in info:
            info["t_end_ns"] = int(info["t_end_ns"]) - self.off_ns
        info["preroll"] = self.report
        return info


def run_preroll(tier, plan: PrerollPlan, *, on_contact=None) -> EventTimeTier:
    """在已打开的物理档上推进 `[−t_pre, 0)`（档内 tick 0 … n_pre − 1），返回事件时间视图。

    空中出生的机体在档内 tick 0 下发 `hover`（悬停保持）；地面起飞的机体不下发指令（静止）。逐 tick 收集 PoseHistory 样本（物理档
    按 `retain_s` 释放旧样本之前），统计每机窗口；`on_contact(ev)` 接收事件时间的接触事件。预滚结束时有机体坠毁或失联抛 630。"""
    from awr.phys.adapter import TierCommand

    hover = [TierCommand(u, "hover", 0) for u, m in plan.modes.items() if m == "hover_hold"]
    if hover and plan.n_ticks:
        res = tier.submit(hover)
        bad = [r for r in res if not r.ok]
        if bad:
            raise PhysError(PHYSICS_TIER_UNAVAILABLE, f"pre-roll hover hold rejected for {[r.entity_id for r in bad]}",
                            {"codes": [r.code for r in bad]})
    off = plan.t_pre_ns
    t0 = {u: tier.history.last_sample(u).t_ns for u in plan.modes}
    rows: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {u: [] for u in plan.modes}
    for u in plan.modes:
        s = tier.history.slice(u, t0[u], t0[u])
        rows[u].append((s.t_ns[-1:], s.state[-1:]))
    contacts: list[dict] = []
    for j in range(plan.n_ticks):
        r = tier.advance(j)
        for c in r.contacts:
            ev = _shift_contact(c, off)
            contacts.append({"uav": ev.uav, "t_ns": ev.t_ns, "kind": ev.kind})
            if on_contact is not None:
                on_contact(ev)
        for u in plan.modes:
            s = tier.history.slice(u, r.t_start_ns, r.t_end_ns)
            keep = s.t_ns > r.t_start_ns
            rows[u].append((s.t_ns[keep], s.state[keep]))
    status = dict(tier.status())
    lost = {u: str(st) for u, st in status.items() if st in (VehicleStatus.CRASHED, VehicleStatus.LOST)}
    if lost:
        raise PhysError(PHYSICS_TIER_UNAVAILABLE, f"vehicles failed during the pre-roll window: {lost}", {"status": lost, "contacts": contacts})
    report: dict[str, Any] = {"t_pre_s": plan.t_pre_s, "n_ticks": plan.n_ticks, "gravity_align_en": plan.gravity_align_en,
                              "contacts": contacts, "uavs": {}}
    samples = {}
    for u, mode in plan.modes.items():
        t = np.concatenate([a for a, _ in rows[u]]) - off
        st = np.concatenate([b for _, b in rows[u]])
        samples[u] = (t, st)
        if plan.n_ticks:
            win = check_init_window(t, st[:, COLS["omega"]], st[:, COLS["f_b"]], vel=st[:, COLS["vel"]], mode=mode, t_event0_ns=0)
            report["uavs"][u] = {"start": plan.start_of(u), **win.to_dict()}
    report["ok"] = all(v["ok"] for v in report["uavs"].values()) if plan.n_ticks else None  # 未预滚：SLAM 导出时被拒（642）
    out = EventTimeTier(tier, plan, report)
    out.samples = samples
    return out
