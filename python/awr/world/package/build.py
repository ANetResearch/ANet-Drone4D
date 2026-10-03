"""构建流水线（M03 §6.12 状态机）：ingest → grid → tile → derive → package → validate → report → publish。

CLI（`worldpkg build`、分阶段子命令）与 job-worker 共用同一实现；分阶段命令经 staging 目录下的 `.work/`
交换中间数组（float64 规范坐标，切片只从这里取，M03 §6.3），发布前删除。
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from multiprocessing import get_context
from pathlib import Path

import numpy as np

from ..ingest.gates import gate_status
from ..ingest.manifest import DataConfig, default_raw_dir, load_data_config, repo_root, sha256_stream
from ..ingest.pipeline import ingest, peak_rss_mb
from ..ingest.types import (
    CloudStats,
    ConfigError,
    GateFailed,
    GateResult,
    IngestAdapter,
    IngestConfig,
    Landmark,
    LockBusy,
    NormalizedCloud,
    RawDataError,
    StageContext,
    TerrainGrids,
    ValidationFailed,
    WorldpkgError,
)
from ..pointcloud.encode import oct16_encode
from ..pointcloud.npx import colmax, colmin
from ..pointcloud.octree import morton_sort, world_cube
from ..pointcloud.source import write_source
from ..pointcloud.tiler import VISUAL_HREF, tile
from ..pointcloud.writer import RootEntry
from ..qa.report import build_report, first_screen_summary, write_report
from ..semantic.zones import curated_zones_path
from ..terrain.dsm import dsm_index, dsm_max, raw_top
from ..terrain.grids import Grid, bilinear_on_centres, write_grid
from .derivers import DeriveContext, LayerSpec, run_derivers
from .jsonio import read_json, sha256_file, write_json
from .manifest import (
    DSM_HREF,
    DSMN_HREF,
    DTM_HREF,
    HAG_HREF,
    QA_HREF,
    SOURCE_HREF,
    build_coordinate,
    build_world_json,
    camera_home,
    scan_content_files,
)
from .params import BuildParams
from .publish import Publisher
from .validate import validate_world
from .version import SCHEMA_MAJOR, generator_outdated

WORK = ".work"
STAGE_ORDER = ("INGESTING", "GRIDDING", "TILING", "DERIVING", "PACKAGING", "VALIDATING", "REPORTING", "PUBLISHING")


@dataclass
class BuildResult:
    world_id: str
    exit_code: int
    content_version: str | None
    published: bool
    error_code: str | None
    stages: list[dict] = field(default_factory=list)
    message: str = ""
    seconds: float = 0.0
    staging: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


# ================================================================ .work 持久化（分阶段命令、续跑）


def _cfg_to_json(c: IngestConfig) -> dict:
    d = asdict(c)
    return d


def _cfg_from_json(d: dict) -> IngestConfig:
    d = dict(d)
    if d.get("landmark"):
        d["landmark"] = Landmark(**d["landmark"])
    for k in ("evidence", "north_evidence"):
        d[k] = tuple(d.get(k) or ())
    if d.get("fallback_anchor") is not None:
        d["fallback_anchor"] = tuple(d["fallback_anchor"])
    return IngestConfig(**d)


def save_work(stage: Path, nc: NormalizedCloud, extras: dict, created_at: str) -> None:
    w = stage / WORK
    w.mkdir(parents=True, exist_ok=True)
    np.ascontiguousarray(nc.xyz, "<f8").tofile(w / "xyz.f64")
    if nc.normal is not None:
        np.ascontiguousarray(nc.normal, "<f4").tofile(w / "normal.f32")
    nc.cls.astype(np.uint8).tofile(w / "cls.u8")
    np.ascontiguousarray(nc.hag, "<f4").tofile(w / "hag.f32")
    np.ascontiguousarray(nc.terrain.dtm, "<f4").tofile(w / "dtm.f32")
    meta = {"world_id": nc.world_id, "n": len(nc.xyz), "has_normal": nc.normal is not None,
            "T_world_source": nc.T_world_source.tolist(), "origin": nc.origin.tolist(), "anchor": nc.anchor,
            "terrain": {"shape": list(nc.terrain.dtm.shape), "origin_xy": list(nc.terrain.dtm_origin_xy),
                        "cell_m": nc.terrain.dtm_cell_m, "ground_type": nc.terrain.ground_type},
            "stats": asdict(nc.stats), "gates": [g.to_report() for g in nc.gates], "config": _cfg_to_json(nc.config),
            "provenance": nc.provenance, "source_files": nc.source_files, "timings": nc.timings,
            "peak_rss_mb": nc.peak_rss_mb, "extras": extras, "created_at": created_at}
    (w / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def load_work(stage: Path) -> tuple[NormalizedCloud, dict, str]:
    w = stage / WORK
    if not (w / "meta.json").exists():
        raise ConfigError(f"{stage}: 缺少 .work/meta.json（先运行 worldpkg ingest ... --out {stage}）")
    m = json.loads((w / "meta.json").read_text(encoding="utf-8"))
    n = int(m["n"])
    xyz = np.fromfile(w / "xyz.f64", "<f8").reshape(n, 3)
    normal = np.fromfile(w / "normal.f32", "<f4").reshape(n, 3) if m["has_normal"] else None
    cls = np.fromfile(w / "cls.u8", np.uint8)
    hag = np.fromfile(w / "hag.f32", "<f4")
    t = m["terrain"]
    dtm = np.fromfile(w / "dtm.f32", "<f4").reshape(t["shape"])
    st = m["stats"]
    st["peak_enu_m"] = tuple(st["peak_enu_m"])
    st["relief_p1p99_m"] = tuple(st["relief_p1p99_m"])
    gates = [GateResult(g["id"], g["name"], g["value"], g["limit"], g["pass"], g["severity"]) for g in m["gates"]]
    nc = NormalizedCloud(
        world_id=m["world_id"], xyz=xyz, normal=normal, cls=cls, hag=hag, T_world_source=np.asarray(m["T_world_source"]),
        origin=np.asarray(m["origin"]), anchor=m["anchor"],
        terrain=TerrainGrids(dtm=dtm, dtm_origin_xy=tuple(t["origin_xy"]), dtm_cell_m=t["cell_m"], ground_type=t["ground_type"]),
        stats=CloudStats(**st), gates=gates, config=_cfg_from_json(m["config"]), provenance=m["provenance"],
        source_files=m["source_files"], timings=m.get("timings", {}), peak_rss_mb=m.get("peak_rss_mb", {}))
    return nc, m["extras"], m["created_at"]


def _write_state(stage: Path, name: str, doc: dict) -> None:
    (stage / WORK).mkdir(parents=True, exist_ok=True)
    (stage / WORK / f"{name}.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _read_state(stage: Path, name: str) -> dict:
    p = stage / WORK / f"{name}.json"
    if not p.exists():
        raise ConfigError(f"{stage}: 缺少 .work/{name}.json（按 ingest → grid → tile → package 顺序运行）")
    return json.loads(p.read_text(encoding="utf-8"))


# ================================================================ 流水线


class BuildPipeline:
    """一个世界的构建；`stage` 为 staging 目录（或分阶段命令的 --out 目录）。"""

    def __init__(self, stage: Path, params: BuildParams, ctx: StageContext | None = None, *, extras: dict | None = None,
                 created_at: str | None = None):
        self.stage = Path(stage)
        self.params = params
        self.ctx = ctx or StageContext(quiet=True)
        self.extras = extras or {}
        self.created_at = created_at or datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()
        self.nc: NormalizedCloud | None = None
        self.stages: list[dict] = []
        self.coordinate: dict | None = None
        self.coordinate_sha: str | None = None
        self.grid_info: dict = {}
        self.morton = None
        self.roots: list[RootEntry] = []
        self.layers: list[LayerSpec] = []
        self.inputs: list[dict] = []
        self.world: dict | None = None
        self.validation = None

    # ---- 计时
    def _stage(self, name: str, seconds: float) -> None:
        self.stages.append({"name": name, "seconds": round(seconds, 3), "peak_rss_mb": peak_rss_mb()})
        self.ctx.log("info", f"{name:<9} done {seconds:6.2f} s")

    # ---- INGESTING（含 READING）
    def run_ingest(self, adapter: IngestAdapter) -> NormalizedCloud:
        wid = adapter.config().world_id
        self.ctx.world_id = wid
        if hasattr(adapter, "manifest_extras"):
            self.extras = {**adapter.manifest_extras(), **self.extras}
        nc = ingest(adapter, self.ctx, dtm_cell_m=self.params.dtm_cell_m, dsm_cell_m=self.params.dsm_cell_m)
        for k in ("read", "ingest", "dtm", "normals", "classify"):
            v = nc.timings.get(k, 0.0) + (nc.timings.get("stats", 0.0) if k == "ingest" else 0.0)
            self.stages.append({"name": k, "seconds": round(v, 3), "peak_rss_mb": nc.peak_rss_mb.get(k, peak_rss_mb())})
        self.ctx.log("info", f"ingest    done {sum(nc.timings.values()):6.2f} s", points=len(nc.xyz))
        errs = [g for g in nc.gates if not g.passed and g.severity == "error"]
        self.nc = nc
        if errs:
            raise GateFailed("; ".join(f"{g.id} {g.name}: {g.value}" for g in errs))
        self.write_ingest_outputs()
        return nc

    def write_ingest_outputs(self) -> None:
        nc = self.nc
        coord = build_coordinate(nc, source_dataset=self.extras.get("source_dataset"), qa_status=gate_status(nc.gates))
        data = write_json(self.stage / "coordinate.json", coord)
        self.coordinate = coord
        self.coordinate_sha = sha256_file(self.stage / "coordinate.json")
        del data
        t = nc.terrain
        write_grid(self.stage / DTM_HREF, Grid(t.dtm, t.dtm_origin_xy, t.dtm_cell_m), "dtm",
                   "10m min-z, 9x9 opening, nan-fill" if t.ground_type == "dtm" else "flat p0.5 (synthetic ground)")

    # ---- GRIDDING：DSM、dsm_2m_n、源点云
    def run_grid(self) -> None:
        t0 = time.perf_counter()
        nc = self.nc
        E = nc.xyz
        cell = self.params.dsm_cell_m
        cache = nc.grid_cache
        if cache is not None and cache[0].W > 0:
            ci, top, origin = cache
        else:
            ci = dsm_index(E, cell)
            top = raw_top(ci, E[:, 2])
            origin = (float(E[:, 0].min()), float(E[:, 1].min()))
        t = nc.terrain
        dsm, count = dsm_max(top, ci, origin, cell, Grid(t.dtm, t.dtm_origin_xy, t.dtm_cell_m))
        nc.grid_cache = None
        write_grid(self.stage / DSM_HREF, dsm, "dsm", "2m max-z per cell; empty cells = DTM bilinear at the cell centre")
        if self.params.dsm_occupancy:
            write_grid(self.stage / DSMN_HREF, Grid(count, origin, cell), "occupancy",
                       "2m source point count per cell, saturated at 255; 0 = DSM value is the DTM fill",
                       dtype="uint8", value_frame="unitless", scale=1.0, offset=0.0)
        if self.params.hag_grid:                 # HAG 2 m（V0.2 / P2，16 §6.3）：dsm − dtm(格心)
            hag = (dsm.a.astype(np.float64) - bilinear_on_centres(Grid(t.dtm, t.dtm_origin_xy, t.dtm_cell_m), origin, cell,
                                                                    *dsm.a.shape)).astype(np.float32)
            write_grid(self.stage / HAG_HREF, Grid(hag, origin, cell), "hag", "2m dsm - dtm bilinear at the cell centre",
                       value_frame="height-m")
        self.grid_info = {"dsm_max_m": float(dsm.a.max()), "dsm_shape": list(dsm.a.shape)}
        s = nc.stats
        stats = {"nn_median_m": s.nn_median_m, "z_p1_m": s.z_p1, "z_p99_m": s.z_p99, "hag_p1_m": s.hag_p1,
                 "hag_p99_m": s.hag_p99, "class_histogram": s.class_histogram}
        cm, size = world_cube(E)
        self.morton = morton_sort(E, cm, size)
        self.oct16 = oct16_encode(nc.normal) if nc.normal is not None else None
        write_source(self.stage, nc.world_id, E, nc.normal, nc.cls, stats, self.coordinate_sha, morton=self.morton,
                     normals_oct16=self.oct16)
        self._stage("dsm", time.perf_counter() - t0)

    # ---- TILING
    def run_tile(self) -> None:
        t0 = time.perf_counter()
        nc = self.nc
        p = self.params
        s = nc.stats
        self.roots = tile(nc.xyz, nc.normal, nc.cls, self.stage / VISUAL_HREF, G=p.G, leaf=p.leaf, compression=p.compression,
                          forest=p.forest, seed=p.seed, z_range=(s.z_p1, s.z_p99), hag_range=(s.hag_p1, s.hag_p99),
                          nn_median_m=s.nn_median_m, name=nc.world_id, normals_flipped_frac=round(s.normals_flipped_frac, 4),
                          presorted=self.morton, normals_oct16=getattr(self, "oct16", None))
        self.morton = None
        self.oct16 = None
        nodes = sum(r.metadata["anet"]["nodeCount"] for r in self.roots)
        fs = first_screen_summary([r.metadata for r in self.roots])["rule_g"]
        self._stage("tile", time.perf_counter() - t0)
        self.ctx.log("info", "tile summary", roots=len(self.roots), nodes=nodes,
                     depth=max(r.depth for r in self.roots), first_screen_g=f"{fs['points']}/{fs['bytes']}")

    # ---- DERIVING
    def run_derive(self) -> None:
        t0 = time.perf_counter()
        c = self.coordinate
        ctx = DeriveContext(stage=self.stage, world_id=self.nc.world_id if self.nc else c["worldId"], coordinate=c,
                            coordinate_sha256=self.coordinate_sha, bounds_min=c["extent"]["min"], bounds_max=c["extent"]["max"],
                            dsm_max_m=self.grid_info["dsm_max_m"], params=self.params, repo_root=repo_root())
        self.layers = run_derivers(ctx)
        self.inputs.extend(ctx.inputs)
        self._stage("zones", time.perf_counter() - t0)

    # ---- PACKAGING
    def run_package(self) -> dict:
        t0 = time.perf_counter()
        nc = self.nc
        c = self.coordinate
        files = scan_content_files(self.stage)
        vis_bytes = sum(f["bytes"] for f in files if f["path"].startswith(VISUAL_HREF))
        pts = int(sum(r.points for r in self.roots))
        layers = [{"id": "pointcloud.visual", "type": "pointcloud", "role": "visual", "format": "potree2/anet-q16@1",
                   "href": VISUAL_HREF, "status": "ready", "default": True, "bytes": int(vis_bytes), "points": pts,
                   "roots": [r.to_json() for r in self.roots]},
                  {"id": "pointcloud.source", "type": "pointcloud", "role": "geometry", "format": "awr-pts@1",
                   "href": SOURCE_HREF, "status": "ready", "default": False, "points": pts},
                  {"id": "terrain.dtm", "type": "terrain", "role": "geometry", "format": "f32-grid@1", "href": DTM_HREF,
                   "status": "ready", "default": False},
                  {"id": "terrain.dsm", "type": "terrain", "role": "geometry", "format": "f32-grid@1", "href": DSM_HREF,
                   "status": "ready", "default": False}]
        by_id = {L.id: L for L in self.layers}
        layers.extend(by_id.pop(lid).to_json() for lid in ("semantic.classes", "semantic.zones", "environment.config")
                      if lid in by_id)
        if self.params.dsm_occupancy:
            layers.append({"id": "terrain.dsm-n", "type": "terrain", "role": "geometry", "format": "f32-grid@1",
                           "href": DSMN_HREF, "status": "ready", "default": False})
        if self.params.hag_grid:
            layers.append({"id": "terrain.hag", "type": "terrain", "role": "geometry", "format": "f32-grid@1",
                           "href": HAG_HREF, "status": "ready", "default": False})
        layers.extend(L.to_json() for L in by_id.values())
        fs = first_screen_summary([r.metadata for r in self.roots])["rule_g"]
        s = nc.stats
        render = {"defaultColorMode": self.extras.get("default_color_mode", "height"), "zRangeM": [s.z_p1, s.z_p99],
                  "hagRangeM": [s.hag_p1, s.hag_p99], "nnMedianM": s.nn_median_m,
                  "syntheticGroundZ": 0.0 if nc.terrain.ground_type == "synthetic" else None}
        Emin, Emax = colmin(nc.xyz), colmax(nc.xyz)
        stats = {"points": pts, "extentM": [round(float(v), 1) + 0.0 for v in (Emax - Emin)],
                 "areaKm2": round(float((Emax[0] - Emin[0]) * (Emax[1] - Emin[1]) / 1e6), 2),
                 "maxHeightM": round(float(Emax[2]), 1)}
        qa = {"status": gate_status(nc.gates), "href": QA_HREF, "messages": _gate_messages(nc.gates)}
        self.world = build_world_json(
            world_id=nc.world_id, extras=self.extras, created_at=self.created_at, generator_params=self.params.generator_params(),
            dataset=nc.provenance, coordinate_sha256=self.coordinate_sha, scale_status=c["scaleStatus"],
            extent=c["extent"], layers=layers, first_screen={"points": fs["points"], "bytes": fs["bytes"], "requests": fs["requests"]},
            render=render, camera=self.extras.get("camera_home") or camera_home(Emin, Emax),
            stats=stats, qa=qa, files=files)
        write_json(self.stage / "world.json", self.world)
        self._stage("hash", time.perf_counter() - t0)
        return self.world

    # ---- VALIDATING
    def run_validate(self, deep: bool = True) -> None:
        t0 = time.perf_counter()
        work = self.stage / WORK
        hidden = None
        if work.exists():                      # .work 不属于包；校验时临时移开避免扫描开销
            hidden = self.stage.parent / f".{self.stage.name}.work"
            os.replace(work, hidden)
        try:
            rep = validate_world(self.stage, deep=deep, staging=True)
        finally:
            if hidden is not None:
                os.replace(hidden, work)
        self.validation = rep
        self._stage("validate", time.perf_counter() - t0)
        if not rep.ok:
            raise ValidationFailed("; ".join(f"{e['rule']} {e['where']}: {e['message']}" for e in rep.errors[:5]))

    # ---- REPORTING
    def run_report(self) -> dict:
        nc = self.nc
        rep = self.validation
        s = nc.stats
        gates = [g.to_report() for g in nc.gates]
        status = gate_status(nc.gates)
        if status == "pass" and rep is not None and rep.warnings:
            status = "warn"
        sizes = _sizes(self.stage)
        cfg = nc.config
        landmark = None
        if cfg.landmark is not None:
            landmark = {"name": cfg.landmark.name, "hag_m": round(s.peak_hag_m, 1), "expected_m": None,
                        "enu_m": [round(v, 1) for v in s.peak_enu_m]}
        ingest_info = {"units_heuristic": s.units_heuristic, "units_applied": cfg.units_to_m, "up_axis_scores": s.up_axis_scores,
                       "tilt_raw_deg": round(s.tilt_raw_deg, 3), "leveled_deg": round(s.leveled_deg, 3), "yaw_deg": cfg.yaw_deg,
                       "ground_frac": round(s.ground_frac, 4), "normals_flipped_frac": round(s.normals_flipped_frac, 4),
                       "zero_normals_frac": round(s.zero_normals_frac, 5), "nn_median_m": s.nn_median_m,
                       "max_height_m": round(float(nc.xyz[:, 2].max()), 2), "landmark": landmark, "unmapped_las_codes": 0,
                       "points_raw": s.n_raw, "points_nonfinite": s.n_nonfinite, "points_dedup": s.n_dedup,
                       "ground_plane_mad_m": s.ground_plane_mad_m}
        inputs = [{"name": f["name"], "bytes": int(f["bytes"]), "sha256": f["sha256"]} for f in nc.source_files] + self.inputs
        validation = {"errors": rep.errors if rep else [], "warnings": rep.warnings if rep else [],
                      "rules_checked": rep.rules_checked if rep else 0, "deep": bool(rep.deep) if rep else False,
                      "seconds": round(rep.seconds, 3) if rep else 0.0}
        report = build_report(world_id=nc.world_id, content_version=self.world["contentVersion"], status=status,
                              generator=self.world["generator"], inputs=inputs, stages=self.stages, ingest=ingest_info,
                              gates=gates, validation=validation, first_screen=first_screen_summary([r.metadata for r in self.roots]),
                              sizes=sizes)
        write_report(self.stage / QA_HREF, report)
        self.world["qa"] = {"status": status, "href": QA_HREF,
                            "messages": (_gate_messages(nc.gates) + [f"{w['rule']} {w['message']}" for w in validation["warnings"]])[:20]}
        write_json(self.stage / "world.json", self.world)
        return report

    # ---- 分阶段命令用的状态保存与恢复
    def save_after(self, name: str) -> None:
        if name == "ingest":
            save_work(self.stage, self.nc, self.extras, self.created_at)
            _write_state(self.stage, "ingest", {"coordinate_sha": self.coordinate_sha, "stages": self.stages})
        elif name == "grid":
            _write_state(self.stage, "grid", {"grid_info": self.grid_info, "stages": self.stages})
        elif name == "tile":
            _write_state(self.stage, "tile", {"roots": [dict(r.to_json(), metadata=r.metadata) for r in self.roots],
                                              "stages": self.stages})

    def restore(self, upto: str) -> None:
        self.nc, extras, self.created_at = load_work(self.stage)
        self.extras = {**extras, **self.extras}
        self.ctx.world_id = self.nc.world_id
        st = _read_state(self.stage, "ingest")
        self.coordinate_sha = st["coordinate_sha"]
        self.coordinate = read_json(self.stage / "coordinate.json")
        self.stages = st["stages"]
        if upto in ("tile", "package"):
            g = _read_state(self.stage, "grid")
            self.grid_info = g["grid_info"]
            self.stages = g["stages"]
        if upto == "package":
            t = _read_state(self.stage, "tile")
            self.roots = [RootEntry(name=r["name"], href=r["href"], cube_min=np.asarray(r["cubeMin"]), cube_size=r["cubeSize"],
                                    points=r["points"], depth=r["depth"], first_screen_bytes=r["firstScreenBytes"],
                                    metadata=r["metadata"]) for r in t["roots"]]
            self.stages = t["stages"]


def _gate_messages(gates: list[GateResult]) -> list[str]:
    return [f"{g.id} {g.name}: {g.value}" for g in gates if not g.passed and g.severity in ("error", "warn")]


def _sizes(stage: Path) -> dict:
    def total(prefix: str, suffix: str = "") -> int:
        n = 0
        for p in stage.rglob("*"):
            rel = p.relative_to(stage).as_posix()
            if p.is_file() and rel.startswith(prefix) and rel.endswith(suffix) and not rel.startswith(WORK):
                n += p.stat().st_size
        return n

    all_bytes = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file() and not p.relative_to(stage).as_posix().startswith(WORK))
    return {"octree_bytes": total(VISUAL_HREF, "octree.bin"), "hierarchy_bytes": total(VISUAL_HREF, "hierarchy.bin")
            + total(VISUAL_HREF, "hierarchy_ext.bin"), "source_bytes": total("geometry/pointcloud/source/"),
            "dsm_bytes": total("geometry/terrain/dsm_2m"), "dtm_bytes": total("geometry/terrain/dtm_10m"), "total_bytes": all_bytes}


# ================================================================ 单城构建与发布


def _exit_reason(e: Exception) -> tuple[int, str, str]:
    if isinstance(e, RawDataError):
        return 4, e.error_code, "raw_missing"
    if isinstance(e, GateFailed):
        return 2, "INGEST_GATE_FAILED", "gate_failed"
    if isinstance(e, ValidationFailed):
        return 1, "VALIDATE_FAILED", "validate_failed"
    if isinstance(e, WorldpkgError):
        return e.exit_code, getattr(e, "error_code", "IO_ERROR"), "io_error"
    return 3, "IO_ERROR", "io_error"


def _raw_status(adapter) -> list[dict] | None:
    try:
        p = adapter.raw_path()
        st = p.stat()
        files = getattr(adapter, "_files", None) or []
        sha = files[0]["sha256"] if files else None
        return [{"name": p.name, "bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}]
    except (AttributeError, OSError):
        return None


def build_world(adapter: IngestAdapter, worlds_dir: Path, *, params: BuildParams | None = None,
                ctx: StageContext | None = None) -> BuildResult:
    """CLI 与 job-worker 共用：锁 → 启动恢复 → staging → 各阶段 → deep 校验 → 报告 → 原子发布 → `.status`。"""
    t0 = time.perf_counter()
    params = params or BuildParams.from_config()
    if hasattr(adapter, "build_params"):          # ArraysAdapter：generator.params 追加 recon（M03-FR-021）
        params = adapter.build_params(params)
    pipe_cls = getattr(adapter, "pipeline_cls", None) or BuildPipeline
    wid = adapter.config().world_id
    ctx = ctx or StageContext(world_id=wid)
    ctx.world_id = wid
    worlds_dir = Path(worlds_dir)
    pub = Publisher(worlds_dir, wid)
    pipe = None
    stg = None
    prog = getattr(ctx, "progress", None) or (lambda f: None)
    try:
        with pub.lock():
            pub.recover(shallow_ok=lambda p: validate_world(p, staging=True).ok)   # .trash 目录名为 <id>-<ts>
            stg = pub.new_staging(nonce=getattr(adapter, "staging_nonce", None))
            pipe = pipe_cls(stg, params, ctx)
            try:
                pipe.run_ingest(adapter)
                prog(0.30)
                ctx.check_cancel()
                pipe.run_grid()
                prog(0.40)
                ctx.check_cancel()
                pipe.run_tile()
                prog(0.70)
                ctx.check_cancel()
                pipe.run_derive()
                pipe.run_package()
                prog(0.80)
                ctx.check_cancel()
                pipe.run_validate(deep=True)
                prog(0.95)
                pipe.run_report()
                ctx.check_cancel()                        # 原子发布前的最后一个检查点
                shutil.rmtree(stg / WORK, ignore_errors=True)
                pub.publish(stg)
                cv = pipe.world["contentVersion"]
                pub.write_status("ready", reason=None, exit_code=0, content_version=cv, deep=True, raw=_raw_status(adapter))
                secs = time.perf_counter() - t0
                prog(1.0)
                ctx.log("info", f"published {cv} in {secs:.1f} s")
                return BuildResult(wid, 0, cv, True, None, pipe.stages, "", round(secs, 3))
            except (WorldpkgError, OSError, ValueError) as e:
                code, err, reason = _exit_reason(e)
                if pipe is not None and pipe.world is not None and pipe.validation is not None and isinstance(e, ValidationFailed):
                    with contextlib.suppress(Exception):          # 报告只用于诊断
                        pipe.run_report()
                keep = params.keep_staging and stg is not None
                if not keep and stg is not None:
                    shutil.rmtree(stg, ignore_errors=True)
                old = pub.read_status()
                if (worlds_dir / wid / "world.json").exists():
                    status = old.get("status", "ready") if old else "ready"
                    pub.write_status(status, reason=old.get("reason") if old else None, exit_code=code,
                                     content_version=old.get("content_version") if old else None, deep=False,
                                     raw=old.get("raw") if old else None)
                else:
                    pub.write_status("failed", reason=reason, exit_code=code, content_version=None, deep=False)
                ctx.log("error", f"build failed ({err}, exit {code}): {e}")
                return BuildResult(wid, code, None, False, err, pipe.stages if pipe else [], str(e),
                                   round(time.perf_counter() - t0, 3), str(stg) if keep else None)
            except BaseException:                         # 取消（JobCancelled）或其他异常：清理 staging 后原样抛出
                if stg is not None and not params.keep_staging:
                    shutil.rmtree(stg, ignore_errors=True)
                raise
    except LockBusy as e:
        ctx.log("error", str(e))
        return BuildResult(wid, 3, None, False, "BUILD_IN_PROGRESS", [], str(e), round(time.perf_counter() - t0, 3))


# ================================================================ --missing 判定与并行构建


def missing_reason(worlds_dir: Path, wid: str, *, raw_dir: Path, data: DataConfig, deep: bool | None = None,
                   synthetic: dict | None = None) -> tuple[str | None, dict]:
    """16 §3.5 第 2 条 6 个条件；返回 (原因 或 None, 详情)。原因取 `.status.reason` 枚举值，`missing` 表示条件 ①。

    合成世界（`synthetic` 为当前 `SynthSpec.params()`，ADR-077）的条件 ⑤ 改为"`generator.params.synthetic` 与当前配置不同"
    （生成器版本、种子、尺寸或目标点数变化），原因同为 `raw_changed`；不读原始文件。"""
    deep = (os.environ.get("WORLDPKG_VERIFY") == "deep") if deep is None else deep
    wd = Path(worlds_dir) / wid
    info: dict = {}
    if not (wd / "world.json").exists():
        return "missing", info
    try:
        w = read_json(wd / "world.json")
    except (OSError, json.JSONDecodeError):
        return "validate_failed", info
    ver = str((w.get("generator") or {}).get("version", "0"))
    if generator_outdated(ver):
        return "generator_outdated", {"version": ver}
    if str(w.get("schemaVersion", "0")).split(".")[0] != str(SCHEMA_MAJOR):
        return "schema_major", {"schemaVersion": w.get("schemaVersion")}
    rep = validate_world(wd, deep=deep)
    info["validation"] = {"errors": len(rep.errors), "warnings": len(rep.warnings), "seconds": round(rep.seconds, 3)}
    if not rep.ok:
        info["first_error"] = rep.errors[0]
        return "validate_failed", info
    if synthetic is not None:
        have = ((w.get("generator") or {}).get("params") or {}).get("synthetic")
        if have != synthetic:
            info["synthetic"] = {"have": have, "want": synthetic}
            return "raw_changed", info
    spec = data.by_world(wid)
    raw = Path(raw_dir) / spec.name if spec else None
    if raw is not None and raw.exists():
        pub = Publisher(worlds_dir, wid)
        st = pub.read_status() or {}
        cached = next((r for r in st.get("raw") or [] if r.get("name") == spec.name), None)
        stt = raw.stat()
        if cached and cached.get("sha256") and cached.get("bytes") == stt.st_size and cached.get("mtime_ns") == stt.st_mtime_ns:
            sha = cached["sha256"]
        else:
            sha = sha256_stream(raw)
            info["raw_cache"] = {"name": spec.name, "bytes": stt.st_size, "mtime_ns": stt.st_mtime_ns, "sha256": sha}
        want = {f.get("name"): f.get("sha256") for f in (w.get("dataset") or {}).get("sourceFiles") or []}
        if want.get(spec.name) != sha:
            return "raw_changed", info
    zp = curated_zones_path(repo_root(), wid)
    try:
        zones = read_json(wd / "semantic/zones.geojson")
        have = zones["awr"].get("source_sha256")
    except (OSError, KeyError, json.JSONDecodeError):
        return "validate_failed", info
    now = sha256_file(zp) if zp.exists() else None
    if now != have:
        return "zones_changed", info
    return None, info


def _child_init() -> None:
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"


def _build_one(args: tuple) -> dict:
    wid, worlds_dir, raw_dir, params, log_json = args
    from ..ingest.synthetic import SyntheticAdapter, synth_specs
    from ..ingest.urbanscene3d import UrbanScene3DAdapter

    ctx = StageContext(world_id=wid, log_json=log_json)
    try:
        synth = synth_specs()
        adapter = SyntheticAdapter(synth[wid]) if wid in synth else UrbanScene3DAdapter(wid, Path(raw_dir))
    except WorldpkgError as e:
        return BuildResult(wid, e.exit_code, None, False, getattr(e, "error_code", "IO_ERROR"), [], str(e)).to_json()
    return build_world(adapter, Path(worlds_dir), params=params, ctx=ctx).to_json()


def build_missing(worlds_dir: Path, raw_dir: Path | None = None, *, jobs: int = 3, cities: list[str] | None = None,
                  missing_only: bool = True, params: BuildParams | None = None, log_json: bool = False,
                  ctx: StageContext | None = None) -> dict[str, BuildResult]:
    """`build --missing`：逐城判定 6 个条件，按原始点数降序提交到 spawn 进程池（每个子进程单线程）。"""
    data = load_data_config()
    raw_dir = Path(raw_dir) if raw_dir else default_raw_dir()
    worlds_dir = Path(worlds_dir)
    worlds_dir.mkdir(parents=True, exist_ok=True)
    params = params or BuildParams.from_config()
    ctx = ctx or StageContext(world_id="worldpkg", log_json=log_json)
    todo: list[str] = []
    results: dict[str, BuildResult] = {}
    for spec in sorted(data.files, key=lambda f: -f.points):
        wid = spec.world_id
        if cities and wid not in cities:
            continue
        reason = "forced"
        if missing_only:
            reason, info = missing_reason(worlds_dir, wid, raw_dir=raw_dir, data=data)
            if info.get("raw_cache"):
                pub = Publisher(worlds_dir, wid)
                st = pub.read_status()
                if st and reason is None:
                    pub.write_status(st["status"], reason=st.get("reason"), exit_code=st.get("exit_code", 0),
                                     content_version=st.get("content_version"), deep=st.get("deep", False), raw=[info["raw_cache"]])
            if reason is None:
                ctx.log("info", f"{wid}: up to date")
                cv = read_json(worlds_dir / wid / "world.json")["contentVersion"]
                Publisher(worlds_dir, wid).write_status("ready", reason=None, exit_code=0, content_version=cv, deep=False,
                                                        raw=(Publisher(worlds_dir, wid).read_status() or {}).get("raw"))
                results[wid] = BuildResult(wid, 0, cv, False, None)
                continue
            if (worlds_dir / wid / "world.json").exists() and reason not in ("missing",):
                Publisher(worlds_dir, wid).write_status("invalid", reason=reason, exit_code=0,
                                                        content_version=read_json(worlds_dir / wid / "world.json").get("contentVersion"),
                                                        deep=False)
        if not (raw_dir / spec.name).exists():
            msg = f"{wid}: 原始数据缺失（{raw_dir / spec.name}）；修复：make fetch-data"
            ctx.log("error", msg)
            if not (worlds_dir / wid / "world.json").exists():
                Publisher(worlds_dir, wid).write_status("failed", reason="raw_missing", exit_code=4, content_version=None, deep=False)
            results[wid] = BuildResult(wid, 4, None, False, "RAW_MISSING", [], msg)
            continue
        ctx.log("info", f"{wid}: rebuild ({reason})")
        todo.append(wid)
    todo += _synthetic_todo(worlds_dir, raw_dir, data, cities=cities, missing_only=missing_only, results=results, ctx=ctx)
    if not todo:
        return results
    jobs = max(1, min(int(jobs), 8))
    try:
        if os.getloadavg()[0] > 4.0 and jobs > 1:
            ctx.log("warn", f"1-minute loadavg {os.getloadavg()[0]:.1f} > 4: --jobs reduced to 1 (M03 R-2)")
            jobs = 1
    except OSError:
        pass
    args = [(wid, str(worlds_dir), str(raw_dir), params, log_json) for wid in todo]
    if jobs == 1 or len(args) == 1:
        for a in args:
            results[a[0]] = BuildResult(**_build_one(a))
    else:
        with ProcessPoolExecutor(max_workers=jobs, mp_context=get_context("spawn"), initializer=_child_init) as ex:
            for a, res in zip(args, ex.map(_build_one, args), strict=True):
                results[a[0]] = BuildResult(**res)
    return results


def _synthetic_todo(worlds_dir: Path, raw_dir: Path, data: DataConfig, *, cities: list[str] | None, missing_only: bool,
                    results: dict[str, BuildResult], ctx: StageContext) -> list[str]:
    """合成世界（configs/worldpkg.yaml 的 synthetic 段，ADR-077）的构建判定：

    - 显式点名（`worldpkg build synthcity`，或 `AWR_WORLD=synthcity`）：按 --missing 判定或强制；
    - `--missing` 且未点名：已发布的合成世界保持新鲜（条件 ①–⑥）；未发布时只在回退需要时生成（`defaults.fallback_needed`：
      主默认世界未发布且其原始数据不在本机）。有原始数据时不自动生成。
    """
    from ..ingest.synthetic import synth_specs
    from .defaults import fallback_needed

    out: list[str] = []
    want_env = (os.environ.get("AWR_WORLD") or "").strip()
    for wid, spec in synth_specs().items():
        named = bool(cities) and wid in cities
        if cities and not named:
            continue
        published = (worlds_dir / wid / "world.json").exists()
        if not named and missing_only and not published and wid != want_env \
                and not (fallback_needed(worlds_dir, raw_dir, data) and wid == _fallback_world()):
            continue
        if not named and not missing_only and not published:
            continue                                     # worlds-force 只重建已有的合成世界
        reason: str | None = "forced"
        if missing_only:
            reason, _info = missing_reason(worlds_dir, wid, raw_dir=raw_dir, data=data, synthetic=spec.params())
            if reason is None:
                ctx.log("info", f"{wid}: up to date")
                cv = read_json(worlds_dir / wid / "world.json")["contentVersion"]
                Publisher(worlds_dir, wid).write_status("ready", reason=None, exit_code=0, content_version=cv, deep=False)
                results[wid] = BuildResult(wid, 0, cv, False, None)
                continue
            if published and reason != "missing":
                Publisher(worlds_dir, wid).write_status("invalid", reason=reason, exit_code=0,
                                                        content_version=read_json(worlds_dir / wid / "world.json").get("contentVersion"),
                                                        deep=False)
        ctx.log("info", f"{wid}: generate ({reason}; synthetic, ADR-077)")
        out.append(wid)
    return out


def _fallback_world() -> str | None:
    from .defaults import runtime_run_defaults

    return runtime_run_defaults()[2]


def overall_exit(results: dict[str, BuildResult]) -> int:
    return max((r.exit_code for r in results.values()), default=0)


def print_summary(results: dict[str, BuildResult], out=sys.stderr) -> None:
    for wid, r in results.items():
        state = "OK  " if r.exit_code == 0 else "FAIL"
        extra = f" cv={r.content_version}" if r.content_version else ""
        built = " built" if r.published else ""
        print(f"{state} {wid:<13} exit={r.exit_code}{extra}{built} {r.seconds:.1f} s {r.message}".rstrip(), file=out)

