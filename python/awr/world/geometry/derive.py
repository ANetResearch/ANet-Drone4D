"""装载期派生（M04 §6.4.1、FR-002 至 FR-004）：观测计数、观测感知闭运算 dsm_eff、Height_map、金字塔、
1 格膨胀、默认 1 m 细校验栅格与 zone 栅格，以及派生 QA。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .grids import Grid
from .heightmap import _ndi, dilate3, heightmap, inflate, max_pyramid
from .types import GeoLoadError, GeoParams
from .zones import ZoneIndex


@dataclass
class DerivedSet:
    obs_n: np.ndarray
    dsm_eff: np.ndarray
    dsm_dil1: np.ndarray
    hm: np.ndarray
    pyr_dsm: list[np.ndarray]
    pyr_hm: list[np.ndarray]
    pyrdil_hm: list[np.ndarray | None]
    inflated_1m: np.ndarray
    pyr_inflated_1m: list[np.ndarray]
    zone_raster: np.ndarray
    qa: dict


def obs_counts(world_dir: Path, dsm: Grid, layers: dict[str, dict]) -> tuple[np.ndarray, str]:
    """每格观测点数（u8，饱和 255）：优先 `dsm_2m_n`（M03 D1-ext），否则从源点云 `xyz.f32` 统计（16 §6.1 的唯一例外读取）。"""
    L = layers.get("terrain.dsm-n")
    if L is not None:
        g = Grid.open(Path(world_dir) / L["href"], expect_kind="occupancy", expect_dtype="uint8", expect_frame=None)
        if g.a.shape != dsm.a.shape or g.cell != dsm.cell or abs(g.x0 - dsm.x0) > 1e-6 or abs(g.y0 - dsm.y0) > 1e-6:
            raise GeoLoadError("GEO_GRID_INVALID", "dsm_2m_n grid differs from dsm_2m")
        return np.asarray(g.a, np.uint8), "dsm_2m_n"
    S = layers.get("pointcloud.source")
    if S is None:
        raise GeoLoadError("GEO_MISSING_FILE", "neither terrain.dsm-n nor pointcloud.source is available")
    src = Path(world_dir) / S["href"]
    sc = json.loads(src.read_text(encoding="utf-8"))
    n = int(sc["count"])
    xyz = np.fromfile(src.parent / "xyz.f32", "<f4")
    if xyz.size != 3 * n:
        raise GeoLoadError("GEO_GRID_INVALID", "xyz.f32 size mismatch")
    xyz = xyz.reshape(n, 3)
    c = np.clip(np.floor((xyz[:, 0].astype(np.float64) - dsm.x0) / dsm.cell).astype(np.int64), 0, dsm.w - 1)
    r = np.clip(np.floor((xyz[:, 1].astype(np.float64) - dsm.y0) / dsm.cell).astype(np.int64), 0, dsm.h - 1)
    cnt = np.bincount(r * dsm.w + c, minlength=dsm.h * dsm.w)
    return np.minimum(cnt, 255).astype(np.uint8).reshape(dsm.h, dsm.w), "source"


def derive_arrays(dsm: Grid, dtm: Grid, zones: ZoneIndex, p: GeoParams, n: np.ndarray) -> DerivedSet:
    a = np.asarray(dsm.a, np.float32)
    hag = a.astype(np.float64) - dtm.centres_bilinear(dsm)
    obs_ground = (n >= 1) & (hag < p.obs_ground_hag_m)
    if p.close_k and p.close_k > 1:
        closed = _ndi().grey_closing(a, size=(p.close_k, p.close_k), mode="nearest").astype(np.float32)
        eff = np.where(obs_ground, a, closed).astype(np.float32)
    else:
        eff = a.copy()
    hm = heightmap(eff, p.hm_dilate_cells, p.hm_safe_m)
    pyr_dsm = max_pyramid(eff, p.pyr_min_cells)
    pyr_hm = max_pyramid(hm, p.pyr_min_cells)
    pyrdil_hm = dilate3(pyr_hm, 2)
    dsm_dil1 = _ndi().maximum_filter(eff, size=3, mode="nearest").astype(np.float32)
    infl = inflate(eff, 1.0, dsm.cell)
    pyr_infl = max_pyramid(infl, p.pyr_min_cells)
    width_m, height_m = dsm.w * dsm.cell, dsm.h * dsm.cell
    zones.build_raster(dsm.x0, dsm.y0, width_m, height_m, p.zone_raster_cell_m)
    zr = zones.raster.mask.astype(np.uint8)
    qa = derive_qa(a, eff, hag, n)
    return DerivedSet(obs_n=n, dsm_eff=eff, dsm_dil1=dsm_dil1, hm=hm, pyr_dsm=pyr_dsm, pyr_hm=pyr_hm, pyrdil_hm=pyrdil_hm,
                      inflated_1m=infl, pyr_inflated_1m=pyr_infl, zone_raster=zr, qa=qa)


def derive_qa(dsm: np.ndarray, eff: np.ndarray, hag: np.ndarray, n: np.ndarray) -> dict:
    """空格率、屋顶坑（空格、自身非建筑、8 邻域 ≥ 6 个 HAG > 5 m 的建筑格）填补比例、抬升比例（M04 §6.11 表 1 口径）。"""
    empty = n == 0
    bld = hag > 5.0
    nb = _ndi().convolve(bld.astype(np.int16), np.ones((3, 3), np.int16), mode="nearest") - bld
    pits = empty & ~bld & (nb >= 6)
    raised = (eff.astype(np.float64) - dsm) > 5.0
    obs_ground = (n >= 1) & (hag < 2.0)
    n_bld = max(1, int(bld.sum()))
    return {"empty_frac": round(float(empty.mean()), 5),
            "pits_frac_of_building": round(float(pits.sum()) / n_bld, 5),
            "pits_left_frac_of_building": round(float((pits & ~raised).sum()) / n_bld, 5),
            "pits_filled_frac": round(float((pits & raised).sum()) / max(1, int(pits.sum())), 5),
            "raised_frac": round(float(raised.mean()), 5),
            "obs_ground_raised": int(((eff != dsm) & obs_ground).sum()),
            "dsm_max_m": round(float(eff.max()), 3)}
