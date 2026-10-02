"""剧本导演（M10-FR-063、FR-064；AWR-12 §7.1；AWR-16 §12.3）：stage `director`（order 160、every 25、phase 8，10 Hz）。

- 布设：设置倍速（`rate`）→ 移除不在剧本中的骨架机体（`fleet/remove`，地面强制）→ 下一 stage 起 `fleet/add` 剧本机体
  （z 为 null 时放在 DSM 表面）→ 机体就位后创建任务（origin scenario）；
- 事件：`at_s` 在跟踪器 stage（每 2 tick）检查，触发误差 ≤ 1 个 L1 tick；`when` 以 10 Hz【仿真】求值，为真后延迟
  `delay_s` 执行，`once` 缺省 true；动作以 principal `scenario:<id>` 经完整准入执行：`cmd` 只允许 rtl、land、hover，
  任务启停用 `mission.*`；`env.*`、`fault.inject` 下发为 `env/*`、`fault/inject` 命令（由 M07、M08 登记实现）；
  `target.spawn`、`agent.task` 发 `scenario.event`（M13、M14 订阅，ext）；`mark` 写事件；
- 成功谓词：封闭文法 all/any/not/leaf；度量缺失判假（失败即关闭）；结束条件为全部任务 DONE 或 ABORTED 且全部机体
  上锁（DISARMED）、或到达 `time_limit_s`；结果 `scenario.result{status, predicates[{expr, value, ok}]}`，
  按 `on_complete`（pause、continue、reset）处理会话；
- `compile_tsir()`：谓词编译为 TSIR 数值形式（AND 1、OR 2、NOT 3、THRESHOLD 12；LT 1、LE 2、EQ 3、GE 4、GT 5；
  `!=` 为 NOT(EQ)），供 M14 复用。
"""

from __future__ import annotations

import contextlib
import logging
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime import M10Runtime
    from .scenario_loader import LoadedScenario

__all__ = ["Director", "compile_tsir", "eval_predicate", "metric_key"]

log = logging.getLogger("awr.sim.mission.director")

TSIR_OP = {"<": 1, "<=": 2, "==": 3, ">=": 4, ">": 5}
ADD_PER_STAGE = 50  # 布设阶段每次导演 stage（10 Hz【仿真】）至多加入的机体数
AND, OR, NOT, THRESHOLD = 1, 2, 3, 12


def metric_key(leaf: dict) -> str:
    args = leaf.get("args") or {}
    if not args:
        return str(leaf["metric"])
    kv = ",".join(f"{k}={_fmt(args[k])}" for k in sorted(args))
    return f"{leaf['metric']}{{{kv}}}"


def _fmt(v: Any) -> str:
    if isinstance(v, list):
        return "[" + ",".join(_fmt(x) for x in v) + "]"
    return str(v)


def compile_tsir(p: dict) -> dict:
    for k, op in (("all", AND), ("any", OR)):
        if k in p:
            items = p[k]
            if len(items) == 1:
                return compile_tsir(items[0])
            return {"op": op, "args": [compile_tsir(x) for x in items]}
    if "not" in p:
        return {"op": NOT, "args": [compile_tsir(p["not"])]}
    v = p["value"]
    val = (1.0 if v else 0.0) if isinstance(v, bool) else (_enum_value(v) if isinstance(v, str) else float(v))
    if p["op"] == "!=":
        return {"op": NOT, "args": [{"op": THRESHOLD, "thresh": {"metric": metric_key(p), "op": TSIR_OP["=="],
                                                                 "value": val}}]}
    return {"op": THRESHOLD, "thresh": {"metric": metric_key(p), "op": TSIR_OP[p["op"]], "value": val}}


def _enum_value(name: str) -> float:
    try:
        from awr.contracts.enums import FLIGHTSTATE_NAMES

        return float(list(FLIGHTSTATE_NAMES).index(name))
    except (ValueError, ImportError):
        return math.nan


def _cmp(a: float, op: str, b: float) -> bool:
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == "==":
        return abs(a - b) <= 1e-9
    if op == "!=":
        return abs(a - b) > 1e-9
    if op == ">=":
        return a >= b
    return a > b


def eval_predicate(p: dict, get: Any, trace: list | None = None) -> bool:
    """get(leaf) → float 或 None（缺失）；缺失判假。trace 收集叶子 {expr, value, ok}。"""
    if "all" in p:
        return all([eval_predicate(x, get, trace) for x in p["all"]])
    if "any" in p:
        return any([eval_predicate(x, get, trace) for x in p["any"]])
    if "not" in p:
        return not eval_predicate(p["not"], get, trace)
    v = get(p)
    tgt = p["value"]
    if v is None or (isinstance(v, float) and math.isnan(v)):
        ok = False
    else:
        t = (1.0 if tgt else 0.0) if isinstance(tgt, bool) else (_enum_value(tgt) if isinstance(tgt, str) else float(tgt))
        ok = _cmp(float(v), p["op"], t)
    if trace is not None:
        trace.append({"expr": f"{metric_key(p)} {p['op']} {_fmt(tgt)}", "value": None if v is None else round(float(v), 6),
                      "ok": bool(ok)})
    return ok


class Director:
    def __init__(self, rt: M10Runtime) -> None:
        self.rt = rt
        self.sc: LoadedScenario | None = None
        self.phase = "idle"            # idle、remove、add、missions、running、done
        self.events: list[dict] = []
        self.fired: set[str] = set()
        self.pending: list[tuple[int, dict]] = []     # (到期 t_ns, event)
        self.when_since: dict[str, int] = {}
        self.result: dict | None = None
        self.n = 0
        self.next_at_ns = math.inf
        self._add_queue: list[dict] = []
        self._add_checked = False
        self.principal: dict = {}
        self.warnings: list[str] = []

    # ------------------------------------------------------------ 加载
    def load(self, sc: LoadedScenario) -> None:
        self.sc = sc
        self.phase = "remove"
        self.events = [dict(e) for e in sc.doc.get("events") or []]
        self.fired.clear()
        self.pending.clear()
        self.when_since.clear()
        self.result = None
        self.principal = {"principal_id": f"scenario:{sc.scenario_id}", "role": "scenario", "entry": "scenario",
                          "seat": False, "source": "SCENARIO"}
        self._next_at()
        rt = self.rt
        clk = getattr(rt.cmd, "clock", None)
        rate = float(sc.doc.get("rate", 1))
        if clk is not None and rate != 1.0:
            try:
                clk.apply("speed", {"rate": rate})
            except Exception:
                log.exception("scenario rate not applied")
        self._step_setup()

    def _next_at(self) -> None:
        ts = [float(e["at_s"]) for e in self.events if e.get("at_s") is not None and e["event_id"] not in self.fired]
        self.next_at_ns = min(ts) * 1e9 if ts else math.inf

    def _step_setup(self) -> None:
        rt = self.rt
        sc = self.sc
        if sc is None or rt.roster is None:
            return
        want = {v["vehicle_id"]: v for v in sc.vehicles}
        if self.phase == "remove":
            busy = False
            sent = 0
            for e in list(rt.roster.by_slot.values()):
                if e.id in want and self._home_matches(e, want[e.id]):
                    continue
                busy = True
                if sent >= ADD_PER_STAGE:  # 与加入同样分批（AWR_SIM_N 布设了大机群时）
                    break
                sent += 1
                self.n += 1
                rt.submit_internal({"cid": f"scn:{sc.scenario_id}:rm:{self.n}", "op": "fleet/remove", "uav": e.id,
                                    "args": {"id": e.id, "force": True, "confirm_token": "scenario"}}, self.principal)
            if not busy:
                self.phase = "add"
                self._add_checked = False
                self._add_queue = [v for v in sc.vehicles if rt.roster.resolve(v["vehicle_id"]) is None]
            else:
                return
        if self.phase == "add":
            if not self._add_checked:
                if any(rt.roster.resolve(v["vehicle_id"]) is not None and not self._home_matches(
                        rt.roster.resolve(v["vehicle_id"]), v) for v in sc.vehicles):
                    return   # 同名骨架机体尚未移除
                self._add_checked = True
            # 每次 stage（10 Hz）至多提交 ADD_PER_STAGE 架：1000 架在同一 tick 内逐架加入时单个 tick 超过 2 s，被 supervisor
            # 判挂死（D1 验收第 1 轮 4.3）；分批后 n1000 的布设约 2 s【仿真】完成，单个 tick 的增量有界
            batch, self._add_queue = self._add_queue[:ADD_PER_STAGE], self._add_queue[ADD_PER_STAGE:]
            for v in batch:
                self.n += 1
                args = {"vehicle_id": v["vehicle_id"], "profile_id": v.get("profile_id", "p600_mid360"),
                        "home_enu_m": list(v["home_enu_m"]), "yaw_rad": float(v.get("yaw_rad", 0.0)),
                        "initial_soc": float(v.get("initial_soc", 1.0))}
                if v.get("speed_profile"):
                    args["speed_profile"] = v["speed_profile"]
                if v.get("sensors") is not None:
                    args["sensors"] = list(v["sensors"])
                self._configure_sensors(v)
                adm = rt.submit_internal({"cid": f"scn:{sc.scenario_id}:add:{self.n}", "op": "fleet/add",
                                          "args": args}, self.principal)
                if adm.get("status") == "rejected":
                    log.warning("scenario vehicle add rejected", extra={"kv": {"id": v["vehicle_id"],
                                                                                "code": adm.get("code"),
                                                                                "detail": adm.get("detail")}})
                    self.warnings.append(f"add {v['vehicle_id']}: {adm.get('code')}")
            if not self._add_queue:
                self.phase = "missions"
            return
        if self.phase == "missions":
            if any(rt.roster.resolve(v["vehicle_id"]) is None for v in sc.vehicles):
                return
            for m in sc.missions:
                spec = {k: m[k] for k in ("mission_id", "generator", "params", "vehicle_ids", "sync_policy", "priority",
                                          "on_done", "on_abort", "resume_on_lease_return") if k in m}
                if m.get("constraints"):
                    spec["constraints"] = m["constraints"]
                tr = sc.doc.get("transit") or {}
                cons = dict(spec.get("constraints") or {})
                if tr.get("planner") and "transit_planner" not in cons:
                    cons["transit_planner"] = tr["planner"]
                if tr.get("margin_m") is not None and "clearance_m" not in cons:
                    cons["clearance_m"] = max(2.0, float(tr["margin_m"]))
                if tr.get("layer_dz_m") is not None and "layer_dz_m" not in cons:
                    cons["layer_dz_m"] = max(2.0, float(tr["layer_dz_m"]))
                spec["constraints"] = cons
                start = m.get("start") or {"on": "ready"}
                try:
                    rt.missions.create(spec, "scenario", self.principal, precheck=sc.doc.get("energy_precheck", "reject"),
                                       start=start)
                except Exception as e:
                    log.exception("scenario mission create failed")
                    self.warnings.append(f"mission {m.get('mission_id')}: {e}")
            self.phase = "running"

    @staticmethod
    def _home_matches(e: Any, v: dict) -> bool:
        h = list(v["home_enu_m"])
        eh = list(e.home_enu_m)
        return abs(float(eh[0]) - float(h[0])) < 0.01 and abs(float(eh[1]) - float(h[1])) < 0.01

    # ------------------------------------------------------------ 定时事件（每 2 tick 检查）
    def check_timed(self, t_ns: int) -> None:
        if t_ns < self.next_at_ns or self.phase not in ("running",):
            return
        for e in self.events:
            if e.get("at_s") is None or e["event_id"] in self.fired:
                continue
            if t_ns >= float(e["at_s"]) * 1e9:
                self._fire(e)
        self._next_at()

    # ------------------------------------------------------------ stage（10 Hz）
    def stage(self, S: Any, ctx: Any) -> None:
        if self.sc is None:
            return
        if self.phase in ("remove", "add", "missions"):
            self._step_setup()
            return
        if self.phase != "running":
            return
        t = int(ctx.t_ns)
        self.check_timed(t)
        for e in self.events:
            if e.get("when") is None:
                continue
            eid = e["event_id"]
            if eid in self.fired and e.get("once", True):
                continue
            ok = eval_predicate(e["when"], self._metric)
            if ok:
                if eid not in self.when_since:
                    self.when_since[eid] = t
                if t - self.when_since[eid] >= int(float(e.get("delay_s", 0.0)) * 1e9):
                    self._fire(e)
                    self.when_since.pop(eid, None)
            else:
                self.when_since.pop(eid, None)
        self._check_end(t)

    def _configure_sensors(self, v: dict) -> None:
        """剧本机体的传感器子集与能力集交给 M13（M13-to-M10 第 5 条；M13-FR-041）。剧本 `agents.tasks[].capability` 所列能力
        在成员机体上为委派能力：只在持有 AGENT 租约时检测（R-12，M14-to-M13 第 2 条、M14-to-M10 第 4 条）。"""
        srt = self.rt.sensor_runtime()
        if srt is None or (v.get("sensors") is None and v.get("caps") is None):
            return
        ag = (self.sc.doc.get("agents") if self.sc is not None else None) or {}
        task_caps = {str(t.get("capability")) for t in ag.get("tasks") or [] if t.get("capability")}
        member = next((m for m in ag.get("members") or [] if m.get("vehicle_id") == v["vehicle_id"]), None)
        delegated = sorted(set(member.get("capabilities") or []) & task_caps) if member is not None else []
        try:
            srt.configure_vehicle(v["vehicle_id"], sensors=v.get("sensors"), caps=v.get("caps"), delegated=delegated or None)
        except TypeError:  # 旧版 SensorRuntime 没有 delegated 参数
            srt.configure_vehicle(v["vehicle_id"], sensors=v.get("sensors"), caps=v.get("caps"))
        except Exception:
            log.exception("sensor configure failed")

    def _metric(self, leaf: dict) -> float | None:
        from awr.sim.core import metrics as MET

        try:
            return float(MET.metric(str(leaf["metric"]), **(leaf.get("args") or {})))
        except (KeyError, LookupError, TypeError, ValueError):
            return None
        except Exception:
            return None

    def _fire(self, e: dict) -> None:
        rt = self.rt
        eid = e["event_id"]
        self.fired.add(eid)
        act = e.get("action")
        args = dict(e.get("args") or {})
        self.n += 1
        cid = f"scn:{self.sc.scenario_id if self.sc else '-'}:{eid}:{self.n}"
        res: Any = None
        if act == "cmd":
            if args.get("op") in ("rtl", "land", "hover"):
                res = rt.submit_internal({"cid": cid, "op": args["op"], "uav": args.get("vehicle_id"),
                                          "args": dict(args.get("args") or {})}, self.principal)
        elif act in ("mission.start", "mission.pause", "mission.abort"):
            mid = str(args.get("mission_id"))
            fn = {"mission.start": rt.missions.start, "mission.pause": rt.missions.pause,
                  "mission.abort": rt.missions.abort}[act]
            res = fn(mid, self.principal)
        elif act in ("env.preset", "env.set", "env.gust"):
            res = rt.submit_internal({"cid": cid, "op": act.replace(".", "/"), "args": args}, self.principal)
        elif act == "vehicle.add":
            a = {"vehicle_id": args.get("vehicle_id"), "profile_id": args.get("profile_id", "p600_mid360"),
                 "home_enu_m": args.get("home_enu_m"), "yaw_rad": float(args.get("yaw_rad", 0.0)),
                 "initial_soc": float(args.get("initial_soc", 1.0))}
            if args.get("sensors") is not None:
                a["sensors"] = list(args["sensors"])
            if args.get("vehicle_id"):
                self._configure_sensors({"vehicle_id": args["vehicle_id"], "sensors": args.get("sensors"),
                                         "caps": args.get("caps")})
            res = rt.submit_internal({"cid": cid, "op": "fleet/add", "args": a}, self.principal)
        elif act == "vehicle.remove":
            res = rt.submit_internal({"cid": cid, "op": "fleet/remove", "uav": args.get("vehicle_id"),
                                      "args": {"id": args.get("vehicle_id")}}, self.principal)
        elif act == "fault.inject":
            res = rt.fault_inject(args, self.principal, cid)
        rt.emit("scenario.event", event_id=eid, action=act, args=args,
                result=None if res is None else {"code": int((res or {}).get("code") or 0)}, level=1)
        if act == "mark":
            rt.emit("scenario.mark", event_id=eid, label=str(args.get("label", "")), level=1)
        if rt.pool is not None and getattr(rt.pool, "inputlog", None) is not None:
            with contextlib.suppress(Exception):
                rt.pool.inputlog.append("scenario_event", rt.tick, {"event_id": eid, "action": act})

    def _check_end(self, t: int) -> None:
        if self.result is not None or self.sc is None:
            return
        rt = self.rt
        tl = float(self.sc.doc.get("time_limit_s", 1800))
        ms = list(rt.missions.missions.values())
        done = bool(ms) and all(m.state in ("DONE", "ABORTED") for m in ms)
        if done:
            try:
                from awr.contracts.enums import FlightState

                act = rt.S.active_idx()
                fs = rt.S.blocks["safety"]["fs"][act]
                done = bool((fs == int(FlightState.DISARMED)).all())
            except Exception:
                pass
        timeout = t >= tl * 1e9
        if not (done or timeout):
            return
        trace: list = []
        ok = eval_predicate(self.sc.doc["success"], self._metric, trace)
        status = "SUCCEEDED" if ok and not timeout else "FAILED"
        self.result = {"status": status, "predicates": trace, "timeout": timeout, "t_s": round(t * 1e-9, 3)}
        self.phase = "done"
        rt.emit("scenario.result", scenario_id=self.sc.scenario_id, status=status, predicates=trace, level=1)
        oc = str(self.sc.doc.get("on_complete", "pause"))
        clk = getattr(rt.cmd, "clock", None)
        if oc == "pause" and clk is not None:
            with contextlib.suppress(Exception):
                clk.apply("pause")

    def evaluate_now(self) -> dict:
        if self.sc is None:
            return {"status": "NO_SCENARIO", "predicates": []}
        trace: list = []
        ok = eval_predicate(self.sc.doc["success"], self._metric, trace)
        return {"status": "SUCCEEDED" if ok else "FAILED", "predicates": trace}
