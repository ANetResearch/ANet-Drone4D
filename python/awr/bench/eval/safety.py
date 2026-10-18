"""安全与合规（M27 §6.7.6）：机体球（r 0.5 m）与演员包围体（人员圆柱近似胶囊 r 0.4 m、h 1.8 m；车辆按尺寸 OBB）的最小距离，
P0a 以 10 Hz 真值轨迹计算，只计机体在空中（AIRBORNE、LANDING）的 tick；距离低于任务间距的连续区段计一次违规，≤ 0 记穿透
（`ranked_valid = false`，`actor_penetration`）。首次升至 5 m 以上之后、降落段之外离地高度 < 2 m 的空中区段计地面告警。
`crashes` 统计物理后端判定的终止类接触：机间碰撞（一对计一次）、撞击静态世界、硬着陆。"""

from __future__ import annotations

import numpy as np

from awr.bench.eval.thresholds import TH

__all__ = ["safety"]

TICK_NS = 100_000_000


def _segments(mask: np.ndarray) -> int:
    m = np.asarray(mask, bool)
    return int(np.sum(m[1:] & ~m[:-1]) + (1 if len(m) and m[0] else 0))


def safety(run, world, th=TH) -> dict:
    info = run.tape.actors_info()
    pos = np.asarray(run.fleet["pos"], np.float64)
    status = np.asarray(run.fleet["status"])
    n, nu = pos.shape[0], pos.shape[1]
    air = np.isin(status, ("AIRBORNE", "LANDING"))
    sep = run.task["safety"]
    persons = np.array([a["cls"] == "person" for a in info])
    sizes = np.array([a["size_m"] for a in info], np.float64)
    dp = np.full((n, nu), np.inf)
    dv = np.full((n, nu), np.inf)
    for k in range(n):
        st = run.tape.actor_states(k * TICK_NS)
        vis = np.isin(st.state, (1, 2))
        for u in range(nu):
            if not air[k, u]:
                continue
            p = pos[k, u]
            m = vis & persons
            if m.any():
                c = st.pos_enu_m[m]
                dh = np.hypot(p[0] - c[:, 0], p[1] - c[:, 1])
                zr = p[2] - c[:, 2]
                dz = zr - np.clip(zr, 0.0, th.person_h_m)
                dp[k, u] = float(np.min(np.sqrt(np.maximum(dh - th.person_r_m, 0.0) ** 2 + dz ** 2) - th.uav_r_m))
            m = vis & ~persons
            if m.any():
                best = np.inf
                for c, yaw, s in zip(st.pos_enu_m[m], st.yaw_rad[m], sizes[m], strict=True):
                    cy, sy = np.cos(yaw), np.sin(yaw)
                    rel = p - (c + np.array([0, 0, s[2] / 2]))
                    local = np.array([cy * rel[0] + sy * rel[1], -sy * rel[0] + cy * rel[1], rel[2]])
                    q = np.abs(local) - s / 2
                    best = min(best, float(np.linalg.norm(np.maximum(q, 0.0)) + min(q.max(), 0.0)) - th.uav_r_m)
                dv[k, u] = best
    agl = pos[:, :, 2] - np.asarray(world.height_dsm(pos[:, :, :2].reshape(-1, 2)), np.float64).reshape(n, nu)
    climbed = np.maximum.accumulate(agl > 5.0, axis=0)                  # 起飞爬升段不计：首次超过 5 m 之后、降落段之外
    ground = air & climbed & (status != "LANDING") & (agl < 2.0)
    viol_p = sum(_segments(dp[:, u] < sep["min_sep_person_m"]) for u in range(nu))
    viol_v = sum(_segments(dv[:, u] < sep["min_sep_vehicle_m"]) for u in range(nu))
    pen = sum(_segments(dp[:, u] <= 0) + _segments(dv[:, u] <= 0) for u in range(nu))
    fin = lambda a: float(np.min(a)) if np.isfinite(a).any() else None   # noqa: E731
    return {"ground_alerts": int(sum(_segments(ground[:, u]) for u in range(nu))), "nofly_incursions": 0, "detected_count": 0,
            "min_sep_m": {"person": fin(dp), "vehicle": fin(dv)}, "violations": {"person": int(viol_p), "vehicle": int(viol_v)},
            "penetrations": int(pen), "crashes": _crashes(run.contacts)}


def _crashes(contacts: list[dict]) -> dict:
    """机体终止类接触（物理后端判定）：机间碰撞（每对计一次）、撞击静态世界、硬着陆。"""
    uav = {(c["t_ns"], round(c["pos_enu_m"][0], 2), round(c["pos_enu_m"][1], 2)) for c in contacts if c["kind"] == "COLLISION_UAV"}
    return {"uav": len(uav), "world": sum(c["kind"] == "COLLISION_WORLD" for c in contacts), "impact": sum(c["kind"] == "IMPACT" for c in contacts)}
