"""Ingest 十步流水线（M03 §6.4；AWR-16 §10.1）：读取与校验 → 单位 → 上方向 → 调平 → 北向 → DTM 与原点 →
法线修正 → HAG 与规则分类 → 统计与示意锚点 → QA 门禁。"""

from __future__ import annotations

import resource
import time

import numpy as np

from awr.world.georef.frames import precision_report

from ..pointcloud.npx import colmax, colmin
from ..terrain.dsm import dsm_index, raw_top
from ..terrain.dtm import dtm_opening
from .anchor import solve_anchor
from .classify import classify
from .gates import run_gates
from .normalize import (
    IDENTITY,
    UP_ROT,
    angle_to_z_deg,
    apply_linear,
    compose3,
    detect_up_axis,
    exact_rz,
    ground_plane_mad,
    rodrigues,
    scale3,
    units_heuristic,
    up_axis_scores,
)
from .normals import fix_normals
from .stats import class_histogram, nn_median, percentiles2
from .types import (
    CloudStats,
    ConfigError,
    GateFailed,
    IngestAdapter,
    NormalizedCloud,
    StageContext,
    TerrainGrids,
)

MAX_POINTS_SINGLE_PASS = 50_000_000     # NFR-017：D1 单遍切片上限（超过为 V0.5 分块切片）


def peak_rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)


def ingest(adapter: IngestAdapter, ctx: StageContext | None = None, *, dtm_cell_m: float = 10.0,
           dsm_cell_m: float = 2.0) -> NormalizedCloud:
    ctx = ctx or StageContext(quiet=True)
    cfg = adapter.config()
    if cfg.level == "auto":
        raise ConfigError("level = auto 属于 V0.2 通用配置化导入")
    if cfg.dedupe != "off":
        raise ConfigError("dedupe = exact 为 V0.2（M03-FR-022）")
    if cfg.semantic != "rules":
        raise ConfigError("--semantic csf 为 P2（V0.2 提供）")
    if cfg.units_to_m is None:
        raise ConfigError("units_to_m = auto 属于 V0.2 通用配置化导入")
    T: dict[str, float] = {}
    rss: dict[str, float] = {}
    t0 = time.perf_counter()
    # ---- 1 读取与校验
    raw = adapter.load()
    n_raw = len(raw.xyz)
    if n_raw > MAX_POINTS_SINGLE_PASS:
        raise ConfigError(f"{n_raw} 点超过 D1 单遍切片上限 {MAX_POINTS_SINGLE_PASS}（V0.5 分块切片）")
    P = np.asarray(raw.xyz, np.float64)
    N0 = np.zeros((n_raw, 3)) if raw.normal is None else np.asarray(raw.normal, np.float64)
    keep = np.isfinite(P[:, 0]) & np.isfinite(P[:, 1]) & np.isfinite(P[:, 2])
    n_nonfinite = int(n_raw - keep.sum())
    if n_nonfinite:
        P, N0 = P[keep], N0[keep]
    N0[~(np.isfinite(N0[:, 0]) & np.isfinite(N0[:, 1]) & np.isfinite(N0[:, 2]))] = 0.0
    T["read"] = time.perf_counter() - t0
    rss["read"] = peak_rss_mb()
    ctx.check_cancel()
    t1 = time.perf_counter()
    # ---- 2–5 单位、上方向、调平、北向
    R_up = UP_ROT[cfg.up_axis]
    scores = up_axis_scores(N0)
    detected = detect_up_axis(scores)
    if detected != cfg.up_axis:
        raise GateFailed(f"上方向判定 {detected}（得分 {scores}）与配置 {cfg.up_axis} 不一致")
    units_h, _hag_p99_raw = units_heuristic(P, R_up)
    Nu = apply_linear(R_up, N0)
    up = Nu[:, 2] > 0.95
    mean_up = Nu[up].mean(0) if up.any() else np.array([0.0, 0.0, 1.0])
    tilt = angle_to_z_deg(mean_up)
    do_level = cfg.level is True and tilt > 0.5
    R_lvl = rodrigues(mean_up) if do_level else IDENTITY
    R = compose3(exact_rz(cfg.yaw_deg), R_lvl, R_up)
    A = scale3(float(cfg.units_to_m), R)
    Q = apply_linear(A, P)
    del P
    Nn = apply_linear(R, N0)
    del N0, Nu
    T["ingest"] = time.perf_counter() - t1
    ctx.check_cancel()
    # ---- 6 DTM 与原点
    t2 = time.perf_counter()
    dtm, gi = dtm_opening(Q, cell=dtm_cell_m)
    hag = Q[:, 2] - dtm[gi.iy, gi.ix]
    ground_frac = float((up & (np.abs(hag) < 1.5)).mean())
    ground_type = "dtm"
    if ground_frac <= 0.10:
        z0 = float(np.percentile(Q[:, 2], 0.5))
        dtm[:] = z0
        hag = Q[:, 2] - z0
        ground_type = "synthetic"
    del up
    qmn, qmx = colmin(Q), colmax(Q)
    origin = np.array([(qmn[0] + qmx[0]) / 2, (qmn[1] + qmx[1]) / 2, float(np.nanmedian(dtm))])
    relief = (round(float(np.nanpercentile(dtm, 1) - origin[2]), 2), round(float(np.nanpercentile(dtm, 99) - origin[2]), 2))
    E = Q
    E -= origin
    dtm_w = (dtm - origin[2]).astype(np.float32)
    dtm_origin = (float(gi.origin_xy[0] - origin[0]), float(gi.origin_xy[1] - origin[1]))
    del gi
    T["dtm"] = time.perf_counter() - t2
    rss["dtm"] = peak_rss_mb()
    ctx.check_cancel()
    # ---- 7 法线修正（与 DSM 共用 2 m 顶面网格）
    t3 = time.perf_counter()
    ci = dsm_index(E, dsm_cell_m)
    top = raw_top(ci, E[:, 2])
    grid_origin = (float(E[:, 0].min()), float(E[:, 1].min()))
    Nn, flipped, zero = fix_normals(E, Nn, hag, top, ci.ix, ci.iy, grid_origin, dsm_cell_m)
    T["normals"] = time.perf_counter() - t3
    # ---- 8 HAG 与规则分类
    t4 = time.perf_counter()
    cls = classify(Nn, hag)
    upf = Nn[:, 2] > 0.95
    tilt_after = angle_to_z_deg(Nn[upf].mean(0)) if upf.any() else 0.0
    mad = ground_plane_mad(E, upf & (np.abs(hag) < 1.5)) if do_level else None
    del upf
    T["classify"] = time.perf_counter() - t4
    ctx.check_cancel()
    # ---- 9 统计与示意锚点
    t5 = time.perf_counter()
    nn = round(nn_median(E), 3)
    hag32 = hag.astype(np.float32)
    zr = percentiles2(E[:, 2])
    hr = percentiles2(hag32)
    ip = int(np.argmax(hag))
    peak = E[ip].copy()
    peak_hag = float(hag[ip])
    del hag
    Hd, Wd = dtm_w.shape
    pr = min(max(int((peak[1] - dtm_origin[1]) // dtm_cell_m), 0), Hd - 1)
    pc = min(max(int((peak[0] - dtm_origin[0]) // dtm_cell_m), 0), Wd - 1)
    anchor = solve_anchor(cfg.landmark, cfg.fallback_anchor, (float(peak[0]), float(peak[1])), float(dtm_w[pr, pc]))
    lo, hi = colmin(E), colmax(E)
    ext_min = [round(float(v), 3) + 0.0 for v in lo]
    ext_max = [round(float(v), 3) + 0.0 for v in hi]
    precision = precision_report(ext_min, ext_max)
    T["stats"] = time.perf_counter() - t5
    rss["ingest"] = peak_rss_mb()
    # ---- 10 QA 门禁
    raw_ok = True
    raw_detail = {"bytes": raw.files[0]["bytes"], "sha256": raw.files[0]["sha256"]} if raw.files else {}
    gates = run_gates(source_kind=cfg.source_kind, peak_hag_m=peak_hag, precision=precision, leveled=do_level,
                      ground_plane_mad_m=mad, tilt_after_deg=tilt_after, zero_normals_frac=float(zero.mean()),
                      ground_frac=ground_frac, true_north=cfg.true_north, evidence=cfg.evidence, n_out=len(E),
                      n_raw=n_raw, n_nonfinite=n_nonfinite, n_dedup=0, raw_ok=raw_ok, raw_detail=raw_detail)
    Tm = np.eye(4)
    Tm[:3, :3] = np.array(A, np.float64)
    Tm[:3, 3] = -origin
    stats = CloudStats(
        n_raw=n_raw, n_nonfinite=n_nonfinite, n_dedup=0, nn_median_m=nn, z_p1=zr[0], z_p99=zr[1], hag_p1=hr[0],
        hag_p99=hr[1], class_histogram=class_histogram(cls), normals_flipped_frac=float(flipped.mean()),
        zero_normals_frac=float(zero.mean()), ground_frac=ground_frac, tilt_raw_deg=tilt,
        leveled_deg=tilt if do_level else 0.0, tilt_after_deg=tilt_after, ground_plane_mad_m=mad,
        units_heuristic=units_h, up_axis_scores=scores, peak_index=ip, peak_hag_m=peak_hag,
        peak_enu_m=(float(peak[0]), float(peak[1]), float(peak[2])), relief_p1p99_m=relief)
    nc = NormalizedCloud(
        world_id=cfg.world_id, xyz=E, normal=Nn.astype(np.float32), cls=cls, hag=hag32, T_world_source=Tm, origin=origin,
        anchor=anchor, terrain=TerrainGrids(dtm=dtm_w, dtm_origin_xy=dtm_origin, dtm_cell_m=dtm_cell_m, ground_type=ground_type),
        stats=stats, gates=gates, config=cfg, provenance=adapter.provenance(), source_files=list(raw.files),
        timings=T, peak_rss_mb=rss)
    nc.grid_cache = (ci, top, grid_origin)
    return nc
