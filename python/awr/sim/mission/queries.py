"""`ctl/sim-core/query` 的 M10 路由（M10 §7.1、§7.4.1；AWR-17 §4.3.7、§9.4）。

api 进程不做规划计算：`rest/scenarios.py` 的 R23–R26、R65 经总线查询到 sim-core，由本模块在慢任务中回答。

| op | 用途 | 角色 |
|---|---|---|
| `mission/list` | R23 列表项 `{mid, state, generator, vehicles[], progress_pct, t_start_ns}` | viewer |
| `mission/detail` | R24 详情（tracks、paths、region、formation、plan、coverage_grid、metrics、energy_precheck） | viewer |
| `mission/create` | R25 由生成器创建任务（IDLE；`start.now` 时立即尝试启动） | operator 席 |
| `mission/preview`、`mission/preview/get` | R26、R65 预览（plan-pool `preview` 作业；结果保留 60 s【墙钟】） | viewer |
| `mission/control` | `mission/{mid}/start|pause|resume|abort` 的服务实现（CommandEngine 服务分发接入前的等价入口） | operator 席 |
| `scenario/result` | 剧本导演当前的谓词求值与结果 | viewer |

写操作校验 api 签名的 principal（K_entry）与席位。
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

import numpy as np

from awr.sim.planning.jobs import BUDGET_MS, PRIO_INTERACTIVE, PlanRequest

if TYPE_CHECKING:
    from .runtime import M10Runtime

__all__ = ["Queries"]

PREVIEW_TTL_NS = 60_000_000_000


class Queries:
    def __init__(self, rt: M10Runtime) -> None:
        self.rt = rt
        self.previews: dict[str, dict] = {}

    # ------------------------------------------------------------ 公共
    def _wall(self) -> int:
        clk = getattr(self.rt.cmd, "clock", None)
        return int(clk.wall_mono_ns()) if clk is not None and hasattr(clk, "wall_mono_ns") else self.rt.t_ns

    def _write_ok(self, m: dict) -> int:
        """0 或原因码：115 viewer、116 席位、302 签名。"""
        p = m.get("principal")
        if not isinstance(p, dict):
            return 115
        if p.get("_internal"):
            return 0
        key = getattr(self.rt.cmd, "entry_key", None)
        if key is not None:
            from awr.runtime.principal import Principal, verify_principal

            sig = p.get("sig")
            if isinstance(sig, str):
                sig = sig.encode("latin-1")
            try:
                pr = Principal(str(p["principal_id"]), p["role"], p["entry"], p.get("conn_id"), bool(p.get("seat", False)))
            except (KeyError, TypeError):
                return 115
            if not isinstance(sig, (bytes, bytearray)) or not verify_principal(pr, str(m.get("cid") or ""), bytes(sig), key):
                return 115
        if p.get("role") not in ("operator", "admin"):
            return 115
        lease = self.rt.lease
        if lease is not None and not lease.is_seat_holder(str(p.get("principal_id"))):
            return 116
        return 0

    def ops(self) -> dict:
        return {"mission/list": self.list, "mission/detail": self.detail, "mission/create": self.create,
                "mission/preview": self.preview, "mission/preview/get": self.preview_get,
                "mission/control": self.control, "scenario/result": self.scenario_result}

    # ------------------------------------------------------------ 读
    def list(self, m: dict, ctx: Any = None) -> dict:
        eng = self.rt.missions
        items = []
        for mid in eng.order:
            x = eng.missions.get(mid)
            if x is None:
                continue
            items.append({"mid": mid, "state": x.state, "generator": x.spec.get("generator"), "vehicles": x.vehicles,
                          "progress_pct": round(eng.progress(x), 1), "t_start_ns": int(x.t_start_ns) or None,
                          "revision": x.revision, "origin": x.origin})
        return {"v": 1, "code": 0, "items": items}

    def detail(self, m: dict, ctx: Any = None) -> dict:
        args = m.get("args") or {}
        eng = self.rt.missions
        x = eng.missions.get(str(args.get("mid")))
        if x is None:
            return {"v": 1, "code": 305, "detail": {"mid": args.get("mid")}}
        tracks, paths = [], []
        for t in x.tracks.values():
            its = []
            for it in t.items:
                its.append({"seq": int(it.get("seq", 0)), "kind": it.get("kind"), "primitive": it.get("primitive"),
                            "pos": [round(float(v), 3) for v in it.get("start", [0, 0, 0])],
                            "speed_mps": it.get("speed_mps"), "est": it.get("est"), "traj_key": it.get("traj_key")})
                g = it.get("geometry") or {}
                if g.get("polyline_enu_m"):
                    paths.append({"vehicle_id": t.vehicle_id, "seq": int(it.get("seq", 0)),
                                  "polyline_enu_m": g["polyline_enu_m"]})
            tracks.append({"vehicle_id": t.vehicle_id, "state": t.state, "cursor": t.cursor, "reason": t.reason,
                           "photos": int(t.photos), "items": its})
        ex = x.extra or {}
        out = {"v": 1, "code": 0, "mid": x.mid, "state": x.state, "generator": x.spec.get("generator"),
               "vehicles": x.vehicles, "progress_pct": round(eng.progress(x), 1), "t_start_ns": int(x.t_start_ns) or None,
               "revision": x.revision, "origin": x.origin, "params": x.spec.get("params"), "tracks": tracks,
               "paths": paths, "region": ex.get("region"), "formation": ex.get("formation"),
               "min_safe_alt_profile": [{"vehicle_id": k} | v for k, v in (ex.get("profiles") or {}).items()],
               "plan": {"planner": (ex.get("stats") or {}).get("planner") or f"generator:{x.spec.get('generator')}",
                        "t_ms": round(x.plan_ms, 2), "degraded": any(
                            (tr.get("source") or {}).get("degraded") for tr in x.trajs.values()),
                        "state": x.gen, "error": x.gen_error},
               "metrics": self.rt.mission_metrics(x), "energy_precheck": x.energy or None}
        cov = self.rt.coverage.area.get(x.mid) if self.rt.coverage is not None else None
        if cov is not None:
            out["coverage_grid"] = cov.snapshot_geom()          # 推送快照（mission/{mid}/coverage）的几何
        return out

    def scenario_result(self, m: dict, ctx: Any = None) -> dict:
        d = self.rt.director
        if d is None or d.sc is None:
            return {"v": 1, "code": 305, "detail": {"scenario": None}}
        return {"v": 1, "code": 0, "scenario_id": d.sc.scenario_id, "sha256": d.sc.sha256, "phase": d.phase,
                "result": d.result, "now": d.evaluate_now()}

    # ------------------------------------------------------------ 写
    def create(self, m: dict, ctx: Any = None) -> dict:
        code = self._write_ok(m)
        if code:
            return {"v": 1, "code": code}
        a = dict(m.get("args") or {})
        spec = {k: a[k] for k in ("generator", "params", "vehicle_ids", "constraints", "sync_policy", "priority",
                                  "on_done", "on_abort", "resume_on_lease_return") if k in a}
        err = self._validate(spec)
        if err is not None:
            return err
        spec["mission_id"] = str(a.get("mission_id") or ("m-" + secrets.token_hex(4)))
        start = a.get("start")
        p = m.get("principal") or {}
        try:
            mid = self.rt.missions.create(spec, "operator", {"principal_id": p.get("principal_id")}, precheck="warn",
                                          start=start if (start and not start.get("now")) else None)
        except Exception as e:
            return {"v": 1, "code": 110, "detail": {"reason": str(e)[:200]}}
        x = self.rt.missions.missions[mid]
        if start and start.get("now"):
            x.start_requested = True
        return {"v": 1, "code": 0, "mid": mid, "state": x.state, "revision": x.revision}

    def _validate(self, spec: dict) -> dict | None:
        from .scenario_loader import gen_validator

        gen = spec.get("generator")
        if gen not in ("lawnmower", "helix_scan", "orbit", "expanding_square", "corridor", "terrain_follow", "formation",
                       "follow_path"):
            return {"v": 1, "code": 110, "detail": {"pointer": "/generator"}}
        if not isinstance(spec.get("vehicle_ids"), list) or not spec["vehicle_ids"]:
            return {"v": 1, "code": 110, "detail": {"pointer": "/vehicle_ids"}}
        for vid in spec["vehicle_ids"]:
            if self.rt.slot_of(str(vid)) < 0:
                return {"v": 1, "code": 107, "detail": {"pointer": "/vehicle_ids", "uav": vid}}
        errs = sorted(gen_validator(gen).iter_errors(spec.get("params") or {}), key=lambda e: list(map(str, e.absolute_path)))
        if errs:
            return {"v": 1, "code": 110, "detail": {"pointer": "/params/" + "/".join(map(str, errs[0].absolute_path)),
                                                    "message": errs[0].message[:160]}}
        return None

    def control(self, m: dict, ctx: Any = None) -> dict:
        code = self._write_ok(m)
        if code:
            return {"v": 1, "code": code}
        a = m.get("args") or {}
        op = str(a.get("op"))
        mid = str(a.get("mid"))
        eng = self.rt.missions
        fn = {"start": eng.start, "pause": eng.pause, "resume": eng.resume, "abort": eng.abort}.get(op)
        if fn is None:
            return {"v": 1, "code": 300, "detail": {"op": op}}
        r = fn(mid, m.get("principal"))
        return {"v": 1, "code": int(r.get("code", 0)), "detail": r.get("detail")}

    def command_handler(self, op: str):
        """`ctl/sim-core/cmd` 的 `mission/<op>` 处理者（M08 `register_command_handler`）：
        `fn(msg, apply_tick, ctx) -> {status, code, detail?}`；在步顶同步执行。"""
        eng = self.rt.missions

        def handler(msg: dict, apply_tick: int, ctx: Any = None) -> dict:
            a = msg.get("args") or {}
            mid = a.get("mid")
            if not isinstance(mid, str) or not mid:
                return {"status": "rejected", "code": 110, "detail": {"pointer": "/args/mid"}}
            if eng is None:
                return {"status": "rejected", "code": 109}
            fn = {"start": eng.start, "pause": eng.pause, "resume": eng.resume, "abort": eng.abort}[op]
            r = fn(mid, msg.get("principal"))
            code = int(r.get("code", 0))
            out = {"status": "accepted" if code == 0 else "rejected", "code": code, "detail": r.get("detail")}
            if code == 0 and op == "start" and (r.get("detail") or {}).get("warnings"):
                out["warnings"] = list(r["detail"]["warnings"])
            return out

        handler.__module__ = __name__
        return handler

    # ------------------------------------------------------------ 预览（R26、R65）
    def preview(self, m: dict, ctx: Any = None) -> dict:
        a = dict(m.get("args") or {})
        spec = {k: a[k] for k in ("generator", "params", "vehicle_ids", "constraints") if k in a}
        err = self._validate(spec)
        if err is not None:
            return err
        rt = self.rt
        self._gc()
        pid = "pv-" + secrets.token_hex(3)
        veh = []
        for vid in spec["vehicle_ids"]:
            s = rt.slot_of(str(vid))
            veh.append({"vehicle_id": vid, "home_enu_m": rt.S.enu.home[s].tolist(), "pos_enu_m": rt.S.enu.pos[s].tolist(),
                        "v_limit_mps": rt.v_limit(s), "cruise_mps": rt.cruise(s), "r_col_m": rt.r_col(s),
                         "yawrate_max_rad_s": rt.yawrate_max(s)})
        payload = {"generator": spec["generator"], "params": spec.get("params") or {}, "vehicles": veh,
                   "constraints": spec.get("constraints") or {}, "zones": rt.zones, "mission_id": pid, "camera": rt.camera}
        req = PlanRequest(f"preview:{pid}:0", "preview", rt.world_key, tuple(spec["vehicle_ids"]), payload,
                          rt.limits_dict(), PRIO_INTERACTIVE, rt.tick, BUDGET_MS["preview"], f"preview:{pid}")
        self.previews[pid] = {"status": "pending", "t_wall": self._wall(), "result": None, "spec": spec}
        rt.pool.submit(req, lambda res, tick, pid=pid: self._on_preview(pid, res))
        return {"v": 1, "code": 0, "preview_id": pid, "status": "pending"}

    def _on_preview(self, pid: str, res: Any) -> None:
        p = self.previews.get(pid)
        if p is None:
            return
        if not res.ok:
            p["status"] = "failed"
            p["result"] = {"code": int(res.code or 125), "detail": res.detail, "remedy": res.remedy}
            return
        ex = res.extra or {}
        rt = self.rt
        per = []
        for i, path in enumerate(ex.get("paths") or []):
            path["color_idx"] = i
        # 能量预检（每机）：以作业轨迹 1 s 抽样调用能量模型（与任务启动时同一实现）
        for vid in p["spec"]["vehicle_ids"]:
            s = rt.slot_of(str(vid))
            smp = [np.asarray(tr["samples_1s"]) for tr in res.trajectories if tr.get("vehicle_id") == vid]
            wh = sum(rt.path_wh(s, x) for x in smp if len(x) >= 2)
            soc_after = rt.soc(s) - wh / rt.e_use(s)
            per.append({"id": vid, "energy_wh": round(wh, 2), "soc_after_pct": round(soc_after * 100.0, 1)})
            for path in ex.get("paths") or []:
                if path["vehicle_id"] == vid:
                    path["energy_wh"] = round(wh, 2)
                    path["soc_after_pct"] = round(soc_after * 100.0, 1)
        cov = (ex.get("stats") or {}).get("coverage") or {}
        p["status"] = "ok"
        p["result"] = {"preview_id": pid, "status": "ok", "generator": p["spec"]["generator"],
                       "planner": "profile", "paths": ex.get("paths") or [],
                       "min_safe_alt_profile": [{"vehicle_id": k} | v for k, v in (ex.get("profiles") or {}).items()],
                       "energy_precheck": {"feasible": all(x["soc_after_pct"] >= 20.0 for x in per), "per_vehicle": per},
                       "region": ex.get("region"), "formation": ex.get("formation"),
                       "stats": {"plan_ms": res.stats.get("t_ms"), "coverage_pred": cov.get("coverage_pred"),
                                 "balance": cov.get("balance"), "theta_deg": cov.get("theta_deg"),
                                 "spacing_m": cov.get("spacing_m")},
                       "warnings": ex.get("warnings") or []}

    def preview_get(self, m: dict, ctx: Any = None) -> dict:
        self._gc()
        pid = str((m.get("args") or {}).get("preview_id"))
        p = self.previews.get(pid)
        if p is None:
            return {"v": 1, "code": 305, "detail": {"reason": "PREVIEW_EXPIRED"}}
        if p["status"] == "pending":
            return {"v": 1, "code": 0, "status": "pending", "preview_id": pid}
        if p["status"] == "failed":
            return {"v": 1, "code": int(p["result"]["code"]), "detail": p["result"]}
        return {"v": 1, "code": 0, "status": "ok", "result": p["result"]}

    def _gc(self) -> None:
        now = self._wall()
        for k in [k for k, v in self.previews.items() if now - v["t_wall"] > PREVIEW_TTL_NS]:
            del self.previews[k]
