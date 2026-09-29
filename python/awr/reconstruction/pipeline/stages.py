"""The seven working stages of a recon job (M01 §6.7.2; M01-FR-024 to FR-031). D1-ext.

`ReconRun` carries one job: resolved parameters, directories, and the in-memory products of earlier stages (rebuilt
deterministically from disk when a retry starts in the middle). Stage functions only write their products atomically;
the caller (`jobs.recon_job`) writes the completion markers and handles cancellation, failures and cleanup.

Job directory layout (`runs/jobs/<job_id>/`, never published): `session/<session_id>/` (the Recon IR being assembled),
`frames/<frame_id>.npz` (INFERRING shards), `prep/gnss.json`, `fused.npz`, `mock_truth.json`, `provenance.json`,
`job.log`, `stages/`.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from awr.world.georef.frames import Anchor, world_to_lla
from awr.world.georef.mock_gnss import FixType, MockGnss

from ..conventions import mat_to_quat_xyzw
from ..engines.base import get_engine
from ..engines.mock_paths import FlightPath, make_path
from ..ir.jsonio import write_json
from ..ir.reader import SessionReader
from ..ir.trajectory_bin import write_trajectory_bin
from ..ir.validate import validate_session
from ..ir.writer import SessionWriter, frame_row
from ..jobs.ctx_ext import ReconCtx
from ..jobs.params import session_id_of
from ..types import (
    FRAME_NONKEY,
    STREAM_RECON_SELFCHECK,
    FrameInput,
    IrInvalid,
    PackageInvalid,
    ReconEmpty,
    SessionSpec,
    TilingFailed,
    rng,
)
from .fuse import Fused, fuse, load_fused, save_fused, save_shard
from .georef import Alignment, georeference, prepare_georef_inputs
from .package import build_ingest_spec, m03_arrays_api, to_m03_spec
from .qa import build_qa, c2c_to_source
from .source import SourceCloud, WorldMeta, load_world_meta, prepare_source, read_full_source_xyz

__all__ = ["STAGE_FUNCS", "ReconRun"]

W_ORIG, H_ORIG = 1920, 1080


@dataclass
class ReconRun:
    ctx: ReconCtx
    params: dict
    worlds_dir: Path
    target: str
    submitted_by: str = "cli"
    # derived
    session_id: str = ""
    meta: WorldMeta | None = None
    source: SourceCloud | None = None
    path: FlightPath | None = None
    gnss_rows: list[dict | None] | None = None
    fused: Fused | None = None
    alignment: Alignment | None = None
    builder: object | None = None
    build_result: object | None = None
    outputs: dict[str, list[Path]] = field(default_factory=dict)
    info: dict = field(default_factory=dict)

    # ---- paths
    @property
    def workdir(self) -> Path:
        return self.ctx.workdir

    @property
    def session_dir(self) -> Path:
        return self.workdir / "session" / self.session_id

    @property
    def shard_dir(self) -> Path:
        return self.workdir / "frames"

    @property
    def engine_name(self) -> str:
        return self.params["engine"]

    # ---- deterministic preparation (re-run on resume; cheap compared with INFERRING)
    def ensure_meta(self) -> WorldMeta:
        if self.meta is None:
            self.meta = load_world_meta(self.worlds_dir, self.params["source"]["world_id"])
            eng = get_engine(self.engine_name)
            self.session_id = session_id_of(self.params, self.params["seed"], self.meta.content_version, self.engine_name, eng.version)
            self.ctx.state_extra["recon"] = {"engine": self.engine_name, "session_id": self.session_id,
                                             "source_world_id": self.meta.world_id, "target_world_id": self.target}
        return self.meta

    def ensure_source(self) -> SourceCloud:
        if self.source is None:
            meta = self.ensure_meta()
            self.source = prepare_source(self.worlds_dir, meta.world_id, keep=float(self.params["params"]["mock"]["source_keep"]),
                                         seed=int(self.params["seed"]))
            self.ctx.checkpoint()
            pp, cam = self.params["params"]["path"], self.params["params"]["camera"]
            lo, hi = meta.extent
            self.path = make_path(self.params["source"]["path"], int(self.params["source"]["frames"]), float(cam["fps"]),
                                  extent_min=lo, extent_max=hi, ground_z_m=self.source.ground_z_m, alt_agl_m=float(pp["alt_agl_m"]),
                                  helix_radius_m=pp["helix_radius_m"], helix_turns=int(pp["helix_turns"]),
                                  helix_climb_m=float(pp["helix_climb_m"]), lawnmower_spacing_m=float(pp["lawnmower_spacing_m"]),
                                  lawnmower_legs=int(pp["lawnmower_legs"]), pitch_deg=float(cam["pitch_deg"]))
        return self.source

    def anchor(self) -> Anchor:
        return Anchor.from_coordinate(self.ensure_meta().coordinate)

    def load_gnss(self) -> list[dict | None]:
        if self.gnss_rows is None:
            self.gnss_rows = json.loads((self.workdir / "prep" / "gnss.json").read_text(encoding="utf-8"))["frames"]
        return self.gnss_rows


# ---------------------------------------------------------------- PREPARING
def stage_preparing(run: ReconRun) -> list[Path]:
    meta = run.ensure_meta()
    if meta.status != "ready":
        from ..types import InputInvalid

        raise InputInvalid(f"source world {meta.world_id} is {meta.status}", detail={"world_id": meta.world_id, "status": meta.status})
    src = run.ensure_source()
    run.ctx.progress(0.6)
    path = run.path
    # synthetic GNSS: true centres + noise, 1 % jumps of 15-40 m, 2 % dropouts (M02 MockGnss, stream 9); world -> WGS84
    # through the source anchor, so GEOREFERENCING exercises the real conversion path (M01 §6.6.1)
    g = run.params["params"]["mock"]["gnss"]
    t_ns = path.t_ns
    C = path.C
    if float(g.get("time_offset_s", 0.0)) != 0.0:
        from awr.world.georef.sim3 import interp_track

        C_i = interp_track(t_ns, C, t_ns + round(float(g["time_offset_s"]) * 1e9))
        C = np.where(np.isfinite(C_i), C_i, C)
    obs = MockGnss(int(run.params["seed"])).sample(C, t_ns, fix_mix={FixType.SPP: 1.0}, dropout_ratio=float(g["dropout_frac"]),
                                                   multipath_ratio=float(g["outlier_frac"]), jump_m=(15.0, 40.0),
                                                   sigma_enu_m=(float(g["sigma_h_m"]), float(g["sigma_h_m"]), float(g["sigma_v_m"])))
    lat, lon, h = world_to_lla(np.where(np.isfinite(obs.pos_world_m), obs.pos_world_m, 0.0), run.anchor())
    rows: list[dict | None] = []
    for k in range(len(t_ns)):
        if run.params["params"]["georef"]["mode"] == "none" or not obs.valid[k]:
            rows.append(None)
            continue
        rows.append({"lat_deg": float(lat[k]), "lon_deg": float(lon[k]), "h_ellipsoid_m": float(h[k]),
                     "sigma_h_m": float(g["sigma_h_m"]), "sigma_v_m": float(g["sigma_v_m"]), "fix": str(obs.fix[k])})
    run.gnss_rows = rows
    prep = run.workdir / "prep" / "gnss.json"
    write_json(prep, {"schema": "awr.recon.mock_gnss.v1", "frames": rows})
    # rig and cameras of the session
    sd = run.session_dir
    if sd.exists():
        shutil.rmtree(sd)
    w = SessionWriter(sd)
    ident = {"q": [0.0, 0.0, 0.0, 1.0], "t": [0.0, 0.0, 0.0]}
    rig = w.write_doc("rig", {"rig_id": "mock-rig", "ref_sensor": "cam0", "sensors": [
        {"sensor_id": "cam0", "type": "CAMERA", "camera_id": 1, "T_body_sensor": ident, "time_offset_ns": 0},
        {"sensor_id": "gnss0", "type": "GNSS", "T_body_sensor": ident, "time_offset_ns": 0},
        {"sensor_id": "imu0", "type": "IMU", "T_body_sensor": ident, "time_offset_ns": 0}]})
    fx = (W_ORIG / 2.0) / np.tan(np.radians(float(run.params["params"]["camera"]["hfov_deg"])) / 2.0)
    cams = w.write_doc("cameras", {"cameras": [{"camera_id": 1, "model": "PINHOLE", "width": W_ORIG, "height": H_ORIG,
                                                "params": [float(fx), float(fx), W_ORIG / 2.0, H_ORIG / 2.0], "source": "synthetic",
                                                "pixel_convention": "colmap"}]})
    run.info.update(source_points=src.n_full, source_kept=len(src.xyz), path_kind=path.kind, radius_m=path.radius_m)
    run.ctx.log("info", "prepared source and path", points=len(src.xyz), frames=len(t_ns), path=path.kind)
    return [prep, rig, cams]


# ---------------------------------------------------------------- SEGMENTING (skipped for the Mock)
def stage_segmenting(run: ReconRun) -> list[Path]:
    return []


# ---------------------------------------------------------------- INFERRING
def stage_inferring(run: ReconRun) -> list[Path]:
    from ..engines.mock import MockInputs

    run.ensure_source()
    gnss = run.load_gnss()
    for p in (run.session_dir / "frames.jsonl", run.session_dir / "frames.jsonl.partial"):
        p.unlink(missing_ok=True)
    if run.shard_dir.exists():
        shutil.rmtree(run.shard_dir)
    mk = run.params["params"]["mock"]
    eng = get_engine(run.engine_name, raw_pose_dir=mk.get("raw_pose_dir", "c2w"))
    n = int(run.params["source"]["frames"])
    cam = run.params["params"]["camera"]
    spec = SessionSpec(run.session_id, n, float(cam["fps"]), W_ORIG, H_ORIG, int(run.params["seed"]), run.params,
                       source=MockInputs(run.source, run.path, run.params))
    from ..types import EngineContext

    eng.prepare(spec, EngineContext(workdir=run.workdir, check_cancel=run.ctx.check_cancel, log=run.ctx.log))
    w = SessionWriter(run.session_dir)
    w.open_frames()
    n_key = 0
    try:
        frames = (FrameInput(k, int(run.path.t_ns[k])) for k in range(n))
        for ef in eng.infer(frames):
            run.ctx.checkpoint()
            save_shard(run.shard_dir / f"{ef.idx:06d}.npz", ef.depth, ef.conf_u8, ef.normals_engine, ef.K_depth)
            valid = ef.depth > 0
            cm = int(np.rint(ef.conf_u8[valid].mean())) if ef.conf_u8 is not None and valid.any() else 0
            w.append_frame(frame_row(ef.idx, ef.t_ns, ef.T_engine_cam, ef.q_xyzw, frame_type=ef.frame_type, pose_source="mock",
                                     conf_mean_u8=cm, valid_ratio=float(valid.mean()), gnss=gnss[ef.idx],
                                     gravity_cam=ef.extras.get("gravity_cam")))
            n_key += ef.frame_type != FRAME_NONKEY
            run.ctx.emit_extra(frames_done=ef.idx + 1, frames_total=n)
            run.ctx.progress((ef.idx + 1) / n)
    except BaseException:
        w.abort_frames()
        raise
    frames_path = w.close_frames()
    norm = eng.normalization_record()
    eng_doc = w.write_doc("engine", {
        "engine": eng.name, "variant": eng.variant, "version": eng.version, "commit": eng.commit, "weights": eng.weights,
        "license": {**eng.license, "notice": f"synthetic resample of {run.meta.world_id}; not a real capture"},
        "convention_raw": eng.convention.to_json(), "normalization": norm, "engine_scale": eng.convention.engine_scale,
        "conditioning": {"intrinsics": True, "poses": "none", "depth": "none"},
        "params": {"path": run.path.kind, "frames": n, "fps": cam["fps"], "depth_res": list(cam["depth_res"]),
                   "hfov_deg": cam["hfov_deg"], "source_keep": mk["source_keep"], "depth_noise_rel": mk["depth_noise_rel"],
                   "raw_pose_dir": mk.get("raw_pose_dir", "c2w"), "seed": run.params["seed"]},
        "runtime": {"device": "cpu", "threads": 1, "dtype": "float64"}})
    truth = eng.write_truth(run.workdir / "mock_truth.json", np.asarray(norm["T_frame0_raw"]))
    run.info.update(frames=n, keyframes=n_key, hidden_scale=truth["hidden_sim3"]["s"])
    return [frames_path, eng_doc, run.workdir / "mock_truth.json"]


# ---------------------------------------------------------------- FUSING
def stage_fusing(run: ReconRun) -> list[Path]:
    run.ensure_meta()
    rd = SessionReader(run.session_dir)
    rows = rd.frames
    T, _ = rd.poses()
    fp = run.params["params"]["fuse"]
    fused = fuse(run.shard_dir, rows, T, fp, check=run.ctx.checkpoint, progress=run.ctx.progress,
                 rng_=rng(int(run.params["seed"]), STREAM_RECON_SELFCHECK, 3))
    if fused.n < int(fp.get("min_points", 10_000)):
        raise ReconEmpty(f"only {fused.n} fused points (< {fp.get('min_points', 10_000)})", detail={"points": fused.n})
    out = run.workdir / "fused.npz"
    save_fused(out, fused)
    run.fused = fused
    run.info.update(points_raw=fused.n_raw, points_fused=fused.n, vox_engine=fused.vox_engine)
    run.ctx.log("info", "fused", points_raw=fused.n_raw, points_fused=fused.n, vox_engine=fused.vox_engine)
    return [out]


def _ensure_fused(run: ReconRun) -> Fused:
    if run.fused is None:
        run.fused = load_fused(run.workdir / "fused.npz")
    return run.fused


# ---------------------------------------------------------------- GEOREFERENCING
def stage_georeferencing(run: ReconRun) -> list[Path]:
    meta = run.ensure_meta()
    rd = SessionReader(run.session_dir)
    rows = rd.frames
    T, _ = rd.poses()
    fused = _ensure_fused(run)
    inp = prepare_georef_inputs(rows, T, run.anchor())
    eng_doc = rd.engine
    al = georeference(inp, run.params["params"]["georef"], seed=int(run.params["seed"]), engine_scale=eng_doc["engine_scale"],
                      conditioning=eng_doc.get("conditioning"))
    run.alignment = al
    run.ctx.checkpoint()
    w = SessionWriter(run.session_dir)
    w.write_doc("alignment", al.to_json())
    # registered layer: T_world_cam = T_world_engine * T_engine_cam (AWTR v1)
    from awr.world.georef.frames import quat_to_mat

    S = al.sim3
    R = quat_to_mat(np.asarray(S.q, dtype=np.float64))
    pos = S.s * T[:, :3, 3] @ R.T + np.asarray(S.t)
    q = mat_to_quat_xyzw(np.einsum("ij,njk->nik", R, T[:, :3, :3]))
    write_trajectory_bin(run.session_dir / "trajectory.bin", 0, [r["t_ns"] for r in rows], pos, q, [r["frame_type"] for r in rows],
                         [r.get("conf_mean_u8", 255) for r in rows], [r["camera_id"] for r in rows])
    # qa.json (C2C against the full source cloud for world_sample inputs)
    c2c = None
    if run.params["source"]["kind"] == "world_sample":
        from .package import world_points

        Xw, _ = world_points(fused, S)
        c2c = c2c_to_source(Xw, None, seed=int(run.params["seed"]), key=(str(meta.dir), meta.content_version),
                            loader=lambda: read_full_source_xyz(meta))
    qa = build_qa(fused, al, eng_doc["normalization"]["self_check"], c2c)
    w.write_doc("qa", qa)
    counts = {"frames": len(rows), "keyframes": sum(r["frame_type"] != FRAME_NONKEY for r in rows),
              "points_raw": fused.n_raw, "points_fused": fused.n}
    src = run.params["source"]
    w.finalize({"session_id": run.session_id,
                "source_world": {"id": meta.world_id, "content_version": meta.content_version,
                                 "coordinate_sha256": meta.coordinate_sha256},
                "engine_ref": "engine.json", "gauge": "engine", "scale_status": al.scale_status,
                "time_base": {"kind": "session", "t0_ns": "0", "sync": "none"},
                "input": {"kind": src["kind"], "world_id": src["world_id"], "path": src["path"], "frames": src["frames"]},
                "counts": counts, "conf_source": "synthetic", "origin_engine": [float(v) for v in fused.origin_engine]})
    rep = validate_session(run.session_dir, deep=False)
    run.ctx.emit_extra(recon={"scale_status": al.scale_status, "alignment": al.summary(),
                              "ir_validation": {"ok": rep.ok, "errors": rep.errors[:20]}})
    run.ctx.state_extra.setdefault("recon", {}).update(scale_status=al.scale_status, alignment=al.summary())
    run.info.update(georef=al.summary(), scale_status=al.scale_status, c2c=c2c, qa_status=qa["status"])
    if not rep.ok:
        raise IrInvalid(f"Recon IR invalid ({len(rep.errors)} errors)", detail=rep.errors[:20])
    run.ctx.log("info", "georeferenced", **al.summary(), scale_status=al.scale_status)
    return [run.session_dir / n for n in ("alignment.json", "trajectory.bin", "qa.json", "session.json")]


# ---------------------------------------------------------------- TILING and PACKAGING
def _camera_home(run: ReconRun) -> dict:
    run.ensure_source()
    return {"position": [round(float(v), 1) for v in run.path.C[0]], "target": [round(float(v), 1) for v in run.path.target[0]],
            "fovDeg": float(run.params["params"]["camera"]["hfov_deg"])}


def stage_tiling(run: ReconRun) -> list[Path]:
    meta = run.ensure_meta()
    fused = _ensure_fused(run)
    if run.alignment is None:
        from .georef import Alignment as _A  # noqa: F401 - type only

        al_doc = json.loads((run.session_dir / "alignment.json").read_text(encoding="utf-8"))
        run.alignment = _alignment_from_doc(al_doc)
    eng = json.loads((run.session_dir / "engine.json").read_text(encoding="utf-8"))
    spec = build_ingest_spec(source=meta, al=run.alignment, fused=fused, target_world_id=run.target, session_id=run.session_id,
                             job_id=run.ctx.job_id, engine=run.engine_name, variant=eng["variant"], camera_home=_camera_home(run),
                             session_dir=run.session_dir)
    api = m03_arrays_api()
    if api is not None:                                            # M03-FR-021 delivered: single build_world call
        from awr.world.package.build import build_world

        run.info["builder"] = "m03.ArraysAdapter"
        res = build_world(api[1](to_m03_spec(spec, api[0])), run.worlds_dir, ctx=run.ctx)
        if res.exit_code != 0:
            err = TilingFailed if res.error_code not in ("VALIDATE_FAILED",) else PackageInvalid
            raise err(res.message or res.error_code, detail={"exit_code": res.exit_code, "error_code": res.error_code},
                      resumable=res.error_code == "IO_ERROR")
        run.build_result = res
        return []
    from awr.world.ingest.types import WorldpkgError

    from .m03_bridge import BridgeBuild

    run.info["builder"] = "m01.m03_bridge"
    b = BridgeBuild(spec, run.worlds_dir, run.ctx.job_id, run.ctx)
    run.builder = b
    try:
        b.tiling(progress=run.ctx.progress)
    except WorldpkgError as e:
        b.abort()
        run.builder = None
        raise TilingFailed(str(e), detail={"error_code": getattr(e, "error_code", "IO_ERROR")},
                           resumable=getattr(e, "error_code", "") == "IO_ERROR") from e
    return []


def stage_packaging(run: ReconRun) -> list[Path]:
    if run.build_result is None:
        from awr.world.ingest.types import ValidationFailed, WorldpkgError

        b = run.builder
        if b is None:
            raise TilingFailed("PACKAGING needs the TILING staging of the same attempt", resumable=True)
        try:
            run.build_result = b.packaging(progress=run.ctx.progress)
        except ValidationFailed as e:
            raise PackageInvalid(str(e)) from e
        except WorldpkgError as e:
            raise TilingFailed(str(e), resumable=getattr(e, "error_code", "") == "IO_ERROR") from e
        finally:
            if run.build_result is None:                        # failure or cancellation: drop the staging, release the lock
                b.abort()
            run.builder = None
    res = run.build_result
    wd = Path(run.worlds_dir) / run.target
    from awr.world.package.validate import validate_world

    rep = validate_world(wd, deep=True)
    if not rep.ok:
        raise PackageInvalid("published world fails validate --deep", detail=rep.errors[:20])
    run.info.update(content_version=res.content_version, world_dir=str(wd))
    run.ctx.log("info", "published", world_id=run.target, content_version=res.content_version)
    return [wd / "world.json"]


def _alignment_from_doc(d: dict) -> Alignment:
    from awr.world.georef.frames import Sim3

    return Alignment(Sim3.from_json(d["T_world_engine"]), d["method"], d["scale_status"], d["status"], bool(d["needs_review"]),
                     list(d.get("gates") or []), None, int(d.get("n_sync", 0)), d.get("reference"), dict(d.get("gravity") or {}),
                     {"inliers": d.get("inliers"), "rmse_m": d.get("rmse_m")})


STAGE_FUNCS = {"PREPARING": stage_preparing, "SEGMENTING": stage_segmenting, "INFERRING": stage_inferring,
               "FUSING": stage_fusing, "GEOREFERENCING": stage_georeferencing, "TILING": stage_tiling,
               "PACKAGING": stage_packaging}
