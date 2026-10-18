"""共享几何服务 `geosvc`（AWR-05 §5.1.6、ADR-167；M22-FR-048、FR-051、FR-053、NFR-005、NFR-010；M22 §6.4、§6.6.15、§7.5.4）。

每个活跃世界、每台主机一个服务进程：常驻瓦片化 BVH（每瓦片一个 Open3D `RaycastingScene`，与 inproc 的 `MeshWorldQuery`
为同一份代码）与 0.5 m 精细 DSM（`dsm_0p5m`，只读 memmap）。sim-core 与 Env 会话只持有本模块的 IPC 句柄
（Unix 套接字 + 每连接一段 memfd 共享内存）与只读 memmap（2 m 派生栅格、`dsm_0p5m`、逐三角形语义），不在会话内建树，
会话匿名内存（或 PSS 的匿名部分）增量 ≤ 20 MB，文件页不计（D3-AC-05、M22-NFR-005）。

协议（M22 §7.5.4；1 版）：
- 套接字 `${AWR_D3_RUN_ROOT}/geosvc/<world_id>-<contentVersion>.sock`（缺省根 `/dev/shm/awr-d3`），权限 0600；
  帧为 u32 小端长度前缀 + msgpack 映射，单帧 ≤ 1 MiB（射线数据不走帧，走共享内存槽）。
- 握手 `{op: hello, proto, world_id, content_version, coordinate_sha256, pid}`；应答 `{ok, shm_name, slots, slot_bytes,
  tiles, limits, world, fine}`，共享内存段的文件描述符以 SCM_RIGHTS 随应答传递（memfd，不落文件系统，进程退出即回收）；
  绑定不一致应答 `{ok: false, code: 605}`。
- 数据：请求 `{op, seq, slot, n, ...}` 的输入与输出都在第 `slot` 槽内（偏移 0 起），布局见 `CAST_LAYOUT` 与
  `CLOSEST_LAYOUT`；`cast` 为核心求交原语（起点、单位方向、逐射线 tmax 各 float64，56 B/射线；输出 t float64、prim
  uint32、局部法线 float32×3），会话侧的 `raycast_batch`、`ray_hit`、`los_batch`、`segment_los`、`free_distance` 都由
  继承的 `MeshWorldQuery` 方法经 `_cast` 落到该原语，因此与 inproc 结果逐位相同、与批次切分无关（M22-FR-050、FR-051）。
- 操作：`cast{slot, n}`、`closest{slot, n, max}`、`walk{slot, n}`（`walkable_surface`：xy、z_top float64，输出 z float64、
  prim int64，40 B/条）、`ping`、`stats`；`walkable`、`nav_path` 在 Detour 查询工作进程接入前
  应答 604（`NAV_UNAVAILABLE`）。同一连接内请求按到达顺序处理，客户端可在多个槽上流水提交。
- 错误：`{ok: false, code, detail}`；604 服务不可用，605 绑定不一致，110 参数越界，300 请求不合法，320 内部错误。

状态（M22 §6.4）：STARTING → LOADING → READY → DRAINING → STOPPED；装载失败为 FAILED（退出码 3）；同一套接字已有活
实例时退出码 4。READY 下连接数为 0 持续 `idle_timeout_s`（缺省 600 s）或收到 SIGTERM 即进入 DRAINING：不再接受连接，
删除套接字与就绪文件，完成在途请求后退出 0。每 10 s 以结构化日志输出射线吞吐、在途请求数、RSS 与连接数（NFR-010）。

客户端（M22-FR-051、FR-053）：`open_geosvc_world_query(..., geosvc="auto" | "service" | "inproc")`，缺省取
`AWR_GEOSVC`（未设置为 auto）。auto 优先连接服务，连接失败时在开发 profile 回退 inproc、在 `d3` profile（`AWR_PROFILE`）
抛 604；service 连接失败即抛 604。请求超时（缺省 2 s）或连接断开时重连一次（重新握手），仍失败抛 `GeoError(604)`；
不静默降级到 DSM 求交。

0.5 m 精细 DSM（M22-FR-026）：世界包有 `terrain.dsm_0p5m` 图层时直接使用；否则由服务在装载期按“每格 2 × 2 条向下网格
射线取最高命中、全部未命中取 2 m DSM 最近格”派生到 `<cache_root>/geosvc/<world>-<contentVersion>-<key>/dsm_0p5m.*`
（`f32-grid@1`，与 `dsm_2m` 同 originXY），会话以只读 memmap 打开同一文件。该文件为运行期缓存，不是世界包内容文件
（ADR-167 第 1 条只约束内容文件）。

命令行：`python -m awr.world.mesh.geosvc --world <id> [--worlds-dir DIR] [--world-dir PATH] [--cache-dir DIR]
[--threads N] [--idle-timeout-s 600] [--socket PATH] [--slots 8] [--slot-bytes 16777216]`。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import mmap
import os
import selectors
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import msgpack
import numpy as np

from awr.world.geometry.cache import default_cache_root
from awr.world.geometry.grids import Grid
from awr.world.geometry.query import DsmWorldQuery, open_dsm_world_query
from awr.world.geometry.status import read_world_header
from awr.world.geometry.types import BAD_REQUEST, INTERNAL_ERROR, GeoError, GeoLoadError, GeoParams, GridView
from awr.world.mesh.query import MAX_RAYS, MeshTile, MeshWorldQuery
from awr.world.mesh.query import PARAM_OUT_OF_RANGE as MESH_PARAM_OUT_OF_RANGE

__all__ = [
    "GEOSVC_BINDING_MISMATCH", "GEOSVC_UNAVAILABLE", "GeoService", "GeoServiceClient", "GeoSvcMeshWorldQuery",
    "InprocMeshWorldQuery", "derive_fine_dsm", "ensure_fine_dsm", "geosvc_socket_path", "main", "open_geosvc_world_query",
    "smaps_rollup", "spawn_geosvc",
]

PROTO = 1
GEOSVC_UNAVAILABLE = 604
GEOSVC_BINDING_MISMATCH = 605
PARAM_OUT_OF_RANGE = 110

ENV_GEOSVC = "AWR_GEOSVC"
ENV_RUN_ROOT = "AWR_D3_RUN_ROOT"
DEFAULT_RUN_ROOT = "/dev/shm/awr-d3"
PRODUCTION_PROFILES = ("d3",)

DEFAULT_SLOTS = 8                     # M22 §9.3：8 槽 × 16 MiB / 连接（2–32 槽）
SLOT_RANGE = (2, 32)
DEFAULT_SLOT_BYTES = 16 << 20
MIN_SLOT_BYTES = 1 << 16
IDLE_TIMEOUT_S = 600.0
REQUEST_TIMEOUT_S = 2.0
STATS_PERIOD_S = 10.0
MAX_FRAME = 1 << 20
SUN_PATH_MAX = 107                    # sockaddr_un.sun_path 108 字节含结尾 NUL

# 槽内布局（n 为本请求条数，偏移为 n 的倍数，均按元素宽度对齐）
CAST_LAYOUT = {"o": 0, "d": 24, "tmax": 48, "t": 56, "prim": 64, "nrm": 68}      # 每条 80 B（输入 56 B、输出 24 B）
CAST_ITEM_B = 80
CLOSEST_LAYOUT = {"p": 0, "dist": 24}                                              # 每条 28 B
CLOSEST_ITEM_B = 28
WALK_LAYOUT = {"xy": 0, "z_top": 16, "z": 24, "prim": 32}                          # 每条 40 B
WALK_ITEM_B = 40

FINE_CELL_M = 0.5
FINE_VERSION = "geosvc-fine@1"
FINE_METHOD = "0.5 m max-z of 2x2 downward mesh rays per cell; cells without a hit = dsm_2m nearest cell"
FINE_LAYER_ID = "terrain.dsm_0p5m"

EXIT_OK, EXIT_LOAD_FAILED, EXIT_ALREADY_SERVING = 0, 3, 4


# ==================================================================== 公共工具


def _log(event: str, **fields) -> None:
    """结构化日志（一行 JSON，stderr；时间为 UTC）。"""
    rec = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"), "event": event, **fields}
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        sys.stderr.flush()


def smaps_rollup(pid: int | str = "self") -> dict[str, int]:
    """`/proc/<pid>/smaps_rollup` 的计数（字节）：Rss、Pss、Pss_Anon、Pss_File、Pss_Shmem、Anonymous 等。
    会话内存验收以 Pss_Anon（匿名页）与 Pss − Pss_File（匿名加共享内存）的增量计，文件页不计（D3-AC-05）。"""
    out: dict[str, int] = {}
    with open(f"/proc/{pid}/smaps_rollup", encoding="ascii") as f:
        for line in f:
            p = line.split()
            if len(p) == 3 and p[2] == "kB":
                out[p[0].rstrip(":")] = int(p[1]) * 1024
    return out


def geosvc_socket_path(world_id: str, content_version: str, run_root: str | Path | None = None) -> Path:
    """`${AWR_D3_RUN_ROOT}/geosvc/<world_id>-<contentVersion>.sock`（M22 §7.5.4）。"""
    root = Path(run_root) if run_root is not None else Path(os.environ.get(ENV_RUN_ROOT, "").strip() or DEFAULT_RUN_ROOT)
    return root / "geosvc" / f"{world_id}-{content_version}.sock"


def _ready_path(sock_path: Path) -> Path:
    return Path(str(sock_path) + ".ready")


def production_profile() -> bool:
    """`AWR_PROFILE = d3` 时为生产 profile：auto 模式连接失败不回退 inproc（M22-FR-051）。"""
    return os.environ.get("AWR_PROFILE", "dev").strip() in PRODUCTION_PROFILES


def _pack(msg: dict) -> bytes:
    b = msgpack.packb(msg, use_bin_type=True)
    if len(b) > MAX_FRAME:
        raise GeoError(BAD_REQUEST, f"frame exceeds {MAX_FRAME} B")
    return struct.pack("<I", len(b)) + b


class _ProtocolError(ConnectionError):
    """帧格式错误、序号错位或对端提前关闭：按连接断开处理（客户端重连一次）。"""


class _FrameIO:
    """长度前缀 + msgpack 的帧读写（一个套接字一个实例，不跨线程共享）。"""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()

    def feed(self, b: bytes) -> None:
        self.buf += b

    def _take(self) -> dict | None:
        if len(self.buf) < 4:
            return None
        (n,) = struct.unpack_from("<I", self.buf)
        if n > MAX_FRAME:
            raise _ProtocolError(f"frame of {n} B exceeds {MAX_FRAME} B")
        if len(self.buf) < 4 + n:
            return None
        body = bytes(self.buf[4:4 + n])
        del self.buf[:4 + n]
        try:
            msg = msgpack.unpackb(body, raw=False)
        except (ValueError, msgpack.ExtraData, msgpack.FormatError, msgpack.StackError) as e:
            raise _ProtocolError(f"bad frame: {e}") from e
        if not isinstance(msg, dict):
            raise _ProtocolError("frame is not a map")
        return msg

    def read(self) -> dict | None:
        """下一帧；对端关闭（且无残留数据）返回 None。"""
        while True:
            msg = self._take()
            if msg is not None:
                return msg
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                if self.buf:
                    raise _ProtocolError("connection closed inside a frame")
                return None
            self.buf += chunk

    def write(self, msg: dict) -> None:
        self.sock.sendall(_pack(msg))


def _view(mm: mmap.mmap, dtype: str, shape: tuple[int, ...], offset: int) -> np.ndarray:
    return np.ndarray(shape, dtype, buffer=mm, offset=offset)


def _raycast_dir(world_dir: Path, doc: dict) -> Path:
    lay = next((L for L in doc.get("layers", []) if L.get("role") == "geometry" and L.get("type") == "mesh"
                and L.get("format") == "awr-trimesh@1"), None)
    if lay is None:
        raise GeoLoadError("GEO_MISSING_FILE", "world has no awr-trimesh@1 geometry layer")
    return Path(world_dir) / lay["href"]


# ==================================================================== 0.5 m 精细 DSM


def _fine_key(content_version: str, coordinate_sha256: str) -> str:
    s = json.dumps([content_version, coordinate_sha256, FINE_VERSION], sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


def fine_dsm_path(world_dir: Path, cache_root: Path | None = None) -> Path:
    """`dsm_0p5m` sidecar 路径：世界包图层优先，否则为 geosvc 运行期缓存位置（不保证已存在）。"""
    hdr = read_world_header(Path(world_dir))
    lay = hdr.layers.get(FINE_LAYER_ID)
    if lay is not None and lay.get("status") == "ready":
        return Path(world_dir) / lay["href"]
    root = Path(cache_root) if cache_root is not None else default_cache_root(Path(world_dir))
    key = _fine_key(hdr.content_version, hdr.coordinate_sha256)
    return root / "geosvc" / f"{hdr.world_id}-{hdr.content_version}-{key}" / "dsm_0p5m.json"


def derive_fine_dsm(q: MeshWorldQuery, out_json: Path, *, block_rows: int = 64) -> Path:
    """以网格向下求交派生 0.5 m DSM（每格 2 × 2 采样取最高命中；全部未命中取 2 m 原始 DSM 最近格），按行块流式写盘，
    `.f32` 与 sidecar 均以临时文件加 `os.replace` 原子落盘（sidecar 最后写，作为完成标记）。"""
    src: Grid = q.dsm_raw
    ratio = round(src.cell / FINE_CELL_M)
    if abs(ratio * FINE_CELL_M - src.cell) > 1e-9:
        raise GeoLoadError("GEO_GRID_INVALID", f"dsm cell {src.cell} m is not a multiple of {FINE_CELL_M} m")
    W, H = src.w * ratio, src.h * ratio
    z_top = float(q._zhi) + 1.0
    tmax = float(q._zhi - q._zlo) + 2.0
    sub = np.array([0.25, 0.75]) * FINE_CELL_M
    xs = (src.x0 + (np.arange(W, dtype=np.float64)[:, None] * FINE_CELL_M + sub)).ravel()       # 格 c、子样 k
    cx = src.x0 + (np.arange(W, dtype=np.float64) + 0.5) * FINE_CELL_M
    out_json = Path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    raw = out_json.with_name("dsm_0p5m.f32")
    tmp = raw.with_name(f"{raw.name}.{os.getpid()}.tmp")
    n_miss = 0
    t0 = time.perf_counter()
    with open(tmp, "wb") as f:
        for r0 in range(0, H, block_rows):
            r1 = min(r0 + block_rows, H)
            nr = r1 - r0
            ys = (src.y0 + (np.arange(r0, r1, dtype=np.float64)[:, None] * FINE_CELL_M + sub)).ravel()
            X, Y = np.meshgrid(xs, ys)
            o = np.column_stack([X.ravel(), Y.ravel(), np.full(X.size, z_top)])
            d = np.zeros_like(o)
            d[:, 2] = -1.0
            t, _, _ = q._cast(o, d, np.full(len(o), tmax))
            z = np.where(np.isfinite(t), z_top - t, -np.inf).reshape(nr, 2, W, 2).max(axis=(1, 3))
            miss = ~np.isfinite(z)
            if miss.any():
                rr, cc = np.nonzero(miss)
                cy = src.y0 + (np.arange(r0, r1, dtype=np.float64) + 0.5) * FINE_CELL_M
                z[miss] = src.nearest(cx[cc], cy[rr])
                n_miss += int(miss.sum())
            f.write(np.ascontiguousarray(z, "<f4").tobytes())
    os.replace(tmp, raw)
    sc = {"schemaVersion": "1.0.0", "kind": "dsm", "dtype": "float32", "href": raw.name, "width": W, "height": H,
          "cellM": FINE_CELL_M, "originXY": [src.x0, src.y0], "rowOrder": "south-to-north", "nodata": None,
          "valueFrame": "world-z-m", "method": FINE_METHOD,
          "source": {"world_id": q.world_id, "content_version": q.content_version, "coordinate_sha256": q.coordinate_sha256,
                     "method_version": FINE_VERSION, "triangles": int(q.n_triangles)},
          "stats": {"cells": W * H, "fallback_cells": n_miss, "build_ms": round((time.perf_counter() - t0) * 1000, 1)}}
    tj = out_json.with_name(f"{out_json.name}.{os.getpid()}.tmp")
    tj.write_text(json.dumps(sc, indent=1), encoding="utf-8")
    os.replace(tj, out_json)
    return out_json


def open_fine_dsm(path_json: Path, *, content_version: str, coordinate_sha256: str) -> Grid:
    """只读 memmap 打开 `dsm_0p5m` 并校验绑定（缓存文件的 source 与世界版本、坐标摘要一致）。"""
    g = Grid.open(Path(path_json), expect_kind="dsm", expect_cell=FINE_CELL_M)
    src = (g.sidecar or {}).get("source")
    if src is not None and (src.get("content_version") != content_version or src.get("coordinate_sha256") != coordinate_sha256):
        raise GeoLoadError("GEO_SHA_MISMATCH", f"{path_json}: dsm_0p5m binding differs from the world header")
    return g


def ensure_fine_dsm(q: MeshWorldQuery, world_dir: Path, cache_root: Path | None = None, *, allow_derive: bool = True) -> Path:
    """返回可用的 `dsm_0p5m` sidecar 路径；缓存缺失时派生（`allow_derive = false` 时抛 GEO_CACHE_MISS）。"""
    p = fine_dsm_path(world_dir, cache_root)
    if p.exists():
        try:
            open_fine_dsm(p, content_version=q.content_version, coordinate_sha256=q.coordinate_sha256)
            return p
        except GeoLoadError:
            if not allow_derive:
                raise
    elif not allow_derive:
        raise GeoLoadError("GEO_CACHE_MISS", str(p))
    return derive_fine_dsm(q, p)


class _FineDsmMixin:
    """0.5 m 精细 DSM 的只读访问（最近格，柱体语义，同 `height_dsm`）；只服务细粒度高度查询（AWR-05 §5.1.6）。"""

    _fine_grid: Grid | None

    def _fine(self) -> Grid:
        raise NotImplementedError

    def dsm_fine_grid(self) -> GridView:
        g = self._fine()
        a = np.asarray(g.a).view()
        a.flags.writeable = False
        return GridView(a, g.x0, g.y0, g.cell)

    def height_dsm_fine(self, xy, *, oob: str = "clamp") -> np.ndarray:
        a = np.asarray(xy, np.float64)
        if a.ndim == 1:
            a = a[None]
        if a.shape[-1] < 2:
            raise GeoError(BAD_REQUEST, "points must have at least 2 components")
        return self._fine().nearest(a[:, 0], a[:, 1], oob)


# ==================================================================== inproc


class InprocMeshWorldQuery(_FineDsmMixin, MeshWorldQuery):
    """inproc 模式（`AWR_GEOSVC = inproc`，或开发 profile 下服务不可达的回退）：在调用进程内建同一瓦片场景，
    结果与 geosvc 逐位相同（M22-FR-051）；`dsm_0p5m` 在首次访问时打开（缺失时派生到同一缓存位置）。"""

    _world_dir: Path
    _fine_cache_root: Path | None

    def _fine(self) -> Grid:
        g = self.__dict__.get("_fine_grid")
        if g is None:
            p = ensure_fine_dsm(self, self._world_dir, self._fine_cache_root, allow_derive=self.allow_derive)
            g = open_fine_dsm(p, content_version=self.content_version, coordinate_sha256=self.coordinate_sha256)
            self._fine_grid = g
        return g

    def mesh_info(self) -> dict:
        return {**super().mesh_info(), "geosvc": {"mode": "inproc"}}


def _open_inproc(base: DsmWorldQuery, world_dir: Path, cache_root: Path | None) -> InprocMeshWorldQuery:
    q = InprocMeshWorldQuery.from_dsm(base, Path(world_dir))
    q._world_dir = Path(world_dir)
    q._fine_cache_root = Path(cache_root) if cache_root is not None else None
    q._fine_grid = None
    return q


# ==================================================================== 服务端


class _ThreadedScene:
    """给瓦片场景的查询固定线程数（`--threads`）；逐射线结果与线程数无关。"""

    __slots__ = ("n", "scene")

    def __init__(self, scene, n: int) -> None:
        self.scene = scene
        self.n = int(n)

    def cast_rays(self, rays, nthreads: int | None = None):
        return self.scene.cast_rays(rays, nthreads=self.n if nthreads is None else nthreads)

    def compute_distance(self, p, nthreads: int | None = None):
        return self.scene.compute_distance(p, nthreads=self.n if nthreads is None else nthreads)

    def __getattr__(self, name: str):
        return getattr(self.scene, name)


def _release_load_memory(memmaps: list[np.memmap]) -> None:
    """装载结束后归还临时内存：回收对象、`malloc_trim(0)` 把 glibc 堆中已释放的大块还给内核（建索引的临时数组），
    并对只读流的 memmap 做 MADV_DONTNEED（文件页留在页缓存，不计入本进程 RSS，查询时按需读入）。"""
    import ctypes
    import gc

    gc.collect()
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    for a in memmaps:
        mm = getattr(a, "_mmap", None)
        if mm is not None and hasattr(mmap, "MADV_DONTNEED"):
            with contextlib.suppress(OSError, ValueError):
                mm.madvise(mmap.MADV_DONTNEED)


class _Segment:
    """每连接一段 memfd 共享内存（`slots × slot_bytes`），文件描述符经 SCM_RIGHTS 交给客户端。"""

    def __init__(self, slots: int, slot_bytes: int) -> None:
        self.slots = int(slots)
        self.slot_bytes = int(slot_bytes)
        self.name = f"awr-geosvc-{os.getpid()}-{id(self):x}"
        self.fd = os.memfd_create(self.name, os.MFD_CLOEXEC)
        os.ftruncate(self.fd, self.slots * self.slot_bytes)
        self.mm = mmap.mmap(self.fd, self.slots * self.slot_bytes, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def close(self) -> None:
        if self.fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = -1
        with contextlib.suppress(BufferError, ValueError):
            self.mm.close()


class GeoService:
    """geosvc 进程主体：`load()` 装载世界（LOADING），`serve()` 监听并处理请求直至空闲退出或 SIGTERM。"""

    def __init__(self, world_dir: Path, *, cache_root: Path | None = None, socket_path: Path | None = None, threads: int = 0,
                 idle_timeout_s: float = IDLE_TIMEOUT_S, slots: int = DEFAULT_SLOTS, slot_bytes: int = DEFAULT_SLOT_BYTES,
                 stats_period_s: float = STATS_PERIOD_S, params: GeoParams | None = None) -> None:
        if not (SLOT_RANGE[0] <= slots <= SLOT_RANGE[1]):
            raise ValueError(f"slots must be in {SLOT_RANGE}")
        if slot_bytes < MIN_SLOT_BYTES or slot_bytes % 8:
            raise ValueError(f"slot_bytes must be >= {MIN_SLOT_BYTES} and a multiple of 8")
        self.world_dir = Path(world_dir)
        self.cache_root = Path(cache_root) if cache_root is not None else None
        self.socket_path = Path(socket_path) if socket_path is not None else None
        self.threads = int(threads)
        self.idle_timeout_s = float(idle_timeout_s)
        self.slots = int(slots)
        self.slot_bytes = int(slot_bytes)
        self.stats_period_s = float(stats_period_s)
        self.params = params
        self.state = "STARTING"
        self.q: MeshWorldQuery | None = None
        self.fine_path: Path | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._conns: dict[int, tuple[socket.socket, threading.Thread]] = {}
        self._next_cid = 0
        self._inflight = 0
        self._rays = 0
        self._requests = 0
        self._started = time.monotonic()
        self._lsock: socket.socket | None = None

    # ---------------------------------------------------------------- 装载
    def _set_state(self, s: str, **fields) -> None:
        self.state = s
        _log("geosvc.state", state=s, pid=os.getpid(), **fields)

    def load(self) -> None:
        from awr.world.mesh.query import open_mesh_world_query

        self._set_state("LOADING", world_dir=str(self.world_dir))
        t0 = time.perf_counter()
        q = open_mesh_world_query(self.world_dir, self.cache_root, self.params, None)
        if self.threads > 0:
            for tile in q.tiles:
                tile.scene = _ThreadedScene(tile.scene, self.threads)
        self.q = q
        self._file_backed_streams(q)
        if hasattr(q, "walk_index"):                       # 可走面索引与其 numba 核在装载期建好，首个 walk 请求不付建树与编译开销
            q.walk_index()
            q.walkable_surface(np.zeros((1, 2)), np.zeros(1))
        _release_load_memory(self._streams)
        self.fine_path = ensure_fine_dsm(q, self.world_dir, self.cache_root, allow_derive=True)
        g = open_fine_dsm(self.fine_path, content_version=q.content_version, coordinate_sha256=q.coordinate_sha256)
        self._fine_shape = (g.h, g.w)                      # 服务本身不读精细栅格：页由会话按需读入页缓存
        if self.socket_path is None:
            self.socket_path = geosvc_socket_path(q.world_id, q.content_version)
        if len(os.fsencode(str(self.socket_path))) > SUN_PATH_MAX:
            raise GeoLoadError("GEO_GRID_INVALID", f"socket path longer than {SUN_PATH_MAX} B: {self.socket_path}")
        self._load_ms = (time.perf_counter() - t0) * 1000

    def _file_backed_streams(self, q: MeshWorldQuery) -> None:
        """把装载时读入匿名内存的 `xyz`、`tri`、`cls` 流（瓦片 V/F 副本、可走面索引的源流、逐三角形语义）换成同一文件的只读
        memmap 视图：取值逐字节相同（结果不变），常驻部分只剩 Embree 场景与索引（M22-NFR-005 的 RSS 预算）。"""
        d = _raycast_dir(self.world_dir, read_world_header(self.world_dir).doc)
        idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
        xyz = np.memmap(d / "xyz.f32", "<f4", mode="r").reshape(-1, 3)
        tri = np.memmap(d / "tri.u32", "<u4", mode="r").reshape(-1, 3)
        if len(xyz) != idx["counts"]["vertices"] or len(tri) != idx["counts"]["triangles"] or len(idx["tiles"]) != len(q.tiles):
            raise GeoLoadError("GEO_GRID_INVALID", "mesh.raycast streams changed during load")
        for tile, t in zip(q.tiles, idx["tiles"], strict=True):
            tile.V = xyz[t["v0"]:t["v0"] + t["nv"]]
            tile.F = tri[t["t0"]:t["t0"] + t["nt"]]
        if getattr(q, "_walk_src", None) is not None:
            q._walk_src = (xyz, tri, idx["tiles"])
        if q.semantic is not None:
            q.semantic = np.memmap(d / "cls.u8", np.uint8, mode="r")
        self._streams = [a for a in (xyz, tri, q.semantic) if isinstance(a, np.memmap)]

    def hello_info(self) -> dict:
        q = self.q
        assert q is not None
        return {"world": {"world_id": q.world_id, "content_version": q.content_version, "coordinate_sha256": q.coordinate_sha256,
                          "triangles": int(q.n_triangles), "zlo": float(q._zlo), "zhi": float(q._zhi)},
                "tiles": [{"id": t.id, "origin_m": [float(v) for v in t.origin], "aabb_m": {"min": [float(v) for v in t.lo],
                           "max": [float(v) for v in t.hi]}, "t0": t.t0, "nt": t.nt} for t in q.tiles],
                "fine": {"sidecar": str(self.fine_path), "shape": list(self._fine_shape), "cell_m": FINE_CELL_M},
                "limits": {"cast_per_slot": self.slot_bytes // CAST_ITEM_B, "closest_per_slot": self.slot_bytes // CLOSEST_ITEM_B,
                           "max_frame": MAX_FRAME, "timeout_s": REQUEST_TIMEOUT_S}}

    # ---------------------------------------------------------------- 请求
    def _slot_args(self, msg: dict, seg: _Segment, item_b: int) -> tuple[int, int]:
        slot, n = msg.get("slot"), msg.get("n")
        if not (isinstance(slot, int) and isinstance(n, int)) or not (0 <= slot < seg.slots) or n < 0 or n * item_b > seg.slot_bytes:
            raise GeoError(PARAM_OUT_OF_RANGE, f"slot/n out of range (slots {seg.slots}, {seg.slot_bytes // item_b} items per slot)")
        return slot * seg.slot_bytes, n

    def _cast(self, msg: dict, seg: _Segment) -> None:
        base, n = self._slot_args(msg, seg, CAST_ITEM_B)
        L, mm = CAST_LAYOUT, seg.mm
        o = _view(mm, "<f8", (n, 3), base + L["o"] * n).copy()
        d = _view(mm, "<f8", (n, 3), base + L["d"] * n).copy()
        tmax = _view(mm, "<f8", (n,), base + L["tmax"] * n).copy()
        t, prim, nrm = self.q._cast(o, d, tmax)
        _view(mm, "<f8", (n,), base + L["t"] * n)[:] = t
        _view(mm, "<u4", (n,), base + L["prim"] * n)[:] = prim
        _view(mm, "<f4", (n, 3), base + L["nrm"] * n)[:] = nrm
        with self._lock:
            self._rays += n

    def _closest(self, msg: dict, seg: _Segment) -> None:
        base, n = self._slot_args(msg, seg, CLOSEST_ITEM_B)
        max_m = msg.get("max")
        if not isinstance(max_m, int | float) or not max_m > 0:
            raise GeoError(PARAM_OUT_OF_RANGE, "max must be > 0")
        p = _view(seg.mm, "<f8", (n, 3), base + CLOSEST_LAYOUT["p"] * n).copy()
        r = self.q.closest_batch(p, float(max_m))
        dist = r[0] if isinstance(r, tuple) else r
        _view(seg.mm, "<f4", (n,), base + CLOSEST_LAYOUT["dist"] * n)[:] = np.asarray(dist, np.float32)

    def _walk(self, msg: dict, seg: _Segment) -> None:
        base, n = self._slot_args(msg, seg, WALK_ITEM_B)
        L, mm = WALK_LAYOUT, seg.mm
        xy = _view(mm, "<f8", (n, 2), base + L["xy"] * n).copy()
        zt = _view(mm, "<f8", (n,), base + L["z_top"] * n).copy()
        z, prim = self.q.walkable_surface(xy, zt)
        _view(mm, "<f8", (n,), base + L["z"] * n)[:] = z
        _view(mm, "<i8", (n,), base + L["prim"] * n)[:] = prim

    def stats(self) -> dict:
        with self._lock:
            conns, inflight, rays, reqs = len(self._conns), self._inflight, self._rays, self._requests
        try:
            rss = smaps_rollup().get("Rss", 0)
        except OSError:
            rss = 0
        return {"state": self.state, "connections": conns, "queue_depth": inflight, "rays_total": rays, "requests_total": reqs,
                "rss_mib": round(rss / 2**20, 1), "uptime_s": round(time.monotonic() - self._started, 1)}

    def _dispatch(self, msg: dict, seg: _Segment) -> dict:
        op, seq = msg.get("op"), msg.get("seq")
        try:
            if op == "cast":
                self._cast(msg, seg)
                return {"ok": True, "seq": seq}
            if op == "closest":
                self._closest(msg, seg)
                return {"ok": True, "seq": seq}
            if op == "walk":
                self._walk(msg, seg)
                return {"ok": True, "seq": seq}
            if op == "ping":
                return {"ok": True, "seq": seq, "pid": os.getpid()}
            if op == "stats":
                st = self.stats()
                st["queue_depth"] = max(0, st["queue_depth"] - 1)          # 不计本请求
                return {"ok": True, "seq": seq, **st}
            if op in ("walkable", "nav_path"):
                raise GeoError(GEOSVC_UNAVAILABLE, "NAV_UNAVAILABLE: navmesh query worker is not attached to this service")
            raise GeoError(BAD_REQUEST, f"unknown op {op!r}")
        except GeoError as e:
            return {"ok": False, "seq": seq, "code": e.code, "detail": e.detail}
        except Exception as e:  # pragma: no cover - 防御：内部错误不终止连接
            _log("geosvc.error", op=op, error=f"{type(e).__name__}: {e}")
            return {"ok": False, "seq": seq, "code": INTERNAL_ERROR, "detail": type(e).__name__}

    def _hello(self, io: _FrameIO, conn: socket.socket, msg: dict) -> _Segment | None:
        q = self.q
        if msg.get("op") != "hello" or msg.get("proto") != PROTO:
            io.write({"ok": False, "code": BAD_REQUEST, "detail": f"expected hello with proto {PROTO}"})
            return None
        want = (msg.get("world_id"), msg.get("content_version"), msg.get("coordinate_sha256"))
        have = (q.world_id, q.content_version, q.coordinate_sha256)
        if want != have:
            io.write({"ok": False, "code": GEOSVC_BINDING_MISMATCH,
                      "detail": f"service binds {have[0]}@{have[1]} ({have[2][:12]}), client asked {want[0]}@{want[1]}"})
            return None
        seg = _Segment(self.slots, self.slot_bytes)
        reply = {"ok": True, "proto": PROTO, "pid": os.getpid(), "shm_name": seg.name, "slots": seg.slots,
                 "slot_bytes": seg.slot_bytes, **self.hello_info()}
        frame = _pack(reply)
        sent = socket.send_fds(conn, [frame], [seg.fd])
        if sent < len(frame):                              # 描述符随首字节送达；余下字节照常发送
            conn.sendall(frame[sent:])
        os.close(seg.fd)                                   # 客户端已持有副本；本端只保留映射
        seg.fd = -1
        return seg

    def _serve_conn(self, cid: int, conn: socket.socket) -> None:
        io = _FrameIO(conn)
        seg: _Segment | None = None
        try:
            msg = io.read()
            if msg is None:
                return
            seg = self._hello(io, conn, msg)
            if seg is None:
                return
            while True:
                msg = io.read()
                if msg is None:
                    break
                with self._lock:
                    self._inflight += 1
                    self._requests += 1
                try:
                    resp = self._dispatch(msg, seg)
                finally:
                    with self._lock:
                        self._inflight -= 1
                io.write(resp)
        except (OSError, _ProtocolError, GeoError) as e:
            _log("geosvc.conn_closed", cid=cid, reason=f"{type(e).__name__}: {e}")
        finally:
            if seg is not None:
                seg.close()
            with contextlib.suppress(OSError):
                conn.close()
            with self._lock:
                self._conns.pop(cid, None)

    # ---------------------------------------------------------------- 监听
    def _bind(self) -> socket.socket | None:
        p = self.socket_path
        assert p is not None
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if p.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1.0)
            try:
                probe.connect(str(p))
                return None                                # 已有活实例（每世界每主机一个）
            except OSError:
                p.unlink(missing_ok=True)                  # 前一实例异常退出遗留
            finally:
                probe.close()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            s.bind(str(p))
        finally:
            os.umask(old)
        os.chmod(p, 0o600)
        s.listen(64)
        s.setblocking(False)
        return s

    def request_stop(self, *_args) -> None:
        self._stop.set()

    def _heartbeat(self):
        run_dir = os.environ.get("AWR_RUN_DIR", "").strip()
        if not run_dir or not os.environ.get("AWR_SUPERVISOR_PID"):
            return None
        from awr.runtime.heartbeat import Heartbeat

        return Heartbeat(Path(run_dir) / "hb.geosvc")

    def serve(self) -> int:
        lsock = self._bind()
        if lsock is None:
            _log("geosvc.already_serving", socket=str(self.socket_path))
            return EXIT_ALREADY_SERVING
        self._lsock = lsock
        ready = _ready_path(self.socket_path)
        q = self.q
        info = {"pid": os.getpid(), "world_id": q.world_id, "content_version": q.content_version, "socket": str(self.socket_path),
                "tiles": len(q.tiles), "triangles": int(q.n_triangles), "fine": str(self.fine_path), "load_ms": round(self._load_ms, 1),
                "ready_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")}
        tmp = ready.with_name(ready.name + ".tmp")
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, ready)
        self._set_state("READY", **{k: info[k] for k in ("socket", "tiles", "triangles", "load_ms")})
        hb = self._heartbeat()
        sel = selectors.DefaultSelector()
        sel.register(lsock, selectors.EVENT_READ)
        idle_since = last_stats = time.monotonic()
        last_rays = 0
        reason = "sigterm"
        try:
            while not self._stop.is_set():
                for _key, _ in sel.select(timeout=0.2):
                    try:
                        conn, _ = lsock.accept()
                    except (BlockingIOError, InterruptedError):
                        continue
                    conn.setblocking(True)
                    with self._lock:
                        cid = self._next_cid
                        self._next_cid += 1
                        th = threading.Thread(target=self._serve_conn, args=(cid, conn), name=f"geosvc-conn-{cid}", daemon=True)
                        self._conns[cid] = (conn, th)
                    th.start()
                now = time.monotonic()
                with self._lock:
                    n_conn = len(self._conns)
                if n_conn:
                    idle_since = now
                elif now - idle_since >= self.idle_timeout_s:
                    reason = "idle"
                    break
                if now - last_stats >= self.stats_period_s:
                    st = self.stats()
                    _log("geosvc.stats", rays_per_s=round((st["rays_total"] - last_rays) / (now - last_stats), 1), **st)
                    last_rays, last_stats = st["rays_total"], now
                if hb is not None:
                    hb.beat()
        finally:
            sel.close()
            self._drain(reason)
            if hb is not None:
                hb.close()
        return EXIT_OK

    def _drain(self, reason: str) -> None:
        self._set_state("DRAINING", reason=reason)
        if self._lsock is not None:
            self._lsock.close()
        for p in (self.socket_path, _ready_path(self.socket_path)):
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)
        with self._lock:
            items = list(self._conns.values())
        for conn, _th in items:                            # 停止读取新请求；在途请求照常完成并应答
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RD)
        for _conn, th in items:
            th.join(timeout=10.0)
        self._set_state("STOPPED")


# ==================================================================== 客户端


class GeoServiceClient:
    """geosvc 的 IPC 客户端（单连接；线程安全由内部锁保证）。超时或断连时重连一次并重新握手，仍失败抛 604。"""

    def __init__(self, socket_path: Path, *, world_id: str, content_version: str, coordinate_sha256: str,
                 timeout_s: float = REQUEST_TIMEOUT_S) -> None:
        self.socket_path = Path(socket_path)
        self.binding = {"world_id": world_id, "content_version": content_version, "coordinate_sha256": coordinate_sha256}
        self.timeout_s = float(timeout_s)
        self.info: dict | None = None
        self._sock: socket.socket | None = None
        self._io: _FrameIO | None = None
        self._mm: mmap.mmap | None = None
        self._seq = 0
        self._lock = threading.RLock()
        self.reconnects = 0

    # ---------------------------------------------------------------- 连接
    def _drop(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        if self._mm is not None:
            with contextlib.suppress(BufferError, ValueError):
                self._mm.close()
        self._sock = self._io = self._mm = None

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _handshake(self) -> dict:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout_s)
        try:
            s.connect(str(self.socket_path))
            s.sendall(_pack({"op": "hello", "proto": PROTO, "pid": os.getpid(), **self.binding}))
            data, fds, _flags, _addr = socket.recv_fds(s, 1 << 16, 1)
            if not data:
                raise _ProtocolError("service closed the connection during hello")
            io = _FrameIO(s)
            io.feed(data)
            try:
                rep = io.read()
            except BaseException:
                for fd in fds:
                    os.close(fd)
                raise
            if rep is None:
                raise _ProtocolError("service closed the connection during hello")
            if not rep.get("ok"):
                for fd in fds:
                    os.close(fd)
                raise GeoError(int(rep.get("code", GEOSVC_UNAVAILABLE)), str(rep.get("detail", "")))
            if len(fds) != 1:
                raise _ProtocolError("hello reply carried no shared-memory descriptor")
            try:
                mm = mmap.mmap(fds[0], int(rep["slots"]) * int(rep["slot_bytes"]), mmap.MAP_SHARED,
                               mmap.PROT_READ | mmap.PROT_WRITE)
            finally:
                os.close(fds[0])
        except BaseException:
            s.close()
            raise
        self._sock, self._io, self._mm = s, io, mm
        self.info = rep
        return rep

    def connect(self) -> dict:
        """握手（首次或重连）；不可达抛 604，绑定不一致抛 605。"""
        with self._lock:
            self._drop()
            try:
                return self._handshake()
            except GeoError:
                raise
            except (OSError, _ProtocolError) as e:
                raise GeoError(GEOSVC_UNAVAILABLE, f"{self.socket_path}: {type(e).__name__}: {e}") from e

    # ---------------------------------------------------------------- 请求
    def _call(self, fn: Callable[[], object]):
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._sock is None:
                        self.reconnects += int(self.info is not None)
                        self._handshake()
                    return fn()
                except GeoError:
                    raise
                except (OSError, _ProtocolError) as e:
                    self._drop()
                    if attempt:
                        raise GeoError(GEOSVC_UNAVAILABLE, f"{self.socket_path}: {type(e).__name__}: {e}") from e
            raise AssertionError("unreachable")  # pragma: no cover

    def _simple(self, op: str) -> dict:
        def run() -> dict:
            self._seq += 1
            self._io.write({"op": op, "seq": self._seq})
            rep = self._io.read()
            if rep is None or rep.get("seq") != self._seq:
                raise _ProtocolError("reply out of sequence")
            if not rep.get("ok"):
                raise GeoError(int(rep["code"]), str(rep.get("detail", "")))
            return rep
        return self._call(run)

    def ping(self) -> dict:
        return self._simple("ping")

    def stats(self) -> dict:
        return self._simple("stats")

    def request(self, op: str, **fields) -> dict:
        """无共享内存数据的请求（`walkable`、`nav_path` 等扩展操作）。"""
        def run() -> dict:
            self._seq += 1
            self._io.write({"op": op, "seq": self._seq, **fields})
            rep = self._io.read()
            if rep is None or rep.get("seq") != self._seq:
                raise _ProtocolError("reply out of sequence")
            if not rep.get("ok"):
                raise GeoError(int(rep["code"]), str(rep.get("detail", "")))
            return rep
        return self._call(run)

    def _pipeline(self, op: str, n: int, item_b: int, fields: dict, put: Callable[[mmap.mmap, int, int, int], None],
                  get: Callable[[mmap.mmap, int, int, int], None]) -> None:
        """把 n 条按槽容量切块，在全部槽上流水提交（同一连接按序应答）；任一块出错时收齐在途应答后抛出。"""
        slots, slot_bytes = int(self.info["slots"]), int(self.info["slot_bytes"])
        cap = max(1, slot_bytes // item_b)
        chunks = [(i, min(i + cap, n)) for i in range(0, n, cap)]
        free = deque(range(slots))
        pending: deque[tuple[int, int, int, int]] = deque()
        nxt = 0
        err: GeoError | None = None
        mm = self._mm
        while pending or (err is None and nxt < len(chunks)):
            while err is None and nxt < len(chunks) and free:
                slot = free.popleft()
                i0, i1 = chunks[nxt]
                nxt += 1
                put(mm, slot * slot_bytes, i0, i1)
                self._seq += 1
                self._io.write({"op": op, "seq": self._seq, "slot": slot, "n": i1 - i0, **fields})
                pending.append((slot, i0, i1, self._seq))
            rep = self._io.read()
            slot, i0, i1, seq = pending.popleft()
            if rep is None or rep.get("seq") != seq:
                raise _ProtocolError("reply out of sequence")
            if not rep.get("ok"):
                err = err or GeoError(int(rep["code"]), str(rep.get("detail", "")))
            elif err is None:
                get(mm, slot * slot_bytes, i0, i1)
            free.append(slot)
        if err is not None:
            raise err

    def cast(self, o: np.ndarray, d: np.ndarray, tmax: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """`MeshWorldQuery._cast` 的远程版本：o (N, 3) float64、d 单位方向、tmax (N,)；返回 (t float64、prim uint32、法线 float32)。"""
        o = np.ascontiguousarray(o, np.float64).reshape(-1, 3)
        d = np.ascontiguousarray(d, np.float64).reshape(-1, 3)
        tmax = np.ascontiguousarray(tmax, np.float64).reshape(-1)
        n = len(o)
        t = np.empty(n, np.float64)
        prim = np.empty(n, np.uint32)
        nrm = np.empty((n, 3), np.float32)
        if n == 0:
            return t, prim, nrm
        L = CAST_LAYOUT

        def put(mm, base, i0, i1):
            k = i1 - i0
            _view(mm, "<f8", (k, 3), base + L["o"] * k)[:] = o[i0:i1]
            _view(mm, "<f8", (k, 3), base + L["d"] * k)[:] = d[i0:i1]
            _view(mm, "<f8", (k,), base + L["tmax"] * k)[:] = tmax[i0:i1]

        def get(mm, base, i0, i1):
            k = i1 - i0
            t[i0:i1] = _view(mm, "<f8", (k,), base + L["t"] * k)
            prim[i0:i1] = _view(mm, "<u4", (k,), base + L["prim"] * k)
            nrm[i0:i1] = _view(mm, "<f4", (k, 3), base + L["nrm"] * k)

        self._call(lambda: self._pipeline("cast", n, CAST_ITEM_B, {}, put, get))
        return t, prim, nrm

    def closest(self, p: np.ndarray, max_m: float) -> np.ndarray:
        P = np.ascontiguousarray(p, np.float64).reshape(-1, 3)
        n = len(P)
        out = np.empty(n, np.float32)
        if n == 0:
            return out
        L = CLOSEST_LAYOUT

        def put(mm, base, i0, i1):
            _view(mm, "<f8", (i1 - i0, 3), base + L["p"] * (i1 - i0))[:] = P[i0:i1]

        def get(mm, base, i0, i1):
            out[i0:i1] = _view(mm, "<f4", (i1 - i0,), base + L["dist"] * (i1 - i0))

        self._call(lambda: self._pipeline("closest", n, CLOSEST_ITEM_B, {"max": float(max_m)}, put, get))
        return out

    def walk(self, xy: np.ndarray, z_top: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """`MeshWorldQuery.walkable_surface` 的远程版本：(z float64，无命中 NaN；prim_id int64，无命中 −1)。"""
        P = np.ascontiguousarray(xy, np.float64).reshape(-1, 2)
        zt = np.ascontiguousarray(np.broadcast_to(np.asarray(z_top, np.float64), (len(P),)))
        n = len(P)
        z = np.empty(n, np.float64)
        prim = np.empty(n, np.int64)
        if n == 0:
            return z, prim
        L = WALK_LAYOUT

        def put(mm, base, i0, i1):
            k = i1 - i0
            _view(mm, "<f8", (k, 2), base + L["xy"] * k)[:] = P[i0:i1]
            _view(mm, "<f8", (k,), base + L["z_top"] * k)[:] = zt[i0:i1]

        def get(mm, base, i0, i1):
            k = i1 - i0
            z[i0:i1] = _view(mm, "<f8", (k,), base + L["z"] * k)
            prim[i0:i1] = _view(mm, "<i8", (k,), base + L["prim"] * k)

        self._call(lambda: self._pipeline("walk", n, WALK_ITEM_B, {}, put, get))
        return z, prim


class GeoSvcMeshWorldQuery(_FineDsmMixin, MeshWorldQuery):
    """会话侧 MeshWorldQuery：2.5D 方法与 D2 相同（本地只读 memmap 栅格），3D 求交经 geosvc（`_cast`、`closest_batch`），
    `semantic_of` 读只读 memmap 的 `cls.u8`，`height_dsm_fine` 读服务给出的 `dsm_0p5m` 只读 memmap。不导入 Open3D、不建树。

    `tiles` 只含瓦片表元数据（`scene` 为 None）；需要直接访问瓦片场景的调用方（MeshLidar 等）应使用 inproc 模式。"""

    client: GeoServiceClient

    @classmethod
    def connect(cls, base: DsmWorldQuery, world_dir: Path, socket_path: Path, *, timeout_s: float = REQUEST_TIMEOUT_S
                ) -> GeoSvcMeshWorldQuery:
        cl = GeoServiceClient(socket_path, world_id=base.world_id, content_version=base.content_version,
                              coordinate_sha256=base.coordinate_sha256, timeout_s=timeout_s)
        info = cl.connect()
        q = cls.__new__(cls)
        q.__dict__.update(base.__dict__)
        q.client = cl
        w = info["world"]
        empty_v, empty_f = np.zeros((0, 3), np.float32), np.zeros((0, 3), np.uint32)
        q.tiles = [MeshTile(int(t["id"]), np.asarray(t["origin_m"], np.float64), np.asarray(t["aabb_m"]["min"], np.float64),
                            np.asarray(t["aabb_m"]["max"], np.float64), int(t["t0"]), int(t["nt"]), None, empty_v, empty_f)
                   for t in info["tiles"]]
        q.n_triangles = int(w["triangles"])
        q._zlo, q._zhi = float(w["zlo"]), float(w["zhi"])
        doc = read_world_header(Path(world_dir)).doc
        cls_path = _raycast_dir(Path(world_dir), doc) / "cls.u8"
        q.semantic = np.memmap(cls_path, np.uint8, mode="r") if cls_path.exists() else None
        q._walk_src = None
        q._walk = None
        q.nav_surface = {}
        try:
            q._fine_grid = open_fine_dsm(Path(info["fine"]["sidecar"]), content_version=base.content_version,
                                         coordinate_sha256=base.coordinate_sha256)
        except GeoLoadError as e:
            cl.close()
            raise GeoError(GEOSVC_BINDING_MISMATCH, f"dsm_0p5m: {e}") from e
        return q

    def _fine(self) -> Grid:
        return self._fine_grid

    def _cast(self, o: np.ndarray, d: np.ndarray, tmax: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return self.client.cast(o, d, tmax)

    def closest_batch(self, p, max_m: float) -> np.ndarray:
        return self.client.closest(np.asarray(p, np.float64).reshape(-1, 3), float(max_m))

    def walkable_surface(self, xy, z_top) -> tuple[np.ndarray, np.ndarray]:
        """自 z_top 竖直向下的首个网格命中，经 geosvc（`WalkIndex` 只在服务端常驻）；条数上限与 inproc 相同。"""
        P = np.asarray(xy, np.float64).reshape(-1, 2)
        if len(P) > MAX_RAYS:
            raise GeoError(MESH_PARAM_OUT_OF_RANGE, f"points must be <= {MAX_RAYS}")
        return self.client.walk(P, z_top)

    def walk_index(self):
        raise GeoError(GEOSVC_UNAVAILABLE, "walk index is resident in geosvc only; use walkable_surface or inproc mode")

    def close(self) -> None:
        self.client.close()

    def mesh_info(self) -> dict:
        return {**super().mesh_info(),
                "geosvc": {"mode": "service", "socket": str(self.client.socket_path), "pid": (self.client.info or {}).get("pid")}}


def open_geosvc_world_query(world_dir: Path, cache_root: Path | None = None, params: GeoParams | None = None,
                            allow_derive: bool | None = None, *, geosvc: str | None = None, socket_path: Path | None = None,
                            timeout_s: float = REQUEST_TIMEOUT_S) -> MeshWorldQuery:
    """会话侧入口（与 `open_mesh_world_query` 同签名，另加关键字参数）：按 `geosvc`（缺省 `AWR_GEOSVC`，未设置为 auto）
    返回 `GeoSvcMeshWorldQuery`（服务）或 `InprocMeshWorldQuery`（进程内）。"""
    mode = (geosvc or os.environ.get(ENV_GEOSVC, "") or "auto").strip().lower()
    if mode not in ("auto", "service", "inproc"):
        raise ValueError(f"{ENV_GEOSVC} must be auto, service or inproc, got {mode!r}")
    world_dir = Path(world_dir)
    base = open_dsm_world_query(world_dir, cache_root, params, allow_derive)
    if mode == "inproc":
        return _open_inproc(base, world_dir, cache_root)
    sock = Path(socket_path) if socket_path is not None else geosvc_socket_path(base.world_id, base.content_version)
    try:
        return GeoSvcMeshWorldQuery.connect(base, world_dir, sock, timeout_s=timeout_s)
    except GeoError as e:
        if e.code != GEOSVC_UNAVAILABLE or mode == "service" or production_profile():
            raise
        _log("geosvc.fallback_inproc", world_id=base.world_id, socket=str(sock), detail=e.detail)
    return _open_inproc(base, world_dir, cache_root)


# ==================================================================== 进程管理与命令行


def spawn_geosvc(world_dir: Path, *, socket_path: Path | None = None, cache_root: Path | None = None, threads: int = 0,
                 idle_timeout_s: float = IDLE_TIMEOUT_S, slots: int = DEFAULT_SLOTS, slot_bytes: int = DEFAULT_SLOT_BYTES,
                 env: dict[str, str] | None = None, python: str | None = None, stderr=None, wait_s: float = 300.0
                 ) -> subprocess.Popen:
    """启动 geosvc 子进程并等待就绪文件（供 M27 调度器的开发路径与测试使用）；超时或进程提前退出抛 604。"""
    world_dir = Path(world_dir)
    e = dict(os.environ if env is None else env)
    if socket_path is None:
        hdr = read_world_header(world_dir)
        socket_path = geosvc_socket_path(hdr.world_id, hdr.content_version, e.get(ENV_RUN_ROOT) or None)
    cmd = [python or sys.executable, "-m", "awr.world.mesh.geosvc", "--world-dir", str(world_dir), "--socket", str(socket_path),
           "--threads", str(threads), "--idle-timeout-s", str(idle_timeout_s), "--slots", str(slots), "--slot-bytes", str(slot_bytes)]
    if cache_root is not None:
        cmd += ["--cache-dir", str(cache_root)]
    p = subprocess.Popen(cmd, env=e, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr)
    ready = _ready_path(Path(socket_path))
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if p.poll() is not None:
            raise GeoError(GEOSVC_UNAVAILABLE, f"geosvc exited with {p.returncode} before ready")
        with contextlib.suppress(OSError, ValueError):
            if json.loads(ready.read_text(encoding="utf-8")).get("pid") == p.pid:
                return p
        time.sleep(0.05)
    p.kill()
    p.wait()
    raise GeoError(GEOSVC_UNAVAILABLE, f"geosvc not ready within {wait_s} s")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.world.mesh.geosvc", description="ANet Drone4D shared geometry service (geosvc)")
    ap.add_argument("--world", help="world id under --worlds-dir")
    ap.add_argument("--worlds-dir", default=os.environ.get("AWR_WORLDS_DIR", "worlds"))
    ap.add_argument("--world-dir", help="world package directory (overrides --world)")
    ap.add_argument("--cache-dir", help="derived cache root (default AWR_GEO_CACHE_DIR or <worlds>/.geo-cache)")
    ap.add_argument("--socket", help=f"socket path (default ${ENV_RUN_ROOT}/geosvc/<world>-<contentVersion>.sock)")
    ap.add_argument("--threads", type=int, default=0, help="ray-casting threads per request (0 = automatic)")
    ap.add_argument("--idle-timeout-s", type=float, default=IDLE_TIMEOUT_S)
    ap.add_argument("--slots", type=int, default=DEFAULT_SLOTS)
    ap.add_argument("--slot-bytes", type=int, default=DEFAULT_SLOT_BYTES)
    ap.add_argument("--stats-period-s", type=float, default=STATS_PERIOD_S)
    a = ap.parse_args(argv)
    if not a.world_dir and not a.world:
        ap.error("--world or --world-dir is required")
    wd = Path(a.world_dir) if a.world_dir else Path(a.worlds_dir) / a.world
    try:
        svc = GeoService(wd, cache_root=Path(a.cache_dir) if a.cache_dir else None, socket_path=Path(a.socket) if a.socket else None,
                         threads=a.threads, idle_timeout_s=a.idle_timeout_s, slots=a.slots, slot_bytes=a.slot_bytes,
                         stats_period_s=a.stats_period_s)
    except ValueError as e:
        ap.error(str(e))
    signal.signal(signal.SIGTERM, svc.request_stop)
    signal.signal(signal.SIGINT, svc.request_stop)
    svc._set_state("STARTING", world_dir=str(wd))
    try:
        svc.load()
    except (GeoLoadError, GeoError, OSError, ValueError, KeyError) as e:
        svc._set_state("FAILED", error=f"{type(e).__name__}: {e}")
        return EXIT_LOAD_FAILED
    return svc.serve()


if __name__ == "__main__":
    sys.exit(main())
