"""协同效率 CE v0（M27 §6.7.6 `collaborative`；D3 §5.6.9 归一化）。

原始量：gain = EC(U) − max_u EC({u})、冗余度（1 s 桶内识别同一演员的机数均值）、航程、发送字节、丢包率。
归一化：`Ĝ = clip((V(U) − max_u V({u})) / (1 − 1/|U|), 0, 1)`，V(S) 为关键事件区间 1 s 桶中"每名在场参与者都被 S 中至少一架机
识别"的占比；`f_res = clip(1 − 0.5 · max(0, rho − 1), 0, 1)`，`rho = ½(ΣD / ΣD_budget + ΣB / ΣB_budget)`；`CE = Ĝ · f_res`（单机为 None）。
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from awr.bench.eval.ec import event_capture

__all__ = ["collaborative"]


def _restrict(ident, u):
    return {k: {x for x in v if x[0] == u} for k, v in ident.items()}


def _coverage_buckets(E, ident, present, n_ticks, S_set) -> float:
    ok = tot = 0
    for e in E:
        for s in range(int(e["a_s"]), int(np.ceil(e["b_s"])) + 1):
            ks = range(s * 10, min(s * 10 + 10, n_ticks + 1))
            here = [p for p in e["participants"] if any(present[p][k] for k in ks)]
            if not here:
                continue
            tot += 1
            if all(any((u, p) in ident.get(k, ()) for k in ks for u in S_set) for p in here):
                ok += 1
    return ok / tot if tot else 0.0


def collaborative(E, U, ident, present, n_ticks, run) -> tuple[dict, float | None, dict]:
    ec_fleet, _, _ = event_capture(E, U, ident, present, n_ticks)
    ec_u = {u: event_capture(E, [u], _restrict(ident, u), present, n_ticks)[0] for u in U}
    G = ec_fleet - max(ec_u.values(), default=0.0)
    B = defaultdict(set)
    for k, pairs in ident.items():
        for u, p in pairs:
            B[(k // 10, p)].add(u)
    redundancy = float(np.mean([len(v) for v in B.values()])) if B else 0.0
    pos = np.asarray(run.fleet["pos"], np.float64)                     # (n, n_uav, 3)
    km = float(np.linalg.norm(np.diff(pos, axis=0), axis=2).sum()) / 1000.0 if len(pos) > 1 else 0.0
    sent_b = sum(a["comm"]["bytes"] for a in run.manifest["agents"].values())
    sent = sum(1 for r in run.net)
    lost = sum(1 for r in run.net if not r["ok"])
    kb = sent_b / 1000.0
    raw = {"gain": float(G), "gain_per_extra_uav": float(G / (len(U) - 1)) if len(U) > 1 else 0.0, "redundancy": redundancy,
           "flight_km": km, "comm_kb": kb, "loss_rate": lost / max(sent, 1), "dec_per_km": float(G / km) if km > 0 else 0.0,
           "dec_per_kb": float(G / kb) if kb > 0 else 0.0}
    if len(U) < 2:
        return raw, None, {"g_hat": None, "f_res": None}
    V = _coverage_buckets(E, ident, present, n_ticks, set(U))
    Vu = max(_coverage_buckets(E, ident, present, n_ticks, {u}) for u in U)
    g_hat = float(np.clip((V - Vu) / (1 - 1 / len(U)), 0, 1))
    bud = run.task["budget"]
    rho = 0.5 * (km / (bud["distance_km_per_uav"] * len(U)) + kb / (bud["comm_kb_per_uav"] * len(U)))
    f_res = float(np.clip(1 - 0.5 * max(0.0, rho - 1), 0, 1))
    return raw, g_hat * f_res, {"g_hat": g_hat, "f_res": f_res, "V_fleet": V, "V_best_single": Vu, "rho": rho}
