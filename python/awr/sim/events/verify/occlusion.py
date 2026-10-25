"""空中遮挡检测器 `aerial_occlusion` 与 1 Hz 天空可见度（M23 §6.9.1、§6.10.1 `skyvis_1hz`，r6 §6.1）。

脚本行人可见时，在 1 Hz 采样时刻（t = 0、1、2 … s）自躯干点（磁带 z + `torso_m`）向仰角 ≥ `elev_min_deg` 的 `n_dirs` 个等立体角
方向发射射线（`MeshWorldQuery.raycast_batch`，射程 `ray_m`）；未命中网格的比例为天空可见度。方向集为仰角带内按 z = sin(仰角) 均匀
分层、方位按黄金角递增的确定性螺旋（等立体角），与演员、时刻无关。天空可见度 < `sky_vis_max` 的连续采样段持续（末采样时刻 −
首采样时刻）≥ `min_dur_s` 记为事件；显著性 min(1, 持续 / 10 s)（§6.9.2）。不可见时刻的天空可见度为 null。
"""

from __future__ import annotations

import math
import time

import numpy as np

from awr.sim.events.verify.base import Context, Detection, rnd, runs
from awr.sim.events.verify.registry import detector

SALIENCE_FULL_S = 10.0
RAY_CHUNK = 1 << 20
DEFAULTS = {"torso_m": 1.0, "elev_min_deg": 25.0, "n_dirs": 64, "sky_vis_max": 0.35, "min_dur_s": 3.0, "hz": 1, "ray_m": 1000.0}


def sky_dirs(n: int, elev_min_deg: float) -> np.ndarray:
    """(n, 3) 单位方向：z ∈ [sin(仰角下限), 1] 等分 n 层取层中值，方位 φ_i = i · π(3 − √5)。"""
    z0 = math.sin(math.radians(elev_min_deg))
    i = np.arange(n, dtype=np.float64)
    z = z0 + (1.0 - z0) * (i + 0.5) / n
    r = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    phi = i * math.pi * (3.0 - math.sqrt(5.0))
    return np.stack([r * np.cos(phi), r * np.sin(phi), z], 1)


def skyvis(ctx: Context, p: dict) -> tuple[np.ndarray, dict[int, np.ndarray]]:
    """(采样 tick (n_samples,), {演员 idx: (n_samples,) float64，不可见为 NaN})；按 (torso_m, elev_min_deg, n_dirs, hz, ray_m) 缓存。"""
    key = ("skyvis", float(p["torso_m"]), float(p["elev_min_deg"]), int(p["n_dirs"]), int(p["hz"]), float(p["ray_m"]))
    if key in ctx.cache:
        return ctx.cache[key]
    t0 = time.perf_counter()
    step = max(1, round(1000 / int(p["hz"]) / ctx.tick_ms))
    ks = np.arange(0, ctx.n_ticks, step)
    D = sky_dirs(int(p["n_dirs"]), float(p["elev_min_deg"]))
    persons = ctx.scripted("person")
    org_parts, owner = [], []
    for i in persons:
        kv = ks[ctx.vis[ks, i]]
        org_parts.append(np.stack([ctx.xy[kv, i, 0], ctx.xy[kv, i, 1], ctx.z[kv, i] + float(p["torso_m"])], 1))
        owner.append((i, kv))
    out = {i: np.full(len(ks), np.nan) for i in persons}
    if sum(len(o) for o in org_parts):
        org = np.repeat(np.concatenate(org_parts), len(D), 0)
        dirs = np.tile(D, (len(org) // len(D), 1))
        free = np.empty(len(org), bool)
        for a in range(0, len(org), RAY_CHUNK):
            t, _prim, _inst = ctx.mesh.raycast_batch(org[a:a + RAY_CHUNK], dirs[a:a + RAY_CHUNK], float(p["ray_m"]))
            free[a:a + RAY_CHUNK] = ~np.isfinite(t)
        frac = free.reshape(-1, len(D)).mean(1)
        off = 0
        for i, kv in owner:
            out[i][np.searchsorted(ks, kv)] = frac[off:off + len(kv)]
            off += len(kv)
        ctx.perf["skyvis_rays"] = ctx.perf.get("skyvis_rays", 0) + len(org)
    ctx.perf["skyvis_ms"] = ctx.perf.get("skyvis_ms", 0) + int((time.perf_counter() - t0) * 1000)
    ctx.cache[key] = ks, out
    return ks, out


def skyvis_1hz(ctx: Context) -> dict[str, list[float | None]]:
    """真值 `skyvis_1hz`：缺省参数下的序列，保留 3 位小数；无网格时为空。"""
    if ctx.mesh is None:
        return {}
    _ks, sv = skyvis(ctx, DEFAULTS)
    return {ctx.ids[i]: [None if not np.isfinite(v) else rnd(v) for v in s] for i, s in sorted(sv.items())}


@detector("aerial_occlusion", defaults=DEFAULTS, needs=("mesh",))
def aerial_occlusion(ctx: Context, p: dict) -> list[Detection]:
    ks, sv = skyvis(ctx, p)
    need_ms = round(float(p["min_dur_s"]) * 1000)
    out = []
    for i, s in sorted(sv.items()):
        low = np.isfinite(s) & (s < float(p["sky_vis_max"]))
        for j0, j1 in runs(low):
            t0, t1 = ctx.t_ms(ks[j0]), ctx.t_ms(ks[j1])
            if t1 - t0 < need_ms:
                continue
            km = int(ks[(j0 + j1) // 2])
            out.append(Detection("aerial_occlusion", (ctx.ids[i],), (t0, t1), min(1.0, (t1 - t0) / (SALIENCE_FULL_S * 1000.0)),
                                 {"sky_vis_min": rnd(np.nanmin(s[j0:j1 + 1]))}, (float(ctx.xy[km, i, 0]), float(ctx.xy[km, i, 1]))))
    return out
