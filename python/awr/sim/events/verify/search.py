"""目标搜索事件类型 `target_search` 的门 1 必备配置与门 3 类型验证器（M23 §15.3 第 4 行、规则 1、2、4；M23-FR-071、076、077；
D3-AC-62 目标搜索条款）。DSL、抽样算法与简报见 `awr.sim.events.search`。

门 1（`@requirement`）：`search.gate1_issues`（`E-TYPE` 611，`detail.why = type_requirement`，`detail.rule` 区分条目）。

门 3（`@validator`）只读取磁带帧数据、锚点表与 `plan.search`：

```text
i        = 目标的演员表 idx；present[k] = state[k, i] ∈ {1 行进, 2 等待}
在场区间 = present 的连续段 [k0, k1] → [t(k0), t(k1)]（毫秒）
target_present  = 每个在场区间一个真值事件（参与者为目标，id target_present、target_present-2 …），写入 event_truth.json 的 events
target_window_k = 每个在场区间按 window_ms 切分（search.key_windows：K = max(1, ⌈L / W⌉)，末窗口可短于 W），全局编号 k = 1…
核对（strict 613、lenient 告警，S-TYPE，detail.why 区分）：
  sample_position  首个在场帧的位置与保留锚点 search_target（抽样位置）水平距离 ≤ tol_m
  sample_replay    舞台可用时以同一先验、任务区、可走面与种子重新抽样，位置与锚点逐毫米相同、抽样表 sha256 与 plan 相同
  static_motion    motion = static：在场帧相对抽样位置的最大水平位移 ≤ tol_m
  wander_radius    motion = wander：在场帧相对抽样位置的最大水平距离 ≤ wander_radius_m + wander_slack_m
  outside_area     在场帧全部位于任务区多边形内（外扩 tol_m）
  target_absent    目标从未在场
```

输出（与 E07 `reid.py` 同一约定）：`gate3.extra` 的 `key_events`（M23-FR-077 形状 `{id, type, participants, interval_ms, window_s,
declared, ref, params}`，`ref` 为所属 `target_present` 事件 id，经 `Gate3.truth_kwargs` 写入真值 `key_events`）、`validator_params`、`report_map`（`found` → `target_present`）与 `target_search`（在场区间、实际位置、窗口数与核对结果）；
验证器参数与抽样诊断同时写入 `detector_params.target_search`（进入 `truth_sha256`，§15.3 规则 1）。
"""

from __future__ import annotations

import numpy as np

from awr.sim.events import search as sr
from awr.sim.events.anchors import point_in_polygon
from awr.sim.events.verify.base import VISIBLE, Context, dist_to_polygon, rnd, runs
from awr.sim.events.verify.registry import requirement, validator

__all__ = ["DEFAULTS", "EVENT_TYPE", "REPORT_MAP", "requirements", "target_search", "validate_search"]

EVENT_TYPE = sr.EVENT_TYPE
DEFAULTS = {"tol_m": 0.001, "wander_slack_m": 1.0}
REPORT_MAP = {"found": ["target_present"]}


@requirement(EVENT_TYPE)
def requirements(doc: dict, anchors: dict[str, dict]) -> list:
    """门 1 必备配置（M23-FR-071，§15.3 第 2 列）。"""
    return sr.gate1_issues(doc, anchors)


def _fail(g, path: str, zh: str, en: str, why: str, **detail) -> None:
    from awr.sim.events.dsl import Issue

    iss = Issue("S-TYPE", 613 if g.strict else None, path, zh, en, {"event_type": EVENT_TYPE, "why": why, **detail})
    (g.errors if g.strict else g.warnings).append(iss)


def validate_search(ctx: Context, g, params: dict | None = None) -> dict:
    """目标搜索类型验证（M23-FR-076、077）。`params` 覆盖 `DEFAULTS`；结果写入 `g`（见模块说明），并返回 `g.extra["target_search"]`。"""
    p = {**DEFAULTS, **(params or {})}
    s = ctx.plan.get("search")
    g.extra["validator_params"] = p
    g.extra["report_map"] = REPORT_MAP
    g.extra["key_events"] = []
    i = ctx.idx_of.get((s or {}).get("target", ""))
    if not s or i is None or i >= ctx.n_scripted:
        _fail(g, "/search", "目标搜索事件缺少已物化的 search 段或目标演员，无法验证", "The target search event has no materialised search "
              "section or target actor", "search_missing", target=(s or {}).get("target"))
        g.detector_params[EVENT_TYPE] = p
        g.extra[EVENT_TYPE] = {"target": None, "present_ms": [], "n_windows": 0}
        return g.extra[EVENT_TYPE]
    target = ctx.ids[i]
    W = int(s["window_ms"])
    g.detector_params[EVENT_TYPE] = {**p, "window_ms": W, "sample": s.get("sample", {})}
    present = np.isin(ctx.state[:, i], VISIBLE)
    segs = runs(present)
    pres = [(ctx.t_ms(k0), ctx.t_ms(k1)) for k0, k1 in segs]
    an = ctx.anchors.get(sr.SEARCH_ANCHOR) or {}
    actual = [float(v) for v in an.get("xy", [np.nan, np.nan])]
    tol = float(p["tol_m"])
    checks: dict[str, dict] = {}
    rep = {"target": target, "actual_xy": [rnd(actual[0]), rnd(actual[1])], "present_ms": [list(x) for x in pres], "window_ms": W,
           "motion": s["motion"], "checks": checks}
    g.extra[EVENT_TYPE] = rep
    if not segs:
        _fail(g, "/search/target", f"搜索目标 {target} 从未在场", f"Search target {target} is never present", "target_absent", target=target)
        rep["n_windows"] = 0
        return rep
    # ------------------------------------------------------------------ 真值事件与关键事件
    rows, keys, n = [], [], 0
    for m, ((k0, _k1), (a, b)) in enumerate(zip(segs, pres, strict=True)):
        at = ctx.xy[k0, i]
        inc = bool(any(point_in_polygon(P, np.array([at[0]]), np.array([at[1]]))[0] for P in ctx.quality))
        rows.append({"id": "target_present" if m == 0 else f"target_present-{m + 1}", "type": "target_present", "participants": [target],
                     "interval_ms": [a, b], "declared": True, "salience": 1.0, "in_incomplete_region": inc, "evidence": {}})
        for w0, w1 in sr.key_windows((a, b), W):
            n += 1
            keys.append({"id": f"target_window_{n}", "type": "target_window", "participants": [target], "interval_ms": [w0, w1],
                         "window_s": W / 1000.0, "declared": False, "ref": rows[-1]["id"], "params": {"k": n}})
    g.events = sorted(g.events + rows, key=lambda r: (r["interval_ms"][0], r["type"], r["participants"], r["id"]))
    g.extra["key_events"] = keys
    rep["n_windows"] = n
    # ------------------------------------------------------------------ 核对
    xy = ctx.xy[present, i]
    d0 = float(np.hypot(*(ctx.xy[segs[0][0], i] - actual)))
    checks["sample_position"] = {"dist_m": rnd(d0, 4), "ok": d0 <= tol}
    if not d0 <= tol:
        _fail(g, "/search", f"目标首个在场位置与抽样位置相差 {d0:.3f} m", f"The first present position of the target is {d0:.3f} m from the "
              "sampled position", "sample_position", target=target, dist_m=rnd(d0, 4))
    dmax = float(np.hypot(xy[:, 0] - actual[0], xy[:, 1] - actual[1]).max())
    if s["motion"] == "static":
        checks["static_motion"] = {"max_disp_m": rnd(dmax, 4), "ok": dmax <= tol}
        if not dmax <= tol:
            _fail(g, "/search/motion", f"静止目标位移 {dmax:.3f} m", f"The static target moves {dmax:.3f} m", "static_motion", target=target,
                  max_disp_m=rnd(dmax, 4))
    else:
        lim = float(s["wander_radius_m"]) + float(p["wander_slack_m"])
        checks["wander_radius"] = {"max_dist_m": rnd(dmax, 3), "limit_m": lim, "ok": dmax <= lim}
        if not dmax <= lim:
            _fail(g, "/search/wander_radius_m", f"游走目标离开抽样位置 {dmax:.2f} m，超过 {lim:g} m", f"The wandering target is {dmax:.2f} m "
                  f"from the sampled position, above {lim:g} m", "wander_radius", target=target, max_dist_m=rnd(dmax, 3))
    P = np.asarray(s["area_polygon"], np.float64)
    out_d = float(dist_to_polygon(P, xy[:, 0], xy[:, 1]).max())
    checks["outside_area"] = {"max_out_m": rnd(out_d, 4), "ok": out_d <= tol}
    if not out_d <= tol:
        _fail(g, "/search/area_polygon", f"目标离开任务区 {out_d:.3f} m", f"The target leaves the search area by {out_d:.3f} m", "outside_area",
              target=target, max_out_m=rnd(out_d, 4))
    st = getattr(ctx.stage, "nav", None)
    smp_doc = s.get("sample") or {}
    if st is not None and smp_doc.get("profile") in st:
        smp = sr.Sampler(s["prior"], s["area_polygon"], st[smp_doc["profile"]], cell_m=float(smp_doc.get("cell_m", 1.0)))
        x, y = smp.sample(int(ctx.plan["seed"]), int(smp_doc.get("actor_no", i)))
        ok = (x, y) == (actual[0], actual[1]) and smp.table_sha256 == smp_doc.get("table_sha256")
        checks["sample_replay"] = {"available": True, "ok": ok, "table_sha256": smp.table_sha256}
        if not ok:
            _fail(g, "/search/prior", "按先验与事件种子重新抽样的位置与磁带不一致", "Re-sampling from the prior with the event seed does not "
                  "reproduce the tape position", "sample_replay", target=target, resampled_xy=[x, y],
                  table_sha256=[smp.table_sha256, smp_doc.get("table_sha256")])
    else:
        checks["sample_replay"] = {"available": False}
    return rep


@validator(EVENT_TYPE)
def target_search(ctx: Context, g) -> None:
    validate_search(ctx, g, (getattr(ctx, "params", None) or {}).get(EVENT_TYPE))
