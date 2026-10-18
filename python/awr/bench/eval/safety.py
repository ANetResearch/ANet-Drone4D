"""安全与合规（M27 §6.7.6）：机体球（r 0.5 m）与演员包围体（人员圆柱近似胶囊 r 0.4 m、h 1.8 m；车辆按尺寸 OBB）的最小距离，
P0a 以 10 Hz 真值轨迹计算，只计机体在空中（AIRBORNE、LANDING）的 tick；距离低于任务间距的连续区段计一次违规，≤ 0 记穿透
（`ranked_valid = false`，`actor_penetration`）。首次升至 5 m 以上之后、降落段之外离地高度 < 2 m 的空中区段计地面告警。
`crashes` 统计物理后端判定的终止类接触：机间碰撞（一对计一次）、撞击静态世界、硬着陆。

逐 tick 只取一次演员状态，空中机体与演员的距离按（机 × 演员）矩阵一次求出（D3-AC-18 评测耗时），与逐机逐演员的定义逐值一致。"""

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
        au = np.nonzero(air[k])[0]
        if not len(au):
            continue
        st = run.tape.actor_states(k * TICK_NS)
        vis = np.isin(st.state, (1, 2))
        P = pos[k, au]                                                    # (m, 3) 空中机体
        m = vis & persons
        if m.any():
            dp[k, au] = _person_dist(P, st.pos_enu_m[m], th)
        m = vis & ~persons
        if m.any():
            dv[k, au] = _vehicle_dist(P, st.pos_enu_m[m], st.yaw_rad[m], sizes[m], th)
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


def _person_dist(P: np.ndarray, c: np.ndarray, th) -> np.ndarray:
    """机体球（中心 P (m, 3)）到人员竖直圆柱（底面中心 c (a, 3)、r、h）的最小净距 → (m,)。"""
    dh = np.hypot(P[:, None, 0] - c[None, :, 0], P[:, None, 1] - c[None, :, 1])
    zr = P[:, None, 2] - c[None, :, 2]
    dz = zr - np.clip(zr, 0.0, th.person_h_m)
    return np.min(np.sqrt(np.maximum(dh - th.person_r_m, 0.0) ** 2 + dz ** 2) - th.uav_r_m, axis=1)


def _vehicle_dist(P: np.ndarray, c: np.ndarray, yaw: np.ndarray, s: np.ndarray, th) -> np.ndarray:
    """机体球到车辆 OBB（底面中心 c (v, 3)、航向 yaw、尺寸 s (v, 3)）的最小有符号净距 → (m,)。"""
    cy, sy = np.cos(yaw), np.sin(yaw)
    rel = P[:, None, :] - (c + np.column_stack([np.zeros(len(c)), np.zeros(len(c)), s[:, 2] / 2]))[None]
    local = np.stack([cy * rel[..., 0] + sy * rel[..., 1], -sy * rel[..., 0] + cy * rel[..., 1], rel[..., 2]], -1)
    q = np.abs(local) - s[None] / 2
    d = np.linalg.norm(np.maximum(q, 0.0), axis=-1) + np.minimum(q.max(axis=-1), 0.0) - th.uav_r_m
    return d.min(axis=1)


def _crashes(contacts: list[dict]) -> dict:
    """机体终止类接触（物理后端判定）：机间碰撞（每对计一次）、撞击静态世界、硬着陆。"""
    uav = {(c["t_ns"], round(c["pos_enu_m"][0], 2), round(c["pos_enu_m"][1], 2)) for c in contacts if c["kind"] == "COLLISION_UAV"}
    return {"uav": len(uav), "world": sum(c["kind"] == "COLLISION_WORLD" for c in contacts), "impact": sum(c["kind"] == "IMPACT" for c in contacts)}
