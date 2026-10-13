"""M20 的命令与查询处理（M20-FR-080、FR-082、FR-084；M20 §7.1、§7.2；17 §17.5 SB-47 至 SB-59、SB-68，§17.6.6 至 §17.6.8；
d2-WP-02-to-M20 第 1–4 条）。

命令（`ctl/sim-core/cmd`，CommandEngine 验签与席位之后于步顶调用 `fn(msg, apply_tick, ctx)`）：`nest/add|update|remove`、`task/create`、
`task/start|pause|resume|cancel`（`args.tid` 由网关放入）、`tasking/plan`。查询（`ctl/sim-core/tasking`，op 加前缀 `tasking/`）：
`tasking/nests`、`tasking/nest`、`tasking/tasks`、`tasking/task`、`tasking/preview`、`tasking/preview_get`、`tasking/sustainability`、
`tasking/snapshot`、`tasking/relay_stats`；M08 调用的 `nest/home`、`tasking/vehicle_work`。输入规模（AWR-04 §11.5）在此再校验一次（585）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from typing import Any

import numpy as np

from awr.tasking.model import TaskingError
from awr.tasking.plan_kinds import measure

__all__ = ["COMMANDS", "QUERIES", "TYPES", "command_handlers", "query_handlers", "validate_spec"]

TYPES = ("point_watch", "polyline_patrol", "area_patrol", "area_scan_gsd", "perimeter_patrol", "relay_watch", "area_guard")
COMMANDS = ("nest/add", "nest/update", "nest/remove", "task/create", "task/start", "task/pause", "task/resume", "task/cancel", "tasking/plan")
QUERIES = ("nest/home", "tasking/vehicle_work", "tasking/nests", "tasking/nest", "tasking/tasks", "tasking/task", "tasking/preview",
           "tasking/preview_get", "tasking/sustainability", "tasking/snapshot", "tasking/relay_stats")
_PARAM_KEYS = {
    "point_watch": {"point", "alt_m", "alt_ref", "dwell_s", "look", "loiter_radius_m"},
    "polyline_patrol": {"waypoints", "mode", "laps", "duration_s", "speed_mps", "alt_m", "sensor_facing", "auto_detour"},
    "area_patrol": {"polygon", "n_vehicles", "target_class", "level_req", "revisit_s", "duration_s", "theta_max_deg", "sidelap", "alt_m"},
    "area_scan_gsd": {"polygon", "gsd_cm", "sidelap", "frontlap", "theta_max_deg", "t_max_s", "h_cap_m", "clearance_m", "allow_multi_sortie",
                      "n_vehicles", "strategy"},
    "perimeter_patrol": {"polygon", "offset_m", "direction", "revisit_s", "n_vehicles", "alt_m"},
    "relay_watch": {"target", "level_req", "duration_s"},
    "area_guard": {"polygon", "target_class", "level_req", "revisit_s", "perimeter_offset_m"},
}
_COMMON_KEYS = {"priority", "start_at_s", "allowed_nests", "allowed_models", "on_detect", "allocator", "external_fallback"}


def validate_spec(spec: Any) -> dict:
    """任务请求的轻量校验（契约 `task.schema.json#/$defs/request` 的约束子集）与输入规模配额（585）。"""
    if not isinstance(spec, dict):
        raise TaskingError(110, "param", field="body")
    t = spec.get("type")
    if t not in TYPES:
        raise TaskingError(110, "param", field="type", allowed=list(TYPES))
    params = spec.get("params")
    if not isinstance(params, dict):
        raise TaskingError(110, "param", field="params")
    bad = sorted(set(params) - _PARAM_KEYS[t])
    if bad:
        raise TaskingError(110, "param", field=f"params.{bad[0]}", why_detail="UNKNOWN_FIELD")
    common = spec.get("common") or {}
    if not isinstance(common, dict) or set(common) - _COMMON_KEYS:
        raise TaskingError(110, "param", field="common")
    if common.get("priority") not in (None, "low", "normal", "high"):
        raise TaskingError(110, "param", field="common.priority")
    if common.get("on_detect") not in (None, "notify", "relay", "ignore"):
        raise TaskingError(110, "param", field="common.on_detect")
    alloc = common.get("allocator")
    if alloc not in (None, "greedy", "external", "anet_cnp", "llm_agent"):
        raise TaskingError(110, "param", field="common.allocator")
    if alloc in ("anet_cnp", "llm_agent"):
        raise TaskingError(584, "allocator_unavailable", allocator=alloc)
    label = spec.get("label")
    if label is not None and (not isinstance(label, str) or len(label) > 32):
        raise TaskingError(110, "param", field="label")
    poly = params.get("polygon")
    if t in ("area_patrol", "area_scan_gsd", "perimeter_patrol", "area_guard") and not (
            isinstance(poly, list) and 3 <= len(poly) <= 64 and all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in poly)):
        raise TaskingError(110, "param", field="params.polygon")
    if t == "polyline_patrol":
        w = params.get("waypoints")
        if not (isinstance(w, list) and 2 <= len(w) <= 64):
            raise TaskingError(110, "param", field="params.waypoints")
    if t == "point_watch":
        p = params.get("point")
        if not (isinstance(p, list) and 2 <= len(p) <= 3):
            raise TaskingError(110, "param", field="params.point")
    if t == "area_scan_gsd":
        g = params.get("gsd_cm", 3.0)
        if not isinstance(g, (int, float)) or g <= 0:
            raise TaskingError(110, "param", field="params.gsd_cm")
    if t == "relay_watch" and not isinstance(params.get("target"), str):
        raise TaskingError(110, "param", field="params.target")
    from awr.sim.planning.jobs import quota_check

    q = quota_check(measure({"spec": spec}))
    if q is not None:
        k, v = next(iter(q[2].items()))
        raise TaskingError(585, "quota", limit=v["limit"], value=v["value"], item=k)
    out = {"type": t, "label": label, "params": params, "common": common}
    return out


def _rej(e: TaskingError) -> dict:
    return {"status": "rejected", "code": e.code, "detail": e.detail}


def _q(code: int = 0, **kw: Any) -> dict:
    return {"v": 1, "code": int(code), **kw}


def _qerr(e: TaskingError) -> dict:
    return {"v": 1, "code": e.code, "detail": e.detail}


def _tid(msg: dict) -> str:
    a = msg.get("args") or {}
    return str(a.get("tid") or a.get("id") or "")


def command_handlers(rt: Any) -> dict:
    def wrap(fn):
        def h(msg: dict, apply_tick: int, ctx: Any = None) -> dict:
            rt.bind(getattr(ctx, "_S", rt.S) if ctx is not None else rt.S, ctx if ctx is not None else rt.ctx)
            try:
                return fn(msg.get("args") or {}, msg)
            except TaskingError as e:
                return _rej(e)
        h.__module__ = __name__
        return h

    def nest_add(a: dict, msg: dict) -> dict:
        body = a.get("nest") if isinstance(a.get("nest"), dict) else a
        nid, vids = rt.add_nest(dict(body))
        return {"status": "accepted", "result": {"id": nid, "vehicle_ids": vids}}

    def nest_update(a: dict, msg: dict) -> dict:
        cfg = rt.update_nest(str(a.get("id", "")), dict(a.get("patch") or {}))
        return {"status": "accepted", "result": {"id": cfg["id"], "nest": cfg}}

    def nest_remove(a: dict, msg: dict) -> dict:
        rt.remove_nest(str(a.get("id", "")))
        return {"status": "accepted", "result": {"id": str(a.get("id"))}}

    def task_create(a: dict, msg: dict) -> dict:
        spec = {k: a.get(k) for k in ("type", "label", "params", "common") if k in a}
        T = rt.create_task(spec, preview_id=a.get("preview_id"))
        out = {"status": "accepted", "result": {"tid": T.tid, "state": T.state}}
        if T.warnings:
            out["warnings"] = list(T.warnings)
        return out

    def ctl(op: str):
        def f(a: dict, msg: dict) -> dict:
            st = getattr(rt, op)(_tid(msg))
            return {"status": "accepted", "result": {"tid": _tid(msg), "state": st}}
        return f

    def plan(a: dict, msg: dict) -> dict:
        return external_plan(rt, a)

    return {k: wrap(v) for k, v in {"nest/add": nest_add, "nest/update": nest_update, "nest/remove": nest_remove, "task/create": task_create,
                                     "task/start": ctl("start"), "task/pause": ctl("pause"), "task/resume": ctl("resume"),
                                     "task/cancel": ctl("cancel"), "tasking/plan": plan}.items()}


def external_plan(rt: Any, a: dict) -> dict:
    """外部计划（M20-FR-082；17 §17.6.8）：`based_on_revision` 过期 105 `STALE_SNAPSHOT`；经 FeasibilityChecker 逐项检查，不可行 584
    `violations`；通过的工作项立即生效。"""
    from awr.tasking.allocator import ExternalAllocator

    rev = int(a.get("based_on_revision", -1))
    snap = getattr(rt, "last_snapshot", None)
    if snap is None or rev != int(snap.revision):
        raise TaskingError(105, "STALE_SNAPSHOT", revision=None if snap is None else int(snap.revision), based_on_revision=rev)
    cur = rt.snapshot()
    stale = {v.id for v in snap.vehicles if v.state in ("ready", "standby")} - {v.id for v in cur.vehicles if v.state in ("ready", "standby", "charging")}
    asg = [x for x in a.get("assignments") or [] if isinstance(x, dict)]
    if not asg:
        raise TaskingError(110, "param", field="assignments")
    if stale & {str(x.get("uav")) for x in asg}:
        raise TaskingError(105, "STALE_SNAPSHOT", vehicles=sorted(stale))
    verdicts, plan = ExternalAllocator().accept(cur, asg)
    bad = [v.to_payload() for v in verdicts if not v.ok]
    if bad:
        raise TaskingError(584, "violations", violations=bad)
    applied = rt.apply_plan(plan.to_payload(), external=True)
    pid = "pl-" + hashlib.sha256(json.dumps(asg, sort_keys=True).encode()).hexdigest()[:8].translate(str.maketrans("0189", "abcd"))
    for T in rt.tasks.values():
        if any(x["wid"].split(".")[0] == T.tid for x in applied):
            rt.ext_plans[T.tid] = {"plan_id": pid}
    return {"status": "accepted", "result": {"plan_id": pid, "applied": bool(applied), "assignments": applied}}


def query_handlers(rt: Any) -> dict:
    def wrap(fn):
        def h(m: dict, ctx: Any = None) -> dict:
            if ctx is not None:
                rt.bind(rt.S, ctx)
            try:
                return fn(m.get("args") or {})
            except TaskingError as e:
                return _qerr(e)
        h.__module__ = __name__
        return h

    def nest_home(a: dict) -> dict:
        rt.sync_nests()
        r = rt.nest_home(str(a.get("nest_id", "")), str(a.get("model_id", "")))
        return {"v": 1, **r}

    def vehicle_work(a: dict) -> dict:
        items = {}
        for uav, so in sorted(rt.sorties.items()):
            items[uav] = {"tid": so.tid, "wid": so.wid, "kind": None, "state": so.state.lower()}
            _T, it = rt.ex.item_of(so)
            if it is not None:
                items[uav]["kind"] = it.w.kind
        return _q(items=items)

    def nests(a: dict) -> dict:
        rt.sync_nests()
        st = rt.nest_status_all()
        out = []
        for nid, n in sorted(rt.nests.items()):
            c = n.config(rt.inventory(nid))
            c["status"] = st.get(nid)
            out.append(c)
        return _q(items=out)

    def nest(a: dict) -> dict:
        rt.sync_nests()
        nid = str(a.get("id", ""))
        n = rt.nests.get(nid)
        if n is None:
            raise TaskingError(305, "not_found", nest_id=nid)
        c = n.config(rt.inventory(nid))
        c["status"] = rt.nest_status_all().get(nid)
        return _q(item=c)

    def tasks(a: dict) -> dict:
        return _q(items=[rt.task_obj(T) for T in sorted(rt.tasks.values(), key=lambda x: x.tid)])

    def task(a: dict) -> dict:
        T = rt.tasks.get(str(a.get("tid", "")))
        if T is None:
            raise TaskingError(305, "not_found", tid=a.get("tid"))
        d = rt.task_obj(T, include_plan=bool(a.get("include_plan")))
        if T.last_error:
            d_err = dict(T.last_error)
            return _q(item=d, last_error=d_err, warnings=list(T.warnings))
        return _q(item=d, warnings=list(T.warnings))

    def preview(a: dict) -> dict:
        return dry_run(rt, a)

    def preview_get(a: dict) -> dict:
        pv = rt.previews.get(str(a.get("preview_id", "")))
        if pv is None:
            raise TaskingError(305, "not_found", preview_id=a.get("preview_id"))
        return _preview_reply(pv)

    def sustainability(a: dict) -> dict:
        T = rt.tasks.get(str(a.get("tid", "")))
        if T is None:
            raise TaskingError(305, "not_found", tid=a.get("tid"))
        h = int(a.get("horizon_h") or 168)
        h = min(max(h, 24), 168)
        return _q(item=sustain_sync(rt, T, h))

    def snapshot(a: dict) -> dict:
        return _q(item=rt.snapshot_wire())

    def relay_stats(a: dict) -> dict:
        T = rt.tasks.get(str(a.get("tid", "")))
        if T is None or T.relay is None:
            raise TaskingError(305, "not_found", tid=a.get("tid"))
        return _q(item=rt.relays.stats_wire(T))

    return {k: wrap(v) for k, v in {"nest/home": nest_home, "tasking/vehicle_work": vehicle_work, "tasking/nests": nests, "tasking/nest": nest,
                                     "tasking/tasks": tasks, "tasking/task": task, "tasking/preview": preview, "tasking/preview_get": preview_get,
                                     "tasking/sustainability": sustainability, "tasking/snapshot": snapshot,
                                     "tasking/relay_stats": relay_stats}.items()}


# ------------------------------------------------------------------------------------------------ dry-run（SB-53 dry_run、SB-68）
def dry_run(rt: Any, a: dict) -> dict:
    """预览：分解（含扫描）→ 分配 →（接力与值守）可持续性评估；实时经共享规划服务异步执行，未完成时回复 `pending`（REST 2.5 s 后 202）。"""
    from .runtime import spec_hash

    spec = validate_spec({k: a.get(k) for k in ("type", "label", "params", "common") if k in a})
    import base64

    n = next(rt._pv_seq)
    raw = hashlib.sha256(f"{spec_hash(spec)}:{n}:{rt.t_ns}".encode()).digest()
    pid = "pv-" + base64.b32encode(raw).decode().lower()[:8]
    pv = {"preview_id": pid, "hash": spec_hash(spec), "state": "pending", "spec": spec, "wall_until": time.monotonic() + 60.0,
          "t_valid": rt.t + 60.0 * max(1.0, float(getattr(rt.ctx, "rate", 1.0) or 1.0))}
    rt.previews[pid] = pv
    tid = "pv"
    from .runtime import STATIC_KO_TYPES

    snap = rt.snapshot(items=[], static_keepouts=spec["type"] in STATIC_KO_TYPES)
    payload = {"spec": spec, "snapshot": snap.to_payload(), "tid": tid, "now": rt.t, "target_classes": rt.target_classes()}
    rt.jobs.submit("tasking.scan", payload, lambda extra, pid=pid: _pv_decomposed(rt, pid, extra), key=f"pv-{pid}")
    rt.jobs.flush_inline()
    return _preview_reply(rt.previews[pid])


def _pv_decomposed(rt: Any, pid: str, extra: dict) -> None:
    pv = rt.previews.get(pid)
    if pv is None:
        return
    pv["decomp"] = extra
    if not extra.get("ok"):
        pv["state"] = "done"
        pv["infeasible"] = [{"code": int(extra.get("code") or 580), "why": str(extra.get("why")), "detail": extra.get("detail") or {},
                             "remedy": _remedy(int(extra.get("code") or 580), str(extra.get("why")))}]
        return
    from awr.tasking.model import WorkItem

    items = [WorkItem.from_payload(d) for d in extra.get("items") or []]
    if not items:
        pv["state"] = "done"
        pv["plan"] = {"assignments": [], "infeasible": []}
        _pv_relay_sustain(rt, pid)
        return
    snap = rt.snapshot(items=items)
    rt.jobs.submit("tasking.allocate", {"snapshot": snap.to_payload(), "allocator": "greedy", "budget_ms": 200},
                   lambda ex2, pid=pid: _pv_allocated(rt, pid, ex2), key=f"pva-{pid}")
    rt.jobs.flush_inline()


def _pv_allocated(rt: Any, pid: str, extra: dict) -> None:
    pv = rt.previews.get(pid)
    if pv is None:
        return
    pv["plan"] = extra.get("plan") if extra.get("ok") else {"assignments": [], "infeasible": [{"wid": "*", "why": extra.get("why")}]}
    pv["state"] = "done"
    _pv_relay_sustain(rt, pid)


def _pv_relay_sustain(rt: Any, pid: str) -> None:
    pv = rt.previews.get(pid)
    spec = pv["spec"]
    if spec["type"] in ("relay_watch", "area_guard"):
        with contextlib.suppress(Exception):
            pv["sustainability"] = sustain_preview(rt, spec, pv.get("decomp") or {})


def _remedy(code: int, why: str) -> list[dict]:
    txt = {"fleet_short": "增加同类机，或开启 allow_multi_sortie", "height": "放宽 GSD、提高 H_cap 或改用变焦范围更大的相机",
           "single_station_far_view": "改用条带扫描（strategy = strips）", "t_max": "增加架数或放宽 t_max_s", "keepout": "移动航点或开启 auto_detour",
           "area_empty": "检查区域与禁入体", "no_capability": "选择带所需传感器的机型", "quota": "缩小区域、减少顶点或放宽 GSD"}
    return [{"kind": why, "text_zh": txt.get(why, "调整任务参数后重试")}]


def _preview_reply(pv: dict) -> dict:
    if pv.get("state") != "done":
        return _q(state="pending", preview_id=pv["preview_id"])
    return _q(state="done", preview_id=pv["preview_id"], item=preview_wire(pv))


def preview_wire(pv: dict) -> dict:
    """预览 `awr.tasking.preview.v1`（`task.schema.json#/$defs/preview`；不可行时另附 `infeasible[]`，与 422 的 detail 同形）。"""
    from .runtime import kind_wire, wire_geometry

    d = pv.get("decomp") or {}
    pr = dict(d.get("preview") or {})
    plan = pv.get("plan") or {}
    by: dict[str, dict] = {}
    for x in plan.get("assignments") or []:
        a = by.setdefault(x["uav"], {"uav": x["uav"], "nest": x.get("nest"), "items": [], "eta_s": max(float(x.get("eta_s") or 0.0), 0.0),
                                     "energy_margin": float(x.get("margin") or 0.0)})
        kind = next((it.get("kind") for it in d.get("items") or [] if it.get("wid") == x["wid"]), "segment")
        a["items"].append({"wid": x["wid"], "kind": kind_wire(str(kind)), "ref": x["wid"]})
    assign = {x["wid"]: x["uav"] for x in plan.get("assignments") or []}
    geo = dict(d.get("geometry") or {})
    items_map = {it.get("wid"): it for it in d.get("items") or []}
    if geo.get("legs"):
        for k, leg in enumerate(geo["legs"]):
            wid = list(items_map)[k] if k < len(items_map) else None
            if wid in assign:
                leg["uav"] = assign[wid]
    out: dict[str, Any] = {"preview_id": pv["preview_id"], "valid_until_unix_ns": str(int((time.time() + max(pv["wall_until"] - time.monotonic(), 0)) * 1e9)),
                           "n_vehicles": int(len(by) or pr.get("n_vehicles") or 0), "assignments": [by[k] for k in sorted(by)],
                           "geometry": wire_geometry(geo, assign), "warnings": list(d.get("warnings") or [])}
    for k in ("makespan_s", "t_max_s"):
        if pr.get(k) is not None:
            out[k] = round(float(pr[k]), 1)
    if pr.get("why_n"):
        out["why_n"] = {"rule": pr["why_n"].get("rule", "makespan"), "n_minus_1_makespan_s": pr["why_n"].get("n_minus_1_makespan_s")}
    if pr.get("makespan_curve"):
        out["makespan_curve"] = [{"n": int(c["n"]), "makespan_s": c["makespan_s"]} for c in pr["makespan_curve"]]
    if pr.get("scan"):
        out["scan"] = {k: (round(float(v), 4) if isinstance(v, float) else v) for k, v in pr["scan"].items()}
    if pr.get("coverage"):
        cv = pr["coverage"]
        out["coverage"] = {"direct_pct": round(float(cv["direct_pct"]), 3), "visible_pct": round(float(cv["visible_pct"]), 3),
                           "fill_waypoints": int(cv["fill_waypoints"]), "obstacle_pct": round(float(cv["obstacle_pct"]), 3), "cells": int(cv["cells"])}
    if pv.get("sustainability") is not None:
        s = dict(pv["sustainability"])
        s.pop("per_position", None)
        out["sustainability"] = s
    for x in plan.get("infeasible") or []:
        out["warnings"].append(f"unassigned:{x.get('wid')}:{x.get('why')}")
    if pv.get("infeasible"):
        out["infeasible"] = pv["infeasible"]
    return out


# ------------------------------------------------------------------------------------------------ 可持续性（SB-56）
def _hours(rt: Any, horizon_h: int) -> list[dict]:
    """评估时段（M20 §6.10）：按照度档分段，档界取 M07 `next_band_change` 的 1 s 精度时刻（D2-FX-R2：此前取逐小时采样点的档，
    不可行时段的起止最多偏一小时，D2-AC-19 (c) 的"不可行总时长与预测偏差 ≤ 5%"无从保证）；没有日历时按小时等分。"""
    from .relay import band_segments

    env = getattr(rt.ctx, "env", None)
    cal0 = 0
    sky = getattr(env, "sky_sample", None)
    if callable(sky):
        with contextlib.suppress(Exception):
            cal0 = int(sky(rt.t_ns).t_cal_unix_ns or 0)
    base = cal0 if cal0 else rt.t_ns
    out = []
    for t0, t1, b in band_segments(rt, horizon_h * 3600.0):
        out.append({"t_from_unix_ns": str(int(base + round((t0 - rt.t) * 1e9))), "t_to_unix_ns": str(int(base + round((t1 - rt.t) * 1e9))),
                    "band": b})
    return out


def _band_feasible(rt: Any, r: Any) -> dict:
    _band, mor = rt.light()
    out = {}
    for b in ("day", "low", "night"):
        ws = rt.relays.windows(r, b, mor)
        good = [(g, w) for g, w, _why in ws if w]
        why = next((why for _g, w, why in ws if not w and why), "no_sensor")
        if good:
            d_lo = min(w[0][1] for _g, w in good)
            d_hi = max(w[-1][2] for _g, w in good)
            out[b] = {"ok": True, "los": True, "why": None, "d_lo_m": round(d_lo, 1), "d_hi_m": round(d_hi, 1), "models": sorted({g[0] for g, _w in good})}
        else:
            out[b] = {"ok": False, "why": why}
    return out


def _resources(rt: Any) -> tuple[dict, list[dict], dict]:
    models = {}
    vs = []
    for v in rt.vehicle_ids():
        nid = rt.nest_of(v)
        mid = rt.model_id(v)
        if not nid or not mid:
            continue
        m = rt.model(mid)
        if m is None:
            continue
        models[mid] = m
        vs.append({"id": v, "model_id": mid, "nest_id": nid})
    nests = {nid: dict(n.cfg, pos=list(n.pos3)) for nid, n in rt.nests.items()}
    return models, vs, nests


def sustain_sync(rt: Any, T: Any, horizon_h: int) -> dict:
    from awr.tasking.sustain import evaluate

    models, vs, nests = _resources(rt)
    hours = _hours(rt, horizon_h)
    if T.relay is not None:
        r = T.relay
        pos = r.station or rt.relays._track_pos(r)
        positions = [{"id": T.tid, "pos": list(pos) if pos is not None else [0.0, 0.0, 0.0], "role": "hover"}]
        bf = _band_feasible(rt, r)
    elif T.type in ("area_guard", "area_patrol", "perimeter_patrol"):
        positions = []
        for C in [T, *[rt.tasks[c] for c in T.children if c in rt.tasks]]:
            for it in C.items.values():
                if it.w.persistent:
                    positions.append({"id": it.w.wid, "pos": [float(x) for x in it.w.start], "role": "cruise"})
        bf = {b: {"ok": True, "los": True} for b in ("day", "low", "night")}
        for c in T.children:
            Cc = rt.tasks.get(c)
            if Cc is not None and Cc.relay is not None:
                pos = Cc.relay.station or rt.relays._track_pos(Cc.relay)
                positions.append({"id": c, "pos": list(pos) if pos is not None else [0.0, 0.0, 0.0], "role": "hover"})
    else:
        return {"verdict": "not_applicable", "horizon_s": float(horizon_h * 3600)}
    wind = max((rt.wind_mps(n.pos3) for n in rt.nests.values()), default=0.0)
    s = evaluate(positions=positions, hours=hours, band_feasible=bf, models=models, vehicles=vs, nests=nests, wind_mps=wind,
                 horizon_s=float(horizon_h * 3600))
    s.pop("per_position", None)
    return s


def sustain_preview(rt: Any, spec: dict, decomp: dict) -> dict:
    from awr.tasking.sustain import evaluate

    from .relay import RelayRT

    models, vs, nests = _resources(rt)
    hours = _hours(rt, 24)
    positions = []
    bf = {b: {"ok": True, "los": True} for b in ("day", "low", "night")}
    if spec["type"] == "relay_watch":
        tgt = str(spec["params"].get("target"))
        r = RelayRT("pv", tgt, str(spec["params"].get("level_req") or "R"))
        tp = rt.relays._track_pos(r)
        positions.append({"id": "relay", "pos": list(tp) if tp is not None else [0.0, 0.0, 0.0], "role": "hover"})
        bf = _band_feasible(rt, r)
    else:
        for it in decomp.get("items") or []:
            if it.get("persistent"):
                P = it.get("path")
                p0 = (np.asarray(P, float)[0].tolist() if P is not None and len(P) else it.get("pos")) or [0.0, 0.0, 0.0]
                positions.append({"id": it["wid"], "pos": p0, "role": "cruise"})
    return evaluate(positions=positions, hours=hours, band_feasible=bf, models=models, vehicles=vs, nests=nests, horizon_s=24 * 3600.0)

