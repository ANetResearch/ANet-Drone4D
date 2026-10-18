"""一致性检查（M23 §6.9.4）：离网、贴地、到达、间距、容器。每项给出统计与最差值（写入真值 `consistency`）与最差的至多
`MAX_FAILS` 条违例；违例在 strict 下为 613 错误，lenient 下为告警（§6.9.2）。

- `S-OFFMESH`：脚本演员可见帧到所属 profile 可走面的水平距离 ≤ `tol_m`（0.1 m）。可走面对象提供 `offmesh_dist_batch(xy)` 时
  直接调用（Recast/Detour 实现，W09/W12），否则按 `nav.NavGrid` 计算到最近可走格的距离（可走格内为 0，搜索半径 2 格）；
- `S-ZGROUND`：全部演员可见帧 |z_tape − ground_z(x, y)| ≤ `tol_m`（2 cm），ground_z 以磁带 z 为层提示；未找到表面的帧只计数；
- `S-ARRIVAL`：脚本演员每个路点的实际到达时刻与计划时刻之差为 0（运动学排时，1 mm 对应的时间小于 1 ms）、到达位置定点化误差
  ≤ `tol_mm`（1 mm），且到达时刻落在 tick 上并可见时磁带位置与计划点之差 ≤ `tol_mm`；
- `S-OVERLAP`：脚本演员两两可见帧的水平间距 ≥ `min_m`（0.5 m），同乘与同一 zone 站位（两者均以该 zone 为路点且同在其内）
  除外；背景与脚本演员的间距只统计并告警 `W-BG-OVERLAP`（人群避让属 D3-AC-29，P1，以 ADR 移出 D3 封板范围），
  `bg_overlap_error = true` 时按 `S-OVERLAP` 报错；
- `S-CONTAINER`：状态 3 的演员 `container` 指向状态 1 或 2 的车辆（P1 的物体可由行人携带），且该帧 x、y、z 相同；非状态 3 的
  演员 `container` 为 255。
"""

from __future__ import annotations

import itertools
import math

import numpy as np

from awr.sim.events.anchors import point_in_polygon
from awr.sim.events.verify.base import NO_CONTAINER, VISIBLE, CheckResult, Context, finite_or_none, rnd
from awr.sim.events.verify.registry import check

MAX_FAILS = 10
SEARCH_CELLS = 2
CARRIERS = {"object": ("person", "vehicle")}           # 被携带类别 → 允许的容器类别；其余类别只能在车辆内


def _worst(rows: list[dict], key: str) -> list[dict]:
    return sorted(rows, key=lambda r: (-r[key], r.get("actor", ""), r.get("t_ms", 0)))[:MAX_FAILS]


def grid_offmesh(nav, xy: np.ndarray) -> np.ndarray:
    """NavGrid：点到最近可走格（主岛）的水平距离（m），可走格内为 0，搜索半径 `SEARCH_CELLS` 格外为 inf。"""
    from awr.sim.events.nav import CELL_M

    W = np.asarray(nav.walk, bool)
    fx, fy = (xy[:, 0] - nav.x0) / CELL_M, (xy[:, 1] - nav.y0) / CELL_M
    ix, iy = np.floor(fx).astype(np.int64), np.floor(fy).astype(np.int64)
    best = np.full(len(xy), np.inf)
    for dy, dx in itertools.product(range(-SEARCH_CELLS, SEARCH_CELLS + 1), repeat=2):
        cx, cy = ix + dx, iy + dy
        ok = (cx >= 0) & (cx < nav.nx) & (cy >= 0) & (cy < nav.ny)
        w = np.zeros(len(xy), bool)
        w[ok] = W[cy[ok], cx[ok]]
        ex = np.maximum(0.0, np.maximum(cx - fx, fx - (cx + 1))) * CELL_M        # 到格盒的距离（格内为 0）
        ey = np.maximum(0.0, np.maximum(cy - fy, fy - (cy + 1))) * CELL_M
        best = np.where(w, np.minimum(best, np.hypot(ex, ey)), best)
    return best


@check("S-OFFMESH", defaults={"tol_m": 0.1})
def offmesh(ctx: Context, p: dict) -> CheckResult:
    if ctx.stage is None:
        return CheckResult({"available": False})
    tol = float(p["tol_m"])
    rows, n, worst = [], 0, 0.0
    for i in ctx.scripted():
        prof = ctx.table[i].get("profile")
        nav = ctx.stage.nav.get(prof) if prof else None
        if nav is None:
            continue
        ks = np.flatnonzero(ctx.vis[:, i])
        if not len(ks):
            continue
        mm = np.stack([ctx.frames["x_mm"][ks, i], ctx.frames["y_mm"][ks, i]], 1)
        u, first = np.unique(mm, axis=0, return_index=True)
        xy = u.astype(np.float64) / 1000.0
        fn = getattr(nav, "offmesh_dist_batch", None)
        if fn is None and not hasattr(nav, "walk"):
            return CheckResult({"available": False, "why": f"nav object {type(nav).__name__} has no offmesh_dist_batch"})
        d = np.asarray(fn(xy) if fn is not None else grid_offmesh(nav, xy), np.float64)
        n += len(ks)
        worst = max(worst, float(d.max()))
        rows += [{"actor": ctx.ids[i], "t_ms": ctx.t_ms(ks[first[j]]), "dist_m": finite_or_none(d[j]) or 1e9} for j in np.flatnonzero(d > tol)]
    return CheckResult({"available": True, "tol_m": tol, "n_frames": n, "n_fail": len(rows), "worst_m": finite_or_none(worst)},
                       _worst(rows, "dist_m"))


@check("S-ZGROUND", defaults={"tol_m": 0.02})
def zground(ctx: Context, p: dict) -> CheckResult:
    if ctx.stage is None or getattr(ctx.stage, "ground", None) is None:
        return CheckResult({"available": False})
    tol = float(p["tol_m"])
    f = ctx.frames
    vis = np.isin(f["state"], VISIBLE)
    kk, ii = np.nonzero(vis)
    xy = np.stack([f["x_mm"][kk, ii], f["y_mm"][kk, ii]], 1).astype(np.float64) / 1000.0
    zt = f["z_mm"][kk, ii].astype(np.float64) / 1000.0
    zg, found = ctx.stage.ground.ground_z_batch(xy, zt)
    dz = np.where(found, np.abs(zt - zg), 0.0)
    bad = np.flatnonzero(found & (dz > tol + 1e-9))
    rows = [{"actor": ctx.ids[ii[j]], "t_ms": ctx.t_ms(kk[j]), "dz_m": rnd(dz[j], 4)} for j in bad[np.argsort(-dz[bad], kind="stable")][:MAX_FAILS]]
    return CheckResult({"available": True, "tol_m": tol, "n_frames": len(kk), "n_unresolved": int((~found).sum()), "n_fail": len(bad),
                        "worst_m": rnd(float(dz.max()) if len(dz) else 0.0, 4)}, rows)


@check("S-ARRIVAL", defaults={"tol_mm": 1.0})
def arrival(ctx: Context, p: dict) -> CheckResult:
    if ctx.arrivals is None:
        return CheckResult({"available": False})
    tol = float(p["tol_mm"])
    rows, worst_t, worst_mm, n = [], 0, 0.0, 0
    for r in ctx.arrivals:
        i = ctx.idx_of.get(r["actor"])
        if i is None:
            continue
        n += 1
        late = abs(int(r["late_ms"]))
        err = float(r["err_mm"])
        t = int(r["t_arrive_ms"])
        if t % ctx.tick_ms == 0 and 0 <= t // ctx.tick_ms < ctx.n_ticks and ctx.vis[t // ctx.tick_ms, i]:
            k = t // ctx.tick_ms
            err = max(err, math.hypot(float(ctx.frames["x_mm"][k, i]) - r["xy_plan_m"][0] * 1000.0,
                                      float(ctx.frames["y_mm"][k, i]) - r["xy_plan_m"][1] * 1000.0))
        worst_t, worst_mm = max(worst_t, late), max(worst_mm, err)
        if late > 0 or err > tol:
            rows.append({"actor": r["actor"], "waypoint": r["waypoint"], "t_ms": r["t_plan_ms"], "late_ms": late, "err_mm": rnd(err),
                         "sev": late + err})
    out = _worst(rows, "sev")
    for r in out:
        r.pop("sev")
    return CheckResult({"available": True, "tol_mm": tol, "n_waypoints": n, "n_fail": len(rows), "worst_late_ms": worst_t,
                        "worst_err_mm": rnd(worst_mm)}, out)


def _route_zones(ctx: Context, i: int) -> set[str]:
    return {w["at"] for w in ctx.routes[ctx.ids[i]] if ctx.anchors.get(w["at"], {}).get("polygon")}


@check("S-OVERLAP", defaults={"min_m": 0.5, "bg_overlap_error": False})
def overlap(ctx: Context, p: dict) -> CheckResult:
    lim = float(p["min_m"])
    rows, ss = [], math.inf
    zs = {i: _route_zones(ctx, i) for i in ctx.scripted()}
    for i, j in itertools.combinations(ctx.scripted(), 2):
        m = ctx.vis[:, i] & ctx.vis[:, j]
        if not m.any():
            continue
        d = np.hypot(*(ctx.xy[:, i] - ctx.xy[:, j]).T)
        ex = (ctx.container[:, i] == j) | (ctx.container[:, j] == i)
        for zid in sorted(zs[i] & zs[j]):
            P = np.asarray(ctx.anchors[zid]["polygon"], np.float64)
            ex |= point_in_polygon(P, ctx.xy[:, i, 0], ctx.xy[:, i, 1]) & point_in_polygon(P, ctx.xy[:, j, 0], ctx.xy[:, j, 1])
        mm = m & ~ex
        if mm.any():
            ss = min(ss, float(d[mm].min()))
        rows += [{"actor": ctx.ids[i], "other": ctx.ids[j], "t_ms": ctx.t_ms(k), "dist_m": rnd(d[k]), "sev": lim - float(d[k])}
                 for k in np.flatnonzero(mm & (d < lim))]
    # 背景与脚本演员（按 tick 分块，块内距离张量约 ≤ 32 MB）
    f = ctx.frames
    n_s, n_b = ctx.n_scripted, f.shape[1] - ctx.n_scripted
    bs, n_bg_fail, bg_rows = math.inf, 0, []
    if n_b and n_s:
        step = max(1, (1 << 22) // (n_b * n_s))
        for k0 in range(0, len(f), step):
            B = f[k0:k0 + step, n_s:]
            vb = np.isin(B["state"], VISIBLE)
            vs = ctx.vis[k0:k0 + step]
            if not (vb.any() and vs.any()):
                continue
            bxy = np.stack([B["x_mm"], B["y_mm"]], -1).astype(np.float64) / 1000.0
            d = np.linalg.norm(bxy[:, :, None] - ctx.xy[k0:k0 + step, None], axis=-1)
            m = vb[:, :, None] & vs[:, None]
            if not m.any():
                continue
            bs = min(bs, float(d[m].min()))
            hit = m & (d < lim)
            n_bg_fail += int(hit.sum())
            for kk, b, s in zip(*np.nonzero(hit), strict=True):
                if len(bg_rows) < 4 * MAX_FAILS:
                    bg_rows.append({"actor": ctx.ids[s], "other": ctx.ids[n_s + b], "t_ms": ctx.t_ms(k0 + kk), "dist_m": rnd(d[kk, b, s]),
                                    "sev": lim - float(d[kk, b, s])})
    stat = {"min_m": lim, "scripted_scripted_min_m": finite_or_none(ss), "bg_scripted_min_m": finite_or_none(bs), "n_fail": len(rows),
            "n_bg_fail_frames": n_bg_fail}
    out = _worst(rows, "sev")
    bg = _worst(bg_rows, "sev")
    for r in out + bg:
        r.pop("sev")
    if out or (bg and p["bg_overlap_error"]):
        return CheckResult(stat, out + (bg if p["bg_overlap_error"] else []))
    return CheckResult(stat, bg, level="warn", code="W-BG-OVERLAP")


@check("S-CONTAINER")
def container(ctx: Context, p: dict) -> CheckResult:
    f = ctx.frames
    n = f.shape[1]
    rows, n3 = [], 0
    for i in ctx.scripted():
        s, c = ctx.state[:, i], ctx.container[:, i].astype(np.int64)
        rows += [{"actor": ctx.ids[i], "t_ms": ctx.t_ms(k), "why": "container_without_state3", "container": int(c[k])}
                 for k in np.flatnonzero((s != 3) & (c != NO_CONTAINER))]
        k3 = np.flatnonzero(s == 3)
        n3 += len(k3)
        allowed = CARRIERS.get(ctx.cls[i], ("vehicle",))
        for k in k3:
            ck = int(c[k])
            if ck == NO_CONTAINER or ck >= n:
                why = "no_container"
            elif ctx.table[ck]["class"] not in allowed:
                why = "not_vehicle"
            elif int(f["state"][k, ck]) not in VISIBLE:
                why = "container_not_visible"
            elif any(int(f[fld][k, i]) != int(f[fld][k, ck]) for fld in ("x_mm", "y_mm", "z_mm")):
                why = "position_differs"
            else:
                continue
            rows.append({"actor": ctx.ids[i], "t_ms": ctx.t_ms(k), "why": why, "container": ck})
    return CheckResult({"n_frames": n3, "n_fail": len(rows)}, rows[:MAX_FAILS])
