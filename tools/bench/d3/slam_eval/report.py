"""分布统计与 D3-AC-14 判定（M27-FR-108、M27-AC-024；AWR-05 §5.4.6 统计口径）。

- 单次运行的 RPE100 标量取段均值（`traj.rpe100_pct`），跨"种子 × 重复"报告中位数、p10、p90、最大值；ATE、地图到网格距离与 F@0.2
  同样给分布；段中位数另报（`rpe100_median_pct` 的分布）。
- D3-AC-14 阈值（§11 原文；均为 r1 样条轨迹口径的暂定值，P-1 以 gz 锁步与 Fast 档轨迹复测后由 ADR 冻结）：
  clean 档 RPE100 ≤ 0.10%、对齐 ATE ≤ 0.20 m、地图到网格距离均值 ≤ 0.15 m；realistic 档 5 种子 × 3 次重复的 RPE100 中位数落在
  [0.15%, 0.40%]、单次最大 ≤ 0.50%、且中位数不低于 clean 档；HKisland03 真机回归 ATE ≤ 1.2 m；8 m/s 航段逐点位姿相对单一位姿的
  ATE 改善 ≥ 5 倍（SL3 判定）。LIVO 为主口径，LIO 单列（同一阈值另报，不进入主判定）。clean 档按跨重复的中位数判定，另报最大值。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

import numpy as np

__all__ = ["AC14", "distribution", "judge_ac14", "run_scalars", "summarize_runs"]

AC14 = {
    "clean": {"rpe100_pct_max": 0.10, "ate_aligned_m_max": 0.20, "map_to_mesh_mean_m_max": 0.15},
    "realistic": {"rpe100_median_range_pct": [0.15, 0.40], "rpe100_single_max_pct": 0.50, "median_not_below_clean": True,
                  "seeds": 5, "repeats": 3},
    "hkisland03_ate_m_max": 1.2,
    "per_point_vs_single_pose_ate_ratio_min": 5.0,
    "main_mode": "livo",
    "status": "provisional",
    "source": "D3-AC-14 (r1 spline trajectories; frozen by ADR after the P-1 re-measurement on gz lockstep and Fast tier)",
}

SCALARS = {
    "rpe100_pct": ("traj", "rpe100_pct"),
    "rpe100_median_pct": ("traj", "rpe100_median_pct"),
    "rpe100_max_pct": ("traj", "rpe100_max_pct"),
    "ate_aligned_rmse_m": ("traj", "ate_aligned_rmse_m"),
    "drift_max_m": ("traj", "drift_max_m"),
    "drift_final_m": ("traj", "drift_final_m"),
    "map_to_mesh_mean_m": ("map", "acc_mean_m"),
    "fscore@0.2": ("map", "fscore@0.2"),
    "fscore@0.2_aligned": ("map_aligned", "fscore@0.2"),
    "rss_peak_gb": ("resources", "rss_peak_gb"),
}


def distribution(values: Iterable[float | None]) -> dict:
    v = np.array([x for x in values if x is not None and np.isfinite(x)], np.float64)
    if len(v) == 0:
        return {"n": 0}
    return {"n": len(v), "median": float(np.median(v)), "p10": float(np.percentile(v, 10)), "p90": float(np.percentile(v, 90)),
            "min": float(v.min()), "max": float(v.max()), "mean": float(v.mean())}


def run_scalars(run: Mapping) -> dict:
    """单次运行（metrics.json 的 runs[] 条目）→ 标量表。"""
    out = {}
    for k, (sec, key) in SCALARS.items():
        out[k] = (run.get(sec) or {}).get(key)
    return out


def summarize_runs(runs: Sequence[Mapping]) -> dict:
    """按模式汇总：成功与失败计数、各标量分布。"""
    out: dict = {}
    for mode in sorted({r["mode"] for r in runs}):
        rs = [r for r in runs if r["mode"] == mode]
        ok = [r for r in rs if r.get("ok")]
        sc = [run_scalars(r) for r in ok]
        out[mode] = {"runs": len(rs), "ok": len(ok), "failed": len(rs) - len(ok),
                     "failures": [{"run": r.get("run"), "code": r.get("code"), "why": r.get("why")} for r in rs if not r.get("ok")],
                     **{k: distribution(s[k] for s in sc) for k in SCALARS}}
    return out


def _runs_of(docs: Sequence[Mapping], mode: str) -> list[dict]:
    return [{**r, "_seed": (d.get("case") or {}).get("seed"), "_noise": (d.get("case") or {}).get("noise_profile"),
             "_case": (d.get("case") or {}).get("name")} for d in docs for r in d.get("runs", []) if r.get("mode") == mode]


def judge_ac14(clean: Sequence[Mapping], realistic: Sequence[Mapping], *, modes: Sequence[str] = ("livo", "lio")) -> dict:
    """由 clean 与 realistic 档的作业结果（metrics.json）判定 D3-AC-14 的 RPE100 / ATE / 地图到网格条款。"""
    th = AC14
    res: dict = {"thresholds": th, "modes": {}}
    for mode in modes:
        c = _runs_of(clean, mode)
        r = _runs_of(realistic, mode)
        c_ok = [x for x in c if x.get("ok")]
        r_ok = [x for x in r if x.get("ok")]
        cd = {k: distribution(run_scalars(x)[k] for x in c_ok) for k in ("rpe100_pct", "ate_aligned_rmse_m", "map_to_mesh_mean_m")}
        rd = distribution(run_scalars(x)["rpe100_pct"] for x in r_ok)
        checks = []

        def chk(name: str, value, ok: bool | None, limit, checks=checks) -> None:
            checks.append({"clause": name, "value": value, "limit": limit, "pass": ok})

        def le(d: dict, lim: float) -> bool | None:
            return None if d.get("n", 0) == 0 else bool(d["median"] <= lim)

        chk("clean RPE100 median <= 0.10%", cd["rpe100_pct"].get("median"), le(cd["rpe100_pct"], th["clean"]["rpe100_pct_max"]),
            th["clean"]["rpe100_pct_max"])
        chk("clean aligned ATE median <= 0.20 m", cd["ate_aligned_rmse_m"].get("median"),
            le(cd["ate_aligned_rmse_m"], th["clean"]["ate_aligned_m_max"]), th["clean"]["ate_aligned_m_max"])
        chk("clean map-to-mesh mean (median over runs) <= 0.15 m", cd["map_to_mesh_mean_m"].get("median"),
            le(cd["map_to_mesh_mean_m"], th["clean"]["map_to_mesh_mean_m_max"]), th["clean"]["map_to_mesh_mean_m_max"])
        lo, hi = th["realistic"]["rpe100_median_range_pct"]
        med = rd.get("median")
        chk("realistic RPE100 median in [0.15%, 0.40%]", med, None if med is None else bool(lo <= med <= hi), [lo, hi])
        chk("realistic RPE100 single max <= 0.50%", rd.get("max"),
            None if med is None else bool(rd["max"] <= th["realistic"]["rpe100_single_max_pct"]), th["realistic"]["rpe100_single_max_pct"])
        cm = cd["rpe100_pct"].get("median")
        chk("realistic median >= clean median", [med, cm], None if med is None or cm is None else bool(med >= cm), "clean median")
        seeds = sorted({x["_seed"] for x in r if x.get("_seed") is not None})
        need = th["realistic"]["seeds"] * th["realistic"]["repeats"]
        complete = len(seeds) >= th["realistic"]["seeds"] and len(r_ok) >= need
        chk(f"realistic sample = {th['realistic']['seeds']} seeds x {th['realistic']['repeats']} repeats",
            {"seeds": len(seeds), "ok_runs": len(r_ok), "runs": len(r)}, bool(complete) if r else None, need)
        verdicts = [x["pass"] for x in checks]
        res["modes"][mode] = {
            "role": "main" if mode == th["main_mode"] else "separate",
            "clean": {**cd, "runs": len(c), "ok": len(c_ok), "rpe100_max_over_runs": cd["rpe100_pct"].get("max")},
            "realistic": {"rpe100_pct": rd, "runs": len(r), "ok": len(r_ok), "seeds": seeds,
                          "rpe100_segment_median": distribution(run_scalars(x)["rpe100_median_pct"] for x in r_ok)},
            "checks": checks,
            "pass": (None if any(v is None for v in verdicts) else all(verdicts)),
        }
    main = res["modes"].get(th["main_mode"], {})
    res["pass"] = main.get("pass")
    res["status"] = th["status"]
    return res
