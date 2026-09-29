"""`formation` 生成器（M10-FR-025、FR-041、FR-045；M10 §6.5.10、§6.5.11；r26 §3.4–§3.6）。

锚点路径（`anchor_path_enu_m`，2D，`turns` > 1 时按闭环重复）在恒定编队高度（`z_m` 或 `agl_m`，并抬升到锚点线与
±max|r_i| 两侧偏移线的 Height_map 以上）上生成锚点轨迹；成员槽位为 `formation_slots(shape, n, spacing)`（虚拟锚点，
偏移减去形心）。成员共用群组时钟（跟踪核 kind 3：`p* = pA + Rz(ψ_f)·off`，`v* = vA + ω×r`，`a* = aA + ω×(ω×r)`），
航向模式 filtered（二阶临界阻尼，τψ = 2 s）、path（对齐锚点航向）、fixed（世界系不旋转）。
可行性检查（FR-045）不通过时以 125 FORMATION_INFEASIBLE 拒绝并给出 remedy。
"""

from __future__ import annotations

import math

import numpy as np

from awr.swarm.formation import formation_feasible, formation_slots, path_curvature, rotate_z

from .common import GenContext, GenError, GenOutput, ItemDraft, min_safe_profile, resample_2d, z_from
from .corridor import offset_polyline

__all__ = ["anchor_polyline", "run"]


def anchor_polyline(params: dict, ctx: GenContext, r_max: float) -> tuple[np.ndarray, float]:
    try:
        P2 = np.asarray(params["anchor_path_enu_m"], np.float64)[:, :2]
    except (KeyError, TypeError, ValueError):
        raise GenError(110, "/params/anchor_path_enu_m") from None
    if len(P2) < 2:
        raise GenError(110, "/params/anchor_path_enu_m")
    turns = max(1, math.ceil(float(params.get("turns", 1) or 1)))
    closed = bool(np.allclose(P2[0], P2[-1]))
    if turns > 1 and closed:
        P2 = np.vstack([P2] + [P2[1:]] * (turns - 1))
    z = z_from(params, ctx.world, P2[0], default_agl=None if params.get("z_m") is not None else 60.0)
    w = ctx.world
    if w is not None:
        tops = [np.asarray(w.heightmap_top_along(P2[:-1], P2[1:], exact=True), np.float64)]
        if r_max > 0.5:
            for d in (r_max, -r_max):
                L = offset_polyline(P2, d)
                tops.append(np.asarray(w.heightmap_top_along(L[:-1], L[1:], exact=True), np.float64))
        top = float(max(t.max() for t in tops))
        z = max(z, top)
    return np.c_[P2, np.full(len(P2), z)], z


def run(params: dict, ctx: GenContext) -> GenOutput:
    members = [str(m) for m in (params.get("members") or [v.vehicle_id for v in ctx.vehicles])]
    by_id = {v.vehicle_id: v for v in ctx.vehicles}
    members = [m for m in members if m in by_id]
    n = len(members)
    if n < 2:
        raise GenError(110, "/params/members")
    shape = str(params.get("shape", "v"))
    spacing = float(params.get("spacing_m", 12.0))
    min_sep = float(ctx.constraints.get("min_sep_m", 10.0))
    if spacing < min_sep:
        raise GenError(110, "/params/spacing_m", f"spacing_m ≥ min_sep_m（{min_sep} m）")
    try:
        off = formation_slots(shape, n, spacing, float(params.get("half_angle_deg", 35.0)), params.get("cols"))
    except ValueError:
        raise GenError(110, "/params/shape") from None
    r_max = float(np.linalg.norm(off[:, :2], axis=1).max())
    P3, z = anchor_polyline(params, ctx, r_max)
    v0 = by_id[members[0]]
    speed = min(float(params.get("speed_mps") or v0.cruise_mps), v0.v_limit_mps)
    hm = str(params.get("heading_mode", "filtered"))
    mode = {"filtered": "filtered", "path": "aligned", "aligned": "aligned", "fixed": "world", "world": "world"}.get(hm, hm)
    kappa = path_curvature(resample_2d(P3[:, :2], 1.0))
    km = float(np.percentile(kappa, 99)) if len(kappa) else 0.0
    tau = float(params.get("tau_psi_s", 2.0))
    rep = formation_feasible(off, km, speed, v0.v_limit_mps, 3.0, mode, tau_psi_s=tau)
    if not rep.ok:
        raise GenError(125, "FORMATION_INFEASIBLE", rep.remedy)
    d0 = P3[1, :2] - P3[0, :2]
    psi0 = math.atan2(d0[1], d0[0]) if mode != "world" else 0.0
    gid = f"form:{ctx.mission_id}"
    out = GenOutput(items={}, formation={"gid": gid, "shape": shape, "spacing_m": spacing, "heading_mode": mode,
                                         "tau_psi_s": tau, "members": members, "slots_flu": off.round(4).tolist(),
                                         "z_m": round(z, 2), "feasibility": rep.to_json()})
    for i, m in enumerate(members):
        r = rotate_z(off[i], psi0)
        Pm = P3 + np.r_[r[:2], 0.0]
        item = ItemDraft("leg", "follow_path", polyline=Pm, speed_mps=speed, yaw={"mode": "path"},
                         group={"gid": gid, "index": i, "slot_off_flu": off[i].tolist(), "heading_mode": mode,
                                "tau_psi_s": tau, "psi0_rad": psi0, "anchor": P3},
                         meta={"formation": True, "slot": i})
        out.items[m] = [item]
        out.profiles[m] = min_safe_profile(Pm, ctx.world)
    out.stats = {"kappa_max": round(km, 5), "r_max_m": round(r_max, 3), "z_m": round(z, 2), "n": n}
    return out
