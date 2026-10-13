"""7×24 接力监控（M20-FR-050 至 FR-062、FR-070、FR-074；M20 §6.9；AWR-04 §9；ADR-105）。

```text
PRECHECK：当前照度档与未来 24 h（按 M07 next_band_change 的精确档界分段，太阳为确定量）对候选（机型, 传感器）求 M19
          feasibility_detail；全部为空 → 581（why、infeasible_periods、suggestions）；当前不可行但未来可行 → DORMANT（窗口开始前预派）
ACQUIRE  ：站位作业 tasking.station（窗口内 25 m 环 × 15° × AGL，禁入圆外、机巢净空、世界边界与禁飞区外、转场可达，三射线视线）
          → 主站位与交接站位 → 站位工作项（优先级 8，只允许可行组的机型）→ 分配；触发接力的巡查机若可行且能量余量 ≥ 0.3，直接转岗
TRACKING ：M19 capture_state(target, level_req) 的 G(t)；1 Hz 能量预测：t_end − now ≤ ETA_B + t_acq 10 + t_overlap 15 + t_margin 60 + t_plan
          → 交接站位派 B（relay.dispatch{energy}）；照度档预报（每 60 s，45 min 视界）：当前站位在下一档窗口外 → 在 t_f − lead 派 B 到
          两档窗口交集内的站位（forecast）；下一档全部不可行 → t_f − 60 s 返航休眠（DORMANT）；每 10 s 核对当前 MOR 下站位仍在窗口内
          （霾时段），否则派 B 到新窗口内（weather）
HANDOVER ：B 到达交接站位且 A、B 同时处于明确捕获连续 ≥ 15 s → relay.handover{overlap_s}，A 沿禁入圆外航线返航；B 到站 90 s 仍未
          同时捕获 → 以站位作业为 B 重选站位（≤ 2 次，handover_slow）
失捕     ：捕获丢失 > 5 s → SEARCH：以航迹预测位置重新选站（站位作业，回避刚失去视线的站位，≤ 3 次、间隔 20 s），仍未重获时在世界边界
          内飞外扩方形；搜索航段被拒不视为作业失败（D2-FX-R2：第 1 轮被拒三次即返航、新机到站立即重复，形成 18 h 空转）
运动目标 ：1 Hz 用 M19 航迹预测位置 + 相对偏移更新站位（偏差 > 5 m 时 goto），位移 > 20 m 重选站位；撤离（禁入守卫）期间间隙记 keepout_priority
统计     ：5 Hz 累计（仿真时间）：监控时长、覆盖率 ∫G dt / T、间隙（按 infeasible > keepout_priority > battery > illum_transition > other
          归类，从 capture_lost 的 t_last_ok_ns 起算，首次捕获之前不计；进行中的间隙计入统计，结束或批处理完成时闭合并发 relay.gap）、
          被发现（我方机体触发的 target.discovered）、进入配置体、交接、最小禁入余量、架次、机时、能耗、电池最短可用库存、能量 RTL、
          派遣延误、t_plan 余量；S7 每仿真日当地 04:00 日切：当日统计归档到日表、清理交接记录（dayroll.py）
```
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from awr.tasking.keepout import signed_margin
from awr.tasking.model import LEVEL_RANK, RELAY_PRIORITY, TaskingError, WorkItem

from .book import ItemRT, SortieRT, TaskRT
from .dayroll import DayRoll
from .models import station_specs

__all__ = ["T_ACQ_S", "T_OVERLAP_S", "RelayController", "RelayRT", "band_segments"]

T_ACQ_S = 10.0
HANDOVER_SEP_M = 8.0   # 交接点与在岗机的最小间隔（D2-INT）
HANDOVER_DZ_M = 10.0   # 没有独立交接点且两侧水平错开都缺净空时的垂直错开
HANDOVER_DH_M = 15.0   # 没有独立交接点时接替机与在岗机的水平错开（D2-INT）
RELIEF_CLEAR_M = 10.0  # 水平错开点的 DSM 净空
T_OVERLAP_S = 15.0
T_MARGIN_S = 60.0
FORECAST_PERIOD_S = 60.0
FORECAST_HORIZON_S = 45 * 60.0
HANDOVER_SLOW_S = 90.0
LOST_SEARCH_S = 5.0
LOST_WARN_S = 300.0
SEARCH_STEP_M = 40.0
SEARCH_LEGS = 8
MOVE_RESELECT_M = 20.0
FOLLOW_TOL_M = 5.0
GAP_MIN_S = 0.2
ILLUM_WINDOW_S = 15 * 60.0
AGLS = (40.0, 60.0, 80.0, 100.0, 120.0)
CAUSES = ("keepout_priority", "illum_transition", "infeasible", "battery", "other")
# D2-FX-R2（验收第 1 轮 D2-AC-19、20）
NEST_CLEAR_M = 60.0          # 站位与机巢的水平净空（起降航线不穿过在岗机）
MOVE_R_M = 20.0              # 运动目标站位的视线稳健性复核半径（跟随几何，8 点）
REACQ_PERIOD_S = 20.0        # 失捕后重新选站的间隔
REACQ_MAX = 3                # 连续重新选站次数（之后外扩方形搜索）
REACQ_RESET_S = 120.0        # 搜索中每隔该时长重新允许选站（目标可能已移动）
DORMANT_LEAD_S = 60.0        # 下一档不可行：提前该时长返航休眠（physical 模式敏感体随照度档扩大前离开）
WEATHER_PERIOD_S = 10.0      # 当前 MOR 下站位仍在窗口内的核对周期
WINDOW_MARGIN_M = 5.0        # 站位距窗口上界的最小余量（选站）
WINDOW_TOL_M = 3.0           # 在岗站位的核对容差（越出窗口上界该值以上才换站，迟滞）
WINDOW_REL = 0.10            # 窗口上界的相对余量（M19 运行时口径求不出时的退回值）
FACE_DEG = 120.0             # 识别物相对机头的方位超过该值时原地转向（云台方位限位 ±160°）
STATION_KEEP_M = 5.0         # 静止识别物：站位离守卫口径禁入圆的最小余量（硬约束；运动识别物另有跟随容差）
REGION_FIXED = False         # 随机游走识别物的固定区域站位（缺省关闭）：诊断中 (c) 2 h 的覆盖率 0.30（交接站位只到 D 级、失捕后在两三个
                             # 站位间反复重选 4839 s），第 1 轮的跟随方式 24 h 只有 17 次短间隙；保留实现与用例，待按区域网格的 N_eff
                             # 复核（不只视线）后再启用
REGION_PAD_M = 8.0           # 固定站位：活动区域到窗口两端的余量
REGION_Q_N = 6               # 活动区域网格（每边点数，视线可见比例）
JOB_FAIL_MAX = 3             # 站位作业连续失败（超时、出错）该次数后才按结果判定
GUARD_TAU_S = 5.0            # 守卫的运动外推时长（guard.TAU_PRED_S）
RT_MARGIN = 0.02             # 窗口上界：M19 运行时口径（feasible_view）最大可行距离之下的余量（D2-FX-R2-tasking 第 2 次）
RT_BEARINGS = 8
RT_ITERS = 12
TENURE_EST_S = 1500.0        # 交接站位"在任期内跨照度档"的判定视界（约一个在岗班次）
ENERGY_LEAVE_S = 0.0         # 在岗机到达 t_end（返航能量 + 20% 储备）且无接替在岗时离岗（不等 M09 能量 RTL）
REZOOM_S = 5.0               # 在岗机与接替机的变焦刷新周期（运动目标距离变化）
RELIEF_RETRY_S = 30.0
BAD_MAX = 8
SEARCH_INSET_M = 40.0
MOVE_Q_N = 5                 # 随机游走活动区域的网格（每边点数）
A_MISS_LEFT_S = 30.0         # A 失去视线而 B 稳定捕获时，A 剩余时间 ≤ 该值才交接
A_MISS_MAX_S = 60.0          # 或 A 失去视线已达该时长
JOB_STALE_S = 60.0           # 站位作业结果的最长等待
PENDING_RELIEF_MAX_S = 180.0 # 接替工作项无人分配的最长等待
FLICKER_WIN_S = 180.0        # 视线闪断的统计窗口
FLICKER_N = 5                # 窗口内失捕次数达到该值即以交接换站
FLICKER_COOLDOWN_S = 300.0
RELIEF_LAT_M = (HANDOVER_DH_M, 25.0)   # 侧向错开交接点的横向距离候选（逐个复核视线）
HANDOVER_URGENT_S = 20.0     # 在岗机临近 t_end 时，接替机到站无捕获多久即另选站位
HANDOVER_URGENT_LEFT_S = 90.0
WEATHER_MOVE_MIN_S = 300.0   # 能见度变化时在岗机自行换站的最低剩余时长
WEATHER_RELIEF_MAX_S = 240.0 # 接替 ETA 超过该值（等电池、通道）时在岗机自行换站


@dataclass
class RelayRT:
    tid: str
    target: str
    level_req: str = "R"
    duration_s: float | None = None
    state: str = "PRECHECK"
    first_uav: str | None = None
    on_station: str | None = None
    relief: str | None = None
    relief_reason: str | None = None
    relief_t0: float | None = None
    station: tuple[float, float, float] | None = None
    handover_station: tuple[float, float, float] | None = None
    offset: dict | None = None
    group: tuple[str, str] | None = None
    groups: list[tuple[str, str]] = field(default_factory=list)
    allowed_models: tuple[str, ...] = ()
    station_job: str | None = None
    station_pos_ref: tuple[float, float] | None = None
    t_station: float = -math.inf
    next_forecast: float = 0.0
    dormant_until: float | None = None
    dormant_reason: str | None = None
    infeasible_periods: list[dict] = field(default_factory=list)
    item_seq: int = 0
    # 统计
    t_last: float | None = None
    t_monitor: float = 0.0
    captured_t: float = 0.0
    G: bool = False
    first_capture: float | None = None
    gap_t0: float | None = None
    gap_hint: str | None = None
    gaps_n: dict = field(default_factory=lambda: dict.fromkeys(CAUSES, 0))
    gaps_s: dict = field(default_factory=lambda: dict.fromkeys(CAUSES, 0.0))
    gap_max: float = 0.0
    discovered: int = 0
    disc_seen: dict = field(default_factory=dict)
    cfg_entries: int = 0
    cfg_s: float = 0.0
    handovers: int = 0
    overlaps: list[float] = field(default_factory=list)
    overlap_acc: float = 0.0        # 本次交接中 A、B 同时捕获的最长连续时长
    ov_run: float = 0.0             # A、B 同时捕获的当前连续时长
    b_run: float = 0.0              # B 的当前连续捕获时长
    a_miss: float = 0.0             # A 的当前连续未捕获时长（有接替时）
    keep_margin_min: float | None = None
    sorties: int = 0
    flight_s: float = 0.0
    energy_wh: float = 0.0
    energy_rtl: int = 0
    delays: dict = field(default_factory=lambda: dict.fromkeys(("battery", "charger", "slot", "wind", "launch_interval"), 0))
    t_plan_slack_min: float | None = None
    retreating: bool = False
    slow_moves: int = 0
    search_t0: float = 0.0
    search_n: int = 0
    lost_warned: bool = False
    t_lost: float | None = None
    t_band_change: float | None = None
    next_band: str | None = None
    next_feas: list | None = None
    day_table: list[dict] = field(default_factory=list)
    # S7 日切（M20-FR-074，dayroll.py；D2-FX-R2-other）：上一次日切时的累计值、当日最长间隙；交接重叠的累计计数、和与最小值
    # （当日明细 overlaps 在日切时归档后清空，累计统计不受影响）
    day_base: dict | None = None
    gap_max_day: float = 0.0
    ov_n: int = 0
    ov_sum: float = 0.0
    ov_min: float | None = None
    illum_limit: bool = False
    # D2-FX-R2
    reacq_n: int = 0
    reacq_t: float = -math.inf
    relief_job: str | None = None
    relief_retry_t: float = -math.inf
    bad: list = field(default_factory=list)          # 回避点 (x, y)：转场失败或失去视线的站位
    flick: list = field(default_factory=list)        # 最近的失捕时刻（视线闪断判定）
    job_t: float = -1e18                             # 最近一次站位作业的提交时刻（作业结果丢失时的看门狗）
    flick_t: float = -1e18                           # 上一次因闪断换站的时刻
    next_weather: float = 0.0
    t_zoom: float = -math.inf
    battery_wait: bool = False                       # 派遣因电池或通道延后、尚未起飞
    b_work_t0: float | None = None                   # 本次交接中接替机首次到站（WORKING）的时刻
    job_fails: int = 0                               # 站位作业连续失败（超时、出错，不是"无解"）次数
    fixed_region: bool = False                       # 随机游走识别物：站位按整个活动区域选定后固定（不跟随，D2-FX-R2-tasking 第 2 次）


class RelayController:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.pending_first: dict[str, str] = {}
        self._fz_cache: dict[tuple, Any] = {}
        self._seg_cache: tuple[int, float, list] | None = None
        self.days = DayRoll(rt)

    def reset(self) -> None:
        self.pending_first.clear()
        self._fz_cache.clear()
        self._seg_cache = None
        self.days.reset()

    # ------------------------------------------------------------ 生命周期
    def create(self, T: TaskRT, rel: dict) -> None:
        rt = self.rt
        trt = rt.targets_rt()
        tgt = str(rel.get("target"))
        cfg = trt.config(tgt) if trt is not None else None
        if trt is not None and cfg is None:
            raise_infeasible(rt, T, 305, "target_not_found", target=tgt)
            return
        lvl = rel.get("level_req") or ((cfg.raw if cfg is not None else {}).get("level_req")) or "R"
        T.relay = RelayRT(T.tid, tgt, str(lvl), rel.get("duration_s"))
        T.relay.first_uav = self.pending_first.pop(T.tid, None)

    def active_for(self, target: str) -> bool:
        return any(T.relay is not None and T.relay.target == target and T.active for T in self.rt.tasks.values())

    def start(self, T: TaskRT) -> None:
        r = T.relay
        if r is None:
            return
        self.precheck(T)

    def end(self, T: TaskRT, reason: str) -> None:
        r = T.relay
        if r is None:
            return
        self._close_gap(r, self.rt.t)
        r.state = "ENDED"
        prt = self.rt.perception_rt()
        for uav in (r.on_station, r.relief):
            if uav and prt is not None:
                with contextlib.suppress(Exception):
                    prt.set_requirement(uav, r.target, None, None)
        del reason

    def finalize(self, T: TaskRT) -> None:
        """批处理结束时闭合进行中的间隙（发 relay.gap），使统计与事件流一致（D2-FX-R2：第 1 轮 (a) 入夜后 18 h 无捕获，进行中的
        间隙从未闭合，统计记间隙 0 而覆盖率 0.237）。"""
        r = T.relay
        if r is not None and r.state != "ENDED":
            self._close_gap(r, self.rt.t)

    # ------------------------------------------------------------ 可行窗口（M19）
    def _target(self, r: RelayRT) -> Any:
        trt = self.rt.targets_rt()
        return trt.config(r.target) if trt is not None else None

    def _models(self) -> list[str]:
        rt = self.rt
        out = set()
        for v in rt.vehicle_ids():
            nid = rt.nest_of(v)
            n = rt.nests.get(nid) if nid else None
            if n is None or n.cfg.get("crewed"):
                continue
            mid = rt.model_id(v)
            if mid:
                out.add(mid)
        return sorted(out)

    def windows(self, r: RelayRT, band: str, mor: float) -> list[tuple[tuple[str, str], list[tuple[float, float, float]], str | None]]:
        """[(（机型, 传感器）, [(agl, d_lo, d_hi)], why)]（某照度档与 MOR 下）。"""
        rt = self.rt
        t = self._target(r)
        out = []
        if t is None:
            return out
        try:
            from awr.sim.perception.feasibility import feasibility_detail
            from awr.sim.targets.signature import signature_of
        except Exception:
            return out
        mode = str((t.raw or {}).get("criterion_mode", "geometric"))
        rev = int((t.raw or {}).get("cfg_rev", 0) or 0)
        T = rt.tasks.get(r.tid)
        allow = set((T.common.get("allowed_models") or []) if T is not None else [])   # 任务的机型限定（D2-INT）
        for mid in self._models():
            if allow and mid not in allow:
                continue
            sig = signature_of(mid, rt.T)
            for name, sn in station_specs(rt.T, mid, band):
                key = (r.target, rev, band, round(mor, -2), mid, name, r.level_req)
                hit = self._fz_cache.get(key)
                if hit is None:
                    ws = []
                    why = None
                    frs: list[tuple[float, Any]] = []
                    for agl in AGLS:
                        try:
                            fr = feasibility_detail(t, sn, band, mor, mode, sig, level_req=r.level_req, agl_m=(agl,))
                        except Exception as e:
                            fr = None
                            why = f"error:{type(e).__name__}"
                        if fr is not None and fr.feasible:
                            frs.append((float(agl), fr))
                        elif fr is not None and why is None:
                            why = fr.why
                    # 窗口上界按 M19 运行时口径（`feasible_view`：按站位方位的关键尺寸投影与所需等级变焦）逐 AGL 二分求出，再留 RT_MARGIN
                    # （D2-FX-R2-tasking 第 2 次：`feasibility_detail` 的上界比运行时高 5–6%，例 (b) 夜间 MOR 3 km、AGL 40 m：377 m 对 357 m；D2-FX-R2 以
                    # 10% 相对余量规避，游走的人的窗口因此只剩 320–339 m，又被规划口径的禁入圆 345 m 吃掉，霾时段完全失去目标）
                    rt_hi = self._rt_dhi(r, t, sn, band, mor, frs)
                    for agl, fr in frs:
                        d_hi = float(fr.d_hi_m) - WINDOW_MARGIN_M
                        if agl in rt_hi:
                            d_hi = min(d_hi, rt_hi[agl] * (1.0 - RT_MARGIN))
                        else:
                            d_hi = min(d_hi, float(fr.d_hi_m) * (1.0 - WINDOW_REL))
                        if d_hi > float(fr.d_lo_m) + WINDOW_MARGIN_M:
                            ws.append((agl, float(fr.d_lo_m), d_hi))
                        elif why is None:
                            why = "narrow"
                    hit = (ws, None if ws else why)
                    if len(self._fz_cache) > 512:
                        self._fz_cache.clear()
                    self._fz_cache[key] = hit
                out.append(((mid, name), hit[0], hit[1]))
        return out

    def _rt_dhi(self, r: RelayRT, t: Any, sn: Any, band: str, mor: float, frs: list) -> dict[float, float]:
        """M19 运行时口径的窗口上界：在各 AGL、8 个方位上对水平距离二分（区间 [d_lo, d_hi]），取各方位最大可行距离的最小值。
        识别物状态取 M18（站位的地面高程取识别物处）；任何异常时返回空（调用方退回相对余量）。"""
        if not frs:
            return {}
        try:
            from awr.sim.perception.feasibility import env_for, feasible_view

            trt = self.rt.targets_rt()
            ts = trt.state1(r.target) if trt is not None else None
            if ts is None:
                return {}
            tp = np.asarray(ts.pos, float).reshape(3)
            g = float(np.asarray(self.rt.dtm(tp[None, :2]), float).reshape(-1)[0])
            if not math.isfinite(g):
                g = float(tp[2])
            env = env_for(band, mor)
            ang = np.arange(RT_BEARINGS) * (2.0 * math.pi / RT_BEARINGS)
            agls = np.array([a for a, _f in frs], float)
            lo = np.repeat(np.array([float(f.d_lo_m) for _a, f in frs]), RT_BEARINGS)
            hi = np.repeat(np.array([float(f.d_hi_m) for _a, f in frs]), RT_BEARINGS) + 1.0
            za = np.repeat(g + agls, RT_BEARINGS)
            ca, sa = np.tile(np.cos(ang), len(agls)), np.tile(np.sin(ang), len(agls))
            ok_lo = None
            for _ in range(RT_ITERS):
                mid = 0.5 * (lo + hi)
                vp = np.c_[tp[0] + mid * ca, tp[1] + mid * sa, za]
                ok = np.asarray(feasible_view(vp, t, ts, sn, env, r.level_req).ok, bool)
                lo = np.where(ok, mid, lo)
                hi = np.where(ok, hi, mid)
                if ok_lo is None:
                    vp0 = np.c_[tp[0] + lo * ca, tp[1] + lo * sa, za]
                    ok_lo = np.asarray(feasible_view(vp0, t, ts, sn, env, r.level_req).ok, bool)
            out: dict[float, float] = {}
            for k, a in enumerate(agls):
                seg = slice(k * RT_BEARINGS, (k + 1) * RT_BEARINGS)
                if ok_lo is not None and not ok_lo[seg].all():
                    out[float(a)] = 0.0                        # 下界处就不可行：该 AGL 不用
                else:
                    out[float(a)] = float(lo[seg].min())
            return out
        except Exception:
            return {}

    def _feas(self, r: RelayRT, band: str, mor: float) -> list[tuple[tuple[str, str], list]]:
        return [(g, ws) for g, ws, _w in self.windows(r, band, mor) if ws]

    def _band_now(self) -> tuple[str, float]:
        return self.rt.light_fc()

    def _forecast(self, horizon_s: float, step_s: float = 3600.0) -> list[tuple[float, str]]:
        rt = self.rt
        env = getattr(rt.ctx, "env", None)
        fn = getattr(env, "forecast", None)
        if not callable(fn):
            band, _m = self._band_now()
            return [(rt.t + k * step_s, band) for k in range(int(horizon_s // step_s) + 1)]
        try:
            f = fn(rt.t_ns, horizon_s, step_s)
            from awr.environment.sky.state import BAND_NAMES

            return [(float(tn) * 1e-9, BAND_NAMES[int(b)] if not isinstance(b, str) else b) for tn, b in zip(f["t_ns"], f["band"], strict=True)]
        except Exception:
            band, _m = self._band_now()
            return [(rt.t, band)]

    def segments(self, horizon_s: float) -> list[tuple[float, float, str]]:
        """未来 horizon 内的照度档分段 [(t0, t1, band)]（仿真秒，档界取 M07 `next_band_change` 的 1 s 精度时刻）。同一分钟内复用。"""
        rt = self.rt
        key = int(rt.t // 60.0)
        c = self._seg_cache
        if c is not None and c[0] == key and c[1] >= horizon_s:
            return [s for s in c[2] if s[0] < rt.t + horizon_s]
        segs = band_segments(rt, horizon_s)
        self._seg_cache = (key, horizon_s, segs)
        return segs

    def precheck(self, T: TaskRT, *, wake: bool = False) -> None:
        """预检（M20-FR-050）：当前照度档与未来 24 h（精确档界）。`wake` 为休眠到期的复核：当前仍不可行而下一档在预派提前量内
        可行时，按下一档的窗口预派（到站后待光照满足即捕获，间隙不跨出不可行时段太多）。"""
        rt = self.rt
        r = T.relay
        band, mor = self._band_now()
        now = self.windows(r, band, mor)
        feas_now = [(g, ws) for g, ws, _why in now if ws]
        segs = self.segments(24 * 3600.0)
        per: list[dict] = []
        any_future = False
        why_now = next((why for _g, ws, why in now if not ws and why), "night_sensor")
        cache_b: dict[str, bool] = {}
        for t0, t1, b in segs:
            if b not in cache_b:
                cache_b[b] = any(ws for _g, ws, _w in self.windows(r, b, mor))
            ok = cache_b[b]
            any_future |= ok
            if not ok:
                if per and abs(per[-1]["t_to"] - t0) < 1.0:
                    per[-1]["t_to"] = t1
                else:
                    per.append({"t_from": t0, "t_to": t1, "band": b})
        r.infeasible_periods = per
        if not feas_now and not any_future:
            sugg = self.suggestions(r, band, mor)
            raise_infeasible(rt, T, 581, _why_code(why_now), band=band, infeasible_periods=[
                {"band": p["band"], "t_from_s": round(p["t_from"], 1), "t_to_s": round(p["t_to"], 1)} for p in per], suggestions=sugg)
            return
        if not feas_now:
            nxt = next((t0 for t0, _t1, b in segs if cache_b.get(b) and t0 > rt.t + 1e-6), None)
            if wake and nxt is not None and nxt - rt.t <= self._lead(T) + 120.0:
                b_next = next(b for t0, _t1, b in segs if abs(t0 - nxt) < 1e-6)
                feas_next = self._feas(r, b_next, mor)
                if feas_next:
                    r.groups = [g for g, _ws in feas_next]
                    r.allowed_models = tuple(sorted({g[0] for g in r.groups}))
                    r.state = "ACQUIRE"
                    self.request_station(T, feas_next, b_next, mor, reason="wake")
                    return
            self._dormant(T, nxt, "infeasible")
            return
        r.groups = [g for g, _ws in feas_now]
        r.allowed_models = tuple(sorted({g[0] for g in r.groups}))
        r.state = "ACQUIRE"
        self.request_station(T, feas_now, band, mor, reason="initial")

    def suggestions(self, r: RelayRT, band: str, mor: float) -> list[dict]:
        """换型建议（M20-FR-067，P1）：目录中可在该档可行的机型与挂载（按"新增资源最少"排序：同机加挂照明器 → 换 LWIR 机型）。"""
        out = []
        if band in ("night", "low"):
            out.append({"kind": "mount", "text_zh": "加挂远距红外照明器 nir_ill_l 或改用 LWIR 100 mm 机型", "mount": "nir_ill_l"})
        out.append({"kind": "model", "text_zh": "改用带 LWIR 100 mm 的机型（如 AWR-Q9、AWR-V1）", "model_id": "awr_q9"})
        del r, mor
        return out

    def _dormant(self, T: TaskRT, until: float | None, reason: str) -> None:
        r = T.relay
        r.state = "DORMANT"
        r.dormant_until = until
        r.dormant_reason = reason
        if "dormant" not in T.warnings:
            T.warnings.append("dormant")
        for uav in (r.on_station, r.relief):
            so = self.rt.sorties.get(uav or "")
            if so is not None:
                self.rt.ex.release(so, "dormant")
        r.on_station = r.relief = None
        r.relief_job = None
        r.station = r.handover_station = None
        r.retreating = False
        self._reset_search(r)
        for it in T.items.values():
            if it.state not in ("DONE", "CANCELED"):
                if it.uav and it.uav in self.rt.sorties and self.rt.sorties[it.uav].state == "ASSIGNED":
                    self.rt.ex.release(self.rt.sorties[it.uav], "cancel")
                it.state = "CANCELED"
        if r.gap_t0 is not None and r.gap_hint != "infeasible":
            # 不可行时段从休眠起算（此前的间隙按原因闭合，下一 5 Hz 周期以 infeasible 重开）
            self._close_gap(r, self.rt.t)

    # ------------------------------------------------------------ 站位
    def request_station(self, T: TaskRT, feas: list, band: str, mor: float, *, reason: str = "initial", others_extra: list | None = None,
                        next_feas: list | None = None, avoid_extra: list | None = None, no_region: bool = False) -> str | None:
        rt = self.rt
        r = T.relay
        tp = self._track_pos(r)
        if tp is None:
            return None
        avail = [(g, ws) for g, ws in feas if self._group_available(g)] if reason.split(":")[0] in ("initial", "reselect", "wake", "relief",
                                                                                                    "relief_now") else feas
        if avail and len(avail) < len(feas):
            # 只在有可派机体的组里选站（D2-FX-R2-tasking 第 2 次，D2-AC-35：sanfrancisco T5 中接替站位选了 AWR-V1 组，而唯一的 AWR-V1 正在
            # 另一接力上，接替工作项以 capability 一直无法分配，在岗机离岗后 2300 s 无人）
            feas = avail
        nests = [(list(n.pos3), n.cfg.get("link_range_m")) for n in rt.nests.values() if not n.cfg.get("crewed")]
        others = [list(R.relay.station) for R in rt.tasks.values() if R.relay is not None and R.relay is not r and R.relay.station is not None]
        others += [list(p) for p in others_extra or []]
        nxt = None
        if next_feas is not None:
            w2 = dict(next_feas)
            nxt = [[list(x) for x in w2.get(g, [])] for g, _ws in feas]
        else:
            env = getattr(rt.ctx, "env", None)
            fn = getattr(env, "next_band_change", None)
            if callable(fn):
                with contextlib.suppress(Exception):
                    nb = fn(rt.t_ns, 86400.0)
                    if nb is not None and (nb[0] * 1e-9 - rt.t) < TENURE_EST_S:
                        w2 = {g: ws for g, ws, _w in self.windows(r, nb[2], mor)}
                        nxt = [[list(x) for x in w2.get(g, [])] for g, _ws in feas]
        origin = min((n[0] for n in nests), key=lambda q: math.hypot(q[0] - tp[0], q[1] - tp[1])) if nests else list(tp)
        a = rt.sorties.get(r.on_station or "")
        if reason in ("follow", "reacquire", "weather_move") and a is not None:
            origin = [float(x) for x in rt.pos(a.uav)]   # 转场项按在岗机当前位置：优先就近换站（跟随连续、少穿越）
        p_ratio = []
        fw: list[bool] = []
        r_min: list[float] = []
        for (mid, _s), _ws in feas:
            m = rt.model(mid)
            p_ratio.append(1.0 if m is None else m.p_role(m.station_role()) / max(m.p_hover_w, 1.0))
            # 固定翼与垂起机在接力中盘旋（垂起机的多旋翼悬停每架次限 180 s，d2-WP-08-to-M20 第 6 条；M20-FR-059）
            fw.append(bool(m is not None and m.frame != "multirotor"))
            r_min.append(0.0 if m is None else float(m.r_loiter_min_m))
        moving = self._moving(r)
        windows = [[list(w) for w in ws] for _g, ws in feas]
        keep = self.station_circles()
        fixed = False
        see_pos = None
        reg = self._region(r) if REGION_FIXED and moving and not any(fw) and not no_region else None
        if reg is not None:
            # 随机游走识别物（活动区域已知）：窗口宽于活动区域时取一个覆盖整个区域的固定站位（站位到区域内任一点的距离都在窗口内，
            # 离区域外接禁入圆有守卫余量，对区域网格的视线可见比例计入评分），不随识别物移动跟随换站（D2-FX-R2-tasking 第 2 次：(c) 游走的狗每移动
            # 20 m 即重选站位，转场中失捕，开场 15 min 15 次间隙）；窗口窄（夜间霾）时仍按相对偏移跟随
            c, h, _Q, hull = reg
            fwin = [[[float(a), float(lo) + h + REGION_PAD_M, float(hi) - h - REGION_PAD_M] for a, lo, hi in ws
                     if float(hi) - float(lo) >= 2.0 * (h + REGION_PAD_M) + 10.0] for ws in windows]
            if any(fwin):
                fixed = True
                windows = fwin
                see_pos = [float(tp[0]), float(tp[1])]
                tp = (float(c[0]), float(c[1]), float(tp[2]))
                keep = np.vstack([keep, np.asarray(hull, float).reshape(-1, 3)]) if len(keep) else np.asarray(hull, float).reshape(-1, 3)
        payload = {"target_pos": [float(x) for x in tp], "windows": windows,
                   "keepouts": keep, "nests": nests, "others": others, "origin": origin, "v_mps": 8.0, "p_ratio": p_ratio,
                   "next_windows": nxt if not fixed else None, "moving": moving and not fixed, "target_h_m": self._target_h(r), "fw": fw,
                   "r_min": r_min, "avoid": [list(a) for a in [*r.bad, *(avoid_extra or [])]], "nest_clear_m": NEST_CLEAR_M,
                   "move_r_m": MOVE_R_M if moving and not fixed else 0.0, "move_q": self._move_q(r) if moving and not fixed else None,
                   "region_q": [list(q) for q in reg[2]] if fixed else None, "see_pos": see_pos}
        if reason in ("initial", "reselect", "wake", "follow", "reacquire", "weather_move"):
            r.station_pos_ref = (float(tp[0]), float(tp[1]))
            r.t_station = rt.t
        groups = [g for g, _ws in feas]
        retry = None
        if fixed:
            # 固定区域站位无解（区域网格视线不足、收窄后的窗口内没有净空点）：同一请求改按跟随方式重选，不判 581
            def retry(T=T, feas=feas, band=band, mor=mor, reason=reason, others_extra=others_extra, next_feas=next_feas,
                      avoid_extra=avoid_extra) -> None:
                self.request_station(T, feas, band, mor, reason=reason, others_extra=others_extra, next_feas=next_feas,
                                     avoid_extra=avoid_extra, no_region=True)
        jid = rt.jobs.submit("tasking.station", payload, lambda extra, tid=T.tid, groups=groups, reason=reason, fixed=fixed, retry=retry:
                             self._on_station(tid, extra, groups, reason, fixed, retry), key=f"st-{T.tid}-{reason.split(':')[0]}")
        if reason.startswith("relief_now"):
            r.relief_job = jid
        else:
            r.station_job = jid
        r.job_t = rt.t
        return jid

    def _group_available(self, g: tuple) -> bool:
        """组 (机型, 传感器) 是否有可派机体：在机巢中（就绪、充电、换电、周转）或已放行待命的同机型机体。"""
        rt = self.rt
        mid = str(g[0])
        for n in rt.nests.values():
            for u, ac in n.air.items():
                if rt.model_id(u) == mid and ac.state in ("ready", "charging", "swapping", "turnaround", "waiting_pack"):
                    return True
        return any(rt.model_id(so.uav) == mid and so.state == "STANDBY" and so.released for so in rt.sorties.values())

    def _on_station(self, tid: str, extra: dict, groups: list, reason: str, fixed: bool = False, retry: Any = None) -> None:
        rt = self.rt
        T = rt.tasks.get(tid)
        if T is None or T.relay is None or T.relay.state in ("ENDED", "DORMANT") or T.state not in ("RUNNING", "READY"):
            if T is not None and T.relay is not None:
                if reason.startswith("relief_now"):
                    T.relay.relief_job = None
                else:
                    T.relay.station_job = None
            return
        r = T.relay
        if reason.startswith("relief_now"):
            r.relief_job = None
        else:
            r.station_job = None
        if not extra.get("ok") and r.job_fails < JOB_FAIL_MAX:
            # 站位作业本身失败（规划服务超时、出错；不是"无可行站位"）：不判 581，稍后由重选核对重新提交（D2-FX-R2-tasking 第 2 次，
            # D2-AC-45 真实栈诊断：负载 17 时首次选站作业超时，接力任务直接 INFEASIBLE）
            r.job_fails += 1
            r.t_station = rt.t
            if reason.startswith("relief_now"):
                r.relief_retry_t = rt.t
            return
        if extra.get("ok"):
            r.job_fails = 0
        if reason == "reacquire" and r.t_lost is None and r.state != "SEARCH" and r.first_capture is not None:
            return          # 作业期间已重新捕获：丢弃重选结果（D2-FX-R2-tasking 第 2 次：(c) 重获 2 s 后仍按结果飞往 300 m 外的站位）
        st = (extra.get("station") or {}) if extra.get("ok") else {}
        if not st.get("ok"):
            why = st.get("why") or extra.get("why") or "los"
            if fixed and retry is not None:
                retry()
                return
            if reason == "initial" and r.on_station is None:
                raise_infeasible(rt, T, 581, _why_code(why), band=self._band_now()[0])
            elif reason.startswith("relief_now"):
                r.relief_retry_t = rt.t
                self._relief_fallback(T, reason.split(":", 1)[-1])
            return
        g = groups[int(st.get("group", 0))] if 0 <= int(st.get("group", 0)) < len(groups) else groups[0]
        prim = tuple(float(x) for x in st["primary"])
        if reason not in ("slow",):
            r.fixed_region = bool(fixed)
        hand = tuple(float(x) for x in st["handover"]) if st.get("handover") else None
        if reason.startswith("relief_now"):
            self._relief_from_station(T, prim, hand, st.get("offset"), (str(g[0]), str(g[1])), reason.split(":", 1)[-1])
            return
        r.group = (str(g[0]), str(g[1]))
        r.offset = st.get("offset")
        if reason == "relief" and r.on_station:
            # 预规划的交接站位：作业已按"离在岗机 ≥ 15 m"（others）选出主站位，优先用它，其次交接点
            a = rt.sorties.get(r.on_station)
            pa = tuple(rt.pos(a.uav)) if a is not None else prim
            ok_p = math.hypot(prim[0] - pa[0], prim[1] - pa[1]) >= HANDOVER_DH_M
            r.handover_station = prim if ok_p or hand is None else hand
            return
        if reason in ("reacquire", "weather_move") and r.on_station:
            self._move_on_station(T, prim, hand)
            return
        if reason == "slow" and r.relief:
            b = rt.sorties.get(r.relief)
            _T, it_b = rt.ex.item_of(b) if b is not None else (None, None)
            if b is not None and it_b is not None:
                it_b.w.pos = prim
                off = self._offset_of(r, prim)
                if off is not None:
                    it_b.w.needs["offset"] = off
                rt.ex.start_transit(b)
                rt.emit("task.warning", 1, tid=T.tid, uav=b.uav, code="handover_slow", station=list(prim))
            return
        if reason == "follow" and r.on_station and r.offset:
            so = rt.sorties.get(r.on_station)
            _Tf, itf = rt.ex.item_of(so) if so is not None else (None, None)
            if itf is not None:
                itf.w.needs["offset"] = dict(r.offset)
                itf.w.pos = prim
        r.station, r.handover_station = prim, hand
        T.geometry["stations"] = [{"id": f"{tid}.st", "pos": list(prim), "target": r.target, "role": "primary", "uav": r.on_station},
                                  *([{"id": f"{tid}.ho", "pos": list(hand), "target": r.target, "role": "handover", "uav": r.relief}] if hand else [])]
        T.revision += 1
        if r.on_station is None and reason in ("initial", "reselect", "wake"):
            # 尚未起飞的首个站位工作项（转场失败后重选）改到新站位，否则新建
            pend = next((it for it in T.items.values() if it.state == "PENDING" and not it.relief_for), None)
            if pend is not None:
                pend.w.pos = prim
                rt.request_allocation()
            elif not any(it.state in ("ASSIGNED", "ACTIVE") and not it.relief_for for it in T.items.values()):
                self._new_item(T, prim, first=True)

    def _move_on_station(self, T: TaskRT, prim: tuple, hand: tuple | None) -> None:
        """失捕后的重新选站：在岗机飞往新站位（多旋翼转场；固定翼与垂起机改盘旋圈）。"""
        rt = self.rt
        r = T.relay
        so = rt.sorties.get(r.on_station or "")
        if so is None:
            return
        _T, it = rt.ex.item_of(so)
        if it is None:
            return
        cur = np.asarray(it.w.pos if it.w.pos is not None else rt.pos(so.uav), float)
        r.station, r.handover_station = prim, hand
        T.revision += 1
        if math.dist(tuple(cur[:2]), prim[:2]) < 3.0 and not self._follows(r):
            return
        it.w.pos = prim
        off = self._offset_of(r, prim)
        if off is not None:
            it.w.needs["offset"] = off
            r.offset = off
        so.station = None
        rt.ex.start_transit(so)

    def _new_item(self, T: TaskRT, pos: tuple, *, first: bool = False, relief: str | None = None) -> ItemRT:
        rt = self.rt
        r = T.relay
        r.item_seq += 1
        wid = f"{T.tid}.{r.item_seq - 1}" if first and r.item_seq == 1 else f"{T.tid}.r{r.item_seq}"
        m = rt.model(r.group[0]) if r.group else None
        # 站位按所选组的构型给出（多旋翼悬停点或固定翼盘旋圈）：只派同构型的机型（D2-INT：S7 中多旋翼站位派给了 AWR-V1，
        # 其多旋翼悬停每架次限 180 s，到点即耗尽后迫降）
        fw_sel = m is not None and m.frame != "multirotor"
        allowed = tuple(x for x in r.allowed_models if (lambda mm: mm is not None and (mm.frame != "multirotor") == fw_sel)(rt.model(x)))
        needs = {"role": "hover", "fw_ok": True, "level": r.level_req}
        off = self._offset_of(r, pos)
        if off is not None:
            needs["offset"] = off
        w = WorkItem(wid, T.tid, "relay_station", priority=RELAY_PRIORITY, persistent=True, workload_s=0.0, pos=pos,  # type: ignore[arg-type]
                     needs=needs, allowed_models=allowed or r.allowed_models, target=r.target,
                     loiter_radius_m=None if m is None else m.r_loiter_min_m or None)
        it = ItemRT(w)
        it.t_created = rt.t
        if relief:
            it.relief_for = relief
        T.items[wid] = it
        # 捕获机转岗（M20-FR-060）：可行且能量余量 ≥ 0.3 时直接成为首个在岗机
        if first and r.first_uav and r.first_uav in rt.sorties:
            so = rt.sorties[r.first_uav]
            mm = rt.model(rt.model_id(so.uav))
            ok_model = mm is not None and (not w.allowed_models or mm.id in w.allowed_models)
            if ok_model and so.airborne and rt.soc(so.uav) - 0.2 >= 0.3:
                _T0, it0 = rt.ex.item_of(so)
                if it0 is not None and it0.state not in ("DONE", "CANCELED"):
                    it0.state, it0.uav, it0.reason = "PENDING", None, "relay_handoff"
                    rt.emit("task.reallocated", 0, tid=it0.w.tid, wid=it0.w.wid, from_uav=so.uav, to_uav=None, reason="lost", items=[])
                so.state = "STANDBY"
                so.released = False
                if rt.ex.assign(so.uav, T, it, {"eta_s": 0.0}):
                    # relay.dispatch 由分配核对（`_sync_roles`）在推出在岗机时发出一次（D2-FX-R2：此前此处另发一次，事件重复）
                    rt.request_allocation()
                    return it
        rt.request_allocation()
        return it

    def station_circles(self) -> np.ndarray:
        """接力站位的禁入圆（N×3）：静止识别物同规划口径；运动识别物取守卫口径的基础半径 + 运动方式的速度 × τ_pred + 跟随容差 + 2 m，
        而不是规划口径的 v·τ_route（20 s）外扩。站位随航迹跟随，与识别物的相对距离由跟随维持，守卫按 τ_pred 外推（D2-FX-R2-tasking 第 2 次：游走
        的人在夜间 3 km 霾中的窗口约 330–350 m，规划口径的 345 m 禁入圆使站位无解）。"""
        from awr.tasking.keepout import circles as _circles

        rt = self.rt
        kos = rt.keepouts(planning=False)
        plan = {k.id: k for k in rt.keepouts(planning=True)}
        trt = rt.targets_rt()
        out = []
        for k in kos:
            if not k.moving:
                pk = plan.get(k.id, k)
                out.append((pk.x, pk.y, pk.r + STATION_KEEP_M))   # 悬停定位误差与守卫判定之间的余量（站位贴圆时到站—撤离循环）
                continue
            cfg = trt.config(k.target) if trt is not None and k.target else None
            mo = ((cfg.raw if cfg is not None else {}) or {}).get("motion") or {}
            v = max(float(mo.get("speed_mps") or mo.get("speed_max_mps") or 0.0), 0.5)
            out.append((k.x, k.y, k.r + v * GUARD_TAU_S + FOLLOW_TOL_M + 2.0))
        del _circles
        return np.asarray(out, float).reshape(-1, 3)

    def _offset_of(self, r: RelayRT, pos: Any) -> dict | None:
        """运动目标：站位相对当前航迹位置的偏移 {bearing_rad, dist_m, agl_m}（静止目标为 None）。"""
        if pos is None or not self._follows(r):
            return None
        tp = self._track_pos(r)
        if tp is None:
            return None
        p = np.asarray(pos, float)
        try:
            agl = float(p[2] - float(self.rt.dtm(p[None, :2])[0]))
        except Exception:
            agl = float(p[2])
        return {"bearing_rad": math.atan2(p[1] - tp[1], p[0] - tp[0]), "dist_m": math.hypot(p[0] - tp[0], p[1] - tp[1]), "agl_m": agl}

    def station_for(self, so: SortieRT, it: ItemRT) -> tuple | None:
        T = self.rt.tasks.get(it.w.tid)
        r = T.relay if T is not None else None
        if r is None:
            return None
        off = (it.w.needs or {}).get("offset") or (r.offset if so.uav == r.on_station else None)
        a = self.rt.sorties.get(r.on_station or "")
        relief = a is not None and a.uav != so.uav and it.relief_for == a.uav
        if off and self._follows(r):
            # 运动目标：各站位工作项保持自己相对航迹的偏移（在岗机为站位作业的偏移，接替机为派遣时相对航迹的偏移）
            tp = self._track_pos(r)
            if tp is not None:
                b, d, agl = float(off["bearing_rad"]), float(off["dist_m"]), float(off["agl_m"])
                x, y = tp[0] + d * math.cos(b), tp[1] + d * math.sin(b)
                if relief and d > 1.0 and self._relief_blocked(so, a, (x, y)):
                    # 接替机的跟随站位离在岗机 < HANDOVER_DH_M，或直飞过去要穿过在岗机：沿以识别物为心的圆错开方位（每档
                    # HANDOVER_DH_M + 5 m 弧长，两侧交替，≤ 4 档）。D2-FX-R2-tasking 第 2 次：(b) 白天接替机与在岗机的偏移相同，直飞
                    # 在岗机身后 19 m 的站位、穿过在岗机，M09 第 3 次让行判 OSCILLATION 保持 HOLD，两机悬停到能量 RTL，2 h 内 4 次
                    step = (HANDOVER_DH_M + 5.0) / d
                    for k in (1, -1, 2, -2, 3, -3, 4, -4):
                        bk = b + k * step
                        xk, yk = tp[0] + d * math.cos(bk), tp[1] + d * math.sin(bk)
                        if not self._relief_blocked(so, a, (xk, yk)):
                            x, y = xk, yk
                            break
                return (x, y, float(self.rt.dtm(np.array([[x, y]]))[0]) + agl)
        pos = tuple(it.w.pos) if it.w.pos is not None else r.station
        if pos is not None and relief and self._relief_blocked(so, a, pos):
            # 固定的交接站位被在岗机占住或直飞要穿过在岗机：改取在岗机侧向错开的交接点
            alt = self._relief_pos(r, a)
            if alt is not None:
                return tuple(alt)
        return pos

    def _relief_blocked(self, so: SortieRT, a: SortieRT, pos: Any) -> bool:
        """接替机的目标点离在岗机 < HANDOVER_DH_M，或从接替机当前位置直飞过去的线段离在岗机 < HANDOVER_DH_M（在岗机在两者之间）。"""
        pa = np.asarray(self.rt.pos(a.uav), float)
        pb = np.asarray(self.rt.pos(so.uav), float)
        q = np.asarray(pos[:2], float)
        if float(np.hypot(*(q - pa[:2]))) < HANDOVER_DH_M:
            return True
        d = q - pb[:2]
        L2 = float(d @ d)
        if L2 <= HANDOVER_DH_M ** 2:
            return False
        tq = float(np.clip((pa[:2] - pb[:2]) @ d / L2, 0.0, 1.0))
        return 0.0 < tq < 1.0 and float(np.hypot(*(pb[:2] + tq * d - pa[:2]))) < HANDOVER_DH_M

    def on_station(self, so: SortieRT, it: ItemRT) -> None:
        """站位到达：设所需等级（M19 set_requirement）与变焦（feasible_view 的焦距）。"""
        rt = self.rt
        T = rt.tasks.get(it.w.tid)
        r = T.relay if T is not None else None
        if r is None:
            return
        prt = rt.perception_rt()
        if prt is not None:
            with contextlib.suppress(Exception):
                prt.set_requirement(so.uav, r.target, r.level_req, None)
        self._zoom(so, r)

    def _zoom(self, so: SortieRT, r: RelayRT) -> None:
        rt = self.rt
        srt = rt.sensor_rt()
        if srt is None or not hasattr(srt, "set_zoom") or r.group is None:
            return
        tp = self._track_pos(r)
        if tp is None:
            return
        try:
            from awr.sim.perception.feasibility import env_for, feasible_view

            band, mor = rt.light()
            specs = dict(station_specs(rt.T, rt.model_id(so.uav) or "", band))
            sn = specs.get(r.group[1]) or next(iter(specs.values()), None)
            trt = rt.targets_rt()
            t, ts = trt.config(r.target), trt.state1(r.target)
            if sn is None or t is None or ts is None:
                return
            fv = feasible_view(rt.pos(so.uav)[None, :], t, ts, sn, env_for(band, mor), r.level_req)
            srt.set_zoom(so.slot, r.group[1], float(fv.f_mm[0]))
        except Exception:
            return

    def target_pos(self, so: SortieRT) -> tuple | None:
        T = self.rt.tasks.get(so.tid or "")
        r = T.relay if T is not None else None
        return None if r is None else self._track_pos(r)

    def _track_pos(self, r: RelayRT) -> tuple | None:
        """接力只用感知航迹（M20-FR-051）；航迹尚未建立时（首次派遣前）用布设情报的识别物位置。"""
        prt = self.rt.perception_rt()
        if prt is not None:
            with contextlib.suppress(Exception):
                tr = prt.track(r.target, self.rt.t)
                if tr is not None and getattr(tr, "state", "") in ("CONFIRMED", "COASTING", "TENTATIVE"):
                    return tuple(float(x) for x in tr.pos)
        trt = self.rt.targets_rt()
        st = trt.state1(r.target) if trt is not None else None
        return None if st is None else tuple(float(x) for x in st.pos)

    def _moving(self, r: RelayRT) -> bool:
        """运动识别物：按布设的运动方式（random_walk、path）判定，不按此刻是否在走（D2-FX-R2：随机游走在停顿时 state1.moving 为
        False，站位工作项因此没有偏移，整个班次不跟随，T5 变体中 480 m 外的固定站位每分钟闪断数次）。"""
        t = self._target(r)
        mo = ((t.raw if t is not None else {}) or {}).get("motion") or {}
        if str(mo.get("mode") or "static") in ("random_walk", "path"):
            return True
        trt = self.rt.targets_rt()
        st = trt.state1(r.target) if trt is not None else None
        return bool(getattr(st, "moving", False))

    def _follows(self, r: RelayRT) -> bool:
        """站位随识别物移动跟随（运动识别物，且没有按活动区域固定站位）。"""
        return self._moving(r) and not r.fixed_region

    def _region(self, r: RelayRT) -> tuple | None:
        """随机游走识别物的活动区域：(中心 xy, 半对角线 h, 网格点 [(x, y)], 外接禁入圆 [(x, y, r)])；其他运动方式 None。外接禁入圆按
        守卫口径的基础半径 + h + v·τ_pred + 跟随容差 + 2 m（识别物走到区域任一点时站位仍在守卫圆外）。"""
        t = self._target(r)
        mo = ((t.raw if t is not None else {}) or {}).get("motion") or {}
        if str(mo.get("mode")) != "random_walk" or not mo.get("region"):
            return None
        R = np.asarray(mo["region"], float).reshape(-1, 2)
        lo, hi = R.min(axis=0), R.max(axis=0)
        c = 0.5 * (lo + hi)
        h = 0.5 * float(np.hypot(*(hi - lo)))
        n = REGION_Q_N
        xs, ys = np.linspace(lo[0], hi[0], n), np.linspace(lo[1], hi[1], n)
        Q = [(float(x), float(y)) for x in xs for y in ys]
        v = max(float(mo.get("speed_mps") or mo.get("speed_max_mps") or 0.0), 0.5)
        hull = [(float(c[0]), float(c[1]), float(k.r) + h + v * GUARD_TAU_S + FOLLOW_TOL_M + 2.0)
                for k in self.rt.keepouts(planning=False) if k.target == r.target]
        return c, h, Q, hull

    def _move_q(self, r: RelayRT) -> list | None:
        """随机游走识别物的活动区域网格（MOVE_Q_N × MOVE_Q_N，场景情报；站位作业按跟随几何复核这些位置的视线）；其他运动方式 None。
        D2-FX-R2：3×3（间距约 30 m）漏掉楼角的遮挡，T5 变体诊断中 465 m 外的站位通过复核，实际跟随时视线每分钟闪断数次。"""
        t = self._target(r)
        mo = ((t.raw if t is not None else {}) or {}).get("motion") or {}
        if str(mo.get("mode")) != "random_walk" or not mo.get("region"):
            return None
        R = np.asarray(mo["region"], float).reshape(-1, 2)
        lo, hi = R.min(axis=0), R.max(axis=0)
        xs, ys = np.linspace(lo[0], hi[0], MOVE_Q_N), np.linspace(lo[1], hi[1], MOVE_Q_N)
        return [[float(x), float(y)] for x in xs for y in ys]

    def _target_h(self, r: RelayRT) -> float:
        t = self._target(r)
        try:
            return float(t.size_lwh[2])
        except Exception:
            return 1.75

    def _near_bad(self, r: RelayRT, pos: Any) -> bool:
        return any(math.hypot(float(pos[0]) - b[0], float(pos[1]) - b[1]) < 25.0 for b in r.bad)

    def _mark_bad(self, r: RelayRT, pos: Any) -> None:
        if pos is None:
            return
        r.bad.append((round(float(pos[0]), 1), round(float(pos[1]), 1)))
        r.bad = r.bad[-BAD_MAX:]

    def station_ok(self, r: RelayRT, pos: Any, ws: list | None, margin_m: float = 0.0) -> bool:
        """站位 pos 是否在窗口 ws [(agl, d_lo, d_hi)] 内（水平距离按航迹预测位置；AGL 取最近的窗口档）。"""
        if pos is None or not ws:
            return False
        tp = self._track_pos(r)
        if tp is None:
            return False
        p = np.asarray(pos, float)
        d = math.hypot(p[0] - tp[0], p[1] - tp[1])
        try:
            agl = float(p[2] - float(self.rt.dtm(p[None, :2])[0]))
        except Exception:
            agl = float(p[2])
        w = min(ws, key=lambda x: abs(float(x[0]) - agl))
        if abs(float(w[0]) - agl) > 10.0:
            return False
        return float(w[1]) - max(0.0, -margin_m) - 1e-6 <= d <= float(w[2]) - margin_m

    def _cur_station(self, r: RelayRT, so: SortieRT | None) -> tuple | None:
        if so is None:
            return r.station
        _T, it = self.rt.ex.item_of(so)
        st = self.station_for(so, it) if it is not None else None
        return st or so.station or r.station

    # ------------------------------------------------------------ 5 Hz：G(t) 与统计
    def sense(self) -> None:
        rt = self.rt
        t = rt.t
        prt = rt.perception_rt()
        for T in sorted(rt.tasks.values(), key=lambda x: x.tid):
            r = T.relay
            if r is None or T.state != "RUNNING" or r.state in ("ENDED", "PRECHECK"):
                if r is not None:
                    r.t_last = t
                continue
            dt = 0.0 if r.t_last is None else max(t - r.t_last, 0.0)
            r.t_last = t
            if r.first_capture is not None:
                r.t_monitor += dt          # 监控时长自首次明确捕获起算（间隙口径同，M20 §6.9.7）
            G = False
            by: tuple = ()
            t_ok = None
            if prt is not None and r.state != "DORMANT":
                with contextlib.suppress(Exception):
                    cs = prt.capture_state(r.target, r.level_req)
                    G, by, t_ok = bool(cs.captured), tuple(cs.by), cs.t_last_ok_ns
            # 监控自接力机在站位上（WORKING）首次明确捕获起算：触发接力的巡查机（或其他任务的机体）在接力机到站前的零星捕获、转岗的
            # 巡查机飞往站位途中的捕获不开始计间隙（D2-FX-R2：S7 中值守的 AWR-V1 看洞时的捕获使接力在 P600 到站前就记了 4 次
            # keepout_priority 与 1 次 other；T5 变体中转岗的 P600 离开看洞点时记了 1.6 s 与 14.6 s 两次 other）
            own = not by or any(u in by and u in rt.sorties and rt.sorties[u].state == "WORKING" for u in (r.on_station, r.relief) if u)
            if r.first_capture is None and not own:
                G = False
            r.G = G
            if G:
                r.captured_t += dt
                if r.first_capture is None:
                    r.first_capture = t
                if r.gap_t0 is not None:
                    self._close_gap(r, t)
                r.t_lost = None
            elif r.first_capture is not None and r.gap_t0 is None:
                r.gap_t0 = (t_ok * 1e-9) if t_ok is not None and r.state != "DORMANT" else t
                r.gap_hint = self._cause(r, t)
                r.t_lost = t
                r.flick = [x for x in r.flick if t - x <= FLICKER_WIN_S] + [t]
            elif r.state == "DORMANT" and r.gap_t0 is not None and r.gap_hint != "infeasible":
                self._close_gap(r, t)
            # 交接重叠：A、B 同时处于明确捕获的连续时长（B 的捕获须稳定，D2-FX-R2：第 1 轮 (a) 闪断的累计重叠促成了到不了位的交接）
            if r.relief and r.on_station:
                a_in, b_in = r.on_station in by, r.relief in by
                if a_in and b_in:
                    r.ov_run += dt
                    r.overlap_acc = max(r.overlap_acc, r.ov_run)
                else:
                    r.ov_run = 0.0
                r.b_run = r.b_run + dt if b_in else 0.0
                r.a_miss = r.a_miss + dt if not a_in else 0.0
            # 被发现（我方机体触发）与最小禁入余量
            self._discovered(r)
            C = rt.circles(planning=False)
            for uav in (r.on_station, r.relief):
                if uav and uav in rt.sorties and rt.sorties[uav].airborne and len(C):
                    mg = float(signed_margin(rt.pos(uav)[None, :2], C)[0])
                    r.keep_margin_min = mg if r.keep_margin_min is None else min(r.keep_margin_min, mg)
            if t - r.t_zoom >= REZOOM_S:
                r.t_zoom = t
                for uav in (r.on_station, r.relief):
                    so = rt.sorties.get(uav or "")
                    if so is not None and so.state == "WORKING":
                        self._zoom(so, r)

    def _cause(self, r: RelayRT, t: float) -> str:
        """间隙原因（M20 §6.9.7 的优先序）：infeasible（休眠）> keepout_priority（撤离中）> battery（在岗机已离岗、接替因电池或
        通道延后）> illum_transition（照度档切换前后 15 min）> other（视线、遮挡、转场等）。D2-FX-R2：第 1 轮把"接替曾因电池延误"
        当作粘滞标记，视线丢失的间隙也记成 battery。"""
        if r.state == "DORMANT":
            return "infeasible"
        if r.retreating:
            return "keepout_priority"
        a = self.rt.sorties.get(r.on_station or "")
        a_ok = a is not None and a.state == "WORKING"
        if not a_ok and r.battery_wait:
            return "battery"
        if r.t_band_change is not None and abs(t - r.t_band_change) <= ILLUM_WINDOW_S:
            return "illum_transition"
        return "other"

    def _close_gap(self, r: RelayRT, t: float) -> None:
        if r.gap_t0 is None:
            return
        d = max(t - r.gap_t0, 0.0)
        if d >= GAP_MIN_S:
            c = r.gap_hint or "other"
            r.gaps_n[c] += 1
            r.gaps_s[c] += d
            r.gap_max = max(r.gap_max, d)
            r.gap_max_day = max(r.gap_max_day, d)
            self.rt.emit("relay.gap", 2, tid=r.tid, target=r.target, t_start_ns=int(r.gap_t0 * 1e9), duration_s=round(d, 2), cause=c)
        r.gap_t0 = None
        r.gap_hint = None

    def _discovered(self, r: RelayRT) -> None:
        trt = self.rt.targets_rt()
        T0 = trt._by_id(r.target) if trt is not None and hasattr(trt, "_by_id") else None
        if T0 is None:
            return
        last = getattr(T0, "last_disc", None) or {}
        key = (int(getattr(T0, "disc_n", 0)), last.get("t_sim_ns"))
        prev = r.disc_seen.get("k")
        r.disc_seen["k"] = key
        if prev is None or key == prev:
            return
        uav = last.get("uav")
        if uav and uav in self.rt.sorties:
            r.discovered += max(1, key[0] - (prev[0] if prev else 0))

    def config_inside(self, so: SortieRT, *, entered: bool) -> None:
        for T in self.rt.tasks.values():
            r = T.relay
            if r is not None and so.uav in (r.on_station, r.relief) and r.state != "ENDED":
                r.cfg_s += 0.2
                if entered:
                    r.cfg_entries += 1

    # ------------------------------------------------------------ 1 Hz：日切、派遣、交接、预报、跟随
    def step(self) -> None:
        rt = self.rt
        t = rt.t
        # S7 日切与软重置（M20-FR-074，dayroll.py）：会话级、按日历当地 04:00，与是否有运行中的接力无关（D2-FX-R2-other：此前按
        # 任务逐个在 UTC 04:00（北京 12:00）归档累计值，不清理交接记录，也没有任何可观测的输出）
        self.days.tick()
        for T in sorted(rt.tasks.values(), key=lambda x: x.tid):
            r = T.relay
            if r is None or T.state != "RUNNING":
                continue
            trt = rt.targets_rt()
            if trt is not None and trt.config(r.target) is None:
                with contextlib.suppress(TaskingError):
                    rt.cancel(T.tid, reason="target_removed")
                continue
            if r.state == "DORMANT":
                if r.dormant_until is not None and t >= r.dormant_until - self._lead(T):
                    r.state = "PRECHECK"
                    r.dormant_until = None
                    if "dormant" in T.warnings:
                        T.warnings.remove("dormant")
                    r.next_forecast = 0.0
                    self.precheck(T, wake=True)
                continue
            if r.state in ("PRECHECK", "ENDED"):
                continue
            if (r.relief_job is not None or r.station_job is not None) and t - r.job_t > JOB_STALE_S:
                # 站位作业结果丢失（被同键作业取代、超时或出错而未回调）：清除挂起标记，否则 `_relief_pending` 永远为真，此后不再派接替
                # （D2-FX-R2：霾时段之后每个在岗机都到 t_end 离岗、间隙 54 s 后才以 initial 重派）
                r.relief_job = r.station_job = None
            for it in T.items.values():
                if it.state == "PENDING" and it.relief_for and (it.relief_for != r.on_station
                                                               or (it.t_created is not None and t - it.t_created > PENDING_RELIEF_MAX_S)):
                    # 接替工作项的在岗机已离开（或长时间无人分配）：取消，由接替核对或开放工作项核对重新派遣（D2-FX-R2：sanfrancisco
                    # 中接替工作项 PENDING 而在岗机到 t_end 离岗后，此后不再派接替）
                    it.state = "CANCELED"
            self._sync_roles(T)
            self._band_watch(T)
            if r.state == "DORMANT":
                continue
            if r.on_station:
                so = rt.sorties.get(r.on_station)
                if so is not None and so.state == "WORKING":
                    self._search_check(T, so)
                    self._follow(T, so)
                    self._face(T, so)
                    self._relief_check(T, so)
                    self._weather_check(T, so)
                    self._flicker_check(T, so)
            if r.relief:
                b = rt.sorties.get(r.relief)
                if b is not None and b.state == "WORKING":
                    self._follow(T, b, reselect=False)    # 接替机同样按自己的相对偏移跟随运动目标
                    self._face(T, b)
                self._handover_check(T)
            if r.on_station is None and r.station is None and r.station_job is None and r.state in ("ACQUIRE", "TRACKING", "SEARCH") \
                    and t - r.t_station > 5.0 and not any(it.state in ("ASSIGNED", "ACTIVE") and not it.relief_for for it in T.items.values()):
                band, mor = rt.light_fc()
                feas = self._feas(r, band, mor)
                if feas:
                    self.request_station(T, feas, band, mor, reason="reselect")
                else:
                    r.t_station = t
            self._open_item_guard(T)

    def _lead(self, T: TaskRT) -> float:
        return 600.0 + self.rt.jobs.t_plan_s()

    def _reset_search(self, r: RelayRT) -> None:
        if r.state == "SEARCH":
            r.state = "TRACKING"
        r.search_n, r.reacq_n, r.reacq_t, r.lost_warned = 0, 0, -math.inf, False

    def _sync_roles(self, T: TaskRT) -> None:
        """由工作项分配推出在岗机与接替机。"""
        rt = self.rt
        r = T.relay
        for it in T.items.values():
            if it.state not in ("ASSIGNED", "ACTIVE") or not it.uav:
                continue
            if it.relief_for and it.relief_for == r.on_station:
                if r.relief != it.uav:
                    r.relief = it.uav
                    r.relief_t0 = rt.t
                    r.b_work_t0 = None
                    r.overlap_acc = r.ov_run = r.b_run = r.a_miss = 0.0
                    r.slow_moves = 0
                    eta = it.eta_s if it.eta_s is not None else 0.0
                    rt.emit("relay.dispatch", 1, uav=it.uav, tid=T.tid, target=r.target, reason=r.relief_reason or "energy",
                            eta_s=round(float(eta), 1), station=list(it.w.pos or ()))
            elif r.on_station is None and not it.relief_for:
                r.on_station = it.uav
                r.state = "TRACKING" if r.state in ("ACQUIRE", "SEARCH") else r.state
                self._reset_search(r)
                rt.emit("relay.dispatch", 1, uav=it.uav, tid=T.tid, target=r.target, reason="initial",
                        eta_s=round(float(it.eta_s or 0.0), 1), station=list(it.w.pos or ()))
                for i2 in T.items.values():
                    if i2 is not it and i2.state == "PENDING" and not i2.relief_for:
                        i2.state = "CANCELED"

    def _relief_pending(self, T: TaskRT) -> bool:
        r = T.relay
        return r.relief is not None or r.relief_job is not None or any(it.state == "PENDING" and it.relief_for for it in T.items.values())

    def _relief_check(self, T: TaskRT, so: SortieRT) -> None:
        rt = self.rt
        r = T.relay
        m = rt.model(rt.model_id(so.uav))
        if m is None:
            return
        t_end = rt.ex.t_end(so, m)
        b = rt.sorties.get(r.relief or "")
        if t_end - rt.t <= ENERGY_LEAVE_S and b is not None and b.state == "WORKING":
            # 在岗机到达 t_end：接替机已在站位时立即交接（M20 §6.9.4"A 若此时到达自身 t_end 则先返航"），重叠按已有的最长连续时长记
            self._promote(T, b, overlap=r.overlap_acc, old=so)
            return
        if t_end - rt.t <= ENERGY_LEAVE_S:
            # 能量见底而接替未到：在岗机离岗返航（不等 M09 能量 RTL）；间隙按 battery（接替因电池延误）或 other 归类
            _T, it = rt.ex.item_of(so)
            if it is not None:
                it.state = "DONE"
            rt.ex.release(so, "energy")
            r.on_station = None
            if b is not None:
                r.on_station, r.relief = b.uav, None
            for it2 in T.items.values():                # 接替工作项（已派或待派）改为首个站位工作项，由 _sync_roles 接续
                if it2.relief_for == so.uav:
                    it2.relief_for = None
            return
        if self._relief_pending(T):
            return
        _T, it = rt.ex.item_of(so)
        if it is None:
            return
        eta = rt.ex.eta_relief(so, it)
        need = (eta if math.isfinite(eta) else 300.0) + T_ACQ_S + T_OVERLAP_S + T_MARGIN_S + rt.jobs.t_plan_s()
        slack = (t_end - rt.t) - need
        if slack <= 0.0:
            r.t_plan_slack_min = slack if r.t_plan_slack_min is None else min(r.t_plan_slack_min, slack)
            r.relief_reason = "energy"
            pos = self._relief_pos(r, so)
            band, mor = rt.light_fc()
            cur = dict(self._feas(r, band, mor)).get(r.group) if r.group else None
            if pos is not None and not self._near_bad(r, pos) and self.station_ok(r, pos, cur, WINDOW_MARGIN_M) \
                    and self._ok_next_tenure(r, pos):
                self._new_item(T, pos, relief=so.uav)
                # 预规划下一交接站位（不在派遣关键路径上）
                feas = self._feas(r, band, mor)
                if feas and r.station_job is None:
                    self.request_station(T, feas, band, mor, reason="relief", others_extra=[list(rt.pos(so.uav))])
            else:
                self._request_relief(T, so, "energy")
        elif slack < 120.0:
            r.t_plan_slack_min = slack if r.t_plan_slack_min is None else min(r.t_plan_slack_min, slack)

    def _ok_next_tenure(self, r: RelayRT, pos: Any) -> bool:
        """交接站位在接替机的任期内（TENURE_EST_S）遇到照度档切换时，须同时落在下一档窗口内。"""
        if r.t_band_change is None or r.t_band_change - self.rt.t > TENURE_EST_S or not r.next_feas or r.group is None:
            return True
        return self.station_ok(r, pos, dict(r.next_feas).get(r.group), WINDOW_MARGIN_M)

    def _flicker_check(self, T: TaskRT, so: SortieRT) -> None:
        """视线闪断：FLICKER_WIN_S 内失捕 ≥ FLICKER_N 次（每次都很快重获，不进入搜索）时把当前站位记为坏站位（此后的接替与交接选站
        回避它），并告警 `flicker`。不为此另派接替：T5 变体诊断中闪断换站的接替机在另选站位上同样拿不到捕获，空等 15 min 后以重叠 0
        交接（D2-FX-R2，第 6 节遗留）。"""
        r = T.relay
        t = self.rt.t
        if r.state != "TRACKING" or t - r.flick_t < FLICKER_COOLDOWN_S:
            return
        if len([x for x in r.flick if t - x <= FLICKER_WIN_S]) < FLICKER_N:
            return
        r.flick_t = t
        r.flick = []
        self._mark_bad(r, self._cur_station(r, so))
        if "flicker" not in T.warnings:
            T.warnings.append("flicker")
            self.rt.emit("task.warning", 1, tid=T.tid, uav=so.uav, code="flicker", target=r.target)

    def _request_relief(self, T: TaskRT, so: SortieRT, why: str, *, feas: list | None = None, next_feas: list | None = None) -> None:
        """以站位作业为接替机选站（禁入圆外、离在岗机 ≥ 15 m、回避坏站位；跨照度档时取两档窗口交集），结果到达后派遣。"""
        rt = self.rt
        r = T.relay
        if self._relief_pending(T) or (rt.t - r.relief_retry_t < RELIEF_RETRY_S and why != "energy"):
            return
        band, mor = rt.light_fc()
        if feas is None:
            feas = self._feas(r, band, mor)
            if r.t_band_change is not None and r.t_band_change - rt.t <= TENURE_EST_S and r.next_feas:
                inter = _intersect(feas, r.next_feas)
                feas = inter or feas
        if not feas:
            return
        r.relief_reason = why
        self.request_station(T, feas, band, mor, reason=f"relief_now:{why}", others_extra=[list(rt.pos(so.uav))], next_feas=next_feas)

    def _relief_from_station(self, T: TaskRT, prim: tuple, hand: tuple | None, offset: dict | None, group: tuple, why: str) -> None:
        rt = self.rt
        r = T.relay
        a = rt.sorties.get(r.on_station or "")
        if a is None or r.relief is not None or any(it.state == "PENDING" and it.relief_for for it in T.items.values()):
            return
        pos = prim
        pa = tuple(rt.pos(a.uav))
        if math.hypot(prim[0] - pa[0], prim[1] - pa[1]) < HANDOVER_DH_M:
            pos = hand if hand is not None and math.hypot(hand[0] - pa[0], hand[1] - pa[1]) >= HANDOVER_DH_M else None
            if pos is None:
                r.handover_station = prim
                pos = self._relief_pos(r, a)
                if pos is None:
                    return
        if group != r.group:
            r.group = group
        if self._follows(r) and offset:
            r.offset = offset
        r.relief_reason = why
        r.handover_station = pos
        self._new_item(T, pos, relief=a.uav)

    def _relief_fallback(self, T: TaskRT, why: str) -> None:
        """接替选站失败：能量接替退回交接点（侧向错开），其余原因等待下一次核对。"""
        r = T.relay
        a = self.rt.sorties.get(r.on_station or "")
        if why != "energy" or a is None:
            return
        pos = self._relief_pos(r, a)
        if pos is not None:
            self._new_item(T, pos, relief=a.uav)

    def _relief_pos(self, r: RelayRT, so: SortieRT) -> tuple[float, float, float] | None:
        """交接站位：站位作业给出的交接点；没有（或与在岗机位置重合）时在在岗机同一高度、与识别物方位垂直的一侧错开
        HANDOVER_DH_M（两侧交替，DSM 净空不足时换另一侧，两侧都不足才退回上下错开 HANDOVER_DZ_M）。D2-INT：此前退回主站位本身，
        接替机飞到在岗机所在点而相撞 COLLISION_UAV；改为上下错开后，离岗机返航爬升穿过正上方的接替机再次相撞（S7 接力）。"""
        pos = r.handover_station or r.station
        if pos is None:
            return None
        try:
            cur = tuple(float(x) for x in np.asarray(self.rt.pos(so.uav), float)[:3])
        except Exception:
            cur = None
        if cur is None or math.hypot(float(pos[0]) - cur[0], float(pos[1]) - cur[1]) >= HANDOVER_DH_M:
            # 水平错开 ≥ 15 m（D2-FX-R2：此前按三维距离 8 m 判定，交接点在在岗机正下方 20 m 时接替机垂直下降穿过在岗机，COLLISION_UAV）
            return tuple(float(x) for x in pos)
        tp = self._track_pos(r)
        if tp is not None:
            b = math.atan2(float(tp[1]) - cur[1], float(tp[0]) - cur[0])
            sides = (1.0, -1.0) if r.handovers % 2 == 0 else (-1.0, 1.0)
            cands: list[tuple[float, float, float]] = []
            for lat in RELIEF_LAT_M:
                for sd in sides:
                    q = (cur[0] - sd * lat * math.sin(b), cur[1] + sd * lat * math.cos(b))
                    try:
                        ground = float(np.asarray(self.rt.dsm(np.array([q], float)), float).reshape(-1)[0])
                        dtm = float(np.asarray(self.rt.dtm(np.array([q], float)), float).reshape(-1)[0])
                    except Exception:
                        ground = dtm = float("nan")
                    if not math.isfinite(ground):
                        continue
                    # 接替机优先比在岗机低 HANDOVER_DZ_M（交接后 A 原地爬升返航，与 B 垂直错开），其次同高、高 HANDOVER_DZ_M
                    for dz in (-HANDOVER_DZ_M, 0.0, HANDOVER_DZ_M):
                        z = float(cur[2]) + dz
                        if z - ground >= RELIEF_CLEAR_M and (dz >= 0.0 or z - dtm >= 40.0) and z - dtm <= 120.0 + 1e-6:
                            cands.append((float(q[0]), float(q[1]), z))
            if cands:
                return self._los_pick(r, cands, tp)
        dz = HANDOVER_DZ_M if r.handovers % 2 == 0 else -HANDOVER_DZ_M
        return (float(cur[0]), float(cur[1]), float(cur[2]) + dz)

    def _los_pick(self, r: RelayRT, cands: list[tuple[float, float, float]], tp: tuple) -> tuple[float, float, float]:
        """按顺序取第一个三射线（识别物脚点上方 0.95/0.5/0.05·h）全可见的候选，其次两条可见，都没有时取第一个
        （D2-FX-R2：S7 中侧向错开的交接点被楼挡住，接替机到站 60 s 无捕获，在岗机到 t_end 后以重叠 0 交接）。"""
        try:
            g = float(np.asarray(self.rt.dtm(np.array([tp[:2]], float)), float).reshape(-1)[0])
        except Exception:
            return cands[0]
        h = self._target_h(r)
        A = np.repeat(np.asarray(cands, float), 3, axis=0)
        B = np.tile(np.array([[float(tp[0]), float(tp[1]), 0.0]]), (len(A), 1))
        B[:, 2] = g + np.tile(np.asarray((0.95, 0.5, 0.05)) * h, len(cands))
        vis = self.rt.los(A, B)
        if vis is None:
            return cands[0]
        k = np.asarray(vis, bool).reshape(len(cands), 3).sum(axis=1)
        for need in (3, 2):
            idx = np.flatnonzero(k >= need)
            if idx.size:
                return cands[int(idx[0])]
        return cands[0]

    def _handover_check(self, T: TaskRT) -> None:
        rt = self.rt
        r = T.relay
        a = rt.sorties.get(r.on_station or "")
        b = rt.sorties.get(r.relief or "")
        if b is None:
            r.relief = None
            return
        if a is None or a.state not in ("WORKING",):
            if b.state == "WORKING":
                self._promote(T, b, overlap=0.0)
            return
        if b.state != "WORKING":
            return
        if r.b_work_t0 is None:
            r.b_work_t0 = rt.t
        if r.ov_run >= T_OVERLAP_S:
            self._promote(T, b, overlap=r.ov_run, old=a)
            return
        if r.b_run >= T_OVERLAP_S and r.a_miss >= 5.0:
            # 在岗机已失去视线而接替机稳定捕获：A 临近 t_end（≤ 30 s）或失去视线已 ≥ 60 s 时交接（重叠按本次交接中同时捕获的最长连续
            # 时长记）；否则继续等 A 重获以达到 15 s 的同时捕获（D2-FX-R2：S7 4 h 诊断中 A 短暂失捕 5 s 即以重叠 6 s 交接）
            ma = rt.model(rt.model_id(a.uav))
            left = rt.ex.t_end(a, ma) - rt.t if ma is not None else 0.0
            if left <= A_MISS_LEFT_S or r.a_miss >= A_MISS_MAX_S:
                self._promote(T, b, overlap=r.overlap_acc, old=a)
                return
        slow = rt.t - r.b_work_t0 > HANDOVER_SLOW_S * (1 + r.slow_moves)
        if not slow and r.slow_moves == 0 and rt.t - r.b_work_t0 > HANDOVER_URGENT_S:
            # 在岗机临近 t_end（剩余 ≤ 90 s）而接替机到站 20 s 仍无捕获：不等 90 s，立即为 B 另选站位（否则到 t_end 只能以重叠 0 交接）
            ma = rt.model(rt.model_id(a.uav))
            slow = ma is not None and rt.ex.t_end(a, ma) - rt.t <= HANDOVER_URGENT_LEFT_S
        if r.ov_run == 0.0 and r.b_run == 0.0 and slow and r.slow_moves < 2 and r.station_job is None:
            # M20 §6.9.4：B 到达交接站位 90 s 仍未与 A 同时捕获 → 保持 A，以站位作业为 B 另选站位（回避当前站位），记 handover_slow
            if "handover_slow" not in T.warnings:
                T.warnings.append("handover_slow")
            r.slow_moves += 1
            _T, it_b = rt.ex.item_of(b)
            band, mor = rt.light_fc()
            feas = self._feas(r, band, mor)
            if feas and it_b is not None:
                self.request_station(T, feas, band, mor, reason="slow", others_extra=[list(rt.pos(a.uav))],
                                     avoid_extra=[list(it_b.w.pos or rt.pos(b.uav))])

    def stations_geometry(self, T: TaskRT) -> list[dict]:
        """接力的站位几何（计划 `geometry.stations`）：主站位（在岗机）与交接站位（接替机），按当前角色实时给出。"""
        r = T.relay
        out: list[dict] = []
        if r is None:
            return out
        a = self.rt.sorties.get(r.on_station or "")
        st = (a.station if a is not None and a.station is not None else None) or r.station
        if st is not None:
            out.append({"id": f"{T.tid}.st", "pos": list(st), "target": r.target, "role": "primary", "uav": r.on_station})
        b = self.rt.sorties.get(r.relief or "")
        ho = None
        if b is not None:
            _T, it_b = self.rt.ex.item_of(b)
            ho = tuple(it_b.w.pos) if it_b is not None and it_b.w.pos is not None else None
        ho = ho or r.handover_station
        if ho is not None:
            out.append({"id": f"{T.tid}.ho", "pos": list(ho), "target": r.target, "role": "handover", "uav": r.relief})
        return out

    def _promote(self, T: TaskRT, b: SortieRT, *, overlap: float, old: SortieRT | None = None) -> None:
        rt = self.rt
        r = T.relay
        if old is not None:
            _T, it_old = rt.ex.item_of(old)
            if it_old is not None:
                it_old.state = "DONE"
            prt = rt.perception_rt()
            if prt is not None:
                with contextlib.suppress(Exception):
                    prt.set_requirement(old.uav, r.target, None, None)
            rt.ex.release(old, "relieved")
            r.handovers += 1
            r.overlaps.append(float(overlap))
            r.ov_n += 1
            r.ov_sum += float(overlap)
            r.ov_min = float(overlap) if r.ov_min is None else min(r.ov_min, float(overlap))
            rt.emit("relay.handover", 1, tid=T.tid, from_uav=old.uav, to_uav=b.uav, overlap_s=round(float(overlap), 2))
        r.on_station, r.relief, r.relief_t0, r.relief_reason, r.slow_moves = b.uav, None, None, None, 0
        r.overlap_acc = r.ov_run = r.b_run = r.a_miss = 0.0
        r.battery_wait = False
        r.b_work_t0 = None
        self._reset_search(r)
        _T, it_b = rt.ex.item_of(b)
        if it_b is not None:
            it_b.relief_for = None
            r.station = tuple(it_b.w.pos) if it_b.w.pos is not None else r.station
            if (it_b.w.needs or {}).get("offset"):
                r.offset = dict(it_b.w.needs["offset"])
        if r.handover_station is not None and r.station is not None and math.dist(r.handover_station[:2], r.station[:2]) < 1.0:
            r.handover_station = None
        if r.handover_station is None or self._near_bad(r, r.handover_station):
            r.handover_station = None
            band, mor = rt.light_fc()
            feas = self._feas(r, band, mor)
            if feas and r.station_job is None:
                self.request_station(T, feas, band, mor, reason="relief", others_extra=[list(rt.pos(b.uav))])

    def _band_watch(self, T: TaskRT) -> None:
        """照度档预报（M20-FR-053 预报触发、FR-058 休眠）：每 60 s 取 45 min 内的下一档与其窗口；每秒判定休眠与预派时刻。
        D2-FX-R2：此前只比较"组是否可行"，同一传感器名在夜间窗口变窄（P600 GX40 白天 475 m 的站位在 nir_ill_l 窗口 320–455 m 之外）
        时从不预派；休眠只在在岗机工作时判定，且在档界前 5 s 才离开。"""
        rt = self.rt
        r = T.relay
        t = rt.t
        if t >= r.next_forecast:
            r.next_forecast = t + FORECAST_PERIOD_S
            env = getattr(rt.ctx, "env", None)
            fn = getattr(env, "next_band_change", None)
            nb = None
            if callable(fn):
                with contextlib.suppress(Exception):
                    nb = fn(rt.t_ns, FORECAST_HORIZON_S)
            if nb is None:
                r.t_band_change, r.next_band, r.next_feas = None, None, None
            else:
                _band, mor = rt.light_fc()
                r.t_band_change, r.next_band = nb[0] * 1e-9, str(nb[2])
                r.next_feas = self._feas(r, r.next_band, mor)
        if r.t_band_change is None or t >= r.t_band_change:
            return
        t_f = r.t_band_change
        if not r.next_feas:
            if t >= t_f - DORMANT_LEAD_S:
                _band, mor = rt.light_fc()
                self._dormant(T, self._next_feasible_t(r, mor), "infeasible")
            return
        a = rt.sorties.get(r.on_station or "")
        if a is None or a.state != "WORKING" or r.group is None:
            return
        w_next = dict(r.next_feas).get(r.group)
        cur_st = self._cur_station(r, a)
        if w_next and self.station_ok(r, cur_st, w_next, -WINDOW_TOL_M):
            return
        _T, it = rt.ex.item_of(a)
        eta = rt.ex.eta_relief(a, it) if it is not None else 300.0
        lead = (eta if math.isfinite(eta) else 300.0) + T_ACQ_S + T_OVERLAP_S + T_MARGIN_S + rt.jobs.t_plan_s() + 30.0
        if t >= t_f - lead and not self._relief_pending(T):
            band, mor = rt.light_fc()
            cur = self._feas(r, band, mor)
            inter = _intersect(cur, r.next_feas)
            r.groups = [g for g, _ws in (inter or r.next_feas)]
            r.allowed_models = tuple(sorted({g[0] for g in r.groups}))
            self._request_relief(T, a, "forecast", feas=inter or r.next_feas, next_feas=cur)

    def _weather_check(self, T: TaskRT, so: SortieRT) -> None:
        """当前 MOR 下站位仍在窗口内（每 10 s；霾时段窗口上界收缩），否则派接替到新窗口内的站位（D2-FX-R2）。"""
        rt = self.rt
        r = T.relay
        if rt.t < r.next_weather or r.group is None or self._relief_pending(T):
            return
        r.next_weather = rt.t + WEATHER_PERIOD_S
        band, mor = rt.light_fc()
        cur = dict(self._feas(r, band, mor)).get(r.group)
        # 迟滞：选站时距窗口上界留 WINDOW_MARGIN_M，核对时只在越出窗口 WINDOW_TOL_M 以上才换站（D2-FX-R2-tasking 第 2 次：航迹位置的抖动使贴着上界的
        # 站位在交接后立即被判出窗、立即再派接替，霾时段每次交接后 2 s 又派一架，架次翻倍、换电节律失步）
        if cur and self.station_ok(r, self._cur_station(r, so), cur, -WINDOW_TOL_M):
            return
        feas = self._feas(r, band, mor)
        if not cur and not feas:
            return                                   # 当前档全部不可行：由休眠逻辑处理
        m = rt.model(rt.model_id(so.uav))
        left = rt.ex.t_end(so, m) - rt.t if m is not None else 0.0
        _T, it = rt.ex.item_of(so)
        eta = rt.ex.eta_relief(so, it) if it is not None else math.inf
        if cur and left > WEATHER_MOVE_MIN_S and (not math.isfinite(eta) or eta > WEATHER_RELIEF_MAX_S) and r.station_job is None:
            # 能见度变化使站位落在窗口外：接替机一时到不了（电池、通道）而在岗机能量充足时，在岗机自己换到新窗口内的站位
            # （同组；站位作业按航迹位置复核视线），不等接替（D2-FX-R2-tasking 第 2 次：第 1 轮 (b) 第三架接替等电池期间在岗机失捕 35 min）
            r.reacq_t = rt.t
            self.request_station(T, feas, band, mor, reason="weather_move")
            return
        self._request_relief(T, so, "weather")

    def _next_feasible_t(self, r: RelayRT, mor: float) -> float | None:
        for t0, _t1, b in self.segments(24 * 3600.0):
            if t0 <= self.rt.t + 1e-6:
                continue
            if any(ws for _g, ws, _w in self.windows(r, b, mor)):
                return t0
        return None

    def _search_check(self, T: TaskRT, so: SortieRT) -> None:
        """失捕处置（M20-FR-057、§6.9.6）：捕获丢失 > 5 s 进入 SEARCH，先以航迹预测位置重新选站（站位作业保证视线与窗口，回避
        刚失去视线的站位，≤ REACQ_MAX 次、间隔 REACQ_PERIOD_S），仍未重获时在世界边界内飞外扩方形航线（禁入圆外，云台指向预测
        位置）；300 s 未重新捕获发 `task.warning{lost}` 并继续。撤离、交接期间不搜索。"""
        rt = self.rt
        r = T.relay
        b = rt.sorties.get(r.relief or "")
        if r.first_capture is None or r.retreating or (b is not None and b.state == "WORKING"):
            # 交接进行中（接替机已在站位上）不搜索；接替已派但未到站（等电池、转场）时在岗机照常重新选站与搜索（D2-FX-R2-tasking 第 2 次：第 1 轮 (b)
            # 霾时段接替机等电池 35 min，在岗机停在窗口外的站位上不重选，间隙 2156 s）
            return
        if r.t_lost is None:
            if r.state == "SEARCH":
                r.state = "TRACKING"
                self._reset_search(r)
                if (so.action or {}).get("search"):
                    self._end_search(T, so)
            return
        if rt.t - r.t_lost <= LOST_SEARCH_S:
            return
        if r.state != "SEARCH":
            r.state, r.search_t0, r.search_n, r.lost_warned = "SEARCH", rt.t, 0, False
            r.reacq_n, r.reacq_t = 0, -math.inf
            rt.emit("task.warning", 1, tid=T.tid, uav=so.uav, code="search", target=r.target)
        if rt.t - r.search_t0 > LOST_WARN_S and not r.lost_warned:
            r.lost_warned = True
            if "lost" not in T.warnings:
                T.warnings.append("lost")
            rt.emit("task.warning", 2, tid=T.tid, uav=so.uav, code="lost", target=r.target)
        if r.station_job is not None or rt.t - r.reacq_t < REACQ_PERIOD_S:
            return
        if r.reacq_n >= REACQ_MAX and rt.t - r.reacq_t >= REACQ_RESET_S:
            r.reacq_n = 0
        if r.reacq_n < REACQ_MAX:
            band, mor = rt.light_fc()
            feas = self._feas(r, band, mor)
            if feas:
                r.reacq_n += 1
                r.reacq_t = rt.t
                avoid = [] if self._follows(r) else [list(so.station or rt.pos(so.uav))]
                self.request_station(T, feas, band, mor, reason="reacquire", avoid_extra=avoid)
                return
        if (so.action or {}).get("op") != "path":
            r.reacq_t = rt.t
            self._search_leg(T, so)

    def _search_leg(self, T: TaskRT, so: SortieRT) -> None:
        from awr.tasking.keepout import inside, plan_route

        rt = self.rt
        r = T.relay
        tp = self._track_pos(r)
        if tp is None:
            return
        p = rt.pos(so.uav)
        d0 = max(math.hypot(p[0] - tp[0], p[1] - tp[1]), 30.0)
        half = d0 + SEARCH_STEP_M * r.search_n
        r.search_n = min(r.search_n + 1, SEARCH_LEGS - 1)
        corners = np.array([[tp[0] + sx * half, tp[1] + sy * half] for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1))])
        corners = self._clip_border(corners, np.asarray(tp[:2], float))
        C = rt.circles(planning=True)
        if len(C):
            corners = corners[~inside(corners, C)]
        if len(corners) < 2:
            return
        k0 = int(np.argmin(np.hypot(corners[:, 0] - p[0], corners[:, 1] - p[1])))
        ring = np.vstack([corners[(k0 + i) % len(corners)] for i in range(len(corners) + 1)])
        route = plan_route(np.vstack([p[None, :2], ring]), C) if len(C) else np.vstack([p[None, :2], ring])
        if route is None:
            return
        z = rt.ex.transit_z(route, float(p[2]))
        so.actions = []
        so.action = {"op": "path", "search": True}
        so.hover_until = None
        rt.ex._path(so, np.c_[route[1:], np.full(len(route) - 1, z)])

    def _clip_border(self, xy: np.ndarray, center: np.ndarray) -> np.ndarray:
        """把点沿指向 center 的方向收进世界边界（内缩 SEARCH_INSET_M）；没有 zones 时原样。"""
        zones = getattr(self.rt.world, "zones", None) if self.rt.world is not None else None
        if zones is None:
            return xy
        out = np.asarray(xy, float).copy()
        for k in range(len(out)):
            for _ in range(12):
                try:
                    dd = float(np.asarray(zones.border_signed_distance(out[k:k + 1]), float)[0])
                except Exception:
                    dd = math.inf
                if dd >= SEARCH_INSET_M:
                    break
                out[k] = center + (out[k] - center) * 0.8
        return out

    def search_failed(self, so: SortieRT) -> None:
        """搜索航段被细校验拒绝（越界、禁飞区或穿越建筑）：就地悬停，下一周期改为重新选站（不计作业失败）。"""
        T = self.rt.tasks.get(so.tid or "")
        r = T.relay if T is not None else None
        if r is not None:
            r.reacq_n = 0
            r.reacq_t = -math.inf

    def _end_search(self, T: TaskRT, so: SortieRT) -> None:
        rt = self.rt
        _T, it = rt.ex.item_of(so)
        if it is None:
            return
        m = rt.model(rt.model_id(so.uav))
        p = rt.pos(so.uav)
        C = rt.circles(planning=True)
        if m is not None and m.frame == "multirotor" and (len(C) == 0 or float(signed_margin(p[None, :2], C)[0]) >= 0.0):
            it.w.pos = (float(p[0]), float(p[1]), float(p[2]))      # 重新捕获处即新站位
            so.actions, so.action, so.hover_until = [], {"op": "hold"}, None
            so.station = it.w.pos
            tp = self._track_pos(T.relay)
            if tp is not None and math.hypot(tp[0] - p[0], tp[1] - p[1]) > 1.0:
                rt.ex.yaw_goto(so, p, math.atan2(tp[1] - p[1], tp[0] - p[0]))
            else:
                rt.ex.submit(so, "hover", {})
            T.relay.station = it.w.pos
            T.revision += 1
        else:
            rt.ex.start_transit(so)

    def _follow(self, T: TaskRT, so: SortieRT, *, reselect: bool = True) -> None:
        """运动目标跟随（M20-FR-055）：期望站位 = 航迹预测位置 + 相对偏移；偏差 > 5 m 时 goto，位移 > 20 m 重选站位。"""
        rt = self.rt
        r = T.relay
        if not self._follows(r) or (r.offset is None and reselect) or (r.state == "SEARCH" and reselect):
            return
        m = rt.model(rt.model_id(so.uav))
        if m is not None and m.frame != "multirotor":
            return      # 固定翼与垂起机以识别物为心盘旋，不按偏移 goto 跟随（D2-FX-R2：垂起机收到跟随 goto 转多旋翼，sanfrancisco 中 TILT_KILL）
        tp = self._track_pos(r)
        if tp is None:
            return
        if reselect and r.station_pos_ref is not None and math.dist(tp[:2], r.station_pos_ref) > MOVE_RESELECT_M and r.station_job is None:
            band, mor = rt.light_fc()
            feas = self._feas(r, band, mor)
            if feas:
                self.request_station(T, feas, band, mor, reason="follow")
        _T, it = rt.ex.item_of(so)
        des = self.station_for(so, it) if it is not None else None
        if des is None:
            return
        p = rt.pos(so.uav)
        if math.dist(p[:2], des[:2]) <= FOLLOW_TOL_M:
            return
        # 新的期望站位偏离上一次跟随目标 > 5 m，或上一次跟随已发出 10 s 仍未到位时再发（D2-FX-R2：此前以"上一条命令是 goto"
        # 阻止重发，goto 完成后 call_op 仍为 goto，运动目标只跟随一次）
        last = getattr(so, "follow_goal", None)
        if last is not None and math.dist(last[:2], des[:2]) <= FOLLOW_TOL_M and rt.t - getattr(so, "follow_t", -1e9) < 10.0:
            return
        C = rt.circles(planning=False)
        if len(C) and float(signed_margin(np.array([des[:2]]), C)[0]) < 0:
            return
        so.follow_goal, so.follow_t = tuple(float(x) for x in des[:3]), rt.t
        from awr.tasking.keepout import seg_blocked

        Cp = self.station_circles()        # 站位口径（运动识别物：守卫基础半径 + v·τ_pred + 跟随容差），与站位选择一致
        if (math.dist(p[:2], des[:2]) > 30.0 and not rt.ex.direct_ok(np.asarray(p, float), np.asarray(des, float), Cp)) \
                or (len(Cp) and len(seg_blocked(p[:2], des[:2], Cp))):
            rt.ex.start_transit(so)                  # 换站直线不安全：按禁入圆外航线转场（到站后重新指向与变焦）
            return
        if abs(tp[0] - des[0]) + abs(tp[1] - des[1]) > 1.0:
            rt.ex.yaw_goto(so, des, math.atan2(tp[1] - des[1], tp[0] - des[0]))     # 跟随中机头朝识别物（云台方位限位 ±160°）
        else:
            rt.ex.submit(so, "goto", {"pos": [float(des[0]), float(des[1]), float(des[2])], "route": "direct"})

    def _face(self, T: TaskRT, so: SortieRT) -> None:
        """站位上的多旋翼：识别物相对机头的方位超过 FACE_DEG（接近云台方位限位 ±160°）时原地转向（goto 当前点带 yaw）。"""
        rt = self.rt
        if so.state != "WORKING" or (so.action or {}).get("op") != "hold" or rt.t - so.follow_t < 5.0:
            return
        m = rt.model(rt.model_id(so.uav))
        if m is not None and m.frame != "multirotor":
            return
        tp = self._track_pos(T.relay)
        yaw = rt.yaw(so.uav)
        if tp is None or yaw is None:
            return
        p = rt.pos(so.uav)
        b = math.atan2(tp[1] - p[1], tp[0] - p[0])
        if abs(math.atan2(math.sin(b - yaw), math.cos(b - yaw))) < math.radians(FACE_DEG):
            return
        so.follow_goal, so.follow_t = tuple(float(x) for x in p[:3]), rt.t
        rt.ex.yaw_goto(so, p, b)

    def _open_item_guard(self, T: TaskRT) -> None:
        """在岗机丢失（DROPPED、被接管）且没有接替：重派新站位工作项。"""
        rt = self.rt
        r = T.relay
        if r.on_station and r.on_station not in rt.sorties:
            r.on_station = None
        if r.relief and r.relief not in rt.sorties:
            r.relief = None
        if r.on_station is None and r.relief and r.relief in rt.sorties:
            b = rt.sorties[r.relief]
            if b.state == "WORKING":
                self._promote(T, b, overlap=0.0)
                return
        if r.on_station is None and r.relief is None and r.station is not None and r.state not in ("DORMANT", "ENDED") \
                and not any(it.state in ("PENDING", "ASSIGNED", "ACTIVE") for it in T.items.values()):
            if self._near_bad(r, r.station):
                r.station = None
                return
            self._new_item(T, r.station)

    # ------------------------------------------------------------ 执行器回调
    def on_evade(self, so: SortieRT) -> None:
        for T in self.rt.tasks.values():
            r = T.relay
            if r is not None and so.uav in (r.on_station, r.relief):
                if not r.retreating and not self._follows(r):
                    # 静止识别物：从站位撤离说明站位贴着禁入圆，记为坏站位，撤离后重选时回避（D2-FX-R2：到站—撤离循环 14.6 h）
                    self._mark_bad(r, self._cur_station(r, so))
                r.retreating = True
                if r.gap_t0 is not None and r.gap_hint in (None, "other"):
                    r.gap_hint = "keepout_priority"

    def after_retreat(self, so: SortieRT, it: ItemRT) -> None:
        T = self.rt.tasks.get(it.w.tid)
        r = T.relay if T is not None else None
        if r is None:
            return
        r.retreating = False
        band, mor = self.rt.light_fc()
        feas = self._feas(r, band, mor)
        if feas and r.station_job is None:
            self.request_station(T, feas, band, mor, reason="follow")

    def on_transit_failed(self, so: SortieRT, it: ItemRT) -> None:
        """接力站位的转场被细校验与越障重试都拒绝（越界、禁飞区或穿越建筑）：记为回避点，取消该工作项并重新选站（D2-FX-R2：
        第 1 轮同一个不可达站位被反复派给每一架新机，每架次起飞约 90 s 即返航）。"""
        T = self.rt.tasks.get(it.w.tid)
        r = T.relay if T is not None else None
        if r is None:
            return
        self._mark_bad(r, it.w.pos)
        it.state = "CANCELED"
        if it.relief_for:
            if r.relief == so.uav:
                r.relief = None
            if r.handover_station is not None and self._near_bad(r, r.handover_station):
                r.handover_station = None
        else:
            if r.on_station == so.uav:
                r.on_station = None
            if r.station is not None and self._near_bad(r, r.station):
                r.station = None
            r.t_station = -math.inf

    def on_launch(self, so: SortieRT) -> None:
        T = self.rt.tasks.get(so.tid or "")
        if T is not None and T.relay is not None:
            T.relay.battery_wait = False

    def on_dropped(self, so: SortieRT, why: str) -> None:
        for T in self.rt.tasks.values():
            r = T.relay
            if r is None:
                continue
            if so.uav == r.on_station:
                r.on_station = None
            if so.uav == r.relief:
                r.relief = None
        del why

    def on_preempt(self, so: SortieRT) -> None:
        self.on_dropped(so, "manual")

    def note_delay(self, T: TaskRT, reason: str) -> None:
        for R in self.rt.tasks.values():
            r = R.relay
            if r is not None and (R.tid == T.tid or R.parent == T.tid):
                if reason in r.delays:
                    r.delays[reason] += 1
                if reason in ("battery", "charger"):
                    r.battery_wait = True

    def account_sortie(self, so: SortieRT, used_wh: float, flight_s: float) -> None:
        T = self.rt.tasks.get(so.tid or "")
        r = T.relay if T is not None else None
        if r is None:
            return
        r.sorties += 1
        r.flight_s += max(flight_s, 0.0)
        r.energy_wh += max(used_wh, 0.0)

    def count_energy_rtl(self, so: SortieRT) -> None:
        T = self.rt.tasks.get(so.tid or "")
        if T is not None and T.relay is not None:
            T.relay.energy_rtl += 1

    # ------------------------------------------------------------ 统计
    def stats_wire(self, T: TaskRT) -> dict:
        """`awr.relay.stats.v1`（AWR-04 §9.5；契约 `tasking/relay_stats.schema.json`）。进行中的间隙计入次数、时长与最大值（批处理
        结束时由 `finalize` 闭合并发事件，统计与事件流一致）。"""
        rt = self.rt
        r = T.relay
        mon = r.t_monitor
        cov = r.captured_t / mon if mon > 0 else 0.0
        gn, gs, gmax = dict(r.gaps_n), dict(r.gaps_s), r.gap_max
        if r.gap_t0 is not None and rt.t - r.gap_t0 >= GAP_MIN_S:
            c = r.gap_hint or "other"
            d = rt.t - r.gap_t0
            gn[c] += 1
            gs[c] += d
            gmax = max(gmax, d)
        on = []
        for uav, role in ((r.on_station, "st"), (r.relief, "ho")):
            if uav:
                so = rt.sorties.get(uav)
                on.append({"uav": uav, "station": f"{T.tid}.{role}", **({"since_t_sim_ns": int(so.t_state * 1e9)} if so is not None else {})})
        nxt = None
        pend = next((it for it in T.items.values() if it.relief_for and it.state in ("PENDING", "ASSIGNED", "ACTIVE")), None)
        if pend is not None:
            nxt = {"uav": pend.uav, "eta_s": None if pend.eta_s is None else round(float(pend.eta_s), 1), "reason": r.relief_reason or "energy"}
        n_ready = [n.min_ready for n in rt.nests.values() if n.min_ready is not None]
        out = {"tid": T.tid, "target": r.target, "t_monitor_s": round(mon, 2), "coverage": round(min(max(cov, 0.0), 1.0), 6),
               "gaps": {"count": int(sum(gn.values())), "max_s": round(gmax, 2), "by_cause": {k: int(v) for k, v in gn.items()},
                        "total_s_by_cause": {k: round(float(v), 2) for k, v in gs.items()}},
               "discovered": int(r.discovered), "config_entries": {"count": int(r.cfg_entries), "total_s": round(r.cfg_s, 2)},
               "handovers": {"count": int(r.handovers), "mean_overlap_s": round(r.ov_sum / r.ov_n, 2) if r.ov_n else None,
                             "min_overlap_s": round(r.ov_min, 2) if r.ov_n and r.ov_min is not None else None},
               "min_keepout_margin_m": None if r.keep_margin_min is None else round(r.keep_margin_min, 2), "sorties": int(r.sorties),
               "flight_h": round(r.flight_s / 3600.0, 4), "energy_wh": round(r.energy_wh, 2), "min_ready_packs": min(n_ready) if n_ready else None,
               "energy_rtl": int(r.energy_rtl), "dispatch_delays": {k: int(v) for k, v in r.delays.items()}, "on_station": on, "next": nxt}
        return out

    def stats_ext(self, T: TaskRT) -> dict:
        """批处理 `relay_stats.json` 的扩展字段（状态、首次捕获、t_plan 余量、日表、不可行时段）。"""
        r = T.relay
        return {"state": r.state, "first_capture_s": None if r.first_capture is None else round(r.first_capture, 2),
                "t_plan_slack_min_s": None if r.t_plan_slack_min is None else round(r.t_plan_slack_min, 2), "day_table": list(r.day_table),
                "infeasible_periods": [{"band": p["band"], "t_from_s": round(p["t_from"], 1), "t_to_s": round(p["t_to"], 1)}
                                       for p in r.infeasible_periods], "level_req": r.level_req, "group": list(r.group) if r.group else None,
                "bad_stations": [list(b) for b in r.bad]}


def _intersect(a: list, b: list) -> list:
    """两组窗口 [(组, [(agl, lo, hi)])] 的交集（同组同 AGL 取 [max lo, min hi]，宽度 ≥ 10 m 才保留）。"""
    bd = dict(b)
    out = []
    for g, ws in a:
        w2 = {float(x[0]): x for x in bd.get(g, [])}
        rows = []
        for agl, lo, hi in ws:
            o = w2.get(float(agl))
            if o is None:
                continue
            lo2, hi2 = max(float(lo), float(o[1])), min(float(hi), float(o[2]))
            if hi2 - lo2 >= 10.0:
                rows.append((float(agl), lo2, hi2))
        if rows:
            out.append((g, rows))
    return out


def band_segments(rt: Any, horizon_s: float) -> list[tuple[float, float, str]]:
    """未来 horizon 内的照度档分段 [(t0, t1, band)]（仿真秒；M07 `next_band_change` 的 1 s 精度档界）；M07 未装配时为一段。"""
    band0, _mor = rt.light()
    t0 = rt.t
    end = t0 + float(horizon_s)
    env = getattr(rt.ctx, "env", None)
    fn = getattr(env, "next_band_change", None)
    out: list[tuple[float, float, str]] = []
    band = band0
    t = t0
    for _ in range(64):
        nb = None
        if callable(fn):
            with contextlib.suppress(Exception):
                nb = fn(round(t * 1e9), max(end - t, 0.0))
        if nb is None or nb[0] * 1e-9 >= end:
            out.append((t, end, band))
            break
        tc = nb[0] * 1e-9
        out.append((t, tc, band))
        band, t = str(nb[2]), tc
    return out


def _why_code(why: str | None) -> str:
    return {"light": "night_sensor", "blur": "night_sensor", "pixels": "night_sensor", "no_sensor": "night_sensor", "narrow": "night_sensor",
            "contrast": "weather", "los": "los", "keepout_gt_range": "keepout_gt_range", "link_range": "link_range", "wind": "wind"}.get(
        str(why or ""), str(why or "night_sensor"))


def raise_infeasible(rt: Any, T: TaskRT, code: int, why: str, **detail: Any) -> None:
    T.last_error = {"code": int(code), "why": why, "detail": {"why": why, **detail}}
    if T.relay is not None:
        T.relay.state = "ENDED"
    rt._set_state(T, "INFEASIBLE", int(code), why)


def level_rank(l: str) -> int:  # noqa: E741 - 线上等级名
    return LEVEL_RANK.get(l, 0)
