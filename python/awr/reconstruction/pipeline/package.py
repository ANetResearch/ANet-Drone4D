"""TILING and PACKAGING: assemble the M03 `IngestFromArrays` of M01 §6.8 and run `build_world` (M01-FR-030, FR-031).

`build_ingest_spec()` maps the recon outputs to the M03 §7.2 `IngestFromArrays` fields one to one: world-frame float64
points (engine points through T_world_engine), rotated normals, the source world's anchor and trueNorth (derived
worlds share the source world frame, AWR-16 §14.1), `registration`, dataset provenance with a rewritten notice,
`generator_params.recon`, the camera home of frame 0 and the session directory to publish under
`reconstruction/<session_id>/`.

M03 owns `IngestFromArrays` / `ArraysAdapter` (M03-FR-021, D1-ext, delivered by FX-SIM2): TILING and PACKAGING drive the
M03 two-phase builder `awr.world.package.arrays_build.PhasedBuild` (publish after the last cancellation checkpoint of
PACKAGING). The transitional bridge `m03_bridge` was removed once M03 delivered (request M01-to-M03 item 1).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from awr.world.georef.frames import Anchor, Sim3, quat_to_mat

from ..types import TilingFailed
from .fuse import Fused
from .georef import Alignment
from .source import WorldMeta

__all__ = ["ReconIngestSpec", "build_ingest_spec", "m03_arrays_api", "world_points"]


@dataclass(frozen=True)
class ReconIngestSpec:
    """Field-for-field twin of M03 §7.2 `IngestFromArrays` (M01 §6.8 mapping table)."""

    world_id: str
    name: str
    name_zh: str | None
    xyz_world: np.ndarray                       # float64 (N, 3), world m
    normals: np.ndarray | None                  # float32 (N, 3), rotated into world
    class_index: np.ndarray | None              # None: rule-based classification
    anchor: Anchor
    true_north: dict
    scale_status: str
    source_kind: str
    registration: dict
    source_origin_engine: tuple[float, float, float] | None
    dataset: dict
    generator_params: dict | None = None
    camera_home: dict | None = None
    recon_session_dir: Path | None = None
    # M01 extras (not part of the M03 dataclass): what the bridge needs to reproduce the source world frame
    anchor_json: dict | None = None             # coordinate.json anchor object of the source world (camelCase)
    T_ecef_world: list | None = None
    tags: tuple[str, ...] = ("recon", "synthetic")
    session_id: str = ""


def world_points(fused: Fused, S: Sim3) -> tuple[np.ndarray, np.ndarray]:
    """(xyz_world float64, normals_world float32): x_world = s R x_engine + t, normals only rotated (M01 §6.8)."""
    R = quat_to_mat(np.asarray(S.q, dtype=np.float64))
    X = S.s * (fused.xyz.astype(np.float64) + fused.origin_engine) @ R.T + np.asarray(S.t)
    N = (fused.normals.astype(np.float64) @ R.T).astype(np.float32)
    return X, N


def _names(target: str, source: WorldMeta, nn: str) -> tuple[str, str]:
    src_name = source.world.get("name") or source.world_id
    src_zh = source.world.get("nameZh") or src_name
    return f"{src_name} (Mock reconstruction {nn})", f"{src_zh}（Mock 重建 {nn}）"


def build_ingest_spec(*, source: WorldMeta, al: Alignment, fused: Fused, target_world_id: str, session_id: str, job_id: str,
                      engine: str, variant: str, camera_home: dict | None, session_dir: Path) -> ReconIngestSpec:
    X, N = world_points(fused, al.sim3)
    if len(X) < 1:
        raise TilingFailed("no fused points to ingest")
    ds = copy.deepcopy(source.world.get("dataset") or {})
    ds["notice"] = (f"Synthesised by MockEngine from {source.world_id}@{source.content_version}; not a real capture. "
                    f"Engine and weights: reconstruction/{session_id}/engine.json")
    nn = target_world_id.rsplit("-", 1)[-1] if target_world_id.rsplit("-", 1)[-1].isdigit() else "01"
    name, name_zh = _names(target_world_id, source, nn)
    reg = {"method": al.method, "T_world_map": al.sim3.to_json(), "alignmentRef": f"reconstruction/{session_id}/alignment.json"}
    r = al.report
    if al.method != "none":
        if r is not None and r.status != "failed":
            reg["rmseM"], reg["inliers"] = round(float(r.rmse_m), 6), int(r.inliers)
        elif al.extra.get("rmse_m") is not None:                    # resumed from alignment.json
            reg["rmseM"], reg["inliers"] = round(float(al.extra["rmse_m"]), 6), int(al.extra.get("inliers") or 0)
    anchor_json = copy.deepcopy(source.coordinate["anchor"])
    return ReconIngestSpec(
        world_id=target_world_id, name=name, name_zh=name_zh, xyz_world=X, normals=N, class_index=None,
        anchor=Anchor.from_coordinate(source.coordinate), true_north=copy.deepcopy(source.coordinate.get("trueNorth") or {}),
        scale_status=al.scale_status, source_kind="reconstruction", registration=reg,
        source_origin_engine=tuple(float(v) for v in fused.origin_engine), dataset=ds,
        generator_params={"job_id": job_id, "session_id": session_id, "engine": engine, "variant": variant},
        camera_home=camera_home, recon_session_dir=Path(session_dir), anchor_json=anchor_json,
        T_ecef_world=copy.deepcopy(source.coordinate.get("T_ecef_world")),
        tags=("recon", "synthetic") if engine == "mock" else ("recon", "real"), session_id=session_id)


def m03_arrays_api() -> tuple[Any, Any] | None:
    """(IngestFromArrays, ArraysAdapter) from M03 when delivered (M03-FR-021), else None."""
    try:
        from awr.world import ingest as m03
    except ImportError:
        return None
    a, b = getattr(m03, "IngestFromArrays", None), getattr(m03, "ArraysAdapter", None)
    return (a, b) if a is not None and b is not None else None


def to_m03_spec(spec: ReconIngestSpec, IngestFromArrays: Any) -> Any:
    kw: dict[str, Any] = dict(world_id=spec.world_id, name=spec.name, name_zh=spec.name_zh, xyz_world=spec.xyz_world,
                              normals=spec.normals, class_index=spec.class_index, anchor=spec.anchor,
                              true_north=spec.true_north, scale_status=spec.scale_status, source_kind=spec.source_kind,
                              registration=spec.registration, source_origin_engine=spec.source_origin_engine,
                              dataset=spec.dataset, generator_params=spec.generator_params, camera_home=spec.camera_home,
                              recon_session_dir=spec.recon_session_dir)
    # M03 §7.2 追加的可选字段（FX-SIM2）：源世界 anchor 原样、T_ecef_world、会话 id、标签、staging 名（job_id，续跑保留）
    extra = {"anchor_json": spec.anchor_json, "T_ecef_world": spec.T_ecef_world, "session_id": spec.session_id,
             "tags": tuple(spec.tags), "staging_nonce": (spec.generator_params or {}).get("job_id")}
    fields = getattr(IngestFromArrays, "__dataclass_fields__", {})
    kw.update({k: v for k, v in extra.items() if k in fields})
    return IngestFromArrays(**kw)
