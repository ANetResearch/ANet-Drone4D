"""Agent 线协议 `drone4d.bench.wire.v1` 的宿主侧编解码与 Unix 套接字传输（AWR-17 §18.9；M26 §7.2；ADR-145）。

帧：`body_len u32le | type u8 | flags u8 (= 0) | version u16le (= 1) | msgpack body`（WS 无 `body_len`）。map 键为字符串，
发送方按字段顺序写 map（dict 插入顺序），bytes 以 msgpack `bin` 携带；解码上限 `max_str_len` 4 KiB、`max_array_len` 2^20、
`max_map_len` 4,096，各类型帧体有字节上限。违反时抛 `WireError(666, why)`。黄金向量见
`packages/contracts/bench/golden/wire/*.hex`（SDK 侧 `drone4d.wire` 逐字节对拍）。
"""

from __future__ import annotations

import contextlib
import socket
import struct
import time

import msgpack

__all__ = ["FRAME_MAX", "PROTOCOL", "T", "WireError", "WireSocket", "decode_body", "encode_frame", "encode_ws_frame"]

PROTOCOL = "drone4d.bench.wire.v1"
VERSION = 1
_HDR = struct.Struct("<IBBH")


class T:
    HELLO, CONFIG, READY = 0x01, 0x02, 0x03
    OBS, ACT = 0x10, 0x11
    CLOSE, BYE = 0x20, 0x21
    ERROR = 0x30
    PING, PONG = 0x40, 0x41


KiB, MiB = 1024, 1024 * 1024
FRAME_MAX = {T.HELLO: 64 * KiB, T.CONFIG: 16 * MiB, T.READY: 64 * KiB, T.OBS: 128 * MiB, T.ACT: 8 * MiB, T.CLOSE: 4 * KiB, T.BYE: 4 * KiB,
             T.ERROR: 4 * KiB, T.PING: 64, T.PONG: 64}


class WireError(Exception):
    def __init__(self, code: int, why: str) -> None:
        super().__init__(f"{code} {why}")
        self.code, self.why = code, why


def _pack(body) -> bytes:
    return msgpack.packb(body, use_bin_type=True)


def encode_frame(t: int, body) -> bytes:
    b = _pack(body)
    return _HDR.pack(len(b), t, 0, VERSION) + b


def encode_ws_frame(t: int, body) -> bytes:
    return struct.pack("<BBH", t, 0, VERSION) + _pack(body)


def decode_body(t: int, b: bytes):
    lim = FRAME_MAX.get(t)
    if lim is None:
        raise WireError(666, "frame")
    if len(b) > lim:
        raise WireError(666, "decode")
    try:
        return msgpack.unpackb(b, raw=False, strict_map_key=True, max_str_len=4 * KiB, max_bin_len=lim, max_array_len=1 << 20,
                               max_map_len=4096, max_ext_len=0)
    except (ValueError, msgpack.ExtraData, msgpack.FormatError, msgpack.StackError, TypeError) as e:
        raise WireError(666, "decode") from e


class WireSocket:
    """一条 Unix 套接字连接上的帧读写；`recv(deadline)` 以单调时钟截止时刻读一帧，超时抛 `TimeoutError`。"""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()

    def send(self, t: int, body) -> None:
        self.sock.sendall(encode_frame(t, body))

    def _fill(self, n: int, deadline: float | None) -> None:
        while len(self.buf) < n:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.sock.settimeout(left)
            else:
                self.sock.settimeout(None)
            try:
                chunk = self.sock.recv(max(65536, n - len(self.buf)))
            except TimeoutError:
                raise
            except OSError as e:
                raise ConnectionError(str(e)) from e
            if not chunk:
                raise ConnectionError("peer closed")
            self.buf += chunk

    def recv(self, deadline: float | None = None) -> tuple[int, object]:
        self._fill(_HDR.size, deadline)
        n, t, flags, ver = _HDR.unpack_from(self.buf)
        if flags != 0:
            if n <= FRAME_MAX.get(t, 0):                     # 丢弃整帧，连接可继续
                self._fill(_HDR.size + n, deadline)
                del self.buf[:_HDR.size + n]
            raise WireError(666, "frame")
        if ver != VERSION:
            raise WireError(666, "version")
        if n > FRAME_MAX.get(t, 0):
            raise WireError(666, "frame" if t not in FRAME_MAX else "decode")
        self._fill(_HDR.size + n, deadline)
        body = bytes(self.buf[_HDR.size:_HDR.size + n])
        del self.buf[:_HDR.size + n]
        return t, decode_body(t, body)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()
