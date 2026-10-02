"""LinkMonitor：GCS 席位链路、agent 链路与策略切换（M09 §6.9、§6.10；FR-060 至 FR-066、FR-073；ADR-026、ADR-045）。

- 链路时钟 `L = wall_mono_ns() − paused_total_ns()`（M08 SimClock）：运行时按墙钟前进，暂停与单步时静止，倍速不放大；
  全部墙钟读数只经 SimClock（NFR-010）。
- 每个链路源只保存"最近一次心跳的链路时刻" `t_last`；收到信标时候选 `L(t_recv) − ping_age_ms`，**取最大值**（暂停期间
  断线的持有者其 `ping_age_ms` 按墙钟继续增长，候选随之变小而被忽略，恢复后年龄从暂停前的值继续累计，NFR-006）。
  租约获取（OPERATOR）视为一次心跳。
- 阶梯：age > 1.5 s DEGRADED（GCS_LINK = 0，ALERT）、> 3 s HOLD/LINK_LOSS、> 13 s RTL（无 home 时 LANDING）；恢复 < 1.5 s
  时 `auto_resume` 使 HOLD/LINK_LOSS 回到 FLYING/HOVER，已进入的 RTL 不撤销。
- 策略（FR-061）：本 run 内从未被 operator 接管的机体取剧本值（缺省 hold_rtl，但链路源为 sim-core 自身，恒为正常）；
  OPERATOR 取得租约后该机改为 hold_rtl 且链路源为席位；交还 MISSION/SWARM 后恢复剧本值与自身链路源；AGENT 持有时
  链路源为 agent-runtime（ext）。策略为 ignore 时 GCS_LINK 恒为 1。
- 流式 watchdog（FR-062）：M08 ingest 检出 250 ms【墙钟】无新 setpoint 后调用 `on_stream_watchdog`，本模块提出 HOLD/LINK_LOSS
  （AUTO，秩 2），仲裁器以 `canceled 209` 结束 Velocity 调用。
- 输入日志（ext，FR-066）：链路源状态变化按 apply_tick 追加 `link` 记录；重仿真时由日志驱动。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from awr.contracts.enums import GcsLossPolicy, Owner

from .flight_fsm import S_FLY_HOVER, S_HOLD_LINK, Origin
from .state import FS

if TYPE_CHECKING:
    from .service import SafetyRuntime

__all__ = ["LINK_DEGRADED", "LINK_LOST_HOLD", "LINK_LOST_RTL", "LINK_OK", "LinkMonitor", "LinkSource"]

LINK_OK, LINK_DEGRADED, LINK_LOST_HOLD, LINK_LOST_RTL = 0, 1, 2, 3
SRC_SELF, SRC_SEAT, SRC_AGENT = 0, 1, 2
BIG_FUTURE = 1 << 62
_HOLD_SRC = (int(FS.TAKING_OFF), int(FS.FLYING), int(FS.CORRECTING))
_RTL_SRC = (int(FS.TAKING_OFF), int(FS.FLYING), int(FS.CORRECTING), int(FS.HOLD))


@dataclass
class LinkSource:
    sid: int
    name: str
    t_last: int = 0
    state: int = LINK_OK
    primed: bool = False


class LinkMonitor:
    def __init__(self, rt: SafetyRuntime) -> None:
        self.rt = rt
        self.seat = LinkSource(SRC_SEAT, "seat")
        self.agent = LinkSource(SRC_AGENT, "agent", t_last=BIG_FUTURE)
        self.sources = (self.seat, self.agent)
        self.seat_holder: str | None = None
        self.logged: list[tuple[int, int, int]] = []  # (tick, sid, state)：输入日志（ext）
        self.prime_on_eval = False
        self.replay: dict[int, list[tuple[int, int]]] | None = None  # 重仿真：tick -> [(sid, state)]

    def reset(self) -> None:
        self.seat = LinkSource(SRC_SEAT, "seat")
        self.agent = LinkSource(SRC_AGENT, "agent", t_last=BIG_FUTURE)
        self.sources = (self.seat, self.agent)
        self.seat_holder = None

    # ---------------------------------------------------------------- 时钟
    def now(self) -> int:
        clk = self.rt.clock
        if clk is None:
            return 0
        return int(clk.wall_mono_ns()) - int(clk.paused_total_ns())

    # ---------------------------------------------------------------- 输入（主循环步顶 drain 时调用）
    def on_gcs_beacon(self, principal_id: str | None, seat_state: str, ping_age_ms: int, t_recv_ns: int,
                      paused_ns: int) -> None:
        if principal_id is None:
            self.seat_holder = None
            return  # 席位 FREE：孤儿 OPERATOR 租约的年龄从 t_last 继续增长
        self.seat_holder = principal_id
        cand = (int(t_recv_ns) - int(paused_ns)) - int(ping_age_ms) * 1_000_000
        src = self.seat
        if not src.primed:
            src.t_last = cand
            src.primed = True
        else:
            src.t_last = max(src.t_last, cand)

    def on_lease_acquired_operator(self) -> None:
        if self.rt.clock is None:  # 尚无时钟（首个 stage 之前）：下一次 eval 时视为一次心跳
            self.prime_on_eval = True
            return
        src = self.seat
        now = self.now()
        src.t_last = max(src.t_last, now) if src.primed else now
        src.primed = True

    def on_agent_liveliness(self, alive: bool) -> None:
        self.agent.t_last = BIG_FUTURE if alive else self.now()

    def on_lease_event(self, kind: str, slots: np.ndarray, owner: str | None) -> None:
        rt = self.rt
        sb = rt.sb if rt.S is not None else None
        if sb is None:
            return
        s = np.asarray(slots, np.int64)
        s = s[(s >= 0) & (s < rt.S.capacity)]
        if kind in ("acquired", "released", "returned", "preempted") and s.size:
            if kind == "preempted":
                return  # 随后的 acquired 事件给出新 owner
            if owner == "OPERATOR":
                sb["ever_operator"][s] = True
                sb["policy"][s] = int(GcsLossPolicy.HOLD_RTL)
                sb["link_src"][s] = SRC_SEAT
                self.on_lease_acquired_operator()
            elif owner == "AGENT":
                sb["link_src"][s] = SRC_AGENT
                sb["policy"][s] = int(GcsLossPolicy.HOLD_RTL)
            else:  # MISSION、SWARM、NONE：恢复剧本值，链路源为自身（链路状态的恢复边沿由下一次 eval 处理）
                sb["link_src"][s] = SRC_SELF
                sb["policy"][s] = rt.default_policy

    # ---------------------------------------------------------------- 年龄
    def age_ns(self, src: LinkSource) -> int:
        if src.t_last >= BIG_FUTURE:
            return 0
        if not src.primed and src is self.seat:
            return 0
        return max(0, self.now() - src.t_last)

    def age_ms_of(self, slots: np.ndarray) -> np.ndarray:
        sb = self.rt.sb
        out = np.full(len(slots), -1, np.int64)
        src = sb["link_src"][slots]
        out[src == SRC_SEAT] = self.age_ns(self.seat) // 1_000_000
        out[src == SRC_AGENT] = self.age_ns(self.agent) // 1_000_000
        return out

    def _classify(self, age_ns: int) -> int:
        P = self.rt.params.link
        a = age_ns / 1e9
        if a < P.warn_s:
            return LINK_OK
        if a < P.hold_s:
            return LINK_DEGRADED
        if a < P.rtl_s:
            return LINK_LOST_HOLD
        return LINK_LOST_RTL

    # ---------------------------------------------------------------- 评估（guard stage，50 Hz【仿真】）
    def eval(self, ctx: Any) -> None:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        tick = int(getattr(ctx, "tick", 0))
        if self.prime_on_eval and rt.clock is not None:
            self.prime_on_eval = False
            self.on_lease_acquired_operator()
        if self.replay is not None:
            for sid, st in self.replay.get(tick, []):
                (self.seat if sid == SRC_SEAT else self.agent).state = st
        else:
            for src in self.sources:
                new = self._classify(self.age_ns(src))
                if new != src.state:
                    src.state = new
                    self.logged.append((tick + 1, src.sid, new))
                    if rt.inputlog is not None:
                        with contextlib.suppress(Exception):
                            rt.inputlog.append("link", tick + 1, {"src": src.name, "state": new})
        act = rt.act_idx
        if act.size == 0:
            return
        srcs = sb["link_src"][act]
        wmask = (srcs != SRC_SELF) & (sb["policy"][act] == int(GcsLossPolicy.HOLD_RTL))
        if not wmask.any() and not (sb["link_state"][act] != LINK_OK).any():
            sb["flag_gcs"][act] = True
            return
        # 受监视机体取链路源状态；其余（策略 ignore 或链路源为自身）恒为正常
        watched = act
        st = np.where(wmask, np.where(srcs == SRC_SEAT, self.seat.state, self.agent.state), LINK_OK).astype(np.uint8)
        old = sb["link_state"][watched].copy()
        sb["link_state"][watched] = st
        ok_now = st == LINK_OK
        sb["flag_gcs"][watched] = ok_now
        if ok_now.all() and (old == LINK_OK).all():
            return  # 全部正常且无恢复边沿：以下边沿、HOLD 与 RTL 候选均为空
        t = rt.t_ns
        fs = sb["fs"][watched]
        air = S.in_air[watched] | (fs == FS.TAKING_OFF)
        agent = sb["link_src"][watched] == SRC_AGENT
        # 边沿事件与条件位
        deg = watched[(st >= LINK_DEGRADED) & (old == LINK_OK)]
        if deg.size:
            rt.set_cond(deg, "LINK_DEGRADED", True)
            rt.sink.add_many(deg, rt.code("SAF.LINK.DEGRADED"), t, values=self.age_ms_of(deg) / 1000.0,
                             threshold=rt.params.link.warn_s)
        rec = watched[(st == LINK_OK) & (old != LINK_OK)]
        if rec.size:
            rt.set_cond(rec, "LINK_DEGRADED", False)
            rt.set_cond(rec, "LINK_LOST", False)
            back = rec[(sb["fs"][rec] == FS.HOLD) & (sb["sub"][rec] == S_HOLD_LINK) & S.in_air[rec]]
            quiet = np.setdiff1d(rec, back)
            if back.size and rt.params.link.auto_resume:
                ok = rt.fsm.propose(back, int(FS.FLYING), S_FLY_HOVER, Origin.AUTO, "SAF.LINK.RESTORED")
                quiet = np.union1d(quiet, back[~ok])
            elif back.size:
                quiet = np.union1d(quiet, back)
            if quiet.size:
                rt.sink.add_many(quiet, rt.code("SAF.LINK.RESTORED"), t)
        # HOLD 与 RTL 候选（持续条件每周期重提，秩过滤去重）
        # 先看链路态再求 fs 集合（全部正常时免去两次 np.isin；1000 架时约 0.2 ms/次，50 Hz）
        hold = st == LINK_LOST_HOLD
        if hold.any():
            hold &= air & np.isin(fs, _HOLD_SRC)
        if hold.any():
            s = watched[hold]
            rt.set_cond(s, "LINK_LOST", True)
            codes = np.where(agent[hold], rt.code("SAF.LINK.AGENT_LOST"), rt.code("SAF.LINK.LOST_HOLD"))
            for c in np.unique(codes):
                m = codes == c
                rt.fsm.propose(s[m], int(FS.HOLD), S_HOLD_LINK, Origin.AUTO, int(c),
                               value=self.age_ms_of(s[m]) / 1000.0, thr=rt.params.link.hold_s)
        lost = st == LINK_LOST_RTL
        if lost.any():
            lost &= air & np.isin(fs, _RTL_SRC)
        if lost.any():
            s = watched[lost]
            rt.set_cond(s, "LINK_LOST", True)
            has_home = np.ones(s.size, np.bool_)  # Mock 恒有 home（出生点）
            code = "SAF.LINK.LOST_RTL"
            rt.fsm.propose(s[has_home], int(FS.RTL), 0, Origin.AUTO, code, value=self.age_ms_of(s[has_home]) / 1000.0,
                           thr=rt.params.link.rtl_s)
            if (~has_home).any():
                rt.fsm.propose(s[~has_home], int(FS.LANDING), 0, Origin.AUTO, code, detail="NO_HOME")

    # ---------------------------------------------------------------- 流式 watchdog（FR-062）
    def on_stream_watchdog(self, slots: np.ndarray) -> None:
        rt = self.rt
        s = np.asarray(slots, np.int64)
        if s.size == 0 or rt.S is None:
            return
        rt.set_cond(s, "WATCHDOG", True)
        ok = rt.fsm.propose(s, int(FS.HOLD), S_HOLD_LINK, Origin.AUTO, "SAF.LINK.WATCHDOG",
                            thr=rt.params.link.watchdog_ms / 1000.0)
        rej = s[~ok]
        if rej.size:  # 不转移（例如已在 HOLD）时仍结束 Velocity 调用
            rt.resolve_calls(rej, "canceled", 209, "SAF.LINK.WATCHDOG")

    def owner_of(self, slot: int) -> int:
        lease = self.rt.lease
        if lease is None:
            return int(Owner.NONE)
        try:
            return int(lease.owner_codes()[slot])
        except Exception:
            return int(Owner.NONE)
