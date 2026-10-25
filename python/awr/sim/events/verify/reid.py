"""Re-ID 遮挡事件类型 `reid_occlusion`（M23 §15.3 第 1 行；M23-FR-071、073、077；AWR-05 §5.2.8；D3-AC-62 Re-ID 条款）。

门 1 必备配置（`@requirement`；`E-TYPE` 611，`detail.why = type_requirement`，`detail.rule` 区分条目，一次报出全部）：

- `target_count`：恰好 1 名 `roles` 含 `target` 的演员；`target_class`：目标为 `person`；
- `distractor_count`：≥ 3 名 `distractor`（同时为目标者不计）；`distractor_class`：干扰者为 `person`；
- `occlusion_facilities`：目标路线经过 ≥ 2 处遮挡设施：建筑出入口对（`enter_building` 紧接 `exit_building`）、上下车（`board`
  之后有对同一车辆的 `alight`）、树冠或廊下（首路点以外的路点 `at` 或 `via` 引用 category ∈ `COVER_CATEGORIES` 的锚点，连续引用
  同一锚点计 1 处）。门 1 只按作者路线静态计数，遮挡是否成立、是否持续 ≥ 5 s 由门 3 判定。

外观引用、属性距离 ≥ 2 与共享属性 ≥ 1 由 `appearance.gate1_issues`（E05，`E-APPEARANCE`）检查，此处不重复报告。

门 3 类型验证器（`@validator`）以参考相机可观测性 oracle 的视线部分（§6.10.3 的 LOS：参考视点与身体点、静态网格与磁带车辆包围盒）
逐帧判定目标是否可见：

```text
vis_los[k] = ∃ v ∈ V(k), b ∈ B(k)：线段 b → v 不与静态网格（raycast_batch）及磁带车辆包围盒相交
  V(k)：世界对齐的 grid_m 网格 × alts_m（相对目标地面 z）中水平距离 ≤ stage_m 的视点（事件舞台为参与者包围盒外扩 300 m）
  B(k)：离地 0.3、0.9、1.6 m 与 1.0 m 处横向 ±0.2 m（垂直于目标航向）；车辆为水平距离 ≤ vehicle_m 的可见车辆（状态 1、2）
  按 rings_m 由近到远分环求交，已可见的帧不再查外环（结果与一次查全部视点相同）
occluded[k] = state ∈ {3 在车内, 4 在建筑内} 或（state ∈ {1, 2} 且 not vis_los[k]）
visible[k]  = state ∈ {1, 2} 且 vis_los[k]
遮挡 = occluded 的连续段 [k0, k1]，且 k0 − 1 与 k1 + 1 均 visible（与缺席、离场相邻者不计）；可见间隙 < merge_gap_s 的相邻遮挡合并
有效遮挡：t(k1 + 1) − t(k0) ≥ min_dur_s；每处有效遮挡在重新可见的首帧 t(k1 + 1) 产出关键事件 target_reacquire（参与者为目标）
有效遮挡 < min_occlusions → S-TYPE（strict 613，lenient 告警，detail.why = occlusion_count）
```

另把门 3 检测器产出的目标 `enter_building`、`exit_building`、`board_vehicle`、`alight_vehicle` 列为关键事件 `enter_building`、
`exit_building`、`board`、`alight`（`ref` 为真值事件 id，`declared` 随该事件）。输出：`gate3.extra` 的 `key_events`（M23-FR-077
形状 `{id, type, participants, interval_ms, declared?, ref?, params}`）、`validator_params`（进入 `truth_sha256`，§15.3 规则 1）、
`report_map`（AWR-05 §5.2.8 规则 5）与 `reid_occlusion`（逐处遮挡与 oracle 统计）；前三项经 `Gate3.truth_kwargs` 写入
`event_truth.json` 的同名字段。
无网格时视线部分不可用，只按磁带状态判定并告警 `W-TYPE-ORACLE`。
"""

from __future__ import annotations

import itertools
import math
import time

import numpy as np

from awr.sim.events.verify.base import VISIBLE, Context, rnd, runs
from awr.sim.events.verify.registry import requirement, type_issue, validator

__all__ = ["COVER_CATEGORIES", "DEFAULTS", "EVENT_TYPE", "KEY_TYPES", "REPORT_MAP", "body_points", "facilities", "los_visible",
           "occlusions", "requirements", "validate_reid", "viewpoints"]

EVENT_TYPE = "reid_occlusion"
COVER_CATEGORIES = frozenset({"poi:canopy", "poi:tree_canopy", "poi:arcade", "poi:colonnade", "poi:covered_walkway", "poi:underpass"})
INSIDE = (3, 4)                                        # 在车内、在建筑内：全部参考视点不可见
MIN_TARGETS, MIN_DISTRACTORS, MIN_FACILITIES = 1, 3, 2
RAY_CHUNK = 1 << 20
DEFAULTS = {"min_occlusions": 2, "min_dur_s": 5.0, "merge_gap_s": 1.0, "grid_m": 10.0, "alts_m": [40.0, 70.0, 100.0], "stage_m": 300.0,
            "rings_m": [20.0, 60.0, 150.0], "body_m": [[0.0, 0.3], [0.0, 0.9], [0.0, 1.6], [-0.2, 1.0], [0.2, 1.0]], "vehicle_m": 15.0,
            "eps_m": 0.05}
KEY_TYPES = {"enter_building": "enter_building", "exit_building": "exit_building", "board_vehicle": "board", "alight_vehicle": "alight"}
REPORT_MAP = {"reacquire": ["target_reacquire"], "identity_link": ["target_reacquire"], "enter_building": ["enter_building"],
              "exit_building": ["exit_building"], "board": ["board"], "alight": ["alight"]}


# ---------------------------------------------------------------------------------------------------- 门 1
def facilities(route: list[dict], anchors: dict[str, dict]) -> list[dict]:
    """作者路线上的遮挡设施（静态计数，见模块说明）：`[{kind: building | vehicle | cover, waypoint, anchor | vehicle}]`。"""
    out: list[dict] = []
    boarded: tuple[int, str | None] | None = None
    last_cover: str | None = None
    for j, w in enumerate(route):
        do = w.get("do", "pass")
        if do == "enter_building" and j + 1 < len(route) and route[j + 1].get("do") == "exit_building":
            out.append({"kind": "building", "waypoint": j, "anchor": w["at"]})
        if do == "board":
            boarded = (j, w.get("target"))
        elif do == "alight" and boarded is not None and boarded[1] == w.get("target"):
            out.append({"kind": "vehicle", "waypoint": boarded[0], "vehicle": boarded[1]})
            boarded = None
        if j == 0:
            continue
        for ref in [w["at"], *w.get("via", [])]:
            if (anchors.get(ref) or {}).get("category") not in COVER_CATEGORIES:
                last_cover = None
                continue
            if ref != last_cover:
                out.append({"kind": "cover", "waypoint": j, "anchor": ref})
            last_cover = ref
    return out


@requirement(EVENT_TYPE)
def requirements(doc: dict, anchors: dict[str, dict]) -> list:
    """门 1 必备配置（M23-FR-071，§15.3 第 2 列）。"""
    acts = doc.get("actors", [])
    tgt = [i for i, a in enumerate(acts) if "target" in (a.get("roles") or ())]
    dis = [i for i, a in enumerate(acts) if "distractor" in (a.get("roles") or ()) and "target" not in a["roles"]]
    out = []
    if len(tgt) != MIN_TARGETS:
        out.append(type_issue("/actors", f"Re-ID 事件须恰好 1 名目标（roles 含 target），实为 {len(tgt)} 名",
                              f"A Re-ID event requires exactly 1 target (roles containing target), found {len(tgt)}",
                              event_type=EVENT_TYPE, rule="target_count", found=len(tgt), required=MIN_TARGETS,
                              actors=[acts[i]["id"] for i in tgt]))
    out.extend(type_issue(f"/actors/{i}/class", f"Re-ID 事件的目标须为人员，{acts[i]['id']} 为 {acts[i]['class']}",
                          f"The target of a Re-ID event must be a person, {acts[i]['id']} is a {acts[i]['class']}",
                          event_type=EVENT_TYPE, rule="target_class", actor=acts[i]["id"]) for i in tgt if acts[i]["class"] != "person")
    if len(dis) < MIN_DISTRACTORS:
        out.append(type_issue("/actors", f"Re-ID 事件须至少 3 名干扰者（roles 含 distractor），实为 {len(dis)} 名",
                              f"A Re-ID event requires at least 3 distractors (roles containing distractor), found {len(dis)}",
                              event_type=EVENT_TYPE, rule="distractor_count", found=len(dis), required=MIN_DISTRACTORS,
                              actors=[acts[i]["id"] for i in dis]))
    out.extend(type_issue(f"/actors/{i}/class", f"Re-ID 事件的干扰者须为人员，{acts[i]['id']} 为 {acts[i]['class']}",
                          f"Distractors of a Re-ID event must be persons, {acts[i]['id']} is a {acts[i]['class']}",
                          event_type=EVENT_TYPE, rule="distractor_class", actor=acts[i]["id"]) for i in dis if acts[i]["class"] != "person")
    for i in tgt:
        fac = facilities(acts[i]["route"], anchors)
        if len(fac) < MIN_FACILITIES:
            out.append(type_issue(f"/actors/{i}/route", f"目标 {acts[i]['id']} 的路线只经过 {len(fac)} 处遮挡设施，至少需要 2 处"
                                  "（建筑出入口对、上下车、树冠或廊下）",
                                  f"The route of target {acts[i]['id']} passes {len(fac)} occlusion facilities, at least 2 are required "
                                  "(building entry and exit, boarding and alighting, canopy or arcade)",
                                  event_type=EVENT_TYPE, rule="occlusion_facilities", actor=acts[i]["id"], found=len(fac),
                                  required=MIN_FACILITIES, facilities=fac))
    return out


# ---------------------------------------------------------------------------------------------------- 门 3：可观测性 oracle
def viewpoints(cx: float, cy: float, z0: float, r_lo: float, r_hi: float, grid_m: float, alts_m: list[float]) -> np.ndarray:
    """(m, 3) 参考视点：世界对齐 `grid_m` 网格中水平距离 ∈ (r_lo, r_hi] 的格点（r_lo < 0 时含 r_lo = 0 的中心格点），每个格点按
    `alts_m` 次序取高度 z0 + alt。次序：x、y 升序，同一格点高度升序（确定性）。"""
    g = float(grid_m)
    gx = np.arange(math.ceil((cx - r_hi) / g), math.floor((cx + r_hi) / g) + 1) * g
    gy = np.arange(math.ceil((cy - r_hi) / g), math.floor((cy + r_hi) / g) + 1) * g
    X, Y = np.meshgrid(gx, gy, indexing="ij")
    d = np.hypot(X - cx, Y - cy)
    m = (d <= r_hi) & (d > r_lo)
    xy = np.stack([X[m], Y[m]], 1)
    alts = np.asarray(alts_m, np.float64)
    out = np.empty((len(xy) * len(alts), 3))
    out[:, :2] = np.repeat(xy, len(alts), 0)
    out[:, 2] = z0 + np.tile(alts, len(xy))
    return out


def body_points(x: float, y: float, z: float, yaw_rad: float, body_m: list[list[float]]) -> np.ndarray:
    """(5, 3) 身体点：`body_m` 每行为（横向偏移，离地高度），横向为航向左侧法向（−sin ψ, cos ψ）。"""
    B = np.asarray(body_m, np.float64)
    return np.stack([x - B[:, 0] * math.sin(yaw_rad), y + B[:, 0] * math.cos(yaw_rad), z + B[:, 1]], 1)


def _box_hits(o: np.ndarray, d: np.ndarray, boxes: np.ndarray) -> np.ndarray:
    """(n,) bool：线段 o → o + d（参数 s ∈ [0, 1]）是否与任一定向包围盒相交（slab 法；起点在盒内计为相交）。
    `boxes` 为 (m, 7)：中心 x、y、z，半长 hl、hw、hh，航向 ψ。"""
    hit = np.zeros(len(o), bool)
    for cx, cy, cz, hl, hw, hh, psi in boxes:
        c, s = math.cos(psi), math.sin(psi)
        px, py, pz = o[:, 0] - cx, o[:, 1] - cy, o[:, 2] - cz
        lo = np.stack([c * px + s * py, -s * px + c * py, pz], 1)
        ld = np.stack([c * d[:, 0] + s * d[:, 1], -s * d[:, 0] + c * d[:, 1], d[:, 2]], 1)
        half = np.array([hl, hw, hh])
        with np.errstate(divide="ignore", invalid="ignore"):
            t1, t2 = (-half - lo) / ld, (half - lo) / ld
        par = ld == 0
        inside_slab = np.abs(lo) <= half
        tmin = np.where(par, np.where(inside_slab, -np.inf, np.inf), np.minimum(t1, t2))
        tmax = np.where(par, np.where(inside_slab, np.inf, -np.inf), np.maximum(t1, t2))
        tn, tf = tmin.max(1), tmax.min(1)
        hit |= (tn <= tf) & (tf >= 0.0) & (tn <= 1.0)
    return hit


def _vehicle_boxes(ctx: Context, k: int, i: int, p: dict) -> np.ndarray:
    """第 k 帧目标水平距离 ≤ vehicle_m 的可见车辆（脚本与背景）的定向包围盒 (m, 7)。"""
    veh = ctx.cache.get("reid_vehicles")
    if veh is None:
        veh = ctx.cache["reid_vehicles"] = np.array([a["idx"] for a in ctx.table if a["class"] == "vehicle"], np.int64)
    if not len(veh):
        return np.zeros((0, 7))
    r = ctx.frames[k, veh]
    x, y = r["x_mm"] / 1000.0, r["y_mm"] / 1000.0
    m = np.isin(r["state"], VISIBLE) & (np.hypot(x - ctx.xy[k, i, 0], y - ctx.xy[k, i, 1]) <= float(p["vehicle_m"]))
    out = []
    for j in np.flatnonzero(m):
        L, W, H = (v / 1000.0 for v in ctx.table[int(veh[j])]["size_mm"])
        out.append((x[j], y[j], r["z_mm"][j] / 1000.0 + H / 2, L / 2, W / 2, H / 2, math.radians(r["yaw_cdeg"][j] / 100.0)))
    return np.asarray(out, np.float64).reshape(-1, 7)


def _los_ring(ctx: Context, i: int, ks: np.ndarray, r_lo: float, r_hi: float, p: dict, info: dict) -> np.ndarray:
    """(len(ks),) bool：第 ks 帧目标在 (r_lo, r_hi] 环内的参考视点中是否至少有一条无遮挡视线。"""
    yaw = ctx.frames["yaw_cdeg"][:, i]
    org, dirs, dist, own, boxes = [], [], [], [], {}
    for n, k in enumerate(ks):
        x, y, z = float(ctx.xy[k, i, 0]), float(ctx.xy[k, i, 1]), float(ctx.z[k, i])
        V = viewpoints(x, y, z, r_lo, r_hi, p["grid_m"], p["alts_m"])
        if not len(V):
            continue
        B = body_points(x, y, z, math.radians(yaw[k] / 100.0), p["body_m"])
        o = np.repeat(B, len(V), 0)
        d = np.tile(V, (len(B), 1)) - o
        org.append(o)
        dirs.append(d)
        dist.append(np.linalg.norm(d, axis=1))
        own.append(np.full(len(o), n, np.int64))
        bx = _vehicle_boxes(ctx, int(k), i, p)
        if len(bx):
            boxes[n] = bx
        info["viewpoints"] = info.get("viewpoints", 0) + len(V)
    out = np.zeros(len(ks), bool)
    if not org:
        return out
    org_a, D, L, owner = np.concatenate(org), np.concatenate(dirs), np.concatenate(dist), np.concatenate(own)
    free = np.ones(len(org_a), bool)
    for a in range(0, len(org_a), RAY_CHUNK):
        sl = slice(a, a + RAY_CHUNK)
        t, _prim, _inst = ctx.mesh.raycast_batch(org_a[sl], D[sl], float(L[sl].max()))
        free[sl] = ~(np.asarray(t, np.float64) < L[sl] - float(p["eps_m"]))
    for n, bx in boxes.items():
        sel = np.flatnonzero(owner == n)
        free[sel] &= ~_box_hits(org_a[sel], D[sel], bx)
    info["rays"] = info.get("rays", 0) + len(org_a)
    np.logical_or.at(out, owner, free)
    return out


def los_visible(ctx: Context, i: int, p: dict) -> tuple[np.ndarray, dict]:
    """(n_ticks,) bool：目标 i 处于可见状态（1、2）且至少一个参考视点有无遮挡视线；无网格时可见状态的帧一律为真。"""
    t0 = time.perf_counter()
    present = np.isin(ctx.state[:, i], VISIBLE)
    info: dict = {"los_available": ctx.mesh is not None, "ticks_present": int(present.sum()), "rays": 0, "viewpoints": 0, "rings": []}
    if ctx.mesh is None:
        return present.copy(), info
    out = np.zeros(ctx.n_ticks, bool)
    pending = np.flatnonzero(present)
    edges = [-1.0, *[float(r) for r in p["rings_m"] if float(r) < float(p["stage_m"])], float(p["stage_m"])]
    for r_lo, r_hi in itertools.pairwise(edges):
        if not len(pending):
            break
        seen = _los_ring(ctx, i, pending, r_lo, r_hi, p, info)
        info["rings"].append({"r_m": [max(r_lo, 0.0), r_hi], "ticks": len(pending), "seen": int(seen.sum())})
        out[pending[seen]] = True
        pending = pending[~seen]
    info["ticks_blocked"] = len(pending)
    ctx.perf["reid_los_rays"] = ctx.perf.get("reid_los_rays", 0) + info["rays"]
    ctx.perf["reid_los_ms"] = ctx.perf.get("reid_los_ms", 0) + int((time.perf_counter() - t0) * 1000)
    return out, info


# ---------------------------------------------------------------------------------------------------- 门 3：遮挡与关键事件
def occlusions(ctx: Context, i: int, los: np.ndarray, p: dict) -> list[dict]:
    """目标 i 的遮挡段（模块说明的定义与合并规则），按时刻排序；`valid` 为持续 ≥ min_dur_s。"""
    st = ctx.state[:, i]
    present = np.isin(st, VISIBLE)
    occl = np.isin(st, INSIDE) | (present & ~los)
    vis = present & los
    n = ctx.n_ticks
    segs: list[list[int]] = []
    gap_ms = round(float(p["merge_gap_s"]) * 1000)
    for k0, k1 in runs(occl):
        if k0 == 0 or k1 + 1 >= n or not vis[k0 - 1] or not vis[k1 + 1]:
            continue
        if segs and (k0 - segs[-1][1] - 1) * ctx.tick_ms < gap_ms:
            segs[-1][1] = k1
        else:
            segs.append([k0, k1])
    need = round(float(p["min_dur_s"]) * 1000)
    out = []
    for k0, k1 in segs:
        s = st[k0:k1 + 1]
        n_los = int((present[k0:k1 + 1] & ~los[k0:k1 + 1]).sum())
        causes = [c for c, hit in (("building", (s == 4).any()), ("los", n_los > 0), ("vehicle", (s == 3).any())) if hit]
        cont = sorted({ctx.ids[int(c)] for c in ctx.container[k0:k1 + 1, i][s == 3] if int(c) < len(ctx.ids)})
        t0, t1 = ctx.t_ms(k0), ctx.t_ms(k1 + 1)
        out.append({"occluded_ms": [t0, t1], "duration_ms": t1 - t0, "valid": t1 - t0 >= need, "causes": causes, "containers": cont,
                    "ticks": {"inside": int(np.isin(s, INSIDE).sum()), "los": n_los},
                    "last_seen_xy": [rnd(ctx.xy[k0 - 1, i, 0]), rnd(ctx.xy[k0 - 1, i, 1])],
                    "reacquire_xy": [rnd(ctx.xy[k1 + 1, i, 0]), rnd(ctx.xy[k1 + 1, i, 1])]})
    return out


def _key_events(target: str, occ: list[dict], events: list[dict]) -> list[dict]:
    rows = []
    for o in occ:
        if not o["valid"]:
            continue
        t0, t1 = o["occluded_ms"]
        linked = [e["id"] for e in events if e["type"] in KEY_TYPES and e["participants"][0] == target and t0 <= e["interval_ms"][0] <= t1]
        rows.append({"type": "target_reacquire", "participants": [target], "interval_ms": [t1, t1],
                     "params": {"occluded_ms": [t0, t1], "duration_ms": o["duration_ms"], "causes": o["causes"], "containers": o["containers"],
                                "events": linked}})
    rows += [{"type": KEY_TYPES[e["type"]], "participants": list(e["participants"]), "interval_ms": list(e["interval_ms"]),
              "declared": bool(e["declared"]), "ref": e["id"], "params": dict(e["evidence"])}
             for e in events if e["type"] in KEY_TYPES and e["participants"][0] == target]
    rows.sort(key=lambda r: (r["interval_ms"][0], r["type"], r["participants"]))
    seq: dict[str, int] = {}
    for r in rows:
        seq[r["type"]] = seq.get(r["type"], 0) + 1
        r["id"] = f"{r['type']}-{seq[r['type']]}"
    return [{"id": r["id"], **{k: v for k, v in r.items() if k != "id"}} for r in rows]


def validate_reid(ctx: Context, g, params: dict | None = None) -> dict:
    """Re-ID 类型验证（M23-FR-073）。参数为 `DEFAULTS`、`verify_event(params={"reid_occlusion": ...})` 与 `params` 依次覆盖；
    结果写入 `g`（见模块说明），并返回 `g.extra["reid_occlusion"]`。"""
    from awr.sim.events.dsl import Issue

    p = {**DEFAULTS, **((getattr(ctx, "params", None) or {}).get(EVENT_TYPE) or {}), **(params or {})}
    g.extra["validator_params"] = p
    g.extra["report_map"] = REPORT_MAP
    acts = ctx.plan["actors"]
    tgt = [j for j, a in enumerate(acts) if "target" in (a.get("roles") or ())]
    i = ctx.idx_of.get(acts[tgt[0]]["id"]) if len(tgt) == 1 else None
    if i is None or i >= ctx.n_scripted or ctx.cls[i] != "person":
        iss = Issue("S-TYPE", 613 if g.strict else None, "/actors", "Re-ID 事件缺少唯一的人员目标，无法验证遮挡",
                    "The Re-ID event has no unique person target; occlusions cannot be verified",
                    {"event_type": EVENT_TYPE, "why": "target_missing", "targets": [acts[j]["id"] for j in tgt]})
        (g.errors if g.strict else g.warnings).append(iss)
        g.extra["key_events"] = []
        g.extra[EVENT_TYPE] = {"target": None, "occlusions": [], "n_valid": 0}
        return g.extra[EVENT_TYPE]
    target = ctx.ids[i]
    los, info = los_visible(ctx, i, p)
    occ = occlusions(ctx, i, los, p)
    n_valid = sum(1 for o in occ if o["valid"])
    g.extra["key_events"] = _key_events(target, occ, g.events)
    rep = {"target": target, "n_valid": n_valid, "n_short": len(occ) - n_valid, "occlusions": occ, "oracle": info}
    g.extra[EVENT_TYPE] = rep
    if not info["los_available"]:
        g.warnings.append(Issue("W-TYPE-ORACLE", None, "/", "无网格，Re-ID 遮挡只按磁带状态（在车内、在建筑内）判定",
                                "No mesh: Re-ID occlusions are judged from tape states (in vehicle, inside) only",
                                {"event_type": EVENT_TYPE, "target": target}))
    if n_valid < int(p["min_occlusions"]):
        path = f"/actors/{tgt[0]}/route"
        iss = Issue("S-TYPE", 613 if g.strict else None, path,
                    f"目标 {target} 只有 {n_valid} 处持续 ≥ {p['min_dur_s']:g} s 的遮挡，至少需要 {p['min_occlusions']} 处",
                    f"Target {target} has {n_valid} occlusions lasting >= {p['min_dur_s']:g} s, at least {p['min_occlusions']} are required",
                    {"event_type": EVENT_TYPE, "why": "occlusion_count", "target": target, "n_valid": n_valid,
                     "min_occlusions": int(p["min_occlusions"]), "min_dur_s": float(p["min_dur_s"]),
                     "occlusions_ms": [o["occluded_ms"] for o in occ]})
        (g.errors if g.strict else g.warnings).append(iss)
    return rep


@validator(EVENT_TYPE)
def reid_occlusion(ctx: Context, g) -> None:
    validate_reid(ctx, g)
