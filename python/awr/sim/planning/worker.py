"""plan-pool 进程入口（M10-FR-030、FR-031、FR-032；M10 §6.3.3、§7.4.3）。

`init_worker(worlds_dir, world_key, cpu, parent_pid, parent_cpus)` 在 spawn 出的进程中执行一次：PDEATHSIG 并复核父进程 pid
（父进程已先行死亡即退出，不留孤儿）、亲和性避开 sim-core 所钉的核、负 nice 恢复为 0、`OMP_NUM_THREADS = 1`（spawn 前已由父进程
设置，这里复核）、按 (world_id, contentVersion, coordinate.sha256) 打开并缓存 GeoWorld（M04 `open_world_query`，只映射
派生缓存，不派生）；绑定不一致时拒绝（123 PLAN_GRID_MISMATCH，P-01）。`worker_main(req)` 按作业类别分派：

| kind | 输入 payload | 输出 |
|---|---|---|
| warm | — | 预热 numba 核与规划栅格 |
| path_valid | polyline、zones、r_col_m | ok / infeasible（102 PATH_OBSTACLE、GOAL_IN_OBSTACLE） |
| safe_transit | start、goal、planner、clearance_m、alt_max_m、prefer_low、layer_dz_m | 转场轨迹 |
| follow_path | waypoints（含起点）、yaw | 轨迹（输入折线细校验失败为 102） |
| generator、preview | generator、params、vehicles、constraints | 每机作业项与轨迹、剖面、统计 |
| resume | start、ctrl_pts、ts_s、tau_s | 转场 + 当前项剩余部分（从静止重新 TOPP） |

结果的 `result_sha256` 为轨迹字节与作业项的哈希（不含耗时），同一输入逐字节相同（NFR-010）。本模块在 plan-pool
进程中执行，可读墙钟（作业耗时统计）；sim-core 侧只经 `pool.PlanPoolClient` 调用。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import bspline as BS
from . import smooth as SM
from .jobs import PlanRequest, PlanResult, result_digest
from .transit import plan_transit

__all__ = ["PlanGridMismatch", "get_world", "init_worker", "set_world", "traj_key", "worker_cpus", "worker_main"]

_STATE: dict[str, Any] = {"world": None, "key": None, "worlds_dir": None, "grid25": None}
MAX_PREVIEW_PTS = 2000


class PlanGridMismatch(RuntimeError):
    pass


def _pdeathsig() -> None:
    try:
        import ctypes
        import signal

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, int(signal.SIGTERM), 0, 0, 0)
    except Exception:
        pass


def worker_cpus(parent: tuple[int, ...] | None, ncpu: int | None = None) -> set[int] | None:
    """plan-pool 进程的 CPU 集合（M10-FR-030；FX2-R3，ADR-070）：spawn 出的进程继承 sim-core 的亲和性（supervisor 把 sim-core
    钉在单核，configs/runtime.yaml `cpus: [1]`），规划作业若与主循环同核会抢占 250 Hz 主循环。父进程只钉在部分 CPU 上时，
    返回其余 CPU；父进程未钉核（亲和性已是全部 CPU）或信息缺失时返回 None（保持继承）。"""
    n = int(ncpu or os.cpu_count() or 0)
    if not parent or n <= 0:
        return None
    rest = set(range(n)) - {int(c) for c in parent}
    return rest if rest and len(parent) < n else None


def init_worker(worlds_dir: str | None, world_key: tuple[str, str, str] | None, cpu: int | None = None,
                parent_pid: int | None = None, parent_cpus: tuple[int, ...] | None = None) -> None:
    """spawn 出的进程中执行一次。PDEATHSIG 只对设置之后的父进程死亡生效：父进程若在本进程走到这里之前已被 SIGKILL（supervisor
    挂死处置、kill -9），本进程已被过继给 init 或 subreaper，再设 PDEATHSIG 也不会触发，且进程自己持有 call_queue 的写端、
    永远读不到 EOF，成为常驻约 160 MB 的孤儿（D1 验收第 2 轮 4.1b）。因此设置之后复核父进程 pid，已变化即退出。
    亲和性：`cpu` 显式给出（`AWR_PLAN_POOL_CPU`）时钉在该核；否则避开父进程所钉的核（`worker_cpus`）。优先级：继承来的负 nice
    （sim-core `nice: -5`）恢复为 0，规划作业不与其他进程争抢。"""
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    _pdeathsig()
    if parent_pid is not None and os.getppid() != int(parent_pid):
        os._exit(0)
    cpus = {int(cpu)} if cpu is not None else worker_cpus(parent_cpus)
    if cpus:
        with contextlib.suppress(OSError, AttributeError, ValueError):
            os.sched_setaffinity(0, cpus)
    with contextlib.suppress(OSError, AttributeError):
        if os.getpriority(os.PRIO_PROCESS, 0) < 0:
            os.setpriority(os.PRIO_PROCESS, 0, 0)
    _STATE["worlds_dir"] = worlds_dir
    _STATE["key"] = tuple(world_key) if world_key else None


def set_world(world_key: tuple[str, str, str] | None, world: Any) -> None:
    """inline/thread 模式：直接注入 sim-core 已打开的 WorldQuery（同一只读栅格）。"""
    _STATE["key"] = tuple(world_key) if world_key else None
    _STATE["world"] = world
    _STATE["grid25"] = None


def get_world(world_key: tuple[str, str, str] | None) -> Any:
    w = _STATE["world"]
    if world_key is None:
        return w
    key = tuple(world_key)
    if w is not None:
        if (w.world_id, w.content_version, w.coordinate_sha256) != key:
            raise PlanGridMismatch(f"world binding {key} != loaded {(w.world_id, w.content_version, w.coordinate_sha256)}")
        return w
    wd = _STATE["worlds_dir"]
    if not wd:
        return None
    from awr.world.geometry.query import open_world_query

    w = open_world_query(Path(wd) / key[0], allow_derive=False)
    if (w.world_id, w.content_version, w.coordinate_sha256) != key:
        raise PlanGridMismatch(f"world on disk {(w.world_id, w.content_version, w.coordinate_sha256)} != session {key}")
    _STATE["world"] = w
    return w


def _grid25(world: Any, alt_min_agl_m: float) -> Any:
    g = _STATE.get("grid25")
    if g is None and world is not None:
        try:
            from .grid25 import Grid25

            g = Grid25.from_world(world, alt_min_agl_m)
            _STATE["grid25"] = g
        except Exception:
            g = None
    return g


def traj_key(world_cv: str, limits: dict, geometry: Any, extra: dict | None = None) -> str:
    """轨迹缓存键（FR-017）：world contentVersion、限值、几何与参数的 sha256 前 16 位。"""
    h = hashlib.sha256()
    h.update(str(world_cv).encode())
    h.update(json.dumps(limits, sort_keys=True).encode())
    g = np.ascontiguousarray(np.asarray(geometry, np.float64))
    h.update(str(g.shape).encode())
    h.update(g.tobytes())
    h.update(json.dumps(extra or {}, sort_keys=True, default=str).encode())
    return h.hexdigest()[:16]


def _traj_dict(r: SM.TrajResult, vehicle_id: str, limits: SM.Limits, planner: str, yaw: dict, key: str,
               t_ms: float) -> dict:
    Q = r.Q
    T = BS.duration(Q, r.ts_s)
    ends = BS.eval_bspline(Q, r.ts_s, [0.0, T])
    tq = np.arange(0.0, T + 1e-9, 1.0)
    if len(tq) and tq[-1] < T - 1e-9:
        tq = np.r_[tq, T]
    P1 = BS.eval_bspline(Q, r.ts_s, tq)
    V1 = BS.eval_bspline(Q, r.ts_s, tq, 1)
    return {"key": key, "vehicle_id": vehicle_id, "kind": "bspline", "ts_s": float(r.ts_s), "ctrl_pts": Q,
            "yaw": dict(yaw or {"mode": "lookahead", "t_fwd_s": 1.0}), "limits": limits.to_json(),
            "source": {"planner": planner, "plan_ms": round(t_ms, 3), "stretch_ratio": round(float(r.stretch_ratio), 4),
                       "degraded": bool(r.degraded)},
            "len_m": float(r.stats.get("len_m", float(np.linalg.norm(np.diff(P1, axis=0), axis=1).sum()) if len(P1) > 1
                                       else 0.0)),
            "duration_s": float(T), "start": ends[0].tolist(), "end": ends[1].tolist(),
            "samples_1s": np.c_[tq, P1, V1]}


def _limits(req: PlanRequest, speed: float | None = None) -> SM.Limits:
    return SM.limits_from(req.limits, speed)


def _fail(req: PlanRequest, status: str, code: int, detail: str | None, remedy: str | None = None,
          stats: dict | None = None) -> PlanResult:
    return PlanResult(req.job_id, status, (), None, stats or {}, detail, remedy, result_digest((), None, {"d": detail}),
                      code)


def _do_path_valid(req: PlanRequest, world: Any) -> PlanResult:
    p = req.payload
    ok, info = SM.validate_polyline(np.asarray(p["polyline"], np.float64), world, float(p.get("r_col_m", 0.49)),
                                    p.get("zones"))
    if ok:
        return PlanResult(req.job_id, "ok", (), None, info, None, None, result_digest((), None, {"ok": True}))
    return _fail(req, "infeasible", 102, info.get("reason", "PATH_OBSTACLE"), None, info)


def _do_transit(req: PlanRequest, world: Any, t0: float) -> PlanResult:
    p = req.payload
    A = np.asarray(p["start"], np.float64)
    B = np.asarray(p["goal"], np.float64)
    lim = _limits(req, p.get("speed_mps"))
    planner = str(p.get("planner", "safe_transit"))
    grid = _grid25(world, float(p.get("alt_min_agl_m", 20.0))) if (planner == "astar25" or p.get("prefer_low")) else None
    tp = plan_transit(A, B, world, planner=planner, clearance_m=float(p.get("clearance_m", 5.0)),
                      alt_max_m=p.get("alt_max_m"), prefer_low=bool(p.get("prefer_low", False)),
                      layer_dz_m=float(p.get("layer_dz_m", 0.0)), alt_min_agl_m=float(p.get("alt_min_agl_m", 20.0)),
                      grid=grid)
    if not tp.ok and planner != "astar25" and not p.get("prefer_low"):
        grid = _grid25(world, float(p.get("alt_min_agl_m", 20.0)))
        if grid is not None:
            tp = plan_transit(A, B, world, planner="astar25", clearance_m=float(p.get("clearance_m", 5.0)),
                              alt_max_m=p.get("alt_max_m"), layer_dz_m=float(p.get("layer_dz_m", 0.0)), grid=grid)
    if not tp.ok:
        return _fail(req, tp.status, 125, tp.detail, tp.remedy, tp.stats)
    if p.get("check_goal") and world is not None:
        top = float(world.column_max_within(B[None, :2], float(p.get("r_col_m", 0.49)))[0])
        if B[2] < top + SM.END_CLEAR_M:
            return _fail(req, "goal_blocked", 102, "GOAL_IN_OBSTACLE", "目标点须不低于碰撞半径内柱顶 + 2 m",
                         {"top_m": round(top, 2)})
    r = SM.make_trajectory(tp.polyline, lim, world, r_col_m=float(p.get("r_col_m", 0.49)), zones=p.get("zones"),
                           check_input=False)
    if not r.ok:
        return _fail(req, "error", 125, r.detail or "NO_PATH", None, r.stats)
    key = traj_key(req.world_key[1], lim.to_json(), tp.polyline, {"yaw": p.get("yaw")})
    t_ms = (time.perf_counter() - t0) * 1000.0
    tr = _traj_dict(r, req.vehicle_ids[0] if req.vehicle_ids else "", lim, tp.planner, p.get("yaw") or {}, key, t_ms)
    tr["polyline"] = tp.polyline
    st = {"planner": tp.planner, "len_m": round(tr["len_m"], 2), "duration_s": round(tr["duration_s"], 3),
          "zmax_m": round(float(np.max(tp.polyline[:, 2])), 2), "stretch_ratio": tr["source"]["stretch_ratio"],
          "degraded": r.degraded} | {k: v for k, v in tp.stats.items() if k not in ("t_ms",)}
    status = "degraded" if r.degraded else "ok"
    return PlanResult(req.job_id, status, (tr,), None, st, None, None, result_digest((tr,), None))


def _do_follow_path(req: PlanRequest, world: Any, t0: float) -> PlanResult:
    p = req.payload
    W = np.asarray(p["waypoints"], np.float64)
    lim = _limits(req, p.get("speed_mps"))
    r = SM.make_trajectory(W, lim, world, r_col_m=float(p.get("r_col_m", 0.49)), zones=p.get("zones"),
                           check_input=bool(p.get("check_input", True)))
    if not r.ok:
        code = 102 if r.status == "infeasible" else 125
        return _fail(req, r.status, code, r.detail or "PATH_OBSTACLE", None, r.stats)
    key = traj_key(req.world_key[1], lim.to_json(), W, {"yaw": p.get("yaw")})
    tr = _traj_dict(r, req.vehicle_ids[0] if req.vehicle_ids else "", lim, "polyline", p.get("yaw") or {}, key,
                    (time.perf_counter() - t0) * 1000.0)
    tr["polyline"] = W
    st = {"planner": "polyline", "len_m": round(tr["len_m"], 2), "duration_s": round(tr["duration_s"], 3),
          "stretch_ratio": tr["source"]["stretch_ratio"], "degraded": r.degraded,
          "zmax_m": round(float(W[:, 2].max()), 2)}
    return PlanResult(req.job_id, "degraded" if r.degraded else "ok", (tr,), None, st, None, None,
                      result_digest((tr,), None))


def _item_trajs(draft: Any, vid: str, req: PlanRequest, world: Any, lim: SM.Limits, r_col: float, zones: Any,
                anchors: dict) -> tuple[list[dict], str | None]:
    """一个作业项草稿 → 轨迹列表（控制点 > 4096 时切分，切点处 p、v、a 连续）；orbit 为解析原语，无轨迹。"""
    t0 = time.perf_counter()
    if draft.primitive == "orbit" or (draft.polyline is None and draft.dense is None):
        return [], None
    if draft.group is not None:
        gid = draft.group["gid"]
        if gid not in anchors:
            P3 = np.asarray(draft.group["anchor"], np.float64)
            r = SM.make_trajectory(P3, lim, world, r_col_m=r_col, zones=zones, check_input=False)
            if not r.ok:
                return [], r.detail or "NO_PATH"
            key = traj_key(req.world_key[1], lim.to_json(), P3, {"gid": gid})
            anchors[gid] = _traj_dict(r, gid, lim, "generator:formation", {"mode": "path"}, key,
                                      (time.perf_counter() - t0) * 1000.0)
        a = anchors[gid]
        tr = dict(a)
        tr.update(kind="formation", vehicle_id=vid, group={k: v for k, v in draft.group.items() if k != "anchor"},
                  key=traj_key(req.world_key[1], lim.to_json(), np.asarray(draft.group["slot_off_flu"]), {"gid": gid,
                                                                                                          "a": a["key"]}))
        off = np.asarray(draft.group["slot_off_flu"], np.float64)
        psi0 = float(draft.group.get("psi0_rad", 0.0))
        c, s = math.cos(psi0), math.sin(psi0)
        rr = np.array([c * off[0] - s * off[1], s * off[0] + c * off[1], off[2]])
        tr["start"] = (np.asarray(a["start"]) + rr).tolist()
        tr["end"] = (np.asarray(a["end"]) + rr).tolist()
        smp = np.asarray(a["samples_1s"]).copy()
        smp[:, 1:4] += rr
        tr["samples_1s"] = smp
        tr["polyline"] = np.asarray(draft.polyline, np.float64)
        return [tr], None
    if draft.dense is not None:
        r = SM.make_trajectory_dense(draft.dense, lim, world, r_col_m=r_col, zones=zones)
        geom = draft.dense
    else:
        r = SM.make_trajectory(draft.polyline, lim, world, r_col_m=r_col, zones=zones, check_input=True)
        geom = draft.polyline
    if not r.ok:
        return [], r.detail or "PATH_OBSTACLE"
    parts = BS.split_ctrl(r.Q)
    out = []
    for k, Qk in enumerate(parts):
        rk = SM.TrajResult(r.status, Qk, r.ts_s, r.stretch_ratio, r.degraded, None, dict(r.stats))
        rk.stats["len_m"] = float(np.linalg.norm(np.diff(BS.eval_bspline(Qk, r.ts_s, np.arange(0, BS.duration(Qk, r.ts_s), 0.5)),
                                                         axis=0), axis=1).sum()) if len(parts) > 1 else r.stats.get("len_m", 0.0)
        key = traj_key(req.world_key[1], lim.to_json(), geom, {"part": k, "yaw": draft.yaw})
        tr = _traj_dict(rk, vid, lim, "generator", draft.yaw, key, (time.perf_counter() - t0) * 1000.0)
        tr["polyline"] = BS.sample_adaptive(Qk, r.ts_s, max_pts=1000)[:, :3]
        out.append(tr)
    return out, None


def _do_generator(req: PlanRequest, world: Any, t0: float, preview: bool = False) -> PlanResult:
    from awr.sim.mission.generators import GenContext, GenError, VehicleCtx, run_generator

    p = req.payload
    vehicles = [VehicleCtx(v["vehicle_id"], np.asarray(v["home_enu_m"], np.float64), np.asarray(v.get("pos_enu_m",
                           v["home_enu_m"]), np.float64), float(v.get("v_limit_mps", 12.0)), float(v.get("cruise_mps", 5.0)),
                           float(v.get("r_col_m", 0.49)), yawrate_max_rad_s=float(v.get("yawrate_max_rad_s", math.inf)))
                for v in p["vehicles"]]
    ctx = GenContext(world, vehicles, dict(p.get("constraints") or {}), p.get("zones"), dict(p.get("camera") or
                     {"hfov_deg": 60.0, "vfov_deg": 42.1}), str(p.get("mission_id", "")))
    try:
        out = run_generator(str(p["generator"]), dict(p.get("params") or {}), ctx)
    except GenError as e:
        return _fail(req, "infeasible" if e.code == 125 else "error", e.code, e.detail, e.remedy)
    zones = p.get("zones")
    items: list[dict] = []
    trajs: list[dict] = []
    anchors: dict = {}
    by_v = {v.vehicle_id: v for v in vehicles}
    for vid, drafts in out.items.items():
        v = by_v[vid]
        seq = 0
        for d in drafts:
            lim = SM.limits_from(req.limits, d.speed_mps)
            lim = SM.Limits(**(lim.to_json() | {"v_max_mps": min(d.speed_mps, v.v_limit_mps)}))
            trs, err = _item_trajs(d, vid, req, world, lim, v.r_col_m, zones, anchors)
            if err is not None:
                code = 102 if err in ("PATH_OBSTACLE", "PATH_CROSSES_ZONE", "OUT_OF_BORDER", "GOAL_IN_OBSTACLE") else 125
                return _fail(req, "infeasible", code, err, "检查参数或抬高任务高度", {"vehicle_id": vid, "seq": seq})
            base = {"vehicle_id": vid, "kind": d.kind, "primitive": d.primitive, "speed_mps": round(d.speed_mps, 4),
                    "yaw": d.yaw, "gimbal": d.gimbal, "actions": d.actions, "meta": _jsonable(d.meta)}
            if d.primitive == "orbit":
                items.append(base | {"seq": seq, "geometry": {"orbit": _jsonable(d.orbit)}, "traj_key": None,
                                     "est": {"len_m": float(2 * math.pi * d.orbit["radius_m"] * float(d.orbit.get("turns") or 0)),
                                             "duration_s": float(2 * math.pi * d.orbit["radius_m"] * float(d.orbit.get("turns") or 0)
                                                                 / max(d.speed_mps, 0.1)), "photos": 0},
                                     "start": d.start.tolist(), "end": d.end.tolist()})
                seq += 1
                continue
            for tr in trs:
                photos = _photos(d.actions, tr)
                items.append(base | {"seq": seq, "geometry": {"polyline_enu_m": np.asarray(tr["polyline"]).round(3).tolist()},
                                     "traj_key": tr["key"], "est": {"len_m": round(tr["len_m"], 2),
                                                                    "duration_s": round(tr["duration_s"], 3),
                                                                    "photos": photos},
                                     "start": tr["start"], "end": tr["end"], "group": tr.get("group")})
                trajs.append(tr)
                seq += 1
    extra = {"region": out.region, "formation": _jsonable(out.formation), "profiles": out.profiles,
             "stats": _jsonable(out.stats), "warnings": sorted(set(out.warnings))}
    if preview:
        extra["paths"] = []
        for tr in trajs:
            S = BS.sample_adaptive(tr["ctrl_pts"], tr["ts_s"], max_pts=MAX_PREVIEW_PTS)
            extra["paths"].append({"vehicle_id": tr["vehicle_id"], "polyline_enu_m": S[:, :3].round(2).tolist(),
                                   "length_m": round(tr["len_m"], 1), "duration_s": round(tr["duration_s"], 1)})
    st = {"planner": f"generator:{p['generator']}", "n_items": len(items), "n_traj": len(trajs)}
    return PlanResult(req.job_id, "ok", tuple(trajs), tuple(items), st, None, None,
                      result_digest(tuple(trajs), tuple(items), {"region": out.region}), 0, extra)


def _photos(actions: list[dict], tr: dict) -> int:
    for a in actions or []:
        if a.get("kind") == "camera.trigger":
            args = a.get("args") or {}
            if args.get("every_m"):
                return int(tr["len_m"] // max(float(args["every_m"]), 0.1)) + 1
            if args.get("every_s"):
                return int(tr["duration_s"] // max(float(args["every_s"]), 0.1)) + 1
    return 0


def _do_resume(req: PlanRequest, world: Any, t0: float) -> PlanResult:
    """续飞（K07）：当前位置 → p(τ_r) 的 safe_transit，加当前项剩余部分（从静止重新 TOPP）。"""
    p = req.payload
    Q = np.asarray(p["ctrl_pts"], np.float64)
    ts = float(p["ts_s"])
    tau = float(p["tau_s"])
    T = BS.duration(Q, ts)
    lim = _limits(req, p.get("speed_mps"))
    tq = np.arange(tau, T + 1e-9, 0.2)
    rem = BS.eval_bspline(Q, ts, tq)
    rem = SM.decimate(rem, 1.0)
    out: list[dict] = []
    goal = rem[0]
    tp = plan_transit(np.asarray(p["start"], np.float64), goal, world, clearance_m=float(p.get("clearance_m", 5.0)),
                      alt_max_m=p.get("alt_max_m"))
    if not tp.ok:
        return _fail(req, tp.status, 125, tp.detail, tp.remedy)
    rt = SM.make_trajectory(tp.polyline, lim, world, r_col_m=float(p.get("r_col_m", 0.49)), zones=p.get("zones"),
                            check_input=False)
    if not rt.ok:
        return _fail(req, "error", 125, rt.detail or "NO_PATH")
    out.append(_traj_dict(rt, req.vehicle_ids[0], lim, tp.planner, {"mode": "lookahead", "t_fwd_s": 1.0},
                          traj_key(req.world_key[1], lim.to_json(), tp.polyline, {"resume": "transit"}),
                          (time.perf_counter() - t0) * 1000.0))
    out[-1]["polyline"] = tp.polyline
    if len(rem) >= 2:
        rr = SM.make_trajectory_dense(rem, lim, world, r_col_m=float(p.get("r_col_m", 0.49)), zones=p.get("zones"),
                                      check=False)
        tr = _traj_dict(rr, req.vehicle_ids[0], lim, "resume", p.get("yaw") or {},
                        traj_key(req.world_key[1], lim.to_json(), rem, {"resume": tau}), (time.perf_counter() - t0) * 1000.0)
        tr["polyline"] = BS.sample_adaptive(rr.Q, rr.ts_s, max_pts=1000)[:, :3]
        out.append(tr)
    return PlanResult(req.job_id, "ok", tuple(out), None, {"planner": "resume", "n_traj": len(out)}, None, None,
                      result_digest(tuple(out), None))


def _jsonable(x: Any) -> Any:
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.floating, np.integer)):
        return x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def _test_hooks(req: PlanRequest) -> None:
    """混沌测试钩子（M10-AC-011；只在 `AWR_PLAN_TEST_HOOKS=1` 时生效）：`_test_crash` 令 worker 进程退出；
    `_test_crash_once` 为标记文件路径，文件不存在时创建并退出（第一次崩溃、重试成功）；`_test_sleep_s` 模拟慢作业。"""
    if os.environ.get("AWR_PLAN_TEST_HOOKS") != "1":
        return
    p = req.payload
    if p.get("_test_sleep_s"):
        time.sleep(float(p["_test_sleep_s"]))
    if p.get("_test_crash"):
        os._exit(9)
    mark = p.get("_test_crash_once")
    if mark and not Path(mark).exists():
        Path(mark).write_text("1")
        os._exit(9)


def _do_assemble(req: PlanRequest, world: Any, t0: float) -> PlanResult:
    """编队集结（FR-044；§6.4.3 LIFTING → ASSEMBLING）。

    LIFTING：各成员原地竖直升到队形高度（每成员一条 TOPP-lite 轨迹，phase = lift）；
    ASSEMBLING：CAPT（Hungarian 分配 + 全员共用五次 smoothstep 的同步直线插值，间距不足时三段式错层）进入槽位，
    全员轨迹共用同一时间参数（采样点即控制点，时间拉伸取全员最大值），B-spline 仍保持"同一 s(t)"的不相交性质
    （phase = capt）。两段之间由 engine 做一次全员同步。

    payload：`members`、`pos`（n×3 当前位置）、`slots`（n×3 槽位起点，第 j 行属于 `members[j]` 的原分配）、
    `z_form_m`、`v_mps`、`min_sep_m`、`layer_dz_m`、`vz_mps`、`rank`、`r_col_m`、`zones`。
    返回每成员 lift（高度差 > 0.5 m 时）与 capt 轨迹，`extra.assign`（成员 i → 槽位行号）。"""
    from awr.swarm.formation import capt_plan, capt_positions

    p = req.payload
    ids = [str(x) for x in p["members"]]
    P = np.asarray(p["pos"], np.float64).reshape(-1, 3)
    G = np.asarray(p["slots"], np.float64).reshape(-1, 3)
    n = len(ids)
    zf = float(p["z_form_m"])
    vz = float(p.get("vz_mps", 1.5))
    v = float(p.get("v_mps", 5.0))
    r_col = float(p.get("r_col_m", 0.49))
    U = np.c_[P[:, :2], np.full(n, zf)]
    iu = np.triu_indices(n, 1)
    d_start = float(np.linalg.norm(U[:, None, :2] - U[None, :, :2], axis=-1)[iu].min()) if n > 1 else math.inf
    d_goal = float(np.linalg.norm(G[:, None] - G[None], axis=-1)[iu].min()) if n > 1 else math.inf
    min_sep = min(float(p.get("min_sep_m", 10.0)), 0.95 * d_start, 0.95 * d_goal)   # 起终构型本身决定可达间距
    rank = np.asarray(p.get("rank") or list(range(n)), np.int64)
    try:
        plan = capt_plan(U, G, v, min_sep_m=min_sep, layer_dz_m=float(p.get("layer_dz_m", 4.0)), vz_mps=vz, rank=rank)
    except ValueError as e:
        return _fail(req, "infeasible", 125, "FORMATION_INFEASIBLE", str(e))
    if not plan.feasible:
        return _fail(req, "infeasible", 125, "FORMATION_INFEASIBLE", "增大集结间距或 layer_dz_m", plan.to_json())
    Gs = G[plan.assign]
    lim = _limits(req, v)
    trs: list[dict] = []
    # LIFTING：竖直段
    for i in range(n):
        if abs(zf - P[i, 2]) <= 0.5:
            continue
        r = SM.make_trajectory(np.stack([P[i], U[i]]), lim, world, r_col_m=r_col, zones=p.get("zones"), check_input=True)
        if not r.ok:
            return _fail(req, "infeasible", 102, r.detail or "PATH_OBSTACLE", "集结竖直段不可行：退回分层转场",
                         {"member": ids[i]})
        key = traj_key(req.world_key[1], lim.to_json(), np.stack([P[i], U[i]]), {"lift": ids[i]})
        tr = _traj_dict(r, ids[i], lim, "lift", {"mode": "none"}, key, 0.0)
        tr["polyline"] = np.stack([P[i], U[i]])
        tr["phase"] = "lift"
        trs.append(tr)
    # ASSEMBLING：CAPT 同步插值
    ts = 0.5
    T = plan.total_s
    tk = np.arange(0.0, T + 1e-9, ts)
    if tk[-1] < T - 1e-9:
        tk = np.r_[tk, T]
    X = capt_positions(U, Gs, plan, tk)
    ratio = 1.0
    Qs = []
    for i in range(n):
        pk = X[:, i, :]
        Q = np.vstack([pk[:1], pk[:1], pk, pk[-1:], pk[-1:]])
        V = np.diff(Q, axis=0) / ts
        A = np.diff(Q, 2, axis=0) / ts ** 2
        rr = max(float(np.linalg.norm(V[:, :2], axis=1).max()) / lim.v_max_mps,
                 max(float(V[:, 2].max()), 0.0) / lim.vz_up_mps, max(float(-V[:, 2].min()), 0.0) / lim.vz_dn_mps,
                 math.sqrt(float(np.linalg.norm(A, axis=1).max()) / lim.a_max_mps2))
        ratio = max(ratio, rr)
        Qs.append(Q)
    ts_eff = ts * ratio                              # 全员共用同一时间拉伸
    for i, Q in enumerate(Qs):
        if world is not None:
            ok, info = SM.validate(Q, ts_eff, world, Q[0], Q[-1], r_col_m=r_col, zones=p.get("zones"))
            if not ok:
                return _fail(req, "infeasible", 102, info.get("reason", "PATH_OBSTACLE"), "集结直线穿越障碍：退回分层转场",
                             info | {"member": ids[i]})
        Tq = BS.duration(Q, ts_eff)
        tq = np.arange(0.0, Tq + 1e-9, 1.0)
        key = traj_key(req.world_key[1], lim.to_json(), Q, {"capt": ids[i], "ts": ts_eff})
        trs.append({"key": key, "vehicle_id": ids[i], "kind": "bspline", "ts_s": float(ts_eff), "ctrl_pts": Q,
                    "yaw": {"mode": "none"}, "limits": lim.to_json(),
                    "source": {"planner": "capt", "plan_ms": 0.0, "stretch_ratio": round(ratio, 4), "degraded": False},
                    "len_m": float(np.linalg.norm(np.diff(X[:, i, :], axis=0), axis=1).sum()),
                    "duration_s": float(Tq), "start": Q[0].tolist(), "end": Q[-1].tolist(),
                    "samples_1s": np.c_[tq, BS.eval_bspline(Q, ts_eff, tq), BS.eval_bspline(Q, ts_eff, tq, 1)],
                    "polyline": np.stack([U[i], Gs[i]]), "phase": "capt"})
    t_ms = (time.perf_counter() - t0) * 1000.0
    for tr in trs:
        tr["source"]["plan_ms"] = round(t_ms, 3)
    ex = {"members": ids, "assign": [int(a) for a in plan.assign], "T_s": round(T * ratio, 3), "capt": plan.to_json(),
          "min_sep_eff_m": round(min_sep, 3), "stretch_ratio": round(ratio, 4)}
    return PlanResult(req.job_id, "ok", tuple(trs), None, {"planner": "capt", "n": n, "T_s": ex["T_s"]}, None, None,
                      result_digest(tuple(trs), None, ex), 0, ex)


def _do_deconflict(req: PlanRequest, world: Any) -> PlanResult:
    """FR-056：payload `trajs=[{vehicle_id, ctrl_pts, ts_s, prio}]`、`clearance_m`、`r_col_m`、`zones`，可选 `delays_s`、
    `dzs_m`（候选集合，缺省为 deconflict.DELAYS_S、DZS_M）；dz 候选须通过 `validate`（整体平移后的 B-spline 净空复核）。"""
    from .deconflict import deconflict_mission, sample_traj

    p = req.payload
    trs = list(p["trajs"])
    Qs = [np.asarray(t["ctrl_pts"], np.float64) for t in trs]
    ts = [float(t["ts_s"]) for t in trs]
    S = [sample_traj(Q, h, 0.1) for Q, h in zip(Qs, ts, strict=True)]
    r_col = float(p.get("r_col_m", 0.49))

    def _valid(k: int, dz: float) -> bool:
        if world is None:
            return True
        Q = Qs[k] + np.array([0.0, 0.0, dz])
        ok, _ = SM.validate(Q, ts[k], world, Q[0], Q[-1], r_col_m=r_col, zones=p.get("zones"))
        return bool(ok)

    kw = {}
    if p.get("delays_s") is not None:      # 入场转场消解只用延迟（engine `_poll_entry`，ADR-070）
        kw["delays"] = tuple(float(x) for x in p["delays_s"])
    if p.get("dzs_m") is not None:
        kw["dzs"] = tuple(float(x) for x in p["dzs_m"])
    res = deconflict_mission(S, [float(t.get("prio", 0.0)) for t in trs], clearance_m=float(p.get("clearance_m", 10.0)),
                             validate=_valid, **kw)
    ids = [str(t.get("vehicle_id", i)) for i, t in enumerate(trs)]
    js = res.to_json(ids)
    status = "ok" if res.ok else "degraded"
    return PlanResult(req.job_id, status, (), None, js, None if res.ok else "DECONFLICT_PARTIAL", None,
                      result_digest((), None, js), 0, dict(js))


def worker_main(req: PlanRequest) -> PlanResult:
    t0 = time.perf_counter()
    _test_hooks(req)
    try:
        world = get_world(req.world_key)
    except PlanGridMismatch as e:
        return PlanResult(req.job_id, "error", (), None, {}, "PLAN_GRID_MISMATCH", str(e), "", 123)
    except Exception as e:  # GeoLoadError 等
        return PlanResult(req.job_id, "error", (), None, {}, "WORLD_LOAD_FAILED", str(e)[:200], "", 123)
    try:
        k = req.kind
        if k == "warm":
            SM.make_trajectory(np.array([[0, 0, 10.0], [10, 0, 10.0], [10, 10, 12.0]]), SM.Limits(), None)
            if req.payload.get("grid25"):
                _grid25(world, float(req.payload.get("alt_min_agl_m", 20.0)))
            res = PlanResult(req.job_id, "ok", (), None, {}, None, None, result_digest((), None))
        elif k == "path_valid":
            res = _do_path_valid(req, world)
        elif k in ("safe_transit", "astar25"):
            res = _do_transit(req, world, t0)
        elif k == "follow_path":
            res = _do_follow_path(req, world, t0)
        elif k == "formation" and req.payload.get("op") == "assemble":
            res = _do_assemble(req, world, t0)
        elif k in ("generator", "coverage", "formation"):
            res = _do_generator(req, world, t0)
        elif k == "preview":
            res = _do_generator(req, world, t0, preview=True)
        elif k == "resume":
            res = _do_resume(req, world, t0)
        elif k == "deconflict":
            res = _do_deconflict(req, world)
        else:
            res = _fail(req, "error", 300, f"UNKNOWN_KIND:{k}")
    except Exception as e:
        res = _fail(req, "error", 320, f"{type(e).__name__}: {str(e)[:200]}")
    res.stats["t_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
    return res
