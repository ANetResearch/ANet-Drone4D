"""qa.json (M01-FR-048, §6.3.3): confidence histogram, de-dup ratio, pose check, georef summary, gates and the Mock-only
cloud-to-cloud distance of the fused points (registered to world) to the full source cloud. No wall-clock fields."""

from __future__ import annotations

import numpy as np

from ..types import STREAM_RECON_SELFCHECK, rng
from .fuse import Fused
from .georef import Alignment

__all__ = ["C2C_P50_MAX_M", "C2C_P95_MAX_M", "build_qa", "c2c_to_source", "clear_cache"]

C2C_P50_MAX_M = 1.5          # M01-AC-009 (provisional, frozen at MS6)
C2C_P95_MAX_M = 4.0
C2C_SAMPLE = 200_000


_TREE: dict = {}


def clear_cache() -> None:
    """Drop the cached source KD-tree (the job runner calls this when a job ends)."""
    _TREE.clear()


def c2c_to_source(xyz_world: np.ndarray, source_xyz: np.ndarray | None, *, seed: int, key: tuple | None = None,
                  loader=None) -> dict:
    """p50 / p95 nearest distance (m) from a fixed sample of fused points to the full source cloud.

    `key` (e.g. (world dir, contentVersion)) caches the KD-tree of the source cloud for the next call in this process;
    `loader()` provides the source points lazily on a cache miss."""
    from scipy.spatial import cKDTree

    n = len(xyz_world)
    if n > C2C_SAMPLE:
        idx = np.sort(rng(seed, STREAM_RECON_SELFCHECK, 7).choice(n, C2C_SAMPLE, replace=False))
        xyz_world = xyz_world[idx]
    tree = _TREE.get(key) if key is not None else None
    if tree is None:
        src = source_xyz if source_xyz is not None else loader()
        tree = cKDTree(np.asarray(src, dtype=np.float64), balanced_tree=False, compact_nodes=False)   # 2x faster build
        if key is not None:
            _TREE.clear()
            _TREE[key] = tree
    d, _ = tree.query(xyz_world, k=1, workers=1)
    return {"p50": round(float(np.percentile(d, 50)), 4), "p95": round(float(np.percentile(d, 95)), 4)}


def build_qa(fused: Fused, al: Alignment, pose_check: dict, c2c: dict | None) -> dict:
    gates = [{"name": g["name"], "value": g["value"], "result": g["result"]} for g in al.gates]
    if c2c is not None:
        ok = c2c["p50"] <= C2C_P50_MAX_M and c2c["p95"] <= C2C_P95_MAX_M
        gates.append({"name": "c2c_p95_m", "value": c2c["p95"], "result": "pass" if ok else "warn"})
    if pose_check.get("pass") is not True:
        status = "fail"
    elif al.status == "warn" or al.needs_review or any(g["result"] in ("warn", "reject") for g in gates):
        status = "warn"
    else:
        status = "pass"
    r = al.report
    return {"status": status, "conf_hist_u8": [int(v) for v in fused.conf_hist], "dedupe_ratio": round(fused.n / max(1, fused.n_raw), 6),
            "vox_engine": float(fused.vox_engine), "pose_check": pose_check,
            "georef": {"method": al.method, "status": al.status,
                       "inlier_ratio": None if r is None or r.status == "failed" else round(float(r.inlier_ratio), 6),
                       "rmse_m": None if r is None or r.status == "failed" else round(float(r.rmse_m), 6),
                       "sv_ratio": al.gravity.get("sv_ratio"), "gravity_used": bool(al.gravity.get("used"))},
            "c2c_to_source_m": c2c, "gates": gates}
