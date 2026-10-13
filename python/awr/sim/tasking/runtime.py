"""TaskingRuntime：M20 在 sim-core 内的组合（M20 §6.1、§6.15；M20-FR-001 至 FR-084 的运行时部分）。

两个 stage（M08 按档换算 `every`）：
- `tasking_guard`（5 Hz，order 185）：禁入守卫与撤离（`guard.KeepoutGuard`）、接力的 G(t) 与间隙累计（`relay.RelayController.sense`）、
  调用结果的推进（`executor.Executor.pump`）；
- `tasking`（1 Hz，order 186）：作业结果生效、机巢充电与换电、任务状态机与分配、架次推进、预测性接替、接力派遣与交接、
  on_detect 触发、访问栅格、状态发布（墙钟 1 Hz 节流）。

外部只读：M18 运行时（禁入体、识别物状态）、M19 运行时（捕获状态、航迹、`set_requirement`、`set_cue`）、M13（云台指向与变焦）、
M09（电池 SOC、`set_soc`）、M07（风、照度档、预报）、M08（FleetState ENU 视图、租约、roster、命令引擎、机队实例配置）。
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import logging
import math
import sys
import time
from typing import Any

import msgpack
import numpy as np

from awr.tasking.keepout import circles, from_volumes
from awr.tasking.model import Keepout, ModelInfo, Snapshot, TaskingError, VehicleView, WorkItem, level_at_least

from .book import ItemRT, SortieRT, TaskRT
from .executor import Executor
from .guard import KeepoutGuard
from .jobs import JobRunner
from .models import model_info
from .nests import MAX_NESTS, NestRT, normalize, slot_offsets
from .relay import RelayController
from .visits import VisitGrid

__all__ = ["MAX_ACTIVE_TASKS", "TaskingRuntime"]

log = logging.getLogger("awr.sim.tasking")

MAX_ACTIVE_TASKS = 6
PREVIEW_TTL_S = 60.0
PUBLISH_WALL_S = 1.0
SIGMA_HOLD = {True: 0.01, False: 0.1}   # RTK 档、SLAM 档（AWR-04 §8.1）
TAU_ROUTE_S = 20.0
TAU_PRED_S = 5.0          # 守卫口径的运动外推（awr.sim.tasking.guard.TAU_PRED_S）
V_NOM_MIN = 0.5
ALLOC_MIN_GAP_S = 3.0
H_TRANSIT_AGL = 60.0
KO_SHAPE_NS = 1_000_000_000
# 固定航线类任务的分解用运动识别物的静态活动包络（`motion_hull`）；定点与巡线由用户给出几何，仍按当时位置的规划口径
STATIC_KO_TYPES = ("area_patrol", "perimeter_patrol", "area_guard", "area_scan_gsd")


def _slug_ok(s: str) -> bool:
    import re

    return bool(re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", s or ""))


class TaskingRuntime:
    def __init__(self) -> None:
        self.S: Any = None
        self.ctx: Any = None
        self.cmd: Any = None
        self.core: Any = None
        self.roster: Any = None
        self.T: Any = None
        self.world: Any = None
        self.events: Any = None
        self.bound = False
        self.nests: dict[str, NestRT] = {}
        self.tasks: dict[str, TaskRT] = {}
        self.sorties: dict[str, SortieRT] = {}
        self.lost: set[str] = set()                # 落在机巢外的机体（不入巢、不可分配；回到机巢附近着地后恢复）
        self.previews: dict[str, dict] = {}
        self.models: dict[str, ModelInfo] = {}
        self.revision = 0
        self.ext_plans: dict[str, dict] = {}
        self._tid_seq = itertools.count(1)
        self._nid_seq = itertools.count(1)
        self._pv_seq = itertools.count(1)
        self._sortie_seq = itertools.count(1)
        self.jobs = JobRunner(self)
        self.ex = Executor(self)
        self.relays = RelayController(self)
        self.guard = KeepoutGuard(self)
        self.alloc_job: str | None = None
        self.alloc_dirty = False
        self.alloc_urgent = False
        self.alloc_last_s = -math.inf
        self.results: list[tuple[str, str, int, Any]] = []
        self._pubs: dict[str, Any] = {}
        self._bus: Any = None
        self._wall_pub = 0.0
        self._ko_cache: dict[bool, tuple[int, tuple, list[Keepout]]] = {}   # 同一 tick 的结果
        self._ko_base: tuple[int, tuple, list[tuple]] | None = None         # 禁入体形状与相对偏移（M18 rev、RTK 档、1 s 失效）
        self._trt_cache: tuple[int, Any] | None = None
        self.sink: Any = None                 # 测试钩子：(key, payload dict)
        self.stats = {"stage_guard": 0, "stage_tasking": 0, "allocations": 0, "events": 0}
        self.event_log: list[tuple[str, dict]] = []
        self.reapply_after_restore = False    # checkpoint 恢复后的首个 tasking 周期：重新下发感知需求与线索（ckpt.decode_into）
        self._look_redo: set[str] = set()     # 因看洞点与其他任务重合而重新分解过的任务
        self.restored_t_ns: int | None = None

    # ------------------------------------------------------------ 绑定
    def bind(self, S: Any, ctx: Any) -> bool:
        if ctx is None:
            return False
        if self.ctx is not ctx or self.S is not S or not self.bound:
            self.S, self.ctx = S, ctx
            self.cmd = getattr(ctx, "calls", None)
            self.core = getattr(self.cmd, "fleet_ops", None)
            self.roster = getattr(self.cmd, "roster", None) or getattr(ctx, "roster", None)
            self.T = getattr(ctx, "profiles", None)
            self.world = getattr(ctx, "world", None)
            self.events = getattr(ctx, "events", None)
            if self.cmd is not None and hasattr(self.cmd, "subscribe_results") and not self.bound:
                self.cmd.subscribe_results("m20:", self._on_call)
            self.bound = True
        return True

    def _on_call(self, call: Any) -> None:
        self.results.append((str(call.cid), str(call.status), int(call.code or 0), dict(getattr(call, "effect", {}) or {})))

    # ------------------------------------------------------------ 时钟
    @property
    def t_ns(self) -> int:
        return int(getattr(self.ctx, "t_ns", 0)) if self.ctx is not None else 0

    @property
    def t(self) -> float:
        return self.t_ns * 1e-9

    @property
    def tick(self) -> int:
        return int(getattr(self.ctx, "tick", 0)) if self.ctx is not None else 0

    # ------------------------------------------------------------ 事件
    def emit(self, kind: str, level: int = 0, uav: str | None = None, **data: Any) -> None:
        self.stats["events"] += 1
        self.event_log.append((kind, dict(data)))
        if len(self.event_log) > 512:
            del self.event_log[:128]
        ev = self.events
        if ev is not None and hasattr(ev, "emit"):
            with contextlib.suppress(Exception):
                ev.emit(kind, t_sim_ns=self.t_ns, severity=int(level), uav=uav, fields=data)

    # ------------------------------------------------------------ 机体
    def vehicle_ids(self) -> list[str]:
        R = self.roster
        if R is None:
            return []
        out = []
        for s in sorted(getattr(R, "by_slot", {})):
            e = R.by_slot[s]
            if getattr(e, "kind", "uav") not in ("uav", None) and str(getattr(e, "kind", "uav")) != "uav":
                continue
            out.append(str(e.id))
        return sorted(out)

    def entry(self, vid: str) -> Any:
        R = self.roster
        return R.resolve(vid) if R is not None and hasattr(R, "resolve") else None

    def slot_of(self, vid: str) -> int:
        e = self.entry(vid)
        return -1 if e is None else int(e.slot)

    def nest_of(self, vid: str) -> str | None:
        vc = getattr(self.core, "vehicle_cfg", None) or {}
        n = (vc.get(vid) or {}).get("nest_id")
        return str(n) if n else None

    def model_id(self, vid: str) -> str | None:
        e = self.entry(vid)
        return None if e is None else str(getattr(e, "profile_id", "") or "")

    def model(self, mid: str | None) -> ModelInfo | None:
        if not mid:
            return None
        m = self.models.get(mid)
        if m is None and self.T is not None:
            try:
                m = self.models[mid] = model_info(self.T, mid)
            except Exception:
                log.exception("model_info failed", extra={"kv": {"model": mid}})
                return None
        return m

    def pos(self, vid: str) -> np.ndarray:
        s = self.slot_of(vid)
        if s < 0 or self.S is None:
            return np.zeros(3)
        return np.asarray(self.S.enu.pos[s], float).copy()

    def vel(self, vid: str) -> np.ndarray:
        s = self.slot_of(vid)
        if s < 0 or self.S is None:
            return np.zeros(3)
        try:
            return np.asarray(self.S.enu.vel[s], float).copy()
        except Exception:
            return np.zeros(3)

    def yaw(self, vid: str) -> float | None:
        """机体航向（ENU，弧度，x 轴为东、逆时针为正；由 WORLD←FLU 四元数求）；未知时 None。"""
        s = self.slot_of(vid)
        if s < 0 or self.S is None:
            return None
        try:
            x, y, z, w = (float(v) for v in np.asarray(self.S.enu.q_xyzw[s], float))
            return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
        except Exception:
            return None

    def soc(self, vid: str) -> float:
        s = self.slot_of(vid)
        b = getattr(self.S, "blocks", {}).get("battery") if self.S is not None else None
        if s < 0 or b is None or "soc" not in b:
            return 1.0
        return float(b["soc"][s])

    def energy_rtl_n(self, vid: str) -> int:
        s = self.slot_of(vid)
        b = getattr(self.S, "blocks", {}).get("battery") if self.S is not None else None
        if s < 0 or b is None or "energy_rtl_n" not in b:
            return 0
        return int(b["energy_rtl_n"][s])

    def fs(self, vid: str) -> str:
        s = self.slot_of(vid)
        if s < 0 or self.S is None:
            return "UNKNOWN"
        try:
            from awr.contracts.enums import FLIGHTSTATE_NAMES

            return FLIGHTSTATE_NAMES[int(self.S.blocks["safety"]["fs"][s])]
        except Exception:
            try:
                return "FLYING" if bool(self.S.in_air[s]) else "LANDED"
            except Exception:
                return "UNKNOWN"

    def fw_slot(self, vid: str) -> Any:
        """M21 固定翼与垂起机的运行时状态（`FwSlot`：fw_mode、mode、悬停计时）；多旋翼或 M21 未装配时 None。"""
        mod = sys.modules.get("awr.sim.fixedwing.plugin")
        fn = getattr(mod, "runtime", None) if mod is not None else None
        try:
            frt = fn() if callable(fn) else None
        except Exception:
            frt = None
        s = self.slot_of(vid)
        return None if frt is None or s < 0 else frt.slot_state(s)

    def fw_mode(self, vid: str) -> str | None:
        """FW、MC、TRANSITION_FW、TRANSITION_MC（固定翼恒 FW）；多旋翼 None。"""
        st = self.fw_slot(vid)
        if st is None:
            return None
        return "FW" if not st.p.vtol else str(st.fw_mode)

    def fw_r_min(self, vid: str) -> float | None:
        """当前风下的盘旋半径下限（M21 第 ⑥ 步准入同一口径：`r_loiter_min(w)`，w 为机体处风速）。"""
        st = self.fw_slot(vid)
        if st is None or self.S is None:
            return None
        try:
            w = np.asarray(self.S.enu.wind(np.array([self.slot_of(vid)])), float)[0]
            return float(st.p.aero.r_loiter_min(float(math.hypot(w[0], w[1]))))
        except Exception:
            return None

    def lease(self, vid: str) -> dict:
        s = self.slot_of(vid)
        L = getattr(self.ctx, "lease", None) if self.ctx is not None else None
        if s < 0 or L is None:
            return {"owner": "NONE", "holder": None}
        try:
            return L.lease_json(s)
        except Exception:
            return {"owner": "NONE", "holder": None}

    def release_lease(self, vid: str, holder: str) -> None:
        s = self.slot_of(vid)
        L = getattr(self.ctx, "lease", None)
        if s < 0 or L is None:
            return
        with contextlib.suppress(Exception):
            cur = L.lease_json(s)
            if cur.get("owner") == "MISSION" and cur.get("holder") == holder:
                L.release(s, holder, return_to="none", uav=vid, t_ns=self.t_ns, emit=getattr(self.events, "emit", None))

    def set_soc(self, vid: str, soc: float) -> None:
        s = self.slot_of(vid)
        svc = getattr(sys.modules.get("awr.sim.safety"), "SERVICE", None)
        bat = getattr(getattr(svc, "rt", None), "bat", None)      # M09 SafetyRuntime.bat（BatteryModel：set_soc、reset_flight）
        if s >= 0 and bat is not None:
            with contextlib.suppress(Exception):
                bat.set_soc(np.array([s]), float(soc))
                bat.reset_flight(np.array([s]))
                return
        b = getattr(self.S, "blocks", {}).get("battery") if self.S is not None else None
        if s >= 0 and b is not None and "soc" in b:  # M09 未装配（单测）：直接写
            b["soc"][s] = float(soc)

    # ------------------------------------------------------------ 世界
    def dsm(self, xy: Any) -> np.ndarray:
        A = np.atleast_2d(np.asarray(xy, float))[:, :2]
        if self.world is None:
            return np.zeros(len(A))
        return np.asarray(self.world.height_dsm(A), float)

    def los(self, A: Any, B: Any) -> np.ndarray | None:
        """M04 视线（N 对端点，分块 ≤ 64 对）；没有世界或世界不支持时 None。"""
        w = self.world
        if w is None or not hasattr(w, "los_batch"):
            return None
        A = np.ascontiguousarray(np.asarray(A, float).reshape(-1, 3))
        B = np.ascontiguousarray(np.asarray(B, float).reshape(-1, 3))
        step = int(getattr(w, "max_los_batch", 0) or 64)
        try:
            return np.concatenate([np.asarray(w.los_batch(A[i:i + step], B[i:i + step]), bool) for i in range(0, len(A), step)])
        except Exception:
            return None

    def dtm(self, xy: Any) -> np.ndarray:
        A = np.atleast_2d(np.asarray(xy, float))[:, :2]
        if self.world is None:
            return np.zeros(len(A))
        return np.asarray(self.world.ground_dtm(A), float)

    def wind_mps(self, pos: Any, agl_m: float = 10.0) -> float:
        env = getattr(self.ctx, "env", None) if self.ctx is not None else None
        if env is None or not hasattr(env, "query"):
            return 0.0
        try:
            from awr.environment.query import Fields

            p = np.asarray(pos, float).reshape(1, 3).copy()
            p[0, 2] = float(self.dtm(p[:, :2])[0]) + float(agl_m)
            o = env.query(p, self.t_ns, fields=Fields.WIND)
            w = np.asarray(o.wind_mean_mps[0], float)
            return float(math.hypot(w[0], w[1]))
        except Exception:
            return 0.0

    def light(self) -> tuple[str, float]:
        """(照度档, MOR m)；M07 未装配时 (day, 10 km)。"""
        env = getattr(self.ctx, "env", None) if self.ctx is not None else None
        fn = getattr(env, "sky_sample", None)
        if callable(fn):
            with contextlib.suppress(Exception):
                s = fn(self.t_ns)
                return str(s.band), float(s.mor_m)
        return "day", 10000.0

    def light_fc(self, lookahead_s: float = 900.0) -> tuple[str, float]:
        """(当前照度档, min(当前 MOR, lookahead 后的 MOR))：M07 关键帧的平滑过渡（`env/set{mode: smooth}`）在过渡开始时即知道终值，
        站位按过渡结束时的能见度选取（D2-FX-R2-tasking 第 2 次：第 1 轮霾时段 10 min 的能见度下降中按当时 MOR 连续派了三架接替，每个站位几分钟后
        又落在窗口外）。M07 未装配时同 `light()`。"""
        band, mor = self.light()
        env = getattr(self.ctx, "env", None) if self.ctx is not None else None
        fn = getattr(env, "sky_sample", None)
        if callable(fn) and lookahead_s > 0:
            with contextlib.suppress(Exception):
                mor = min(mor, float(fn(self.t_ns + int(lookahead_s * 1e9)).mor_m))
        return band, mor

    # ------------------------------------------------------------ 识别物与禁入体
    def targets_rt(self) -> Any:
        c = self._trt_cache
        if c is not None and c[0] == self.tick:
            return c[1]
        mod = sys.modules.get("awr.sim.targets.plugin")
        fn = getattr(mod, "runtime", None) if mod is not None else None
        try:
            v = fn(create=False) if callable(fn) else None
        except Exception:
            v = None
        self._trt_cache = (self.tick, v)
        return v

    def perception_rt(self) -> Any:
        mod = sys.modules.get("awr.sim.perception.plugin")
        fn = getattr(mod, "runtime", None) if mod is not None else None
        try:
            return fn(create=False) if callable(fn) else None
        except Exception:
            return None

    def sensor_rt(self) -> Any:
        mod = sys.modules.get("awr.sim.sensors.plugin")
        fn = getattr(mod, "runtime", None) if mod is not None else None
        try:
            return fn() if callable(fn) else None
        except Exception:
            return None

    def margin_m(self) -> float:
        rtk = any(n.cfg.get("rtk_base") for n in self.nests.values())
        return 20.0 + 3.0 * SIGMA_HOLD[bool(rtk)]

    def keepouts(self, *, planning: bool = True) -> list[Keepout]:
        """全部识别物的 2.5D 禁入圆（M20 §6.4）：规划口径另按 `v_nom·τ_route` 外扩运动识别物。

        两级缓存：禁入体的形状（M18 `keepout`，含 physical 模式随照度档变化的有效半径）在识别物增删改（M18 rev）、RTK 档变化或每
        1 s【仿真】时重算，并记下各体相对识别物的偏移；位置每次按 `state1` 平移（只对运动识别物取状态）。结果在同一 tick 内复用。"""
        trt = self.targets_rt()
        m = self.margin_m()
        rev = (int(getattr(trt, "rev", 0) or 0), id(trt), round(m, 3))
        t_ns = self.t_ns
        hit = self._ko_cache.get(planning)
        if hit is not None and hit[1] == rev and hit[0] == t_ns:
            return hit[2]
        base = self._ko_base
        if base is None or base[1] != rev or not 0 <= t_ns - base[0] < KO_SHAPE_NS:
            rows: list[tuple] = []
            if trt is not None:
                with contextlib.suppress(Exception):
                    trt.bind(self.ctx)
                for tid in sorted(getattr(trt, "ids", lambda: [])()):
                    try:
                        vols = trt.keepout(tid, margin_m=m)
                        st = trt.state1(tid)
                    except Exception:
                        continue
                    if st is None:
                        continue
                    moving = bool(getattr(st, "moving", False))
                    cfg = trt.config(tid)
                    v_cfg = float(((cfg.raw if cfg else {}).get("motion") or {}).get("speed_mps", 0.0) or 0.0)
                    rows.extend((k, tid, k.x - float(st.pos[0]), k.y - float(st.pos[1]), moving, v_cfg)
                                for k in from_volumes(vols, target=tid, moving=moving))
            base = self._ko_base = (t_ns, rev, rows)
        cur: dict[str, Any] = {}
        out: list[Keepout] = []
        for k, tid, dx, dy, moving, v_cfg in base[2]:
            x, y, r = k.x, k.y, k.r
            if moving and trt is not None:
                st = cur.get(tid)
                if st is None:
                    st = cur[tid] = trt.state1(tid)
                if st is not None:
                    x, y = float(st.pos[0]) + dx, float(st.pos[1]) + dy
                    if planning:
                        r += max(float(getattr(st, "speed_mps", 0.0)), v_cfg, V_NOM_MIN) * TAU_ROUTE_S
            out.append(Keepout(k.id, x, y, r, k.z_min, k.z_max, k.target, k.moving) if (x, y, r) != (k.x, k.y, k.r) else k)
        self._ko_cache[planning] = (t_ns, rev, out)
        return out

    def circles(self, *, planning: bool = True) -> np.ndarray:
        return circles(self.keepouts(planning=planning))

    def motion_hull(self, tid: str | None, k: Keepout) -> list[tuple[float, float, float]] | None:
        """运动识别物的静态活动包络（场景情报：随机游走区域或路径），按禁入圆 k（守卫口径的基础半径，含外扩 m）给出静态禁入圆：
        随机游走 → 区域外接圆心、半径 r + 区域半对角线 + v·τ_pred + 10 m；路径 → 沿路径每 ≤ r/2 一个、半径 r + v·τ_pred + 10 m；
        其他运动方式为 None。D2-FX-R2：巡查、巡边与值守的固定航线此前按识别物当时位置的禁入圆规划，识别物移动后航线被守卫判违规，
        S7 中 AWR-V1 对沿路往返的车辆反复撤离并被发现（D2-AC-39）。"""
        trt = self.targets_rt()
        cfg = trt.config(tid) if trt is not None and tid else None
        if cfg is None:
            return None
        mo = (cfg.raw or {}).get("motion") or {}
        mode = str(mo.get("mode") or "static")
        v = max(float(mo.get("speed_mps") or mo.get("speed_max_mps") or 0.0), V_NOM_MIN)
        st = trt.state1(tid)
        dx = dy = 0.0
        if st is not None:
            dx, dy = k.x - float(st.pos[0]), k.y - float(st.pos[1])
        ext = v * TAU_PRED_S + 10.0
        if mode == "random_walk" and mo.get("region"):
            R = np.asarray(mo["region"], float).reshape(-1, 2)
            lo, hi = R.min(axis=0), R.max(axis=0)
            c = 0.5 * (lo + hi)
            half = 0.5 * float(np.hypot(*(hi - lo)))
            return [(float(c[0] + dx), float(c[1] + dy), float(k.r + half + ext))]
        if mode == "path" and mo.get("path"):
            W = np.asarray([[float(q[0]), float(q[1])] for q in mo["path"]], float)
            if len(W) < 1:
                return None
            out = []
            step = max(k.r / 2.0, 10.0)
            pts = [W[0]]
            for a, b in itertools.pairwise(W):
                L = float(np.hypot(*(b - a)))
                n = max(1, math.ceil(L / step))
                pts.extend(a + (b - a) * (i / n) for i in range(1, n + 1))
            if mo.get("loop") == "loop" and len(W) > 2:
                a, b = W[-1], W[0]
                L = float(np.hypot(*(b - a)))
                n = max(1, math.ceil(L / step))
                pts.extend(a + (b - a) * (i / n) for i in range(1, n))
            out = [(float(q[0] + dx), float(q[1] + dy), float(k.r + ext)) for q in pts]
            return out
        return None

    def target_classes(self) -> dict[str, str]:
        """识别物 → 类别（场景情报；分解时看洞只给任务目标类别的洞）。"""
        trt = self.targets_rt()
        out: dict[str, str] = {}
        for tid in sorted(getattr(trt, "ids", lambda: [])()) if trt is not None else []:
            cfg = trt.config(tid)
            c = getattr(cfg, "cls", None) if cfg is not None else None
            if c:
                out[str(tid)] = str(c)
        return out

    def keepouts_static(self) -> list[Keepout]:
        """任务分解（巡查、巡边、值守的固定航线与看洞站位）用的禁入圆：运动识别物取 `motion_hull` 的静态包络，其余同规划口径。"""
        out: list[Keepout] = []
        plan = {k.id: k for k in self.keepouts(planning=True)}
        for k in self.keepouts(planning=False):
            hull = self.motion_hull(k.target, k) if k.moving else None
            if not hull:
                out.append(plan.get(k.id, k))
                continue
            for i, (x, y, r) in enumerate(hull):
                out.append(Keepout(f"{k.id}/h{i}", x, y, r, k.z_min, k.z_max, k.target, False))
        return out

    # ------------------------------------------------------------ 机巢与在巢机体
    def sync_nests(self) -> None:
        """机队实例（M08 `vehicle_cfg.nest_id`）与机巢在巢记录对齐；新入巢的机体登记所装电池。"""
        ids = set(self.vehicle_ids())
        for n in self.nests.values():
            for uav in list(n.air):
                if uav not in ids or self.nest_of(uav) != n.id:
                    n.detach(uav)
        for vid in sorted(self.lost):
            nid = self.nest_of(vid)
            n = self.nests.get(nid or "")
            p = self.pos(vid) if vid in ids else None
            if vid not in ids or (n is not None and p is not None and self.fs(vid) in ("LANDED", "DISARMED", "READY")
                                  and math.hypot(float(p[0]) - n.pos3[0], float(p[1]) - n.pos3[1]) <= 30.0):
                self.lost.discard(vid)              # 已删除，或已回到机巢附近着地（操作员处置）
        for vid in sorted(ids):
            nid = self.nest_of(vid)
            if vid in self.lost:
                if nid in self.nests:
                    self.nests[nid].detach(vid)
                continue
            if nid and nid in self.nests and vid not in self.nests[nid].air:
                m = self.model(self.model_id(vid))
                self.nests[nid].attach(vid, dict((m.pack if m else {}) or {}), self.soc(vid))
                if m is not None:
                    self.nests[nid].add_spares(m.pack)

    def inventory(self, nid: str) -> list[dict]:
        n = self.nests.get(nid)
        if n is None:
            return []
        by: dict[str, dict] = {}
        for uav, a in sorted(n.air.items()):
            mid = self.model_id(uav) or "?"
            d = by.setdefault(mid, {"model_id": mid, "count": 0, "in_nest": 0, "airborne": 0})
            d["count"] += 1
            if a.state in ("airborne", "standby"):
                d["airborne"] += 1
            else:
                d["in_nest"] += 1
        return [by[k] for k in sorted(by)]

    def nest_home(self, nid: str, model_id: str) -> dict:
        n = self.nests.get(nid)
        if n is None:
            return {"code": 305, "detail": {"nest_id": nid, "why": "NEST_NOT_FOUND"}}
        why = self.nest_compat(n, model_id)
        if why is not None:
            return {"code": 525, "detail": why}
        used = {self._home_k(n, u) for u in n.air}
        pending = sum(1 for v in self.vehicle_ids() if self.nest_of(v) == nid and v not in n.air)
        if len(n.air) + pending >= int(n.cfg["slots"]):
            return {"code": 525, "detail": {"why": "slots", "slots": int(n.cfg["slots"])}}
        k = next(i for i in range(64) if i not in used and i >= pending - 0 and i not in self._pending_slots(n))
        z = float(self.dsm(np.array([n.pos3[:2]]))[0]) if n.cfg["pos"][2] is None else n.pos3[2]
        return {"code": 0, "pos_enu_m": n.slot_pos(k, z), "yaw_rad": 0.0}

    def _pending_slots(self, n: NestRT) -> set[int]:
        out = set()
        for v in self.vehicle_ids():
            if self.nest_of(v) == n.id and v not in n.air:
                out.add(self._home_k(n, v))
        return out

    def _home_k(self, n: NestRT, uav: str) -> int:
        e = self.entry(uav)
        h = getattr(e, "home_enu_m", None) if e is not None else None
        if h is None:
            a = n.air.get(uav)
            return a.slot_k if a is not None else -1
        offs = slot_offsets(max(int(n.cfg["slots"]), 1))
        d = [math.hypot(h[0] - (n.pos3[0] + dx), h[1] - (n.pos3[1] + dy)) for dx, dy in offs]
        return int(np.argmin(d)) if d else -1

    def nest_compat(self, n: NestRT, model_id: str) -> dict | None:
        try:
            from awr.sim.catalog.loader import nest_compatible

            doc = self.T.get(model_id).doc
            r = nest_compatible(doc, {"kind": n.cfg["kind"], "chargers": n.cfg["chargers"]})
            if not r.get("ok"):
                return {"why": r.get("why"), **(r.get("detail") or {})}
        except KeyError:
            return {"why": "model", "model_id": model_id}
        except Exception:
            return None
        return None

    def add_nest(self, d: dict) -> tuple[str, list[str]]:
        if len(self.nests) >= MAX_NESTS:
            raise TaskingError(505, "limit", kind="nest", max=MAX_NESTS)
        nid = str(d.get("id") or "")
        if nid and (not _slug_ok(nid) or nid in self.nests):
            raise TaskingError(110, "param", field="id")
        if not nid:
            while True:
                nid = f"n-{next(self._nid_seq)}"
                if nid not in self.nests:
                    break
        inv = d.get("inventory") or []
        cfg = normalize({k: v for k, v in d.items() if k not in ("inventory", "vehicle_ids")}, nid=nid)
        xy = np.array([cfg["pos"][:2]])
        if self.world is not None:
            with contextlib.suppress(Exception):
                zones = self.world.zones
                if not bool(zones.border.contains_xy(xy)[0]):
                    raise TaskingError(110, "param", field="pos", why_detail="OUT_OF_BORDER")
        C = self.circles(planning=False)
        if len(C) and bool(((np.hypot(C[:, 0] - xy[0, 0], C[:, 1] - xy[0, 1]) < C[:, 2])).any()):
            raise TaskingError(583, "keepout", field="pos")
        z_g = float(self.dsm(xy)[0])
        n = NestRT.build(cfg, z_g)
        total = 0
        for k, it in enumerate(inv if isinstance(inv, list) else []):
            mid = str(it.get("model_id") or "")
            if self.T is None or not self.T.has(mid):
                raise TaskingError(520, "model", field=f"inventory[{k}].model_id", model_id=mid)
            why = self.nest_compat(n, mid)
            if why is not None:
                raise TaskingError(525, str(why.get("why") or "kind"), **{k2: v for k2, v in why.items() if k2 != "why"})
            total += int(it.get("count") or 0)
        if total > int(cfg["slots"]):
            raise TaskingError(525, "slots", slots=int(cfg["slots"]), add=total)
        self.nests[nid] = n
        vids: list[str] = []
        if inv and self.core is not None:
            for it in inv:
                for _ in range(int(it.get("count") or 0)):
                    code, det = self.core.admit_add({"model_id": str(it["model_id"]), "nest_id": nid})
                    if code:
                        for v in vids:
                            with contextlib.suppress(Exception):
                                self.core.remove(v, True)
                        self.nests.pop(nid, None)
                        raise TaskingError(int(code), "inventory", detail=det)
                    vids.append(str(det["id"]))
        self.sync_nests()
        return nid, vids

    def update_nest(self, nid: str, patch: dict) -> dict:
        n = self.nests.get(nid)
        if n is None:
            raise TaskingError(305, "not_found", nest_id=nid)
        if "inventory" in patch:
            raise TaskingError(110, "INVENTORY_READONLY", field="inventory")
        merged = {**n.cfg, **{k: v for k, v in patch.items() if k != "id"}}
        cfg = normalize(merged, nid=nid, prev=n.cfg)
        if cfg["kind"] != n.cfg["kind"] and n.air:
            for uav in n.air:
                why = self.nest_compat(NestRT(cfg, n.pos3), self.model_id(uav) or "")
                if why is not None:
                    raise TaskingError(525, str(why.get("why") or "kind"))
        if cfg["pos"][:2] != n.cfg["pos"][:2] and n.air:
            raise TaskingError(105, "state", why_detail="NEST_OCCUPIED")
        n.cfg = cfg
        n.pad.interval_s = float(cfg["launch_interval_s"])
        n.rebuild_channels()
        n.add_spares()
        return n.config(self.inventory(nid))

    def remove_nest(self, nid: str) -> None:
        n = self.nests.get(nid)
        if n is None:
            raise TaskingError(305, "not_found", nest_id=nid)
        if n.air or any(self.nest_of(v) == nid for v in self.vehicle_ids()):
            raise TaskingError(105, "state", why_detail="NEST_OCCUPIED")
        if any(T.active and nid in (T.common.get("allowed_nests") or []) for T in self.tasks.values()):
            raise TaskingError(105, "state", why_detail="NEST_REFERENCED")
        self.nests.pop(nid, None)

    # ------------------------------------------------------------ 快照（M20 §7.3 TaskingSnapshot）
    def vehicle_views(self) -> list[VehicleView]:
        out = []
        t = self.t
        for vid in self.vehicle_ids():
            nid = self.nest_of(vid)
            if nid is None or nid not in self.nests:
                continue
            mid = self.model_id(vid) or ""
            m = self.model(mid)
            if m is None:
                continue
            n = self.nests[nid]
            so = self.sorties.get(vid)
            a = n.air.get(vid)
            lease = self.lease(vid)
            if lease.get("owner") not in ("NONE", "MISSION", None) or (lease.get("owner") == "MISSION" and not str(lease.get("holder") or "").startswith("tasking:")):
                state = "unavailable"
                tr = math.inf
            elif so is not None and so.state == "STANDBY" and not so.released:
                state, tr = "standby", t
            elif so is not None:
                state, tr = "busy", math.inf
            elif a is None:
                state, tr = "unavailable", math.inf
            else:
                tr = n.t_ready(vid, t)
                state = "ready" if a.state == "ready" else ("charging" if math.isfinite(tr) else "unavailable")
                if state == "ready" and self.soc(vid) < 0.30:
                    state = "charging"
                if not n.wind_ok:
                    tr = max(tr, t + 60.0)
            airborne = so is not None and so.airborne
            soc = self.soc(vid)
            e_av = soc * m.e_use_wh if (airborne or (a is not None and a.state == "ready")) else 0.95 * m.e_use_wh
            holds = tuple(sorted(w for w in [so.wid] if so is not None and w)) if so is not None else ()
            p = self.pos(vid)
            out.append(VehicleView(vid, mid, nid, (float(p[0]), float(p[1]), float(p[2])), soc, e_av, float(tr if math.isfinite(tr) else 1e12),
                                   state, airborne, holds, tuple(m.station_sensors)))
        return out

    def snapshot(self, items: list[WorkItem] | None = None, *, static_keepouts: bool = False) -> Snapshot:
        self.revision += 1
        models = {}
        vs = self.vehicle_views()
        for v in vs:
            m = self.model(v.model_id)
            if m is not None:
                models[v.model_id] = m
        nests = {k: n.view() for k, n in sorted(self.nests.items())}
        if items is None:
            items = self.pending_items()
        wind = max((self.wind_mps(n.pos3) for n in self.nests.values()), default=0.0)
        wind_hi = None
        if any(m.frame != "multirotor" for m in models.values()):
            wind_hi = max((self.wind_mps(n.pos3, 120.0) for n in self.nests.values()), default=0.0)
        kos = self.keepouts_static() if static_keepouts else self.keepouts()
        return Snapshot(self.revision, self.t, vs, models, nests, items, kos, wind, H_TRANSIT_AGL, wind_hi_mps=wind_hi)

    def pending_items(self) -> list[WorkItem]:
        out = []
        for T in sorted(self.tasks.values(), key=lambda x: x.tid):
            if T.state != "RUNNING":
                continue
            out.extend(it.w for it in sorted(T.items.values(), key=lambda x: x.w.wid) if it.state == "PENDING")
        return out

    def snapshot_wire(self) -> dict:
        """REST SB-57 `awr.tasking.snapshot.v1`。"""
        s = self.snapshot()
        items = []
        for w in s.items:
            d = {"wid": w.wid, "tid": w.tid, "kind": w.kind, "capability": list(w.needs.get("caps") or []), "level_req": w.needs.get("level"),
                 "workload_s": round(float(w.workload_s), 1), "deadline_s": w.deadline_s, "priority": int(w.priority)}
            if w.pos is not None:
                d["pos"] = [round(float(x), 2) for x in w.pos]
            elif w.path is not None and len(w.path):
                d["pos"] = [round(float(x), 2) for x in w.path[0]]
            items.append(d)
        vs = [{"id": v.id, "model_id": v.model_id, "nest_id": v.nest_id, "pos": [round(float(x), 2) for x in v.pos],
               "soc": round(min(max(v.soc, 0.0), 1.0), 4), "e_avail_wh": round(max(v.e_avail_wh, 0.0), 1),
               "t_ready_s": round(max(v.t_ready_s - self.t, 0.0), 1) if v.t_ready_s < 1e11 else 0.0,
               "sensors": list(v.sensors), "capabilities": list((s.models.get(v.model_id) or ModelInfo(v.model_id)).capabilities)} for v in s.vehicles
              if v.state in ("ready", "standby", "charging")]
        nests = [n.config(self.inventory(n.id)) for _k, n in sorted(self.nests.items())]
        for n in nests:
            n.pop("vehicle_ids", None)
        kos = [{"id": k.id[:32], "shape": "cylinder", "center_enu_m": [round(k.x, 2), round(k.y, 2), 0.0], "r_m": round(max(k.r, 0.01), 2),
                "z_min_m": -1000.0, "z_max_m": 10000.0, "source": {"kind": "target", "id": str(k.target or "")}} for k in s.keepouts]
        self.last_snapshot = s
        return {"revision": int(s.revision), "t_sim_ns": int(self.t_ns), "work_items": items, "vehicles": vs,
                "nests": nests, "keepouts": kos, "zones_ref": None}

    # ------------------------------------------------------------ 任务生命周期
    def active_tasks(self) -> int:
        return sum(1 for T in self.tasks.values() if T.active and T.parent is None)

    def new_tid(self) -> str:
        while True:
            tid = f"t-{next(self._tid_seq)}"
            if tid not in self.tasks:
                return tid

    def create_task(self, spec: dict, *, parent: str | None = None, preview_id: str | None = None, auto_start: bool = False) -> TaskRT:
        from .queries import validate_spec

        spec = validate_spec(spec)
        if parent is None and self.active_tasks() >= MAX_ACTIVE_TASKS:
            raise TaskingError(505, "limit", kind="task", max=MAX_ACTIVE_TASKS)
        self._precheck_sync(spec)
        tid = self.new_tid()
        T = TaskRT(tid, spec, "PLANNING", 0, self.t_ns, parent=parent, auto_start=auto_start)
        self.tasks[tid] = T
        if parent and parent in self.tasks:
            self.tasks[parent].children.append(tid)
        self.emit("task.created", 1, tid=tid, type=T.type, parent=parent, **{"from": None, "to": "PLANNING"})
        pv = self.previews.get(preview_id or "")
        if pv is not None and pv.get("hash") == spec_hash(spec) and pv.get("decomp") is not None and self.t <= pv.get("t_valid", 0):
            self._on_decomposed(tid, _retag(pv["decomp"], "pv", tid))
        else:
            if preview_id:
                T.warnings.append("PREVIEW_RECOMPUTED")
            self._submit_decompose(T)
        return T

    def _precheck_sync(self, spec: dict) -> None:
        """创建时同步的廉价检查（M20-FR-012、FR-011）：定点与巡线航点在禁入圆内 583（列出序号）；接力的识别物不存在 305。"""
        p = spec.get("params") or {}
        C = self.circles(planning=False)
        if spec["type"] == "polyline_patrol" and len(C):
            W = np.asarray([w[:2] for w in p.get("waypoints") or []], float)
            bad = [int(i) for i in np.flatnonzero(((np.hypot(W[:, None, 0] - C[None, :, 0], W[:, None, 1] - C[None, :, 1]) - C[None, :, 2]) < 0).any(axis=1))]
            if bad:
                raise TaskingError(583, "keepout", waypoints=bad)
        if spec["type"] == "point_watch" and len(C):
            q = np.asarray(p.get("point")[:2], float)
            if bool(((np.hypot(C[:, 0] - q[0], C[:, 1] - q[1]) - C[:, 2]) < 0).any()):
                raise TaskingError(583, "keepout", point=[float(q[0]), float(q[1])])
        if spec["type"] == "relay_watch":
            trt = self.targets_rt()
            if trt is not None and trt.config(str(p.get("target"))) is None:
                raise TaskingError(305, "not_found", target=p.get("target"))

    def look_points(self, exclude: str | None = None) -> list[list[float]]:
        """其他任务的看洞点与接力站位（水平）：分解时看洞点与之保持间距（`decompose.LOOK_SEP_M`）。"""
        out: list[list[float]] = []
        for T in sorted(self.tasks.values(), key=lambda x: x.tid):
            if T.tid == exclude or not T.active:
                continue
            for st in (T.geometry or {}).get("stations") or []:
                p = st.get("pos")
                if p is not None and len(p) >= 2:
                    out.append([float(p[0]), float(p[1])])
            if T.relay is not None and T.relay.station is not None:
                out.append([float(T.relay.station[0]), float(T.relay.station[1])])
        return out

    def work_layers(self, exclude: str | None = None) -> list[list[float]]:
        """其他活动任务的航线层 [x0, y0, x1, y1, 高度中位数]（`decompose.path_layer`）：分解时本任务的作业高度与之错开。"""
        from awr.tasking.decompose import path_layer

        out: list[list[float]] = []
        for T in sorted(self.tasks.values(), key=lambda x: x.tid):
            if T.tid == exclude or not T.active:
                continue
            for it in T.items.values():
                P = it.w.path
                if P is not None and len(P) >= 2 and it.w.kind != "station":
                    out.append(list(path_layer(np.asarray(P, float))))
        return out

    def _submit_decompose(self, T: TaskRT) -> None:
        snap = self.snapshot(items=[], static_keepouts=T.type in STATIC_KO_TYPES)
        payload = {"spec": T.spec, "snapshot": snap.to_payload(), "tid": T.tid, "now": self.t, "target_classes": self.target_classes(),
                   "avoid_pts": self.look_points(exclude=T.tid), "avoid_layers": self.work_layers(exclude=T.tid)}
        T.plan_job = self.jobs.submit("tasking.scan", payload, lambda extra, tid=T.tid: self._on_decomposed(tid, extra), key=T.tid)

    def _set_state(self, T: TaskRT, to: str, code: int | None = None, why: str | None = None) -> None:
        if T.state == to:
            return
        frm = T.state
        T.state = to
        T.revision += 1
        if to in ("DONE", "CANCELED", "FAILED", "INFEASIBLE"):
            T.done_s = self.t
        lvl = 2 if to in ("FAILED", "INFEASIBLE") else 1
        self.emit("task.state", lvl, tid=T.tid, type=T.type, code=code, why=why, **{"from": frm, "to": to})

    def _on_decomposed(self, tid: str, extra: dict) -> None:
        T = self.tasks.get(tid)
        if T is None or T.state != "PLANNING":
            return
        if not extra.get("ok"):
            T.last_error = {"code": int(extra.get("code") or 580), "why": str(extra.get("why") or ""), "detail": extra.get("detail")}
            self._set_state(T, "INFEASIBLE", T.last_error["code"], T.last_error["why"])
            return
        looks = [st.get("pos") for st in (extra.get("geometry") or {}).get("stations") or [] if ".look" in str(st.get("id", ""))]
        others = self.look_points(exclude=T.tid)
        if T.tid not in self._look_redo:
            from awr.tasking.decompose import LOOK_SEP_M, layers_conflict, path_layer

            redo = False
            if looks and others:
                A = np.asarray([q[:2] for q in looks], float)
                B = np.asarray(others, float)
                redo = float(np.hypot(A[:, None, 0] - B[None, :, 0], A[:, None, 1] - B[None, :, 1]).min()) < LOOK_SEP_M
            if not redo:
                lay = self.work_layers(exclude=T.tid)
                mine = [path_layer(np.asarray(d["path"], float)) for d in extra.get("items") or []
                        if d.get("path") is not None and len(d["path"]) >= 2 and d.get("kind") != "station"]
                redo = bool(lay and mine and any(layers_conflict(a, tuple(b)) for a in mine for b in lay))
            if redo:
                # 与同时创建的另一任务取了同一看洞点或同一作业高度层（两次分解互相不知道对方）：带回避点与回避层重新分解一次
                self._look_redo.add(T.tid)
                self._submit_decompose(T)
                return
        T.geometry = dict(extra.get("geometry") or {})
        T.preview = dict(extra.get("preview") or {})
        T.warnings = sorted(set(T.warnings) | set(extra.get("warnings") or []))
        T.mode = extra.get("mode")
        T.scan_detail = extra.get("scan_detail")
        for d in extra.get("items") or []:
            w = WorkItem.from_payload(d)
            T.items[w.wid] = ItemRT(w)
        if T.type in ("area_patrol", "area_guard"):
            with contextlib.suppress(Exception):
                T.visits = VisitGrid(np.asarray(T.params.get("polygon"), float))
        rel = extra.get("relay")
        if rel is not None:
            self.relays.create(T, rel)
            if T.state != "PLANNING":
                return
        for spec in extra.get("children") or []:
            try:
                C = self.create_task(spec, parent=T.tid)
                C.auto_start = True
            except TaskingError as e:
                T.warnings.append(f"child_failed:{e.why}")
        self._set_state(T, "READY")
        if T.auto_start or (T.parent and self.tasks.get(T.parent) is not None and self.tasks[T.parent].state == "RUNNING"):
            self.start(tid)

    def start(self, tid: str) -> str:
        T = self.tasks.get(tid)
        if T is None:
            raise TaskingError(305, "not_found", tid=tid)
        if T.state == "PLANNING":
            T.auto_start = True
            return T.state
        if T.state not in ("READY",):
            raise TaskingError(105, "state", state=T.state)
        st = T.common.get("start_at_s")
        T.started_s = self.t if st is None else max(self.t, float(st))
        self._set_state(T, "RUNNING")
        for c in T.children:
            C = self.tasks.get(c)
            if C is not None and C.state == "READY":
                self.start(c)
            elif C is not None:
                C.auto_start = True
        if T.relay is not None:
            self.relays.start(T)
        self.alloc_dirty = True
        return T.state

    def pause(self, tid: str) -> str:
        T = self.tasks.get(tid)
        if T is None:
            raise TaskingError(305, "not_found", tid=tid)
        if T.state != "RUNNING":
            raise TaskingError(105, "state", state=T.state)
        self._set_state(T, "PAUSED")
        for so in list(self.sorties.values()):
            if so.tid == tid and so.airborne:
                self.ex.standby(so, hold=True)
        for c in T.children:
            with contextlib.suppress(TaskingError):
                self.pause(c)
        return T.state

    def resume(self, tid: str) -> str:
        T = self.tasks.get(tid)
        if T is None:
            raise TaskingError(305, "not_found", tid=tid)
        if T.state != "PAUSED":
            raise TaskingError(105, "state", state=T.state)
        self._set_state(T, "RUNNING")
        for so in list(self.sorties.values()):
            if so.tid == tid and so.state == "STANDBY" and so.wid and not so.released:
                self.ex.resume_work(so)
        for c in T.children:
            with contextlib.suppress(TaskingError):
                self.resume(c)
        self.alloc_dirty = True
        return T.state

    def cancel(self, tid: str, *, reason: str = "user") -> str:
        T = self.tasks.get(tid)
        if T is None:
            raise TaskingError(305, "not_found", tid=tid)
        if T.state in ("DONE", "CANCELED", "FAILED", "INFEASIBLE"):
            raise TaskingError(105, "state", state=T.state)
        for c in list(T.children):
            with contextlib.suppress(TaskingError):
                self.cancel(c, reason=reason)
        for it in T.items.values():
            if it.state not in ("DONE",):
                it.state = "CANCELED"
        for so in list(self.sorties.values()):
            if so.tid == tid:
                self.ex.release(so, "cancel")
        if T.relay is not None:
            self.relays.end(T, reason)
        self._set_state(T, "CANCELED", None, reason)
        return T.state

    def finish_if_done(self, T: TaskRT) -> None:
        if T.state != "RUNNING" or not T.items:
            return
        if T.relay is not None:
            return      # 接力监控是持续性任务：休眠或换站时工作项可能全部结束，任务本身只由取消或识别物删除结束（D2-FX-R2：(c) 入夜休眠即 DONE，天亮不再唤醒）
        if all(it.state in ("DONE", "CANCELED") for it in T.items.values()):
            for so in list(self.sorties.values()):
                if so.tid == T.tid and not so.released:
                    self.ex.release(so, "done")
            self._set_state(T, "DONE")

    # ------------------------------------------------------------ 分配
    def request_allocation(self, *, urgent: bool = False) -> None:
        """登记一次分配；常规请求与上一次分配至少相隔 3 s【仿真】（共享规划服务的负载），租约抢占与安全事件（urgent）不受此限，
        使工作项在 2 s【仿真】内重新分配（M20-FR-046、FR-047）。"""
        self.alloc_dirty = True
        if urgent:
            self.alloc_urgent = True

    def _maybe_allocate(self) -> None:
        if not self.alloc_dirty or self.alloc_job is not None:
            return
        if self.t - self.alloc_last_s < ALLOC_MIN_GAP_S and not self.alloc_urgent:
            return
        items = self.pending_items()
        if not items:
            self.alloc_dirty = False
            return
        snap = self.snapshot(items)
        if not any(v.state in ("ready", "standby", "charging") for v in snap.vehicles):
            return
        ext = [T for T in self.tasks.values() if T.state == "RUNNING" and str(T.common.get("allocator") or "greedy") == "external"]
        payload = {"snapshot": snap.to_payload(), "allocator": "greedy", "budget_ms": 200}
        if ext and not any(T.tid in self.ext_plans for T in ext):
            # 外部计划等待 30 s【仿真】，未到时按 external_fallback（缺省 greedy）
            t0 = min((T.started_s or self.t) for T in ext)
            if self.t - t0 < 30.0:
                return
            if all(str(T.common.get("external_fallback") or "greedy") == "none" for T in ext):
                return
        self.alloc_dirty = False
        self.alloc_urgent = False
        self.alloc_last_s = self.t
        rev = snap.revision
        self.alloc_job = self.jobs.submit("tasking.allocate", payload, lambda extra, rev=rev: self._on_plan(extra, rev), key="alloc")
        self.stats["allocations"] += 1

    def _on_plan(self, extra: dict, rev: int) -> None:
        self.alloc_job = None
        if not extra.get("ok"):
            self.alloc_dirty = True
            return
        self.apply_plan(extra["plan"])

    def apply_plan(self, plan: dict, *, external: bool = False) -> list[dict]:
        """计划生效：仍待分配的工作项交给计划中的机体（机体须仍可用）；不可行项记原因（每任务的 warnings）。"""
        applied = []
        by_task: dict[str, list[dict]] = {}
        for a in sorted(plan.get("assignments") or [], key=lambda x: (x["uav"], int(x.get("order", 0)), x["wid"])):
            tid = a["wid"].split(".")[0]
            T = self.tasks.get(tid)
            it = T.items.get(a["wid"]) if T is not None else None
            if T is None or it is None or it.state != "PENDING" or T.state != "RUNNING":
                continue
            so = self.sorties.get(a["uav"])
            if so is not None and not (so.state == "STANDBY" and not so.released):
                continue
            if any(x.get("uav") == a["uav"] for x in applied):
                continue   # 每机一次只执行一个工作项（其余在下一轮分配）
            if not self.ex.assign(a["uav"], T, it, a):
                continue
            applied.append({"wid": a["wid"], "uav": a["uav"]})
            by_task.setdefault(tid, []).append({"wid": a["wid"], "uav": a["uav"]})
        for tid, items in by_task.items():
            T = self.tasks[tid]
            T.assignments = [x for x in T.assignments if x["wid"] not in {i["wid"] for i in items}] + items
            T.revision += 1           # 计划几何的分配着色变化：客户端据 revision 重取 SB-54 include=plan（17 §17.7.4）
            self.emit("task.allocated", 0, tid=tid, items=items, allocator="external" if external else "greedy")
        for x in plan.get("infeasible") or []:
            tid = str(x.get("wid", "")).split(".")[0]
            T = self.tasks.get(tid)
            it = T.items.get(x.get("wid")) if T is not None else None
            if it is not None and it.state == "PENDING":
                it.reason = str(x.get("why"))
                w = f"unassigned:{x.get('why')}"
                if w not in T.warnings:
                    T.warnings.append(w)
        if any(it.state == "PENDING" for T in self.tasks.values() if T.state == "RUNNING" for it in T.items.values()):
            self.alloc_dirty = True
        return applied

    # ------------------------------------------------------------ stage
    def st_guard(self, S: Any, ctx: Any) -> None:
        if not self.bind(S, ctx):
            return
        self.stats["stage_guard"] += 1
        self.ex.pump()
        self.guard.step()
        self.relays.sense()

    def st_tasking(self, S: Any, ctx: Any) -> None:
        if not self.bind(S, ctx):
            return
        self.stats["stage_tasking"] += 1
        t = self.t
        if self.reapply_after_restore:
            self.reapply_after_restore = False
            self._after_restore()
        self.jobs.drain(self.tick)
        self.sync_nests()
        for n in sorted(self.nests.values(), key=lambda x: x.id):
            n.wind_ok = self.wind_mps(n.pos3) <= float(n.cfg["wind_max_mps"]) + 1e-9
            for kind, data in n.tick(t):
                if kind == "nest.swap_done":
                    self.set_soc(data["swap_uav"], float(data["soc"]))
                    self.emit("nest.swap_done", 0, uav=data["uav"], nest=data["nest"], pack_id=data["pack_id"], soc_pct=data["soc_pct"])
                elif kind == "_charged":
                    self.set_soc(data["uav"], float(data["soc"]))
            n.pad.drop_before(t)
        for T in sorted(self.tasks.values(), key=lambda x: x.tid):
            if T.state == "RUNNING" and T.started_s is not None and t >= T.started_s:
                dur = T.params.get("duration_s")
                if T.type in ("area_patrol", "perimeter_patrol", "polyline_patrol", "point_watch", "relay_watch") and dur is not None \
                        and t - T.started_s >= float(dur):
                    for it in T.items.values():
                        if it.state != "DONE":
                            it.state = "DONE"
                    for so in list(self.sorties.values()):
                        if so.tid == T.tid:
                            self.ex.release(so, "done")
                    if T.relay is not None:
                        self.relays.end(T, "done")
                    self._set_state(T, "DONE")
                    continue
                self.finish_if_done(T)
            if T.visits is not None and T.state == "RUNNING":
                T.visits.stamp(self, T, t)
        self.ex.step()
        self.relays.step()
        self._on_detect()
        self._maybe_allocate()
        self._expire_previews()
        self.publish()

    def _after_restore(self) -> None:
        """checkpoint 恢复（驻留恢复、崩溃重启）之后：M19 的所需等级与巡查线索、M13 的指向与变焦不在 checkpoint 中，按架次重新下发
        （接力在岗与接替机：`set_requirement` 与变焦；巡查类：`set_cue`；其余指向由各自的 1 Hz 推进刷新）。"""
        prt = self.perception_rt()
        for uav in sorted(self.sorties):
            so = self.sorties[uav]
            if not so.airborne:
                continue
            T, it = self.ex.item_of(so)
            if T is None or it is None:
                continue
            if it.w.kind == "relay_station":
                if so.state == "WORKING":
                    self.relays.on_station(so, it)
            elif prt is not None and T.type in ("area_patrol", "area_scan_gsd", "area_guard", "perimeter_patrol", "polyline_patrol"):
                with contextlib.suppress(Exception):
                    prt.set_cue(uav, True)
        self.request_allocation(urgent=True)

    # ------------------------------------------------------------ on_detect（M20-FR-017、FR-060）
    def _on_detect(self) -> None:
        prt = self.perception_rt()
        trt = self.targets_rt()
        if prt is None or trt is None:
            return
        mine = {so.uav for so in self.sorties.values() if so.airborne}
        for T in sorted(self.tasks.values(), key=lambda x: x.tid):
            if T.state != "RUNNING" or T.on_detect != "relay" or T.type == "relay_watch":
                continue
            lvl = str(T.params.get("level_req") or "R")
            want_cls = T.params.get("target_class") or ("person" if T.type in ("area_guard", "area_patrol") else None)
            for tgt in sorted(getattr(trt, "ids", lambda: [])()):
                if tgt in T.relayed or self.relays.active_for(tgt):
                    continue
                if want_cls:  # 区域值守只为所选类别的识别物转入接力（D2-INT：S7 曾为路过的车辆生成接力）
                    cfg = trt.config(tgt) if hasattr(trt, "config") else None
                    if cfg is not None and getattr(cfg, "cls", None) not in (None, want_cls):
                        continue
                try:
                    cd = prt.capture_detail(tgt) or {}
                except Exception:
                    cd = {}
                uav = cd.get("uav")
                if not uav or uav not in mine or not level_at_least(cd.get("best_level"), lvl):
                    continue
                if self.tasks_of_uav(uav) not in ({T.tid} | set(T.children)) and self.sorties[uav].tid not in ({T.tid} | set(T.children)):
                    continue
                T.relayed.add(tgt)
                spec = {"type": "relay_watch", "label": None, "params": {"target": tgt, "level_req": lvl}, "common": {"on_detect": "notify"}}
                try:
                    C = self.create_task(spec, parent=T.parent or T.tid, auto_start=True)
                    if C.relay is not None:
                        C.relay.first_uav = uav
                    elif C.state == "PLANNING":
                        self.relays.pending_first[C.tid] = uav
                except TaskingError as e:
                    T.warnings.append(f"relay_failed:{e.why}")

    def tasks_of_uav(self, uav: str) -> str | None:
        so = self.sorties.get(uav)
        return so.tid if so is not None else None

    # ------------------------------------------------------------ 预览
    def _expire_previews(self) -> None:
        wall = time.monotonic()
        for k in [k for k, v in self.previews.items() if wall > v.get("wall_until", 0)]:
            self.previews.pop(k, None)

    # ------------------------------------------------------------ 状态与发布
    def task_status(self, T: TaskRT) -> dict:
        items = [it.wire() for it in sorted(T.items.values(), key=lambda x: x.w.wid)]
        n_v = len({it.uav for it in T.items.values() if it.uav})
        prog = self.progress(T)
        warns = list(T.warnings)
        if T.relay is not None and T.relay.state == "DORMANT" and "dormant" not in warns:
            warns.append("dormant")
        return {"tid": T.tid, "type": T.type, "state": T.state, "revision": int(T.revision), "t_sim_ns": int(self.t_ns), "progress": prog,
                "n_vehicles": n_v, "makespan_s": None if T.preview.get("makespan_s") is None else round(float(T.preview["makespan_s"]), 1),
                "items": items, "children": list(T.children), "warnings": warns[:16]}

    def progress(self, T: TaskRT) -> dict:
        its = list(T.items.values())
        done = sum(1 for it in its if it.state == "DONE")
        pct = 100.0 * done / len(its) if its else (100.0 if T.state == "DONE" else 0.0)
        out: dict[str, Any] = {"pct": round(pct, 1)}
        if T.visits is not None:
            out["coverage_pct"] = round(T.visits.coverage_pct(), 1)
        if T.type == "polyline_patrol":
            out["laps_done"] = int(sum(it.laps for it in its))
        if T.type == "point_watch":
            out["dwell_s"] = round(sum(it.dwell_s for it in its), 1)
        if T.type == "area_scan_gsd" and T.scan_detail is not None and T.preview.get("coverage"):
            out["coverage_pct"] = round(float(T.preview["coverage"].get("visible_pct", 0.0)) * (done / max(len(its), 1)), 1)
        return out

    def task_obj(self, T: TaskRT, include_plan: bool = False) -> dict:
        st = self.task_status(T)
        d = {"tid": T.tid, "type": T.type, "label": T.spec.get("label"), "state": T.state, "revision": int(T.revision), "params": T.params,
             "common": T.common, "progress": st["progress"], "n_vehicles": st["n_vehicles"], "makespan_s": st["makespan_s"], "items": st["items"],
             "children": list(T.children), "parent": T.parent, "created_t_sim_ns": int(T.created_ns)}
        if include_plan:
            geo = T.geometry
            if T.relay is not None:
                geo = {**geo, "stations": self.relays.stations_geometry(T)}
            d["plan"] = {"revision": int(T.revision), "geometry": wire_geometry(geo, self.assign_map(T)),
                         "assignments": self.assignments_wire(T)}
        return d

    def assign_map(self, T: TaskRT) -> dict[str, str]:
        return {it.w.wid: it.uav for it in T.items.values() if it.uav}

    def assignments_wire(self, T: TaskRT) -> list[dict]:
        by: dict[str, dict] = {}
        for it in sorted(T.items.values(), key=lambda x: x.w.wid):
            if not it.uav:
                continue
            a = by.setdefault(it.uav, {"uav": it.uav, "nest": self.nest_of(it.uav), "items": []})
            a["items"].append({"wid": it.w.wid, "kind": kind_wire(it.w.kind), "ref": it.w.ref or it.w.wid})
            if it.eta_s is not None:
                a["eta_s"] = round(max(float(it.eta_s), 0.0), 1)
        return [by[k] for k in sorted(by)]

    def nest_status_all(self) -> dict:
        socs = {v: self.soc(v) for v in self.vehicle_ids()}
        return {n.id: n.status(self.t_ns, self.inventory(n.id), socs) for n in sorted(self.nests.values(), key=lambda x: x.id)}

    def publish(self, force: bool = False) -> None:
        wall = time.monotonic()
        batch = getattr(self.ctx, "batch", None) is not None
        if not force and not batch and wall - self._wall_pub < PUBLISH_WALL_S - 1e-3:
            return
        if batch and not force:
            return
        self._wall_pub = wall
        from awr.contracts import bus_keys

        t_ns = self.t_ns
        nests = self.nest_status_all()
        tasks = {T.tid: self.task_status(T) for T in sorted(self.tasks.values(), key=lambda x: x.tid)
                 if T.active or (T.done_s is not None and self.t - T.done_s < 60.0)}
        relay = {T.tid: self.relays.stats_wire(T) for T in sorted(self.tasks.values(), key=lambda x: x.tid) if T.relay is not None}
        for key, items in ((bus_keys.STATE_NESTS, nests), (bus_keys.STATE_TASKS, tasks), (bus_keys.STATE_RELAY, relay)):
            if not items and key in getattr(self, "_empty_sent", set()):
                continue
            msg = {"v": 1, "t_sim_ns": int(t_ns), "items": items}
            if self.sink is not None:
                self.sink(key, msg)
                continue
            bus = getattr(self.events, "bus", None)
            if bus is None:
                continue
            try:
                if self._bus is not bus:
                    self._bus, self._pubs = bus, {}
                pub = self._pubs.get(key)
                if pub is None:
                    pub = self._pubs[key] = bus.publisher(key)
                pub.put(msgpack.packb(msg, use_bin_type=True, default=_np_default))
            except Exception:
                self._pubs.pop(key, None)
            if not items:
                self._empty_sent = getattr(self, "_empty_sent", set()) | {key}
            else:
                self._empty_sent = getattr(self, "_empty_sent", set()) - {key}

    # ------------------------------------------------------------ 会话配置
    def reset(self) -> None:
        for so in list(self.sorties.values()):
            with contextlib.suppress(Exception):
                self.release_lease(so.uav, so.principal)
        self.nests.clear()
        self.tasks.clear()
        self.sorties.clear()
        self.previews.clear()
        self.relays.reset()
        self.jobs.due.clear()
        self.alloc_job = None
        self._look_redo.clear()


def _retag(extra: dict, old: str, new: str) -> dict:
    """预览的分解结果复用到新任务：工作项 wid/tid 与几何 id 的前缀由预览占位 tid 换成新 tid。"""
    out = dict(extra)
    items = []
    for d in extra.get("items") or []:
        d2 = dict(d)
        if str(d2.get("wid", "")).startswith(old + "."):
            d2["wid"] = new + str(d2["wid"])[len(old):]
        d2["tid"] = new
        if d2.get("group") == old:
            d2["group"] = new
        nd = dict(d2.get("needs") or {})
        if str(nd.get("chain", "")).startswith(old + "."):
            nd["chain"] = new + str(nd["chain"])[len(old):]
        d2["needs"] = nd
        items.append(d2)
    out["items"] = items
    geo = json.loads(json.dumps(extra.get("geometry") or {}, default=_np_default))
    for st in geo.get("stations") or []:
        if str(st.get("id", "")).startswith(old + "."):
            st["id"] = new + str(st["id"])[len(old):]
    out["geometry"] = geo
    return out


def _np_default(o: Any) -> Any:
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(type(o).__name__)


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


KIND_WIRE = {"strip_group": "strip_group", "subregion": "subregion", "segment": "segment", "station": "station", "relay_station": "relay_station",
             "perimeter_phase": "perimeter_phase", "guard_cycle": "guard_cycle", "fill": "fill", "look_in": "look_in", "dwell": "dwell"}


def kind_wire(k: str) -> str:
    return KIND_WIRE.get(k, "segment")


def _v3(p: Any) -> list[float]:
    a = [round(float(x), 2) for x in list(p)[:3]]
    while len(a) < 3:
        a.append(0.0)
    return a


def wire_geometry(g: dict, assign: dict[str, str] | None = None, max_segs: int = 512) -> dict:
    """计划几何 → 契约形状（`task.schema.json#/$defs/geometry`）；条带折线合计 ≤ 512 段（图层上限）。"""
    out: dict[str, Any] = {}
    if g.get("subregions"):
        out["subregions"] = [[[round(float(x), 2), round(float(y), 2)] for x, y, *_ in sr] for sr in g["subregions"]]
    if g.get("strips"):
        out["strips"] = [{"id": str(s["id"]), "p0": _v3(s["p0"]), "p1": _v3(s["p1"]), "uav": s.get("uav")} for s in g["strips"][:max_segs]]
    if g.get("fill_waypoints"):
        out["fill_waypoints"] = [{"pos": _v3(f["pos"]), "dwell_s": float(f.get("dwell_s", 0.0))} for f in g["fill_waypoints"][:256]]
    legs = []
    budget = max_segs
    for leg in g.get("legs") or []:
        P = np.asarray(leg.get("polyline"), float).reshape(-1, 3)
        if len(P) < 2 or budget <= 0:
            continue
        if len(P) - 1 > budget:
            idx = np.linspace(0, len(P) - 1, budget + 1).round().astype(int)
            P = P[np.unique(idx)]
        budget -= len(P) - 1
        legs.append({"uav": leg.get("uav") or "unassigned", "kind": str(leg.get("kind") or "patrol"), "polyline": [_v3(p) for p in P]})
    if legs:
        out["legs"] = legs
    if g.get("stations"):
        out["stations"] = [{"id": str(s["id"]), "pos": _v3(s["pos"]), "target": s.get("target"), "role": s.get("role", "primary"),
                            "uav": s.get("uav"), **({"loiter_radius_m": s["loiter_radius_m"]} if s.get("loiter_radius_m") is not None else {})}
                           for s in g["stations"][:32]]
    return out
