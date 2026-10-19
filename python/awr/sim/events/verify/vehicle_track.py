"""车辆追踪事件类型 `vehicle_track`（M23 §15.3 第 3 行、§15.4；M23-FR-056、071、075、077；AWR-05 §5.2.8；D3-AC-33、D3-AC-62 车辆追踪条款）。

门 1 必备配置（`@requirement`；`E-TYPE` 611，`detail.rule` 区分条目，一次报出全部）：

- `target_count`：恰好 1 名 `roles` 含 `target` 的演员；`target_class`：目标为 `vehicle`；
- `distractor_count`：≥ 2 名 `distractor`（同时为目标者不计）；`distractor_class`：干扰者为 `vehicle`；
- `drive_required`：全部 `vehicle` 演员给出 `drive`（按车道图驾驶）。路线能否在车道图上解析由门 2 判定（`gen.lane_traffic`：
  `I-LANES`、`I-DISCONNECTED`）。外观相似规则（同类型或同色，`rules.vehicle_track`）由 `appearance.gate1_issues`（E05）检查。

门 3 类型验证器（`@validator`）读取磁带帧数据与 `gen.lane_traffic`（同一舞台上的生成结果，或以同一 plan 与车道图确定性重算）：

```text
一致    磁带中每辆车道图车辆（脚本与背景）的 x_mm、y_mm、state 与车流生成结果的毫米定点值逐帧相同，否则 S-TYPE why = tape_mismatch
审计    vehicle.audit（磁带位置、状态、路线与决策日志）：沿车道（横向 ≤ 0.02 m）、道路走廊、车距 ≥ 2 m、车身无穿越、
        冲突单元不同占、准入与让行、弯道限速；任一违例 S-TYPE why = audit（D3-AC-33）
关键事件 vehicle.key_events（磁带位置与状态，发动机标志取生成结果）中脚本车辆的 depart、turn{intersection}、stop、park；
        门 3 检测器 zone_enter、zone_exit 中脚本车辆的事件映射为 enter_zone、exit_zone（ref 为真值事件 id）；
        目标车辆的 target_reacquire：Re-ID 的视线 oracle（verify.reid，车身 5 点，遮挡物为静态网格与其他车辆）判定的遮挡
        持续 ≥ 3 s 后重新可见的首帧（无网格时不产出并告警 W-TYPE-ORACLE）
核对    关键事件与生成决策一致（"与磁带一致"）：每次出生对应一个 depart（首次移动帧，±100 ms）；每个泊车对应一个区间相同的
        park；每次途中停车对应一个起点 ±100 ms 内的 stop；按生成轨迹求出的每次 left、right、uturn 连接段驶入与驶出对应一个
        turn（两端 ±100 ms），且没有多余的 turn；否则 S-TYPE why = key_mismatch
```

输出（与 E06 至 E08 同一约定）：`gate3.extra` 的 `key_events`（M23-FR-077 形状 `{id, type, participants, interval_ms, declared,
ref?, params}`，id 为 `<type>-<n>`）、`validator_params`（进入 `truth_sha256`，§15.3 规则 1）、`report_map`（规则 4：`vehicle_event`
→ depart、turn、stop、park、enter_zone、exit_zone，由 report 的 `event` 字段选择；`identity_link` → target_reacquire）与
`vehicle_track`（目标、审计统计、生成摘要与核对结果，写入编译报告）。
"""

from __future__ import annotations

import numpy as np

from awr.sim.events.verify.base import Context
from awr.sim.events.verify.registry import requirement, type_issue, validator

__all__ = ["DEFAULTS", "EVENT_TYPE", "KEY_TYPES", "REPORT_MAP", "requirements", "validate_vehicle_track"]

EVENT_TYPE = "vehicle_track"
MIN_TARGETS, MIN_DISTRACTORS = 1, 2
KEY_TYPES = {"zone_enter": "enter_zone", "zone_exit": "exit_zone"}
REPORT_MAP = {"vehicle_event": ["depart", "turn", "stop", "park", "enter_zone", "exit_zone"], "identity_link": ["target_reacquire"]}
MAX_FAILS = 10


def _defaults() -> dict:
    from awr.sim.events import vehicle as V

    return {"audit": dict(V.AUDIT_DEFAULTS), "events": dict(V.EVENT_DEFAULTS), "match_ms": 100,
            "reacquire": {"min_dur_s": 3.0, "merge_gap_s": 1.0, "body_m": [[0.0, 0.5], [0.0, 1.0], [0.0, 1.4], [-0.6, 1.0], [0.6, 1.0]]}}


DEFAULTS = _defaults()


# ---------------------------------------------------------------------------------------------------- 门 1
@requirement(EVENT_TYPE)
def requirements(doc: dict, anchors: dict[str, dict]) -> list:
    """门 1 必备配置（M23-FR-071，§15.3 第 2 列）。"""
    acts = doc.get("actors", [])
    out = []
    tgt = [i for i, a in enumerate(acts) if "target" in (a.get("roles") or ())]
    dis = [i for i, a in enumerate(acts) if "distractor" in (a.get("roles") or ()) and i not in tgt]
    if len(tgt) != MIN_TARGETS:
        out.append(type_issue("/actors", f"车辆追踪事件须恰好 1 名目标（roles 含 target），实为 {len(tgt)} 名",
                              f"A vehicle tracking event requires exactly one target (roles containing target), found {len(tgt)}",
                              event_type=EVENT_TYPE, rule="target_count", found=len(tgt), required=MIN_TARGETS,
                              actors=[acts[i]["id"] for i in tgt]))
    out.extend(type_issue(f"/actors/{i}/class", f"车辆追踪事件的目标须为车辆，{acts[i]['id']} 为 {acts[i]['class']}",
                          f"The target of a vehicle tracking event must be a vehicle, {acts[i]['id']} is a {acts[i]['class']}",
                          event_type=EVENT_TYPE, rule="target_class", actor=acts[i]["id"]) for i in tgt if acts[i]["class"] != "vehicle")
    if len(dis) < MIN_DISTRACTORS:
        out.append(type_issue("/actors", f"车辆追踪事件须至少 {MIN_DISTRACTORS} 辆干扰车辆（roles 含 distractor），实为 {len(dis)} 辆",
                              f"A vehicle tracking event requires at least {MIN_DISTRACTORS} distractor vehicles (roles containing distractor), "
                              f"found {len(dis)}", event_type=EVENT_TYPE, rule="distractor_count", found=len(dis), required=MIN_DISTRACTORS))
    out.extend(type_issue(f"/actors/{i}/class", f"车辆追踪事件的干扰者须为车辆，{acts[i]['id']} 为 {acts[i]['class']}",
                          f"Distractors of a vehicle tracking event must be vehicles, {acts[i]['id']} is a {acts[i]['class']}",
                          event_type=EVENT_TYPE, rule="distractor_class", actor=acts[i]["id"]) for i in dis if acts[i]["class"] != "vehicle")
    out.extend(type_issue(f"/actors/{i}", f"车辆追踪事件的车辆 {a['id']} 须给出 drive（按车道图驾驶）",
                          f"Vehicle {a['id']} of a vehicle tracking event must give drive (lane-graph driving)",
                          event_type=EVENT_TYPE, rule="drive_required", actor=a["id"]) for i, a in enumerate(acts)
               if a["class"] == "vehicle" and a.get("drive") is None)
    return out


# ---------------------------------------------------------------------------------------------------- 门 3
def _fail(g, path: str, zh: str, en: str, why: str, **detail) -> None:
    from awr.sim.events.dsl import Issue

    iss = Issue("S-TYPE", 613 if g.strict else None, path, zh, en, {"event_type": EVENT_TYPE, "why": why, **detail})
    (g.errors if g.strict else g.warnings).append(iss)


def _warn(g, code: str, zh: str, en: str, **detail) -> None:
    from awr.sim.events.dsl import Issue

    g.warnings.append(Issue(code, None, "/", zh, en, {"event_type": EVENT_TYPE, **detail}))


def _tape_cols(ctx: Context, lt) -> dict[int, int]:
    """sim 列 → 演员表 idx（脚本车辆按 id，背景车辆按 `table_idx`）。"""
    cols = {c: ctx.idx_of[v] for v, c in lt.index.items() if v in ctx.idx_of and v in lt.scripted}
    cols.update({c: i for i, c in lt.table_idx.items()})
    return cols


def _tape_check(ctx: Context, lt, cols: dict[int, int]) -> list[dict]:
    """磁带与车流生成结果的逐帧对照（毫米定点位置与状态）：不一致的车辆与首个不一致帧。"""
    tp = lt.sim.tape()
    bad = []
    for c, i in sorted(cols.items()):
        f = ctx.frames[:, i]
        xm = np.floor(lt.sim.xy[:, c] * 1000.0 + 0.5).astype(np.int64)
        diff = (f["x_mm"].astype(np.int64) != xm[:, 0]) | (f["y_mm"].astype(np.int64) != xm[:, 1]) | (f["state"] != tp["state"][:, c])
        if diff.any():
            k = int(np.flatnonzero(diff)[0])
            bad.append({"actor": ctx.ids[i], "t_ms": ctx.t_ms(k), "n": int(diff.sum())})
    return bad


def _tape_view(ctx: Context, lt, cols: dict[int, int]) -> dict[str, np.ndarray]:
    """磁带量（sim 列次序）：位置取帧数据的毫米值，状态取帧数据，发动机标志取生成结果（磁带尚无 flags 侧车）。"""
    n = len(lt.sim.ids)
    xy = np.zeros((ctx.n_ticks, n, 2))
    st = np.zeros((ctx.n_ticks, n), np.uint8)
    for c, i in cols.items():
        xy[:, c, 0] = ctx.frames["x_mm"][:, i] / 1000.0
        xy[:, c, 1] = ctx.frames["y_mm"][:, i] / 1000.0
        st[:, c] = ctx.frames["state"][:, i]
    return {"xy": xy, "state": st, "flags": lt.sim.flags}


def _turns_from_sim(lt, g, net, c: int) -> dict[str, list[int]]:
    """生成轨迹（路线坐标 u）上 left、right、uturn 路口连接段的驶入与驶出帧时刻（与 `vehicle.key_events` 同一判据）。"""
    from awr.sim.events.vehicle import TICK_MS

    spec, u = lt.sim.specs[c], lt.sim.u[:, c]
    U, out = -spec.route.s_start, {}
    with np.errstate(invalid="ignore"):
        for k in spec.route.segments:
            s = g.segs[k]
            if s.kind == "connector" and s.props.get("turn") in ("left", "right", "uturn") and net.junction_of.get(k):
                ins, outs = np.flatnonzero(u > U + 1e-6), np.flatnonzero(u >= U + s.length - 1e-6)
                if len(ins) and len(outs) and outs[0] >= ins[0]:
                    out[k] = [int(ins[0]) * TICK_MS, int(outs[0]) * TICK_MS]
            U += s.length
    return out


def _cross_check(lt, g, net, ev: list[dict], tol: int, stop_min_ms: int) -> list[dict]:
    """关键事件与生成决策的核对（模块说明"核对"）：返回不一致项。"""
    from awr.sim.events.vehicle import TICK_MS

    lg, bad = lt.sim.log, []
    by: dict[tuple[str, str], list[dict]] = {}
    for e in ev:
        by.setdefault((e["type"], e["actor"]), []).append(e)
    for v in lt.scripted:
        c = lt.index[v]
        u = lt.sim.u[:, c]
        with np.errstate(invalid="ignore"):
            mv = np.flatnonzero(u[1:] > u[:-1])
        if len(mv):
            got = [e["interval_ms"][0] for e in by.get(("depart", v), [])]
            if len(got) != 1 or abs(got[0] - int(mv[0]) * TICK_MS) > tol:
                bad.append({"actor": v, "type": "depart", "want_ms": int(mv[0]) * TICK_MS, "got_ms": got})
        want = _turns_from_sim(lt, g, net, c)
        got_t = {e["params"]["connector"]: e["interval_ms"] for e in by.get(("turn", v), [])}
        if set(got_t) != set(want) or any(abs(got_t[k][0] - want[k][0]) > tol or abs(got_t[k][1] - want[k][1]) > tol for k in want):
            bad.append({"actor": v, "type": "turn", "want": want, "got": got_t})
    for p in lg["parks"]:
        if p["actor"] not in lt.scripted:
            continue
        got = [e["interval_ms"] for e in by.get(("park", p["actor"]), [])]
        if got != [[p["t_ms"], p["t_ms"]]]:
            bad.append({"actor": p["actor"], "type": "park", "want_ms": p["t_ms"], "got": got})
    seen: dict[str, int] = {}
    for s in lg["stops"]:                                       # 途中停车：第 si 次停车的实际停留 ≥ stop_min_ms 时须有 stop
        if s["actor"] not in lt.scripted:
            continue
        si = seen[s["actor"]] = seen.get(s["actor"], -1) + 1
        eff = lt.holds.get(s["actor"], [])
        hit = [e for e in by.get(("stop", s["actor"]), []) if abs(e["interval_ms"][0] - s["t_ms"]) <= tol]
        if not hit and si < len(eff) and eff[si] >= stop_min_ms:
            bad.append({"actor": s["actor"], "type": "stop", "want_ms": s["t_ms"], "got": [e["interval_ms"] for e in by.get(("stop", s["actor"]), [])]})
    return bad[:MAX_FAILS]


def _reacquire(ctx: Context, i: int, p: dict) -> tuple[list[dict], dict]:
    """目标车辆的遮挡与重新可见（Re-ID 视线 oracle，车身点见 `DEFAULTS.reacquire.body_m`；目标自身的包围盒不作遮挡物）。"""
    from awr.sim.events.verify import reid as R

    pr = {**R.DEFAULTS, **p}
    saved = ctx.cache.get("reid_vehicles")
    ctx.cache["reid_vehicles"] = np.array([a["idx"] for a in ctx.table if a["class"] == "vehicle" and a["idx"] != i], np.int64)
    try:
        los, info = R.los_visible(ctx, i, pr)
        occ = R.occlusions(ctx, i, los, pr)
    finally:
        if saved is None:
            ctx.cache.pop("reid_vehicles", None)
        else:
            ctx.cache["reid_vehicles"] = saved
    return occ, info


def validate_vehicle_track(ctx: Context, g, params: dict | None = None) -> dict:
    """车辆追踪类型验证（M23-FR-075、077；D3-AC-33、62）。参数为 `DEFAULTS`、`verify_event(params={"vehicle_track": ...})` 与 `params`
    依次逐段覆盖；结果写入 `g`（见模块说明），并返回 `g.extra["vehicle_track"]`。"""
    from awr.sim.events import gen
    from awr.sim.events import vehicle as V

    over = {**((getattr(ctx, "params", None) or {}).get(EVENT_TYPE) or {}), **(params or {})}
    p = {k: ({**v, **(over.get(k) or {})} if isinstance(v, dict) else over.get(k, v)) for k, v in DEFAULTS.items()}
    g.extra["validator_params"] = p
    g.extra["report_map"] = REPORT_MAP
    g.extra["key_events"] = []
    acts = ctx.plan["actors"]
    tgt = [a["id"] for a in acts if "target" in (a.get("roles") or ())]
    rep: dict = {"target": tgt[0] if len(tgt) == 1 else None, "distractors": [a["id"] for a in acts if "distractor" in (a.get("roles") or ())]}
    g.extra[EVENT_TYPE] = rep
    if ctx.stage is None or not hasattr(ctx.stage, "lane_graph"):
        _warn(g, "W-TYPE-LANES", "无事件舞台（车道图），车辆追踪的审计与关键事件未计算", "No event stage (lane graph): the vehicle tracking audit and key events "
              "were not computed")
        rep["available"] = False
        return rep
    lt = gen.lane_traffic(ctx.plan, ctx.stage)
    if lt is None or lt.sim is None:
        _fail(g, "/actors", "车辆追踪事件没有可用的车道图车流", "The vehicle tracking event has no usable lane-graph traffic", "lanes_unavailable",
              errors=[e.code for e in (lt.errors if lt is not None else [])])
        rep["available"] = False
        return rep
    lane_g, net = ctx.stage.lane_graph(), lt.net
    rep["available"] = True
    rep["navgraph_sha256"], rep["digest"] = lt.navgraph_sha256, lt.sim.digest()
    cols = _tape_cols(ctx, lt)
    mism = _tape_check(ctx, lt, cols)
    rep["tape_mismatch"] = mism[:MAX_FAILS]
    if mism:
        _fail(g, "/actors", f"磁带中 {len(mism)} 辆车道图车辆的位置或状态与车流生成结果不一致", f"Positions or states of {len(mism)} lane-graph vehicles in "
              "the tape differ from the generated traffic", "tape_mismatch", vehicles=mism[:MAX_FAILS])
    tape = _tape_view(ctx, lt, cols)
    specs = lt.sim.specs
    # ------------------------------------------------------------------ 审计（D3-AC-33）
    if not mism and p["audit"] == V.AUDIT_DEFAULTS and lt.audit is not None:
        au = lt.audit
    else:
        au = V.audit(lane_g, lt.sim.routes, tape, log=lt.sim.log, ids=lt.sim.ids, lengths=[s.length_m for s in specs],
                     widths=[s.width_m for s in specs], net=net, params=p["audit"])
    rep["audit"] = {"ok": au["ok"], "counts": au["counts"], "stats": au["stats"]}
    if not au["ok"]:
        _fail(g, "/actors", f"车道图车辆审计未通过：{', '.join(f'{k} {v}' for k, v in au['counts'].items() if v)}",
              f"Lane-graph vehicle audit failed: {', '.join(f'{k} {v}' for k, v in au['counts'].items() if v)}", "audit", counts=au["counts"],
              violations={k: v[:3] for k, v in au["violations"].items() if v})
    # ------------------------------------------------------------------ 关键事件
    ev = V.key_events(lane_g, lt.sim.routes, tape, ids=lt.sim.ids, lengths=[s.length_m for s in specs], net=net, params=p["events"])
    ev = [e for e in ev if e["actor"] in lt.scripted]
    bad = _cross_check(lt, lane_g, net, ev, int(p["match_ms"]), int(p["events"]["stop_min_ms"]))
    rep["key_mismatch"] = bad
    if bad:
        _fail(g, "/actors", f"{len(bad)} 项车辆关键事件与生成决策不一致", f"{len(bad)} vehicle key events disagree with the generator decisions", "key_mismatch",
              mismatches=bad)
    rows = [{"type": e["type"], "participants": [e["actor"]], "interval_ms": list(e["interval_ms"]), "declared": False,
             "params": {**e["params"], "at_xy": e["at_xy"]}} for e in ev]
    veh = set(lt.scripted)
    rows += [{"type": KEY_TYPES[e["type"]], "participants": list(e["participants"]), "interval_ms": list(e["interval_ms"]),
              "declared": bool(e["declared"]), "ref": e["id"], "params": dict(e["evidence"])}
             for e in g.events if e["type"] in KEY_TYPES and e["participants"][0] in veh]
    i = ctx.idx_of.get(rep["target"]) if rep["target"] is not None else None
    if i is not None and i < ctx.n_scripted and ctx.cls[i] == "vehicle":
        occ, info = _reacquire(ctx, i, p["reacquire"])
        rep["reacquire"] = {"occlusions": occ, "oracle": {k: v for k, v in info.items() if k != "rings"}}
        if not info["los_available"]:
            _warn(g, "W-TYPE-ORACLE", "无网格，目标车辆的 target_reacquire 未计算", "No mesh: target_reacquire of the target vehicle was not computed",
                  target=rep["target"])
        rows += [{"type": "target_reacquire", "participants": [rep["target"]], "interval_ms": [o["occluded_ms"][1], o["occluded_ms"][1]],
                  "declared": False, "params": {"occluded_ms": o["occluded_ms"], "duration_ms": o["duration_ms"], "causes": o["causes"]}}
                 for o in occ if o["valid"]]
    rows.sort(key=lambda r: (r["interval_ms"][0], r["type"], r["participants"]))
    seq: dict[str, int] = {}
    keys = []
    for r in rows:
        seq[r["type"]] = seq.get(r["type"], 0) + 1
        keys.append({"id": f"{r['type']}-{seq[r['type']]}", **r})
    g.extra["key_events"] = keys
    rep["n_key_events"] = dict(sorted(seq.items()))
    rep["log"] = {k: lt.sim.log[k] for k in ("gap_clamps", "stop_clamps", "safety_stops", "max_wait_ms")}
    rep["key_events_ok"] = not bad and not mism
    return rep


@validator(EVENT_TYPE)
def vehicle_track(ctx: Context, g) -> None:
    validate_vehicle_track(ctx, g)
