"""StateRing：单写者、多读者的 mmap seqlock 环（AWR-17 §9.2；ADR-018；M11-FR-001 至 FR-005）。

文件 `/dev/shm/awr/<run>/state.<producer>`，权限 0600，布局（小端，字段自然对齐）：

    [0, 256)          RingHeader（写者独占写；时钟组以 clock_seq 顺序锁保护）
    [256, 512)        ReaderCursor x 8（每个读者只写自己的行）
    [512, 512 + K*S)  Slot x K；S = ceil64(64 + cap*96)；Slot = SlotHeader 64 B | Full64[cap] | Lite32[cap]

默认 K = 32、cap = 1024：S = 98,368 B，文件 3,148,288 B。布局常量全部来自生成的 `awr.contracts.layouts`
（IPC_RING_HEADER、IPC_READER_CURSOR、IPC_SLOT_HEADER），本文件不写格式串。

seqlock 在 x86-64 TSO 下正确（存储按程序顺序可见）；其他架构创建或 attach 时抛 PlatformUnsupported
（V0.5 改用 ZenohStateBus）。锁字、head、clock_seq 均为 8 字节对齐的单次存储（numpy 标量写）。

由 g05 原型 `.cache/research/g05/statering.py` 转正：游标 4 -> 8（头区 512 B）；K 默认 32；头部新增
segment、rtf_milli、id_base、id_count、step_max_us、clock_seq、step_p50_us、catchup_saturated；
open_or_create、identity、header() 一致性读、begin_publish/commit_publish；原型文件（4 游标）不得复用。

`LocalRing` 与 StateRing 同接口，缓冲为进程内匿名映射，用于 `--inproc` 与单元测试（M11-FR-005）。
"""

from __future__ import annotations

import contextlib
import fcntl
import itertools
import mmap
import os
import platform
import threading
import time
import zlib
from pathlib import Path
from typing import ClassVar, NamedTuple

import numpy as np

from awr.contracts.enums import TimeState
from awr.contracts.frame import STRUCTS
from awr.contracts.layouts import (
    DRONE_STATE64,
    IPC_READER_CURSOR,
    IPC_READER_CURSOR_OFFSETS,
    IPC_RING_HEADER,
    IPC_RING_HEADER_CONST,
    IPC_RING_HEADER_OFFSETS,
    IPC_SLOT_HEADER,
    IPC_SLOT_HEADER_BITS,
    SWARM_LITE32,
)

from . import heartbeat as _hb

__all__ = [
    "LOSSLESS",
    "LOSSY",
    "MAX_READERS",
    "MIN_SLOTS",
    "SLOT_FASTFWD",
    "SLOT_REPLAY",
    "SLOT_RESET",
    "Frame",
    "LayoutMismatch",
    "LocalRing",
    "PlatformUnsupported",
    "PublishTicket",
    "RingHeader",
    "RingNotReady",
    "StateRing",
    "file_bytes",
    "slot_bytes",
]

# ---------------------------------------------------------------- 布局常量（全部取自生成的契约）
MODE_FREE, LOSSY, LOSSLESS = 0, 1, 2
MAGIC: int = IPC_RING_HEADER_CONST["magic"]  # 0x31525741，"AWR1"
VERSION: int = IPC_RING_HEADER_CONST["version"]
HDR_BYTES = IPC_RING_HEADER.itemsize  # 256
CUR_BYTES = IPC_READER_CURSOR.itemsize  # 32
MAX_READERS = 8
SLOTS_OFF = HDR_BYTES + MAX_READERS * CUR_BYTES  # 512
SLOT_HDR_BYTES = IPC_SLOT_HEADER.itemsize  # 64
ROW_BYTES = DRONE_STATE64.itemsize + SWARM_LITE32.itemsize  # 96
DEFAULT_SLOTS = 32
MIN_SLOTS = 16
DEFAULT_CAPACITY = 1024
RING_FILE_MODE = 0o600

SLOT_RESET = 1 << IPC_SLOT_HEADER_BITS["flags"]["reset"][0]
SLOT_FASTFWD = 1 << IPC_SLOT_HEADER_BITS["flags"]["fastfwd"][0]
SLOT_REPLAY = 1 << IPC_SLOT_HEADER_BITS["flags"]["replay"][0]
RING_FLAG_REPLAY = 1  # 头部 flags bit0：REPLAY 源

LOSSLESS_WAIT_S = 0.25  # 无损读者卡住超过 250 ms 降级为 LOSSY（17 §9.1）
LOSSLESS_STALE_NS = 2_000_000_000  # 无损读者心跳超过 2 s 视为失联
HEADER_RETRIES = 3

_SLOT = STRUCTS["awr.ipc.SlotHeader.v1"]
_RHDR = STRUCTS["awr.ipc.RingHeader.v1"]
assert _SLOT.size == SLOT_HDR_BYTES and _RHDR.size == HDR_BYTES

_HO = IPC_RING_HEADER_OFFSETS
_CO = IPC_READER_CURSOR_OFFSETS
_I64 = {k: _HO[k] // 8 for k in ("head", "heartbeat_ns", "t_sim_ns", "created_ns", "step_seq", "clock_seq")}
_I32 = {k: _HO[k] // 4 for k in ("slot_count", "slot_bytes", "capacity", "layout_id", "writer_pid", "epoch", "rate_milli",
                                 "roster_version", "segment", "step_budget_us", "step_p99_us", "rtf_milli", "id_base",
                                 "id_count", "step_max_us", "step_p50_us", "catchup_saturated")}
_I16 = {k: _HO[k] // 2 for k in ("version", "flags")}
for _k, _v in _HO.items():  # 对齐自检：上面按字宽取下标，偏移必须整除
    if _k in _I64:
        assert _v % 8 == 0
    elif _k in _I32:
        assert _v % 4 == 0


def _struct_index() -> dict[str, int]:
    """IPC_RING_HEADER 字段名 -> 生成的 RingHeader 格式串 unpack 结果中的下标（子数组按元素展开）。"""
    idx: dict[str, int] = {}
    i = 0
    for name in IPC_RING_HEADER.names:
        sub = IPC_RING_HEADER.fields[name][0]
        idx[name] = i
        i += int(np.prod(sub.shape)) if sub.shape else 1
    return idx


_RIDX = _struct_index()


def _ceil64(n: int) -> int:
    return (n + 63) // 64 * 64


def slot_bytes(capacity: int) -> int:
    return _ceil64(SLOT_HDR_BYTES + capacity * ROW_BYTES)


def file_bytes(capacity: int = DEFAULT_CAPACITY, slots: int = DEFAULT_SLOTS) -> int:
    return SLOTS_OFF + slots * slot_bytes(capacity)


def _now_ns() -> int:
    return time.monotonic_ns()  # CLOCK_MONOTONIC 全机一致，可跨进程比较


def _machine() -> str:
    return platform.machine().lower()


def _check_platform() -> None:
    if _machine() not in ("x86_64", "amd64"):
        raise PlatformUnsupported(f"StateRing 只支持 x86-64（TSO）；当前架构 {platform.machine()}；V0.5 起改用 ZenohStateBus")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _name_hash(name: str) -> int:
    return zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF


# ---------------------------------------------------------------- 异常与值类型
class LayoutMismatch(Exception):
    """环文件的布局（layout_id、版本、容量推导出的尺寸）与读者期望不一致（原因码 312）。"""

    code = 312


class PlatformUnsupported(Exception):
    """非 x86-64 平台（seqlock 依赖 TSO，ADR-018）。"""


class RingNotReady(Exception):
    """环文件不存在或尚未发布（magic 未写入）。"""


class RingHeader(NamedTuple):
    """17 §9.2（v1.1）头部字段；时钟组来自同一次 clock_seq 一致性读。"""

    version: int
    flags: int
    slot_count: int
    capacity: int
    layout_id: int
    writer_pid: int
    epoch: int
    segment: int
    head: int
    heartbeat_ns: int
    t_sim_ns: int
    rate_milli: int
    clock_state: int
    roster_version: int
    created_ns: int
    step_seq: int
    step_budget_us: int
    step_p50_us: int
    step_p99_us: int
    step_max_us: int
    rtf_milli: int
    catchup_saturated: int
    id_base: int
    id_count: int

    @classmethod
    def placeholder(cls, *, heartbeat_ns: int) -> RingHeader:
        """无生产者时 TIME 使用的占位头部（M11-FR-052）：STOPPED、倍率 1、t_sim 0。"""
        return cls(VERSION, 0, 0, 0, 0, 0, 0, 0, 0, heartbeat_ns, 0, 1000, int(TimeState.STOPPED), 0, 0, 0, 0, 0, 0, 0,
                   0, 0, 0, 0)

    @property
    def rate(self) -> float:
        return self.rate_milli / 1000.0


class Frame(NamedTuple):
    frame_seq: int
    t_sim_ns: int
    t_pub_ns: int
    epoch: int
    roster_version: int
    n_rows: int
    flags: int
    full: bytes  # 已拷贝，可长期持有
    lite: bytes


class PublishTicket(NamedTuple):
    frame_seq: int
    slot: int


# ---------------------------------------------------------------- StateRing
class StateRing:
    """单写者、多读者的状态环。写者方法只能在单一线程调用；每个读者句柄只属于一个线程。"""

    _is_local: ClassVar[bool] = False

    def __init__(self, path: Path, mm: mmap.mmap, fd: int, ino: int) -> None:
        self.path = Path(path)
        self._mm = mm
        self._fd = fd
        self._ino = ino
        u32 = np.ndarray((HDR_BYTES // 4,), "<u4", buffer=mm)
        self.slots = int(u32[_I32["slot_count"]])
        self.capacity = int(u32[_I32["capacity"]])
        self.slot_bytes = int(u32[_I32["slot_bytes"]])
        self._u64 = np.ndarray((HDR_BYTES // 8,), "<u8", buffer=mm)
        self._i64 = np.ndarray((HDR_BYTES // 8,), "<i8", buffer=mm)
        self._u32 = u32
        self._i32 = np.ndarray((HDR_BYTES // 4,), "<i4", buffer=mm)
        self._u16 = np.ndarray((HDR_BYTES // 2,), "<u2", buffer=mm)
        self._u8 = np.ndarray((HDR_BYTES,), "u1", buffer=mm)
        self._cur = np.ndarray((MAX_READERS,), IPC_READER_CURSOR, buffer=mm, offset=HDR_BYTES)
        self._cur_mode = np.ndarray((MAX_READERS,), "<u4", buffer=mm, offset=HDR_BYTES + _CO["mode"], strides=(CUR_BYTES,))
        self._lock = np.ndarray((self.slots,), "<u8", buffer=mm, offset=SLOTS_OFF, strides=(self.slot_bytes,))
        cap, S = self.capacity, self.slot_bytes
        self._full = [np.ndarray((cap,), DRONE_STATE64, buffer=mm, offset=SLOTS_OFF + i * S + SLOT_HDR_BYTES)
                      for i in range(self.slots)]
        self._lite = [np.ndarray((cap,), SWARM_LITE32, buffer=mm, offset=SLOTS_OFF + i * S + SLOT_HDR_BYTES + cap * 64)
                      for i in range(self.slots)]
        self._ticket: PublishTicket | None = None
        self._reader_idx: int | None = None
        self._last_header: RingHeader | None = None
        self.header_retries = 0
        self.lossless_demotions = 0
        self.torn_reads = 0
        self._closed = False

    # ------------------------------------------------------------ 生命周期
    @classmethod
    def create(cls, path: Path, *, capacity: int = DEFAULT_CAPACITY, slots: int = DEFAULT_SLOTS, layout_id: int,
               epoch: int = 1, segment: int = 0, id_base: int = 0, id_count: int = 1024, flags: int = 0) -> StateRing:
        """新建环文件：写 tmp 文件、初始化头部、magic 最后写入、os.replace 原子发布（M11-FR-001）。"""
        _check_platform()
        cls._validate_dims(capacity, slots)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        size = file_bytes(capacity, slots)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_TRUNC, RING_FILE_MODE)
        try:
            os.ftruncate(fd, size)
            with contextlib.suppress(OSError):
                os.posix_fallocate(fd, 0, size)  # 预先分配 tmpfs 页，避免首轮 publish 的缺页抖动
            mm = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            _init_header(mm, capacity, slots, layout_id, epoch, segment, id_base, id_count, flags)
            os.replace(tmp, path)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return cls(path, mm, fd, os.fstat(fd).st_ino)

    @classmethod
    def open_or_create(cls, path: Path, *, capacity: int = DEFAULT_CAPACITY, slots: int = DEFAULT_SLOTS, layout_id: int,
                       id_base: int = 0, id_count: int = 1024, flags: int = 0) -> tuple[StateRing, bool]:
        """兼容（capacity、slots、layout_id、版本一致）则复用原文件，head 继续递增，读者 mmap 不失效；否则替换（M11-FR-004）。

        返回 (ring, reused)。复用时写入本进程 pid 与 id 区段；epoch、segment 保持原值，由调用方按 17 §9.2 规则推进。
        """
        _check_platform()
        path = Path(path)
        try:
            ring = cls.attach(path, expect_layout_id=layout_id)
        except (RingNotReady, LayoutMismatch, FileNotFoundError, ValueError):
            ring = None
        if ring is not None and ring.capacity == capacity and ring.slots == slots:
            ring._u32[_I32["writer_pid"]] = os.getpid()
            ring._u32[_I32["id_base"]] = id_base
            ring._u32[_I32["id_count"]] = id_count
            ring._u16[_I16["flags"]] = flags
            return ring, True
        if ring is not None:
            ring.close()
        return cls.create(path, capacity=capacity, slots=slots, layout_id=layout_id, id_base=id_base, id_count=id_count,
                          flags=flags), False

    @classmethod
    def attach(cls, path: Path, *, expect_layout_id: int) -> StateRing:
        """以读者身份映射已发布的环；layout_id、版本或尺寸不一致抛 LayoutMismatch（312）。"""
        _check_platform()
        path = Path(path)
        try:
            fd = os.open(path, os.O_RDWR)  # 读者需要写自己的游标行
        except FileNotFoundError as e:
            raise RingNotReady(f"环文件不存在：{path}") from e
        try:
            st = os.fstat(fd)
            if st.st_size < SLOTS_OFF:
                raise RingNotReady(f"环文件过小：{path}（{st.st_size} B）")
            mm = mmap.mmap(fd, st.st_size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            _validate_mapped(mm, st.st_size, expect_layout_id, path)
        except BaseException:
            os.close(fd)
            raise
        return cls(path, mm, fd, st.st_ino)

    @staticmethod
    def _validate_dims(capacity: int, slots: int) -> None:
        if not 1 <= capacity <= 65536:
            raise ValueError(f"capacity 超出范围：{capacity}")
        if slots < MIN_SLOTS:
            raise ValueError(f"slots 不得小于 {MIN_SLOTS}（ADR-018）：{slots}")

    def close(self) -> None:
        """释放读者游标并解除映射（写者关闭不删除文件；删除由 supervisor 清理运行目录完成）。"""
        if self._closed:
            return
        self._closed = True
        if self._reader_idx is not None:
            with contextlib.suppress(ValueError, TypeError):
                self._cur[self._reader_idx]["mode"] = MODE_FREE
            self._reader_idx = None
        for name in ("_u64", "_i64", "_u32", "_i32", "_u16", "_u8", "_cur", "_cur_mode", "_lock", "_full", "_lite"):
            self.__dict__.pop(name, None)
        with contextlib.suppress(BufferError):  # 调用方仍持有 begin_publish 返回的视图时，映射随对象回收释放
            self._mm.close()
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> StateRing:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ 写者
    def heartbeat(self, t_sim_ns: int, clock_state: int, rate_milli: int = 1000, *, step_seq: int | None = None) -> None:
        """主循环每次迭代调用（暂停时 20 Hz 空转照写），禁止后台线程代写；时钟组以 clock_seq 包围（17 §9.2 v1.1）。"""
        if _hb.suppressed():
            return
        u64, i64 = self._u64, self._i64
        c = int(u64[_I64["clock_seq"]])
        u64[_I64["clock_seq"]] = c + 1 if not c & 1 else c + 2  # 奇数：时钟组正在写
        i64[_I64["t_sim_ns"]] = t_sim_ns
        self._u8[_HO["clock_state"]] = clock_state & 0xFF
        self._i32[_I32["rate_milli"]] = rate_milli
        i64[_I64["heartbeat_ns"]] = _now_ns()
        u64[_I64["clock_seq"]] = int(u64[_I64["clock_seq"]]) + 1  # 偶数：已提交
        if step_seq is not None:
            u64[_I64["step_seq"]] = step_seq

    def set_epoch(self, epoch: int) -> None:
        self._clock_group_write(_I32["epoch"], epoch)

    def set_segment(self, segment: int) -> None:
        self._clock_group_write(_I32["segment"], segment)

    def _clock_group_write(self, i32_index: int, value: int) -> None:
        u64 = self._u64
        c = int(u64[_I64["clock_seq"]])
        u64[_I64["clock_seq"]] = c + 1 if not c & 1 else c + 2
        self._u32[i32_index] = value & 0xFFFFFFFF
        u64[_I64["clock_seq"]] = int(u64[_I64["clock_seq"]]) + 1

    def set_step_stats(self, p50_us: int, p99_us: int, max_us: int, budget_us: int, rtf_milli: int,
                       catchup_saturated: int) -> None:
        """1 Hz 写入自报步长统计（17 §9.2）。"""
        u32 = self._u32
        u32[_I32["step_p50_us"]] = min(int(p50_us), 0xFFFFFFFF)
        u32[_I32["step_p99_us"]] = min(int(p99_us), 0xFFFFFFFF)
        u32[_I32["step_max_us"]] = min(int(max_us), 0xFFFFFFFF)
        u32[_I32["step_budget_us"]] = min(int(budget_us), 0xFFFFFFFF)
        u32[_I32["rtf_milli"]] = min(int(rtf_milli), 0xFFFFFFFF)
        u32[_I32["catchup_saturated"]] = min(int(catchup_saturated), 0xFFFFFFFF)

    def publish(self, full: np.ndarray, lite: np.ndarray, t_sim_ns: int, roster_version: int, flags: int = 0) -> int:
        """拷贝 Full64 与 Lite32 行到下一槽并提交，返回 frame_seq。

        通常两者行数相同（n 行）；回放复合帧（M12 §6.7.5）的 Full64 只含 m ≤ n 行（行首 agent_no 自描述），
        此时 `full_len = m × 64`、`lite_len = n × 32`。
        """
        n = len(lite)
        m = len(full)
        if n > self.capacity or m > n:
            raise ValueError(f"行数不合法：full={m} lite={n} capacity={self.capacity}")
        fv, lv, ticket = self.begin_publish()
        try:
            fv[:m] = full
            lv[:n] = lite
        except BaseException:
            self._abort(ticket)
            raise
        return self.commit_publish(ticket, n, t_sim_ns, roster_version, flags, full_rows=m)

    def begin_publish(self) -> tuple[np.ndarray, np.ndarray, PublishTicket]:
        """零拷贝写入：返回下一槽的 Full64[cap]、Lite32[cap] 视图（槽锁已置奇数），写完调用 commit_publish。"""
        if self._ticket is not None:
            raise RuntimeError("上一次 begin_publish 尚未提交")
        fs = int(self._u64[_I64["head"]]) + 1
        i = fs % self.slots
        self._wait_lossless(fs)
        self._lock[i] = 2 * fs - 1  # 奇数：正在写（8 字节对齐单次存储）
        t = PublishTicket(fs, i)
        self._ticket = t
        return self._full[i], self._lite[i], t

    def commit_publish(self, ticket: PublishTicket, n_rows: int, t_sim_ns: int, roster_version: int, flags: int = 0, *,
                       full_rows: int | None = None) -> int:
        """提交槽；`full_rows`（仅关键字，缺省 = n_rows）小于 n_rows 时为回放复合帧（M12 §6.7.5）。"""
        if ticket != self._ticket:
            raise RuntimeError("PublishTicket 与当前写入不匹配")
        m = n_rows if full_rows is None else int(full_rows)
        if not 0 <= n_rows <= self.capacity or not 0 <= m <= n_rows:
            self._abort(ticket)
            raise ValueError(f"n_rows 超出容量：{n_rows}（full_rows={m}）")
        fs, i = ticket
        cap = self.capacity
        off = SLOTS_OFF + i * self.slot_bytes
        _SLOT.pack_into(self._mm, off, 2 * fs - 1, fs, t_sim_ns, _now_ns(), n_rows, int(self._u32[_I32["epoch"]]),
                        roster_version & 0xFFFFFFFF, flags & 0xFFFFFFFF, SLOT_HDR_BYTES, m * 64,
                        SLOT_HDR_BYTES + cap * 64, n_rows * 32)
        self._lock[i] = 2 * fs  # 偶数：已提交
        self._u64[_I64["head"]] = fs
        self._u32[_I32["roster_version"]] = roster_version & 0xFFFFFFFF
        self._ticket = None
        return fs

    def _abort(self, ticket: PublishTicket) -> None:
        """放弃写入：槽锁回退为上一代偶数值之外的奇数不可读状态，head 不前进。"""
        self._ticket = None
        self._lock[ticket.slot] = 2 * ticket.frame_seq - 1

    def _wait_lossless(self, fs: int) -> None:
        """无损读者反压：不覆盖其尚未消费的槽；卡住 > 250 ms 或失联 > 2 s 的无损读者降级为 LOSSY。"""
        oldest_allowed = fs - self.slots
        if oldest_allowed <= 0 or LOSSLESS not in self._cur_mode:
            return
        cur = self._cur
        for r in range(MAX_READERS):
            if int(cur[r]["mode"]) != LOSSLESS:
                continue
            deadline = None
            while int(cur[r]["mode"]) == LOSSLESS and int(cur[r]["cursor"]) < oldest_allowed:
                now = time.perf_counter()
                if deadline is None:
                    deadline = now + LOSSLESS_WAIT_S
                stale = _now_ns() - int(cur[r]["heartbeat_ns"]) > LOSSLESS_STALE_NS
                if now > deadline or stale or not _pid_alive(int(cur[r]["pid"])):
                    cur[r]["mode"] = LOSSY
                    self.lossless_demotions += 1
                    break
                time.sleep(0.0002)

    # ------------------------------------------------------------ 读者
    def register(self, mode: int = LOSSY, name: str = "") -> int:
        """登记读者游标（8 行）：优先复用同 pid 同名的行，其次空行或进程已退出的行。"""
        if mode not in (LOSSY, LOSSLESS):
            raise ValueError(f"mode 只能是 LOSSY 或 LOSSLESS：{mode}")
        pid, nh = os.getpid(), _name_hash(name)
        with self._registry_lock():
            cur = self._cur
            chosen = None
            for r in range(MAX_READERS):
                if int(cur[r]["mode"]) != MODE_FREE and int(cur[r]["pid"]) == pid and int(cur[r]["name_hash"]) == nh:
                    chosen = r
                    break
            if chosen is None:
                for r in range(MAX_READERS):
                    m, p = int(cur[r]["mode"]), int(cur[r]["pid"])
                    if m == MODE_FREE or not self._reader_alive(p):
                        chosen = r
                        break
            if chosen is None:
                raise RuntimeError("StateRing 没有空闲的读者游标（8 个）")
            row = cur[chosen]
            row["pid"] = pid
            row["name_hash"] = nh
            row["cursor"] = int(self._u64[_I64["head"]])
            row["heartbeat_ns"] = _now_ns()
            row["mode"] = mode
        self._reader_idx = chosen
        return chosen

    def _reader_alive(self, pid: int) -> bool:
        return _pid_alive(pid)

    def _registry_lock(self):
        return _FileLock(self._fd)

    def unregister(self) -> None:
        if self._reader_idx is not None:
            self._cur[self._reader_idx]["mode"] = MODE_FREE
            self._reader_idx = None

    @property
    def reader_index(self) -> int | None:
        return self._reader_idx

    def read_latest(self, last_seq: int = 0) -> Frame | None:
        """最新帧（已拷贝）；与 last_seq 相同或尚无帧时返回 None；撕裂重试 ≤ 3 次。"""
        for _ in range(1 + HEADER_RETRIES):
            h = int(self._u64[_I64["head"]])
            if h == 0 or h == last_seq:
                return None
            f = self._read_slot(h)
            if f is not None:
                if self._reader_idx is not None:
                    row = self._cur[self._reader_idx]
                    row["cursor"] = h
                    row["heartbeat_ns"] = _now_ns()
                return f
            self.torn_reads += 1
        return None

    def drain(self) -> tuple[list[Frame], int]:
        """无损式消费（recorder）：返回游标之后的全部帧与 overrun（被覆盖或撕裂而丢失的帧数）。需先 register。"""
        if self._reader_idx is None:
            raise RuntimeError("drain() 需要先 register()")
        row = self._cur[self._reader_idx]
        cur, h = int(row["cursor"]), int(self._u64[_I64["head"]])
        out: list[Frame] = []
        start = max(cur + 1, h - self.slots + 1, 1)
        lost = max(0, start - (cur + 1))
        for fs in range(start, h + 1):
            f = self._read_slot(fs)
            if f is None:
                lost += 1
            else:
                out.append(f)
        row["cursor"] = h
        row["heartbeat_ns"] = _now_ns()
        return out, lost

    def _read_slot(self, fs: int) -> Frame | None:
        i = fs % self.slots
        lock = self._lock
        s1 = int(lock[i])
        if s1 != 2 * fs:
            return None
        off = SLOTS_OFF + i * self.slot_bytes
        _lk, fseq, t_sim, t_pub, n, ep, rv, fl, full_off, full_len, lite_off, lite_len = _SLOT.unpack_from(self._mm, off)
        S = self.slot_bytes
        if fseq != fs or n > self.capacity or full_off + full_len > S or lite_off + lite_len > S:
            return None
        mm = self._mm
        full = mm[off + full_off:off + full_off + full_len]
        lite = mm[off + lite_off:off + lite_off + lite_len]
        if int(lock[i]) != s1:
            return None  # 撕裂：拷贝期间写者已覆盖该槽
        return Frame(fs, t_sim, t_pub, ep, rv, n, fl, full, lite)

    def header(self) -> RingHeader:
        """头部一致性读：clock_seq 为奇数或前后不等即重试（≤ 3 次），仍失败沿用上一次读数并计 header_retries。"""
        u64 = self._u64
        ci = _I64["clock_seq"]
        buf = None
        for _ in range(1 + HEADER_RETRIES):
            c1 = int(u64[ci])
            if c1 & 1:
                continue
            buf = self._mm[0:HDR_BYTES]
            if int(u64[ci]) != c1:
                continue
            h = _decode_header(buf)
            self._last_header = h
            return h
        self.header_retries += 1
        if self._last_header is not None:
            return self._last_header
        return _decode_header(buf if buf is not None else self._mm[0:HDR_BYTES])

    def identity(self) -> tuple[int, int, int, int]:
        """(路径当前指向的 st_ino, writer_pid, epoch, segment)；读者每 1 s 比较，inode 或 pid 变化即重新 attach。"""
        try:
            ino = os.stat(self.path).st_ino
        except FileNotFoundError:
            ino = 0
        h = self.header()
        return ino, h.writer_pid, h.epoch, h.segment

    @property
    def mapped_ino(self) -> int:
        """本句柄映射的文件 inode；与 identity()[0] 不同说明文件已被替换。"""
        return self._ino

    def writer_age_ms(self) -> float:
        hb = int(self._i64[_I64["heartbeat_ns"]])
        if hb <= 0:
            return float("inf")
        return (_now_ns() - hb) / 1e6

    @property
    def head(self) -> int:
        return int(self._u64[_I64["head"]])

    def reader_cursors(self) -> list[dict]:
        """游标表快照（awr ring dump）。"""
        return [{"idx": r, "pid": int(c["pid"]), "mode": int(c["mode"]), "cursor": int(c["cursor"]),
                 "heartbeat_ns": int(c["heartbeat_ns"]), "name_hash": int(c["name_hash"])}
                for r, c in enumerate(self._cur)]


class _FileLock:
    """读者登记时对环文件加 flock，串行化并发登记（写者不取此锁）。"""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def __enter__(self) -> None:
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self.fd, fcntl.LOCK_UN)


def _init_header(mm, capacity: int, slots: int, layout_id: int, epoch: int, segment: int, id_base: int, id_count: int,
                 flags: int) -> None:
    u32 = np.ndarray((HDR_BYTES // 4,), "<u4", buffer=mm)
    u16 = np.ndarray((HDR_BYTES // 2,), "<u2", buffer=mm)
    i64 = np.ndarray((HDR_BYTES // 8,), "<i8", buffer=mm)
    i32 = np.ndarray((HDR_BYTES // 4,), "<i4", buffer=mm)
    u16[_I16["version"]] = VERSION
    u16[_I16["flags"]] = flags
    u32[_I32["slot_count"]] = slots
    u32[_I32["slot_bytes"]] = slot_bytes(capacity)
    u32[_I32["capacity"]] = capacity
    u32[_I32["layout_id"]] = layout_id & 0xFFFFFFFF
    u32[_I32["writer_pid"]] = os.getpid()
    u32[_I32["epoch"]] = epoch
    u32[_I32["segment"]] = segment
    u32[_I32["id_base"]] = id_base
    u32[_I32["id_count"]] = id_count
    i32[_I32["rate_milli"]] = 1000
    i64[_I64["created_ns"]] = _now_ns()
    del u16, i64, i32
    u32[_HO["magic"] // 4] = MAGIC  # magic 最后写入：读者把没有 magic 的文件视为未就绪
    del u32


def _validate_mapped(mm, size: int, expect_layout_id: int, path: Path) -> None:
    buf = mm[0:HDR_BYTES]
    v = _RHDR.unpack_from(buf)
    magic, version = v[_RIDX["magic"]], v[_RIDX["version"]]
    if magic != MAGIC:
        raise RingNotReady(f"环文件未就绪或不是 StateRing：{path}（magic=0x{magic:08x}）")
    slots, sb, cap, lid = v[_RIDX["slot_count"]], v[_RIDX["slot_bytes"]], v[_RIDX["capacity"]], v[_RIDX["layout_id"]]
    if version != VERSION:
        raise LayoutMismatch(f"StateRing 版本不符：{path} version={version}，期望 {VERSION}")
    if cap == 0 or slots == 0 or sb != slot_bytes(cap) or size != file_bytes(cap, slots):
        raise LayoutMismatch(f"StateRing 尺寸与布局不符（原型文件不得复用）：{path} size={size} cap={cap} slots={slots}")
    if lid != (expect_layout_id & 0xFFFFFFFF):
        raise LayoutMismatch(f"layout_id 不一致：{path} 0x{lid:08X}，期望 0x{expect_layout_id & 0xFFFFFFFF:08X}")


def _decode_header(buf: bytes) -> RingHeader:
    v = _RHDR.unpack_from(buf)
    g = _RIDX
    return RingHeader(
        v[g["version"]], v[g["flags"]], v[g["slot_count"]], v[g["capacity"]], v[g["layout_id"]], v[g["writer_pid"]],
        v[g["epoch"]], v[g["segment"]], v[g["head"]], v[g["heartbeat_ns"]], v[g["t_sim_ns"]], v[g["rate_milli"]],
        v[g["clock_state"]], v[g["roster_version"]], v[g["created_ns"]], v[g["step_seq"]], v[g["step_budget_us"]],
        v[g["step_p50_us"]], v[g["step_p99_us"]], v[g["step_max_us"]], v[g["rtf_milli"]], v[g["catchup_saturated"]],
        v[g["id_base"]], v[g["id_count"]])


# ---------------------------------------------------------------- LocalRing（--inproc 与单元测试）
class _LocalEntry:
    __slots__ = ("buf", "ino", "lock")

    def __init__(self, buf: mmap.mmap, ino: int) -> None:
        self.buf = buf
        self.ino = ino
        self.lock = threading.Lock()


class LocalRing(StateRing):
    """与 StateRing 同接口的进程内实现：缓冲为匿名映射，按路径名登记在进程内注册表（M11-FR-005）。

    不参与性能验收，也不是前端早期数据源（17 §9.8）。不做平台检查（同一进程内不依赖跨进程存储顺序）。
    """

    _is_local = True
    _registry: ClassVar[dict[str, _LocalEntry]] = {}
    _reg_lock: ClassVar[threading.Lock] = threading.Lock()
    _ino_seq = itertools.count(1)

    def __init__(self, path: Path, entry: _LocalEntry) -> None:
        self._entry = entry
        super().__init__(path, entry.buf, -1, entry.ino)

    @classmethod
    def create(cls, path: Path, *, capacity: int = DEFAULT_CAPACITY, slots: int = DEFAULT_SLOTS, layout_id: int,
               epoch: int = 1, segment: int = 0, id_base: int = 0, id_count: int = 1024, flags: int = 0) -> LocalRing:
        cls._validate_dims(capacity, slots)
        size = file_bytes(capacity, slots)
        buf = mmap.mmap(-1, size)
        _init_header(buf, capacity, slots, layout_id, epoch, segment, id_base, id_count, flags)
        entry = _LocalEntry(buf, next(cls._ino_seq))
        with cls._reg_lock:
            cls._registry[str(path)] = entry
        return cls(Path(path), entry)

    @classmethod
    def open_or_create(cls, path: Path, *, capacity: int = DEFAULT_CAPACITY, slots: int = DEFAULT_SLOTS, layout_id: int,
                       id_base: int = 0, id_count: int = 1024, flags: int = 0) -> tuple[LocalRing, bool]:
        try:
            ring = cls.attach(path, expect_layout_id=layout_id)
        except (RingNotReady, LayoutMismatch):
            ring = None
        if ring is not None and ring.capacity == capacity and ring.slots == slots:
            ring._u32[_I32["writer_pid"]] = os.getpid()
            ring._u32[_I32["id_base"]] = id_base
            ring._u32[_I32["id_count"]] = id_count
            ring._u16[_I16["flags"]] = flags
            return ring, True
        return cls.create(path, capacity=capacity, slots=slots, layout_id=layout_id, id_base=id_base, id_count=id_count,
                          flags=flags), False

    @classmethod
    def attach(cls, path: Path, *, expect_layout_id: int) -> LocalRing:
        with cls._reg_lock:
            entry = cls._registry.get(str(path))
        if entry is None:
            raise RingNotReady(f"LocalRing 不存在：{path}")
        _validate_mapped(entry.buf, len(entry.buf), expect_layout_id, Path(path))
        return cls(Path(path), entry)

    @classmethod
    def remove(cls, path: Path) -> None:
        with cls._reg_lock:
            cls._registry.pop(str(path), None)

    def identity(self) -> tuple[int, int, int, int]:
        with self._reg_lock:
            entry = self._registry.get(str(self.path))
        h = self.header()
        return (entry.ino if entry is not None else 0), h.writer_pid, h.epoch, h.segment

    def _registry_lock(self):
        return self._entry.lock

    def _reader_alive(self, pid: int) -> bool:
        return pid == os.getpid() or _pid_alive(pid)

    def register(self, mode: int = LOSSY, name: str = "") -> int:
        # 同一进程内多个读者句柄共用 pid：按句柄区分，名字冲突时附加句柄 id
        return super().register(mode, f"{name}#{id(self)}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_idx is not None:
            self._cur[self._reader_idx]["mode"] = MODE_FREE
            self._reader_idx = None
        for name in ("_u64", "_i64", "_u32", "_i32", "_u16", "_u8", "_cur", "_cur_mode", "_lock", "_full", "_lite"):
            self.__dict__.pop(name, None)
        # 匿名映射由注册表持有，随 LocalRing.remove() 与回收释放
