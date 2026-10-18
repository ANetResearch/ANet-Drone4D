"""聚合与原型榜（D3 §5.6.9；`d3-score-0`，不含合成权重）。

对每项指标 m ∈ {EC, EU, AO, CE}：事件级 `m_e = mean_{seed, rep} m(r)`，赛道级 `m_lane = mean_e m_e`（事件等权）。
95% 置信区间：两阶段分层自助法（每个事件内对种子有放回重采样，再对被抽中种子的重复有放回重采样；四项指标共用同一重采样序列，
缺省 10,000 次）；重采样种子取 `sha256(entry_id ‖ board_version ‖ lane)` 前 8 字节（P0b 起改为 HMAC(K_board, ·)）。
名次：每项指标按点估计降序，相邻条目的配对差值 95% 区间含 0 时归入同一并列组；非支配层级：A 在四项点估计均不低于 B 且至少一项
配对差值区间整体大于 0 时 A 支配 B，逐层剥离。基线为参考行，不参与名次与分层（不影响他者层级；其 `dominance_tier` 为支配它的条目的最高层 + 1）。
无效条目（无效 Run 占比 > 20%）与非排名运行的处理见 `build_board`；合成接口 `compose` 只在评分版本给定权重时使用（缺省 `weights = null`）。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

import numpy as np

from awr.bench.eval.thresholds import EVALUATOR_VERSION, SCORE_VERSION

__all__ = ["METRICS", "build_board", "compose", "entry_details", "lane_metrics", "ranked_row"]

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
    """→ (n, 4) 重采样后的赛道级指标（向量化：每个事件一次抽取全部 n 组种子与重复下标）。"""
    acc = np.zeros((n, len(METRICS)))
    k = np.zeros((n, len(METRICS)))
    for e in sorted(units):
        seeds = [units[e][s] for s in sorted(units[e])]
        r = np.array([len(x) for x in seeds])
        a = np.full((len(seeds), int(r.max()), len(METRICS)), np.nan)
        for i, reps in enumerate(seeds):
            a[i, :len(reps)] = [[np.nan if c.get(m) is None else c[m] for m in METRICS] for c in reps]
        pick = rng.integers(0, len(seeds), (n, len(seeds)))
        rp = r[pick]
        ridx = (rng.random((n, len(seeds), a.shape[1])) * rp[..., None]).astype(np.int64)
        v = a[pick[..., None], ridx]
        v[np.arange(a.shape[1])[None, None, :] >= rp[..., None]] = np.nan
        ev = _nanmean(v.reshape(n, -1, len(METRICS)), axis=1)
        ok = np.isfinite(ev)
        acc += np.where(ok, ev, 0.0)
        k += ok
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(k > 0, acc / np.maximum(k, 1), np.nan)


def _nanmean(x: np.ndarray, axis: int) -> np.ndarray:
    ok = np.isfinite(x)
    c = ok.sum(axis=axis)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(c > 0, np.where(ok, x, 0.0).sum(axis=axis) / np.maximum(c, 1), np.nan)


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


def ranked_row(r: dict) -> bool:
    """可进入正式榜的运行：Realistic 档的排名运行（Fast 档、SITL 与开发运行标注 `physics_not_ranked` 或 `ranked = false`）。"""
    ev = r["evaluation"]
    return ev.get("ranked", True) is not False and "physics_not_ranked" not in ev.get("invalid_reasons", ())


class _Pairs:
    """同一赛道内条目两两配对差值的 95% 区间（按需计算并缓存）。"""

    def __init__(self, boots: dict[str, np.ndarray]) -> None:
        self.boots, self.cache = boots, {}

    def ci(self, a: str, b: str, j: int) -> tuple[float, float]:
        k = (a, b, j)
        if k not in self.cache:
            self.cache[k] = _paired_diff_ci(self.boots[a][:, j], self.boots[b][:, j])
        return self.cache[k]

    def dominates(self, a: dict, b: dict) -> bool:
        """A 支配 B：四项点估计均不低于 B，且至少一项配对差值区间整体大于 0。"""
        va = [a["metrics"][m]["value"] for m in METRICS]
        vb = [b["metrics"][m]["value"] for m in METRICS]
        if any(x is None or y is None or x < y for x, y in zip(va, vb, strict=True)):
            return False
        return any(self.ci(a["entry_id"], b["entry_id"], j)[0] > 0 for j in range(len(METRICS)))


def build_board(rows: list[dict], *, board_version: str, n_boot: int = N_BOOT, track: dict | None = None) -> dict:
    """rows: [{entry_id, team, display_name, baseline, track, lane, event, seed, evaluation, first_listed_unix_ns?}]。

    非排名运行（Fast 档、SITL、开发运行）不进入榜单；`track.dev = true` 的开发榜（Fast 档自检）例外，且不得发布为当前版本。
    `ranked_valid = false` 的 Run 占比 > 20% 的条目 `status = invalid`：指标只按有效 Run 计算并显示，不参与名次与分层
    （`dominance_tier = null`）。不同分轨、不同赛道的条目分组计算，互不混排。"""
    dev = bool((track or {}).get("dev"))
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if dev or ranked_row(r):
            groups[(r["track"], r["lane"], r["entry_id"])].append(r)
    entries, boots_of = [], {}
    for (tr, lane, eid), rs in sorted(groups.items()):
        valid = [r for r in rs if r["evaluation"]["ranked_valid"]]
        invalid = len(valid) < 0.8 * len(rs) and not dev
        use = rs if dev else valid
        met, boots = lane_metrics(use, key=(eid, board_version, lane), n_boot=n_boot) if use else ({m: {"value": None, "ci95": [0.0, 0.0]}
                                                                                                    for m in METRICS}, None)
        r0 = rs[0]
        e = {"entry_id": eid, "team": r0.get("team", "internal"), "display_name": r0.get("display_name", eid), "track": tr, "lane": lane,
             "metrics": met, "status": "invalid" if invalid else "valid", "dominance_tier": None,
             "first_listed_unix_ns": int(r0.get("first_listed_unix_ns", 0)), "baseline": bool(r0.get("baseline"))}
        entries.append(e)
        if boots is not None:
            boots_of[(tr, lane, eid)] = boots
    for (tr, lane) in sorted({(e["track"], e["lane"]) for e in entries}):
        here = [e for e in entries if e["track"] == tr and e["lane"] == lane and (tr, lane, e["entry_id"]) in boots_of and e["status"] == "valid"]
        pairs = _Pairs({e["entry_id"]: boots_of[(tr, lane, e["entry_id"])] for e in here})
        cand = [e for e in here if not e["baseline"]]
        for j, m in enumerate(METRICS):
            ranked = sorted([e for e in cand if e["metrics"][m]["value"] is not None], key=lambda e: (-e["metrics"][m]["value"], e["entry_id"]))
            group, rank = 0, 1
            for i, e in enumerate(ranked):
                if i > 0:
                    lo, hi = pairs.ci(ranked[i - 1]["entry_id"], e["entry_id"], j)
                    if not (lo <= 0.0 <= hi):
                        group += 1
                        rank = i + 1
                e["metrics"][m]["rank"] = rank
                e["metrics"][m]["tie_group"] = group
        remaining, tier = list(cand), 1
        while remaining:
            nd = [a for a in remaining if not any(pairs.dominates(b, a) for b in remaining if b is not a)]
            for a in nd:
                a["dominance_tier"] = tier
            remaining = [a for a in remaining if a not in nd]
            tier += 1
        for b in [e for e in here if e["baseline"]]:                     # 参考行：不改变他者层级，只标注其所处层
            b["dominance_tier"] = 1 + max((a["dominance_tier"] for a in cand if pairs.dominates(a, b)), default=0)
    used = [r for rs in groups.values() for r in rs]
    tracks = sorted({r["track"] for r in used})
    lanes = sorted({(r["lane"], r.get("event_type", "rendezvous_handoff")) for r in used})
    seeds = sorted({int(r.get("seed", 0)) for r in used})
    commit = hashlib.sha256(",".join(map(str, seeds)).encode()).hexdigest()
    t = track or {}
    return {"schema": "drone4d.bench.board.v1", "board_version": board_version,
            "tracks": [{"track": x, "fleet_ref": t.get("fleet_ref", "3x-x500"), "obs_level": t.get("obs_level", "L1"), "physics": "realistic",
                        "mode": "exogenous", "agent_class": t.get("agent_class", "python"), "appearance": "mesh"} for x in tracks],
            "lanes": [{"lane": ln, "kind": "event_type", "event_type": et, "test_events": sorted({r["event"] for r in used if r["lane"] == ln}),
                       "seed_set_commit": commit} for ln, et in lanes],
            "seed_set_commit": commit, "evaluator_major": int(EVALUATOR_VERSION.split(".")[0]),
            "platform_digest": "sha256:" + hashlib.sha256(f"{SCORE_VERSION}|{EVALUATOR_VERSION}|{t.get('platform', 'd3-gz:dev')}".encode()).hexdigest(),
            "entries": entries}


def compose(rows: list[dict], weights: dict[str, float], *, board_version: str, n_boot: int = N_BOOT, track: dict | None = None) -> list[dict]:
    """合成接口（D3-AC-57 (h)；ADR-191）：以评分版本给定的权重，由存储的分量重算各条目的合成值与 95% 置信区间，无需重跑。
    合成值 = Σ w_m · m / Σ w_m；区间取同一重采样序列（与 `build_board` 相同的重采样种子）上合成值的 2.5% 与 97.5% 分位。"""
    w = np.array([float(weights[m]) for m in METRICS])
    if np.any(w < 0) or w.sum() <= 0:
        raise ValueError("weights must be non-negative with a positive sum")
    w = w / w.sum()
    dev = bool((track or {}).get("dev"))
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if dev or ranked_row(r):
            groups[(r["track"], r["lane"], r["entry_id"])].append(r)
    out = []
    for (tr, lane, eid), rs in sorted(groups.items()):
        use = rs if dev else [r for r in rs if r["evaluation"]["ranked_valid"]]
        if not use:
            continue
        met, boots = lane_metrics(use, key=(eid, board_version, lane), n_boot=n_boot)
        vals = [met[m]["value"] for m in METRICS]
        if any(v is None for v in vals):
            out.append({"entry_id": eid, "track": tr, "lane": lane, "value": None, "ci95": [0.0, 0.0]})
            continue
        col = boots @ w
        col = col[np.isfinite(col)]
        out.append({"entry_id": eid, "track": tr, "lane": lane, "value": round(float(np.dot(w, vals)), 6),
                    "ci95": [round(float(np.percentile(col, 2.5)), 6), round(float(np.percentile(col, 97.5)), 6)]})
    return out


def entry_details(rows: list[dict]) -> dict[str, dict]:
    """BN-84 条目详情：entry_id → 按（分轨、赛道、test 事件）分解的四项指标均值、Run 数与无效 Run 数（不含合成值；
    `repeat_delta` 在 P0a 无重复运行，为 null）。"""
    groups: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["entry_id"], r["track"], r["lane"], r["event"])].append(r)
    out: dict[str, dict] = {}
    for (eid, tr, lane, ev), rs in sorted(groups.items()):
        valid = [r for r in rs if r["evaluation"]["ranked_valid"]]
        use = valid or rs
        met = {}
        for m in METRICS:
            vals = [r["evaluation"]["components"][m] for r in use if r["evaluation"]["components"].get(m) is not None]
            met[m] = float(np.mean(vals)) if vals else None
        reasons = sorted({x for r in rs for x in r["evaluation"].get("invalid_reasons", [])})
        d = out.setdefault(eid, {"entry_id": eid, "display_name": rs[0].get("display_name", eid), "baseline": bool(rs[0].get("baseline")),
                                 "tracks": {}})
        lanes = d["tracks"].setdefault(tr, {})
        lanes.setdefault(lane, []).append({"event": ev, "n_runs": len(rs), "invalid_runs": len(rs) - len(valid), "invalid_reasons": reasons,
                                           "seeds": sorted({int(r.get("seed", 0)) for r in rs}), "metrics": met, "repeat_delta": None})
    return out
