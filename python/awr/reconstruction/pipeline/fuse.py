"""FUSING: filtering, back-projection and footprint-voxel de-duplication (M01 §6.5; M01-FR-027). D1-ext.

Reads the per-frame shards written by INFERRING (`runs/jobs/<job>/frames/<frame_id>.npz`: depth f16 in engine units,
conf u8, engine-gauge normals f16, K of the depth map in COLMAP pixel centres) and the raw-layer poses. The voxel edge is
`voxel_k` times the median pixel footprint (depth / focal), so it does not depend on metric scale; within a voxel the
first-seen sample (lowest frame id, then pixel index) is kept, which makes the result independent of shard read order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..types import ReconEmpty

__all__ = ["Fused", "fuse", "load_fused", "load_shard", "save_fused", "save_shard"]


@dataclass
class Fused:
    xyz: np.ndarray            # float32 (n, 3), engine units, relative to origin_engine
    origin_engine: np.ndarray  # float64 (3,)
    normals: np.ndarray        # float32 (n, 3), engine gauge (unit or zero)
    conf_u8: np.ndarray        # uint8 (n,)
    first_seen: np.ndarray     # uint32 (n,) frame id
    conf_hist: np.ndarray      # int64 (256,)
    vox_engine: float
    n_raw: int

    @property
    def n(self) -> int:
        return len(self.xyz)

    def xyz_engine(self) -> np.ndarray:
        return self.xyz.astype(np.float64) + self.origin_engine


def save_shard(path: Path, depth: np.ndarray, conf_u8: np.ndarray | None, normals: np.ndarray | None, K_depth: np.ndarray) -> None:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.npz")
    arrays = {"depth": np.asarray(depth, dtype=np.float16), "K": np.asarray(K_depth, dtype=np.float64)}
    if conf_u8 is not None:
        arrays["conf"] = np.asarray(conf_u8, dtype=np.uint8)
    if normals is not None:
        arrays["normals"] = np.asarray(normals, dtype=np.float16)
    np.savez(tmp, **arrays)
    os.replace(tmp, path)


def load_shard(path: Path) -> dict:
    with np.load(path) as z:
        return {k: z[k] for k in z.files}


def fuse(shard_dir: Path, rows: list[dict], T_engine_cam: np.ndarray, p: dict, *, check: Callable[[], None] = lambda: None,
         progress: Callable[[float], None] = lambda f: None, rng_: np.random.Generator | None = None) -> Fused:
    """Fuse the key frames (or all frames when keyframes_only is false) into a de-duplicated engine-gauge cloud."""
    use = [i for i, r in enumerate(rows) if r["frame_type"] != 2 or not p.get("keyframes_only", True)]
    if not use:
        raise ReconEmpty("no frames to fuse")
    shards = {i: load_shard(Path(shard_dir) / f"{rows[i]['frame_id']:06d}.npz") for i in use}
    fp = []
    for i in use:
        d = shards[i]["depth"].astype(np.float64)
        v = d[d > 0]
        if len(v):
            fp.append(float(np.median(v)) / float(shards[i]["K"][0, 0]))
    if not fp:
        raise ReconEmpty("no valid depth in any frame")
    vox = float(p.get("voxel_k", 0.5)) * float(np.median(fp))
    conf_min = int(p.get("conf_min_u8", 85))
    depth_pct = float(p.get("depth_pct", 98.0))
    conf_pct = float(p.get("conf_pct", 0.0))
    stride = int(p.get("stride", 1))
    jitter = bool(p.get("jitter", False))
    xs, cs, ns, fs, ps = [], [], [], [], []
    for j, i in enumerate(use):
        if j % 25 == 0:
            check()
            progress(0.8 * j / len(use))
        s = shards[i]
        d = s["depth"].astype(np.float64)
        c = s.get("conf")
        c = np.full(d.shape, 255, dtype=np.uint8) if c is None else c
        m = (d > 0) & (c >= conf_min)
        if not m.any():
            continue
        if depth_pct < 100:
            m &= d < np.percentile(d[m], depth_pct)                   # drop the farthest 2 % (sky shell, far noise)
        if conf_pct > 0 and m.any():
            m &= c > np.percentile(c[m], conf_pct)
        v, u = np.nonzero(m)
        v, u = v[::stride], u[::stride]
        if len(u) == 0:
            continue
        if jitter and rng_ is not None:
            du, dv = rng_.uniform(-0.4, 0.4, (2, len(u))) * stride
        else:
            du = dv = 0.0
        z = d[v, u]
        K = s["K"]
        Xc = np.c_[(u + 0.5 + du - K[0, 2]) * z / K[0, 0], (v + 0.5 + dv - K[1, 2]) * z / K[1, 1], z]
        T = T_engine_cam[i]
        xs.append(Xc @ T[:3, :3].T + T[:3, 3])
        cs.append(c[v, u])
        nrm = s.get("normals")
        ns.append(np.zeros((len(u), 3)) if nrm is None else nrm[v, u].astype(np.float64))   # shards hold engine-gauge normals
        fs.append(np.full(len(u), rows[i]["frame_id"], dtype=np.uint32))
        ps.append((v * d.shape[1] + u).astype(np.uint32))
    if not xs:
        raise ReconEmpty("every frame was filtered out")
    X = np.vstack(xs)
    C = np.concatenate(cs)
    Nn = np.vstack(ns)
    F = np.concatenate(fs)
    P = np.concatenate(ps)
    n_raw = len(X)
    check()
    key = np.floor(X / vox).astype(np.int64)
    key -= key.min(axis=0)
    if int(key.max()) < (1 << 21):
        k1 = (key[:, 0] << 42) | (key[:, 1] << 21) | key[:, 2]
        order = np.lexsort((P, F, k1))
        ks = k1[order]
        first = order[np.r_[True, ks[1:] != ks[:-1]]]
    else:                                                          # > 2^21 voxels per axis: fall back to row-unique
        order = np.lexsort((P, F, key[:, 2], key[:, 1], key[:, 0]))
        ks = key[order]
        first = order[np.r_[True, np.any(ks[1:] != ks[:-1], axis=1)]]
    first = first[np.lexsort((P[first], F[first]))]                # frame, then pixel order
    progress(0.95)
    Xf = X[first]
    origin = np.floor(Xf.mean(axis=0))
    nf = Nn[first]
    nn = np.linalg.norm(nf, axis=1, keepdims=True)
    nf = np.where(nn > 0, nf / np.where(nn > 0, nn, 1.0), 0.0).astype(np.float32)
    conf = C[first].astype(np.uint8)
    return Fused(xyz=(Xf - origin).astype(np.float32), origin_engine=origin.astype(np.float64), normals=nf, conf_u8=conf,
                 first_seen=F[first].astype(np.uint32), conf_hist=np.bincount(conf, minlength=256).astype(np.int64),
                 vox_engine=vox, n_raw=int(n_raw))


def save_fused(path: Path, f: Fused) -> None:
    import os

    tmp = Path(path).with_name(Path(path).name + ".tmp.npz")
    np.savez(tmp, xyz=f.xyz, origin_engine=f.origin_engine, normals=f.normals.astype(np.float16), conf_u8=f.conf_u8,
             first_seen=f.first_seen, conf_hist=f.conf_hist, vox_engine=np.float64(f.vox_engine), n_raw=np.int64(f.n_raw))
    os.replace(tmp, path)


def load_fused(path: Path) -> Fused:
    with np.load(path) as z:
        return Fused(xyz=z["xyz"], origin_engine=z["origin_engine"], normals=z["normals"].astype(np.float32),
                     conf_u8=z["conf_u8"], first_seen=z["first_seen"], conf_hist=z["conf_hist"],
                     vox_engine=float(z["vox_engine"]), n_raw=int(z["n_raw"]))
