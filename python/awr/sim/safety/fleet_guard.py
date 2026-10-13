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
- 解锁（D2-FX-R2-tasking 第 2 次，D2-AC-16 验收第 1 轮：两机互相让行悬停在 1.8–2.7 m 处约 290 s 不解锁，随后一方能量 RTL 撞机）：让行悬停的
  多旋翼（含 MC 模式的 VTOL）在下列情形由本守卫下发一次避让位移（SafetyActuator `correct`，状态保持 HOLD/SEPARATION，到点后悬停，
  间距恢复后按上一条照常恢复）：①与对端都已静止而间距 ≤ recover_m 持续 `ESC_AFTER_S`（双方都让行时由优先级低的一方位移）；
  ②对端不让行（自动 RTL、着陆、起飞等 K1 较高者）且仍在逼近（CPA < min_sep）。位移方向：对端在作垂直运动（如 RTL 爬升）时水平
  离开 `ESC_DH_M`，否则垂直错开 `ESC_DZ_M`（对端在上方时下降，否则爬升；越过高度上限或 DSM 净空不足时取另一方向或水平）；同一机
  `ESC_RETRY_S` 内至多一次。事件 `SAF.SEP.AVOIDING`，detail 为 `ESCAPE:<对端>`。
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
ESC_AFTER_S = 2.0      # 双方都静止让行而间距未恢复的持续时间（之后位移解锁）
ESC_RETRY_S = 6.0      # 同一机两次避让位移的最小间隔
ESC_DZ_M = 8.0         # 垂直错开（> recover_m，与水平残余间距合成后可恢复）
ESC_DH_M = 8.0         # 水平离开（对端在作垂直运动时）
ESC_CLEAR_M = 5.0      # 避让目标点的 DSM 净空
ESC_STILL_MPS = 0.5    # 静止判据（水平与垂直速度）
FW_BIT = 16            # Fidelity.FW：固定翼与 FW 模式的 VTOL（不能悬停，不做避让位移）
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
        self._stuck: dict[int, float] = {}   # 让行悬停而间距未恢复的起始时刻（解锁判定，非 checkpoint 状态：恢复后重新计时）
        self._esc: dict[int, float] = {}     # 最近一次避让位移的时刻
        self.escapes = 0

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
        # 片数取 M08 按档展开后的实际值（ctx.shard = (k, n)）：sandbox100 档 N ≤ 12 时只有 1 片（stages/rates.py），此前仍按
        # params 的 4 片扫描与定稿——只检查 slot % 4 == 0 为主机的机对，且第 3 片从不运行，`_finalize`（间距定稿、CONFLICT、让行
        # 恢复）永不执行，让行悬停的两机永不恢复（D2-FX-R2-tasking 第 2 次：第 1 轮 D2-AC-16 区域巡查 + 巡边的 290 s 死锁与随后的撞机）
        sh = getattr(ctx, "shard", None)
        n = int(sh[1]) if isinstance(sh, (tuple, list)) and len(sh) == 2 and int(sh[1]) >= 1 else int(P.n_shards)
        k = int(k) % n
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
            npairs, ncand = fn(pos, vel, idx.astype(np.int64), int(k), n, float(P.cell_m),
                               float(P.cpa_horizon_s), float(P.min_sep_m), float(P.warn_m), float(P.zband_m), rcol,
                               float(P.sweep_s), sb["cyc_sep"], sb["cyc_mate"], sb["cyc_cpa"], sb["cyc_cpa_mate"],
                               self.pairs, MAX_PAIRS)
            self.last_cand = int(ncand)
            if npairs:
                self._conflicts(self.pairs[:int(npairs)].copy())
        if k == n - 1:
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
        self._unlock(act, t_s)

    # ---------------------------------------------------------------- 解锁：让行悬停的避让位移
    def _hovers(self, s: int) -> bool:
        S = self.rt.S
        return int(S.frame_kind[s]) != 1 and not (int(S.fidelity[s]) & FW_BIT)

    def _unlock(self, act: np.ndarray, t_s: float) -> None:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        P = rt.params.sep
        hold = act[(sb["fs"][act] == FS.HOLD) & (sb["sub"][act] == S_HOLD_SEP)]
        live = {int(x) for x in hold}
        for k in [k for k in self._stuck if k not in live]:
            del self._stuck[k]
        for k in [k for k in self._esc if k not in live and t_s - self._esc[k] > ESC_RETRY_S]:
            del self._esc[k]
        if not live:
            return
        vel = S.enu.vel
        for s in sorted(live):
            mate = int(sb["sep_mate"][s])
            cmate = int(sb["sep_cpa_mate"][s])
            sep = float(sb["sep_m"][s])
            if mate < 0 or not np.isfinite(sep):
                self._stuck.pop(s, None)
                continue
            v_m = np.asarray(vel[mate], float)
            moving = float(np.linalg.norm(v_m[:3])) > 1.0
            threat = (cmate >= 0 and float(sb["cpa_min_m"][s]) < P.min_sep_m and cmate not in live and sb["fs"][cmate] != FS.FLYING
                      and float(np.linalg.norm(np.asarray(vel[cmate], float)[:3])) > 1.0)
            if sep > P.recover_m and not threat:
                self._stuck.pop(s, None)
                continue
            if threat:
                other, esc = cmate, s            # 对端不让行而仍在逼近：本机立即离开
            else:
                t0 = self._stuck.setdefault(s, t_s)
                if moving or t_s - t0 < ESC_AFTER_S - 1e-9:
                    continue
                other, esc = mate, s
                if mate in live:                  # 双方都让行：优先级低者位移
                    both = np.array([s, mate])
                    keys = priority_keys(sb["fs"][both], sb["fs_auto"][both], self.task_prio[both],
                                         S.blocks["battery"]["soc"][both], rt.owner_codes()[both], S.agent_no[both])
                    if not _lex_less(keys[0], keys[1]):
                        other, esc = s, mate
            if not self._hovers(esc):
                if not self._hovers(other) or other not in live:
                    continue
                esc, other = other, esc
            if t_s - self._esc.get(esc, -np.inf) < ESC_RETRY_S or t_s - self._esc.get(other, -np.inf) < ESC_RETRY_S:
                continue
            tgt = self._escape_target(esc, other)
            if tgt is None:
                continue
            rt.act_.correct(np.array([esc], np.int32), tgt[None], S_HOLD_SEP, "sep_escape")
            self._esc[esc] = t_s
            self._stuck.pop(esc, None)
            self.escapes += 1
            rt.sink.add(esc, rt.code("SAF.SEP.AVOIDING"), rt.t_ns, value=float(sb["sep_m"][esc]), threshold=P.recover_m,
                        detail="ESCAPE:" + str(S.ids[other]))

    def _escape_target(self, s: int, other: int) -> np.ndarray | None:
        """避让位移的目标点（ENU）：对端在作垂直运动时水平离开，否则垂直错开（对端在上方时下降）；高度上限与 DSM 净空复核，
        不满足时依次试另一方向。"""
        rt, S = self.rt, self.rt.S
        p = np.asarray(S.enu.pos[s], float)
        q = np.asarray(S.enu.pos[other], float)
        vq = np.asarray(S.enu.vel[other], float)
        geo = rt.geo
        z_hi = float(geo.max_z) - 2.0 if geo is not None and np.isfinite(getattr(geo, "max_z", np.inf)) else np.inf

        def ground(xy: np.ndarray) -> float:
            w = rt.world
            if w is None:
                return -np.inf
            try:
                return float(np.asarray(w.height_dsm(np.asarray(xy, float).reshape(1, 2)), float)[0])
            except Exception:
                return -np.inf

        d = p[:2] - q[:2]
        n = float(np.hypot(*d))
        if n < 0.3:
            vh = vq[:2]
            d = np.array([-vh[1], vh[0]]) if float(np.hypot(*vh)) > 0.3 else np.array([1.0, 0.0])
            n = float(np.hypot(*d))
        u = d / max(n, 1e-9)
        horiz = p.copy()
        horiz[:2] = p[:2] + u * ESC_DH_M
        up = p + np.array([0.0, 0.0, ESC_DZ_M])
        down = p - np.array([0.0, 0.0, ESC_DZ_M])
        if abs(float(vq[2])) > ESC_STILL_MPS * 2:
            order = (horiz, down if vq[2] > 0 else up, up if vq[2] > 0 else down)
        elif q[2] > p[2] + 0.5:
            order = (down, horiz, up)
        else:
            order = (up, down, horiz)
        for c in order:
            if c[2] > z_hi or c[2] < ground(c[:2]) + ESC_CLEAR_M:
                continue
            if c is horiz and ground(c[:2]) + ESC_CLEAR_M > p[2]:
                continue
            return c
        return None

    def set_task_priority(self, slots: np.ndarray, prio: np.ndarray) -> None:
        """M10 提供的任务优先级（K2，0–9）。"""
        self.task_prio[np.asarray(slots, np.int64)] = np.clip(np.asarray(prio, np.int64), 0, 9)

    def min_separation(self, slots: np.ndarray | None = None) -> float:
        if slots is None:
            return float(self.min_sep_seen)
        sl = np.asarray(slots, np.int64)
        sl = sl[(sl >= 0) & (sl < self.slot_min.size)]
        return float(self.slot_min[sl].min()) if sl.size else float("inf")
