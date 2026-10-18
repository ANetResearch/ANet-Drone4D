"""主动观测效率 AO（M27-FR-090；P0a 为 v0）。

每个计分事件取区间中点为关键时刻 τ，参与者取 τ 时在场的人员。证据质量 `q = min(1, h / 30 px) · vis`（与识别同口径）：
- `AO_real`：τ ± 1 s 内同一架机对全部参与者实际获得的证据质量（取各参与者最大值后取最小，再对机取最大）÷ oracle 最优质量；
- `AO_pos_los`：τ 时各机所在位置（不看相机指向）的视点质量 ÷ oracle 最优质量（含静态网格遮挡）；
- `AO_pos`：同上但不计遮挡（只看距离），÷ 无遮挡 oracle 最优质量。
oracle：以参与者质心为中心、半径 `fx · 1.6 / 16`、10 m 网格与任务高度包络（10 m 一档）的候选视点上取最大质量；视点可见性以
身体 5 点视线计。P0a 的 oracle 按本 Run 的标称相机计算（版本 `ao-oracle-v0-grid@1`），P1 的 v1 按机队相机与云台范围计算。
"""

from __future__ import annotations

import math

import numpy as np

from awr.agentsdk.env.geom import los
from awr.bench.eval.thresholds import ORACLE_VERSION, TH

__all__ = ["active_observation"]

TICK_NS = 100_000_000


def _body_points(c: np.ndarray, H: float, L: float, yaw: float) -> np.ndarray:
    cy, sy = math.cos(yaw), math.sin(yaw)
    return c + np.array([[0, 0, 0.30 * H], [0, 0, 0.55 * H], [0, 0, 0.90 * H], [0.35 * L * cy, 0.35 * L * sy, 0.5 * H],
                         [-0.35 * L * cy, -0.35 * L * sy, 0.5 * H]])


def _quality(V: np.ndarray, persons: list[tuple[np.ndarray, float, np.ndarray]], fx: float, world, with_los: bool, th) -> np.ndarray:
    """V (m, 3) 视点；persons [(中心, 身高, 5 点)] → (m,) 质量（对人员取最小）。"""
    q = np.full(len(V), np.inf)
    for c, H, pts in persons:
        d = np.linalg.norm(V - (c + np.array([0, 0, H / 2])), axis=1)
        qp = np.minimum(1.0, fx * H / np.maximum(d, 1e-3) / th.ao_q_px)
        if with_los:
            A = np.repeat(V, 5, axis=0)
            B = np.tile(pts, (len(V), 1))
            vis = los(world, A, B).reshape(len(V), 5).mean(axis=1)
            qp = qp * vis
        q = np.minimum(q, qp)
    return q


def active_observation(E: list[dict], U: list[str], run, world, *, fx: float, envelope_agl_m: tuple[float, float], th=TH) -> dict:
    info = {a["id"]: a for a in run.tape.actors_info()}
    pos = np.asarray(run.fleet["pos"], np.float64)
    real, pos_los, pos_only = [], [], []
    for e in E:
        k0 = round((e["a_s"] + e["b_s"]) / 2 * 10)
        k0 = min(k0, run.n_ticks)
        ids = [p for p in e["participants"] if info[p]["cls"] == "person"]
        if not ids:
            continue
        st = run.tape.actor_states(k0 * TICK_NS, [info[p]["idx"] for p in ids])
        here = [(p, st.pos_enu_m[i], info[p]["size_m"][2], float(st.yaw_rad[i])) for i, p in enumerate(ids) if st.state[i] in (1, 2)]
        if not here:
            continue
        persons = [(c, H, _body_points(c, H, info[p]["size_m"][0], yaw)) for p, c, H, yaw in here]
        cen = np.mean([c for _p, c, _H, _y in here], axis=0)
        r_max = fx * 1.6 / th.h_px
        g = np.arange(-r_max, r_max + 1e-9, th.oracle_grid_m)
        gx, gy = np.meshgrid(g, g)
        m = gx ** 2 + gy ** 2 <= r_max ** 2
        xy = np.stack([cen[0] + gx[m], cen[1] + gy[m]], 1)
        zg = np.asarray(world.ground_dtm(xy), np.float64)
        lo, hi = envelope_agl_m
        V = np.concatenate([np.column_stack([xy, zg + a]) for a in np.arange(lo, hi + 1e-9, th.oracle_alt_step_m)])
        q_star = float(_quality(V, persons, fx, world, True, th).max())
        q_star_nl = float(_quality(V, persons, fx, world, False, th).max())
        here_ids = {p for p, *_ in here}
        best = 0.0
        for u in U:
            per: dict[str, float] = {}
            for r in run.l1:
                if r["uav"] == u and r["actor"] in here_ids and abs(int(r["seq"]) - k0) <= th.ao_window_s * 10:
                    per[r["actor"]] = max(per.get(r["actor"], 0.0), min(1.0, r["h_unocc_px"] / th.ao_q_px) * r["vis"])
            if len(per) == len(here_ids):
                best = max(best, min(per.values()))
        Vu = pos[k0]
        real.append(min(1.0, best / max(q_star, 1e-9)))
        pos_los.append(min(1.0, float(_quality(Vu, persons, fx, world, True, th).max()) / max(q_star, 1e-9)))
        pos_only.append(min(1.0, float(_quality(Vu, persons, fx, world, False, th).max()) / max(q_star_nl, 1e-9)))
    return {"ao_real": float(np.mean(real)) if real else 0.0, "ao_pos_los": float(np.mean(pos_los)) if pos_los else 0.0,
            "ao_pos": float(np.mean(pos_only)) if pos_only else 0.0,
            "oracle": {"version": ORACLE_VERSION, "camera_models": ["d4d_eo1080@1"], "grid_m": th.oracle_grid_m, "alt_step_m": th.oracle_alt_step_m,
                       "envelope_agl_m": [float(envelope_agl_m[0]), float(envelope_agl_m[1])]}}
