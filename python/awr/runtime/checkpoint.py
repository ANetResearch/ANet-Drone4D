"""checkpoint 文件格式与 CheckpointStore（M11 §6.3.7，MS1 冻结；ADR-019；g05 §7.4；M11-FR-016、FR-017）。

路径 `/dev/shm/awr/<run>/ckpt/<t_sim_ns 20 位补零>.bin`，毒性标记为同名 `.poison` 空文件；镜像目录 `runs/<run>/ckpt/`。
小端布局（M11 内部格式，不是对外文件格式）：

    头部 [0, 64)            magic "AWRC"、version u16 = 1、flags u16、epoch u32、segment u32、t_sim_ns i64、layout_id u32、
                           n_arrays u32、meta_off u64、meta_len u64、crc32 u32（覆盖 [64, 文件尾)）、填充
    TOC [64, 64 + 96·n)     每项 name char[40]、dtype char[16]（numpy dtype.str）、shape u32×4（未用维为 0xFFFFFFFF）、
                           offset u64、nbytes u64、填充 8 B
    数组区                  每个数组 C 连续、64 字节对齐
    meta                    msgpack（SimClock、RNG、任务、租约与席位、FSM、围栏、cid 幂等表等，内容由 M08 定义）

写入：`save()` 在调用方主线程只把数组拷贝进预分配缓冲（1000 架 1–2 MB）；序列化、crc32、写 `.tmp` 与 `os.replace`
在后台线程；保留 3 代；每 10 s（墙钟）把最新一代复制到镜像目录。两份缓冲都在写盘时本次保存跳过并计数（不阻塞主循环）。
读取：校验 magic、version、layout_id 与 crc32，任一失败视为毒性（写 `.poison`）并尝试上一代；tmpfs 目录为空时读镜像。
结构化 dtype 以 `|V<n>` 保存，读回为 void 数组，由调用方 `.view(dtype)` 还原。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import threading
import time
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgpack
import numpy as np

__all__ = ["MAGIC", "Checkpoint", "CheckpointError", "CheckpointStore", "read_checkpoint", "serialize"]

log = logging.getLogger("awr.runtime.checkpoint")

MAGIC = b"AWRC"
VERSION = 1
HDR_BYTES = 64
TOC_BYTES = 96
ALIGN = 64
NAME_BYTES = 40
DTYPE_BYTES = 16
MAX_DIMS = 4
DIM_UNUSED = 0xFFFFFFFF
FILE_RE_SUFFIX = ".bin"
POISON_WINDOW_S = 5.0
MAX_POISONED = 3

_HDR = np.dtype({
    "names": ["magic", "version", "flags", "epoch", "segment", "t_sim_ns", "layout_id", "n_arrays", "meta_off", "meta_len",
              "crc32"],
    "formats": ["S4", "<u2", "<u2", "<u4", "<u4", "<i8", "<u4", "<u4", "<u8", "<u8", "<u4"],
    "offsets": [0, 4, 6, 8, 12, 16, 24, 28, 32, 40, 48],
    "itemsize": HDR_BYTES,
})
_TOC = np.dtype({
    "names": ["name", "dtype", "shape", "offset", "nbytes"],
    "formats": [f"S{NAME_BYTES}", f"S{DTYPE_BYTES}", ("<u4", MAX_DIMS), "<u8", "<u8"],
    "offsets": [0, 40, 56, 72, 80],
    "itemsize": TOC_BYTES,
})


class CheckpointError(Exception):
    code = 10  # 退出码 STATE_INCOMPATIBLE（AWR-19 §16.2）


@dataclass
class Checkpoint:
    t_sim_ns: int
    epoch: int
    segment: int
    layout_id: int
    flags: int
    arrays: dict[str, np.ndarray]
    meta: dict
    path: Path


def _align(n: int) -> int:
    return (n + ALIGN - 1) // ALIGN * ALIGN


def serialize(t_sim_ns: int, epoch: int, segment: int, layout_id: int, arrays: Mapping[str, np.ndarray], meta: Any,
              flags: int = 0) -> bytes:
    names = list(arrays)
    n = len(names)
    toc = np.zeros(n, _TOC)
    off = _align(HDR_BYTES + n * TOC_BYTES)
    chunks: list[tuple[int, memoryview]] = []
    for i, name in enumerate(names):
        a = np.asarray(arrays[name])
        if not a.flags.c_contiguous:
            a = a.copy(order="C")  # ascontiguousarray 会把 0 维数组升为 1 维
        bname = name.encode("utf-8")
        ds = a.dtype.str.encode("ascii")
        if len(bname) > NAME_BYTES or len(ds) > DTYPE_BYTES:
            raise ValueError(f"数组名或 dtype 过长：{name!r} {a.dtype.str}")
        if a.ndim > MAX_DIMS:
            raise ValueError(f"数组维数超过 {MAX_DIMS}：{name!r}")
        shape = list(a.shape) + [DIM_UNUSED] * (MAX_DIMS - a.ndim)
        toc[i] = (bname, ds, shape, off, a.nbytes)
        chunks.append((off, memoryview(a.reshape(-1).view(np.uint8)) if a.nbytes else memoryview(b"")))
        off = _align(off + a.nbytes)
    mb = msgpack.packb(meta, use_bin_type=True)
    meta_off = off
    total = meta_off + len(mb)
    buf = bytearray(total)
    toc_b = toc.tobytes()
    buf[HDR_BYTES:HDR_BYTES + len(toc_b)] = toc_b
    for o, mv in chunks:
        buf[o:o + len(mv)] = mv
    buf[meta_off:total] = mb
    crc = zlib.crc32(memoryview(buf)[HDR_BYTES:]) & 0xFFFFFFFF
    hdr = np.zeros(1, _HDR)
    hdr[0] = (MAGIC, VERSION, flags, epoch, segment, t_sim_ns, layout_id & 0xFFFFFFFF, n, meta_off, len(mb), crc)
    buf[0:HDR_BYTES] = hdr.tobytes()
    return bytes(buf)


def read_checkpoint(path: Path, *, layout_id: int) -> Checkpoint:
    """读取并校验一代 checkpoint；任何不一致抛 CheckpointError。"""
    path = Path(path)
    data = path.read_bytes()
    if len(data) < HDR_BYTES:
        raise CheckpointError(f"文件过短：{path}")
    h = np.frombuffer(data, _HDR, count=1)[0]
    if bytes(h["magic"]) != MAGIC or int(h["version"]) != VERSION:
        raise CheckpointError(f"magic 或版本不符：{path}")
    if int(h["layout_id"]) != (layout_id & 0xFFFFFFFF):
        raise CheckpointError(f"layout_id 不符：{path} 0x{int(h['layout_id']):08X}")
    n, meta_off, meta_len = int(h["n_arrays"]), int(h["meta_off"]), int(h["meta_len"])
    if HDR_BYTES + n * TOC_BYTES > len(data) or meta_off + meta_len != len(data):
        raise CheckpointError(f"长度不符：{path}")
    if zlib.crc32(memoryview(data)[HDR_BYTES:]) & 0xFFFFFFFF != int(h["crc32"]):
        raise CheckpointError(f"crc32 不符：{path}")
    toc = np.frombuffer(data, _TOC, count=n, offset=HDR_BYTES)
    arrays: dict[str, np.ndarray] = {}
    for t in toc:
        name = bytes(t["name"]).rstrip(b"\0").decode("utf-8")
        dt = np.dtype(bytes(t["dtype"]).rstrip(b"\0").decode("ascii"))
        shape = tuple(int(x) for x in t["shape"] if int(x) != DIM_UNUSED)
        o, nb = int(t["offset"]), int(t["nbytes"])
        if o + nb > meta_off or (dt.itemsize and nb != dt.itemsize * int(np.prod(shape, dtype=np.int64))):
            raise CheckpointError(f"数组 {name} 越界或尺寸不符：{path}")
        arrays[name] = np.frombuffer(data, dt, count=nb // dt.itemsize if dt.itemsize else 0, offset=o).reshape(shape).copy()
    meta = msgpack.unpackb(data[meta_off:meta_off + meta_len], raw=False, strict_map_key=False)
    return Checkpoint(int(h["t_sim_ns"]), int(h["epoch"]), int(h["segment"]), int(h["layout_id"]), int(h["flags"]),
                      arrays, meta, path)


class _Slot:
    __slots__ = ("arrays", "busy", "epoch", "meta", "segment", "t_sim_ns")

    def __init__(self) -> None:
        self.arrays: dict[str, np.ndarray] = {}
        self.busy = False
        self.t_sim_ns = 0
        self.epoch = 0
        self.segment = 0
        self.meta: Any = None


class CheckpointStore:
    """tmpfs 上保留 keep 代 checkpoint，后台线程写盘并定期镜像；主线程 save() 只做 numpy 拷贝。"""

    def __init__(self, dir: Path, *, layout_id: int, keep: int = 3, mirror: Path | None = None,
                 mirror_every_s: float = 10.0) -> None:
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.layout_id = layout_id
        self.keep = max(1, int(keep))
        self.mirror = Path(mirror) if mirror is not None else None
        self.mirror_every_s = mirror_every_s
        self._slots = [_Slot(), _Slot()]
        self._cv = threading.Condition()
        self._queue: list[_Slot] = []
        self._stop = False
        self._last_mirror = 0.0
        self.stats = {"saves": 0, "skipped": 0, "written": 0, "write_ms_last": 0.0, "mirrored": 0, "errors": 0}
        self._thread = threading.Thread(target=self._writer, name="awr-ckpt-writer", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ 写
    def save(self, t_sim_ns: int, epoch: int, segment: int, arrays: Mapping[str, np.ndarray], meta: dict) -> bool:
        """主线程：拷贝数组到空闲缓冲并排队；两份缓冲都忙时跳过（返回 False）。meta 交出后调用方不得再修改。"""
        with self._cv:
            slot = next((s for s in self._slots if not s.busy), None)
            if slot is None:
                self.stats["skipped"] += 1
                return False
            slot.busy = True
        dst = slot.arrays
        for name, a in arrays.items():
            b = dst.get(name)
            if b is None or b.shape != a.shape or b.dtype != a.dtype:
                b = dst[name] = np.empty(a.shape, a.dtype)
            np.copyto(b, a)
        for name in [k for k in dst if k not in arrays]:
            del dst[name]
        slot.t_sim_ns, slot.epoch, slot.segment, slot.meta = int(t_sim_ns), int(epoch), int(segment), dict(meta)
        with self._cv:
            self._queue.append(slot)
            self._cv.notify()
        self.stats["saves"] += 1
        return True

    def _writer(self) -> None:
        while True:
            slot = None
            with self._cv:
                if not self._queue and not self._stop:
                    self._cv.wait(timeout=1.0)
                if self._queue:
                    slot = self._queue.pop(0)
                elif self._stop:
                    return
            if slot is not None:
                try:
                    self._write(slot)
                except Exception:
                    self.stats["errors"] += 1
                    log.exception("checkpoint write failed")
                finally:
                    with self._cv:
                        slot.busy = False
                        self._cv.notify_all()
            self._maybe_mirror(force=False)  # 锁外做文件复制，不阻塞主线程的 save()

    def _write(self, slot: _Slot) -> None:
        t0 = time.perf_counter()
        data = serialize(slot.t_sim_ns, slot.epoch, slot.segment, self.layout_id, slot.arrays, slot.meta)
        final = self.dir / f"{slot.t_sim_ns:020d}{FILE_RE_SUFFIX}"
        tmp = final.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, final)
        self._prune(self.dir)
        self.stats["written"] += 1
        self.stats["write_ms_last"] = round((time.perf_counter() - t0) * 1e3, 3)

    def _generations(self, d: Path) -> list[Path]:
        try:
            files = [p for p in d.iterdir() if p.suffix == FILE_RE_SUFFIX and p.stem.isdigit()]
        except FileNotFoundError:
            return []
        return sorted(files, key=lambda p: int(p.stem), reverse=True)

    def _prune(self, d: Path) -> None:
        for p in self._generations(d)[self.keep:]:
            for q in (p, p.with_suffix(".poison")):
                q.unlink(missing_ok=True)

    def _maybe_mirror(self, *, force: bool) -> None:
        if self.mirror is None:
            return
        now = time.monotonic()
        if not force and now - self._last_mirror < self.mirror_every_s:
            return
        gens = self._generations(self.dir)
        if not gens:
            return
        self._last_mirror = now
        src = gens[0]
        try:
            self.mirror.mkdir(parents=True, exist_ok=True, mode=0o700)
            dst = self.mirror / src.name
            if dst.exists():
                return
            tmp = dst.with_suffix(".tmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
            self._prune(self.mirror)
            self.stats["mirrored"] += 1
        except OSError:
            self.stats["errors"] += 1
            log.warning("checkpoint mirror failed", exc_info=True)

    def flush(self, timeout: float = 5.0) -> bool:
        """等待排队的保存写完。"""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._queue or any(s.busy for s in self._slots):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(timeout=left)
        return True

    def close(self, *, final: bool = True) -> None:
        """SIGTERM 时调用：等待写盘完成；final 时同步镜像最新一代。"""
        self.flush()
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        self._thread.join(timeout=5.0)
        if final:
            self._maybe_mirror(force=True)

    # ------------------------------------------------------------ 读与毒性
    def poison(self, t_sim_ns: int) -> None:
        for d in (self.dir, self.mirror):
            if d is None:
                continue
            p = d / f"{int(t_sim_ns):020d}.poison"
            if (d / f"{int(t_sim_ns):020d}{FILE_RE_SUFFIX}").exists():
                p.touch()

    def is_poisoned(self, path: Path) -> bool:
        return path.with_suffix(".poison").exists()

    def load_latest(self, *, skip_poisoned: bool = True) -> Checkpoint | None:
        """最新的有效一代；校验失败的代写 `.poison` 并尝试上一代；tmpfs 目录无可用代时读镜像目录。"""
        for d in (self.dir, self.mirror):
            if d is None:
                continue
            for p in self._generations(d):
                if skip_poisoned and self.is_poisoned(p):
                    continue
                try:
                    return read_checkpoint(p, layout_id=self.layout_id)
                except (CheckpointError, OSError, ValueError) as e:
                    log.warning("checkpoint invalid, poisoned", extra={"kv": {"path": str(p), "err": str(e)}})
                    with contextlib.suppress(OSError):
                        p.with_suffix(".poison").touch()
        return None

    def restore_for_restart(self, restart_count: int, *, poison_window_s: float = POISON_WINDOW_S,
                            max_poisoned: int = MAX_POISONED) -> Checkpoint | None:
        """崩溃重启时的恢复策略（D1-ext；M11-FR-017；ADR-019；D1-AC-11b）：

        - `restart_count == 0`（冷启动）：不恢复，清除恢复标记，返回 None；
        - 上一次恢复后 `poison_window_s`（5 s，系统单调时钟）内再次崩溃：把上一次恢复的那一代标记为 poison，改用上一代；
        - 从最新起连续中毒的代数达到 `max_poisoned`（3）：返回 None，生产者从剧本起点重开（segment + 1）；
        - 否则返回最新有效一代，并写恢复标记 `.restored`（`t_sim_ns`、恢复时刻），供下一次崩溃判定。
        生产者在恢复后稳定运行超过 `poison_window_s` 时可调用 `confirm_restored()` 提前清除标记（不调用也不影响判定）。
        """
        marker = self.dir / ".restored"
        if restart_count <= 0:
            with contextlib.suppress(OSError):
                marker.unlink()
            return None
        with contextlib.suppress(OSError, ValueError):
            t_prev, at_ns = (int(x) for x in marker.read_text().split())
            if time.monotonic_ns() - at_ns < int(poison_window_s * 1e9):
                self.poison(t_prev)
                log.warning("crashed shortly after restore; generation poisoned", extra={"kv": {"t_sim_ns": t_prev}})
        with contextlib.suppress(OSError):
            marker.unlink()
        if self.poisoned_generations() >= max_poisoned:
            log.warning("too many poisoned generations; restart from scenario start",
                        extra={"kv": {"poisoned": self.poisoned_generations()}})
            return None
        ck = self.load_latest(skip_poisoned=True)
        if ck is not None:
            with contextlib.suppress(OSError):
                self.dir.mkdir(parents=True, exist_ok=True)
                tmp = marker.with_suffix(".tmp")
                tmp.write_text(f"{int(ck.t_sim_ns)} {time.monotonic_ns()}")
                os.replace(tmp, marker)
        return ck

    def confirm_restored(self) -> None:
        with contextlib.suppress(OSError):
            (self.dir / ".restored").unlink()

    def poisoned_generations(self) -> int:
        """tmpfs 目录中连续（从最新起）中毒的代数；达到 3 时生产者应从剧本起点重开（FR-017）。"""
        n = 0
        for p in self._generations(self.dir):
            if not self.is_poisoned(p):
                break
            n += 1
        return n
