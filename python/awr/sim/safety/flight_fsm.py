"""Flight FSM 与 ActionArbiter（M09 §6.4；FR-001 至 FR-014；ADR-015、ADR-026）。

- 14 态白名单 `WHITELIST`（M09 为数值定义方，与 12 §4.4.4 逐项相等）、仲裁秩 `RANK`（Severity 0–6、8，自动 kill 为 7）、
  锁存（ELAND、FAILSAFE，到达 LANDED 或 DISARMED 解除）、5 种自动恢复例外（12 §4.4.5）。
- 候选缓冲 `Candidates`：守卫、定时器、contact 标志在本 tick 内写入，同一 slot 只保留仲裁秩最大者（同秩先到者）；
  OPERATOR 候选逐条保存，先于 SYSTEM/AUTO 提交。唯一的状态改变点是 `FlightFSM.resolve()`（任何 stage 直接写 `fs` 都视为缺陷，
  `tests/safety/test_single_writer.py`）。
- 名义推进（SYSTEM）与操作员命令（OPERATOR）按 M08 运动模式的变化推断：M08 CommandEngine 直接执行命令运动，fsm 以
  `mode_t`（每次模式、阶段事件都会更新）检测变化，用上一 tick 下发的 Supervisor 期望模式（`sup_expect`）区分安全动作与
  操作员命令；安全类命令另经 `SafetyHooks.apply_operator()` 显式登记（M08-FR-060）。
- 动作经 SafetyActuator 写 M08 SupervisorQueue，下一 tick 由 ingest 执行（固定 1 tick 延迟）；在途调用经
  `CommandEngine.resolve_calls()` 结束（204、208、209；103 预检失败）。
"""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING, Any

import numpy as np

from awr.contracts.enums import SEVERITY, Lifecycle
from awr.contracts.reasons import Reason
from awr.sim.fleet.state import EVT_ARRIVED, EVT_COLLISION, EVT_MODE, EVT_TOUCHDOWN, CtrlMode

from . import codes as C
from .state import FS, SUBV, WARN_OR_ABOVE_MASK

if TYPE_CHECKING:
    from .service import SafetyRuntime

__all__ = [
    "ELAND_OK",
    "FAIL_OK",
    "KILL_RANK",
    "NONE_KEY",
    "RANK",
    "WHITELIST",
    "Candidates",
    "FlightFSM",
    "Origin",
    "is_restore",
    "whitelist_table",
]


class Origin(IntEnum):
    SYSTEM = 0
    AUTO = 1
    OPERATOR = 2


# ---------------------------------------------------------------- 白名单（M09 §6.4.2）
_ABBR = ("UNK", "DIS", "PRE", "RDY", "TKO", "FLY", "COR", "HLD", "RTL", "LND", "ELD", "FSF", "LDD", "CRS")
_TABLE = """
UNK = Y - - - - - - - - - - - Y
DIS Y = Y Y - - - - - - - - - Y
PRE Y Y = Y - - - - - - - - - Y
RDY Y Y - = Y - - - - - - - - Y
TKO Y Y - - = Y - Y - Y Y Y - Y
FLY Y Y - - - = Y Y Y Y Y Y - Y
COR Y Y - - - Y = Y Y Y Y Y - Y
HLD Y Y - - - Y - = Y Y Y Y - Y
RTL Y Y - - - Y - Y = Y Y Y Y Y
LND Y Y - - - Y - Y Y = Y Y Y Y
ELD Y Y - - - - - - - - = Y Y Y
FSF Y Y - - - - - - - - - = Y Y
LDD Y Y - Y - - - - - - - - = Y
CRS Y Y - - - - - - - - - - - =
"""


def whitelist_table() -> np.ndarray:
    wl = np.zeros((14, 14), np.bool_)
    rows = [ln.split() for ln in _TABLE.strip().splitlines()]
    for r in rows:
        i = _ABBR.index(r[0])
        for j, sym in enumerate(r[1:]):
            wl[i, j] = sym == "Y"
    return wl


WHITELIST = whitelist_table()
WHITELIST.flags.writeable = False
RANK = np.array([-1, -1, -1, -1, 0, 0, 1, 2, 3, 4, 5, 6, -1, 8], np.int8)
KILL_RANK = 7
NONE_KEY = -2


def _mask(*states: int) -> np.ndarray:
    m = np.zeros(14, np.bool_)
    m[list(states)] = True
    return m


ELAND_OK = _mask(FS.FAILSAFE, FS.LANDED, FS.DISARMED, FS.CRASHED)
FAIL_OK = _mask(FS.LANDED, FS.DISARMED, FS.CRASHED)
LATCH_ELAND, LATCH_FAILSAFE = 1, 2
SEV = np.array([SEVERITY.get(i, 0) for i in range(14)], np.uint8)

S_HOLD_STOP = SUBV[(FS.HOLD, "SAFETY_STOP")]
S_HOLD_LINK = SUBV[(FS.HOLD, "LINK_LOSS")]
S_HOLD_ESC = SUBV[(FS.HOLD, "ESCALATE")]
S_HOLD_SEP = SUBV[(FS.HOLD, "SEPARATION")]
S_HOLD_LOC = SUBV[(FS.HOLD, "LOC_LOST")]
S_DIS_READY = SUBV[(FS.DISARMED, "READY_TO_ARM")]
S_DIS_NOTREADY = SUBV[(FS.DISARMED, "NOT_READY")]
S_DIS_KILLED = SUBV[(FS.DISARMED, "KILLED")]
S_FLY_HOVER = SUBV[(FS.FLYING, "HOVER")]
S_TKO_SPOOL, S_TKO_CLIMB = SUBV[(FS.TAKING_OFF, "SPOOLUP")], SUBV[(FS.TAKING_OFF, "CLIMB")]
S_LND_DESC, S_LND_GOTO, S_LND_TD = SUBV[(FS.LANDING, "DESCEND")], SUBV[(FS.LANDING, "GOTO")], SUBV[(FS.LANDING, "TOUCHDOWN")]
S_RTL_CLIMB, S_RTL_CRUISE, S_RTL_DESCEND, S_RTL_FINAL = 0, 1, 2, 3

TIMER_NONE, TIMER_PREFLIGHT, TIMER_READY, TIMER_LANDED, TIMER_LOC, TIMER_NOTREADY = 0, 1, 2, 3, 4, 5

_FLY_SUB = {int(CtrlMode.GOTO): SUBV[(FS.FLYING, "GOTO")], int(CtrlMode.PATH): SUBV[(FS.FLYING, "PATH")],
            int(CtrlMode.ORBIT): SUBV[(FS.FLYING, "ORBIT")], int(CtrlMode.VELOCITY): SUBV[(FS.FLYING, "VELOCITY")],
            int(CtrlMode.OFFBOARD_POS): SUBV[(FS.FLYING, "EXTERNAL")]}
_TRAJ_SUB = {"goto": SUBV[(FS.FLYING, "GOTO")], "follow_path": SUBV[(FS.FLYING, "PATH")],
             "orbit": SUBV[(FS.FLYING, "ORBIT")]}
_MODE_CODE = {int(CtrlMode.GOTO): "OP.GOTO", int(CtrlMode.PATH): "OP.FOLLOW_PATH", int(CtrlMode.ORBIT): "OP.ORBIT",
              int(CtrlMode.VELOCITY): "OP.VELOCITY", int(CtrlMode.OFFBOARD_POS): "OP.OFFBOARD", int(CtrlMode.TRAJ): "OP.TRAJ"}
_LC_UNKNOWN_SUB = {int(Lifecycle.PENDING): "BOOTING", int(Lifecycle.PROVISIONING): "BOOTING",
                   int(Lifecycle.STARTING): "BOOTING", int(Lifecycle.LOST): "LINK_LOST",
                   int(Lifecycle.RESTARTING): "RESTARTING", int(Lifecycle.FAILED): "FAILED",
                   int(Lifecycle.STOPPED): "NO_DATA", int(Lifecycle.REMOVED): "NO_DATA"}
_READY_LC = np.zeros(16, np.bool_)
_READY_LC[[int(Lifecycle.READY), int(Lifecycle.DEGRADED), int(Lifecycle.DRAINING)]] = True
_CRASH_CODE = {0: "SAF.WORLD.IMPACT", 1: "SAF.WORLD.COLLISION", 2: "SAF.SEP.COLLISION", 3: "SAF.WORLD.IMPACT"}
_BAT_REASONS = frozenset(C.idx(c) for c in ("SAF.BAT.CRIT", "SAF.BAT.ENERGY_RTL", "SAF.BAT.EMERG"))


def is_restore(src: np.ndarray, sub: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """自动恢复的 5 种例外（12 §4.4.5）：COR→FLY、HLD/LINK_LOSS→FLY、HLD/SEPARATION→FLY、LDD→DIS、RDY→DIS。"""
    src, sub, dst = np.asarray(src), np.asarray(sub), np.asarray(dst)
    return (((src == FS.CORRECTING) & (dst == FS.FLYING))
            | ((src == FS.HOLD) & ((sub == S_HOLD_LINK) | (sub == S_HOLD_SEP)) & (dst == FS.FLYING))
            | ((src == FS.LANDED) & (dst == FS.DISARMED)) | ((src == FS.READY) & (dst == FS.DISARMED)))


def allowed(fs0: int, fs1: int, latch: int) -> bool:
    """白名单 + 锁存（同状态的子模式变化恒允许）。"""
    if fs0 == fs1:
        return True
    if not WHITELIST[fs0, fs1]:
        return False
    if latch & LATCH_ELAND and not ELAND_OK[fs1]:
        return False
    return not (latch & LATCH_FAILSAFE and not FAIL_OK[fs1])


# 准入矩阵中每条命令被接受后 FSM 的第一跳目标（启动自检用：矩阵为 Y 的格子必须能沿白名单到达，M09-FR-112）
_CMD_HOP = {"takeoff": FS.TAKING_OFF, "land": FS.LANDING, "goto": FS.FLYING, "follow_path": FS.FLYING,
            "orbit": FS.FLYING, "velocity": FS.FLYING, "hover": FS.FLYING, "rtl": FS.RTL, "safety_stop": FS.HOLD,
            "pause": FS.FLYING, "resume": FS.FLYING, "arm": FS.PREFLIGHT, "disarm": FS.DISARMED, "kill": FS.DISARMED}
_FIRST_HOP = {("takeoff", FS.DISARMED): FS.PREFLIGHT, ("takeoff", FS.LANDED): FS.READY}


def whitelist_selfcheck(wl: np.ndarray | None = None) -> list[str]:
    """白名单与 `commands.json` 准入矩阵一致性、仲裁秩单调性（M09-FR-112）；返回问题列表（空为通过）。"""
    import json

    from awr.contracts._paths import contracts_root

    wl = WHITELIST if wl is None else wl
    errs: list[str] = []
    doc = json.loads((contracts_root() / "rt" / "commands.json").read_text(encoding="utf-8"))
    cols = {c["id"]: FS[c["flight_state"]] for c in doc["admission_columns"]}
    for svc in doc["services"]:
        op = svc.get("op")
        adm = svc.get("admission")
        if op not in _CMD_HOP or not isinstance(adm, dict):
            continue
        for col, sym in adm.items():
            if sym != "Y" or col not in cols:
                continue
            src = int(cols[col])
            hop = int(_FIRST_HOP.get((op, FS(src)), _CMD_HOP[op]))
            if src != hop and not wl[src, hop]:
                errs.append(f"{op} accepted in {col} but {FS(src).name}->{FS(hop).name} is not whitelisted")
    if not (RANK[FS.CORRECTING] < RANK[FS.HOLD] < RANK[FS.RTL] < RANK[FS.LANDING] < RANK[FS.ELAND] < RANK[FS.FAILSAFE]
            < KILL_RANK < RANK[FS.CRASHED]):
        errs.append("arbiter rank is not monotonic (COR < HLD < RTL < LND < ELD < FSF < KILL < CRS)")
    errs.extend(f"{FS(s).name} must reach UNKNOWN and CRASHED" for s in range(14)
                if (not wl[s, FS.UNKNOWN] and s != FS.UNKNOWN) or (not wl[s, FS.CRASHED] and s != FS.CRASHED))
    return errs


class Candidates:
    """每 tick 的候选缓冲（M09 §6.4.6）。"""

    def __init__(self, n: int) -> None:
        self.key = np.full(n, NONE_KEY, np.int8)
        self.fs = np.zeros(n, np.uint8)
        self.sub = np.zeros(n, np.uint8)
        self.origin = np.zeros(n, np.uint8)
        self.code = np.zeros(n, np.uint16)
        self.value = np.full(n, np.nan, np.float32)
        self.thr = np.full(n, np.nan, np.float32)
        self.target = np.full((n, 3), np.nan)
        self.detail: dict[int, str] = {}
        self.losers: list[tuple[int, int, float, float, str | None]] = []
        self.operator: list[tuple[int, int, int, int, dict]] = []
        self.t_ns = np.zeros(n, np.int64)
        self.pending = False  # 本 tick 有候选（快路径：无候选时 fsm 不做仲裁）

    def any(self) -> bool:
        return self.pending

    def clear(self) -> None:
        if not self.pending:
            return
        self.pending = False
        i = np.flatnonzero(self.key > NONE_KEY)
        self.key[i] = NONE_KEY
        self.value[i] = np.nan
        self.thr[i] = np.nan
        self.target[i] = np.nan
        self.detail.clear()
        self.losers.clear()
        self.operator.clear()


class FlightFSM:
    def __init__(self, rt: SafetyRuntime) -> None:
        self.rt = rt
        n = rt.S.capacity
        self.C = Candidates(n)
        self.rejected_wall = np.zeros(n, np.int64)
        self.transitions = 0
        self.last_resolve: list[tuple[int, int, int, int, int]] = []  # 测试：(slot, from, to, origin, tick)
        # 快路径状态（空闲 tick 只做少量 O(N) 字节比较，M09-NFR-002 fsm ≤ 0.02 ms）
        self.flags_dirty = True
        self.lc_shadow = np.full(n, 255, np.uint8)
        self.next_deadline = 0

    # ================================================================== 候选 API
    def propose(self, slots: np.ndarray, fs: int, sub: int, origin: Origin, code: str | int, *, key: int | None = None,
                value: np.ndarray | float | None = None, thr: np.ndarray | float | None = None, detail: str | None = None,
                target: np.ndarray | None = None, relabel: bool = False) -> np.ndarray:
        """提出候选；返回实际登记（未被过滤、未被更高秩压过）的 slot 掩码。

        AUTO 候选先按"只升不降"过滤：目标秩必须严格大于当前秩（恢复例外与同状态重标记除外），无意义的候选不进入缓冲，
        避免持续条件在每个守卫周期重复发事件。"""
        s = np.asarray(slots, np.int64).reshape(-1)
        if s.size == 0:
            return np.zeros(0, np.bool_)
        rt = self.rt
        sb = rt.sb
        C = self.C
        ci = C_idx(code)
        k = np.int8(key if key is not None else (RANK[fs] if origin != Origin.SYSTEM or RANK[fs] >= 0 else -1))
        cur = sb["fs"][s]
        if origin == Origin.AUTO:
            cur_rank = RANK[cur].astype(np.int16)
            cur_rank = np.where((cur == FS.DISARMED) & (sb["sub"][s] == S_DIS_KILLED), KILL_RANK, cur_rank)
            up = k > cur_rank
            rest = is_restore(cur, sb["sub"][s], np.full(s.size, fs))
            same = (cur == fs) & (sb["sub"][s] == sub) & relabel & ~sb["fs_auto"][s]
            keep = rest if fs == FS.FLYING else (up | rest | same)
        else:
            keep = np.ones(s.size, np.bool_)
        s2 = s[keep]
        if s2.size == 0:
            return keep
        better = k > C.key[s2]
        lose = s2[~better]
        vals = None if value is None else np.broadcast_to(np.asarray(value, np.float32), s.shape)[keep]
        thrs = None if thr is None else np.broadcast_to(np.asarray(thr, np.float32), s.shape)[keep]
        if lose.size and origin == Origin.AUTO:
            for j, sl in enumerate(lose):
                v = float(vals[~better][j]) if vals is not None else float("nan")
                th = float(thrs[~better][j]) if thrs is not None else float("nan")
                C.losers.append((int(sl), ci, v, th, detail))
        w = s2[better]
        if w.size:
            C.pending = True
            prev = w[(C.key[w] > NONE_KEY) & (C.origin[w] == Origin.AUTO)]
            for sl in prev:  # 被更高秩压过的既有候选：只发事件
                C.losers.append((int(sl), int(C.code[sl]), float(C.value[sl]), float(C.thr[sl]), C.detail.get(int(sl))))
            C.key[w] = k
            C.fs[w] = fs
            C.sub[w] = sub
            C.origin[w] = int(origin)
            C.code[w] = ci
            C.t_ns[w] = rt.t_ns
            if vals is not None:
                C.value[w] = vals[better]
            else:
                C.value[w] = np.nan
            C.thr[w] = np.nan if thrs is None else thrs[better]
            if target is not None:
                tg = np.asarray(target, np.float64).reshape(-1, 3)
                if tg.shape[0] == s.size:
                    C.target[w] = tg[keep][better]
                else:
                    C.target[w] = tg[0]
            for sl in w:
                if detail is not None:
                    C.detail[int(sl)] = detail
                else:
                    C.detail.pop(int(sl), None)
        out = np.zeros(s.size, np.bool_)
        idx_keep = np.flatnonzero(keep)
        out[idx_keep[better]] = True
        return out

    def propose_operator(self, slot: int, fs: int, sub: int, code: str, *, tick: int | None = None, **extra: Any) -> None:
        """OPERATOR 候选；`tick` 为命令生效的 tick（apply_operator 在 ingest 中调用，早于本 tick 的 M09 stage）。"""
        self.C.operator.append((int(slot), int(fs), int(sub), C.idx(code), dict(extra)))
        self.C.pending = True
        self.rt.sb["op_tick"][slot] = self.rt.tick if tick is None else int(tick)

    # ================================================================== 行初始化
    def init_rows(self, slots: np.ndarray) -> None:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        s = np.asarray(slots, np.int64)
        if s.size == 0:
            return
        sb["fs"][s] = FS.UNKNOWN
        sb["sub"][s] = SUBV[(FS.UNKNOWN, "BOOTING")]
        for f in ("fs_auto", "locked", "flag_failsafe", "flag_loc_deg", "takeoff_pending", "sep_warn", "ever_operator"):
            sb[f][s] = False
        for f in ("flag_gcs", "flag_fcu", "flag_loc_ok", "flag_alert"):
            sb[f][s] = True
        sb["latch"][s] = 0
        sb["t_enter_ns"][s] = rt.t_ns
        sb["reason"][s] = C.idx("SYS.SPAWN")
        sb["deadline_ns"][s] = 0
        sb["timer_kind"][s] = TIMER_NONE
        sb["cond"][s] = 0
        sb["cond_since_ns"][s] = -1
        sb["last_mode"][s] = S.ctrl_mode[s]
        sb["last_phase"][s] = S.ctrl_phase[s]
        sb["last_mode_t"][s] = S.mode_t[s]
        sb["landed_prev"][s] = S.landed[s]
        sb["sup_expect"][s] = -1
        sb["op_tick"][s] = -1
        for f in ("te_since", "pe_since", "thr_since", "trk_since", "near_ok_since", "sep_rearm_since", "sep_ok_since"):
            sb[f][s] = np.nan
        sb["last_ref"][s] = np.nan
        sb["last_ref_t"][s] = 0.0
        sb["pe_max_m"][s] = 0.0
        sb["pe_gust_max_m"][s] = 0.0
        for f in ("geo_margin_m", "clearance_m", "sep_m", "cpa_min_m", "cyc_sep", "cyc_cpa"):
            sb[f][s] = np.inf
        sb["d_free_fence_m"][s] = np.inf
        for f in ("zone_hit", "zone_near"):
            sb[f][s] = -1
        for f in ("sep_mate", "sep_cpa_mate", "yield_mate", "cyc_mate", "cyc_cpa_mate"):
            sb[f][s] = -1
        sb["geo_state"][s] = 0
        sb["correct_target"][s] = np.nan
        sb["correct_since_ns"][s] = 0
        sb["link_src"][s] = 0
        sb["policy"][s] = rt.default_policy
        sb["link_state"][s] = 0
        sb["yield_state"][s] = 0
        sb["yield_hist"][s] = -np.inf
        sb["fault_mask"][s] = 0
        sb["est_age_extra_s"][s] = 0.0
        sb["severity"][s] = 0
        sb["inited"][s] = True
        self.C.key[s] = NONE_KEY
        self.lc_shadow[s] = 255  # 强制下一 tick 做生命周期投影
        self.flags_dirty = True
        if rt.bat is not None:
            rt.bat.init_rows(s)

    # ================================================================== fsm stage（每 tick）
    def tick(self, ctx: Any) -> None:
        rt, sb = self.rt, self.rt.sb
        act = rt.act_idx
        if act.size == 0:
            self.C.clear()
            return
        S = rt.S
        t = rt.t_ns
        a = rt.act_
        if a.dirty:
            exp = sb["sup_expect"][act].copy()
            sb["sup_expect"][act] = -1
            a.dirty = False
        else:
            exp = None
        lc = S.lifecycle[act]
        if not np.array_equal(lc, self.lc_shadow[act]):
            self._lifecycle(act, t)
            self.lc_shadow[act] = lc
            self.flags_dirty = True
        if S.mode_evt[act].any():
            self._motion(act, np.full(act.size, -1, np.int16) if exp is None else exp, t)
            self._crash(act)
        if t >= self.next_deadline:
            self._timers(act, t)
        if (sb["fs"][act] == FS.RTL).any():
            self._rtl_phase(act, t)
        if self.C.pending:
            self.resolve(t)
        if self.flags_dirty:
            self.flags_dirty = False
            self._flags(act)

    # ---------------------------------------------------------------- 生命周期投影（g04 §4.6；12 F41）
    def _lifecycle(self, act: np.ndarray, t: int) -> None:
        S, sb = self.rt.S, self.rt.sb
        lc = S.lifecycle[act]
        ready = _READY_LC[np.minimum(lc, 15)]
        fs = sb["fs"][act]
        sub = sb["sub"][act]
        nr = act[~ready]
        for s in nr:
            lcs = int(S.lifecycle[s])
            if lcs == int(Lifecycle.BOOTED):
                want = (int(FS.DISARMED), S_DIS_NOTREADY)
            else:
                want = (int(FS.UNKNOWN), SUBV[(FS.UNKNOWN, _LC_UNKNOWN_SUB.get(lcs, "NO_DATA"))])
            if (int(sb["fs"][s]), int(sb["sub"][s])) != want:
                self._commit(int(s), want[0], want[1], Origin.SYSTEM, C.idx("SYS.LIFECYCLE"), t)
        promote = act[ready & ((fs == FS.UNKNOWN) | ((fs == FS.DISARMED) & (sub == S_DIS_NOTREADY)
                                                      & (sb["timer_kind"][act] != TIMER_NOTREADY)))]
        for s in promote:
            self._commit(int(s), int(FS.DISARMED), S_DIS_READY, Origin.SYSTEM, C.idx("SYS.LIFECYCLE"), t)

    # ---------------------------------------------------------------- 运动模式推断（名义推进与操作员命令）
    def _motion(self, act: np.ndarray, exp: np.ndarray, t: int) -> None:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        mt = S.mode_t[act]
        changed = mt != sb["last_mode_t"][act]
        landed = S.landed[act]
        td = landed & ~sb["landed_prev"][act]
        sb["landed_prev"][act] = landed
        # 触地（12 F38）：落地类状态 → LANDED/SETTLING；其余空中状态经 LANDING/TOUCHDOWN 再到 LANDED
        for s in act[td]:
            s = int(s)
            fs = int(sb["fs"][s])
            if fs in (FS.LANDING, FS.ELAND, FS.FAILSAFE, FS.RTL):
                self._commit(s, int(FS.LANDED), 0, Origin.SYSTEM, C.idx("SYS.TOUCHDOWN"), t)
            elif fs in (FS.FLYING, FS.HOLD, FS.CORRECTING, FS.TAKING_OFF):
                self._commit(s, int(FS.LANDING), S_LND_TD, Origin.SYSTEM, C.idx("SYS.TOUCHDOWN"), t)
                self._commit(s, int(FS.LANDED), 0, Origin.SYSTEM, C.idx("SYS.TOUCHDOWN"), t)
        idx = np.flatnonzero(changed)
        if idx.size == 0:
            return
        for k in idx:
            s = int(act[k])
            mode = int(S.ctrl_mode[s])
            phase = int(S.ctrl_phase[s])
            last = int(sb["last_mode"][s])
            bits = int(S.mode_evt[s])
            sb["last_mode"][s] = mode
            sb["last_phase"][s] = phase
            sb["last_mode_t"][s] = S.mode_t[s]
            if int(sb["op_tick"][s]) == rt.tick:
                continue  # 本 tick 已有显式 OPERATOR 候选（apply_operator）
            fs, sub = int(sb["fs"][s]), int(sb["sub"][s])
            if mode == int(exp[k]):
                continue  # Supervisor 动作的执行（本模块上一 tick 下发）
            if mode == int(CtrlMode.KILLED):
                if int(S.crash_sub[s]) == 0 and not bits & EVT_COLLISION and fs != FS.DISARMED:
                    self.propose_operator(s, int(FS.DISARMED), S_DIS_KILLED, "OP.KILL")
                continue
            if mode == last:
                self._phase_change(s, mode, phase, fs, sub, t)
                continue
            self._mode_change(s, mode, phase, last, bits, fs, sub, t)

    def _phase_change(self, s: int, mode: int, phase: int, fs: int, sub: int, t: int) -> None:
        if mode == CtrlMode.TAKEOFF and fs == FS.TAKING_OFF:
            want = S_TKO_CLIMB if phase >= 1 else S_TKO_SPOOL
            if want != sub:
                self._commit(s, int(FS.TAKING_OFF), want, Origin.SYSTEM, C.idx("SYS.SPOOLUP"), t)
        elif mode == CtrlMode.LAND and fs == FS.LANDING:
            want = (S_LND_GOTO, S_LND_DESC, S_LND_TD)[min(phase, 2)]
            if want != sub:
                self._commit(s, int(FS.LANDING), want, Origin.SYSTEM, C.idx("SYS.PHASE"), t)

    def _mode_change(self, s: int, mode: int, phase: int, last: int, bits: int, fs: int, sub: int, t: int) -> None:
        rt = self.rt
        if mode == CtrlMode.IDLE:
            if bits & EVT_TOUCHDOWN or (rt.S.landed[s] and fs in (FS.LANDED, FS.LANDING, FS.ELAND, FS.FAILSAFE, FS.RTL)):
                return
            if fs in (FS.READY, FS.LANDED, FS.PREFLIGHT):
                self.propose_operator(s, int(FS.DISARMED), S_DIS_READY, "OP.DISARM")
            return
        if mode == CtrlMode.SPOOLUP:
            if fs == FS.DISARMED:
                self.propose_operator(s, int(FS.PREFLIGHT), 0, "OP.ARM", preflight=True)
            return
        if mode == CtrlMode.TAKEOFF:
            if fs == FS.DISARMED:
                self.propose_operator(s, int(FS.PREFLIGHT), 0, "OP.TAKEOFF", preflight=True, takeoff=True)
            elif fs in (FS.READY, FS.LANDED):
                self.propose_operator(s, int(FS.TAKING_OFF), S_TKO_CLIMB if phase >= 1 else S_TKO_SPOOL, "OP.TAKEOFF",
                                      via_ready=fs == FS.LANDED)
            elif fs == FS.PREFLIGHT:
                rt.sb["takeoff_pending"][s] = True
            return
        if mode == CtrlMode.HOLD:
            if bits & EVT_ARRIVED and last in (CtrlMode.TAKEOFF, CtrlMode.GOTO, CtrlMode.PATH, CtrlMode.ORBIT, CtrlMode.TRAJ) \
                    and not bits & EVT_MODE:
                if fs in (FS.TAKING_OFF, FS.FLYING):
                    self._commit(s, int(FS.FLYING), S_FLY_HOVER, Origin.SYSTEM, C.idx("SYS.ARRIVED"), t)
                return
            if fs in (FS.FLYING, FS.TAKING_OFF) or (fs in (FS.RTL, FS.LANDING) and not rt.sb["fs_auto"][s]):
                if not (fs == FS.FLYING and sub == S_FLY_HOVER):
                    self.propose_operator(s, int(FS.FLYING), S_FLY_HOVER, "OP.HOVER")
            elif fs in (FS.ELAND, FS.FAILSAFE, FS.CORRECTING, FS.RTL, FS.LANDING):
                self.reassert(np.array([s]))
            return
        if mode == CtrlMode.LAND:
            if last == CtrlMode.RTL and fs == FS.RTL:
                return  # RTL/FINAL 触地斜坡（M08 refgen 内部切换）
            if fs != FS.LANDING:
                self.propose_operator(s, int(FS.LANDING), S_LND_GOTO if phase == 0 else S_LND_DESC, "OP.LAND")
            return
        if mode == CtrlMode.RTL:
            if fs != FS.RTL:
                self.propose_operator(s, int(FS.RTL), S_RTL_CLIMB, "OP.RTL")
            return
        if mode in _FLY_SUB or mode == CtrlMode.TRAJ:
            if mode == CtrlMode.TRAJ:
                call = rt.current_call(s)
                want = _TRAJ_SUB.get(getattr(call, "op", ""), SUBV[(FS.FLYING, "PATH")])
            else:
                want = _FLY_SUB[mode]
            if (fs, sub) != (int(FS.FLYING), want):
                self.propose_operator(s, int(FS.FLYING), want, _MODE_CODE.get(mode, "OP.GOTO"))
            return
        if mode in (CtrlMode.ELAND, CtrlMode.DESCENT_FF) and fs not in (FS.ELAND, FS.FAILSAFE):
            self.reassert(np.array([s]))

    # ---------------------------------------------------------------- 碰撞（M08 contact 写 crash_sub；12 F35–F37）
    def _crash(self, act: np.ndarray) -> None:
        S, sb = self.rt.S, self.rt.sb
        cs = S.crash_sub[act]
        hit = act[(cs > 0) & (sb["fs"][act] != FS.CRASHED)]
        for s in hit:
            sub = int(S.crash_sub[s]) - 1
            self.propose(np.array([s]), int(FS.CRASHED), sub, Origin.AUTO, _CRASH_CODE.get(sub, "SAF.WORLD.COLLISION"),
                         key=8)

    # ---------------------------------------------------------------- FSM 定时器（FR-007；向量化筛选，逐机只在到期时执行）
    def _timers(self, act: np.ndarray, t: int) -> None:
        rt, sb = self.rt, self.rt.sb
        kind = sb["timer_kind"][act]
        due = act[(kind != TIMER_NONE) & (sb["deadline_ns"][act] <= t)]
        self.next_deadline = 1 << 62
        for s in due:
            s = int(s)
            k = int(sb["timer_kind"][s])
            fs, sub = int(sb["fs"][s]), int(sb["sub"][s])
            sb["timer_kind"][s] = TIMER_NONE
            if k == TIMER_PREFLIGHT and fs == FS.PREFLIGHT:
                items = rt.admission.preflight_items(s) if rt.admission is not None else []
                if items:
                    self._commit(s, int(FS.DISARMED), S_DIS_NOTREADY, Origin.SYSTEM, C.idx("SAF.FSM.PREFLIGHT_FAILED"), t)
                    rt.act_.disarm(np.array([s], np.int32), "preflight_failed")
                    rt.sink.add(s, C.idx("SAF.FSM.PREFLIGHT_FAILED"), t, detail=",".join(items), origin=Origin.SYSTEM)
                    rt.resolve_calls(np.array([s]), "failed", int(Reason.PREFLIGHT_FAILED), "preflight: " + ",".join(items))
                    sb["timer_kind"][s] = TIMER_NOTREADY
                    sb["deadline_ns"][s] = t + int(1e9)
                else:
                    takeoff = bool(sb["takeoff_pending"][s])
                    self._commit(s, int(FS.READY), 0, Origin.SYSTEM, C.idx("SYS.PREFLIGHT_OK"), t, no_timer=takeoff)
                    if takeoff:
                        ph = int(rt.S.ctrl_phase[s]) if int(rt.S.ctrl_mode[s]) == CtrlMode.TAKEOFF else 0
                        self._commit(s, int(FS.TAKING_OFF), S_TKO_CLIMB if ph >= 1 else S_TKO_SPOOL, Origin.SYSTEM,
                                     C.idx("OP.TAKEOFF"), t)
            elif k == TIMER_READY and fs == FS.READY:
                self._commit(s, int(FS.DISARMED), S_DIS_READY, Origin.SYSTEM, C.idx("SAF.FSM.PREFLIGHT_INACTION"), t)
                rt.act_.disarm(np.array([s], np.int32), "ready_timeout")
                rt.sink.add(s, C.idx("SAF.FSM.PREFLIGHT_INACTION"), t, origin=Origin.SYSTEM,
                            frm=(fs, sub), to=(int(FS.DISARMED), S_DIS_READY))
            elif k == TIMER_LANDED and fs == FS.LANDED:
                self._commit(s, int(FS.DISARMED), S_DIS_READY, Origin.SYSTEM, C.idx("SYS.LANDED_DISARM"), t)
                rt.act_.disarm(np.array([s], np.int32), "landed_disarm")
            elif k == TIMER_LOC and fs == FS.HOLD and sub == S_HOLD_LOC:
                self.propose(np.array([s]), int(FS.LANDING), S_LND_DESC, Origin.AUTO, "SAF.EST.LOC_LOST",
                             detail="LOC_LOST_30S")
            elif k == TIMER_NOTREADY and fs == FS.DISARMED and sub == S_DIS_NOTREADY:
                if _READY_LC[min(int(rt.S.lifecycle[s]), 15)]:
                    self._commit(s, int(FS.DISARMED), S_DIS_READY, Origin.SYSTEM, C.idx("SYS.LIFECYCLE"), t)
        live = act[sb["timer_kind"][act] != TIMER_NONE]
        self.next_deadline = int(sb["deadline_ns"][live].min()) if live.size else 1 << 62

    # ---------------------------------------------------------------- RTL 子阶段推进（FR-011；12 §4.4.6）
    def _rtl_phase(self, act: np.ndarray, t: int) -> None:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        r = act[sb["fs"][act] == FS.RTL]
        if r.size == 0:
            return
        P = rt.params.rtl
        pos = S.enu.pos[r]
        home = S.enu.home[r]
        sub = sb["sub"][r]
        z = pos[:, 2]
        dxy = np.hypot(pos[:, 0] - home[:, 0], pos[:, 1] - home[:, 1])
        z_rtl = S.z_rtl[r]
        for k, s in enumerate(r):
            s = int(s)
            sb_sub = int(sub[k])
            nxt = sb_sub
            if sb_sub == S_RTL_CLIMB and z[k] >= z_rtl[k] - P.climb_tol_m:
                need = rt.bat.climb_end_z(s) if rt.bat is not None else -np.inf
                if need > z_rtl[k] + P.climb_tol_m:
                    rt.act_.rtl(np.array([s], np.int32), "rtl_climb_recompute", z_rtl_m=float(need))
                    rt.sb["sup_expect"][s] = -1  # 已在 RTL：M08 只抬高 z_rtl，不产生模式事件
                else:
                    nxt = S_RTL_CRUISE
            if nxt == S_RTL_CRUISE and dxy[k] < P.cruise_done_m:
                nxt = S_RTL_DESCEND
            if nxt == S_RTL_DESCEND and z[k] - home[k, 2] < P.descend_alt_m + P.climb_tol_m:
                nxt = S_RTL_FINAL
            if nxt != sb_sub:
                self._commit(s, int(FS.RTL), nxt, Origin.SYSTEM, C.idx("SYS.RTL_PHASE"), t)

    # ================================================================== 仲裁（M09 §6.4.6）
    def resolve(self, t: int) -> np.ndarray:
        rt, sb, Cd = self.rt, self.rt.sb, self.C
        # ① OPERATOR：只查白名单与锁存，可以降级
        for s, fs1, sub1, ci, ex in Cd.operator:
            fs0 = int(sb["fs"][s])
            if ex.get("via_ready") and allowed(fs0, int(FS.READY), int(sb["latch"][s])):
                self._commit(s, int(FS.READY), 0, Origin.OPERATOR, ci, t, no_timer=True)
                fs0 = int(FS.READY)
            if not allowed(fs0, fs1, int(sb["latch"][s])):
                self._reject(s, fs0, fs1, t)
                self.reassert(np.array([s]))
                continue
            if (fs0, int(sb["sub"][s])) == (fs1, sub1) and not ex:
                continue
            self._commit(s, fs1, sub1, Origin.OPERATOR, ci, t)
            if ex.get("preflight"):
                sb["timer_kind"][s] = TIMER_PREFLIGHT
                sb["deadline_ns"][s] = t + int(rt.params.fsm.preflight_s * 1e9)
                self.next_deadline = min(self.next_deadline, int(sb["deadline_ns"][s]))
                sb["takeoff_pending"][s] = bool(ex.get("takeoff"))
            if ex.get("lock"):
                sb["locked"][s] = True
                rt.lease_suspend(np.array([s]))
            if ex.get("unlock") and sb["locked"][s]:
                sb["locked"][s] = False
                rt.lease_resume(np.array([s]))
            if ex.get("resume_motion"):
                rt.act_.resume_hover(np.array([s], np.int32), "operator_resume")
            motion = ex.get("motion")
            if motion is not None:  # escalate（ext）：M08 不为该命令执行运动，由 Supervisor 下发
                getattr(rt.act_, motion)(np.array([s], np.int32), "escalate")
        # ② SYSTEM 与 AUTO：提交 OPERATOR 之后的状态上仲裁
        idx = np.flatnonzero(Cd.key > NONE_KEY)
        acc_list: list[int] = []
        if idx.size:
            src = sb["fs"][idx].astype(np.int64)
            dst = Cd.fs[idx].astype(np.int64)
            ssub = sb["sub"][idx]
            auto = Cd.origin[idx] == Origin.AUTO
            latch = sb["latch"][idx]
            ok = WHITELIST[src, dst] | (src == dst)
            ok &= ~(((latch & LATCH_ELAND) != 0) & ~ELAND_OK[dst] & (src != dst))
            ok &= ~(((latch & LATCH_FAILSAFE) != 0) & ~FAIL_OK[dst] & (src != dst))
            cur_rank = np.where((src == FS.DISARMED) & (ssub == S_DIS_KILLED), KILL_RANK, RANK[src]).astype(np.int16)
            ok &= ~auto | (Cd.key[idx].astype(np.int16) > cur_rank) | is_restore(src, ssub, dst) | (src == dst)
            same = auto & (src == dst) & (ssub == Cd.sub[idx])
            acc_list = idx[ok & ~same].tolist()
            for k in np.flatnonzero(same | ~ok):
                s = int(idx[k])
                if same[k]:
                    self._relabel(s, t)
                else:
                    self._reject(s, int(src[k]), int(dst[k]), t)
                    self._emit_candidate(s, t, transition=None)
        acc = np.asarray(acc_list, np.int64)
        if acc.size:
            fs0 = sb["fs"][acc].copy()
            sub0 = sb["sub"][acc].copy()
            self._commit_many(acc, Cd.fs[acc], Cd.sub[acc], Cd.origin[acc], Cd.code[acc], t, Cd.target[acc])
            self._emit_many(acc, fs0, sub0, t)
        if acc.size:
            self._actions(acc, t)
        for sl, ci, v, th, det in Cd.losers:
            rt.sink.add(sl, ci, t, value=None if v != v else v, threshold=None if th != th else th, detail=det,
                        rank=int(SEV[int(sb["fs"][sl])]))
        self.C.clear()
        return acc

    def _emit_candidate(self, s: int, t: int, transition: tuple | None) -> None:
        rt, Cd = self.rt, self.C
        v = float(Cd.value[s])
        th = float(Cd.thr[s])
        if transition is not None:
            to = transition[1]
            rank = KILL_RANK if (to[0] == FS.DISARMED and to[1] == S_DIS_KILLED) else int(RANK[to[0]])
            rt.sink.add(s, int(Cd.code[s]), t, value=None if v != v else v, threshold=None if th != th else th,
                        detail=Cd.detail.get(s), frm=transition[0], to=to, origin=int(Cd.origin[s]), rank=max(rank, 0))
        else:
            rt.sink.add(s, int(Cd.code[s]), t, value=None if v != v else v, threshold=None if th != th else th,
                        detail=Cd.detail.get(s), origin=int(Cd.origin[s]), rank=int(SEV[int(rt.sb["fs"][s])]))

    def _emit_many(self, acc: np.ndarray, fs0: np.ndarray, sub0: np.ndarray, t: int) -> None:
        """批量转移的 SafetyEvent（向量取数、逐条只做一次元组构造）。"""
        rt, Cd = self.rt, self.C
        f1 = Cd.fs[acc]
        s1 = Cd.sub[acc]
        rank = np.where((f1 == FS.DISARMED) & (s1 == S_DIS_KILLED), KILL_RANK, np.maximum(RANK[f1], 0))
        val = Cd.value[acc]
        thr = Cd.thr[acc]
        det = Cd.detail
        rt.sink.add_transitions(acc.tolist(), Cd.code[acc].tolist(), t, np.where(np.isnan(val), None, val).tolist(),
                                np.where(np.isnan(thr), None, thr).tolist(), [det.get(x) for x in acc.tolist()],
                                fs0.tolist(), sub0.tolist(), f1.tolist(), s1.tolist(), Cd.origin[acc].tolist(), rank.tolist())

    def _relabel(self, s: int, t: int) -> None:
        """同状态重标记（M09 §6.4.6）：不转移，置 fs_auto、更新原因并发事件。"""
        sb, Cd = self.rt.sb, self.C
        sb["fs_auto"][s] = True
        sb["reason"][s] = Cd.code[s]
        self._emit_candidate(s, t, transition=None)

    def _reject(self, s: int, fs0: int, fs1: int, t: int) -> None:
        rt = self.rt
        now = rt.wall_ns()
        if now - int(self.rejected_wall[s]) < int(rt.params.fsm.rejected_rate_s * 1e9) and self.rejected_wall[s] != 0:
            return
        self.rejected_wall[s] = max(now, 1)
        rt.sink.add(s, C.idx("SAF.FSM.REJECTED"), t, detail=f"{FS(fs0).name}->{FS(fs1).name}", origin=Origin.AUTO,
                    rank=int(SEV[fs0]))

    # ---------------------------------------------------------------- 提交一次转移
    def _commit(self, s: int, fs1: int, sub1: int, origin: Origin, code: int, t: int, *, no_timer: bool = False,
                target: np.ndarray | None = None) -> None:
        rt, sb = self.rt, self.rt.sb
        fs0, sub0 = int(sb["fs"][s]), int(sb["sub"][s])
        if (fs0, sub0) == (fs1, sub1) and origin != Origin.AUTO:
            return
        self.flags_dirty = True
        sb["fs"][s] = fs1
        sb["sub"][s] = sub1
        if fs1 != fs0 or origin != Origin.SYSTEM:
            sb["fs_auto"][s] = origin == Origin.AUTO
        sb["reason"][s] = code
        sb["t_enter_ns"][s] = t
        for f in ("te_since", "pe_since", "thr_since", "trk_since"):
            sb[f][s] = np.nan
        if fs1 == FS.ELAND:
            sb["latch"][s] |= LATCH_ELAND
        elif fs1 == FS.FAILSAFE:
            sb["latch"][s] |= LATCH_FAILSAFE
        elif fs1 in (FS.LANDED, FS.DISARMED, FS.UNKNOWN):
            sb["latch"][s] = 0
        if fs1 in (FS.LANDED, FS.DISARMED, FS.CRASHED, FS.UNKNOWN) and sb["locked"][s]:
            sb["locked"][s] = False
            rt.lease_resume(np.array([s]))
        if fs1 != fs0:
            sb["timer_kind"][s] = TIMER_NONE
            if fs1 != FS.PREFLIGHT:
                sb["takeoff_pending"][s] = False
            if fs1 == FS.READY and not no_timer:
                sb["timer_kind"][s] = TIMER_READY
                sb["deadline_ns"][s] = t + int(rt.params.fsm.ready_autodisarm_s * 1e9)
            elif fs1 == FS.LANDED:
                sb["timer_kind"][s] = TIMER_LANDED
                sb["deadline_ns"][s] = t + int(rt.params.fsm.landed_disarm_s * 1e9)
            elif fs1 == FS.DISARMED and rt.bat is not None:
                rt.bat.reset_flight(np.array([s]))
        if fs1 == FS.HOLD and sub1 == S_HOLD_LOC:
            sb["timer_kind"][s] = TIMER_LOC
            sb["deadline_ns"][s] = t + int(rt.params.fsm.loc_lost_land_s * 1e9)
        if sb["timer_kind"][s] != TIMER_NONE:
            self.next_deadline = min(self.next_deadline, int(sb["deadline_ns"][s]))
        if fs1 == FS.CORRECTING:
            sb["correct_since_ns"][s] = t if fs0 != FS.CORRECTING else sb["correct_since_ns"][s]
            if target is not None and np.all(np.isfinite(target)):
                sb["correct_target"][s] = target
        elif fs0 == FS.CORRECTING:
            sb["correct_target"][s] = np.nan
        if fs0 == FS.HOLD and sub0 == S_HOLD_SEP and not (fs1 == FS.HOLD and sub1 == S_HOLD_SEP):
            sb["yield_state"][s] = 0
        self.transitions += 1
        self.last_resolve.append((s, fs0, fs1, int(origin), rt.tick))
        if len(self.last_resolve) > 4096:
            del self.last_resolve[:2048]
        rt.sink.state_change(s, fs0, sub0, fs1, sub1, code, t, bool(sb["fs_auto"][s]))

    def _commit_many(self, s: np.ndarray, fs1: np.ndarray, sub1: np.ndarray, origin: np.ndarray, code: np.ndarray, t: int,
                     targets: np.ndarray | None = None) -> None:
        """`_commit` 的向量化版本（SYSTEM 与 AUTO 的批量转移；语义逐项相同，M09-NFR-004 转移风暴）。"""
        rt, sb = self.rt, self.rt.sb
        fs0 = sb["fs"][s].copy()
        sub0 = sb["sub"][s].copy()
        fs1 = np.asarray(fs1, np.uint8)
        sub1 = np.asarray(sub1, np.uint8)
        origin = np.asarray(origin, np.uint8)
        self.flags_dirty = True
        sb["fs"][s] = fs1
        sb["sub"][s] = sub1
        chg = fs1 != fs0
        upd = chg | (origin != Origin.SYSTEM)
        sb["fs_auto"][s[upd]] = origin[upd] == Origin.AUTO
        sb["reason"][s] = code
        sb["t_enter_ns"][s] = t
        for f in ("te_since", "pe_since", "thr_since", "trk_since"):
            sb[f][s] = np.nan
        sb["latch"][s[fs1 == FS.ELAND]] |= LATCH_ELAND
        sb["latch"][s[fs1 == FS.FAILSAFE]] |= LATCH_FAILSAFE
        clr = (fs1 == FS.LANDED) | (fs1 == FS.DISARMED) | (fs1 == FS.UNKNOWN)
        sb["latch"][s[clr]] = 0
        unl = s[(clr | (fs1 == FS.CRASHED)) & sb["locked"][s]]
        if unl.size:
            sb["locked"][unl] = False
            rt.lease_resume(unl)
        c = s[chg]
        if c.size:
            f1 = fs1[chg]
            sb["timer_kind"][c] = TIMER_NONE
            sb["takeoff_pending"][c[f1 != FS.PREFLIGHT]] = False
            r = c[f1 == FS.READY]
            sb["timer_kind"][r] = TIMER_READY
            sb["deadline_ns"][r] = t + int(rt.params.fsm.ready_autodisarm_s * 1e9)
            ld = c[f1 == FS.LANDED]
            sb["timer_kind"][ld] = TIMER_LANDED
            sb["deadline_ns"][ld] = t + int(rt.params.fsm.landed_disarm_s * 1e9)
            dis = c[f1 == FS.DISARMED]
            if dis.size and rt.bat is not None:
                rt.bat.reset_flight(dis)
            if r.size or ld.size:
                self.next_deadline = min(self.next_deadline, t + int(min(rt.params.fsm.ready_autodisarm_s,
                                                                         rt.params.fsm.landed_disarm_s) * 1e9))
        loc = s[(fs1 == FS.HOLD) & (sub1 == S_HOLD_LOC)]
        if loc.size:
            sb["timer_kind"][loc] = TIMER_LOC
            sb["deadline_ns"][loc] = t + int(rt.params.fsm.loc_lost_land_s * 1e9)
            self.next_deadline = min(self.next_deadline, t + int(rt.params.fsm.loc_lost_land_s * 1e9))
        cor = fs1 == FS.CORRECTING
        newc = s[cor & (fs0 != FS.CORRECTING)]
        sb["correct_since_ns"][newc] = t
        if targets is not None and cor.any():
            tg = np.asarray(targets, np.float64).reshape(-1, 3)[cor]
            okt = np.all(np.isfinite(tg), axis=1)
            sb["correct_target"][s[cor][okt]] = tg[okt]
        sb["correct_target"][s[~cor & (fs0 == FS.CORRECTING)]] = np.nan
        ysep = s[(fs0 == FS.HOLD) & (sub0 == S_HOLD_SEP) & ~((fs1 == FS.HOLD) & (sub1 == S_HOLD_SEP))]
        sb["yield_state"][ysep] = 0
        self.transitions += int(s.size)
        tk = rt.tick
        sl, f0, f1, o = s.tolist(), fs0.tolist(), fs1.tolist(), origin.tolist()
        self.last_resolve.extend(zip(sl, f0, f1, o, [tk] * len(sl), strict=True))
        if len(self.last_resolve) > 4096:
            del self.last_resolve[:-2048]
        n = len(sl)
        rt.sink.states.extend(zip(sl, f0, sub0.tolist(), f1, sub1.tolist(), np.asarray(code).tolist(), [t] * n,
                                  sb["fs_auto"][s].tolist(), strict=True))

    # ---------------------------------------------------------------- 动作（SafetyActuator）与在途调用
    def _actions(self, acc: np.ndarray, t: int) -> None:
        rt, sb, Cd = self.rt, self.rt.sb, self.C
        orig = Cd.origin[acc]
        fs = sb["fs"][acc]
        sub = sb["sub"][acc]
        auto = orig == Origin.AUTO
        codes = Cd.code[acc]
        a = rt.act_
        a32 = acc.astype(np.int32)
        m = auto & (fs == FS.HOLD)
        a.hold(a32[m], "auto_hold")
        m = auto & (fs == FS.CORRECTING)
        if m.any():
            a.correct(a32[m], sb["correct_target"][acc[m]], 0, "geofence")
        m = auto & (fs == FS.RTL)
        a.rtl(a32[m], "auto_rtl")
        m = auto & (fs == FS.LANDING)
        a.land(a32[m], "auto_land")
        m = auto & (fs == FS.ELAND)
        a.eland(a32[m], "eland")
        m = auto & (fs == FS.FAILSAFE)
        a.failsafe(a32[m], "failsafe")
        m = auto & (((fs == FS.DISARMED) & (sub == S_DIS_KILLED)) | (fs == FS.CRASHED))
        a.kill(a32[m], "kill")
        m = auto & (fs == FS.FLYING)
        a.resume_hover(a32[m], "restore")
        # 在途调用（FR-010）：安全抢占 failed 204，碰撞 failed 208，watchdog canceled 209
        pre = auto & (fs != FS.FLYING)
        wd = pre & (codes == C.idx("SAF.LINK.WATCHDOG"))
        crash = pre & (fs == FS.CRASHED)
        other = pre & ~wd & ~crash
        rt.resolve_calls(acc[wd], "canceled", int(Reason.WATCHDOG), "SAF.LINK.WATCHDOG")
        rt.resolve_calls(acc[crash], "failed", int(Reason.CRASHED), "crashed")
        rt.resolve_calls(acc[other], "failed", int(Reason.PREEMPTED_BY_SAFETY), "preempted by safety")

    def reassert(self, slots: np.ndarray) -> None:
        """运动与 FSM 不一致时（竞态）按当前状态重新下发 Supervisor 动作。"""
        rt, sb = self.rt, self.rt.sb
        for s in np.asarray(slots, np.int64):
            s = int(s)
            fs, sub = int(sb["fs"][s]), int(sb["sub"][s])
            a = np.array([s], np.int32)
            if fs == FS.ELAND:
                rt.act_.eland(a, "reassert")
            elif fs == FS.FAILSAFE:
                rt.act_.failsafe(a, "reassert")
            elif fs == FS.HOLD:
                rt.act_.hold(a, "reassert")
            elif fs == FS.CORRECTING and np.all(np.isfinite(sb["correct_target"][s])):
                rt.act_.correct(a, sb["correct_target"][s][None], 0, "reassert")
            elif fs == FS.RTL and sb["fs_auto"][s]:
                rt.act_.rtl(a, "reassert")
            elif fs == FS.LANDING and sb["fs_auto"][s]:
                rt.act_.land(a, "reassert")
            elif (fs == FS.DISARMED and sub == S_DIS_KILLED) or fs == FS.CRASHED:
                rt.act_.kill(a, "reassert")

    # ---------------------------------------------------------------- 标志派生（FR-008）
    def _flags(self, act: np.ndarray) -> None:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        fs = sb["fs"][act]
        fsub = sb["sub"][act]
        rank = np.where((fs == FS.DISARMED) & (fsub == S_DIS_KILLED) & sb["fs_auto"][act], KILL_RANK, RANK[fs])
        sb["flag_failsafe"][act] = sb["fs_auto"][act] | (rank >= 5)
        lc_ok = _READY_LC[np.minimum(S.lifecycle[act], 15)]
        fm = sb["fault_mask"][act]
        sb["flag_fcu"][act] = lc_ok & ((fm & rt.FAULT_FCU) == 0)
        sb["flag_loc_ok"][act] = (fm & rt.FAULT_GNSS) == 0
        sb["flag_loc_deg"][act] = False
        sb["flag_alert"][act] = ((sb["cond"][act] & np.uint64(WARN_OR_ABOVE_MASK)) != 0) | sb["flag_loc_deg"][act] | ~lc_ok
        sb["severity"][act] = SEV[fs]


def C_idx(code: str | int) -> int:
    return C.idx(code) if isinstance(code, str) else int(code)

