"""GlbAdapter 与 GlbPipeline（M22 §6.2、§9.2；复用 M03 BuildPipeline 与 build_world 的锁、staging、校验与原子发布）。

本版阶段：normalize（配准记录的 `T_world_source`）→ pointcloud（表面彩色采样，经扩展点 6 写 sRGB 点云）→ D1 栅格、切片与派生
（与六城同一代码）→ raycast（`awr-trimesh@1`）→ appearance（`gltf-glb@2`）→ package（world.json 1.1.0 字段：`profile = d3-glb`、
`sensorHints`、`dataset.derivedFrom`）→ D2 全部校验规则。质量、语义、碰撞、占据、NavMesh 与锚点阶段随后加入；D1 栅格当前由
采样点云按 D2 规则派生，网格竖向栅格化核（§6.6.6）落地后替换。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from awr.world.georef.frames import mat_to_quat
from awr.world.glb.derive.appearance import HREF as APPEARANCE_HREF
from awr.world.glb.derive.appearance import write_appearance
from awr.world.glb.derive.pointcloud import sample_surface
from awr.world.glb.derive.raycast import HREF as RAYCAST_HREF
from awr.world.glb.derive.raycast import write_raycast
from awr.world.glb.reader import Scene, load_scene
from awr.world.glb.recipe import Recipe, load_recipe, load_registration, verify_raw
from awr.world.ingest.gates import gate_status
from awr.world.ingest.normalize import apply_linear
from awr.world.ingest.types import GateFailed, IngestConfig, NormalizedCloud
from awr.world.package.arrays_build import ingest_arrays
from awr.world.package.build import BuildPipeline
from awr.world.package.derivers import LayerSpec
from awr.world.package.jsonio import sha256_file, write_json
from awr.world.package.manifest import DTM_HREF, build_coordinate

__all__ = ["CAMERA_PROFILES", "GlbAdapter", "GlbPipeline", "sensor_hints"]

DEFAULT_POINTS = 5_000_000
DEFAULT_SEED = 20261012
# 交付分辨率宽度与水平视场（M22 §6.6.12）：纹理受限距离 = texelM × f_px，f_px = (W / 2) / tan(hfov / 2)
CAMERA_PROFILES = {"720p_hfov60": (1280, 60.0), "720p_hfov84": (1280, 84.0), "1080p_hfov84": (1920, 84.0), "4k_hfov84": (3840, 84.0)}


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
        d = self.recipe.doc
        sk = d["source"].get("sketchfab", {})
        g = d["source"]["glb"]
        name = Path(g["path"]).name
        out = {"name": f"Sketchfab: {d.get('name', d['world_id'])}", "version": f"sketchfab {sk.get('uid', '')}, sha256 {g['sha256'][:12]}",
               "citation": f"{sk.get('author', '')}, {sk.get('license', '')}".strip(", "), "license": sk.get("license", "unknown"),
               "redistribution": sk.get("license", "").startswith(("CC-BY", "CC0")),
               "notice": "Modified from the original: metric calibration, levelling and derived assets.",
               "sourceFiles": [{"name": name, "bytes": verify_raw(g["path"], g["sha256"]).stat().st_size, "sha256": g["sha256"]}]}
        if sk.get("uid"):
            out["url"] = f"https://sketchfab.com/3d-models/{sk['uid']}"
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
        self.nc = nc
        errs = [gt for gt in nc.gates if not gt.passed and gt.severity == "error"]
        if errs:
            raise GateFailed("; ".join(f"{gt.id} {gt.name}: {gt.value}" for gt in errs))
        self.write_ingest_outputs()
        return nc

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

    def run_derive(self) -> None:
        super().run_derive()
        t0 = time.perf_counter()
        wid = self.nc.world_id
        tile_m = float(self.adapter.recipe.doc.get("build", {}).get("tiling_m", 512.0))
        rc = write_raycast(self.stage, wid, self.coordinate_sha, self.Vw, self.F, tile_m=tile_m)
        self.layers.append(LayerSpec("mesh.raycast", "mesh", "geometry", "awr-trimesh@1", RAYCAST_HREF,
                                     extra={"axes": "enu", "stats": {"triangles": rc["counts"]["triangles"], "tiles": rc["counts"]["tiles"]}}))
        self.raycast = rc
        self._stage("raycast", time.perf_counter() - t0)
        t1 = time.perf_counter()
        ap = write_appearance(self.stage, wid, self.coordinate_sha, self.scene, self.Vw_by_prim, self.adapter.recipe.doc["source"]["glb"]["sha256"])
        self.layers.append(LayerSpec("mesh.appearance", "mesh", "visual", "gltf-glb@2", APPEARANCE_HREF,
                                     extra={"axes": "gltf-y-up-enu", "bytes": ap["glb"]["bytes"], "stats": {"triangles": ap["triangles"]},
                                            "note": self.adapter.dataset()["citation"][:256]}))
        self._stage("appearance", time.perf_counter() - t1)

    def run_package(self) -> dict[str, Any]:
        w = super().run_package()
        w["schemaVersion"] = "1.1.0"
        w["profile"] = "d3-glb"
        w["sensorHints"] = sensor_hints(self.scene, self.Vw, self.F, self.uv, self.tex_of_face, self.raycast["stats"]["edge_p50_m"])
        w.setdefault("dataset", {})["derivedFrom"] = []
        write_json(self.stage / "world.json", w)
        self.world = w
        return w
