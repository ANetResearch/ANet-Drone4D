"""`.awrrt` 采集（D1-ext；M11-FR-088；AWR-16 §13.9；ADR-050）：dev/test 构建中设 `AWR_RT_CAPTURE=<dir>` 时，把新连接的收发帧
按 16 §13.9 写成 `<dir>/<connId>.awrrt`，用于生成 `packages/contracts/fixtures/rt/` 夹具（读写库为生成的
`awr.contracts.frame.write_awrrt/read_awrrt`）。

- 事件循环只做 O(1) 追加（字节引用与单调时刻），连接关闭时整段交给后台线程序列化写盘；
- 每连接上限 200,000 条或 256 MiB（超出后停止采集并在头部记 `truncated: true`）；
- 头部：`{schema: "awr.rt.capture.v1", conn_id, session_id, contracts, principal_role, t0_wall_ns, truncated}`（密钥与 token
  不写入：握手前的 HTTP 头与子协议不在采集范围内）。
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Any

from awr.contracts import CONTRACTS_VERSION
from awr.contracts import frame as F

__all__ = ["Capture", "capture_dir"]

MAX_RECORDS = 200_000
MAX_BYTES = 256 << 20
PROFILES = ("dev", "ci", "test", "perf")


def capture_dir(profile: str) -> Path | None:
    d = os.environ.get("AWR_RT_CAPTURE")
    if not d or profile not in PROFILES:
        return None
    return Path(d)


class Capture:
    def __init__(self, out_dir: Path, conn_id: str, session_id: str, role: str) -> None:
        self.path = out_dir / f"{conn_id}.awrrt"
        self.header = {"schema": "awr.rt.capture.v1", "conn_id": conn_id, "session_id": session_id,
                       "contracts": CONTRACTS_VERSION, "principal_role": role}
        self.t0 = time.monotonic_ns()
        self.t0_wall = time.time_ns()
        self.recs: list[F.AwrtRecord] = []
        self.bytes = 0
        self.truncated = False

    def add(self, direction: int, data: Any) -> None:
        if self.truncated:
            return
        if isinstance(data, str):
            kind, payload = F.AWRT_TEXT, data.encode("utf-8")
        else:
            kind, payload = F.AWRT_BINARY, bytes(data)
        self.bytes += len(payload)
        if len(self.recs) >= MAX_RECORDS or self.bytes > MAX_BYTES:
            self.truncated = True
            return
        self.recs.append(F.AwrtRecord(direction, kind, time.monotonic_ns() - self.t0, payload))

    def s2c(self, data: Any) -> None:
        self.add(F.AWRT_S2C, data)

    def c2s(self, data: Any) -> None:
        self.add(F.AWRT_C2S, data)

    def close(self) -> threading.Thread:
        header = dict(self.header, t0_wall_ns=str(self.t0_wall), truncated=self.truncated)
        recs = self.recs
        self.recs = []
        path = self.path

        def write() -> None:
            with contextlib.suppress(OSError):
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(F.write_awrrt(header, recs, t0_wall_ns=int(header["t0_wall_ns"])))
                os.replace(tmp, path)

        t = threading.Thread(target=write, name="awrrt-capture", daemon=True)
        t.start()
        return t
