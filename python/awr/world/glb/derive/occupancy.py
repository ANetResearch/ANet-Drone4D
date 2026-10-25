"""占据体素 `awr-occ@1`（M22 §6.2 第 7 阶段、§6.6.10、§7.3.2；M22-FR-031；D3-AC-04 "occupancy 中覆盖外体素为 unknown"）。

16³ 稀疏砖块四态体素（0 unknown、1 free、2 occupied、3 below_surface），体素边长取配方 `build.voxel_m`（crop 1 m、town 2 m、
hill 0.25 m）。规则：

- 表面体素：三角形重心坐标格点（间距 v / 3，含三个顶点）所在体素为 occupied；
- 体素柱的竖向采样（每柱 2 × 2，`awr.world.glb.raster`）给出柱顶 z：覆盖内且有命中的柱，z_center > 柱顶为 free，其余为 below_surface；
- 覆盖外的柱（与 `coverage_mask(dsm_2m_n)` 的覆盖格无重叠）一律 unknown，含其中的表面体素；覆盖内无竖向命中的柱、质量位为
  nodata 或 hole 的柱为 unknown（表面体素仍为 occupied）。表面之下与洞一律不视为空地；
- 砖块柱全部在覆盖外为 `top = 0xFFFF`（整柱 unknown，不写砖块）；砖块柱内全部体素柱均为已知（覆盖内且有命中）时，`top` 取
  `brick_of(max(柱顶) + v)` 与最高表面体素所在砖块的较大者，`top` 之上的砖块隐式为 free；其余砖块柱全部砖块显式写出，因此读取方
  不需要覆盖信息即可解码（`implicit = free-in-cover`）；
- 砖块键为 (bx, by, bz) 的三维 Morton 交织（x 在最低位），`bricks.idx` 按键升序；砖块内体素按 (z, y, x) 行主序；全同值砖块
  `flags & 1`、数据 1 字节；其余为 zstd（level 3）压缩的 4096 字节。

ESDF（P0b）不在本阶段。全部计算为确定性：格点权重、采样位置与砖块次序固定，zstd 输出对相同输入逐字节相同。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from awr.world.glb.raster import raster_top

__all__ = ["BELOW", "BRICK", "FREE", "HREF_FMT", "IDX_DTYPE", "OCCUPIED", "UNKNOWN", "dense_states", "morton3", "occ_href", "read_states",
           "surface_voxels", "write_occupancy"]

UNKNOWN, FREE, OCCUPIED, BELOW = 0, 1, 2, 3
STATES = {"0": "unknown", "1": "free", "2": "occupied", "3": "below_surface"}
BRICK = 16
TOP_NONE = 0xFFFF
IDX_DTYPE = np.dtype([("key", "<u8"), ("offset", "<u8"), ("length", "<u4"), ("flags", "u1")])
HREF_FMT = "geometry/voxel/occ_{v}/"
COLUMN_K = 2                      # 每个体素柱 2 × 2 竖向采样（M22 §6.6.10 `raster(k=2 per voxel side)`）
LATTICE_DIV = 3                   # 重心格点间距 v / 3
LATTICE_MAX = 4096                # 单个三角形每边最多分段（极端拉伸面）
CHUNK_POINTS = 4_000_000
ZSTD_LEVEL = 3
TRUNC_M = 20.0


def occ_href(voxel_m: float) -> str:
    """`geometry/voxel/occ_<v>/`，`<v>` 写作 `0p25m`、`1m`、`2m`（M22 §7.1.3）。"""
    return HREF_FMT.format(v=f"{voxel_m:g}".replace(".", "p") + "m")


def morton3(x: np.ndarray | int, y: np.ndarray | int, z: np.ndarray | int) -> np.ndarray:
    """(x, y, z) 每轴 21 位的三维 Morton 交织（x 在最低位）。"""
    x, y, z = (np.asarray(a, np.uint64) for a in (x, y, z))
    k = np.zeros(np.broadcast(x, y, z).shape, np.uint64)
    one = np.uint64(1)
    for b in range(21):
        bb = np.uint64(b)
        k |= ((x >> bb) & one) << np.uint64(3 * b) | ((y >> bb) & one) << np.uint64(3 * b + 1) | ((z >> bb) & one) << np.uint64(3 * b + 2)
    return k


def surface_voxels(V: np.ndarray, F: np.ndarray, origin: np.ndarray, v: float, dims: np.ndarray) -> np.ndarray:
    """表面体素的线性下标 `(iz · ny + iy) · nx + ix`（升序、去重）：每个三角形按最长边取 n = ceil(edge_max / (v / 3)) 段，
    重心格点 (i, j, n − i − j) / n（含顶点，顶点坐标逐位不变）。"""
    V = np.asarray(V, np.float64)
    F = np.asarray(F, np.int64)
    if len(F) == 0:
        return np.zeros(0, np.int64)
    o = np.asarray(origin, np.float64)
    nx, ny, nz = (int(d) for d in dims)
    P = V[F]
    emax = np.max(np.stack([np.linalg.norm(P[:, (i + 1) % 3] - P[:, i], axis=1) for i in range(3)], 1), axis=1)
    n_seg = np.clip(np.ceil(emax / (v / LATTICE_DIV)), 1, LATTICE_MAX).astype(np.int64)
    parts: list[np.ndarray] = []
    for n in np.unique(n_seg).tolist():
        tris = np.flatnonzero(n_seg == n)
        i, j = np.meshgrid(np.arange(n + 1), np.arange(n + 1), indexing="ij")
        m = (i + j) <= n
        Wt = np.stack([i[m], j[m], n - i[m] - j[m]], 1).astype(np.float64) / float(n)
        per = max(1, CHUNK_POINTS // len(Wt))
        for t0 in range(0, len(tris), per):
            T = P[tris[t0:t0 + per]]
            pts = (Wt[None, :, 0, None] * T[:, None, 0] + Wt[None, :, 1, None] * T[:, None, 1] + Wt[None, :, 2, None] * T[:, None, 2])
            ijk = np.floor((pts.reshape(-1, 3) - o) / v).astype(np.int64)
            ok = (ijk[:, 0] >= 0) & (ijk[:, 0] < nx) & (ijk[:, 1] >= 0) & (ijk[:, 1] < ny) & (ijk[:, 2] >= 0) & (ijk[:, 2] < nz)
            ijk = ijk[ok]
            parts.append(np.unique((ijk[:, 2] * ny + ijk[:, 1]) * nx + ijk[:, 0]))
    return np.unique(np.concatenate(parts)) if parts else np.zeros(0, np.int64)


def _column_footprint(nx: int, ny: int, o: np.ndarray, v: float, cover: np.ndarray, c_org: tuple[float, float],
                      c_cell: float) -> np.ndarray:
    """体素柱 (ny, nx) 是否与任一覆盖格重叠：柱内按 ≤ 覆盖格边长的间距取 m × m 个点（距柱边 1e-6 m）查覆盖。"""
    Hc, Wc = cover.shape
    m = max(2, math.ceil(v / c_cell) + 1)
    offs = np.linspace(1e-6, v - 1e-6, m)
    xs = o[0] + np.arange(nx) * v
    ys = o[1] + np.arange(ny) * v
    out = np.zeros((ny, nx), bool)
    for dy in offs:
        r = np.floor((ys + dy - c_org[1]) / c_cell).astype(np.int64)
        rok = (r >= 0) & (r < Hc)
        for dx in offs:
            c = np.floor((xs + dx - c_org[0]) / c_cell).astype(np.int64)
            cok = (c >= 0) & (c < Wc)
            sub = cover[np.clip(r, 0, Hc - 1)][:, np.clip(c, 0, Wc - 1)] & rok[:, None] & cok[None, :]
            out |= sub
    return out


def write_occupancy(stage: Path, world_id: str, coordinate_sha256: str, V: np.ndarray, F: np.ndarray, *, voxel_m: float,
                    bounds_min, bounds_max, cover: np.ndarray, cover_origin_xy: tuple[float, float], cover_cell_m: float,
                    quality: tuple[np.ndarray, tuple[float, float], float] | None = None) -> dict:
    """写 `geometry/voxel/occ_<v>/`（index.json、top.u16、bricks.idx、bricks.bin），返回 index.json 文档。

    `V`、`F` 为求交网格（写出后坐标）；`cover` 为 DSM 格上的覆盖（`coverage_mask(dsm_2m_n)`）；`quality` 为 (质量位栅格, 原点, 格边长)，
    nodata 与 hole 位的柱为 unknown。
    """
    import zstandard

    from awr.world.glb.derive.quality import BIT_HOLE, BIT_NODATA

    v = float(voxel_m)
    B = BRICK
    bmin, bmax = np.asarray(bounds_min, np.float64), np.asarray(bounds_max, np.float64)
    o = np.floor(bmin / v) * v
    dims = (np.floor((bmax - o) / v).astype(np.int64) + 1)
    nx, ny, nz = (int(d) for d in dims)
    nbx, nby, nbz = (-(-nx // B)), (-(-ny // B)), (-(-nz // B))
    occ = surface_voxels(V, F, o, v, dims)
    col_top, col_n = raster_top(V, F, (float(o[0]), float(o[1])), v, ny, nx, COLUMN_K)
    foot = _column_footprint(nx, ny, o, v, np.asarray(cover, bool), cover_origin_xy, cover_cell_m)
    known = foot & (col_n > 0)
    if quality is not None:
        Q, q_org, q_cell = quality
        Hq, Wq = Q.shape
        cx = o[0] + (np.arange(nx) + 0.5) * v
        cy = o[1] + (np.arange(ny) + 0.5) * v
        qc = np.floor((cx - q_org[0]) / q_cell).astype(np.int64)
        qr = np.floor((cy - q_org[1]) / q_cell).astype(np.int64)
        bad = (Q[np.clip(qr, 0, Hq - 1)][:, np.clip(qc, 0, Wq - 1)] & (BIT_NODATA | BIT_HOLE)) > 0
        bad |= ~(((qr >= 0) & (qr < Hq))[:, None] & ((qc >= 0) & (qc < Wq))[None, :])
        known &= ~bad
    # 补齐到整砖块：补出的柱在覆盖外
    PX, PY = nbx * B, nby * B
    footp = np.zeros((PY, PX), bool)
    footp[:ny, :nx] = foot
    knownp = np.zeros((PY, PX), bool)
    knownp[:ny, :nx] = known
    topp = np.full((PY, PX), -np.inf)
    topp[:ny, :nx] = np.where(known, col_top, -np.inf)
    ix = occ % nx
    iy = (occ // nx) % ny
    iz = occ // (nx * ny)
    sel = foot[iy, ix]                                                   # 覆盖外的表面体素为 unknown
    ix, iy, iz = ix[sel], iy[sel], iz[sel]
    bc = (iy // B) * nbx + ix // B
    order = np.lexsort((iz, iy, ix, bc))
    ix, iy, iz, bc = ix[order], iy[order], iz[order], bc[order]
    bstart = np.searchsorted(bc, np.arange(nbx * nby + 1))
    zc_all = o[2] + (np.arange(nbz * B) + 0.5) * v
    top = np.full((nby, nbx), TOP_NONE, np.uint16)
    cctx = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
    keys: list[np.ndarray] = []
    blobs: list[bytes] = []
    flags: list[int] = []
    counts = np.zeros(4, np.int64)
    n_uniform = 0
    for by in range(nby):
        for bx in range(nbx):
            ys, xs = slice(by * B, by * B + B), slice(bx * B, bx * B + B)
            kn = knownp[ys, xs]
            s0, s1 = int(bstart[by * nbx + bx]), int(bstart[by * nbx + bx + 1])
            if not kn.any() and s1 == s0:
                continue                                                 # 整柱 unknown（隐式）
            tz = topp[ys, xs]
            if kn.all():
                tmax = float(tz.max())
                tb = int(np.floor((tmax + v - o[2]) / v)) // B if np.isfinite(tmax) else 0
                if s1 > s0:
                    tb = max(tb, int(iz[s0:s1].max()) // B)
                tb = min(max(tb, 0), nbz - 1)
            else:
                tb = nbz - 1
            top[by, bx] = tb
            zc = zc_all[:(tb + 1) * B][:, None, None]
            st = np.where(kn[None], np.where(zc > tz[None], FREE, BELOW), UNKNOWN).astype(np.uint8)
            if s1 > s0:
                lz, ly, lx = iz[s0:s1], iy[s0:s1] - by * B, ix[s0:s1] - bx * B
                keep = lz < (tb + 1) * B
                st[lz[keep], ly[keep], lx[keep]] = OCCUPIED
            counts += np.bincount(st.ravel(), minlength=4)[:4]
            bz = np.arange(tb + 1)
            keys.append(morton3(np.full(tb + 1, bx), np.full(tb + 1, by), bz))
            for z in range(tb + 1):
                blk = st[z * B:(z + 1) * B]
                first = blk.flat[0]
                if (blk == first).all():
                    blobs.append(bytes([int(first)]))
                    flags.append(1)
                    n_uniform += 1
                else:
                    blobs.append(cctx.compress(blk.tobytes()))
                    flags.append(0)
    out = Path(stage) / occ_href(v)
    out.mkdir(parents=True, exist_ok=True)
    kk = np.concatenate(keys) if keys else np.zeros(0, np.uint64)
    order = np.argsort(kk, kind="stable")
    idx = np.zeros(len(kk), IDX_DTYPE)
    data = bytearray()
    for r, j in enumerate(order.tolist()):
        b = blobs[j]
        idx[r] = (int(kk[j]), len(data), len(b), flags[j])
        data += b
    top.astype("<u2").tofile(out / "top.u16")
    idx.tofile(out / "bricks.idx")
    (out / "bricks.bin").write_bytes(bytes(data))
    doc = {"schema": "awr.occ.v1", "schema_version": "1.0.0", "world_id": world_id, "coordinate_sha256": coordinate_sha256,
           "kind": "occupancy", "voxel_m": v, "brick": B, "origin_m": [float(x) for x in o], "dims": [nx, ny, nz], "states": STATES,
           "top": {"href": "top.u16", "width": nbx, "height": nby},
           "bricks": {"index": "bricks.idx", "data": "bricks.bin", "codec": "zstd", "count": len(kk), "key": "morton3-xyz",
                      "voxel_order": "zyx"},
           "trunc_m": TRUNC_M, "implicit": "free-in-cover",
           "stats": {"bricks": len(kk), "uniform_bricks": n_uniform, "bytes": len(data), "surface_voxels": int(sel.sum()),
                     "footprint_columns": int(foot.sum()), "known_columns": int(known.sum()), "columns": nx * ny,
                     "explicit_voxels": {STATES[str(s)]: int(counts[s]) for s in range(4)}}}
    (out / "index.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return doc


def read_states(index_json: Path, vox: np.ndarray) -> np.ndarray:
    """按 `awr-occ@1` 解码 vox (n, 3) 体素下标 → u8 状态（本模块写出的层：`top = 0xFFFF` 为 unknown，`top` 之上为 free，网格外为
    unknown）。"""
    import zstandard

    index_json = Path(index_json)
    doc = json.loads(index_json.read_text(encoding="utf-8"))
    base = index_json.parent
    B = int(doc["brick"])
    dims = np.asarray(doc["dims"], np.int64)
    tp = doc["top"]
    top = np.fromfile(base / tp["href"], "<u2").reshape(int(tp["height"]), int(tp["width"]))
    idx = np.fromfile(base / doc["bricks"]["index"], IDX_DTYPE)
    data = (base / doc["bricks"]["data"]).read_bytes()
    vox = np.asarray(vox, np.int64).reshape(-1, 3)
    out = np.full(len(vox), UNKNOWN, np.uint8)
    inside = np.all((vox >= 0) & (vox < dims), axis=1)
    dctx = zstandard.ZstdDecompressor()
    cache: dict[int, np.ndarray] = {}
    bxyz = vox // B
    keys = morton3(bxyz[:, 0], bxyz[:, 1], bxyz[:, 2])
    for i in np.flatnonzero(inside).tolist():
        bx, by, bz = (int(a) for a in bxyz[i])
        t = int(top[by, bx])
        if t == TOP_NONE:
            continue
        if bz > t:
            out[i] = FREE
            continue
        j = int(np.searchsorted(idx["key"], keys[i]))
        if j >= len(idx) or idx["key"][j] != keys[i]:
            continue
        if j not in cache:
            off, ln, fl = int(idx["offset"][j]), int(idx["length"][j]), int(idx["flags"][j])
            raw = data[off:off + ln]
            cache[j] = np.full(B ** 3, raw[0], np.uint8) if fl & 1 else np.frombuffer(dctx.decompress(raw, max_output_size=B ** 3), np.uint8)
        lx, ly, lz = (int(a) for a in vox[i] % B)
        out[i] = cache[j][(lz * B + ly) * B + lx]
    return out


def dense_states(index_json: Path) -> np.ndarray:
    """整层解码为稠密 (nz, ny, nx) u8（小世界与测试用；规则同 `read_states`）。"""
    import zstandard

    index_json = Path(index_json)
    doc = json.loads(index_json.read_text(encoding="utf-8"))
    base = index_json.parent
    B = int(doc["brick"])
    nx, ny, nz = (int(d) for d in doc["dims"])
    tp = doc["top"]
    nbx, nby = int(tp["width"]), int(tp["height"])
    nbz = -(-nz // B)
    top = np.fromfile(base / tp["href"], "<u2").reshape(nby, nbx)
    idx = np.fromfile(base / doc["bricks"]["index"], IDX_DTYPE)
    data = (base / doc["bricks"]["data"]).read_bytes()
    g = np.full((nbz * B, nby * B, nbx * B), UNKNOWN, np.uint8)
    for by in range(nby):
        for bx in range(nbx):
            t = int(top[by, bx])
            if t != TOP_NONE:
                g[(t + 1) * B:, by * B:(by + 1) * B, bx * B:(bx + 1) * B] = FREE
    dctx = zstandard.ZstdDecompressor()
    inv = {int(morton3(bx, by, bz)): (bx, by, bz) for by in range(nby) for bx in range(nbx) for bz in range(nbz)}
    for key, off, ln, fl in idx.tolist():
        bx, by, bz = inv[int(key)]
        raw = data[off:off + ln]
        blk = np.full(B ** 3, raw[0], np.uint8) if fl & 1 else np.frombuffer(dctx.decompress(raw, max_output_size=B ** 3), np.uint8)
        g[bz * B:(bz + 1) * B, by * B:(by + 1) * B, bx * B:(bx + 1) * B] = blk.reshape(B, B, B)
    return g[:nz, :ny, :nx]
