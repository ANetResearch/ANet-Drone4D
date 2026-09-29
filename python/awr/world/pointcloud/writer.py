"""Potree 2.0 + ANET_Q16 v1 容器写出（AWR-16 §4.1–§4.9；M03-FR-033 至 FR-038）。

写出顺序即 RNG 消费顺序：节点按 (level, nid) 升序（层级优先 BFS），每个有点的节点按
`default_rng(shuffleSeed).permutation(n)` 连续消费同一个生成器打散（16 §4.7）。
子树包围盒自底向上合并（本节点加全部子孙，M03 O-3）。
"""

from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import awr

from ..package.jsonio import write_json
from .encode import baked_height, oct16_encode, quantize_node
from .morton import B, node_xyz
from .npx import colmax, colmin
from .octree import build_octree

REC = 22
EXT_REC = 12
NORMAL, LEAF, PROXY = 0, 1, 2
HIER_DTYPE = np.dtype([("t", "u1"), ("m", "u1"), ("n", "<u4"), ("o", "<i8"), ("s", "<i8")])
SINGLE_CHUNK_MAX_NODES = 40_000
PAGE_STEP = 4


@dataclass(slots=True)
class RootEntry:
    """`world.json` 点云图层 `roots[]` 的一项（16 §4.10 第 3 条）。"""

    name: str
    href: str
    cube_min: np.ndarray
    cube_size: float
    points: int
    depth: int
    first_screen_bytes: int
    metadata: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"name": self.name, "href": self.href, "cubeMin": [float(v) for v in self.cube_min],
                "cubeSize": float(self.cube_size), "points": int(self.points), "depth": int(self.depth),
                "firstScreenBytes": int(self.first_screen_bytes)}


def rule_g_level(levels_points: list[int], depth: int, k: int) -> int:
    """规则 G（16 §4.11）：最小 L 使 levelsPoints[L] ≥ 1e5/k；超过 4.5e5/k 时逐层回退。"""
    fsl = next((L for L in range(depth + 1) if levels_points[L] >= 100_000 / k), depth)
    while fsl > 0 and levels_points[fsl] > 450_000 / k:
        fsl -= 1
    return fsl


@dataclass(slots=True)
class _Node:
    s: int
    e: int
    mn: np.ndarray
    mx: np.ndarray


def write_container(out_dir: Path, name: str, P: np.ndarray, normals: np.ndarray | None, cls: np.ndarray,
                    z_range: tuple[float, float], cube_min: np.ndarray, size: float, *, G: int = 64, leaf: int = 20000,
                    seed: int = 1, compression: str = "none", forest: tuple[int, int] = (0, 1),
                    hag_range: tuple[float, float] | None = None, nn_median_m: float = 1.0,
                    normals_flipped_frac: float | None = None, presorted=None, normals_oct16: np.ndarray | None = None) -> dict:
    """写出一个根的完整容器；返回 metadata（dict）。P 为 float64 world 坐标。

    `normals_oct16` 为已编码的 oct16（与 P 同序），给出时不再重复编码（源点云与切片器共用一次编码）。
    """
    if compression not in ("none", "gzip"):
        raise ValueError(f"compression {compression!r}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cube_min = np.asarray(cube_min, np.float64)
    ob = build_octree(P, cube_min, size, G=G, leaf=leaf, seed=seed, presorted=presorted)
    order, level, nid = ob.order, ob.level, ob.nid
    Ps = P[order]
    cls_s = np.asarray(cls, np.uint8)[order]
    key = (level.astype(np.int64) << 58) | nid
    o2 = np.argsort(key, kind="stable")
    ks = key[o2]
    N = len(P)
    st = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]]) if N else np.zeros(0, np.int64)
    en = np.r_[st[1:], N]
    own_min = np.zeros((len(st), 3))
    own_max = np.zeros((len(st), 3))
    if N:
        for a in range(3):                                         # 逐列 reduceat（连续数组，O-3）
            col = Ps[:, a][o2]
            own_min[:, a] = np.minimum.reduceat(col, st)
            own_max[:, a] = np.maximum.reduceat(col, st)
        del col
    nodes: dict[tuple[int, int], _Node] = {}
    for i, (s, e) in enumerate(zip(st.tolist(), en.tolist(), strict=True)):
        k = int(ks[s])
        nodes[(k >> 58, k & ((1 << 58) - 1))] = _Node(s, e, own_min[i].copy(), own_max[i].copy())
    for (L, n) in list(nodes):
        for pl in range(L - 1, -1, -1):
            pk = (pl, n >> (3 * (L - pl)))
            if pk in nodes:
                break
            nodes[pk] = _Node(0, 0, np.full(3, np.inf), np.full(3, -np.inf))
    if not nodes:
        nodes[(0, 0)] = _Node(0, 0, cube_min.copy(), cube_min.copy())
    keys = sorted(nodes)
    child: dict[tuple[int, int], int] = {}
    for L, n in keys:
        if L > 0:
            child[(L - 1, n >> 3)] = child.get((L - 1, n >> 3), 0) | (1 << (n & 7))
    for L, n in sorted(keys, reverse=True):                       # 自底向上：子树包围盒
        if L > 0:
            p = nodes[(L - 1, n >> 3)]
            c = nodes[(L, n)]
            np.minimum(p.mn, c.mn, out=p.mn)
            np.maximum(p.mx, c.mx, out=p.mx)
    z1, z99 = float(z_range[0]), float(z_range[1])
    rgb = baked_height(Ps[:, 2], z1, z99)
    if normals_oct16 is not None:
        w = np.asarray(normals_oct16, np.uint16)[order]
        w_mode = "oct16"
    elif normals is not None:
        w = oct16_encode(np.asarray(normals)[order])
        w_mode = "oct16"
    else:
        w = np.zeros(N, np.uint16)
        w_mode = "zero"
    rng = np.random.default_rng(seed)
    depth = max(k[0] for k in keys)
    lvl_end = [0] * (depth + 1)
    lvl_pts = [0] * (depth + 1)
    lvl_nodes = [0] * (depth + 1)
    recs = np.zeros(len(keys), HIER_DTYPE)
    ext = np.zeros((len(keys), 6), "<u2")
    off = 0
    with open(out_dir / "octree.bin", "wb") as fo:
        for i, (L, n) in enumerate(keys):
            nd = nodes[(L, n)]
            cnt = nd.e - nd.s
            ns = size / (1 << L)
            x, y, z = node_xyz(n, L)
            nmin = cube_min + ns * np.array([x, y, z], float)
            if cnt:
                idx = o2[nd.s:nd.e][rng.permutation(cnt)]
                pos = np.empty((cnt, 4), "<u2")
                pos[:, :3] = quantize_node(Ps[idx], nmin, ns)
                pos[:, 3] = w[idx]
                col = np.empty((cnt, 4), np.uint8)
                col[:, :3] = rgb[idx]
                col[:, 3] = cls_s[idx]
                buf = pos.tobytes() + col.tobytes()
                if compression == "gzip":
                    buf = gzip.compress(buf, 6, mtime=0)
                fo.write(buf)
            else:
                buf = b""
            cm = child.get((L, n), 0)
            recs[i] = (NORMAL if cm else LEAF, cm, cnt, off, len(buf))
            smin = np.floor((nd.mn - nmin) / ns * 65535)
            smax = np.ceil((nd.mx - nmin) / ns * 65535)
            ext[i] = np.clip(np.r_[smin, smax], 0, 65535).astype("<u2")
            off += len(buf)
            lvl_end[L] = int(off)
            lvl_pts[L] += int(cnt)
            lvl_nodes[L] += 1
    for L in range(1, depth + 1):
        lvl_pts[L] += lvl_pts[L - 1]
        lvl_end[L] = max(lvl_end[L], lvl_end[L - 1])
    if len(keys) > SINGLE_CHUNK_MAX_NODES:
        hier, ext_bytes, first_chunk = _write_hierarchy_paged(keys, recs, ext, child)
        step = PAGE_STEP
    else:
        hier, ext_bytes, first_chunk = recs.tobytes(), ext.tobytes(), len(keys) * REC
        step = max(depth, 1)
    (out_dir / "hierarchy.bin").write_bytes(hier)
    (out_dir / "hierarchy_ext.bin").write_bytes(ext_bytes)
    k_roots = forest[1]
    fsl = rule_g_level(lvl_pts, depth, k_roots)
    hist = np.bincount(np.asarray(cls, np.uint8), minlength=16) if N else np.zeros(16, np.int64)
    tmin = colmin(P) if N else cube_min
    tmax = colmax(P) if N else cube_min
    meta = {
        "version": "2.0", "name": name, "description": "", "points": int(N), "projection": "",
        "hierarchy": {"firstChunkSize": int(first_chunk), "stepSize": int(step), "depth": int(depth)},
        "offset": [float(v) for v in cube_min], "scale": [0.001, 0.001, 0.001], "spacing": size / G,
        "boundingBox": {"min": [float(v) for v in cube_min], "max": [float(v) for v in (cube_min + size)]},
        "encoding": "ANET_Q16",
        "attributes": [
            {"name": "anet:pos", "description": "node-cube unorm16 xyz + oct16 normal (w)", "size": 8, "numElements": 4,
             "elementSize": 2, "type": "uint16"},
            {"name": "anet:col", "description": "sRGB display albedo + class index (a)", "size": 4, "numElements": 4,
             "elementSize": 1, "type": "uint8"}],
        "anet": {
            "formatVersion": 1, "frame": "world", "streams": ["pos", "col"], "bytesPerPoint": 12,
            "pos": {"format": "unorm16x4", "xyz": "node-cube", "w": w_mode},
            "col": {"format": "unorm8x4", "rgb": "baked-height", "a": "class-index"},
            "ext": None, "compression": compression, "pointOrder": "shuffled", "shuffleSeed": int(seed),
            "sampling": {"method": "grid-center", "G": int(G), "leaf": int(leaf), "minChild": 0, "B": B, "seed": int(seed)},
            "classTable": {"id": "anet-classes@1", "href": "semantic/anet-classes@1.json"},
            "nodeCount": len(keys), "levelsByteEnd": [int(v) for v in lvl_end], "levelsPoints": [int(v) for v in lvl_pts],
            "levelsNodes": [int(v) for v in lvl_nodes], "firstScreenLevel": int(fsl),
            "tightBounds": {"min": [float(v) for v in tmin], "max": [float(v) for v in tmax]},
            "hierarchyExt": {"href": "hierarchy_ext.bin", "recordSize": 12, "content": "subtree-aabb-u16"},
            "stats": _stats(z1, z99, hag_range, nn_median_m, normals_flipped_frac, hist),
            "root": {"forestIndex": int(forest[0]), "forestSize": int(forest[1])},
            "twin": None,
            "generator": {"name": "worldpkg", "version": awr.__version__},
        },
    }
    write_json(out_dir / "metadata.json", meta)
    return meta


def _stats(z1, z99, hag_range, nn, flipped, hist) -> dict:
    s = {"zP1": z1, "zP99": z99,
         "hagP1": None if hag_range is None else float(hag_range[0]),
         "hagP99": None if hag_range is None else float(hag_range[1]),
         "nnMedianM": float(nn), "nnSource": "knn"}
    if flipped is not None:
        s["normalsFlippedFrac"] = float(flipped)
    s["classHistogram"] = {str(i): int(v) for i, v in enumerate(hist) if v}
    return s


def _write_hierarchy_paged(keys, recs, ext, child) -> tuple[bytes, bytes, int]:
    """PotreeConverter `Indexer::createHierarchyChunks` 语义的分页层级（step = 4，M03-FR-036 P1）。

    每个 chunk 含 chunk 根及其下 1..4 层的全部子孙（gatherChunk）；相对深度恰为 4 的节点一律写 PROXY
    （`byteOffset/byteSize` 指向以它为根的子 chunk），子 chunk 的首条记录是该节点本身的真实记录。
    chunk 内按 (level, nid) 升序（等价 BFS）；chunk 依发现顺序（FIFO）追加。hierarchy_ext 对 PROXY
    重复记录同样占位（16 §4.8）。
    """
    del child
    index = {k: i for i, k in enumerate(keys)}
    children: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for L, n in keys:
        if L > 0:
            children.setdefault((L - 1, n >> 3), []).append((L, n))
    chunks: list[tuple[tuple[int, int], list[tuple[int, int]]]] = []
    queue = [keys[0]]
    while queue:
        root = queue.pop(0)
        nodes_c = [root]
        frontier = [root]
        for _ in range(PAGE_STEP):
            nxt = [c for k in frontier for c in children.get(k, [])]
            nodes_c.extend(nxt)
            frontier = nxt
        nodes_c.sort()
        chunks.append((root, nodes_c))
        queue.extend(sorted(k for k in nodes_c if k[0] == root[0] + PAGE_STEP))
    offsets = {}
    o = 0
    for root, nodes_c in chunks:
        offsets[root] = (o, len(nodes_c) * REC)
        o += len(nodes_c) * REC
    hier = bytearray()
    ext_out = bytearray()
    for root, nodes_c in chunks:
        for k in nodes_c:
            r = recs[index[k]].copy()
            if k[0] == root[0] + PAGE_STEP:
                r["t"] = PROXY
                r["o"], r["s"] = offsets[k]
            hier += r.tobytes()
            ext_out += ext[index[k]].tobytes()
    return bytes(hier), bytes(ext_out), len(chunks[0][1]) * REC


def pack_hierarchy_record(t: int, mask: int, n: int, off: int, size: int) -> bytes:
    return struct.pack("<BBIqq", t, mask, n, off, size)
