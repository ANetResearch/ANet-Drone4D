"""聚合与原型榜（D3 §5.6.9；`d3-score-0`，不含合成权重）。

对每项指标 m ∈ {EC, EU, AO, CE}：事件级 `m_e = mean_{seed, rep} m(r)`，赛道级 `m_lane = mean_e m_e`（事件等权）。
95% 置信区间：两阶段分层自助法（每个事件内对种子有放回重采样，再对被抽中种子的重复有放回重采样；四项指标共用同一重采样序列，
缺省 10,000 次）；重采样种子取 `sha256(entry_id ‖ board_version ‖ lane)` 前 8 字节（P0b 起改为 HMAC(K_board, ·)）。
名次：每项指标按点估计降序，相邻条目的配对差值 95% 区间含 0 时归入同一并列组；非支配层级：A 在四项点估计均不低于 B 且至少一项
配对差值区间整体大于 0 时 A 支配 B，逐层剥离。基线为参考行，不参与名次与分层（不影响他者层级；其 `dominance_tier` 为支配它的条目的最高层 + 1）。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

import numpy as np

from awr.bench.eval.thresholds import EVALUATOR_VERSION, SCORE_VERSION

__all__ = ["METRICS", "build_board", "lane_metrics"]

METRICS = ("ec", "eu", "ao", "ce")
N_BOOT = 10_000


def _units(rows: list[dict]) -> dict[str, dict[int, list[dict]]]:
    """event → seed → [components]（重复）。"""
    u: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        u[r["event"]][int(r.get("seed", 0))].append(r["evaluation"]["components"])
    return u


def _point(units, m: str) -> float | None:
    ev = []
    for seeds in units.values():
        vals = [c[m] for reps in seeds.values() for c in reps if c.get(m) is not None]
        if vals:
            ev.append(float(np.mean(vals)))
    return float(np.mean(ev)) if ev else None


def _boot(units, rng: np.random.Generator, n: int) -> np.ndarray:
    """→ (n, 4) 重采样后的赛道级指标。"""
    events = sorted(units)
    out = np.full((n, len(METRICS)), np.nan)
    arr = {e: [(s, np.array([[np.nan if c.get(m) is None else c[m] for m in METRICS] for c in units[e][s]])) for s in sorted(units[e])]
           for e in events}
    for b in range(n):
        ev_means = []
        for e in events:
            seeds = arr[e]
            pick = rng.integers(0, len(seeds), len(seeds))
            vals = []
            for i in pick:
                reps = seeds[i][1]
                vals.append(reps[rng.integers(0, len(reps), len(reps))])
            ev_means.append(np.nanmean(np.concatenate(vals), axis=0))
        out[b] = np.nanmean(np.stack(ev_means), axis=0)
    return out


def _rng(*parts: str) -> np.random.Generator:
    h = hashlib.sha256("‖".join(parts).encode()).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "little"))


def lane_metrics(rows: list[dict], *, key: tuple[str, str, str], n_boot: int = N_BOOT) -> tuple[dict, np.ndarray]:
    units = _units(rows)
    boots = _boot(units, _rng(*key), n_boot)
    out = {}
    for j, m in enumerate(METRICS):
        v = _point(units, m)
        col = boots[:, j][np.isfinite(boots[:, j])]
        ci = [float(np.percentile(col, 2.5)), float(np.percentile(col, 97.5))] if len(col) and v is not None else [0.0, 0.0]
        rd = None
        per_seed = [[c[m] for c in rs if c.get(m) is not None] for seeds in units.values() for rs in seeds.values()]
        if any(len(x) > 1 for x in per_seed):
            rd = float(max(max(x) - min(x) for x in per_seed if len(x) > 1))
        out[m] = {"value": None if v is None else round(v, 6), "ci95": [round(ci[0], 6), round(ci[1], 6)], "repeat_delta": rd}
    return out, boots


def _paired_diff_ci(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    d = a - b
    d = d[np.isfinite(d)]
    if not len(d):
        return (0.0, 0.0)
    return float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))


def build_board(rows: list[dict], *, board_version: str, n_boot: int = N_BOOT, track: dict | None = None) -> dict:
    """rows: [{entry_id, team, display_name, baseline, track, lane, event, seed, evaluation, first_listed_unix_ns?}]。
    `ranked_valid = false` 的 Run 占比 > 20% 的条目标注无效（指标为 null，不参与名次与分层）。"""
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["track"], r["lane"], r["entry_id"])].append(r)
    entries, boots_of = [], {}
    for (tr, lane, eid), rs in sorted(groups.items()):
        valid = [r for r in rs if r["evaluation"]["ranked_valid"]]
        invalid = len(valid) < 0.8 * len(rs)
        # 原型榜（P0a）：同一 track 的固定管线可能全部标注 physics_not_ranked，以 `track.physics` 显式放宽
        use = rs if (invalid and (track or {}).get("allow_invalid")) else valid
        met, boots = lane_metrics(use, key=(eid, board_version, lane), n_boot=n_boot) if use else ({m: {"value": None, "ci95": [0.0, 0.0]}
                                                                                                    for m in METRICS}, None)
        r0 = rs[0]
        e = {"entry_id": eid, "team": r0.get("team", "internal"), "display_name": r0.get("display_name", eid), "track": tr, "lane": lane,
             "metrics": met, "dominance_tier": 1, "first_listed_unix_ns": int(r0.get("first_listed_unix_ns", 0)), "baseline": bool(r0.get("baseline"))}
        entries.append(e)
        boots_of[(tr, lane, eid)] = boots
    for (tr, lane) in sorted({(e["track"], e["lane"]) for e in entries}):
        cand = [e for e in entries if e["track"] == tr and e["lane"] == lane and not e["baseline"] and boots_of[(tr, lane, e["entry_id"])] is not None]
        for j, m in enumerate(METRICS):
            ranked = sorted([e for e in cand if e["metrics"][m]["value"] is not None], key=lambda e: (-e["metrics"][m]["value"], e["entry_id"]))
            group, rank = 0, 1
            for i, e in enumerate(ranked):
                if i > 0:
                    lo, hi = _paired_diff_ci(boots_of[(tr, lane, ranked[i - 1]["entry_id"])][:, j], boots_of[(tr, lane, e["entry_id"])][:, j])
                    if not (lo <= 0.0 <= hi):
                        group += 1
                        rank = i + 1
                e["metrics"][m]["rank"] = rank
                e["metrics"][m]["tie_group"] = group
        remaining, tier = list(cand), 1
        while remaining:
            nd = [a for a in remaining if not any(_dominates(b, a, boots_of[(tr, lane, b["entry_id"])], boots_of[(tr, lane, a["entry_id"])])
                                                  for b in remaining if b is not a)]
            for a in nd:
                a["dominance_tier"] = tier
            remaining = [a for a in remaining if a not in nd]
            tier += 1
        for b in [e for e in entries if e["track"] == tr and e["lane"] == lane and e["baseline"]]:
            bb = boots_of[(tr, lane, b["entry_id"])]                          # 参考行：不改变他者层级，只标注其所处层
            dom = [a["dominance_tier"] for a in cand if bb is not None and _dominates(a, b, boots_of[(tr, lane, a["entry_id"])], bb)]
            b["dominance_tier"] = 1 + max(dom, default=0)
    tracks = sorted({r["track"] for r in rows})
    lanes = sorted({(r["lane"], r.get("event_type", "rendezvous_handoff")) for r in rows})
    seeds = sorted({int(r.get("seed", 0)) for r in rows})
    commit = hashlib.sha256(",".join(map(str, seeds)).encode()).hexdigest()
    t = track or {}
    return {"schema": "drone4d.bench.board.v1", "board_version": board_version,
            "tracks": [{"track": x, "fleet_ref": t.get("fleet_ref", "3x-x500"), "obs_level": t.get("obs_level", "L1"), "physics": "realistic",
                        "mode": "exogenous", "agent_class": t.get("agent_class", "python"), "appearance": "mesh"} for x in tracks],
            "lanes": [{"lane": ln, "kind": "event_type", "event_type": et, "test_events": sorted({r["event"] for r in rows if r["lane"] == ln}),
                       "seed_set_commit": commit} for ln, et in lanes],
            "seed_set_commit": commit, "evaluator_major": int(EVALUATOR_VERSION.split(".")[0]),
            "platform_digest": "sha256:" + hashlib.sha256(f"{SCORE_VERSION}|{EVALUATOR_VERSION}|{t.get('platform', 'd3-gz:dev')}".encode()).hexdigest(),
            "entries": entries}


def _dominates(a: dict, b: dict, ba: np.ndarray, bb: np.ndarray) -> bool:
    va = [a["metrics"][m]["value"] for m in METRICS]
    vb = [b["metrics"][m]["value"] for m in METRICS]
    if any(x is None or y is None for x, y in zip(va, vb, strict=True)):
        return False
    if any(x < y for x, y in zip(va, vb, strict=True)):
        return False
    return any(_paired_diff_ci(ba[:, j], bb[:, j])[0] > 0 for j in range(len(METRICS)))
