"""Transitional stand-in for M03 `ArraysAdapter` + `build_world` (M03-FR-021 not delivered yet; request M01-to-M03).

Used by TILING / PACKAGING only while `awr.world.ingest` does not export `IngestFromArrays` and `ArraysAdapter`. It follows
the M03 §6.13 (3) contract with M03 library pieces only (no copy of their formulas): the M03 `BuildPipeline` runs
unchanged for gridding, tiling, derivers, packaging, deep validation, report and atomic publish; only the ingest step is
replaced because a derived world keeps the source world frame (steps 2-5 and the origin re-selection are skipped,
AWR-16 §14.1 "derived worlds"), and the manifest receives the recon fields of M01 §6.8:
`coordinate.source.kind = reconstruction`, `registration`, the source anchor / `T_ecef_world` / `trueNorth`,
`scaleStatus`, the `reconstruction.<session_id>` layer, `tags`, `dataset.notice`, `generator.params.recon` and the
camera home. Staging is `worlds/.staging/<target>-<job_id>` (M03-FR-042 job nonce), held under the M03 per-world lock
from TILING through PACKAGING.
"""

from __future__ import annotations

import contextlib
import resource
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from awr.world.georef.frames import precision_report
from awr.world.ingest.classify import classify
from awr.world.ingest.gates import run_gates
from awr.world.ingest.normalize import angle_to_z_deg
from awr.world.ingest.normals import fix_normals
from awr.world.ingest.stats import class_histogram, nn_median, percentiles2
from awr.world.ingest.types import (
    CloudStats,
    GateFailed,
    IngestConfig,
    NormalizedCloud,
    RawCloud,
    StageContext,
    TerrainGrids,
)
from awr.world.package.build import WORK, BuildPipeline, BuildResult
from awr.world.package.derivers import LayerSpec
from awr.world.package.jsonio import sha256_file, write_json
from awr.world.package.manifest import build_coordinate
from awr.world.package.params import BuildParams
from awr.world.package.publish import Publisher
from awr.world.package.validate import validate_world
from awr.world.pointcloud.npx import colmax, colmin
from awr.world.terrain.dsm import dsm_index, raw_top
from awr.world.terrain.dtm import dtm_opening

from .package import ReconIngestSpec

__all__ = ["BridgeBuild", "ReconArraysAdapter"]


def _rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)


class ReconArraysAdapter:
    """IngestAdapter over a ReconIngestSpec (M03 §7.2 IngestAdapter protocol)."""

    kind = "arrays"

    def __init__(self, spec: ReconIngestSpec) -> None:
        self.spec = spec

    def config(self) -> IngestConfig:
        s = self.spec
        tn = s.true_north or {}
        return IngestConfig(world_id=s.world_id, source_kind="reconstruction", units_to_m=float(s.registration["T_world_map"]["s"]),
                            up_axis="+z", level=False, yaw_deg=0.0, true_north=tn.get("confidence", "assumed"),
                            scale_status=s.scale_status, landmark=None, fallback_anchor=None,
                            evidence=(s.session_id, f"registration {s.registration['method']}"),
                            north_evidence=tuple(tn.get("evidence") or ()))

    def load(self) -> RawCloud:
        return RawCloud(xyz=self.spec.xyz_world, normal=self.spec.normals, files=[])

    def provenance(self) -> dict:
        return self.spec.dataset

    def manifest_extras(self) -> dict:
        s = self.spec
        ex = {"name": s.name, "nameZh": s.name_zh, "tags": list(s.tags), "source_dataset": None}
        if s.camera_home:
            ex["camera_home"] = s.camera_home
        return ex


class _ReconParams(BuildParams):
    """BuildParams whose generator.params also carries `recon` (M01 §6.8 generator row)."""

    recon: dict | None = None

    def generator_params(self) -> dict:
        d = super().generator_params()
        if self.recon:
            d["recon"] = dict(self.recon)
        return d


def _ingest_arrays(spec: ReconIngestSpec, cfg: IngestConfig, ctx: StageContext, dtm_cell_m: float, dsm_cell_m: float) -> NormalizedCloud:
    """Steps 6-10 of the M03 ingest on world-frame arrays; origin and frame are the source world's (no re-selection)."""
    T: dict[str, float] = {}
    rss: dict[str, float] = {}
    t0 = time.perf_counter()
    E = np.ascontiguousarray(spec.xyz_world, dtype=np.float64)
    keep = np.all(np.isfinite(E), axis=1)
    n_raw = len(E)
    Nn = np.zeros_like(E) if spec.normals is None else np.asarray(spec.normals, dtype=np.float64)
    if not keep.all():
        E, Nn = E[keep], Nn[keep]
    T["read"] = time.perf_counter() - t0
    ctx.check_cancel()
    t2 = time.perf_counter()
    dtm, gi = dtm_opening(E, cell=dtm_cell_m)
    hag = E[:, 2] - dtm[gi.iy, gi.ix]
    up = Nn[:, 2] > 0.95
    ground_frac = float((up & (np.abs(hag) < 1.5)).mean())
    ground_type = "dtm"
    if ground_frac <= 0.10:
        z0 = float(np.percentile(E[:, 2], 0.5))
        dtm[:] = z0
        hag = E[:, 2] - z0
        ground_type = "synthetic"
    relief = (round(float(np.nanpercentile(dtm, 1)), 2), round(float(np.nanpercentile(dtm, 99)), 2))
    T["dtm"] = time.perf_counter() - t2
    rss["dtm"] = _rss_mb()
    ctx.check_cancel()
    t3 = time.perf_counter()
    ci = dsm_index(E, dsm_cell_m)
    top = raw_top(ci, E[:, 2])
    grid_origin = (float(E[:, 0].min()), float(E[:, 1].min()))
    Nn, flipped, zero = fix_normals(E, Nn, hag, top, ci.ix, ci.iy, grid_origin, dsm_cell_m)
    T["normals"] = time.perf_counter() - t3
    t4 = time.perf_counter()
    cls = classify(Nn, hag) if spec.class_index is None else np.asarray(spec.class_index, np.uint8)
    upf = Nn[:, 2] > 0.95
    tilt_after = angle_to_z_deg(Nn[upf].mean(0)) if upf.any() else 0.0
    T["classify"] = time.perf_counter() - t4
    ctx.check_cancel()
    t5 = time.perf_counter()
    nn = round(nn_median(E), 3)
    hag32 = hag.astype(np.float32)
    zr = percentiles2(E[:, 2])
    hr = percentiles2(hag32)
    ip = int(np.argmax(hag))
    peak = E[ip].copy()
    peak_hag = float(hag[ip])
    lo, hi = colmin(E), colmax(E)
    precision = precision_report([round(float(v), 3) + 0.0 for v in lo], [round(float(v), 3) + 0.0 for v in hi])
    T["stats"] = time.perf_counter() - t5
    rss["ingest"] = _rss_mb()
    gates = run_gates(source_kind="reconstruction", peak_hag_m=peak_hag, precision=precision, leveled=False, ground_plane_mad_m=None,
                      tilt_after_deg=tilt_after, zero_normals_frac=float(zero.mean()), ground_frac=ground_frac,
                      true_north=cfg.true_north, evidence=cfg.evidence, n_out=len(E), n_raw=n_raw,
                      n_nonfinite=int(n_raw - len(E)), n_dedup=0, raw_ok=True, raw_detail={})
    S = spec.registration["T_world_map"]
    from awr.world.georef.frames import quat_to_mat

    Tm = np.eye(4)                                   # source frame = engine gauge: x_world = s R x_engine + t
    Tm[:3, :3] = float(S["s"]) * quat_to_mat(np.asarray(S["q"], dtype=np.float64))
    Tm[:3, 3] = np.asarray(S["t"], dtype=np.float64)
    stats = CloudStats(
        n_raw=n_raw, n_nonfinite=int(n_raw - len(E)), n_dedup=0, nn_median_m=nn, z_p1=zr[0], z_p99=zr[1], hag_p1=hr[0], hag_p99=hr[1],
        class_histogram=class_histogram(cls), normals_flipped_frac=float(flipped.mean()), zero_normals_frac=float(zero.mean()),
        ground_frac=ground_frac, tilt_raw_deg=0.0, leveled_deg=0.0, tilt_after_deg=tilt_after, ground_plane_mad_m=None,
        units_heuristic=float(S["s"]), up_axis_scores={}, peak_index=ip, peak_hag_m=peak_hag,
        peak_enu_m=(float(peak[0]), float(peak[1]), float(peak[2])), relief_p1p99_m=relief)
    nc = NormalizedCloud(
        world_id=cfg.world_id, xyz=E, normal=Nn.astype(np.float32), cls=cls, hag=hag32, T_world_source=Tm, origin=np.zeros(3),
        anchor=dict(spec.anchor_json or {}), terrain=TerrainGrids(dtm=dtm.astype(np.float32), dtm_origin_xy=tuple(gi.origin_xy),
                                                                  dtm_cell_m=dtm_cell_m, ground_type=ground_type),
        stats=stats, gates=gates, config=cfg, provenance=spec.dataset, source_files=[], timings=T, peak_rss_mb=rss)
    nc.grid_cache = (ci, top, grid_origin)
    return nc


class _ReconPipeline(BuildPipeline):
    spec: ReconIngestSpec

    def run_ingest(self, adapter: ReconArraysAdapter) -> NormalizedCloud:
        self.spec = adapter.spec
        cfg = adapter.config()
        self.ctx.world_id = cfg.world_id
        self.extras = {**adapter.manifest_extras(), **self.extras}
        nc = _ingest_arrays(adapter.spec, cfg, self.ctx, self.params.dtm_cell_m, self.params.dsm_cell_m)
        for k, src in (("read", "read"), ("ingest", "stats"), ("dtm", "dtm"), ("normals", "normals"), ("classify", "classify")):
            self.stages.append({"name": k, "seconds": round(nc.timings.get(src, 0.0), 3), "peak_rss_mb": _rss_mb()})
        self.nc = nc
        errs = [g for g in nc.gates if not g.passed and g.severity == "error"]
        if errs:
            raise GateFailed("; ".join(f"{g.id} {g.name}: {g.value}" for g in errs))
        self.write_ingest_outputs()
        return nc

    def write_ingest_outputs(self) -> None:
        from awr.world.ingest.gates import gate_status

        nc = self.nc
        sp = self.spec
        coord = build_coordinate(nc, source_dataset=None, qa_status=gate_status(nc.gates))
        coord["anchor"] = dict(sp.anchor_json or coord["anchor"])
        if sp.T_ecef_world is not None:
            coord["T_ecef_world"] = sp.T_ecef_world
        tn = sp.true_north or {}
        coord["trueNorth"] = {"yawOffsetDeg": float(tn.get("yawOffsetDeg", 0.0)), "confidence": tn.get("confidence", "assumed"),
                              "evidence": list(tn.get("evidence") or [])}
        coord["scaleStatus"] = sp.scale_status
        src = coord["source"]
        src.update(kind="reconstruction", dataset=None, files=[], crs="LOCAL", projPipeline=None, handedness="right", upAxis="+z",
                   unitsToMeters=float(sp.registration["T_world_map"]["s"]), leveledDeg=0.0, yawDeg=0.0,
                   evidence=[sp.session_id, f"registration {sp.registration['method']}"])
        # V-C-06 (M03 validator) requires source.upAxis to map to world +Z within 30 deg, which an arbitrary engine gauge
        # cannot satisfy; until M03 exempts reconstruction sources (request M01-to-M03) the declared source frame is the
        # engine gauge rotated by R(T_world_map.q): T_world_source = [[s I, t], [0, 1]]. registration.T_world_map and
        # alignment.json keep the exact engine gauge.
        Sg = sp.registration["T_world_map"]
        Tws = np.eye(4) * float(Sg["s"])
        Tws[3, 3] = 1.0
        Tws[:3, 3] = np.asarray(Sg["t"], dtype=np.float64)
        src["T_world_source"] = [[round(float(v), 12) + 0.0 for v in row] for row in Tws]
        src["evidence"].append("source frame = engine gauge rotated by registration.T_world_map.q (V-C-06 transitional)")
        coord["registration"] = {k: v for k, v in sp.registration.items() if v is not None}
        write_json(self.stage / "coordinate.json", coord)
        self.coordinate = coord
        self.coordinate_sha = sha256_file(self.stage / "coordinate.json")
        from awr.world.package.manifest import DTM_HREF
        from awr.world.terrain.grids import Grid, write_grid

        t = nc.terrain
        write_grid(self.stage / DTM_HREF, Grid(t.dtm, t.dtm_origin_xy, t.dtm_cell_m), "dtm",
                   "10m min-z, 9x9 opening, nan-fill" if t.ground_type == "dtm" else "flat p0.5 (synthetic ground)")

    def run_package(self) -> dict:
        sp = self.spec
        if sp.recon_session_dir is not None:
            dst = self.stage / "reconstruction" / sp.session_id
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(sp.recon_session_dir, dst)
            sj = dst / "session.json"
            files = sorted(p for p in dst.rglob("*") if p.is_file())
            layer = LayerSpec(id=f"reconstruction.{sp.session_id}", type="reconstruction", role="reconstruction",
                              format="recon-ir@1", href=f"reconstruction/{sp.session_id}/", default=False,
                              extra={"bytes": int(sum(p.stat().st_size for p in files)), "sha256": sha256_file(sj)})
            self.layers.append(layer)
        return super().run_package()


@dataclass
class BridgeBuild:
    """Two-phase build: `tiling()` (ingest, DSM, source cloud, tiling) and `packaging()` (derivers, manifest with the
    session layer, deep validation, report, atomic publish). `abort()` releases the lock and removes the staging."""

    spec: ReconIngestSpec
    worlds_dir: Path
    job_id: str
    ctx: StageContext
    keep_staging: bool = False

    def __post_init__(self) -> None:
        self.params = _with_recon(BuildParams.from_config(keep_staging=self.keep_staging), self.spec.generator_params)
        self.pub = Publisher(Path(self.worlds_dir), self.spec.world_id)
        self._stack = contextlib.ExitStack()
        self.stage: Path | None = None
        self.pipe: _ReconPipeline | None = None
        self.t0 = time.perf_counter()

    def tiling(self, progress: Callable[[float], None] = lambda f: None) -> None:
        self._stack.enter_context(self.pub.lock())
        self.pub.recover(shallow_ok=lambda p: validate_world(p, staging=True).ok)
        self.stage = self.pub.new_staging(nonce=self.job_id)
        self.pipe = _ReconPipeline(self.stage, self.params, self.ctx)
        self.pipe.run_ingest(ReconArraysAdapter(self.spec))
        progress(0.35)
        self.ctx.check_cancel()
        self.pipe.run_grid()
        progress(0.55)
        self.ctx.check_cancel()
        self.pipe.run_tile()
        progress(1.0)

    def packaging(self, progress: Callable[[float], None] = lambda f: None) -> BuildResult:
        p = self.pipe
        p.run_derive()
        self.ctx.check_cancel()
        p.run_package()
        progress(0.3)
        self.ctx.check_cancel()
        p.run_validate(deep=True)
        progress(0.8)
        p.run_report()
        self.ctx.check_cancel()                                    # last checkpoint before the atomic publish
        shutil.rmtree(self.stage / WORK, ignore_errors=True)
        self.pub.publish(self.stage)
        cv = p.world["contentVersion"]
        self.pub.write_status("ready", reason=None, exit_code=0, content_version=cv, deep=True, raw=None)
        self._stack.close()
        progress(1.0)
        return BuildResult(self.spec.world_id, 0, cv, True, None, p.stages, "", round(time.perf_counter() - self.t0, 3))

    def abort(self, *, keep: bool = False) -> None:
        if self.stage is not None and not keep and not self.keep_staging:
            shutil.rmtree(self.stage, ignore_errors=True)
        self._stack.close()


def _with_recon(p: BuildParams, recon: dict | None) -> _ReconParams:
    fields = {f: getattr(p, f) for f in p.__dataclass_fields__ if f != "recon"}
    q = _ReconParams(**fields)
    object.__setattr__(q, "recon", dict(recon or {}))
    return q

