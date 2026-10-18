"""线协议 `drone4d.bench.wire.v1` 的 Agent 侧（AWR-17 §18.9）：容器内经 Unix 套接字 `/run/drone4d/agent.sock` 与 Env 交换
HELLO、CONFIG、READY、OBS、ACT、CLOSE。帧与编码规则同宿主侧 `awr.agentsdk.wire`（黄金向量逐字节一致）。

用法（容器入口，或 `python -m drone4d.serve pkg.module:Factory`）：

    from drone4d.wire import serve
    serve(lambda: MyAgent())          # 每个容器一架机、一个 Agent 实例
"""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import sys
from collections.abc import Callable

import msgpack

from drone4d import __version__
from drone4d.agent import DroneAgent

__all__ = ["DEFAULT_SOCK", "PROTOCOL", "T", "encode_frame", "serve"]

PROTOCOL = "drone4d.bench.wire.v1"
DEFAULT_SOCK = "/run/drone4d/agent.sock"
_HDR = struct.Struct("<IBBH")


class T:
    HELLO, CONFIG, READY = 0x01, 0x02, 0x03
    OBS, ACT = 0x10, 0x11
    CLOSE, BYE = 0x20, 0x21
    ERROR = 0x30


def encode_frame(t: int, body) -> bytes:
    b = msgpack.packb(body, use_bin_type=True)
    return _HDR.pack(len(b), t, 0, 1) + b


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise ConnectionError("env closed the connection")
        out += chunk
    return bytes(out)


def _recv(sock: socket.socket) -> tuple[int, object]:
    n, t, _flags, _ver = _HDR.unpack(_recv_exact(sock, _HDR.size))
    return t, msgpack.unpackb(_recv_exact(sock, n), raw=False)


def serve(factory: Callable[[], DroneAgent], sock_path: str | None = None, *, prefs: dict | None = None) -> str:
    """连接 Env、握手并逐步应答，直到 CLOSE；→ CLOSE 的 reason。Agent 的异常不吞掉（容器以非 0 退出，Env 按崩溃处置）。"""
    path = sock_path or os.environ.get("DRONE4D_AGENT_SOCK", DEFAULT_SOCK)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    try:
        hello = {"protocol": PROTOCOL, "sdk_version": __version__, "prefs": prefs or {"camera": "latest", "lidar": "all", "max_per_sensor": 4}}
        s.sendall(encode_frame(T.HELLO, hello))
        agent: DroneAgent | None = None
        while True:
            t, body = _recv(s)
            if t == T.CONFIG:
                agent = factory()
                agent.reset(body)
                s.sendall(encode_frame(T.READY, {}))
            elif t == T.OBS:
                if agent is None:
                    raise RuntimeError("OBS before CONFIG")
                s.sendall(encode_frame(T.ACT, agent.step(body["obs"], body["inbox"])))
            elif t == T.CLOSE:
                with contextlib.suppress(OSError):
                    s.sendall(encode_frame(T.BYE, {}))
                return str(body.get("reason", ""))
            elif t == T.ERROR:
                print(f"drone4d: env reported error {body.get('code')} {body.get('why')}", file=sys.stderr, flush=True)
    finally:
        s.close()
