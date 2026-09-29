"""Source world access for the MockEngine and the product world (M01-FR-016, FR-024, §6.8).

Reads the source world's manifest (status, contentVersion, coordinate sha256) and its full-resolution geometry
`geometry/pointcloud/source/` (`awr-pts@1`, M03 `awr.world.pointcloud.source.read_source`), subsamples it with
`source_keep` (stream 11, fixed seed) and builds the 64 m planar cell index used for view culling.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..types import STREAM_RECON_MOCK_SUBSAMPLE, InputInvalid, rng

__all__ = ["CellGrid", "SourceCloud", "WorldMeta", "load_world_meta", "prepare_source", "read_full_source_xyz"]

CELL_M = 64.0


@dataclass
class WorldMeta:
    world_id: str
    dir: Path
    world: dict
    coordinate: dict
    content_version: str
    coordinate_sha256: str
    status: str

    @property
    def extent(self) -> tuple[np.ndarray, np.ndarray]:
        e = self.coordinate["extent"]
        return np.asarray(e["min"], dtype=np.float64), np.asarray(e["max"], dtype=np.float64)


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_world_meta(worlds_dir: Path, world_id: str) -> WorldMeta:
    d = Path(worlds_dir) / world_id
    try:
        w = json.loads((d / "world.json").read_text(encoding="utf-8"))
        c = json.loads((d / "coordinate.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise InputInvalid(f"source world {world_id!r} is not readable: {e}", detail={"world_id": world_id}) from e
    status = "unknown"
    st = Path(worlds_dir) / ".status" / f"{world_id}.json"
    try:
        s = json.loads(st.read_text(encoding="utf-8"))
        status = "ready" if s.get("status") == "ready" and s.get("content_version") == w.get("contentVersion") else s.get("status", "stale")
    except (OSError, ValueError):
        status = "ready" if (d / "world.json").exists() else "missing"
    return WorldMeta(world_id, d, w, c, str(w.get("contentVersion")), _sha256(d / "coordinate.json"), status)


@dataclass
class CellGrid:
    cell_m: float
    starts: np.ndarray       # int64 per occupied cell
    counts: np.ndarray       # int64 per occupied cell
    cx: np.ndarray           # cell centre x (world m)
    cy: np.ndarray

    def cull(self, C: np.ndarray, fwd: np.ndarray, *, far_m: float = 900.0, cone_deg: float = 70.0,
             near_r_m: float = 250.0) -> np.ndarray:
        """Indices of the points in cells within `far_m` that lie in the forward horizontal cone or closer than
        `near_r_m` (M01 §6.4.1 culling)."""
        d = np.c_[self.cx - C[0], self.cy - C[1]]
        dist = np.hypot(d[:, 0], d[:, 1])
        fh = np.asarray(fwd[:2], dtype=np.float64)
        nf = np.linalg.norm(fh)
        if nf < 1e-9:
            sel = dist < far_m
        else:
            cosang = (d @ (fh / nf)) / np.maximum(dist, 1e-9)
            sel = (dist < far_m) & ((cosang > np.cos(np.radians(cone_deg))) | (dist < near_r_m))
        lens = self.counts[sel]
        if len(lens) == 0:
            return np.zeros(0, dtype=np.int64)
        total = int(lens.sum())
        offs = np.cumsum(lens) - lens
        return np.arange(total, dtype=np.int64) - np.repeat(offs, lens) + np.repeat(self.starts[sel], lens)


@dataclass
class SourceCloud:
    meta: WorldMeta
    xyz: np.ndarray                 # float64 (n, 3), world m, sorted by cell
    normals: np.ndarray             # float32 (n, 3)
    grid: CellGrid
    ground_z_m: float
    keep: float
    n_full: int
    extra: dict = field(default_factory=dict)


def _grid(P: np.ndarray, cell: float) -> tuple[np.ndarray, CellGrid]:
    gx = np.floor(P[:, 0] / cell).astype(np.int64)
    gy = np.floor(P[:, 1] / cell).astype(np.int64)
    gx0, gy0 = gx.min(), gy.min()
    W = int(gx.max() - gx0 + 1)
    cid = (gy - gy0) * W + (gx - gx0)
    order = np.argsort(cid, kind="stable")
    cs = cid[order]
    uc, starts, counts = np.unique(cs, return_index=True, return_counts=True)
    cx = (uc % W + gx0 + 0.5) * cell
    cy = (uc // W + gy0 + 0.5) * cell
    return order, CellGrid(cell, starts.astype(np.int64), counts.astype(np.int64), cx.astype(np.float64), cy.astype(np.float64))


def read_full_source_xyz(meta: WorldMeta) -> np.ndarray:
    from awr.world.pointcloud.source import read_source

    return read_source(meta.dir).xyz


def prepare_source(worlds_dir: Path, world_id: str, *, keep: float, seed: int) -> SourceCloud:
    from awr.world.pointcloud.encode import oct16_decode
    from awr.world.pointcloud.source import read_source

    meta = load_world_meta(worlds_dir, world_id)
    if not (meta.dir / "geometry" / "pointcloud" / "source" / "source.json").exists():
        raise InputInvalid(f"source world {world_id!r} has no full-resolution geometry", detail={"world_id": world_id})
    sc = read_source(meta.dir)
    n = len(sc.xyz)
    m = rng(seed, STREAM_RECON_MOCK_SUBSAMPLE).random(n) < keep
    P = sc.xyz[m].astype(np.float64)
    N = oct16_decode(sc.normal[m]).astype(np.float32)
    if len(P) < 1000:
        raise InputInvalid(f"source world {world_id!r} keeps only {len(P)} points at source_keep={keep}",
                           detail={"world_id": world_id, "points": len(P)})
    order, grid = _grid(P, CELL_M)
    P = P[order]
    N = N[order]
    ground = float(np.percentile(P[:, 2], 5.0))
    return SourceCloud(meta, P, N, grid, ground, keep, n)
