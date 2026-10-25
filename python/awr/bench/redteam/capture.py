"""下发帧录制（M26 §8.3；D3-AC-16）：录下宿主发往 Agent 的每一帧的线上字节（CONFIG、OBS 含 inbox、ERROR、CLOSE）。

录制点与白名单审计同一处：`SocketAgentHost._send` 先编码、再交 `ObsAudit.check_body` 按线上字节校验、通过后才发送。
`CaptureAudit` 是 `ObsAudit` 的子类，校验通过（即该帧将被发送）后记录帧体；校验失败即关闭时帧不发送，也不记录。
经 `audit_scope(CaptureAudit(...))` 绑定后，套接字宿主与容器宿主（`run_container_episode`）的全部 E2A 帧都被录下，
不改变下发内容与时刻。

落盘格式（`out_dir/<uav>.wire`）：按发送顺序拼接的 wire.v1 帧（`body_len u32le | type u8 | flags u8 | version u16le | body`），
与套接字上的字节逐字节相同；`read_capture` 读回。录制文件含 Agent 收到的全部内容，属于运行的 Agent 层，不含任何真值。
"""

from __future__ import annotations

import struct
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from awr.agentsdk.env.obs_audit import KINDS, ObsAudit
from awr.agentsdk.wire import decode_body

__all__ = ["CaptureAudit", "CapturedFrame", "read_capture"]

_HDR = struct.Struct("<IBBH")
_VERSION = 1


@dataclass
class CapturedFrame:
    """一帧下发消息：`body` 为线上帧体（msgpack），`doc` 为按 wire.v1 解码上限解码的对象（惰性）。"""

    uav: str
    t: int
    body: bytes
    idx: int
    _doc: object = field(default=None, repr=False)

    @property
    def kind(self) -> str:
        return KINDS.get(self.t, f"0x{self.t:02x}")

    @property
    def doc(self):
        if self._doc is None:
            self._doc = decode_body(self.t, self.body)
        return self._doc


class CaptureAudit(ObsAudit):
    """白名单审计 + 下发帧录制。`keep=False` 时只落盘不留内存（长运行），`out_dir=None` 时只留内存。"""

    def __init__(self, *, out_dir: Path | None = None, keep: bool = True, fail_closed: bool = True) -> None:
        super().__init__(fail_closed=fail_closed)
        self.out_dir = Path(out_dir) if out_dir else None
        self.keep = keep
        self.frames: list[CapturedFrame] = []
        self.n_frames = 0
        self.n_bytes = 0
        self._fh: dict[str, object] = {}
        self._cap_lock = threading.Lock()
        if self.out_dir:
            self.out_dir.mkdir(parents=True, exist_ok=True)

    def check_body(self, t: int, body: bytes, *, uav: str | None = None) -> None:
        super().check_body(t, body, uav=uav)                 # 违规且失败即关闭时抛 644：帧不发送，不记录
        if t not in KINDS:
            return
        u = uav or "-"
        b = bytes(body)
        with self._cap_lock:
            if self.keep:
                self.frames.append(CapturedFrame(u, t, b, self.n_frames))
            self.n_frames += 1
            self.n_bytes += len(b)
            if self.out_dir:
                f = self._fh.get(u)
                if f is None:
                    f = self._fh[u] = (self.out_dir / f"{u}.wire").open("ab")
                f.write(_HDR.pack(len(b), t, 0, _VERSION) + b)

    def close(self) -> None:
        with self._cap_lock:
            for f in self._fh.values():
                f.close()
            self._fh.clear()

    def capture_summary(self) -> dict:
        by: dict[str, int] = {}
        for fr in self.frames:
            by[fr.kind] = by.get(fr.kind, 0) + 1
        return {"frames": self.n_frames, "bytes": self.n_bytes, "by_kind": dict(sorted(by.items()))}


def read_capture(path: Path) -> Iterator[CapturedFrame]:
    """读回 `CaptureAudit` 的落盘录制：`path` 为目录（按文件名排序的全部 `<uav>.wire`）或单个文件。"""
    path = Path(path)
    files = sorted(path.glob("*.wire")) if path.is_dir() else [path]
    idx = 0
    for p in files:
        uav = p.stem
        b = p.read_bytes()
        i = 0
        while i < len(b):
            if i + _HDR.size > len(b):
                raise ValueError(f"{p.name}: truncated frame header at {i}")
            n, t, _flags, _ver = _HDR.unpack_from(b, i)
            i += _HDR.size
            if i + n > len(b):
                raise ValueError(f"{p.name}: truncated frame body at {i}")
            yield CapturedFrame(uav, t, b[i:i + n], idx)
            idx += 1
            i += n
