"""ANET_Q16 容器读取（AWR-16 §4.12 冻结签名；语义与 M05 的 parseHierarchy 一致，含 PROXY 解析）。"""

from __future__ import annotations

import gzip
import json
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .encode import decode_node_positions, oct16_decode
from .morton import name_to_key

REC = 22
EXT_REC = 12
NORMAL, LEAF, PROXY = 0, 1, 2


class HierarchyError(ValueError):
    """hierarchy.bin 结构错误（记录数、PROXY、BFS 展开不一致）。"""


@dataclass(slots=True)
class HNode:
    name: str
    level: int
    type: int
    mask: int
    n: int
    off: int
    size: int
    rec: int           # 记录在 hierarchy.bin 中的序号（字节 22·rec）


@dataclass(slots=True)
class Container:
    root_dir: Path
    metadata: dict
    records: list[HNode]           # 文件记录顺序（含 PROXY 重复记录）
    nodes: list[HNode]             # 真实节点（不含 PROXY 记录）
    chunks: list[tuple[int, int]]  # (byteOffset, byteSize)
    ext: np.ndarray | None         # (n_records, 6) uint16
    hierarchy_bytes: int
    octree_bytes: int
    cube_min: np.ndarray = field(default_factory=lambda: np.zeros(3))
    cube_size: float = 0.0

    @property
    def depth(self) -> int:
        return max(n.level for n in self.nodes) if self.nodes else 0


@dataclass(slots=True)
class NodeView:
    name: str
    level: int
    xyz: tuple[int, int, int]
    node_min: np.ndarray
    node_size: float
    num_points: int
    byte_offset: int
    byte_size: int
    rec: int
    pos_u16: np.ndarray | None = None      # (n,4) uint16
    col_u8: np.ndarray | None = None       # (n,4) uint8
    positions: np.ndarray | None = None    # (n,3) float64 world
    normals: np.ndarray | None = None      # (n,3) float64
    classes: np.ndarray | None = None      # (n,) uint8


def parse_hierarchy(buf: bytes, first_chunk_size: int) -> tuple[list[HNode], list[tuple[int, int]]]:
    """按 Potree parseHierarchy 语义解析；返回 (记录序列, chunk 列表)。"""
    records: list[HNode] = []
    queue = [("r", 0, first_chunk_size)]
    chunks: list[tuple[int, int]] = []
    while queue:
        start_name, o, s = queue.pop(0)
        if o < 0 or s <= 0 or s % REC or o % REC or o + s > len(buf):
            raise HierarchyError(f"chunk ({o}, {s}) out of range or misaligned")
        chunks.append((o, s))
        cur = [start_name]
        count = s // REC
        for i in range(count):
            if i >= len(cur):
                raise HierarchyError(f"chunk at {o}: record {i} has no parent in BFS expansion")
            t, mask, n, off, size = struct.unpack_from("<BBIqq", buf, o + REC * i)
            name = cur[i]
            records.append(HNode(name, len(name) - 1, t, mask, n, off, size, o // REC + i))
            if t == PROXY and i > 0:
                queue.append((name, off, size))
            elif t == PROXY and i == 0:
                raise HierarchyError(f"chunk at {o}: first record is a PROXY")
            else:
                cur.extend(name + str(ch) for ch in range(8) if mask >> ch & 1)
        if len(cur) != count:
            raise HierarchyError(f"chunk at {o}: {count} records but BFS expects {len(cur)}")
    return records, chunks


def read_container(root_dir: Path) -> Container:
    """metadata + 节点表（含 PROXY 解析）+ hierarchy_ext。"""
    root_dir = Path(root_dir)
    md = json.loads((root_dir / "metadata.json").read_text(encoding="utf-8"))
    hb = (root_dir / "hierarchy.bin").read_bytes()
    records, chunks = parse_hierarchy(hb, int(md["hierarchy"]["firstChunkSize"]))
    nodes = [r for r in records if r.type != PROXY]
    ext = None
    a = md.get("anet") or {}
    he = a.get("hierarchyExt")
    if he:
        p = root_dir / he["href"]
        if p.exists():
            eb = p.read_bytes()
            if len(eb) % EXT_REC == 0:
                ext = np.frombuffer(eb, "<u2").reshape(-1, 6)
    bb_min = np.asarray(md["boundingBox"]["min"], np.float64)
    bb_max = np.asarray(md["boundingBox"]["max"], np.float64)
    ob = root_dir / "octree.bin"
    return Container(root_dir=root_dir, metadata=md, records=records, nodes=nodes, chunks=chunks, ext=ext,
                     hierarchy_bytes=len(hb), octree_bytes=ob.stat().st_size if ob.exists() else 0,
                     cube_min=bb_min, cube_size=float((bb_max - bb_min).max()))


def iter_nodes(c: Container, *, decode: bool = False, raw: bool = False) -> Iterator[NodeView]:
    """按记录顺序遍历真实节点；`decode=True` 时返回 float64 world 坐标、法线与类别。"""
    a = c.metadata.get("anet") or {}
    bpp = int(a.get("bytesPerPoint", 12))
    gz = a.get("compression") == "gzip"
    f = open(c.root_dir / "octree.bin", "rb") if (decode or raw) else None  # noqa: SIM115 - 生成器期间保持打开
    try:
        for n in c.nodes:
            L, x, y, z = name_to_key(n.name)
            ns = c.cube_size / (1 << L)
            nmin = c.cube_min + ns * np.array([x, y, z], float)
            v = NodeView(n.name, L, (x, y, z), nmin, ns, n.n, n.off, n.size, n.rec)
            if f is not None and n.n:
                f.seek(n.off)
                buf = f.read(n.size)
                if gz:
                    buf = gzip.decompress(buf)
                cnt = n.n
                if len(buf) != cnt * bpp:
                    raise ValueError(f"{n.name}: payload {len(buf)} B != {cnt}*{bpp}")
                v.pos_u16 = np.frombuffer(buf, "<u2", 4 * cnt).reshape(cnt, 4)
                v.col_u8 = np.frombuffer(buf, np.uint8, 4 * cnt, 8 * cnt).reshape(cnt, 4)
                if decode:
                    v.positions = decode_node_positions(v.pos_u16[:, :3], nmin, ns)
                    v.normals = oct16_decode(v.pos_u16[:, 3])
                    v.classes = v.col_u8[:, 3].copy()
            yield v
    finally:
        if f is not None:
            f.close()
