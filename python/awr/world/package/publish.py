"""构建锁、staging、`renameat2(RENAME_EXCHANGE)` 原子发布、`.status` 与 `.trash`（AWR-16 §3.5；M03 §6.9、FR-042、FR-043）。

任意时刻 `worlds/<id>/` 要么是完整的旧包，要么是完整的新包；`renameat2` 不可用时退化为两次 rename，
中间窗口由启动恢复补齐（持锁后执行）。
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import fcntl
import json
import os
import shutil
import time
from pathlib import Path

from ..ingest.types import LockBusy
from .version import VALIDATOR_VERSION

AT_FDCWD = -100
RENAME_EXCHANGE = 2
TRASH_KEEP_S = 60.0
STATUS_SCHEMA = "awr.world.status.v1"

_libc = None


def _libc_handle():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    return _libc


def renameat2_exchange(a: Path, b: Path) -> bool:
    """原子交换两个目录；返回 False 表示内核或文件系统不支持（调用方退化为两次 rename）。"""
    try:
        fn = _libc_handle().renameat2
    except (AttributeError, OSError):
        return False
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    r = fn(AT_FDCWD, os.fsencode(str(a)), AT_FDCWD, os.fsencode(str(b)), RENAME_EXCHANGE)
    if r == 0:
        return True
    e = ctypes.get_errno()
    if e in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
        return False
    raise OSError(e, os.strerror(e), str(a))


def fsync_dir(p: Path) -> None:
    fd = os.open(str(p), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root: Path) -> None:
    for dp, _dns, fns in os.walk(root):
        for fn in fns:
            fd = os.open(os.path.join(dp, fn), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fsync_dir(Path(dp))


class Publisher:
    def __init__(self, worlds_dir: Path, world_id: str):
        self.worlds = Path(worlds_dir)
        self.wid = world_id
        for d in (".locks", ".staging", ".trash", ".status"):
            (self.worlds / d).mkdir(parents=True, exist_ok=True)
        self._lock_fd: int | None = None

    # ---- 锁
    @contextlib.contextmanager
    def lock(self):
        p = self.worlds / ".locks" / f"{self.wid}.lock"
        fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(fd)
            raise LockBusy(f"{self.wid}: 另一个构建正在进行（{p}）") from e
        self._lock_fd = fd
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            self._lock_fd = None

    def is_locked(self) -> bool:
        """只读探测：锁被其他进程持有时为 True（catalog 用于判定 building）。"""
        p = self.worlds / ".locks" / f"{self.wid}.lock"
        if not p.exists():
            return False
        fd = os.open(str(p), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)

    # ---- staging
    def new_staging(self, nonce: str | None = None) -> Path:
        nonce = nonce or f"{os.getpid():x}{time.monotonic_ns() & 0xFFFFFF:06x}"
        stg = self.worlds / ".staging" / f"{self.wid}-{nonce}"
        if stg.exists():
            shutil.rmtree(stg)
        stg.mkdir(parents=True)
        return stg

    def recover(self, shallow_ok=None) -> list[str]:
        """启动恢复（持锁后）：包缺失且 `.trash` 最新一代浅校验通过时改名回来；删除本城残留 staging。"""
        actions = []
        dst = self.worlds / self.wid
        trash = sorted((self.worlds / ".trash").glob(f"{self.wid}-*"), key=lambda p: p.name)
        if not dst.exists() and trash:
            cand = trash[-1]
            if shallow_ok is None or shallow_ok(cand):
                os.rename(cand, dst)
                actions.append(f"restored {cand.name}")
        for stg in (self.worlds / ".staging").glob(f"{self.wid}-*"):
            if stg.name.startswith(f"{self.wid}-j-"):
                continue                          # job 模式：续跑用（D1-ext）
            shutil.rmtree(stg, ignore_errors=True)
            actions.append(f"removed {stg.name}")
        return actions

    def publish(self, stg: Path) -> None:
        fsync_tree(stg)
        dst = self.worlds / self.wid
        ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"{time.time_ns() % 1_000_000_000:09d}"
        trash = self.worlds / ".trash" / f"{self.wid}-{ts}"
        if dst.exists():
            if renameat2_exchange(stg, dst):
                os.rename(stg, trash)             # 交换后 stg 指向旧包
            else:
                os.rename(dst, trash)
                os.rename(stg, dst)
        else:
            os.rename(stg, dst)
        fsync_dir(self.worlds)
        self.gc_trash()

    def gc_trash(self, older_than_s: float = TRASH_KEEP_S) -> int:
        now = time.time()
        n = 0
        for p in (self.worlds / ".trash").iterdir():
            try:
                if now - p.stat().st_mtime > older_than_s:
                    shutil.rmtree(p, ignore_errors=True)
                    n += 1
            except OSError:
                continue
        return n

    # ---- 状态缓存
    def status_path(self) -> Path:
        return self.worlds / ".status" / f"{self.wid}.json"

    def read_status(self) -> dict | None:
        try:
            return json.loads(self.status_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def write_status(self, status: str, *, reason: str | None, exit_code: int, content_version: str | None,
                     deep: bool, raw: list[dict] | None = None) -> dict:
        doc = {"schema": STATUS_SCHEMA, "world_id": self.wid, "status": status, "reason": reason, "exit_code": int(exit_code),
               "content_version": content_version, "checked_unix_ns": time.time_ns(), "validator": VALIDATOR_VERSION,
               "deep": bool(deep)}
        if raw is not None:
            doc["raw"] = raw
        p = self.status_path()
        tmp = p.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, p)
        return doc
