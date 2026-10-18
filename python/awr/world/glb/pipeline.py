"""GlbAdapter 与 GlbPipeline（M22 §6.2、§9.2；复用 M03 BuildPipeline 与 build_world 的锁、staging、校验与原子发布）。

本版阶段：normalize（配准记录的 `T_world_source`）→ pointcloud（表面彩色采样，经扩展点 6 写 sRGB 点云；网格包围盒的极值顶点并入
采样，使点云、`coordinate.extent` 与 `bounds` 取同一网格包围盒，V-GLB-03、V-GLB-11）→ raycast（`awr-trimesh@1`）→ grids（`dsm_2m`
与 `dsm_2m_n` 由确定性竖向栅格化核在求交网格上生成，每格 4 × 4 采样，§6.6.6，V-GLB-04；`dtm_10m` 仍由采样点云按 D2 规则派生）
→ D1 切片与派生（与六城同一代码）→ quality（质量位栅格、`semantic/quality.geojson` 覆盖多边形、求交网格 `q.u8`，`zones.border`
改为覆盖多边形内缩，§6.6.7）→ appearance（`gltf-glb@2`，`tri.q.u8`）→ occupancy（`awr-occ@1`，覆盖外 unknown，§6.6.10）→ navmesh（行人与车辆 `detour-navmesh@1`，
Node recast-navigation WASM 烘焙，§6.6.11，ADR-196）→ package（world.json 1.1.0：`profile = d3-glb`、`sensorHints`、
`dataset`（含 `derivedFrom[]` 与署名，M22-FR-065））→ D2 全部校验规则与 V-GLB 规则组（M22 §7.8）→ qa 报告 `glb` 段（配准、调平、
V-GLB 逐条结果）。V-GLB-08（许可与署名）失败以 602 WORLD_LICENSE_MISSING 报告。语义、碰撞与锚点阶段随后加入。
"""

from __future__ import annotations

import copy
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from awr.world.georef.frames import mat_to_quat
from awr.world.glb.derive.appearance import HREF as APPEARANCE_HREF
from awr.world.glb.derive.appearance import write_appearance
from awr.world.glb.derive.navmesh import write_navmesh
from awr.world.glb.derive.occupancy import occ_href, write_occupancy
from awr.world.glb.derive.pointcloud import sample_surface
from awr.world.glb.derive.quality import GEOJSON_HREF as QUALITY_GEOJSON_HREF
from awr.world.glb.derive.quality import QualityResult, border_ring, compute_quality, write_quality
from awr.world.glb.derive.raycast import HREF as RAYCAST_HREF
from awr.world.glb.derive.raycast import write_raycast
from awr.world.glb.qa import calibrate, replace_gates
from awr.world.glb.raster import raster_top, trimesh_world
from awr.world.glb.reader import Scene, load_scene
from awr.world.glb.recipe import Recipe, load_recipe, load_registration, verify_raw
from awr.world.ingest.gates import gate_status
from awr.world.ingest.normalize import apply_linear
from awr.world.ingest.types import ConfigError, GateFailed, IngestConfig, NormalizedCloud, ValidationFailed
from awr.world.package.arrays_build import ingest_arrays
from awr.world.package.build import BuildPipeline
from awr.world.package.derivers import LayerSpec
from awr.world.package.jsonio import read_json, sha256_file, write_json
from awr.world.package.manifest import DSM_HREF, DSMN_HREF, DTM_HREF, QA_HREF, build_coordinate
from awr.world.package.streams import add_stream
from awr.world.package.validate import report_reason, validate_world, vglb_results

__all__ = ["CAMERA_PROFILES", "DEM_MODELS", "GlbAdapter", "GlbPipeline", "LicenseMissing", "derived_from", "face_texel_m", "mesh_bounds",
           "sensor_hints"]

DEFAULT_POINTS = 5_000_000
DEFAULT_SEED = 20261012
# 交付分辨率宽度与水平视场（M22 §6.6.12）：纹理受限距离 = texelM × f_px，f_px = (W / 2) / tan(hfov / 2)
CAMERA_PROFILES = {"720p_hfov60": (1280, 60.0), "720p_hfov84": (1280, 84.0), "1080p_hfov84": (1920, 84.0), "4k_hfov84": (3840, 84.0)}
DSM_SAMPLES_K = 4                     # dsm_2m 每格 4 × 4 采样（M22 §6.8）
DEFAULT_VOXEL_M = 1.0                 # 占据体素边长缺省（crop 1 m；配方 build.voxel_m）
NOTE_ODBL_QUALITY = "Water polygons derived from OpenStreetMap (ODbL-1.0, OpenStreetMap contributors)."
NOTICE = "Modified from the original: metric calibration, levelling and derived assets."
NOTICE_ODBL = "OpenStreetMap-derived layers are provided under ODbL-1.0."
# 配方 `osm` 与 `dem` 输入的 derivedFrom 条目（M22 §7.1.4、FR-003、FR-065；05 §14）
OSM_ENTRY = {"name": "OpenStreetMap", "license": "ODbL-1.0", "attribution": "OpenStreetMap contributors",
             "url": "https://www.openstreetmap.org/copyright", "use": "registration,anchors,navgraph"}
DERIVED_USES = ("registration", "anchors", "navgraph", "levelling", "hMsl")     # world.schema dataset.derivedFrom[].use
DEM_MODELS = {"srtm30m": {"name": "SRTM 1 arc-second", "version": "SRTMGL1 v003", "license": "public-domain",
                          "attribution": "NASA/USGS SRTM", "url": "https://doi.org/10.5067/MEaSUREs/SRTM/SRTMGL1.003"},
              "eudem25m": {"name": "EU-DEM v1.1", "version": "v1.1", "license": "Copernicus-data",
                           "attribution": "Produced using Copernicus data and information funded by the European Union - EU-DEM layers",
                           "url": "https://land.copernicus.eu/imagery-in-situ/eu-dem/eu-dem-v1.1"}}


class LicenseMissing(ValidationFailed):
    """V-GLB-08 失败：许可、署名或派生来源字段缺失（602 WORLD_LICENSE_MISSING；构建退出码 1）。"""

    reason_code = 602
    error_code = "WORLD_LICENSE_MISSING"


def derived_from(doc: dict) -> list[dict]:
    """配方 `osm`、`dem` 输入 → `dataset.derivedFrom[]`（逐个按入库清单校验字节数与 sha256，不符为退出码 4）。

    `osm: {path, sha256, extract, use?}`（或其列表），`dem: {path, sha256, model, use?, version?}`（或其列表）；`path` 相对
    `data/raw/d3-glb/`（M22 §7.6.1）。调平 QA 另用的 `qa.osm_extra`（如水体抽取，缺省用途 levelling）与 `qa.dem` 同样登记，
    同一 sha256 只登记一次。
    """
    def items(v: Any) -> list[dict]:
        return [e for e in (v if isinstance(v, list) else [v]) if e]

    def use(e: dict, default: str) -> str:
        """`use` 取契约枚举（registration、anchors、navgraph、levelling、hMsl）的逗号组合；配方中的其他说明词不写入。"""
        toks = [t for t in re.split(r"[,\s]+", str(e.get("use") or default)) if t in DERIVED_USES]
        return ",".join(dict.fromkeys(toks)) or default

    q = doc.get("qa") or {}
    out = []
    seen: set[str] = set()
    for e, extra in [(e, False) for e in items(doc.get("osm"))] + [(e, True) for e in items(q.get("osm_extra"))]:
        if not e.get("extract") and not extra:
            raise ConfigError("recipe osm.extract (extract name and date) missing")
        verify_raw(e["path"], e["sha256"])
        if e["sha256"] in seen:
            continue
        seen.add(e["sha256"])
        out.append({"name": OSM_ENTRY["name"], "version": str(e.get("extract") or f"{Path(e['path']).name} (sha256 {e['sha256'][:12]})"),
                    "license": OSM_ENTRY["license"], "use": use(e, "levelling" if extra else OSM_ENTRY["use"]),
                    "sha256": e["sha256"], "attribution": OSM_ENTRY["attribution"], "url": OSM_ENTRY["url"]})
    for d in items(doc.get("dem")) + items(q.get("dem")):
        m = DEM_MODELS.get(str(d.get("model", "srtm30m")))
        if m is None:
            raise ConfigError(f"recipe dem.model {d.get('model')!r} not in {sorted(DEM_MODELS)}")
        verify_raw(d["path"], d["sha256"])
        if d["sha256"] in seen:
            continue
        seen.add(d["sha256"])
        out.append({"name": m["name"], "version": str(d.get("version", m["version"])), "license": m["license"],
                    "use": use(d, "levelling,hMsl"), "sha256": d["sha256"], "attribution": m["attribution"], "url": m["url"]})
    return out


def mesh_bounds(Vw: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """网格包围盒与取得各极值的顶点下标（x、y、z 的最小与最大，按 argmin/argmax 首个出现，确定性）。"""
    lo, hi = Vw.min(0), Vw.max(0)
    idx = np.unique(np.r_[Vw.argmin(0), Vw.argmax(0)])
    return lo, hi, idx


@dataclass
class GlbSpec:
    """`ingest_arrays` 读取的字段（与 IngestFromArrays 同名）。"""

    xyz_world: np.ndarray
    normals: np.ndarray
    class_index: np.ndarray | None
    source_kind: str
    registration: dict
    anchor_json: dict
    dataset: dict
    rgb: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), np.uint8))


class GlbAdapter:
    kind = "glb"
    staging_nonce = None

    def __init__(self, world_id: str, *, points: int | None = None, recipe: Recipe | None = None) -> None:
        self.recipe = recipe or load_recipe(world_id)
        self.reg = load_registration(self.recipe.registration_path)
        bp = self.recipe.doc.get("build", {}).get("pointcloud", {})
        self.points = int(points or bp.get("target_points", DEFAULT_POINTS))
        self.seed = int(bp.get("seed", DEFAULT_SEED))

    @property
    def pipeline_cls(self) -> type:
        return GlbPipeline

    def config(self) -> IngestConfig:
        r = self.reg["result"]
        return IngestConfig(world_id=self.recipe.world_id, source_kind="reconstruction", units_to_m=float(r["m_per_unit"]), up_axis="+y",
                            level=True, yaw_deg=float(r["yaw_deg"]), true_north="verified", scale_status=self.reg["scale_status"],
                            landmark=None, fallback_anchor=None, evidence=tuple(self.reg.get("evidence", [])),
                            north_evidence=(f"registration {self.reg['method']}: yaw baked into T_world_source",))

    def dataset(self) -> dict:
        """`dataset`（M22-FR-065）；原始文件逐个校验字节数与 sha256，结果在本适配器内缓存（构建内多次引用不重复读取）。"""
        if getattr(self, "_dataset", None) is None:
            self._dataset = self._make_dataset()
        return copy.deepcopy(self._dataset)

    def _make_dataset(self) -> dict:
        d = self.recipe.doc
        sk = d["source"].get("sketchfab", {})
        g = d["source"]["glb"]
        name = Path(g["path"]).name
        df = derived_from(d)
        notice = NOTICE + (" " + NOTICE_ODBL if any(e["license"] == OSM_ENTRY["license"] for e in df) else "")
        out = {"name": f"Sketchfab: {d.get('name', d['world_id'])}", "version": f"sketchfab {sk.get('uid', '')}, sha256 {g['sha256'][:12]}",
               "citation": f"{sk.get('author', '')}, {sk.get('license', '')}".strip(", "), "license": sk.get("license", "unknown"),
               "redistribution": sk.get("license", "").startswith(("CC-BY", "CC0")), "notice": notice,
               "sourceFiles": [{"name": name, "bytes": verify_raw(g["path"], g["sha256"]).stat().st_size, "sha256": g["sha256"]}]}
        if sk.get("uid"):
            out["url"] = f"https://sketchfab.com/3d-models/{sk['uid']}"
        out["derivedFrom"] = df
        return out

    def manifest_extras(self) -> dict:
        d = self.recipe.doc
        return {"name": d.get("name", d["world_id"]), "nameZh": d.get("name_zh"), "tags": ["real", "glb", f"role-{d.get('role', 'event-main')}"],
                "source_dataset": f"Sketchfab: {d.get('name', d['world_id'])}", "default_color_mode": "rgb"}


def _anchor(reg: dict) -> dict:
    r = reg["result"]
    a = r["anchor"]
    h = a.get("h_msl_m")
    h = 0.0 if h is None else float(h)
    return {"kind": "synthetic", "georeferenced": False, "datum": "WGS84", "latDeg": a["lat_deg"], "lonDeg": a["lon_deg"], "hEllipsoidM": h,
            "hMslM": h, "geoid": None, "epoch": None, "uncertaintyM": {"horizontal": 5.0, "vertical": 5.0},
            "label": f"illustrative: origin from {reg['method']} registration to OpenStreetMap; horizontal uncertainty about 5 m; not for navigation"}


def face_texel_m(sc: Scene, Vw: np.ndarray, F: np.ndarray, uv: np.ndarray | None, tex_of_face: np.ndarray) -> np.ndarray | None:
    """逐面纹素边长（m）：世界面积 / 纹理面积（texel²）开方；无纹理或零纹理面积的面为 NaN；场景没有纹理时返回 None。"""
    if uv is None or not (tex_of_face >= 0).any():
        return None
    out = np.full(len(F), np.nan)
    m = tex_of_face >= 0
    f = F[m]
    a = 0.5 * np.linalg.norm(np.cross(Vw[f[:, 1]] - Vw[f[:, 0]], Vw[f[:, 2]] - Vw[f[:, 0]]), axis=1)
    d1, d2 = uv[f[:, 1]] - uv[f[:, 0]], uv[f[:, 2]] - uv[f[:, 0]]
    wh = np.array([sc.textures[t].width * sc.textures[t].height for t in range(len(sc.textures))], np.float64)
    ta = 0.5 * np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]) * wh[tex_of_face[m]]
    with np.errstate(divide="ignore", invalid="ignore"):
        out[m] = np.where(ta > 0, np.sqrt(a / ta), np.nan)
    return out


def sensor_hints(sc: Scene, Vw: np.ndarray, F: np.ndarray, uv: np.ndarray | None, tex_of_face: np.ndarray, edge_p50_m: float) -> dict:
    """texelM：面积加权的纹素边长中位数（每个面：世界面积 / 纹理面积（texel²）开方）；纹理受限距离按相机档换算（M22 §6.6.12）。"""
    texel = None
    if uv is not None and (tex_of_face >= 0).any():
        m = tex_of_face >= 0
        f = F[m]
        a = 0.5 * np.linalg.norm(np.cross(Vw[f[:, 1]] - Vw[f[:, 0]], Vw[f[:, 2]] - Vw[f[:, 0]]), axis=1)
        d1, d2 = uv[f[:, 1]] - uv[f[:, 0]], uv[f[:, 2]] - uv[f[:, 0]]
        wh = np.array([sc.textures[t].width * sc.textures[t].height for t in range(len(sc.textures))], np.float64)
        ta = 0.5 * np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]) * wh[tex_of_face[m]]
        ok = (ta > 0) & (a > 0)
        ts, w = np.sqrt(a[ok] / ta[ok]), a[ok]
        o = np.argsort(ts)
        cw = np.cumsum(w[o])
        texel = float(ts[o][np.searchsorted(cw, cw[-1] / 2)])
    if texel is None:
        texel = edge_p50_m
    rng = {k: round(texel * (W / 2) / math.tan(math.radians(h / 2)), 1) for k, (W, h) in CAMERA_PROFILES.items()}
    return {"texelM": round(texel, 4), "meshEdgeP50M": edge_p50_m, "textureLimitedRangeM": rng, "bakedSunAzElDeg": None,
            "bakedSunConfidence": "unknown"}


class GlbPipeline(BuildPipeline):
    """`BuildPipeline` 的 GLB 版本：ingest 由网格采样替代，派生增加网格图层，清单补写 world.json 1.1.0 字段。"""

    adapter: GlbAdapter

    def run_ingest(self, adapter: GlbAdapter) -> NormalizedCloud:
        self.adapter = adapter
        cfg = adapter.config()
        self.ctx.world_id = cfg.world_id
        self.extras = {**adapter.manifest_extras(), **self.extras}
        g = adapter.recipe.doc["source"]["glb"]
        t0 = time.perf_counter()
        sc = load_scene(verify_raw(g["path"], g["sha256"]))
        T = np.asarray(adapter.reg["result"]["T_world_source"], np.float64)
        A = tuple(tuple(float(v) for v in T[r, :3]) for r in range(3))
        self.scene = sc
        self.Vw_by_prim = [apply_linear(A, p.pos) + T[:3, 3] for p in sc.prims]
        self.Vw = np.concatenate(self.Vw_by_prim)
        _, self.F, _ = sc.merged()
        has_uv = any(p.uv is not None for p in sc.prims)
        self.uv = np.concatenate([p.uv if p.uv is not None else np.zeros((len(p.pos), 2), np.float32) for p in sc.prims]) if has_uv else None
        self.tex_of_face = np.concatenate([np.full(len(p.idx), -1 if p.texture is None else p.texture, np.int32) for p in sc.prims])
        self.stages.append({"name": "normalize", "seconds": round(time.perf_counter() - t0, 3), "peak_rss_mb": 0.0})
        t1 = time.perf_counter()
        xyz, nrm, rgb = sample_surface(self.Vw, self.F, self.uv, self.tex_of_face, sc.texture_bytes, adapter.points, adapter.seed)
        xyz, nrm, rgb = self._with_extremes(xyz, nrm, rgb)
        self.stages.append({"name": "pointcloud", "seconds": round(time.perf_counter() - t1, 3), "peak_rss_mb": 0.0})
        s = float(adapter.reg["result"]["m_per_unit"])
        q = mat_to_quat(T[:3, :3] / s)
        spec = GlbSpec(xyz, nrm, None, "reconstruction", {"method": adapter.reg["method"], "T_world_map": {"s": s, "q": list(q), "t": T[:3, 3].tolist()}},
                       _anchor(adapter.reg), adapter.dataset(), rgb)
        nc = ingest_arrays(spec, cfg, self.ctx, self.params.dtm_cell_m, self.params.dsm_cell_m)
        nc.rgb = rgb
        nc.T_world_source = T
        nc.source_files = [{"name": Path(g["path"]).name, "bytes": spec.dataset["sourceFiles"][0]["bytes"], "points": len(xyz), "sha256": g["sha256"]}]
        for k, src in (("read", "read"), ("ingest", "stats"), ("dtm", "dtm"), ("normals", "normals"), ("classify", "classify")):
            self.stages.append({"name": k, "seconds": round(nc.timings.get(src, 0.0), 3), "peak_rss_mb": 0.0})
        t2 = time.perf_counter()
        self.calib = calibrate(adapter.recipe, adapter.reg, self.Vw, self.F)     # 调平与尺度复测，替换 G-04、G-05（awr.world.glb.qa）
        self.calib.report["leveling"]["seconds"] = round(time.perf_counter() - t2, 3)
        nc.gates = replace_gates(nc.gates, self.calib.gates)
        self.nc = nc
        errs = [gt for gt in nc.gates if not gt.passed and gt.severity == "error"]
        if errs:
            raise GateFailed("; ".join(f"{gt.id} {gt.name}: {gt.value}" for gt in errs))
        self.write_ingest_outputs()
        return nc

    def _with_extremes(self, xyz: np.ndarray, nrm: np.ndarray, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """把网格包围盒的极值顶点替换进采样的前若干点（点数不变）：点云包围盒 = 网格包围盒，`coordinate.extent`、`bounds`、D1 栅格
        原点与范围随之取网格包围盒（V-GLB-03 全部求交顶点在 `bounds` 内、V-GLB-11 外观包围盒与 `bounds` 差 ≤ 1 cm）。
        替换点的法线与颜色取离它最近的采样点（确定性：并列取下标最小者）。"""
        _lo, _hi, idx = mesh_bounds(self.Vw)
        P = self.Vw[idx]
        n = min(len(P), len(xyz))
        if n == 0:
            return xyz, nrm, rgb
        xyz = np.array(xyz, np.float64, copy=True)
        nrm, rgb = np.array(nrm, copy=True), np.array(rgb, copy=True)
        near = [int(np.argmin(((xyz - p) ** 2).sum(1))) for p in P[:n]]
        nn, cc = nrm[near].copy(), rgb[near].copy()
        xyz[:n], nrm[:n] = P[:n], nn
        if len(rgb) == len(xyz):
            rgb[:n] = cc
        return xyz, nrm, rgb

    def write_ingest_outputs(self) -> None:
        from awr.world.terrain.grids import Grid, write_grid

        nc, reg = self.nc, self.adapter.reg
        r = reg["result"]
        coord = build_coordinate(nc, source_dataset=self.extras.get("source_dataset"), qa_status=gate_status(nc.gates))
        coord["source"]["leveledDeg"] = round(float(r["leveling"]["tilt_deg"]), 3)
        coord["source"]["evidence"] = list(reg.get("evidence", []))[:8] or [f"registration {reg['method']}"]
        S = {"s": float(r["m_per_unit"]), "q": [round(float(v), 12) for v in mat_to_quat(nc.T_world_source[:3, :3] / float(r["m_per_unit"]))],
             "t": [round(float(v), 9) for v in nc.T_world_source[:3, 3]]}
        coord["registration"] = {"method": reg["method"], "T_world_map": S}
        write_json(self.stage / "coordinate.json", coord)
        self.coordinate = coord
        self.coordinate_sha = sha256_file(self.stage / "coordinate.json")
        t = nc.terrain
        write_grid(self.stage / DTM_HREF, Grid(t.dtm, t.dtm_origin_xy, t.dtm_cell_m), "dtm", "10m min-z, 9x9 opening, nan-fill")

    def run_grid(self) -> None:
        """raycast 阶段写出求交网格后，以竖向栅格化核在其上生成 `dsm_2m` 与 `dsm_2m_n`（§6.6.6），其余同 D2（源点云、切片排序）。"""
        from awr.world.terrain.grids import Grid, bilinear_on_centres, grid_shape, write_grid

        t0 = time.perf_counter()
        wid = self.nc.world_id
        tile_m = float(self.adapter.recipe.doc.get("build", {}).get("tiling_m", 512.0))
        self.raycast = write_raycast(self.stage, wid, self.coordinate_sha, self.Vw, self.F, tile_m=tile_m)
        self._stage("raycast", time.perf_counter() - t0)
        t1 = time.perf_counter()
        nc = self.nc
        cell = self.params.dsm_cell_m
        lo, hi = nc.xyz.min(0), nc.xyz.max(0)
        origin = (float(lo[0]), float(lo[1]))
        H, W = grid_shape(lo[:2], hi[:2], cell)
        V, F = trimesh_world(self.stage / RAYCAST_HREF / "index.json")
        top, count = raster_top(V, F, origin, cell, H, W, DSM_SAMPLES_K)
        t = nc.terrain
        fill = bilinear_on_centres(Grid(t.dtm, t.dtm_origin_xy, t.dtm_cell_m), origin, cell, H, W)
        dsm = Grid(np.where(count > 0, np.maximum(top, fill), fill).astype(np.float32), origin, cell)
        write_grid(self.stage / DSM_HREF, dsm, "dsm", f"2m max of vertical mesh hits ({DSM_SAMPLES_K}x{DSM_SAMPLES_K} samples per cell, "
                   "awr.world.glb.raster); not below the DTM; cells without hits = DTM bilinear at the cell centre")
        write_grid(self.stage / DSMN_HREF, Grid(count, origin, cell), "occupancy",
                   f"2m number of the {DSM_SAMPLES_K * DSM_SAMPLES_K} vertical samples per cell that hit the mesh; 0 = DSM value is the DTM fill",
                   dtype="uint8", value_frame="unitless", scale=1.0, offset=0.0)
        self.grid_info = {"dsm_max_m": float(dsm.a.max()), "dsm_shape": [H, W], "dsm_hit_cells": int((count > 0).sum())}
        self.rc_mesh = (V, F)                                   # quality、occupancy 阶段沿用写出后的求交网格（与校验器同一份几何）
        self.dsm_n = (count, origin, cell)
        nc.grid_cache = None
        self._write_source()
        self._stage("dsm", time.perf_counter() - t1)

    def _write_source(self) -> None:
        """源点云与切片排序（与 D2 `BuildPipeline.run_grid` 的后半段相同）。"""
        from awr.world.pointcloud.encode import oct16_encode
        from awr.world.pointcloud.octree import morton_sort, world_cube
        from awr.world.pointcloud.source import write_source

        nc = self.nc
        s = nc.stats
        stats = {"nn_median_m": s.nn_median_m, "z_p1_m": s.z_p1, "z_p99_m": s.z_p99, "hag_p1_m": s.hag_p1,
                 "hag_p99_m": s.hag_p99, "class_histogram": s.class_histogram}
        cm, size = world_cube(nc.xyz)
        self.morton = morton_sort(nc.xyz, cm, size)
        self.oct16 = oct16_encode(nc.normal) if nc.normal is not None else None
        write_source(self.stage, nc.world_id, nc.xyz, nc.normal, nc.cls, stats, self.coordinate_sha, morton=self.morton,
                     normals_oct16=self.oct16)

    def run_derive(self) -> None:
        super().run_derive()
        wid = self.nc.world_id
        rc = self.raycast
        build = self.adapter.recipe.doc.get("build", {}) or {}
        t0 = time.perf_counter()
        q = self.run_quality(build)
        self._stage("quality", time.perf_counter() - t0)
        qs = q.stats
        self.layers.append(LayerSpec("quality.raster", "quality", "semantic", "f32-grid@1", self.quality_info["raster_href"],
                                     extra={"stats": {"cellM": q.cell_m, "bits": qs["bits"]}}))
        self.layers.append(LayerSpec("mesh.raycast", "mesh", "geometry", "awr-trimesh@1", RAYCAST_HREF,
                                     extra={"axes": "enu", "stats": {"triangles": rc["counts"]["triangles"], "tiles": rc["counts"]["tiles"]}}))
        t1 = time.perf_counter()
        ap = write_appearance(self.stage, wid, self.coordinate_sha, self.scene, self.Vw_by_prim, self.adapter.recipe.doc["source"]["glb"]["sha256"])
        q_app = np.zeros(int(ap["triangles"]), np.uint8)
        q_app[self.rc_src] = q.tri_q                                    # 外观三角形序（M25 invalid_mask）
        add_stream(self.stage / APPEARANCE_HREF / "index.json", "q", "tri.q.u8", q_app, dtype="uint8", count=len(q_app))
        self._stage("appearance", time.perf_counter() - t1)
        t2 = time.perf_counter()
        V, F = self.rc_mesh
        ext = self.coordinate["extent"]
        v = float(build.get("voxel_m", DEFAULT_VOXEL_M))
        occ = write_occupancy(self.stage, wid, self.coordinate_sha, V, F, voxel_m=v, bounds_min=ext["min"], bounds_max=ext["max"],
                              cover=q.cover, cover_origin_xy=q.dsm_origin_xy, cover_cell_m=q.dsm_cell_m,
                              quality=(q.grid, q.origin_xy, q.cell_m))
        self._stage("occupancy", time.perf_counter() - t2)
        self.layers.append(LayerSpec("occupancy.voxels", "voxel", "geometry", "awr-occ@1", occ_href(v),
                                     extra={"stats": {"voxelM": v, "dims": occ["dims"], "bricks": occ["stats"]["bricks"]}}))
        t3 = time.perf_counter()                                        # navmesh（§6.6.11，ADR-196）：行人与车辆 Detour NavMesh
        nav = write_navmesh(self.stage, wid, self.coordinate, self.coordinate_sha, self.adapter.recipe, mesh=(V, F), tri_q=q.tri_q)
        self.navmesh_qa = nav.qa
        self.layers.extend(nav.layers)
        self._stage("navmesh", time.perf_counter() - t3)
        qx: dict[str, Any] = {"stats": {"features": self.quality_info["features"], "coverAreaM2": qs["cover_area_m2"],
                                        "footprintVertices": qs["footprint_vertices"]}}
        if q.water:
            qx["note"] = NOTE_ODBL_QUALITY
        self.layers.append(LayerSpec("semantic.quality", "quality", "semantic", "geojson/awr-quality@1", QUALITY_GEOJSON_HREF, extra=qx))
        self.layers.append(LayerSpec("mesh.appearance", "mesh", "visual", "gltf-glb@2", APPEARANCE_HREF,
                                     extra={"axes": "gltf-y-up-enu", "bytes": ap["glb"]["bytes"], "stats": {"triangles": ap["triangles"]},
                                            "note": self.adapter.dataset()["citation"][:256]}))

    def run_quality(self, build: dict) -> QualityResult:
        """quality 阶段（M22 §6.6.7）：质量位栅格与覆盖多边形、求交网格 `q.u8`；`zones.border` 改为覆盖多边形内缩（V-GLB-12）。
        配方 `build.quality_cell_m`（缺省同 DSM 格）、`build.quality_simplify_m`（缺省：格 ≥ 1 m 为 1.0 m，否则 0.25 m）。"""
        V, F = self.rc_mesh
        count, origin, cell = self.dsm_n
        rcd = self.stage / RAYCAST_HREF
        self.rc_src = np.fromfile(rcd / "src.u32", "<u4").astype(np.int64)
        tex = face_texel_m(self.scene, self.Vw, self.F, self.uv, self.tex_of_face)
        qcell = float(build.get("quality_cell_m", cell))
        tol = build.get("quality_simplify_m")
        res = compute_quality(V, F, count, origin, cell, cell_m=qcell, texel_m=None if tex is None else tex[self.rc_src],
                              water_rings=self._water_rings(), simplify_m=None if tol is None else float(tol))
        self.quality_info = write_quality(self.stage, self.nc.world_id, self.coordinate_sha, res,
                                          simplify_m=None if tol is None else float(tol))
        add_stream(rcd / "index.json", "q", "q.u8", res.tri_q, dtype="uint8", count=len(F))
        self._inset_border(res, 1.0 if tol is None else float(tol))
        self.quality = res
        return res

    def _water_rings(self) -> list[np.ndarray]:
        """配方 `osm` 与 `qa.osm_extra` 中的水面多边形（世界帧 (E, N)；ODbL 派生）。无 OSM 输入时为空。"""
        from awr.world.georef.types import Anchor
        from awr.world.glb.qa import parse_osm, rings_to_world
        from awr.world.glb.recipe import verify_raw

        d = self.adapter.recipe.doc
        ents = []
        for v in (d.get("osm"), (d.get("qa") or {}).get("osm_extra")):
            ents += [] if not v else ([v] if isinstance(v, dict) else list(v))
        if not ents:
            return []
        osm = parse_osm([verify_raw(e["path"], e.get("sha256")) for e in ents])
        water = [p["ring"] for p in osm.planes if p["kind"] == "water"]
        if not water:
            return []
        a = self.adapter.reg["result"]["anchor"]
        h = a.get("h_msl_m")
        anchor = Anchor(kind="synthetic", datum="WGS84", lat_deg=float(a["lat_deg"]), lon_deg=float(a["lon_deg"]),
                        h_ellipsoid_m=0.0 if h is None else float(h), h_msl_m=None if h is None else float(h))
        return rings_to_world(water, anchor)

    def _inset_border(self, res: QualityResult, tol: float) -> None:
        """`zones.border = inset(footprint, borderInsetM)`（M22 §6.6.7）：保留 D2 的 `max_z_m` 与属性，只替换几何；内缩后为空或
        简化后自交（V-Z-03）时依次减半容差，仍不成立则保留矩形边界。"""
        from awr.world.semantic.zones import check_zone_geometry

        zp = self.stage / "semantic" / "zones.geojson"
        if not zp.exists():
            return
        fc = read_json(zp)
        b = next((f for f in fc.get("features", []) if (f.get("properties") or {}).get("kind") == "border"), None)
        if b is None:
            return
        for t in (tol, tol / 2, tol / 4):
            ring = border_ring(res.cover, res.dsm_origin_xy, res.dsm_cell_m, float(self.params.border_inset_m), t)
            if ring is None:
                return
            old = b["geometry"]
            b["geometry"] = {"type": "Polygon", "coordinates": [[[round(float(x), 3) + 0.0, round(float(y), 3) + 0.0] for x, y in
                                                                  np.vstack([ring, ring[:1]])]]}
            if not [e for e in check_zone_geometry(fc) if e[0] == "V-Z-03"]:
                write_json(zp, fc)
                return
            b["geometry"] = old

    def run_package(self) -> dict[str, Any]:
        w = super().run_package()
        w["schemaVersion"] = "1.1.0"
        w["profile"] = "d3-glb"
        w["sensorHints"] = sensor_hints(self.scene, self.Vw, self.F, self.uv, self.tex_of_face, self.raycast["stats"]["edge_p50_m"])
        q = getattr(self, "quality", None)
        if q is not None:
            w["sensorHints"]["multiLayerColumnFrac"] = float(q.stats["multilayer_cover_frac"])
        w.setdefault("dataset", {}).setdefault("derivedFrom", [])
        write_json(self.stage / "world.json", w)
        self.world = w
        return w

    def run_validate(self, deep: bool = True) -> None:
        """D2 全部规则 + V-GLB 规则组（staging）；V-GLB-08 失败归 602。"""
        try:
            super().run_validate(deep)
        except ValidationFailed as e:
            if self.validation is not None and report_reason(self.validation) == 602:
                raise LicenseMissing(f"602 WORLD_LICENSE_MISSING: {e}") from e
            raise

    def glb_segment(self) -> dict:
        """qa/report.json 的 `glb` 段（M22 §7.3.8）：配准记录摘要、调平记录、分阶段耗时与 V-GLB 逐条结果。"""
        reg = self.adapter.reg
        r = reg["result"]
        calib = getattr(self, "calib", None)                    # 分阶段恢复（restore）时不重算，退回配准记录的调平摘要
        seg = {"registration": {"method": reg["method"], "status": reg.get("status"), "scale_status": reg.get("scale_status"),
                                "m_per_unit": r["m_per_unit"], "yaw_deg": r["yaw_deg"], "t_osm_m": r.get("t_osm_m"),
                                "metrics": reg.get("metrics", {}), "evidence": list(reg.get("evidence", [])),
                                "frame_of": (reg.get("frame_of") or {}).get("world_id"),
                                "accepted_by": reg.get("accepted_by"), "accepted_at": reg.get("accepted_at")},
               "leveling": calib.report["leveling"] if calib is not None else dict(r.get("leveling") or {}),
               "scale": None if calib is None else calib.report["scale"],
               "stages": [{"name": s["name"], "seconds": s["seconds"], "peak_rss_mb": s.get("peak_rss_mb", 0.0)} for s in self.stages],
               "vglb": vglb_results(self.validation) if self.validation is not None else []}
        if getattr(self, "navmesh_qa", None) is not None:
            seg["navmesh"] = self.navmesh_qa
        return seg

    def run_report(self) -> dict:
        """D2 报告后补写 `glb` 段，再以已发布形态（非 staging）补验依赖报告的 V-GLB-02、V-GLB-15，结果并入报告与 `world.json`。"""
        report = super().run_report()
        report["glb"] = self.glb_segment()
        write_json(self.stage / QA_HREF, report)
        again = {"V-GLB-02", "V-GLB-15"}
        post = validate_world(self.stage, rules=again, staging=False)
        rows = {x["id"]: x for x in vglb_results(post) if x["id"] in again and x["status"] != "n/a"}   # 未执行的规则不覆盖 staging 结果
        report["glb"]["vglb"] = [rows.get(x["id"], x) for x in report["glb"]["vglb"]]
        v = report["validation"]
        known = {(e["rule"], e["message"]) for e in v["errors"] + v["warnings"]}
        v["errors"] += [e for e in post.errors if (e["rule"], e["message"]) not in known]
        v["warnings"] += [e for e in post.warnings if (e["rule"], e["message"]) not in known]
        if post.warnings and report["status"] == "pass":
            report["status"] = "warn"
        write_json(self.stage / QA_HREF, report)
        w = read_json(self.stage / "world.json")
        w["qa"]["status"] = report["status"]
        w["qa"]["messages"] = (w["qa"]["messages"] + [f"{e['rule']} {e['message']}" for e in post.warnings
                                                      if f"{e['rule']} {e['message']}" not in w["qa"]["messages"]])[:20]
        write_json(self.stage / "world.json", w)
        self.world = w
        if post.errors:
            raise ValidationFailed("; ".join(f"{e['rule']} {e['where']}: {e['message']}" for e in post.errors[:5]))
        return report
