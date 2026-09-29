"""MissionEngine（M10-FR-001 至 FR-010、FR-054；M10 §6.4、§6.7、§7.4.4；AWR-12 §4.5、§5.8.4、§6.4）。

Mission（M01–M09）与 Track（K01–K12）状态机的实现（语义以 AWR-12 §4.5 为准）：

- 创建（M01）：参数按 `gen_<name>` schema 校验后提交 generator 作业（plan-pool，优先级 1），结果生效后得到每机作业项
  与轨迹（缓存键 traj_key），Mission 为 IDLE；
- 启动（M02/M03）：能量预检（§6.5.17，`energy_reserve` 0.20；剧本 reject → 119，UI 创建 warn → `mission.energy_warning`），
  各 Track 以 principal `mission:<mid>` 经 CommandEngine 准入 ④–⑩ 下发内部调用（隐式取得 MISSION 租约）：
  在地面先 takeoff（10 m AGL）→ safe_transit 入场（plan-pool，按 rank 分层 Δz）→ 作业项（follow_path 装入预生成轨迹、
  orbit）→ on_done（rtl、hover、land）→ 释放租约；
- 暂停、恢复、中止（M04–M06）：暂停在跟踪器上把轨迹时钟倍率以 a_brake 斜坡降到 0，机体停在轨迹上后交回 HOLD，
  调用保持在途（暂停期间延长截止时间）；恢复时回到 TRAJ 并从 τ 斜坡恢复；中止执行 on_abort 并以 `return_to = none`
  释放租约；
- 挂起与续飞（K06/K07）：被取代（206）、租约抢占、安全抢占为 HOLD（204，例如 FleetGuard 让行）→ SUSPENDED 并记录续飞点
  （当前项 + τ）；租约回到本任务且机体可用时（`resume_on_lease_return`）经 plan-pool 规划"转场 + 剩余部分"续飞；
  同一对机体 60 s 内让行 ≥ 3 次保持 SUSPENDED（FR-054）；ELAND、FAILSAFE、坠毁、失联、能量 RTL → DROPPED（K11）；
- 结束（M07/M08）：全部 Track 终态且至少一条 DONE → DONE（`incomplete` = 存在 DROPPED）；全部 DROPPED → ABORTED。
Track 投影到状态块 `mission_item`（当前项 seq，0xFFFF 为无）与 `track_state`（本表的序号）。
事件经 `ctx.events.emit`（按步合批，§7.3）；本模块不读墙钟（status 节流用注入的 SimClock 墙钟）。
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from awr.sim.planning import bspline as BS
from awr.sim.planning.jobs import BUDGET_MS, PRIO_START, PlanRequest, PlanResult

from . import energy as EN

if TYPE_CHECKING:
    from .runtime import M10Runtime

__all__ = ["MISSION_STATES", "TRACK_STATES", "MissionEngine", "MissionRT", "TrackRT"]

log = logging.getLogger("awr.sim.mission.engine")

MISSION_STATES = ("IDLE", "RUNNING", "PAUSED", "DONE", "ABORTED")
TRACK_STATES = ("PENDING", "TRANSIT", "WORKING", "WAITING", "SUSPENDED", "RETURNING", "DONE", "DROPPED")
TRACK_CODE = {s: i for i, s in enumerate(TRACK_STATES)}
TAKEOFF_AGL_M = 10.0
BARRIER_TIMEOUT_NS = 30_000_000_000
SYNC_REASONS = ("barrier", "deconflict_sync", "formation_sync")    # _poll_barrier 负责的等待（集结两段由 _poll_assemble 负责）
SYNC_TIMEOUT_NS = 300_000_000_000       # 首项同步（4D 消解）等待上限【仿真】：各机入场转场时长可相差数分钟
YIELD_WINDOW_NS = 60_000_000_000
STATUS_MIN_WALL_NS = 500_000_000
HEARTBEAT_WALL_NS = 1_000_000_000
# 调用结果 → Track 动作
DROP_CODES = {207, 208}                  # VEHICLE_LOST、CRASHED
SUSPEND_CODES = {206, 210, 202, 203, 6}  # SUPERSEDED、LEASE_PREEMPTED、PROGRESS_TIMEOUT、STALLED、CANCELLED
FS_DROP = {"ELAND", "FAILSAFE", "CRASHED", "LANDING", "RTL", "LANDED", "DISARMED"}


@dataclass
class TrackRT:
    vehicle_id: str
    slot: int
    state: str = "PENDING"
    items: list[dict] = field(default_factory=list)
    cursor: int = 0
    step: str = "idle"          # idle、takeoff、plan_transit、transit、item、done_action、wait_sync、plan_resume
    cid: str | None = None
    ncall: int = 0
    reason: str | None = None
    paused: bool = False
    resume: dict | None = None
    job: str | None = None
    progress: float = 0.0
    t_state_ns: int = 0
    yields: deque = field(default_factory=lambda: deque(maxlen=8))
    lagging: bool = False
    rank: int = 0
    pending_items: list[dict] = field(default_factory=list)   # 续飞时插入的"转场 + 剩余部分"
    done_items: int = 0
    wait_since_ns: int = -1
    delay_s: float = 0.0          # 任务内 4D 消解给出的首项延迟（FR-056，ext）
    layer_m: float = 0.0
    synced: bool = False
    not_before_ns: int = -1
    asm: str = ""                 # 编队集结：""、lift、capt_wait、capt、done（done 含退回分层转场）
    photos: int = 0               # camera.trigger 的逻辑计数（FR-008）
    rej_n: int = 0                # 连续准入拒绝次数（INT-1：续飞退避，避免 10 Hz 重试风暴）
    retry_at_ns: int = -1

    @property
    def total(self) -> int:
        return len(self.items)


@dataclass
class MissionRT:
    mid: str
    spec: dict
    origin: str
    principal: dict
    state: str = "IDLE"
    tracks: dict[str, TrackRT] = field(default_factory=dict)
    revision: int = 0
    gen: str = "none"           # none、pending、ok、failed
    gen_job: str | None = None
    gen_error: dict | None = None
    extra: dict = field(default_factory=dict)
    trajs: dict[str, dict] = field(default_factory=dict)
    start_requested: bool = False
    start_policy: str = "reject"
    t_start_ns: int = 0
    t_created_ns: int = 0
    incomplete: bool = False
    energy: dict = field(default_factory=dict)
    last_status: dict | None = None
    last_pub_wall: int = 0
    dirty: bool = True
    reason: str | None = None
    plan_ms: float = 0.0
    precheck: str = "reject"
    auto_start_blocked: bool = False
    start_result: dict | None = None
    deconf_job: str | None = None
    deconf: dict | None = None
    fphase: str = "PLANNED"       # 编队阶段（§6.4.3，ext）：PLANNED、LIFTING、ASSEMBLING、CRUISE、DISBANDED
    assemble_job: str | None = None
    assemble: dict | None = None

    @property
    def vehicles(self) -> list[str]:
        return list(self.spec.get("vehicle_ids") or [])


class MissionEngine:
    def __init__(self, rt: M10Runtime) -> None:
        self.rt = rt
        self.missions: dict[str, MissionRT] = {}
        self.order: list[str] = []
        self._results: deque = deque()
        self._by_cid: dict[str, tuple[str, str]] = {}
        self._n = 0
        self.stats = {"calls": 0, "results": 0, "rejected": 0}

    # ------------------------------------------------------------ 公共 API（§7.4.4）
    def create(self, spec: dict, origin: str = "scenario", principal: dict | None = None, *, precheck: str | None = None,
               start: dict | None = None) -> str:
        from .model import MissionSpec

        ms = MissionSpec.model_validate(dict(spec) | {"origin": origin})
        mid = ms.mission_id
        if mid in self.missions:
            raise ValueError(f"mission id exists: {mid}")
        d = ms.model_dump(mode="json")
        if start is not None:
            d["start"] = start
        pr = dict(principal or {})
        pr.setdefault("principal_id", f"mission:{mid}")
        m = MissionRT(mid, d, origin, {"principal_id": f"mission:{mid}", "role": "mission", "entry": "scenario",
                                       "seat": False, "source": "MISSION"})
        m.precheck = precheck or ("reject" if origin == "scenario" else "warn")
        m.t_created_ns = self.rt.t_ns
        roster = self.rt.roster
        for i, vid in enumerate(ms.vehicle_ids):
            e = roster.resolve(vid) if roster is not None else None
            m.tracks[vid] = TrackRT(vid, e.slot if e is not None else -1, rank=i)
        self.missions[mid] = m
        self.order.append(mid)
        self.rt.emit("mission.created", mid=mid, generator=ms.generator, vehicle_ids=list(ms.vehicle_ids),
                     revision=m.revision, origin=origin)
        self._generate(m)
        return mid

    def start(self, mid: str, principal: dict | None = None) -> dict:
        m = self.missions.get(mid)
        if m is None:
            return {"code": 305, "detail": {"mid": mid}}
        if m.state != "IDLE":
            return {"code": 105, "detail": {"state": m.state}}
        if m.gen == "failed":
            return {"code": 125, "detail": m.gen_error}
        m.start_requested = True
        m.auto_start_blocked = False
        if m.gen != "ok":
            return {"code": 0, "detail": {"pending": "generator"}}
        r = self._try_start(m)
        m.start_result = r
        if r.get("code"):
            m.start_requested = False
        return r

    def pause(self, mid: str, principal: dict | None = None) -> dict:
        m = self.missions.get(mid)
        if m is None:
            return {"code": 305}
        if m.state != "RUNNING":
            return {"code": 105, "detail": {"state": m.state}}
        for t in m.tracks.values():
            if t.state in ("TRANSIT", "WORKING", "WAITING", "RETURNING"):
                t.paused = True
                if t.cid is not None and t.step in ("transit", "item") and t.slot >= 0:
                    self.rt.tracker.pause_slot(t.slot)       # 时钟斜坡停在轨迹上（FR-007）；调用保持在途
        self._set_state(m, "PAUSED", "pause")
        return {"code": 0}

    def resume(self, mid: str, principal: dict | None = None) -> dict:
        m = self.missions.get(mid)
        if m is None:
            return {"code": 305}
        if m.state != "PAUSED":
            return {"code": 105, "detail": {"state": m.state}}
        S = self.rt.S
        for t in m.tracks.values():
            if t.slot >= 0 and self.rt.fs_name(t.slot) in ("ELAND", "FAILSAFE"):
                return {"code": 101, "detail": {"vehicle_id": t.vehicle_id}}
        for t in m.tracks.values():
            if t.paused:
                t.paused = False
                if t.cid is not None and t.step in ("transit", "item") and t.slot >= 0:
                    self.rt.tracker.resume_slot(S, t.slot, self.rt.t_ns * 1e-9)
        self._set_state(m, "RUNNING", "resume")
        return {"code": 0}

    def abort(self, mid: str, principal: dict | None = None, reason: str = "operator") -> dict:
        m = self.missions.get(mid)
        if m is None:
            return {"code": 305}
        if m.state not in ("RUNNING", "PAUSED", "IDLE"):
            return {"code": 105, "detail": {"state": m.state}}
        on_abort = str(m.spec.get("on_abort", "hover"))
        for t in m.tracks.values():
            if t.state in ("DONE", "DROPPED"):
                continue
            if t.state != "PENDING" and t.slot >= 0 and self.rt.airborne(t.slot):
                self._submit(m, t, on_abort if on_abort != "land" else "land", {}, step="abort")
            self._release_lease(m, t)
            self._track(m, t, "DROPPED", "abort")
        self._set_state(m, "ABORTED", reason)
        self._set_fphase(m, "DISBANDED")
        self.rt.emit("mission.aborted", mid=mid, reason=reason, level=2)
        return {"code": 0}

    def reset(self) -> None:
        for m in self.missions.values():
            for t in m.tracks.values():
                if t.job:
                    self.rt.pool_cancel(t.job)
        self.missions.clear()
        self.order.clear()
        self._results.clear()
        self._by_cid.clear()

    # ------------------------------------------------------------ 生成
    def _vehicle_payload(self, m: MissionRT) -> list[dict]:
        out = []
        rt = self.rt
        for vid, t in m.tracks.items():
            if t.slot < 0:
                e = rt.roster.resolve(vid) if rt.roster is not None else None
                t.slot = e.slot if e is not None else -1
            if t.slot < 0:
                continue
            s = t.slot
            out.append({"vehicle_id": vid, "home_enu_m": rt.S.enu.home[s].tolist(), "pos_enu_m": rt.S.enu.pos[s].tolist(),
                        "v_limit_mps": rt.v_limit(s), "cruise_mps": rt.cruise(s), "r_col_m": rt.r_col(s)})
        return out

    def _generate(self, m: MissionRT) -> None:
        rt = self.rt
        veh = self._vehicle_payload(m)
        if not veh:
            m.gen = "failed"
            m.gen_error = {"code": 305, "detail": "NO_VEHICLE"}
            return
        payload = {"generator": m.spec["generator"], "params": m.spec["params"], "vehicles": veh,
                   "constraints": m.spec.get("constraints") or {}, "zones": rt.zones, "mission_id": m.mid,
                   "camera": rt.camera}
        m.revision += 1
        jid = f"generator:{m.mid}:{m.revision}"
        req = PlanRequest(jid, "generator", rt.world_key, tuple(v["vehicle_id"] for v in veh), payload, rt.limits_dict(),
                          PRIO_START, rt.tick, BUDGET_MS["generator"], f"mission:{m.mid}")
        m.gen, m.gen_job = "pending", jid
        rt.pool.submit(req, lambda res, tick, mid=m.mid: self._on_generated(mid, res, tick))

    def _on_generated(self, mid: str, res: PlanResult, tick: int) -> None:
        m = self.missions.get(mid)
        if m is None or res.job_id != m.gen_job:
            return
        m.plan_ms = float(res.stats.get("t_ms", 0.0))
        if not res.ok:
            m.gen = "failed"
            m.gen_error = {"code": int(res.code or 125), "detail": res.detail, "remedy": res.remedy}
            self.rt.emit("plan.failed", job_id=res.job_id, code=int(res.code or 125), detail=res.detail,
                         remedy=res.remedy, level=2)
            if res.detail == "FORMATION_INFEASIBLE":
                self.rt.emit("formation.infeasible", mid=mid, reason=res.detail, remedy=res.remedy, level=2)
            m.dirty = True
            return
        cache = self.rt.tracker.cache
        for tr in res.trajectories:
            e = cache.put_traj(tr)
            cache.pin(e)
            m.trajs[tr["key"]] = tr
        for it in res.items or ():
            t = m.tracks.get(it["vehicle_id"])
            if t is not None:
                t.items.append(it)
        m.extra = dict(res.extra or {})
        m.gen = "ok"
        m.dirty = True
        self.rt.emit("plan.ready", job_id=res.job_id, fields={"kind": "generator"}, mid=mid, planner=res.stats.get("planner"),
                     t_ms=res.stats.get("t_ms"), apply_tick=int(tick),
                     stats={"n_items": res.stats.get("n_items"), "n_traj": res.stats.get("n_traj")})
        for w in res.extra.get("warnings") or []:
            if w == "SPEED_CLAMPED":
                self.rt.emit("mission.warning", mid=mid, warning=w, level=1)
        if self._is_formation(m):
            m.extra["sync_first"] = True           # 群组时钟在全员到达槽位起点后同时启动
        self._deconflict(m)

    @staticmethod
    def _is_formation(m: MissionRT) -> bool:
        return m.spec.get("generator") == "formation" and len(m.tracks) >= 2

    # ------------------------------------------------------------ 编队阶段（§6.4.3，FR-044，ext）
    def _set_fphase(self, m: MissionRT, phase: str) -> None:
        if m.fphase == phase or not self._is_formation(m):
            return
        old, m.fphase = m.fphase, phase
        m.dirty = True
        f = m.extra.get("formation") or {}
        self.rt.emit("formation.phase", mid=m.mid, fields={"from": old, "to": phase}, shape=str(f.get("shape", "")),
                     level=1)

    def _poll_assemble(self, m: MissionRT) -> None:
        """PLANNED：全员起飞后在原地等待（assemble）→ 提交集结作业（LIFTING）；
        LIFTING：竖直段全部完成（capt_sync）→ 同一步下发全员 CAPT 轨迹（ASSEMBLING）。"""
        if m.assemble_job is not None:
            return
        live = [t for t in m.tracks.values() if t.state not in ("DONE", "DROPPED")]
        now = self.rt.t_ns
        if m.fphase == "LIFTING":
            waiting = [t for t in live if t.state == "WAITING" and t.reason == "capt_sync"]
            lifting = [t for t in live if t.asm == "lift"]
            if not waiting or (lifting and not any(now - t.wait_since_ns >= SYNC_TIMEOUT_NS for t in waiting)):
                return
            cache = self.rt.tracker.cache
            for t in waiting:
                tr = (m.assemble or {}).get("capt", {}).get(t.vehicle_id)
                if tr is None:
                    t.asm = "done"
                    t.step = "idle"
                    self._track(m, t, "TRANSIT", "assemble_fallback")
                    continue
                e = cache.put_traj(tr)
                cache.pin(e)
                t.asm = "capt"
                self._track(m, t, "TRANSIT", "capt")
                self._submit(m, t, "follow_path", self._fp_args(tr, e), step="transit")
            for t in lifting:
                t.asm = "done"                             # 超时未升到位的成员各自分层转场
            self._set_fphase(m, "ASSEMBLING")
            return
        if m.fphase != "PLANNED":
            return
        waiting = [t for t in live if t.state == "WAITING" and t.reason == "assemble"]
        if not waiting:
            return
        if len(waiting) < len(live) and not any(now - t.wait_since_ns >= SYNC_TIMEOUT_NS for t in waiting):
            return
        rt = self.rt
        cons = m.spec.get("constraints") or {}
        f = m.extra.get("formation") or {}
        S = rt.S
        payload = {"op": "assemble", "members": [t.vehicle_id for t in waiting],
                   "pos": np.array([S.enu.pos[t.slot] for t in waiting], np.float64),
                   "slots": np.array([t.items[0]["start"] for t in waiting], np.float64),
                   "z_form_m": float(f.get("z_m", max(float(t.items[0]["start"][2]) for t in waiting))),
                   "v_mps": float(waiting[0].items[0].get("speed_mps") or rt.cruise(waiting[0].slot)),
                   "min_sep_m": float(cons.get("min_sep_m", 10.0)),
                   "layer_dz_m": float(cons.get("layer_dz_m", rt.transit.get("layer_dz_m", 4.0))),
                   "rank": [int(t.rank) for t in waiting], "zones": rt.zones,
                   "r_col_m": max(rt.r_col(t.slot) for t in waiting)}
        jid = f"assemble:{m.mid}:{m.revision}"
        req = PlanRequest(jid, "formation", rt.world_key, tuple(payload["members"]), payload, rt.limits_dict(),
                          PRIO_START, rt.tick, BUDGET_MS["formation"], f"assemble:{m.mid}")
        m.assemble_job = jid
        for t in live:
            if t not in waiting:
                t.asm = "done"                             # 超时未到的成员各自分层转场
        self._set_fphase(m, "LIFTING")
        rt.pool.submit(req, lambda res, tick, mid=m.mid, ids=tuple(payload["members"]): self._on_assemble(mid, res, tick,
                                                                                                          ids))

    def _on_assemble(self, mid: str, res: PlanResult, tick: int, ids: tuple = ()) -> None:
        m = self.missions.get(mid)
        if m is None or res.job_id != m.assemble_job:
            return
        m.assemble_job = None
        waiting = [m.tracks[v] for v in ids if v in m.tracks]
        if not res.ok or m.state not in ("RUNNING", "PAUSED"):
            if m.state in ("RUNNING", "PAUSED"):
                self.rt.emit("plan.degraded", job_id=res.job_id, reason=f"CAPT_FALLBACK:{res.detail}", level=2)
            for t in waiting:
                t.asm = "done"
                if t.state == "WAITING":
                    t.step = "idle"
                    self._track(m, t, "TRANSIT", "assemble_fallback")
            self._set_fphase(m, "ASSEMBLING")
            return
        order = [t.vehicle_id for t in waiting]
        assign = [int(a) for a in (res.extra.get("assign") or range(len(order)))]
        first = {v: m.tracks[v].items[0] for v in order}
        for i, v in enumerate(order):                                  # CAPT 分配：成员 i 取原属 order[assign[i]] 的槽位项
            m.tracks[v].items[0] = first[order[assign[i]]]
        lift = {tr["vehicle_id"]: tr for tr in res.trajectories if tr.get("phase") == "lift"}
        capt = {tr["vehicle_id"]: tr for tr in res.trajectories if tr.get("phase") == "capt"}
        m.assemble = {"extra": dict(res.extra), "capt": capt}
        cache = self.rt.tracker.cache
        for t in waiting:
            tr = lift.get(t.vehicle_id)
            if tr is None:                                             # 已在队形高度：直接等待 CAPT
                t.asm = "capt_wait"
                self._track(m, t, "WAITING", "capt_sync")
                t.wait_since_ns = self.rt.t_ns
                continue
            e = cache.put_traj(tr)
            t.asm = "lift"
            self._track(m, t, "TRANSIT", "lift")
            self._submit(m, t, "follow_path", self._fp_args(tr, e), step="transit")
        self.rt.emit("plan.ready", job_id=res.job_id, fields={"kind": "formation"}, mid=mid, planner="capt",
                     t_ms=res.stats.get("t_ms"), apply_tick=int(tick),
                     stats={"duration_s": res.extra.get("T_s"), "stretch_ratio": res.extra.get("stretch_ratio")})

    # ------------------------------------------------------------ 任务内 4D 冲突检查（FR-056，ext）
    def _deconflict(self, m: MissionRT) -> None:
        """多机任务（编队除外，编队由 CAPT 与群时钟保证间距）：对各机首个 B-spline 作业项做 4D 检查；
        结果生效前 Mission 的生成状态保持 pending（不能启动）。"""
        if len(m.tracks) < 2 or m.spec.get("generator") == "formation":
            return
        trs = []
        for vid, t in m.tracks.items():
            it = t.items[0] if t.items else None
            tr = m.trajs.get(it.get("traj_key")) if it is not None and it.get("primitive") != "orbit" else None
            if tr is None or tr.get("kind", "bspline") != "bspline":
                return
            trs.append({"vehicle_id": vid, "ctrl_pts": tr["ctrl_pts"], "ts_s": float(tr["ts_s"]),
                        "prio": -float(t.rank)})
        rt = self.rt
        cons = m.spec.get("constraints") or {}
        payload = {"trajs": trs, "clearance_m": float(cons.get("min_sep_m", 10.0)), "zones": rt.zones,
                   "r_col_m": max(rt.r_col(t.slot) for t in m.tracks.values())}
        jid = f"deconflict:{m.mid}:{m.revision}"
        req = PlanRequest(jid, "deconflict", rt.world_key, tuple(m.tracks), payload, rt.limits_dict(), PRIO_START, rt.tick,
                          BUDGET_MS["deconflict"], f"deconflict:{m.mid}")
        m.gen, m.deconf_job = "pending", jid
        rt.pool.submit(req, lambda res, tick, mid=m.mid: self._on_deconflict(mid, res, tick))

    def _on_deconflict(self, mid: str, res: PlanResult, tick: int) -> None:
        m = self.missions.get(mid)
        if m is None or res.job_id != m.deconf_job:
            return
        m.deconf_job = None
        m.gen = "ok"
        m.dirty = True
        if res.status not in ("ok", "degraded"):
            self.rt.emit("plan.failed", job_id=res.job_id, code=int(res.code or 125), detail=res.detail, level=1)
            return
        d = dict(res.extra or res.stats)
        m.deconf = d
        delays = d.get("delays_s") or {}
        layers = d.get("layers_m") or {}
        cache = self.rt.tracker.cache
        for vid, t in m.tracks.items():
            t.delay_s = float(delays.get(vid, 0.0))
            t.layer_m = float(layers.get(vid, 0.0))
            if t.layer_m and t.items:
                it = t.items[0]
                tr = dict(m.trajs[it["traj_key"]])
                dz = np.array([0.0, 0.0, t.layer_m])
                tr["ctrl_pts"] = np.asarray(tr["ctrl_pts"], np.float64) + dz
                tr["key"] = f"{tr['key']}:dz{t.layer_m:+.0f}"
                tr["start"] = (np.asarray(tr["start"], np.float64) + dz).tolist()
                tr["end"] = (np.asarray(tr["end"], np.float64) + dz).tolist()
                if tr.get("samples_1s") is not None:
                    S = np.array(tr["samples_1s"], np.float64)
                    S[:, 3] += t.layer_m
                    tr["samples_1s"] = S
                if tr.get("polyline") is not None:
                    tr["polyline"] = np.asarray(tr["polyline"], np.float64) + dz
                cache.pin(cache.put_traj(tr))
                m.trajs[tr["key"]] = tr
                t.items[0] = {**it, "traj_key": tr["key"], "start": tr["start"], "end": tr["end"]}
        m.extra["sync_first"] = any(t.delay_s > 0 or t.layer_m for t in m.tracks.values())
        self.rt.emit("deconflict.result", mid=mid, delays_s=delays, layers_m=layers, residual=d.get("residual") or [],
                     level=0)
        for vid in d.get("partial") or []:
            self.rt.emit("deconflict.partial", mid=mid, vehicle_id=vid, level=2)

    # ------------------------------------------------------------ 启动与能量预检
    def _try_start(self, m: MissionRT) -> dict:
        rt = self.rt
        per, ok = self.energy_precheck(m)
        m.energy = {"feasible": ok, "per_vehicle": per}
        if not ok:
            if m.precheck == "reject":
                m.start_requested = False
                deficit = [{"id": p["id"], "deficit_wh": p["deficit_wh"]} for p in per if not p["feasible"]]
                self.rt.emit("mission.state", mid=m.mid, fields={"from": "IDLE", "to": "IDLE"}, reason="ENERGY_INFEASIBLE",
                             incomplete=False, level=1)
                return {"code": 119, "detail": {"vehicles": deficit}}
            rt.emit("mission.energy_warning", mid=m.mid, per_vehicle=[{"id": p["id"], "need_wh": p["need_wh"],
                                                                       "soc_after_pct": p["soc_after_pct"]} for p in per],
                    level=2)
        m.t_start_ns = rt.t_ns
        self._set_state(m, "RUNNING", "start")
        rt.set_task_priority([t.slot for t in m.tracks.values() if t.slot >= 0], int(m.spec.get("priority", 0)))
        prio = sorted(m.tracks.values(), key=lambda t: (-int(m.spec.get("priority", 0)), t.rank))
        for r, t in enumerate(prio):
            t.rank = r
            self._advance(m, t)
        return {"code": 0}

    def energy_precheck(self, m: MissionRT) -> tuple[list[dict], bool]:
        rt = self.rt
        S = rt.S
        reserve = float((m.spec.get("constraints") or {}).get("energy_reserve", 0.20))
        per = []
        ok_all = True
        for vid, t in m.tracks.items():
            if t.slot < 0:
                continue
            s = t.slot
            home = S.enu.home[s].copy()
            p0 = S.enu.pos[s].copy()
            soc = rt.soc(s)
            parts = []
            tk = 0.0
            z_to = max(float(p0[2]), float(home[2]) + TAKEOFF_AGL_M) if not rt.airborne(s) else float(p0[2])
            up = EN.vertical_samples(p0, z_to, 1.5, tk)
            if len(up):
                parts.append(up)
                tk = float(up[-1, 0])
            pos = np.array([p0[0], p0[1], z_to])
            speed = rt.cruise(s)
            for it in t.items:
                st = np.asarray(it["start"], np.float64)
                v_it = float(it.get("speed_mps") or speed)
                tr_parts, tk = self._transit_samples(pos, st, v_it, tk)
                parts += tr_parts
                if it["primitive"] == "orbit":
                    o = it["geometry"]["orbit"]
                    turns = float(o.get("turns") or 0) or 1.0
                    R = float(o["radius_m"])
                    T = 2 * math.pi * R * turns / max(v_it, 0.1)
                    n = max(2, int(T) + 1)
                    th = np.linspace(0, 2 * math.pi * turns, n)
                    c = np.asarray(o["center_enu_m"], np.float64)
                    P = np.c_[c[0] + R * np.cos(th), c[1] + R * np.sin(th), np.full(n, c[2])]
                    V = np.c_[-v_it * np.sin(th), v_it * np.cos(th), np.zeros(n)]
                    parts.append(np.c_[tk + np.linspace(0, T, n), P, V])
                    tk += T
                    pos = P[-1]
                else:
                    tr = m.trajs.get(it.get("traj_key") or "")
                    if tr is not None:
                        smp = np.asarray(tr["samples_1s"], np.float64).copy()
                        smp[:, 0] += tk
                        parts.append(smp)
                        tk = float(smp[-1, 0])
                    pos = np.asarray(it["end"], np.float64)
            h_top = rt.hm_top(pos, home)
            rtl, _z = EN.rtl_samples(pos, home, h_top, rt.wind, rt.dtm_at(home), 5.0, tk)
            wh_work = sum(rt.path_wh(s, p) for p in parts if len(p) >= 2)
            wh_rtl = rt.path_wh(s, rtl) if len(rtl) >= 2 else 0.0
            need = wh_work + wh_rtl
            e_use = rt.e_use(s)
            after = soc - need / e_use
            feas = after >= reserve - 1e-9
            ok_all &= feas
            per.append({"id": vid, "energy_wh": round(need, 2), "need_wh": round(need, 2), "rtl_wh": round(wh_rtl, 2),
                        "soc_after_pct": round(after * 100.0, 1), "soc_after": round(after, 4), "feasible": bool(feas),
                        "deficit_wh": round(max(0.0, (reserve - after) * e_use), 2)})
        return per, ok_all

    def _transit_samples(self, a: np.ndarray, b: np.ndarray, v: float, tk: float) -> tuple[list[np.ndarray], float]:
        rt = self.rt
        out = []
        if float(np.linalg.norm(b - a)) < 1.0:
            return out, tk
        zc = rt.transit_z(a, b)
        legs = [(a, np.array([a[0], a[1], zc])), (np.array([a[0], a[1], zc]), np.array([b[0], b[1], zc])),
                (np.array([b[0], b[1], zc]), b)]
        for p, q in legs:
            d = q - p
            if float(np.linalg.norm(d)) < 1e-6:
                continue
            if abs(d[2]) > 1e-6 and float(np.hypot(d[0], d[1])) < 1e-6:
                smp = EN.vertical_samples(p, float(q[2]), 3.0 if d[2] > 0 else 1.5, tk)
            else:
                smp = EN.segment_samples(p, q, v, tk)
            if len(smp):
                out.append(smp)
                tk = float(smp[-1, 0])
        return out, tk

    # ------------------------------------------------------------ Track 推进
    def _submit(self, m: MissionRT, t: TrackRT, op: str, args: dict, *, step: str, keep: bool = False) -> dict:
        self._n += 1
        t.ncall += 1
        cid = f"m10:{m.mid}:{t.vehicle_id}:{t.ncall}"
        if not keep:
            t.cid = cid
            t.step = step
        self._by_cid[cid] = (m.mid, t.vehicle_id)
        self.stats["calls"] += 1
        adm = self.rt.submit_internal({"cid": cid, "op": op, "uav": t.vehicle_id, "args": args}, m.principal)
        if adm.get("status") == "rejected":
            self.stats["rejected"] += 1
            if not keep:
                self._results.append((cid, "rejected", int(adm.get("code") or 0), adm.get("detail")))
        return adm

    def _advance(self, m: MissionRT, t: TrackRT) -> None:
        """当前步骤完成后选择下一步（K01–K03、K08–K10）。"""
        if m.state not in ("RUNNING",) or t.state in ("DONE", "DROPPED", "SUSPENDED") or t.paused:
            return
        rt = self.rt
        if t.not_before_ns > rt.t_ns and t.cursor > 0:
            return                                     # dwell（作业项末端静止段）未结束
        s = t.slot
        if t.pending_items:
            it = t.pending_items.pop(0)
            self._run_item(m, t, it, resume=True)
            return
        if t.state == "PENDING":
            if not rt.airborne(s):
                self._track(m, t, "TRANSIT", "takeoff")
                self._submit(m, t, "takeoff", {"alt_m": TAKEOFF_AGL_M}, step="takeoff")
                return
            self._track(m, t, "TRANSIT", "start")
        if self._is_formation(m) and not t.asm and t.cursor == 0 and t.items:
            t.step = "idle"
            self._track(m, t, "WAITING", "assemble")
            t.wait_since_ns = rt.t_ns
            return
        if t.cursor >= len(t.items):
            self._finish_track(m, t)
            return
        it = t.items[t.cursor]
        start = np.asarray(it["start"], np.float64)
        pos = rt.S.enu.pos[s].copy()
        if t.step not in ("transit",) and float(np.linalg.norm(pos - start)) > max(1.0, 0.25 * float(it.get("speed_mps")
                                                                                                        or 5.0)):
            self._plan_transit(m, t, pos, start)
            return
        self._start_item(m, t, it)

    def _start_item(self, m: MissionRT, t: TrackRT, it: dict) -> None:
        """首个作业项在 4D 消解给出延迟时先同步（全员到达起点）再按各自延迟起步（FR-056）。"""
        if t.cursor == 0 and m.extra.get("sync_first") and not t.synced:
            t.step = "idle"
            self._track(m, t, "WAITING", "formation_sync" if self._is_formation(m) else "deconflict_sync")
            t.wait_since_ns = self.rt.t_ns
            return
        if t.not_before_ns > self.rt.t_ns:
            t.step = "idle"
            return
        self._run_item(m, t, it)

    def _plan_transit(self, m: MissionRT, t: TrackRT, a: np.ndarray, b: np.ndarray) -> None:
        rt = self.rt
        cons = m.spec.get("constraints") or {}
        n = len(m.tracks)
        payload = {"start": a, "goal": b, "planner": cons.get("transit_planner", rt.transit.get("planner", "safe_transit")),
                   "clearance_m": float(cons.get("clearance_m", rt.transit.get("margin_m", 5.0))),
                   "alt_max_m": cons.get("alt_max_m"), "prefer_low": bool(cons.get("prefer_low", False)),
                   "layer_dz_m": float(t.rank * float(cons.get("layer_dz_m", rt.transit.get("layer_dz_m", 4.0))))
                   if n > 1 else 0.0, "speed_mps": float(t.items[t.cursor].get("speed_mps") or rt.cruise(t.slot))
                   if t.cursor < len(t.items) else rt.cruise(t.slot),
                   "zones": rt.zones, "r_col_m": rt.r_col(t.slot)}
        jid = f"safe_transit:{m.mid}:{t.vehicle_id}:{t.ncall}:{t.cursor}"
        req = PlanRequest(jid, "safe_transit", rt.world_key, (t.vehicle_id,), payload, rt.limits_dict(), PRIO_START,
                          rt.tick, BUDGET_MS["safe_transit"], f"track:{m.mid}:{t.vehicle_id}")
        t.step, t.job = "plan_transit", jid
        rt.pool.submit(req, lambda res, tick, mid=m.mid, vid=t.vehicle_id: self._on_transit(mid, vid, res, tick))

    def _on_transit(self, mid: str, vid: str, res: PlanResult, tick: int) -> None:
        m = self.missions.get(mid)
        if m is None:
            return
        t = m.tracks[vid]
        if t.job != res.job_id:
            return
        t.job = None
        if not res.ok:
            self.rt.emit("plan.failed", job_id=res.job_id, code=int(res.code or 125), detail=res.detail,
                         remedy=res.remedy, level=2)
            self._track(m, t, "SUSPENDED", res.detail or "PLAN_FAILED")
            return
        tr = res.trajectories[0]
        e = self.rt.tracker.cache.put_traj(tr)
        self.rt.emit("plan.ready", job_id=res.job_id, fields={"kind": "safe_transit"}, mid=mid, vehicle_id=vid,
                     planner=res.stats.get("planner"), t_ms=res.stats.get("t_ms"), apply_tick=int(tick),
                     stats={k: res.stats.get(k) for k in ("len_m", "duration_s", "zmax_m", "stretch_ratio")})
        if m.state != "RUNNING" or t.paused:
            t.step = "idle"
            return
        self._track(m, t, "TRANSIT", "transit")
        self._submit(m, t, "follow_path", self._fp_args(tr, e), step="transit")

    def _fp_args(self, tr: dict, e: Any, it: dict | None = None) -> dict:
        if it is not None and it.get("group"):
            # 编队成员：共享锚点轨迹，航点与终点取本成员槽位（M08 据此判到达与进度）
            wp = np.asarray([it["start"], it["end"]], np.float64)
            return {"waypoints": wp.round(4).tolist(), "speed_mps": round(float(it.get("speed_mps") or 5.0), 4),
                    "bspline": {"order": 3, "ts_s": float(e.ts_s), "ctrl_pts": e.Q.tolist(), "traj_id": e.key}}
        wp = np.asarray(tr.get("polyline") if tr.get("polyline") is not None else BS.sample_adaptive(e.Q, e.ts_s,
                                                                                                        max_pts=1000)[:, :3],
                        np.float64)
        if len(wp) > 1000:
            wp = wp[np.unique(np.r_[np.linspace(0, len(wp) - 1, 1000).round().astype(np.int64)])]
        wp[-1] = e.end
        if float(np.linalg.norm(np.diff(wp, axis=0), axis=1).sum()) > 20_000.0:
            wp = np.stack([e.start, e.end])
        return {"waypoints": wp.round(4).tolist(), "speed_mps": round(float(tr.get("limits", {}).get("v_max_mps", 5.0)), 4),
                "bspline": {"order": 3, "ts_s": float(e.ts_s), "ctrl_pts": e.Q.tolist(), "traj_id": e.key}}

    def _run_item(self, m: MissionRT, t: TrackRT, it: dict, resume: bool = False) -> None:
        rt = self.rt
        self._track(m, t, "WORKING", "item")
        rt.set_item(t.slot, int(it.get("seq", t.cursor)))
        if not resume:
            self._item_actions(m, t, it, "start")
        if it["primitive"] == "orbit":
            o = it["geometry"]["orbit"]
            args = {"center": [float(x) for x in o["center_enu_m"]], "radius_m": float(o["radius_m"]),
                    "speed_mps": float(o.get("speed_mps") or it.get("speed_mps") or 5.0), "cw": bool(o.get("cw", True)),
                    "turns": float(o.get("turns") or 0), "yaw_behavior": str(o.get("yaw_behavior", "center"))}
            self._submit(m, t, "orbit", args, step="item")
            return
        key = it.get("traj_key")
        e = rt.tracker.cache.get(key)
        tr = m.trajs.get(key) if not resume else it.get("_traj")
        if e is None and tr is not None:
            e = rt.tracker.cache.put_traj(tr)
        if e is None:
            self._track(m, t, "SUSPENDED", "TRAJ_MISSING")
            return
        self._submit(m, t, "follow_path", self._fp_args(tr or {"limits": {"v_max_mps": it.get("speed_mps", 5.0)}}, e, it),
                     step="item")

    def _item_actions(self, m: MissionRT, t: TrackRT, it: dict, when: str) -> None:
        """作业项动作（FR-008；M10 §6.3.1 ActionSpec）：
        start：云台（经 M13 `apply_item_gimbal` 锁存）、`sensor` 开关（ext）、`mark`（at = start）；
        end：`camera.trigger` 逻辑计数（按生成时的 `est.photos`；覆盖戳记由 coverage stage 5 Hz 完成）、`dwell`（末端静止
        `duration_s` 后再推进）、`mark`（at = end）。`yaw` 已由生成器写入作业项的航向规格（跟踪器航向模式），不另行执行。"""
        rt = self.rt
        srt = rt.sensor_runtime()
        acts = list(it.get("actions") or [])
        if when == "start":
            g = it.get("gimbal")
            if srt is not None and t.slot >= 0:
                try:
                    if isinstance(g, dict) and g:
                        srt.apply_item_gimbal(t.slot, g)
                    elif t.cursor == 0:
                        srt.set_default_for(t.slot, str(m.spec.get("generator")), dict(m.spec.get("params") or {}))
                except Exception:
                    log.exception("gimbal action failed", extra={"kv": {"mid": m.mid, "vehicle": t.vehicle_id}})
        for a in acts:
            kind = str(a.get("kind"))
            at = str(a.get("at", "during"))
            args = dict(a.get("args") or {})
            if kind == "mark" and at == when:
                rt.emit("scenario.mark", event_id=f"{m.mid}:{t.vehicle_id}:{int(it.get('seq', t.cursor))}",
                        label=str(args.get("label", "")), mid=m.mid, vehicle_id=t.vehicle_id, level=1)
            elif kind == "sensor" and when == "start" and srt is not None and t.slot >= 0 and args.get("name"):
                try:
                    srt.set_active(t.slot, str(args["name"]), bool(args.get("on", True)))
                except Exception:
                    log.exception("sensor action failed")
            elif kind == "camera.trigger" and when == "end":
                t.photos += int((it.get("est") or {}).get("photos") or 0)
            elif kind == "dwell" and when == "end":
                t.not_before_ns = rt.t_ns + int(max(0.0, min(600.0, float(args.get("duration_s", 0.0)))) * 1e9)

    def _finish_track(self, m: MissionRT, t: TrackRT) -> None:
        if m.fphase == "CRUISE":
            self._set_fphase(m, "DISBANDED")           # 锚点到达终点：解散，各成员按 on_done 返航（按 rank 分层）
        on_done = str(m.spec.get("on_done", "rtl"))
        self.rt.set_item(t.slot, 0xFFFF)
        if on_done == "rtl":
            self._track(m, t, "RETURNING", "on_done")
            self._submit(m, t, "rtl", {}, step="done_action")
        elif on_done == "land":
            self._submit(m, t, "land", {}, step="done_action")
        else:
            self._submit(m, t, "hover", {}, step="done_action")

    # ------------------------------------------------------------ 调用结果
    def on_call_result(self, call: Any) -> None:
        """CommandEngine 终态回调（按 cid 前缀 `m10:` 过滤）；只入队，在 stage 内处理。被取代或抢占时同步记下跟踪器
        保存的轨迹时钟 τ（续飞点，K06），因为保存的状态随后会随调用终态释放。"""
        st = self.rt.tracker.suspended.get(call.cid)
        tau = None if st is None else float(st.get("tau", 0.0))
        self._results.append((call.cid, call.status, int(call.code or 0), {"tau_s": tau}))

    def _handle_result(self, cid: str, status: str, code: int, detail: Any) -> None:
        key = self._by_cid.pop(cid, None)
        if key is None:
            return
        m = self.missions.get(key[0])
        if m is None:
            return
        t = m.tracks.get(key[1])
        if t is None or t.cid != cid:
            return
        self.stats["results"] += 1
        step = t.step
        t.cid = None
        if status == "rejected":
            # INT-1（M16-to-M10 第 2 条）：准入拒绝后按 0.5·2^k s（≤ 30 s，仿真）退避再续飞，不再每个 stage 周期重发
            t.rej_n += 1
            t.retry_at_ns = self.rt.t_ns + int(min(30.0, 0.5 * 2.0 ** (t.rej_n - 1)) * 1e9)
        elif status in ("succeeded", "running", "accepted"):
            t.rej_n = 0
        if status == "succeeded":
            if step == "takeoff":
                t.step = "idle"
                self._advance(m, t)
            elif step == "transit":
                t.step = "idle"
                if t.asm == "lift":
                    t.asm = "capt_wait"
                    self._track(m, t, "WAITING", "capt_sync")
                    t.wait_since_ns = self.rt.t_ns
                    return
                if t.asm == "capt":
                    t.asm = "done"
                if t.cursor < len(t.items):
                    self._start_item(m, t, t.items[t.cursor])
                else:
                    self._advance(m, t)
            elif step == "item":
                if t.pending_items:
                    t.step = "idle"
                    self._advance(m, t)
                    return
                t.done_items += 1
                done_it = t.items[t.cursor] if t.cursor < len(t.items) else None
                t.cursor += 1
                t.step = "idle"
                self.rt.emit("mission.item_reached", mid=m.mid, vehicle_id=t.vehicle_id, seq=t.cursor - 1,
                             fields={"kind": str((done_it or {}).get("kind", "leg"))})
                if done_it is not None:
                    self._item_actions(m, t, done_it, "end")
                if self._barrier(m, t):
                    return
                self._advance(m, t)
            elif step == "done_action":
                self._release_lease(m, t)
                self._track(m, t, "DONE", "done")
            else:
                t.step = "idle"
            return
        # 失败、取消、拒绝
        fs = self.rt.fs_name(t.slot) if t.slot >= 0 else "UNKNOWN"
        if step == "abort":
            return
        if code in DROP_CODES or fs in ("ELAND", "FAILSAFE", "CRASHED") or (code == 204 and fs in FS_DROP):
            self._release_lease(m, t)
            self._track(m, t, "DROPPED", f"{status}:{code}:{fs}")
            return
        if step == "done_action" and status == "failed":
            self._release_lease(m, t)
            self._track(m, t, "DROPPED", f"on_done failed {code}")
            return
        if code == 204:          # 安全抢占为 HOLD（例如让行）：挂起，间距恢复后自动续飞
            t.yields.append(self.rt.t_ns)
        tau = (detail or {}).get("tau_s") if isinstance(detail, dict) else None
        self._suspend(m, t, step, f"{status}:{code}", tau)

    def _suspend(self, m: MissionRT, t: TrackRT, step: str, reason: str, tau: float | None = None) -> None:
        rt = self.rt
        res = None
        if step == "item" and t.cursor < len(t.items):
            res = {"cursor": t.cursor, "tau_s": float(tau) if tau is not None else rt.tracker_tau_for(t.slot)}
        elif step in ("transit", "plan_transit", "takeoff"):
            res = {"cursor": t.cursor, "tau_s": 0.0}
        t.resume = res
        t.step = "idle"
        if t.asm in ("lift", "capt"):
            t.asm = "done"                 # 集结段被打断：续飞时按分层转场入场，其余成员不再等待本机
        self._track(m, t, "SUSPENDED", reason)

    def _try_resume(self, m: MissionRT, t: TrackRT) -> None:
        """K07：租约回到本任务、机体可用且未处于安全动作时续飞。"""
        rt = self.rt
        s = t.slot
        if s < 0 or not m.spec.get("resume_on_lease_return", True):
            return
        if t.job is not None or t.cid is not None:
            return
        if t.retry_at_ns > rt.t_ns:
            return
        lease = rt.lease_of(s)
        if lease["owner"] not in ("NONE", "MISSION") or (lease["owner"] == "MISSION" and lease["holder"] != m.principal["principal_id"]):
            return
        fs = rt.fs_name(s)
        if fs not in ("FLYING", "HOLD", "READY", "DISARMED"):
            return
        now = rt.t_ns
        recent = [x for x in t.yields if now - x <= YIELD_WINDOW_NS]
        if len(recent) >= 3:
            if t.reason != "YIELD_OSCILLATION":
                t.reason = "YIELD_OSCILLATION"
                rt.emit("track.state", mid=m.mid, vehicle_id=t.vehicle_id, fields={"from": "SUSPENDED", "to": "SUSPENDED"},
                        item=t.cursor, reason="YIELD_OSCILLATION", level=2)
            return
        if fs in ("READY", "DISARMED"):
            if t.reason and "landed" in str(t.reason):
                return
            self._track(m, t, "PENDING", "resume")
            t.step = "idle"
            self._advance(m, t)
            return
        r = t.resume or {"cursor": t.cursor, "tau_s": 0.0}
        t.resume = None
        if r["cursor"] >= len(t.items):
            self._track(m, t, "TRANSIT", "resume")
            self._advance(m, t)
            return
        it = t.items[r["cursor"]]
        t.cursor = r["cursor"]
        if it["primitive"] == "orbit" or r["tau_s"] <= 0.5:
            self._track(m, t, "TRANSIT", "resume")
            t.step = "idle"
            self._advance(m, t)
            return
        e = rt.tracker.cache.get(it.get("traj_key"))
        if e is None:
            self._track(m, t, "TRANSIT", "resume")
            t.step = "idle"
            self._advance(m, t)
            return
        payload = {"start": rt.S.enu.pos[s].copy(), "ctrl_pts": e.Q, "ts_s": e.ts_s, "tau_s": float(r["tau_s"]),
                   "speed_mps": float(it.get("speed_mps") or rt.cruise(s)), "zones": rt.zones, "r_col_m": rt.r_col(s),
                   "yaw": e.yaw}
        jid = f"resume:{m.mid}:{t.vehicle_id}:{t.ncall}"
        req = PlanRequest(jid, "resume", rt.world_key, (t.vehicle_id,), payload, rt.limits_dict(), PRIO_START, rt.tick,
                          BUDGET_MS["resume"], f"track:{m.mid}:{t.vehicle_id}")
        t.job = jid
        t.step = "plan_resume"
        self._track(m, t, "TRANSIT", "resume")
        rt.pool.submit(req, lambda res, tick, mid=m.mid, vid=t.vehicle_id: self._on_resume_plan(mid, vid, res, tick))

    def _on_resume_plan(self, mid: str, vid: str, res: PlanResult, tick: int) -> None:
        m = self.missions.get(mid)
        if m is None:
            return
        t = m.tracks[vid]
        if t.job != res.job_id:
            return
        t.job = None
        if not res.ok or not res.trajectories:
            self._track(m, t, "SUSPENDED", res.detail or "PLAN_FAILED")
            return
        cache = self.rt.tracker.cache
        trs = list(res.trajectories)
        e0 = cache.put_traj(trs[0])
        if len(trs) > 1:
            e1 = cache.put_traj(trs[1])
            it = dict(t.items[t.cursor]) | {"traj_key": e1.key, "_traj": trs[1]}
            t.pending_items = [it]
        t.step = "idle"
        self._submit(m, t, "follow_path", self._fp_args(trs[0], e0), step="transit")

    def _barrier(self, m: MissionRT, t: TrackRT) -> bool:
        """barrier 同步（ext）：到达同步点（作业项边界）后等待全员，超时 30 s【仿真】标记 lagging。"""
        if m.spec.get("sync_policy") != "barrier" or t.cursor >= len(t.items):
            return False
        self._track(m, t, "WAITING", "barrier")
        t.wait_since_ns = self.rt.t_ns
        return True

    def _poll_barrier(self, m: MissionRT) -> None:
        waiting = [t for t in m.tracks.values() if t.state == "WAITING" and t.reason in SYNC_REASONS]
        if not waiting:
            return
        live = [t for t in m.tracks.values() if t.state not in ("DONE", "DROPPED")]
        now = self.rt.t_ns
        target = min(t.cursor for t in waiting)
        arrived = [t for t in live if t.state == "WAITING" and t.reason in SYNC_REASONS and t.cursor >= target]
        limit = SYNC_TIMEOUT_NS if (target == 0 and m.extra.get("sync_first")) else BARRIER_TIMEOUT_NS
        timeout = any(now - t.wait_since_ns >= limit for t in waiting)
        if len(arrived) == len(live) or timeout:
            for t in live:
                if t.state != "WAITING" and timeout and not t.lagging:
                    t.lagging = True
                    self.rt.emit("track.lagging", mid=m.mid, vehicle_id=t.vehicle_id, sync_id=f"{m.mid}:{target}",
                                 level=2)
            for t in arrived:
                if t.cursor == 0 and m.extra.get("sync_first") and not t.synced:
                    t.synced = True
                    t.not_before_ns = now + round(t.delay_s * 1e9)
                    if self._is_formation(m):
                        self._set_fphase(m, "CRUISE")
                self._track(m, t, "WORKING", "barrier_release")
                self._advance(m, t)

    # ------------------------------------------------------------ 状态与投影
    def _release_lease(self, m: MissionRT, t: TrackRT) -> None:
        if t.slot >= 0:
            self.rt.release_lease(t.slot, m.principal["principal_id"], t.vehicle_id)

    def _track(self, m: MissionRT, t: TrackRT, state: str, reason: str | None = None) -> None:
        if t.state == state:
            return
        old = t.state
        t.state = state
        t.reason = reason
        t.t_state_ns = self.rt.t_ns
        m.dirty = True
        if t.slot >= 0:
            self.rt.set_track_state(t.slot, TRACK_CODE[state])
            if state in ("DONE", "DROPPED"):
                self.rt.set_item(t.slot, 0xFFFF)
        if state == "DROPPED" and reason != "abort" and self._is_formation(m) and m.fphase in (
                "LIFTING", "ASSEMBLING", "CRUISE"):
            pol = str((m.spec.get("params") or {}).get("on_member_loss", "keep_slot"))
            self.rt.emit("formation.member_lost", mid=m.mid, vehicle_id=t.vehicle_id, policy=pol, level=2)
        self.rt.emit("track.state", mid=m.mid, vehicle_id=t.vehicle_id, fields={"from": old, "to": state},
                     item=t.cursor, reason=reason, level=0)

    def _set_state(self, m: MissionRT, state: str, reason: str) -> None:
        if m.state == state:
            return
        old = m.state
        m.state = state
        m.reason = reason
        m.dirty = True
        m.revision += 0
        self.rt.emit("mission.state", mid=m.mid, fields={"from": old, "to": state}, reason=reason,
                     incomplete=m.incomplete, level=2 if state == "ABORTED" else 1)

    def _check_done(self, m: MissionRT) -> None:
        if m.state not in ("RUNNING", "PAUSED"):
            return
        ts = list(m.tracks.values())
        if not ts or any(t.state not in ("DONE", "DROPPED") for t in ts):
            return
        self.rt.set_task_priority([t.slot for t in ts if t.slot >= 0], 0)
        if all(t.state == "DROPPED" for t in ts):
            self._set_state(m, "ABORTED", "all_tracks_dropped")
            self.rt.emit("mission.aborted", mid=m.mid, reason="all_tracks_dropped", level=2)
            return
        m.incomplete = any(t.state == "DROPPED" for t in ts)
        self._set_state(m, "DONE", "complete")

    def progress(self, m: MissionRT) -> float:
        ts = [t for t in m.tracks.values()]
        if not ts:
            return 0.0
        tot = 0.0
        for t in ts:
            if t.state == "DONE":
                tot += 1.0
                continue
            n = max(len(t.items), 1)
            frac = t.done_items / n
            if t.state == "WORKING" and t.cursor < len(t.items):
                frac += self.rt.item_progress(t.slot) / n
            if t.state == "RETURNING":
                frac = 1.0
            tot += min(frac, 1.0) * (0.95 if t.state != "DONE" else 1.0)
        return 100.0 * tot / len(ts)

    def eta_s(self, m: MissionRT) -> float | None:
        if m.state != "RUNNING":
            return None
        best = 0.0
        for t in m.tracks.values():
            rem = 0.0
            for it in t.items[t.cursor:]:
                rem += float((it.get("est") or {}).get("duration_s", 0.0))
            best = max(best, rem)
        return round(best, 1)

    def status(self, m: MissionRT) -> dict:
        tracks = [{"vehicle_id": t.vehicle_id, "state": t.state, "item": int(t.cursor) if t.items else None,
                   "total": len(t.items)} for t in list(m.tracks.values())[:64]]
        st = {"mid": m.mid, "state": m.state, "progress_pct": round(self.progress(m), 1), "revision": int(m.revision),
              "tracks": tracks, "t_ns": int(self.rt.t_ns), "eta_s": self.eta_s(m)}
        mets = self.rt.mission_metrics(m)
        if mets:
            st["metrics"] = mets
        st["plan"] = {"pending": m.gen == "pending" or any(t.job is not None for t in m.tracks.values()),
                      "last_ms": round(float(m.plan_ms), 2)}
        f = m.extra.get("formation") if m.extra else None
        if f:
            st["formation"] = {"phase": m.fphase, "shape": str(f.get("shape")),
                               "rms_m": float(mets.get("formation_err_rms_m", 0.0)) if mets else 0.0,
                               "heading_rad": 0.0}
        return st

    # ------------------------------------------------------------ stage（order 150、every 25、phase 8，10 Hz）
    def stage(self, S: Any, ctx: Any) -> None:
        while self._results:
            cid, status, code, detail = self._results.popleft()
            try:
                self._handle_result(cid, status, code, detail)
            except Exception:
                log.exception("mission result handling failed", extra={"kv": {"cid": cid}})
        for mid in list(self.order):
            m = self.missions.get(mid)
            if m is None:
                continue
            if m.state == "IDLE" and m.gen == "ok" and m.start_requested:
                r = self._try_start(m)
                if r.get("code"):
                    m.start_requested = False
                    m.auto_start_blocked = True          # 自动启动失败后不再重试（操作员可手动 start）
                    self.rt.on_start_failed(m, r)
            elif m.state == "IDLE" and not m.start_requested and m.gen == "ok" and not m.auto_start_blocked:
                self._auto_start(m)
            if m.state == "PAUSED":
                for t in m.tracks.values():
                    if t.paused and t.cid is not None:
                        self.rt.extend_deadline(t.cid, 120.0)      # 暂停期间不判截止（M08 对 paused 调用的同一规则）
            if m.state == "RUNNING":
                for t in m.tracks.values():
                    if t.state == "SUSPENDED":
                        self._try_resume(m, t)
                    elif t.state in ("TRANSIT", "WORKING") and t.cid is None and t.job is None and t.step == "idle" \
                            and not t.paused:
                        self._advance(m, t)
                self._poll_barrier(m)
                if self._is_formation(m):
                    self._poll_assemble(m)
            self._check_done(m)
        self.rt.publish_status(self.missions, self.order)

    def _auto_start(self, m: MissionRT) -> None:
        st = m.spec.get("start")
        if not st:
            return
        rt = self.rt
        if st.get("now"):
            m.start_requested = True
        elif st.get("on") == "ready":
            if rt.session_playing() and all(rt.vehicle_ready(t.slot) for t in m.tracks.values()):
                m.start_requested = True
        elif st.get("at_s") is not None:
            if rt.t_ns >= int(float(st["at_s"]) * 1e9) and all(rt.vehicle_ready(t.slot) for t in m.tracks.values()):
                m.start_requested = True
        elif st.get("after"):
            other = self.missions.get(str(st["after"]))
            if other is not None and other.state in ("DONE", "ABORTED"):
                m.start_requested = True
