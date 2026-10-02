"""M10 运行时：sim-core 内的组合对象（M10 §6.1、§7.4.1）。

插件入口（`awr/sim/mission/__init__.py`）在 import 时调用 `install()`：登记状态块 `mission`、四个 stage（`mission` 027/2/0、
`mission_engine` 150/25/8、`coverage` 155/50/13、`director` 160/25/8）、三个运动提供者、细校验执行者、度量与查询。
运行时对象在第一次 stage 调用时绑定 sim-core 的 FleetState 与 StageCtx（`ctx.calls` 为 CommandEngine、`ctx.world` 为
M04 WorldQuery、`ctx.events` 为 EventPublisher、`ctx.lease` 为 LeaseManager、`ctx.profiles` 为 ProfileTable），并创建
PlanPoolClient（`AWR_PLAN_POOL`，缺省 process）。剧本经 `AWR_SCENARIO`（`scenarios/<id>.json`）在绑定后加载
（`AWR_SCENARIO_LOAD=0` 关闭），`sim/reset` 经 SimCore 的 reset hook 重新加载（`args.scenario_id` 可切换剧本）。

文件平面：`uav/{id}/path` blob 写到 `$AWR_RUN_DIR/paths/<vehicle>.<rev>.bin` 后发 `path.changed` 事件（M11 网关推送）；
任务状态以 `state/sim-core/mission`（msgpack 列表，变化时发布、墙钟节流 ≤ 2 Hz，另有 1 Hz 心跳）发布。
本模块不读墙钟（节流用注入的 `SimClock.wall_mono_ns()`）。
"""

from __future__ import annotations

import logging
import math
import os
import sys
from pathlib import Path
from typing import Any

import msgpack
import numpy as np

from awr.contracts import bus_keys
from awr.sim.planning import bspline as BS
from awr.sim.planning.jobs import BUDGET_MS, PRIO_INTERACTIVE, PRIO_START, PlanRequest, PlanResult
from awr.world.georef.frames import yaw_enu_from_ned

from . import energy as EN
from .engine import MissionEngine, MissionRT
from .tracker import Tracker

__all__ = ["M10Runtime"]

log = logging.getLogger("awr.sim.mission.runtime")

LEVEL = {0: 0, 1: 1, 2: 2, 3: 3}
STATUS_TRACKS_PER_STAGE = 256  # 每次 stage（10 Hz）状态汇总的轨道数上限（publish_status）
APPROACH_EXACT_M = 30.0  # 入场直线段的精确判定上限（水平长度，m）：更长的段只认粗校验（采样点数与段长成正比，ADR-070）


class M10Runtime:
    def __init__(self) -> None:
        self.S: Any = None
        self.ctx: Any = None
        self.cmd: Any = None               # CommandEngine
        self.core: Any = None              # SimCore（经 CommandEngine.fleet_ops；只用于 reset hook 与机群增删）
        self.world: Any = None
        self.events: Any = None
        self.lease: Any = None
        self.T: Any = None
        self.roster: Any = None
        self.pool: Any = None
        self.tracker = Tracker(self)
        self.missions = MissionEngine(self)
        self.director: Any = None
        self.scenario: Any = None          # scenario_loader.LoadedScenario
        self.coverage: Any = None
        self.zones: list[str] | None = None
        self.camera = {"hfov_deg": 60.0, "vfov_deg": 42.1}
        self.transit: dict = {"planner": "safe_transit", "margin_m": 5.0, "layer_dz_m": 4.0}
        self.wind = EN.WindProfile()
        self.world_key: tuple[str, str, str] = ("", "", "")
        self.run_dir: Path | None = None
        self.bound = False
        self.managed_cids: set[str] = set()
        self._job_cids: dict[str, str] = {}
        self._pub_mission: Any = None
        self._status_rot = 0
        self._wall_last_pub: dict[str, int] = {}
        self._path_files: dict[str, str] = {}
        self._cov_files: dict[str, str] = {}
        self._ep_cache: dict[int, EN.EnergyParams] = {}
        self._loaded_scenario_on_bind = False
        self.load_error: dict | None = None
        self.queries: Any = None
        self.pending_starts: list[tuple] = []
        self.stats = {"paths": 0, "status_puts": 0}

    # ------------------------------------------------------------ 绑定
    @property
    def t_ns(self) -> int:
        return int(self.S.t_ns) if self.S is not None else 0

    @property
    def tick(self) -> int:
        return int(self.S.tick) if self.S is not None else 0

    def bind(self, S: Any, ctx: Any) -> None:
        if self.bound:
            return
        from awr.sim.planning.pool import PlanPoolClient

        self.S, self.ctx = S, ctx
        self.cmd = ctx.calls
        self.world = ctx.world
        self.events = ctx.events
        self.lease = ctx.lease
        self.T = ctx.profiles
        self.core = getattr(self.cmd, "fleet_ops", None)
        self.roster = getattr(self.cmd, "roster", None)
        if self.world is not None:
            self.world_key = (self.world.world_id, self.world.content_version, self.world.coordinate_sha256)
            worlds_dir = os.environ.get("AWR_WORLDS_DIR") or str(Path(__file__).resolve().parents[4] / "worlds")
        else:
            worlds_dir = None
        rd = os.environ.get("AWR_RUN_DIR")
        self.run_dir = Path(rd) if rd else None
        inputlog = getattr(self.cmd, "inputlog", None)
        cpu = os.environ.get("AWR_PLAN_POOL_CPU")
        self.pool = PlanPoolClient(self.world_key if self.world is not None else None, worlds_dir=worlds_dir,
                                   world=self.world, cpu=int(cpu) if cpu else None, inputlog=inputlog)
        self.tracker.bind(S)
        if self.cmd is not None:
            self.cmd.subscribe_results("m10:", self.missions.on_call_result)
            self.cmd.subscribe_results("", self._on_any_result)
        if self.core is not None and hasattr(self.core, "register_reset_hook"):
            self.core.register_reset_hook(self.on_reset)
        self.bound = True
        if self.world is not None:
            self.pool.warm(self.tick, on_done=self._on_warm)
        from .coverage import CoverageTracker
        from .director import Director

        if self.coverage is None:
            self.coverage = CoverageTracker(self)
        if self.director is None:
            self.director = Director(self)
        for prov, call, slots, args, apply_tick in self.pending_starts:
            prov.start(call, slots, args, apply_tick)
        self.pending_starts.clear()
        self._autoload()

    def _on_warm(self, res: PlanResult, tick: int) -> None:
        self.emit("plan.warm", world_id=self.world_key[0], t_ms=res.stats.get("t_ms"), ok=res.ok, level=1)

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()

    # ------------------------------------------------------------ 剧本
    def _autoload(self) -> None:
        if os.environ.get("AWR_SCENARIO_LOAD", "1") == "0":
            return
        sid = getattr(getattr(self.core, "cfg", None), "scenario", None) or os.environ.get("AWR_SCENARIO")
        if not sid:
            return
        from .scenario_loader import scenario_path

        if scenario_path(sid) is None:
            log.info("scenario file not found; skeleton spawn kept", extra={"kv": {"scenario": sid}})
            return
        self.load_scenario(sid, os.environ.get("AWR_SCENARIO_PROFILE") or None)

    def load_scenario(self, sid: str, profile: str | None = None, doc: dict | None = None) -> dict:
        from .scenario_loader import ScenarioError, apply_scenario, load_scenario

        try:
            sc = load_scenario(sid, profile=profile, world=self.world, profiles=self.T, doc=doc)
        except ScenarioError as e:
            self.load_error = {"code": 121, "rule": e.rule, "detail": e.detail}
            log.warning("scenario invalid", extra={"kv": {"scenario": sid, "rule": e.rule, "detail": e.detail}})
            self.emit("scenario.invalid", scenario_id=sid, rule=e.rule, detail=str(e.detail)[:300], level=2)
            return self.load_error
        self.load_error = None
        self.scenario = sc
        self.zones = sc.zones_active
        self.transit = dict(sc.doc.get("transit") or self.transit)
        self.wind = EN.WindProfile.from_env_patch(sc.doc.get("env"))
        if self.cmd is not None and hasattr(self.cmd, "active_zone_ids"):
            self.cmd.active_zone_ids = list(sc.zones_active) if sc.zones_active is not None else None   # M08 细校验透传
        svc = self.safety_service()
        if svc is not None:                                    # M09 剧本配置（gcs_loss_policy、active_zones）
            try:
                svc.configure(gcs_loss_policy=sc.doc.get("gcs_loss_policy"), active_zones=sc.zones_active)
            except Exception as e:
                self.load_error = {"code": 121, "rule": "SAFETY_CONFIGURE", "detail": str(e)[:200]}
                self.emit("scenario.invalid", scenario_id=sc.scenario_id, rule="SAFETY_CONFIGURE", detail=str(e)[:300],
                          level=2)
                return self.load_error
        apply_scenario(self, sc)
        self._apply_scenario_env(sc)
        self.emit("scenario.loaded", scenario_id=sc.scenario_id, sha256=sc.sha256, level=1)
        return {"code": 0, "scenario_id": sc.scenario_id, "sha256": sc.sha256}

    def _apply_scenario_env(self, sc: Any) -> None:
        """剧本顶层 `env`（preset + patch，profile 已合并）作为环境初值：以 step 帧下发给 M07（M07 §6.6.1 K01/K06「剧本初值」；
        INT-1：此前只做 V-SC-08 校验与本模块能量风廓线，环境服务一直停在缺省 3 m/s、来向 270°，S1 的 6 m/s、135° 未生效）。"""
        env = sc.doc.get("env") or {}
        if not isinstance(env, dict) or not env:
            return
        principal = {"principal_id": f"scenario:{sc.scenario_id}", "role": "scenario", "entry": "scenario", "seat": False,
                     "source": "SCENARIO"}
        cmds = []
        if isinstance(env.get("preset"), str):
            cmds.append(("env/preset", {"name": env["preset"], "duration_s": 0}))
        if isinstance(env.get("patch"), dict) and env["patch"]:
            cmds.append(("env/set", {"patch": env["patch"], "mode": "step", "duration_s": 0}))
        for k, (op, args) in enumerate(cmds):
            try:
                r = self.submit_internal({"cid": f"scn:{sc.scenario_id}:env:{k}", "op": op, "args": args}, principal)
            except Exception as e:  # 环境插件缺失或拒绝：只告警，剧本照常运行
                r = {"status": "rejected", "code": 211, "detail": type(e).__name__}
            if (r or {}).get("status") == "rejected":
                log.warning("scenario env not applied", extra={"kv": {"op": op, "code": r.get("code"), "detail": r.get("detail")}})

    @staticmethod
    def sensor_runtime() -> Any:
        """M13 SensorRuntime（组合根已导入 awr.sim.sensors 时）；云台与传感器动作经其 sim-core 内部 API 锁存。"""
        mod = sys.modules.get("awr.sim.sensors.plugin")
        fn = getattr(mod, "runtime", None) if mod is not None else None
        try:
            return fn() if callable(fn) else None
        except Exception:
            return None

    @staticmethod
    def safety_service() -> Any:
        """M09 SafetyService（组合根已导入 awr.sim.safety 时）；M10 不直接依赖 M09 的导入。"""
        mod = sys.modules.get("awr.sim.safety")
        return getattr(mod, "SERVICE", None) if mod is not None else None

    def max_z(self) -> float | None:
        """围栏最大高度（ENU z，M09 GeofenceRT.max_z）；M09 未装配或围栏无效时 None。"""
        svc = self.safety_service()
        geo = getattr(getattr(svc, "rt", None), "geo", None)
        if geo is None or not getattr(geo, "valid", False):
            return None
        z = getattr(geo, "max_z", None)
        return float(z) if z is not None and np.isfinite(z) else None

    def set_task_priority(self, slots: list[int], prio: int) -> None:
        """FleetGuard 让行优先级 K2（Mission.priority，0–9；M09 `fg.set_task_priority`）。"""
        svc = self.safety_service()
        fg = getattr(getattr(svc, "rt", None), "fg", None)
        if fg is None or not slots:
            return
        try:
            fg.set_task_priority(np.asarray(slots, np.int64), np.full(len(slots), int(prio), np.int64))
        except Exception:
            log.exception("set_task_priority failed")

    def fault_inject(self, args: dict, principal: dict, cid: str) -> dict:
        """剧本 `fault.inject`：优先调 M09 登记的查询 `safety/fault`（同步、确定），否则经 CommandEngine `fault/inject`。"""
        from awr.sim.fleet.stages import registry as R

        q = R.registry().queries.get("safety/fault")
        if q is not None:
            msg = {"v": 1, "cid": cid, "op": "safety/fault", "principal": principal,
                   "args": {"op": "inject", "uav": args.get("vehicle_id") or args.get("uav"), "kind": args.get("kind"),
                            "params": args.get("params"), "at_s": args.get("at_s"),
                            "duration_s": args.get("duration_s")}}
            try:
                r = q.fn(msg, self.ctx)
            except Exception as e:
                return {"code": 109, "detail": type(e).__name__}
            if isinstance(r, (bytes, bytearray)):
                r = msgpack.unpackb(r, raw=False)
            return r if isinstance(r, dict) else {"code": 0}
        return self.submit_internal({"cid": cid, "op": "fault/inject", "uav": args.get("vehicle_id"), "args": args},
                                    principal)

    def on_reset(self, args: dict) -> None:
        """`sim/reset`（SimCore reset hook）：任务回到初始（M09）、导演重新计时；scenario_id 给出时切换剧本。"""
        self.missions.reset()
        self.tracker.reset()
        if self.pool is not None:
            self.pool.close()
            from awr.sim.planning.pool import PlanPoolClient

            self.pool = PlanPoolClient(self.world_key if self.world is not None else None,
                                       worlds_dir=self.pool.worlds_dir, world=self.world, cpu=self.pool.cpu,
                                       inputlog=self.pool.inputlog)
        if self.coverage is not None:
            self.coverage.reset()
        sid = (args or {}).get("scenario_id")
        prof = (args or {}).get("profile")
        if sid or self.scenario is not None:
            self.load_scenario(str(sid or self.scenario.scenario_id), prof if sid else (prof or self.scenario.profile))

    # ------------------------------------------------------------ 机体与世界查询
    def slot_of(self, vid: str) -> int:
        e = self.roster.resolve(vid) if self.roster is not None else None
        return -1 if e is None else int(e.slot)

    def v_limit(self, s: int) -> float:
        from awr.sim.fleet import kernels_l1 as K

        return float(self.T.LT[int(self.S.limits_id[s]), K.L_VXY])

    def cruise(self, s: int) -> float:
        from awr.sim.fleet import kernels_l1 as K

        return float(self.T.LT[int(self.S.limits_id[s]), K.L_CRUISE])

    def yawrate_max(self, s: int) -> float:
        """自动模式偏航角速度上限（rad/s）：限速配置的 yawrate 与 MPC_YAWRAUTO_MAX 的较小者（M08 姿态环同一钳制）。"""
        from awr.sim.fleet import kernels_l1 as K

        return float(min(self.T.LT[int(self.S.limits_id[s]), K.L_YAWRATE], K.YAWRAUTO))

    def a_max(self, s: int) -> float:
        from awr.sim.fleet import kernels_l1 as K

        return float(self.T.LT[int(self.S.limits_id[s]), K.L_ACC])

    def r_col(self, s: int) -> float:
        try:
            return float(self.T.collision_r[int(self.S.profile_id[s])])
        except Exception:
            return 0.49

    def speed_for(self, s: int, speed: float | None) -> float:
        lim = self.v_limit(s)
        return min(float(speed), lim) if speed else min(self.cruise(s), lim)

    def limits_dict(self, s: int | None = None) -> dict:
        from awr.sim.fleet import params_px4 as P

        v = self.v_limit(s) if s is not None and self.S is not None else 12.0
        a = self.a_max(s) if s is not None and self.S is not None else 3.0
        return {"v_max_mps": v, "a_tan_mps2": 2.0, "a_lat_mps2": 3.0, "vz_up_mps": float(P.MPC_Z_V_AUTO_UP),
                "vz_dn_mps": float(P.MPC_Z_V_AUTO_DN), "a_max_mps2": a}

    def psi_enu(self, S: Any, s: int) -> float:
        return float(yaw_enu_from_ned(float(S.yaw_sp[s])))

    def fs_name(self, s: int) -> str:
        from awr.contracts.enums import FLIGHTSTATE_NAMES

        try:
            return FLIGHTSTATE_NAMES[int(self.S.blocks["safety"]["fs"][s])]
        except Exception:
            return "UNKNOWN"

    def airborne(self, s: int) -> bool:
        return s >= 0 and bool(self.S.in_air[s]) and not bool(self.S.landed[s])

    def vehicle_ready(self, s: int) -> bool:
        if s < 0:
            return False
        try:
            return int(self.S.lifecycle[s]) == 4
        except Exception:
            return True

    def session_playing(self) -> bool:
        clk = getattr(self.cmd, "clock", None)
        try:
            from awr.contracts.enums import TimeState

            return int(clk.state) in (int(TimeState.PLAYING), int(TimeState.STEPPING)) or bool(getattr(clk, "advancing",
                                                                                                        False))
        except Exception:
            return True

    def soc(self, s: int) -> float:
        b = self.S.blocks.get("battery")
        if b is None or "soc" not in b:
            return 1.0
        return float(b["soc"][s])

    def _energy_params(self, s: int) -> EN.EnergyParams:
        pid = int(self.S.profile_id[s])
        ep = self._ep_cache.get(pid)
        if ep is None:
            ep = EN.EnergyParams.from_profile(self.T.get(self.T.ids[pid]))
            self._ep_cache[pid] = ep
        return ep

    def e_use(self, s: int) -> float:
        return self._energy_params(s).e_use_wh

    def path_wh(self, s: int, samples: np.ndarray) -> float:
        from awr.sim.fleet.stages import registry as R

        em = R.energy_model()
        if em is not None:
            try:
                return float(em.path_wh(self.T.ids[int(self.S.profile_id[s])], samples, None))
            except Exception:
                log.exception("EnergyModel.path_wh failed; fallback model used")
        gz = self.world.ground_dtm(np.asarray(samples)[:, 1:3]) if self.world is not None else None
        return EN.fallback_path_wh(self._energy_params(s), samples, self.wind, gz)

    def rtl_via(self, p: np.ndarray, home: np.ndarray, s: int) -> tuple[float, float] | None:
        """M09 返航路线的绕行点（ADR-054，EnergyModel 可选方法 `rtl_route`）；未提供或直飞时 None。"""
        from awr.sim.fleet.stages import registry as R

        fn = getattr(R.energy_model(), "rtl_route", None)
        if fn is None:
            return None
        try:
            r = fn(np.asarray(p, np.float64), np.asarray(home, np.float64), int(s))
        except Exception:
            log.exception("EnergyModel.rtl_route failed; direct return assumed")
            return None
        return None if r is None else r[0]

    def hm_top(self, a: np.ndarray, b: np.ndarray) -> float:
        if self.world is None:
            return -math.inf
        try:
            return float(self.world.heightmap_top_along(np.asarray(a, np.float64)[:2], np.asarray(b, np.float64)[:2],
                                                        exact=True))
        except Exception:
            return -math.inf

    def dtm_at(self, p: np.ndarray) -> float:
        if self.world is None:
            return float(p[2])
        return float(self.world.ground_dtm(np.asarray(p, np.float64)[None, :2])[0])

    def transit_z(self, a: np.ndarray, b: np.ndarray) -> float:
        if self.world is None:
            return max(float(a[2]), float(b[2]))
        try:
            pr = self.world.safe_transit_profile(a, b, margin_m=float(self.transit.get("margin_m", 5.0)))
            return float(pr.z_cruise_m)
        except Exception:
            return max(float(a[2]), float(b[2]))

    def coarse_proven(self, p: np.ndarray, goal: np.ndarray, s: int) -> bool:
        """直线段可证无障碍：M04 粗校验 `all_proven`；粗校验只给出 MAYBE（无违规、无延后的分区边）且水平长度
        ≤ APPROACH_EXACT_M 时，按段上逐 0.5 m 采样点的柱体最大值（半径 = 缓冲 1 m + 碰撞半径）精确判定（FR-013 入圆段，
        ADR-070）。粗校验的金字塔容差为 100 m，从低空起爬的短段（例如 ladder 起飞后 10 m AGL 爬到 60–105 m 的入圆段）
        几乎总被附近建筑判为 MAYBE，此前因此全部改走 plan-pool 的 safe_transit。"""
        if self.world is None:
            return True
        try:
            P = np.stack([np.asarray(p, np.float64), np.asarray(goal, np.float64)])
            r = self.world.path_coarse_check(P, buffer_m=1.0, goal_radius_m=self.r_col(s), active_zone_ids=self.zones)
            if r.ok and r.all_proven:
                return True
            if not r.ok or bool(np.any(r.zone_deferred)) or int(np.max(r.verdict)) > 1:
                return False
            d = P[1, :2] - P[0, :2]
            L = float(np.hypot(d[0], d[1]))
            if L > APPROACH_EXACT_M:
                return False
            n = max(2, math.ceil(L / 0.5) + 1)
            xy = P[0, :2] + np.linspace(0.0, 1.0, n)[:, None] * d
            top = float(np.max(self.world.column_max_within(xy, 1.0 + self.r_col(s))))
            return top <= float(min(P[0, 2], P[1, 2])) - 1.0
        except Exception:
            return False

    # ------------------------------------------------------------ 状态块投影
    def set_item(self, s: int, seq: int) -> None:
        if s >= 0 and self.tracker.B is not None:
            self.tracker.B["mission_item"][s] = min(int(seq), 0xFFFF)

    def set_track_state(self, s: int, code: int) -> None:
        if s >= 0 and self.tracker.B is not None:
            self.tracker.B["track_state"][s] = int(code)

    def tracker_tau_for(self, s: int) -> float:
        st = None
        m = self.tracker.meta.get(int(s))
        if m is not None and m.cid is not None:
            st = self.tracker.suspended.get(m.cid)
        if st is not None:
            return float(st.get("tau", 0.0))
        for stv in self.tracker.suspended.values():
            if stv.get("entry") is not None and stv["entry"].vehicle_id and self.slot_of(stv["entry"].vehicle_id) == s:
                return float(stv.get("tau", 0.0))
        if self.tracker.B is not None and int(self.tracker.B["kind"][s]) == 1:
            return float(self.tracker.B["tau"][s])
        return 0.0

    def item_progress(self, s: int) -> float:
        B = self.tracker.B
        if B is None or s < 0 or int(B["kind"][s]) != 1:
            return 0.0
        T = float(B["nseg"][s]) * float(B["ts"][s])
        return 0.0 if T <= 0 else min(1.0, float(B["tau"][s]) / T)

    def item_progress_many(self, slots: list[int]) -> list[float]:
        """`item_progress` 的按机向量化版本（同一公式与运算次序）。"""
        B = self.tracker.B
        if B is None or not slots:
            return [0.0] * len(slots)
        sl = np.asarray(slots, np.int64)
        T = B["nseg"][sl].astype(np.float64) * B["ts"][sl].astype(np.float64)
        ok = (B["kind"][sl] == 1) & (T > 0)
        v = np.where(ok, np.minimum(1.0, B["tau"][sl].astype(np.float64) / np.where(T > 0, T, 1.0)), 0.0)
        return v.tolist()

    def lease_of(self, s: int) -> dict:
        try:
            return self.lease.lease_json(s)
        except Exception:
            return {"owner": "NONE", "holder": None}

    def release_lease(self, s: int, holder: str, vid: str) -> None:
        try:
            L = self.lease.lease_json(s)
            if L["owner"] == "MISSION" and L["holder"] == holder:
                self.lease.release(s, holder, return_to="none", uav=vid, t_ns=self.t_ns, emit=self.events.emit)
        except Exception:
            log.exception("lease release failed")

    # ------------------------------------------------------------ 调用与规划
    def submit_internal(self, cmd: dict, principal: dict) -> dict:
        if self.cmd is None:
            return {"status": "rejected", "code": 213}
        return self.cmd.submit_internal(cmd, principal)

    def emit(self, kind: str, *, level: int = 0, uav: str | None = None, cid: str | None = None,
             fields: dict | None = None, **data: Any) -> None:
        if self.events is None:
            return
        try:
            self.events.emit(kind, t_sim_ns=self.t_ns, severity=LEVEL.get(level, 0), uav=uav, cid=cid, fields=fields,
                             **data)
        except TypeError:
            self.events.emit(kind, t_sim_ns=self.t_ns, severity=LEVEL.get(level, 0), uav=uav, cid=cid,
                             **(dict(fields or {}) | data))

    def pool_cancel(self, job_id: str) -> None:
        return None

    def cancel_jobs_for(self, cid: str) -> None:
        self.managed_cids.discard(cid)
        for jid, c in list(self._job_cids.items()):
            if c == cid:
                self._job_cids.pop(jid, None)

    def extend_deadline(self, cid: str, duration_s: float) -> None:
        """调用截止时间不短于 1.5·T + 10 s（M08 对原生 PATH 的同一规则；§14 反馈：请 M08 提供 extend_deadline）。"""
        eng = self.cmd
        until = int(self.t_ns + (1.5 * float(duration_s) + 10.0) * 1e9)
        try:
            if hasattr(eng, "extend_deadline"):          # M08 公开接口（INT-1）
                eng.extend_deadline(cid, until)
                return
            ent = eng.idem.get(cid)
            if ent is None:
                return
            for c in ent.calls:
                if c.row >= 0 and not c.final:
                    eng.table.deadline[c.row] = max(int(eng.table.deadline[c.row]), until)
        except Exception:
            pass

    def plan_for_call(self, call: Any, s: int, kind: str, payload: dict, polyline: np.ndarray, then: dict | None = None
                      ) -> None:
        """运动提供者的规划作业：调用停在 accepted（fine_pending），结果在步边界生效后装入轨迹并 fine_result。"""
        cid = call.cid
        prio = PRIO_INTERACTIVE if call.source == "OPERATOR" else PRIO_START
        vid = self.S.ids[s] if self.S is not None else ""
        pl = {"zones": self.zones, "r_col_m": self.r_col(s), "clearance_m": float(self.transit.get("margin_m", 5.0)),
              "planner": str(self.transit.get("planner", "safe_transit"))} | dict(payload)
        pl.setdefault("speed_mps", self.speed_for(s, None))
        m = self.tracker.slot_meta(s)
        m.cid = cid
        self.managed_cids.add(cid)
        if self.cmd is not None:
            self.cmd.schedule_fine_check(cid, np.asarray(polyline, np.float64))
        if self.pool is None or self.pool.state == "down":
            if self.cmd is not None:
                self.cmd.fine_result(cid, False, 213)
            return
        jid = f"{kind}:{cid}:{self.tick}"
        req = PlanRequest(jid, kind, self.world_key, (str(vid),), pl, self.limits_dict(s), prio, self.tick,
                          BUDGET_MS.get(kind, 500), f"slot:{s}")
        m.job_id = jid
        self._job_cids[jid] = cid
        self.pool.submit(req, lambda res, tick, s=s, cid=cid, then=then: self._on_call_plan(s, cid, res, tick, then))

    def _on_call_plan(self, s: int, cid: str, res: PlanResult, tick: int, then: dict | None) -> None:
        self._job_cids.pop(res.job_id, None)
        m = self.tracker.meta.get(int(s))
        self.managed_cids.discard(cid)
        if m is None or m.cid != cid:
            return
        m.job_id = None
        if not res.ok or not res.trajectories:
            code = int(res.code or 125)
            self.emit("plan.failed", job_id=res.job_id, code=code, detail=res.detail, remedy=res.remedy, level=2)
            self.tracker.release(s)
            if self.cmd is not None:
                self.cmd.fine_result(cid, False, code)
            return
        e = self.tracker.cache.put_traj(res.trajectories[0])
        self.tracker.load_bspline(self.S, s, e, cid=cid)
        if then is not None:
            self.tracker.slot_meta(s).next_phase = then
        self.extend_deadline(cid, e.duration_s + (60.0 if then else 0.0))
        self.emit("plan.ready", job_id=res.job_id, fields={"kind": res.job_id.split(":", 1)[0]}, vehicle_id=str(self.S.ids[s]),
                  planner=res.stats.get("planner"), t_ms=res.stats.get("t_ms"), apply_tick=int(tick),
                  stats={k: res.stats.get(k) for k in ("len_m", "duration_s", "zmax_m", "stretch_ratio")})
        if res.status == "degraded":
            self.emit("plan.degraded", job_id=res.job_id, reason=str(res.stats.get("degraded_reason")), level=2)
        if self.cmd is not None:
            self.cmd.fine_result(cid, True, 0)

    def fine_check(self, cid: str, polyline: np.ndarray, engine: Any) -> None:
        """M08 细校验执行者（register_fine_checker）：M10 提供者自管的调用跳过；其余调用提交 path_valid 作业。"""
        if cid in self.managed_cids or (self.cmd is not None and engine is not self.cmd):
            return
        if self.pool is None or self.world is None:
            engine.fine_result(cid, True, 0)
            return
        jid = f"path_valid:{cid}:{self.tick}"
        req = PlanRequest(jid, "path_valid", self.world_key, (), {"polyline": np.asarray(polyline, np.float64),
                                                                  "zones": self.zones, "r_col_m": 0.49},
                          {}, PRIO_INTERACTIVE, self.tick, BUDGET_MS["path_valid"], f"fine:{cid}")
        self.pool.submit(req, lambda res, tick, cid=cid: engine.fine_result(cid, res.ok, int(res.code or 102)))

    def _on_any_result(self, call: Any) -> None:
        if call.provider is not None:
            self.tracker.drop_suspended(call.cid)
            self.managed_cids.discard(call.cid)

    def on_slot_finished(self, s: int, cid: str | None, final_pos: Any = None) -> None:
        """跟踪器交回 HOLD。编队成员（`final_pos` 给出）：M08 的 follow_path 完成判据以调用目标点（生成时按本成员槽位与
        锚点终点航向算出的终点）为准，而交回点是锚点终点加滤波航向旋转后的槽位偏置；环形锚点上滤波航向滞后路径航向
        （τψ = 2 s，ω = v/R），偏置 12–24 m 时两点相差 0.5 m 以上，成员停在交回点永远不"到达"，到截止时间以 202 失败、
        挂起后续飞整圈（D1 验收第 1 轮 S2：soc 0.117、guard 6、POS_ERR_FAILSAFE）。交回时把调用目标点改为交回点
        （FX2-R2，ADR-065）。"""
        if final_pos is None or not cid or self.cmd is None:
            return None
        fn = getattr(self.cmd, "retarget_goal", None)
        if callable(fn):
            fn(cid, final_pos, int(s))
        return None

    def on_start_failed(self, m: MissionRT, r: dict) -> None:
        self.emit("mission.start_failed", mid=m.mid, code=int(r.get("code", 0)), detail=r.get("detail"), level=2)

    # ------------------------------------------------------------ 发布
    def publish_path(self, s: int, e: Any, rev: int) -> None:
        Q = np.asarray(e.Q, np.float64)
        pts = self._path_pts(Q, e.ts_s)
        self.publish_path_pts(s, pts, rev, int(e.traj_id))

    @staticmethod
    def _path_pts(Q: np.ndarray, ts: float) -> np.ndarray:
        """按弧长 ≤ 5 m、转角 ≤ 5° 抽样控制点（Schoenberg 控制点即按 ts 的时间样本）；返回 (k, 4)。"""
        P = Q[2:-2] if len(Q) > 4 else Q
        t = np.arange(len(P)) * ts
        if len(P) < 2:
            return np.c_[P, t]
        d = np.linalg.norm(np.diff(P, axis=0), axis=1)
        cum = np.r_[0.0, np.cumsum(d)]
        keep = np.zeros(len(P), bool)
        keep[0] = keep[-1] = True
        bucket = np.floor(cum / 5.0)
        keep[1:] |= bucket[1:] != bucket[:-1]
        u = np.diff(P, axis=0)
        n = np.linalg.norm(u, axis=1)
        ok = n > 1e-6
        un = np.where(ok[:, None], u / np.maximum(n, 1e-9)[:, None], 0.0)
        cosang = (un[1:] * un[:-1]).sum(1)
        keep[1:-1] |= cosang < math.cos(math.radians(5.0))
        idx = np.flatnonzero(keep)
        if len(idx) > 4096:
            idx = idx[np.linspace(0, len(idx) - 1, 4096).round().astype(np.int64)]
        return np.c_[P[idx], t[idx]]

    def publish_path_pts(self, s: int, pts4: np.ndarray, rev: int, traj_id: int) -> None:
        self.stats["paths"] += 1
        if self.S is None:
            return
        vid = self.S.ids[s]
        if vid is None:
            return
        blob = BS.blob_polyline4(pts4)
        fname = f"{vid}.{rev}.{traj_id}.bin"
        if self.run_dir is not None:
            d = self.run_dir / "paths"
            try:
                d.mkdir(parents=True, exist_ok=True)
                tmp = d / (fname + ".tmp")
                tmp.write_bytes(blob)
                tmp.replace(d / fname)
                old = self._path_files.get(vid)
                if old and old != fname:
                    (d / old).unlink(missing_ok=True)
                self._path_files[vid] = fname
            except OSError:
                log.exception("path blob write failed")
                return
        self.emit("path.changed", vehicle_id=str(vid), traj_id=int(traj_id), revision=int(rev), file=fname,
                  bytes=len(blob))

    def publish_coverage(self, mid: str, seq: int, blob: bytes, geom: dict) -> None:
        """覆盖栅格 owner 快照（FR-052，ext）：写 `$AWR_RUN_DIR/coverage/<mid>.<seq>.bin`，发内部事件 `coverage.changed`，
        由 api 推送 `mission/{mid}/coverage`（只推给订阅者；sim-core 侧 ≤ 1 Hz、仅在栅格有新戳记时写出）。"""
        self.stats["coverage"] = self.stats.get("coverage", 0) + 1
        fname = f"{mid}.{int(seq)}.bin"
        if self.run_dir is not None:
            d = self.run_dir / "coverage"
            try:
                d.mkdir(parents=True, exist_ok=True)
                tmp = d / (fname + ".tmp")
                tmp.write_bytes(blob)
                tmp.replace(d / fname)
                old = self._cov_files.get(mid)
                if old and old != fname:
                    (d / old).unlink(missing_ok=True)
                self._cov_files[mid] = fname
            except OSError:
                log.exception("coverage blob write failed")
                return
        self.emit("coverage.changed", mid=mid, seq=int(seq), file=fname, bytes=len(blob), grid=geom)

    def mission_metrics(self, m: MissionRT) -> dict:
        out = {}
        if self.coverage is not None:
            out.update(self.coverage.metrics_for(m.mid))
        return out

    def publish_status(self, missions: dict, order: list[str]) -> None:
        """`state/mission` 状态发布：任务有变化且距上次发布 ≥ 0.5 s【墙钟】，或距上次发布 ≥ 1 s（心跳）时发布。每次 stage
        汇总的轨道数至多 STATUS_TRACKS_PER_STAGE（至少一个任务），超出的任务按轮转留到下一次 stage：ladder n1000 的 4 个
        250 机任务此前在同一次 stage 内一起发布心跳，约 4.6 ms（FX2-R3，ADR-070）。轨道总数不超过该值时（S1–S6）与此前
        同一 stage 内发布全部到期任务一致。"""
        if self.events is None or not order:
            return
        clk = getattr(self.cmd, "clock", None)
        now_w = int(clk.wall_mono_ns()) if clk is not None and hasattr(clk, "wall_mono_ns") else self.t_ns
        items = []
        n = len(order)
        start = self._status_rot % n
        used = 0
        for j in range(n):
            mid = order[(start + j) % n]
            m = missions.get(mid)
            if m is None:
                continue
            last = self._wall_last_pub.get(mid, 0)
            hb = now_w - last >= 1_000_000_000
            if not (m.dirty or hb):
                continue
            if m.dirty and not hb and now_w - last < 500_000_000:
                continue
            nt = len(m.tracks)
            if items and used + nt > STATUS_TRACKS_PER_STAGE:
                self._status_rot = (start + j) % n
                break
            used += nt
            st = self.missions.status(m)
            m.last_status = st
            m.dirty = False
            self._wall_last_pub[mid] = now_w
            items.append(st)
        else:
            self._status_rot = start
        if not items:
            return
        try:
            if self._pub_mission is None:
                self._pub_mission = self.events.bus.publisher(bus_keys.STATE_MISSION)
            self._pub_mission.put(msgpack.packb(items, use_bin_type=True))
            self.stats["status_puts"] += 1
        except Exception:
            log.exception("mission status publish failed")

    # ------------------------------------------------------------ stage 入口
    def st_tracker(self, S: Any, ctx: Any) -> None:
        if not self.bound:
            self.bind(S, ctx)
        if self.pool is not None:
            self.pool.drain(int(ctx.tick))
        if self.director is not None:
            self.director.check_timed(int(ctx.t_ns))
        self.tracker.stage(S, ctx)

    def st_engine(self, S: Any, ctx: Any) -> None:
        if not self.bound:
            self.bind(S, ctx)
        self.missions.stage(S, ctx)

    def st_coverage(self, S: Any, ctx: Any) -> None:
        if self.bound and self.coverage is not None:
            self.coverage.stage(S, ctx)

    def st_director(self, S: Any, ctx: Any) -> None:
        if self.bound and self.director is not None:
            self.director.stage(S, ctx)
