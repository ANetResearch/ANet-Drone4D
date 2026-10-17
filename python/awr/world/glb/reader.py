"""GLB 安全解析与节点展平（M22 §6.6.1、FR-004、FR-005；← r2 glbworld.py）。

只接受 glTF 2.0 二进制容器：文件 ≤ 4 GiB、JSON 块 ≤ 64 MiB、BIN 块只读 memmap；全部 bufferView 与 accessor 做越界检查，
索引须小于图元顶点数；拒绝外部 URI 与压缩扩展（`KHR_draco_mesh_compression`、`EXT_meshopt_compression` 须先经 gltf-transform
解压到 staging）。节点按场景根与 JSON 子节点顺序深度优先展平，矩阵以 Python float 累乘，点以逐元素乘加变换（不经 BLAS，
跨机器逐位一致，同 M03 normalize）；任一网格节点 `det ≤ 0`（镜像）以 GateFailed（退出码 2）拒绝。纹理只登记字节范围与
图像头中的宽高，不解码。
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from awr.world.ingest.normalize import apply_linear
from awr.world.ingest.types import ConfigError, GateFailed

__all__ = ["MAX_FILE_BYTES", "MAX_JSON_BYTES", "Mat4", "Prim", "Scene", "Texture", "load_scene", "node_local", "walk_nodes"]

MAX_FILE_BYTES = 4 << 30
MAX_JSON_BYTES = 64 << 20
COMPRESSION_EXT = ("KHR_draco_mesh_compression", "EXT_meshopt_compression")
CT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
Mat4 = tuple[tuple[float, float, float, float], ...]
I4: Mat4 = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))


@dataclass
class Prim:
    node: int
    mesh: int
    prim: int
    material: int | None
    texture: int | None                 # baseColorTexture 的 image 下标
    pos: np.ndarray                     # (n, 3) float64，glTF 场景帧（已乘节点矩阵）
    idx: np.ndarray                     # (m, 3) uint32，图元内顶点下标
    uv: np.ndarray | None               # (n, 2) float32
    nrm: np.ndarray | None              # (n, 3) float32，已按节点矩阵逆转置变换并归一化


@dataclass(frozen=True)
class Texture:
    index: int
    mime: str
    width: int
    height: int
    offset: int                         # BIN 块内字节偏移
    nbytes: int
    sha256: str


@dataclass
class Scene:
    path: Path
    json: dict[str, Any]
    bin: np.ndarray                     # uint8 只读 memmap
    prims: list[Prim] = field(default_factory=list)
    textures: list[Texture] = field(default_factory=list)
    nodes: list[tuple[int, Mat4]] = field(default_factory=list)   # 展平后的 (节点下标, 累乘矩阵)，含无网格节点

    @property
    def extras(self) -> dict[str, Any]:
        return dict(self.json.get("asset", {}).get("extras", {}) or {})

    def texture_bytes(self, i: int) -> bytes:
        t = self.textures[i]
        return bytes(self.bin[t.offset:t.offset + t.nbytes])

    def merged(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """全部图元拼接：V (n, 3) float64、F (m, 3) int64、图元号 (m,) int32（外观三角形序）。"""
        offs, Vs, Fs, P = 0, [], [], []
        for k, p in enumerate(self.prims):
            Vs.append(p.pos)
            Fs.append(p.idx.astype(np.int64) + offs)
            P.append(np.full(len(p.idx), k, np.int32))
            offs += len(p.pos)
        if not Vs:
            return np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros(0, np.int32)
        return np.concatenate(Vs), np.concatenate(Fs), np.concatenate(P)


# ---------------------------------------------------------------- 容器与限界


def _read_container(path: Path) -> tuple[dict, np.ndarray]:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ConfigError(f"{path.name}: {size} B exceeds the 4 GiB GLB limit")
    with path.open("rb") as f:
        head = f.read(20)
        if len(head) < 20:
            raise ConfigError(f"{path.name}: truncated GLB header")
        magic, ver, length = struct.unpack_from("<4sII", head, 0)
        if magic != b"glTF" or ver != 2:
            raise ConfigError(f"{path.name}: not a glTF 2.0 binary (magic {magic!r}, version {ver})")
        if length != size:
            raise ConfigError(f"{path.name}: header length {length} != file size {size}")
        jlen, jtype = struct.unpack_from("<I4s", head, 12)
        if jtype != b"JSON" or jlen > MAX_JSON_BYTES or 20 + jlen > size:
            raise ConfigError(f"{path.name}: bad JSON chunk ({jtype!r}, {jlen} B)")
        js = json.loads(f.read(jlen))
        o = 20 + jlen
        binv = np.zeros(0, np.uint8)
        if o + 8 <= size:
            f.seek(o)
            blen, btype = struct.unpack("<I4s", f.read(8))
            if btype != b"BIN\0" or o + 8 + blen > size:
                raise ConfigError(f"{path.name}: bad BIN chunk ({btype!r}, {blen} B)")
            binv = np.memmap(path, np.uint8, mode="r", offset=o + 8, shape=(blen,)) if blen else binv
    return js, binv


def _check_limits(js: dict, binv: np.ndarray, name: str) -> None:
    used = set(js.get("extensionsUsed", [])) | set(js.get("extensionsRequired", []))
    comp = sorted(used & set(COMPRESSION_EXT))
    if comp:
        raise ConfigError(f"{name}: compressed GLB ({', '.join(comp)}); decompress with gltf-transform into staging first")
    for i, b in enumerate(js.get("buffers", [])):
        if "uri" in b:
            raise ConfigError(f"{name}: buffer {i} has an external URI")
        if b.get("byteLength", 0) > len(binv):
            raise ConfigError(f"{name}: buffer {i} byteLength exceeds the BIN chunk")
    for i, im in enumerate(js.get("images", [])):
        if "uri" in im or "bufferView" not in im:
            raise ConfigError(f"{name}: image {i} is not embedded in a bufferView")
    nbv = len(js.get("bufferViews", []))
    for i, bv in enumerate(js.get("bufferViews", [])):
        o, n = bv.get("byteOffset", 0), bv.get("byteLength", -1)
        if bv.get("buffer", 0) != 0 or o < 0 or n < 0 or o + n > len(binv):
            raise ConfigError(f"{name}: bufferView {i} out of range")
    for i, a in enumerate(js.get("accessors", [])):
        if a.get("componentType") not in CT or a.get("type") not in NC or a.get("count", -1) < 0:
            raise ConfigError(f"{name}: accessor {i} has an unsupported type")
        if "sparse" in a:
            raise ConfigError(f"{name}: accessor {i} is sparse (not supported)")
        if "bufferView" not in a:
            continue
        if not 0 <= a["bufferView"] < nbv:
            raise ConfigError(f"{name}: accessor {i} references a missing bufferView")
        bv = js["bufferViews"][a["bufferView"]]
        el = np.dtype(CT[a["componentType"]]).itemsize * NC[a["type"]]
        stride = bv.get("byteStride", 0) or el
        need = a.get("byteOffset", 0) + (stride * (a["count"] - 1) + el if a["count"] else 0)
        if stride < el or need > bv["byteLength"]:
            raise ConfigError(f"{name}: accessor {i} exceeds its bufferView")


def _accessor(js: dict, binv: np.ndarray, i: int) -> np.ndarray:
    a = js["accessors"][i]
    dt = np.dtype(CT[a["componentType"]]).newbyteorder("<")
    nc, cnt = NC[a["type"]], a["count"]
    if "bufferView" not in a:
        return np.zeros((cnt, nc), dt)
    bv = js["bufferViews"][a["bufferView"]]
    off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride", 0)
    if stride and stride != dt.itemsize * nc:
        rows = np.lib.stride_tricks.as_strided(binv[off:], shape=(cnt, dt.itemsize * nc), strides=(stride, 1))
        arr = np.ascontiguousarray(rows).view(dt).reshape(cnt, nc)
    else:
        arr = np.frombuffer(binv, dt, count=cnt * nc, offset=off).reshape(cnt, nc)
    if a.get("normalized"):
        arr = np.maximum(arr.astype(np.float32) / np.iinfo(dt).max, -1.0)
    return arr


# ---------------------------------------------------------------- 节点矩阵（Python float，逐元素）


def _mul4(A: Mat4, B: Mat4) -> Mat4:
    return tuple(tuple(sum(A[i][k] * B[k][j] for k in range(4)) for j in range(4)) for i in range(4))  # type: ignore[return-value]


def node_local(n: dict) -> Mat4:
    """节点局部矩阵（行主序）：`matrix`（列主序存储）或 T·R·S。"""
    if "matrix" in n:
        m = [float(v) for v in n["matrix"]]
        return tuple(tuple(m[c * 4 + r] for c in range(4)) for r in range(4))  # type: ignore[return-value]
    tx, ty, tz = (float(v) for v in n.get("translation", (0, 0, 0)))
    x, y, z, w = (float(v) for v in n.get("rotation", (0, 0, 0, 1)))
    sx, sy, sz = (float(v) for v in n.get("scale", (1, 1, 1)))
    R = ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
         (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
         (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)))
    return ((R[0][0] * sx, R[0][1] * sy, R[0][2] * sz, tx), (R[1][0] * sx, R[1][1] * sy, R[1][2] * sz, ty),
            (R[2][0] * sx, R[2][1] * sy, R[2][2] * sz, tz), (0.0, 0.0, 0.0, 1.0))


def det3(M: Mat4) -> float:
    a = M
    return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
            + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))


def walk_nodes(js: dict) -> list[tuple[int, Mat4]]:
    """场景根节点起深度优先、按 JSON 子节点顺序展平；检测环。"""
    nodes = js.get("nodes", [])
    scenes = js.get("scenes", [])
    roots = scenes[js.get("scene", 0)]["nodes"] if scenes else list(range(len(nodes)))
    out: list[tuple[int, Mat4]] = []
    stack: list[tuple[int, Mat4, tuple[int, ...]]] = [(r, I4, ()) for r in reversed(roots)]
    while stack:
        ni, parent, path = stack.pop()
        if ni in path or not 0 <= ni < len(nodes):
            raise ConfigError(f"node graph invalid at node {ni}")
        M = _mul4(parent, node_local(nodes[ni]))
        out.append((ni, M))
        stack.extend((c, M, (*path, ni)) for c in reversed(nodes[ni].get("children", [])))
    return out


# ---------------------------------------------------------------- 纹理头


def _image_size(b: bytes, mime: str) -> tuple[int, int]:
    if b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) >= 24:
        w, h = struct.unpack(">II", b[16:24])
        return int(w), int(h)
    if b[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(b):
            if b[i] != 0xFF:
                i += 1
                continue
            m = b[i + 1]
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", b[i + 2:i + 4])[0]
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", b[i + 5:i + 9])
                return int(w), int(h)
            i += 2 + seg
    raise ConfigError(f"image header not recognised ({mime})")


def _base_color_image(js: dict, mat: int | None) -> int | None:
    if mat is None:
        return None
    t = js["materials"][mat].get("pbrMetallicRoughness", {}).get("baseColorTexture")
    return None if t is None else js["textures"][t["index"]].get("source")


# ---------------------------------------------------------------- 入口


def load_scene(path: str | Path, *, geometry: bool = True) -> Scene:
    """解析 GLB；`geometry = False` 时只做容器、限界、节点与纹理登记（inspect 的快速路径）。"""
    path = Path(path)
    js, binv = _read_container(path)
    _check_limits(js, binv, path.name)
    sc = Scene(path, js, binv)
    sc.nodes = walk_nodes(js)
    for i, im in enumerate(js.get("images", [])):
        bv = js["bufferViews"][im["bufferView"]]
        o, n = bv.get("byteOffset", 0), bv["byteLength"]
        b = bytes(binv[o:o + n])
        w, h = _image_size(b[:65536] if len(b) > 65536 else b, im.get("mimeType", ""))
        sc.textures.append(Texture(i, im.get("mimeType", ""), w, h, o, n, hashlib.sha256(b).hexdigest()))
    if not geometry:
        return sc
    for ni, M in sc.nodes:
        n = js["nodes"][ni]
        if "mesh" not in n:
            continue
        if det3(M) <= 0:
            raise GateFailed(f"{path.name}: mirrored node {ni} (det {det3(M):.6g})")
        A = tuple(tuple(M[r][c] for c in range(3)) for r in range(3))
        t = np.array([M[0][3], M[1][3], M[2][3]])
        inv_t = _normal_matrix(A)
        for pi, p in enumerate(js["meshes"][n["mesh"]]["primitives"]):
            if p.get("mode", 4) != 4:
                continue
            P = apply_linear(A, _accessor(js, binv, p["attributes"]["POSITION"]).astype(np.float64)) + t
            N = None
            if "NORMAL" in p["attributes"]:
                N = apply_linear(inv_t, _accessor(js, binv, p["attributes"]["NORMAL"]).astype(np.float64))
                N = (N / np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-12)).astype(np.float32)
            UV = _accessor(js, binv, p["attributes"]["TEXCOORD_0"]).astype(np.float32) if "TEXCOORD_0" in p["attributes"] else None
            if "indices" in p:
                idx = _accessor(js, binv, p["indices"]).reshape(-1).astype(np.uint32)
                if len(idx) % 3:
                    raise ConfigError(f"{path.name}: primitive {n['mesh']}/{pi} index count not a multiple of 3")
                if len(idx) and int(idx.max()) >= len(P):
                    raise ConfigError(f"{path.name}: primitive {n['mesh']}/{pi} index out of range")
                idx = idx.reshape(-1, 3)
            else:
                idx = np.arange(len(P) - len(P) % 3, dtype=np.uint32).reshape(-1, 3)
            sc.prims.append(Prim(ni, n["mesh"], pi, p.get("material"), _base_color_image(js, p.get("material")), P, idx, UV, N))
    return sc


def _normal_matrix(A: tuple) -> tuple:
    """(A⁻¹)ᵀ（伴随矩阵除以行列式），Python float。"""
    (a, b, c), (d, e, f), (g, h, i) = A
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    cof = ((e * i - f * h, -(d * i - f * g), d * h - e * g), (-(b * i - c * h), a * i - c * g, -(a * h - b * g)),
           (b * f - c * e, -(a * f - c * d), a * e - b * d))
    return tuple(tuple(v / det for v in row) for row in cof)
