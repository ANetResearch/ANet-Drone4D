"""`world.json`、`coordinate.json` 生成、`files[]` 与 `contentVersion`（AWR-16 §3.2–§3.4；M03 §6.9、FR-002、FR-003、FR-041）。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np

import awr

from ..ingest.anchor import t_ecef_world_json
from ..ingest.types import NormalizedCloud
from ..pointcloud.npx import colmax, colmin
from .jsonio import sha256_file

COORDINATE_HREF = "coordinate.json"
NON_CONTENT = ("world.json", "qa/report.json")
NON_CONTENT_DIRS = ("export/", ".work/")
CONVENTIONS = {"matrix": "row-major T_to_from", "quaternion": "xyzw, WORLD<-BODY(FLU)",
               "heading": "deg, north=0, clockwise", "windDirection": "meteorological-from",
               "render": "three Y-up: (x,y,z)=(E,U,-N)", "px4Boundary": "NED/FRD at PX4 adapter boundary only",
               "time": "int64 t_sim_ns"}
DTM_HREF = "geometry/terrain/dtm_10m.json"
DSM_HREF = "geometry/terrain/dsm_2m.json"
DSMN_HREF = "geometry/terrain/dsm_2m_n.json"
HAG_HREF = "geometry/terrain/hag_2m.json"
SOURCE_HREF = "geometry/pointcloud/source/source.json"
QA_HREF = "qa/report.json"


def _r6(v: float) -> float:
    return round(float(v), 6) + 0.0


def rounded3(v) -> list[float]:
    return [round(float(x), 3) + 0.0 for x in v]


def build_coordinate(nc: NormalizedCloud, *, source_dataset: str | None, qa_status: str) -> dict:
    cfg = nc.config
    s = nc.stats
    lo, hi = colmin(nc.xyz), colmax(nc.xyz)
    ext_min, ext_max = rounded3(lo), rounded3(hi)
    from awr.world.georef.frames import precision_report

    return {
        "schemaVersion": "1.0.0",
        "worldId": nc.world_id,
        "frame": {"id": "world", "type": "ENU", "units": "m", "handedness": "right", "upAxis": "+Z"},
        "anchor": nc.anchor,
        "T_ecef_world": t_ecef_world_json(nc.anchor),
        "trueNorth": {"yawOffsetDeg": 0.0, "confidence": cfg.true_north, "evidence": list(cfg.north_evidence)},
        "scaleStatus": cfg.scale_status,
        "source": {
            "kind": cfg.source_kind, "dataset": source_dataset,
            "files": [{"name": f["name"], "bytes": int(f["bytes"]), "points": int(f["points"]), "sha256": f["sha256"]}
                      for f in nc.source_files],
            "crs": "LOCAL", "projPipeline": None, "handedness": "right", "upAxis": cfg.up_axis,
            "unitsToMeters": float(cfg.units_to_m), "leveledDeg": round(float(s.leveled_deg), 3),
            "yawDeg": float(cfg.yaw_deg),
            "T_world_source": [[_r6(v) for v in row] for row in nc.T_world_source],
            "evidence": list(cfg.evidence),
        },
        "registration": None,
        "ground": {"type": nc.terrain.ground_type, "zM": 0.0,
                   "dtm": {"href": DTM_HREF, "cellM": float(nc.terrain.dtm_cell_m)},
                   "reliefP1P99M": [float(s.relief_p1p99_m[0]), float(s.relief_p1p99_m[1])]},
        "extent": {"min": ext_min, "max": ext_max},
        "precision": precision_report(ext_min, ext_max),
        "conventions": dict(CONVENTIONS),
        "qa": {"status": qa_status, "normalsFlippedFrac": round(s.normals_flipped_frac, 4),
               "zeroNormalsFrac": round(s.zero_normals_frac, 5), "groundFrac": round(s.ground_frac, 3),
               "nnMedianM": s.nn_median_m, "maxHeightM": round(float(hi[2]), 1), "tiltRawDeg": round(s.tilt_raw_deg, 3),
               "gates": [g.to_coordinate() for g in nc.gates]},
    }


def scan_content_files(stage: Path) -> list[dict]:
    """遍历世界目录（排除非内容文件）：按 `path` 字节序排序、流式 sha256（16 §3.4 第 1 条）。"""
    stage = Path(stage)
    out = []
    for dp, dns, fns in os.walk(stage):
        dns[:] = [d for d in dns if not d.startswith(".")]
        for fn in fns:
            p = Path(dp) / fn
            rel = p.relative_to(stage).as_posix()
            if rel in NON_CONTENT or rel.startswith(NON_CONTENT_DIRS) or fn.startswith("."):
                continue
            if p.is_symlink():
                raise OSError(f"symbolic link in world package: {rel}")
            out.append({"path": rel, "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    out.sort(key=lambda f: f["path"].encode("utf-8"))
    return out


def content_version(files: list[dict]) -> str:
    s = "".join(f"{f['path']}:{f['sha256']}\n" for f in sorted(files, key=lambda f: f["path"].encode("utf-8")))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def camera_home(E_min: np.ndarray, E_max: np.ndarray) -> dict:
    span = float(max(E_max[0] - E_min[0], E_max[1] - E_min[1]))
    return {"position": [round(float(E_min[0]) * 0.9, 1), round(float(E_min[1]) * 1.2, 1), round(span * 0.45, 1)],
            "target": [0.0, 0.0, 0.0], "fovDeg": 50}


def git_commit() -> str | None:
    head = Path(awr.__file__).resolve().parents[2] / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            return (head.parent / ref[5:]).read_text().strip() or None
        return ref or None
    except OSError:
        return None


def build_world_json(*, world_id: str, extras: dict, created_at: str, generator_params: dict, dataset: dict | None,
                     coordinate_sha256: str, scale_status: str, extent: dict, layers: list[dict], first_screen: dict,
                     render: dict, camera: dict, stats: dict, qa: dict, files: list[dict]) -> dict:
    w = {"schemaVersion": "1.0.0", "id": world_id, "name": extras["name"]}
    if extras.get("nameZh"):
        w["nameZh"] = extras["nameZh"]
    if extras.get("description"):
        w["description"] = extras["description"]
    if extras.get("tags"):
        w["tags"] = list(extras["tags"])
    w.update({
        "contentVersion": content_version(files),
        "createdAt": created_at,
        "generator": {"name": "worldpkg", "version": awr.__version__, "commit": git_commit(), "params": generator_params},
        "dataset": dataset,
        "coordinate": {"href": COORDINATE_HREF, "sha256": coordinate_sha256},
        "scaleStatus": scale_status,
        "bounds": {"min": list(extent["min"]), "max": list(extent["max"])},
        "layers": layers,
        "lod": {"errorTargetPx": 1.35, "firstScreen": first_screen},
        "render": render,
        "camera": {"home": camera},
        "stats": stats,
        "qa": qa,
        "files": files,
    })
    return w
