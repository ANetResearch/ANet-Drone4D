"""派生世界流水线：`ArraysAdapter`（M03-FR-021）经 `build_world` 使用的 `BuildPipeline` 子类（M03 §6.13 (3)）。

只替换 ingest 一步（第 6–10 步：DTM、DSM 顶面、法线修正、分类、统计与门禁，坐标帧与原点取源世界），并在清单中写入
重建字段；网格、切片、派生、打包、`--deep` 校验、报告与原子发布与六城相同。行为与 M01 原过渡实现 `m03_bridge`（已移除）逐项一致
（M01-to-M03 第 1 条关键约定）；过渡期的两处声明（源帧取"按 T_world_map.q 旋转后的引擎系"以满足 V-C-06，会话图层 href
取目录）在校验器规则修订前保持不变（M01-to-M03 第 2、3 条，见 FX-SIM2 报告遗留项）。
"""

from __future__ import annotations

import resource
import shutil
import time
from typing import Any

import numpy as np

from awr.world.georef.frames import precision_report, quat_to_mat
from awr.world.ingest.classify import classify
from awr.world.ingest.gates import gate_status, run_gates
from awr.world.ingest.normalize import angle_to_z_deg
from awr.world.ingest.normals import fix_normals
from awr.world.ingest.stats import class_histogram, nn_median, percentiles2
from awr.world.ingest.types import CloudStats, GateFailed, IngestConfig, NormalizedCloud, StageContext, TerrainGrids
from awr.world.pointcloud.npx import colmax, colmin
from awr.world.terrain.dsm import dsm_index, raw_top
from awr.world.terrain.dtm import dtm_opening

from .build import BuildPipeline
from .derivers import LayerSpec
from .jsonio import sha256_file, write_json
from .manifest import DTM_HREF, build_coordinate
from .params import BuildParams

__all__ = ["ArraysPipeline", "PhasedBuild", "ReconParams", "ingest_arrays", "with_recon"]


def _rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)


class ReconParams(BuildParams):
    """BuildParams whose generator.params also carries `recon`（M01 §6.8 generator 行）。"""

    recon: dict | None = None

    def generator_params(self) -> dict:
        d = super().generator_params()
        if self.recon:
            d["recon"] = dict(self.recon)
        return d


def with_recon(p: BuildParams, recon: dict | None) -> ReconParams:
    fields = {f: getattr(p, f) for f in p.__dataclass_fields__ if f != "recon"}
    q = ReconParams(**fields)
    object.__setattr__(q, "recon", dict(recon or {}))
    return q


def ingest_arrays(spec: Any, cfg: IngestConfig, ctx: StageContext, dtm_cell_m: float, dsm_cell_m: float) -> NormalizedCloud:
    """第 6–10 步：坐标与原点沿用源世界（不重选）；返回 NormalizedCloud（`grid_cache` 供 GRIDDING 复用）。"""
    T: dict[str, float] = {}
    t0 = time.perf_counter()
    E = np.ascontiguousarray(spec.xyz_world, dtype=np.float64)
    keep = np.all(np.isfinite(E), axis=1)
    n_raw = len(E)
    Nn = np.zeros_like(E) if spec.normals is None else np.asarray(spec.normals, dtype=np.float64)
    cls_in = None if spec.class_index is None else np.asarray(spec.class_index, np.uint8)
    if not keep.all():
        E, Nn = E[keep], Nn[keep]
        cls_in = None if cls_in is None else cls_in[keep]
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
    ctx.check_cancel()
    ctx.heartbeat()
    t3 = time.perf_counter()
    ci = dsm_index(E, dsm_cell_m)
    top = raw_top(ci, E[:, 2])
    grid_origin = (float(E[:, 0].min()), float(E[:, 1].min()))
    Nn, flipped, zero = fix_normals(E, Nn, hag, top, ci.ix, ci.iy, grid_origin, dsm_cell_m)
    T["normals"] = time.perf_counter() - t3
    t4 = time.perf_counter()
    cls = classify(Nn, hag) if cls_in is None else cls_in
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
    gates = run_gates(source_kind=spec.source_kind, peak_hag_m=peak_hag, precision=precision, leveled=False,
                      ground_plane_mad_m=None, tilt_after_deg=tilt_after, zero_normals_frac=float(zero.mean()),
                      ground_frac=ground_frac, true_north=cfg.true_north, evidence=cfg.evidence, n_out=len(E), n_raw=n_raw,
                      n_nonfinite=int(n_raw - len(E)), n_dedup=0, raw_ok=True, raw_detail={})
    S = spec.registration["T_world_map"]
    Tm = np.eye(4)                                   # 源帧 = 引擎规范系：x_world = s R x_engine + t
    Tm[:3, :3] = float(S["s"]) * quat_to_mat(np.asarray(S["q"], dtype=np.float64))
    Tm[:3, 3] = np.asarray(S["t"], dtype=np.float64)
    stats = CloudStats(
        n_raw=n_raw, n_nonfinite=int(n_raw - len(E)), n_dedup=0, nn_median_m=nn, z_p1=zr[0], z_p99=zr[1], hag_p1=hr[0],
        hag_p99=hr[1], class_histogram=class_histogram(cls), normals_flipped_frac=float(flipped.mean()),
        zero_normals_frac=float(zero.mean()), ground_frac=ground_frac, tilt_raw_deg=0.0, leveled_deg=0.0,
        tilt_after_deg=tilt_after, ground_plane_mad_m=None, units_heuristic=float(S["s"]), up_axis_scores={}, peak_index=ip,
        peak_hag_m=peak_hag, peak_enu_m=(float(peak[0]), float(peak[1]), float(peak[2])), relief_p1p99_m=relief)
    nc = NormalizedCloud(
        world_id=cfg.world_id, xyz=E, normal=Nn.astype(np.float32), cls=cls, hag=hag32, T_world_source=Tm,
        origin=np.zeros(3), anchor=dict(spec.anchor_json or {}),
        terrain=TerrainGrids(dtm=dtm.astype(np.float32), dtm_origin_xy=tuple(gi.origin_xy), dtm_cell_m=dtm_cell_m,
                             ground_type=ground_type),
        stats=stats, gates=gates, config=cfg, provenance=spec.dataset, source_files=[], timings=T,
        peak_rss_mb={"ingest": _rss_mb()})
    nc.grid_cache = (ci, top, grid_origin)
    return nc


class ArraysPipeline(BuildPipeline):
    """`BuildPipeline` 的派生世界版本：ingest 与坐标文件、打包时的会话图层。"""

    spec: Any

    def run_ingest(self, adapter: Any) -> NormalizedCloud:
        self.spec = adapter.spec
        cfg = adapter.config()
        self.ctx.world_id = cfg.world_id
        self.extras = {**adapter.manifest_extras(), **self.extras}
        nc = ingest_arrays(adapter.spec, cfg, self.ctx, self.params.dtm_cell_m, self.params.dsm_cell_m)
        for k, src in (("read", "read"), ("ingest", "stats"), ("dtm", "dtm"), ("normals", "normals"), ("classify", "classify")):
            self.stages.append({"name": k, "seconds": round(nc.timings.get(src, 0.0), 3), "peak_rss_mb": _rss_mb()})
        self.nc = nc
        errs = [g for g in nc.gates if not g.passed and g.severity == "error"]
        if errs:
            raise GateFailed("; ".join(f"{g.id} {g.name}: {g.value}" for g in errs))
        self.write_ingest_outputs()
        return nc

    def write_ingest_outputs(self) -> None:
        from awr.world.terrain.grids import Grid, write_grid

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
        Sg = sp.registration["T_world_map"]
        src.update(kind=sp.source_kind, dataset=None, files=[], crs="LOCAL", projPipeline=None, handedness="right",
                   upAxis="+z", unitsToMeters=float(Sg["s"]), leveledDeg=0.0, yawDeg=0.0,
                   evidence=[sp.session_id, f"registration {sp.registration.get('method')}"])
        # V-C-06 要求 source.upAxis 经 T_world_source 映射到 world +Z（30° 内），任意朝向的引擎规范系不满足；在校验器豁免
        # 重建源（M01-to-M03 第 2 条）之前，声明的源帧为按 R(T_world_map.q) 旋转后的引擎系：T_world_source = [[s I, t], [0, 1]]；
        # registration.T_world_map 与 alignment.json 保持精确的引擎规范系。
        Tws = np.eye(4) * float(Sg["s"])
        Tws[3, 3] = 1.0
        Tws[:3, 3] = np.asarray(Sg["t"], dtype=np.float64)
        src["T_world_source"] = [[round(float(v), 12) + 0.0 for v in row] for row in Tws]
        src["evidence"].append("source frame = engine gauge rotated by registration.T_world_map.q (V-C-06 transitional)")
        coord["registration"] = {k: v for k, v in sp.registration.items() if v is not None}
        write_json(self.stage / "coordinate.json", coord)
        self.coordinate = coord
        self.coordinate_sha = sha256_file(self.stage / "coordinate.json")
        t = nc.terrain
        write_grid(self.stage / DTM_HREF, Grid(t.dtm, t.dtm_origin_xy, t.dtm_cell_m), "dtm",
                   "10m min-z, 9x9 opening, nan-fill" if t.ground_type == "dtm" else "flat p0.5 (synthetic ground)")

    def run_package(self) -> dict:
        sp = self.spec
        if sp.recon_session_dir is not None:
            sid = sp.session_id or sp.recon_session_dir.name
            dst = self.stage / "reconstruction" / sid
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(sp.recon_session_dir, dst, symlinks=False)
            sj = dst / "session.json"
            files = sorted(p for p in dst.rglob("*") if p.is_file())
            layer = LayerSpec(id=f"reconstruction.{sid}", type="reconstruction", role="reconstruction", format="recon-ir@1",
                              href=f"reconstruction/{sid}/", default=False,
                              extra={"bytes": int(sum(p.stat().st_size for p in files)), "sha256": sha256_file(sj)})
            self.layers.append(layer)
        return super().run_package()


class PhasedBuild:
    """两段式派生世界构建（M01 的 TILING 与 PACKAGING 两个阶段分别调用；M01-FR-030、FR-031、FR-041）：

    - `tiling(progress)`：持世界锁 → 启动恢复 → staging（`<world_id>-<staging_nonce>`）→ ingest、网格、切片；
    - `packaging(progress)`：派生、打包（含会话图层）→ `--deep` 校验 → 报告 → 最后一个取消检查点 → 原子发布 → `.status`；
    - `abort()`：取消或失败时删除 staging 并释放锁（发布之前取消，世界列表不变）。
    与 `build_world(ArraysAdapter(spec))` 的单次调用使用同一流水线（`ArraysPipeline`），只是把发布推迟到第二段。"""

    def __init__(self, adapter: Any, worlds_dir: Any, ctx: StageContext, *, params: BuildParams | None = None,
                 keep_staging: bool = False) -> None:
        import contextlib
        from pathlib import Path

        from .publish import Publisher

        self.adapter = adapter
        self.spec = adapter.spec
        self.ctx = ctx
        base = params or BuildParams.from_config(keep_staging=keep_staging)
        self.params = adapter.build_params(base)
        self.keep_staging = keep_staging
        self.pub = Publisher(Path(worlds_dir), self.spec.world_id)
        self._stack = contextlib.ExitStack()
        self.stage = None
        self.pipe: ArraysPipeline | None = None
        self.t0 = time.perf_counter()

    def tiling(self, progress: Any = lambda f: None) -> None:
        from .validate import validate_world

        self._stack.enter_context(self.pub.lock())
        self.pub.recover(shallow_ok=lambda p: validate_world(p, staging=True).ok)
        self.stage = self.pub.new_staging(nonce=self.adapter.staging_nonce)
        self.pipe = ArraysPipeline(self.stage, self.params, self.ctx)
        self.pipe.run_ingest(self.adapter)
        progress(0.35)
        self.ctx.check_cancel()
        self.pipe.run_grid()
        progress(0.55)
        self.ctx.check_cancel()
        self.pipe.run_tile()
        progress(1.0)

    def packaging(self, progress: Any = lambda f: None) -> Any:
        from .build import WORK, BuildResult

        p = self.pipe
        assert p is not None, "packaging() before tiling()"
        p.run_derive()
        self.ctx.check_cancel()
        p.run_package()
        progress(0.3)
        self.ctx.check_cancel()
        p.run_validate(deep=True)
        progress(0.8)
        p.run_report()
        self.ctx.check_cancel()                                    # 原子发布前的最后一个检查点
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
