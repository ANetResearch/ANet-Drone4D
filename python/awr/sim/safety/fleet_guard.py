"""fleet_guard.0–3 stage（10 Hz【仿真】，order 130–133，phase 4、9、14、19）：最小间距、CPA 让行、机间碰撞
（M09 §6.11；FR-080 至 FR-085；12 §5.10）。只写候选与条件位。

- 第 k 片只以 `slot % 4 == k` 的机体为主机、对端取全体中 slot 更大者，4 片合起来每个无序对每周期恰好处理一次；每片单独
  重建网格。`sep_m`、`cpa_min_m`、`sep_mate` 在第 0 片开始时清空、第 3 片结束时定稿；CONFLICT 边沿、重新布防与恢复判定在
  定稿后执行；AVOIDING、VIOLATION、碰撞候选由发现它的分片立即提出（FR-085）。
- 让行优先级 K1–K5（12 §5.10.2，键值大者优先，完全相同时 agent_no 小者优先）：K1 机动受限（ELAND、FAILSAFE、LANDING、
  TAKING_OFF 为 2，自动 RTL 为 1）、K2 任务优先级（M10 经 `set_task_priority` 提供，缺省 0）、K3 `−round(soc/0.05)`、
  K4 控制方类别（OPERATOR 4、AGENT 3、MISSION 2、SWARM 1、NONE 0）、K5 agent_no。
- 低优先级方已不在 FLYING（例如已让行悬停）而冲突仍在时，高优先级方同样让行（D1 无避让机动，否则高优先级方会撞上
  悬停的一方；本文设定，见实现报告偏差表）。
- 让行状态：YIELD（HOLD/SEPARATION）→ 距离 > 5 m 且 CPA > 3 m 持续 2 s 后恢复 FLYING/HOVER；同一对 60 s 内第 3 次让行时
  发 OSCILLATION 并保持 HOLD（等待操作员）。双方 K1 都为 2 时无人可让，只发事件。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from awr.contracts.enums import Owner

from . import kernels as KN
from .flight_fsm import S_FLY_HOVER, S_HOLD_SEP, Origin
from .state import COND, FS

if TYPE_CHECKING:
    from .service import SafetyRuntime

__all__ = ["FleetGuard", "priority_keys"]

YIELD_NONE, YIELD_ON, YIELD_OSC = 0, 1, 2
_K4 = np.zeros(8, np.int64)
_K4[int(Owner.OPERATOR)] = 4
_K4[int(Owner.AGENT)] = 3
_K4[int(Owner.MISSION)] = 2
_K4[int(Owner.SWARM)] = 1
_K1_2 = (int(FS.ELAND), int(FS.FAILSAFE), int(FS.LANDING), int(FS.TAKING_OFF))
MAX_PAIRS = 4096


def priority_keys(fs: np.ndarray, fs_auto: np.ndarray, task_prio: np.ndarray, soc: np.ndarray, owner: np.ndarray,
                  agent_no: np.ndarray) -> np.ndarray:
    """(n, 5) 键矩阵；按字典序比较，大者优先（K5 取 −agent_no）。"""
    k1 = np.where(np.isin(fs, _K1_2), 2, np.where((fs == FS.RTL) & fs_auto, 1, 0))
    k3 = -np.round(np.asarray(soc, np.float64) / 0.05).astype(np.int64)
    k4 = _K4[np.clip(np.asarray(owner, np.int64), 0, 7)]
    return np.stack([k1, np.asarray(task_prio, np.int64), k3, k4, -np.asarray(agent_no, np.int64)], 1)


def _lex_less(a: np.ndarray, b: np.ndarray) -> bool:
    for x, y in zip(a, b, strict=True):
        if x != y:
            return bool(x < y)
    return False


class FleetGuard:
    def __init__(self, rt: SafetyRuntime) -> None:
        self.rt = rt
        self.pairs = np.zeros((MAX_PAIRS, KN.PAIR_COLS))
        self.task_prio = np.zeros(rt.S.capacity, np.int64)
        self.use_numba = KN.HAVE_NUMBA and rt.kernel == "numba"
        self.last_cand = 0
        self.min_sep_seen = np.inf
        # 逐机"与任一机对的最小间距"（此前为机对字典 pair_min，1000 架时定稿逐对 Python 更新约 0.5 ms，checkpoint 逐对编码
        # 约 1.5 ms）。`min_separation(slots)` 求"涉及 slots 中任一机的机对"的最小值，等于各机最小值的最小值，两种存法结果相同
        self.slot_min = np.full(rt.S.capacity, np.inf)
        self.pair_gen = 0  # 修改代数（诊断）

    def reset(self) -> None:
        self.min_sep_seen = np.inf

    def reset_pairs(self) -> None:
        self.slot_min[:] = np.inf
        self.pair_gen += 1

    @property
    def pair_min(self) -> dict[tuple[int, int], float]:
        """兼容视图：{(s, s): 该机最小间距}（只读快照）。"""
        fin = np.flatnonzero(np.isfinite(self.slot_min))
        return {(int(s), int(s)): float(self.slot_min[s]) for s in fin}

    @pair_min.setter
    def pair_min(self, pm: dict[tuple[int, int], float]) -> None:
        """由机对表设置（旧 checkpoint 与测试）：两端各取最小。"""
        self.slot_min[:] = np.inf
        for (a, b), v in pm.items():
            for s_ in (int(a), int(b)):
                if 0 <= s_ < self.slot_min.size:
                    self.slot_min[s_] = min(self.slot_min[s_], float(v))
        self.pair_gen += 1

    def _idx(self) -> np.ndarray:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        act = rt.act_idx
        return act[S.in_air[act] & (sb["fs"][act] != FS.CRASHED)]

    def step(self, ctx: Any, k: int) -> None:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        P = rt.params.sep
        idx = self._idx()
        if k == 0:
            act = rt.act_idx
            sb["cyc_sep"][act] = np.inf
            sb["cyc_cpa"][act] = np.inf
            sb["cyc_mate"][act] = -1
            sb["cyc_cpa_mate"][act] = -1
        if idx.size >= 2:
            pos = np.ascontiguousarray(S.enu.pos)
            vel = np.ascontiguousarray(S.enu.vel)
            rcol = rt.collision_r
            fn = KN.fleet_scan if self.use_numba else KN.fleet_scan_np
            npairs, ncand = fn(pos, vel, idx.astype(np.int64), int(k), int(P.n_shards), float(P.cell_m),
                               float(P.cpa_horizon_s), float(P.min_sep_m), float(P.warn_m), float(P.zband_m), rcol,
                               float(P.sweep_s), sb["cyc_sep"], sb["cyc_mate"], sb["cyc_cpa"], sb["cyc_cpa_mate"],
                               self.pairs, MAX_PAIRS)
            self.last_cand = int(ncand)
            if npairs:
                self._conflicts(self.pairs[:int(npairs)].copy())
        if k == P.n_shards - 1:
            self._finalize()

    # ---------------------------------------------------------------- 冲突对（本片立即提出候选）
    def _conflicts(self, pairs: np.ndarray) -> None:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        P = rt.params.sep
        t = rt.t_ns
        t_s = t * 1e-9
        rcol = rt.collision_r
        for row in pairs:
            i, j = int(row[0]), int(row[1])
            d0, cpa, sw = float(row[2]), float(row[3]), float(row[5])
            if sw < rcol[i] + rcol[j]:
                for a, b in ((i, j), (j, i)):
                    rt.fsm.propose(np.array([a]), int(FS.CRASHED), 2, Origin.AUTO, "SAF.SEP.COLLISION", key=8,
                                   value=sw, thr=float(rcol[i] + rcol[j]), detail=S.ids[b])
                continue
            viol = d0 < P.min_sep_m
            code = "SAF.SEP.VIOLATION" if viol else "SAF.SEP.AVOIDING"
            cond = "SEP_VIOLATION" if viol else "SEP_AVOIDING"
            both = np.array([i, j])
            keys = priority_keys(sb["fs"][both], sb["fs_auto"][both], self.task_prio[both],
                                 S.blocks["battery"]["soc"][both], rt.owner_codes()[both], S.agent_no[both])
            bit = np.uint64(1 << COND[cond])
            fresh = [(sb["cond"][a] & bit) == 0 for a in (i, j)]
            rt.set_cond(both, cond, True)
            if keys[0, 0] == 2 and keys[1, 0] == 2:  # 无人可让：只发事件（12 §5.10.3 第 4 条）
                for (a, b), f in zip(((i, j), (j, i)), fresh, strict=True):
                    if f:
                        rt.sink.add(a, rt.code(code), t, value=cpa, threshold=P.min_sep_m, detail="NO_YIELD:" + str(S.ids[b]))
                continue
            loser, winner = (i, j) if _lex_less(keys[0], keys[1]) else (j, i)
            if sb["fs"][loser] != FS.FLYING:
                # 低优先级方已无法再让（已悬停或处于其他状态）而冲突仍在：高优先级方也让行（否则会撞上悬停的一方）
                if sb["fs"][winner] != FS.FLYING or keys[0 if winner == i else 1, 0] == 2:
                    continue
                loser, winner = winner, loser
            ok = rt.fsm.propose(np.array([loser]), int(FS.HOLD), S_HOLD_SEP, Origin.AUTO, code, value=cpa if not viol else d0,
                                thr=P.min_sep_m, detail=S.ids[winner])
            if ok[0]:
                self._record_yield(loser, winner, t_s)

    def _record_yield(self, s: int, mate: int, t_s: float) -> None:
        rt, sb = self.rt, self.rt.sb
        P = rt.params.sep
        hist = sb["yield_hist"][s]
        same = int(sb["yield_mate"][s]) == mate
        if not same:
            hist[:] = -np.inf
        hist[:-1] = hist[1:].copy()
        hist[-1] = t_s
        sb["yield_hist"][s] = hist
        sb["yield_mate"][s] = mate
        n_recent = int(np.count_nonzero(t_s - hist <= P.osc_window_s))
        if n_recent >= P.osc_count:
            sb["yield_state"][s] = YIELD_OSC
            rt.sink.add(s, rt.code("SAF.SEP.OSCILLATION"), rt.t_ns, value=float(n_recent), threshold=float(P.osc_count),
                        detail=str(rt.S.ids[mate]))
        else:
            sb["yield_state"][s] = YIELD_ON

    # ---------------------------------------------------------------- 周期定稿（第 3 片结束）
    def _finalize(self) -> None:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        P = rt.params.sep
        act = rt.act_idx
        if act.size == 0:
            return
        t = rt.t_ns
        t_s = t * 1e-9
        sep = sb["cyc_sep"][act]
        sb["sep_m"][act] = sep
        sb["sep_mate"][act] = sb["cyc_mate"][act]
        sb["cpa_min_m"][act] = sb["cyc_cpa"][act]
        sb["sep_cpa_mate"][act] = sb["cyc_cpa_mate"][act]
        fin = np.isfinite(sep)
        if fin.any():
            self.min_sep_seen = min(self.min_sep_seen, float(sep[fin].min()))
            # 逐机记录"与任一机对的最小间距"（机对两端各取最小；向量化，FX2-R2）
            sf = act[fin]
            mate = sb["sep_mate"][sf].astype(np.int64)
            v = sb["sep_m"][sf].astype(np.float64)
            ok = mate >= 0
            if ok.any():
                sf, mate, v = sf[ok], mate[ok], v[ok]
                sm = self.slot_min
                if (v < sm[sf]).any() or (v < sm[mate]).any():
                    np.minimum.at(sm, sf, v)
                    np.minimum.at(sm, mate, v)
                    self.pair_gen += 1
        # CONFLICT：逐机边沿，> 12 m 持续 2 s 重新布防
        warn = sep < P.warn_m
        was = sb["sep_warn"][act]
        new = act[warn & ~was]
        if new.size:
            sb["sep_warn"][new] = True
            rt.set_cond(new, "SEP_CONFLICT", True)
            for s in new:
                m = int(sb["sep_mate"][s])
                rt.sink.add(int(s), rt.code("SAF.SEP.CONFLICT"), t, value=float(sb["sep_m"][s]), threshold=P.warn_m,
                            detail=S.ids[m] if m >= 0 else None)
        far = sep > P.rearm_m
        rs = sb["sep_rearm_since"][act]
        rs = np.where(far & was, np.where(np.isnan(rs), t_s, rs), np.nan)
        sb["sep_rearm_since"][act] = rs
        clr = act[was & far & (t_s - np.nan_to_num(rs, nan=t_s) >= P.rearm_s)]
        if clr.size:
            sb["sep_warn"][clr] = False
            rt.set_cond(clr, "SEP_CONFLICT", False)
        # AVOIDING/VIOLATION 条件位：CPA 与距离均已安全时清除
        cpa = sb["cpa_min_m"][act]
        safe = (cpa >= P.min_sep_m) & (sep >= P.min_sep_m)
        rt.set_cond(act[safe], "SEP_AVOIDING", False)
        rt.set_cond(act[safe], "SEP_VIOLATION", False)
        # 恢复：HOLD/SEPARATION 且未振荡，距离 > 5 m 且 CPA > 3 m 持续 2 s
        yh = act[(sb["fs"][act] == FS.HOLD) & (sb["sub"][act] == S_HOLD_SEP)]
        if yh.size:
            ok = (sb["sep_m"][yh] > P.recover_m) & (sb["cpa_min_m"][yh] > P.min_sep_m)
            since = sb["sep_ok_since"][yh]
            since = np.where(ok, np.where(np.isnan(since), t_s, since), np.nan)
            sb["sep_ok_since"][yh] = since
            done = yh[ok & (t_s - np.nan_to_num(since, nan=t_s) >= P.recover_s - 1e-9)
                      & (sb["yield_state"][yh] != YIELD_OSC)]
            if done.size:
                rt.fsm.propose(done, int(FS.FLYING), S_FLY_HOVER, Origin.AUTO, "SAF.SEP.RESTORED")
        other = act[~((sb["fs"][act] == FS.HOLD) & (sb["sub"][act] == S_HOLD_SEP))]
        sb["sep_ok_since"][other] = np.nan

    def set_task_priority(self, slots: np.ndarray, prio: np.ndarray) -> None:
        """M10 提供的任务优先级（K2，0–9）。"""
        self.task_prio[np.asarray(slots, np.int64)] = np.clip(np.asarray(prio, np.int64), 0, 9)

    def min_separation(self, slots: np.ndarray | None = None) -> float:
        if slots is None:
            return float(self.min_sep_seen)
        sl = np.asarray(slots, np.int64)
        sl = sl[(sl >= 0) & (sl < self.slot_min.size)]
        return float(self.slot_min[sl].min()) if sl.size else float("inf")
