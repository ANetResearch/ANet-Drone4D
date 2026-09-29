"""最小 `awr.rt.v1` 客户端（M16 §6.12；AWR-17 §3–§7）：握手、hello（可带 resume）、订阅、解析 TIME 与 BATCH 帧头
（epoch、frame_seq、rflags）、发 `call` 并跟踪 `result`、收集事件。只用生成的 `awr.contracts.frame` 解码，不依赖前端代码。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from awr.contracts import CONTRACTS_VERSION
from awr.contracts import frame as F

PROTO = "awr.rt.v1"


@dataclass
class Probe:
    ws: Any
    server_info: dict = field(default_factory=dict)
    channels: dict[int, dict] = field(default_factory=dict)
    times: list[F.TimeFrame] = field(default_factory=list)
    batches: list[F.BatchHeader] = field(default_factory=list)
    order: list[tuple[str, int]] = field(default_factory=list)       # ("time"|"snapshot"|"batch", epoch)
    texts: list[dict] = field(default_factory=list)
    reset_records: int = 0

    @classmethod
    async def open(cls, base: str, token: str | None, *, resume: dict | None = None, timeout: float = 10.0) -> Probe:
        from websockets.asyncio.client import connect

        url = base.replace("http", "ws", 1) + "/api/rt"
        protos = [PROTO] + ([f"bearer.{token}"] if token else [])
        ws = await connect(url, subprotocols=protos, origin=base, compression=None, max_size=None, open_timeout=timeout)
        p = cls(ws)
        _, info = await p.recv(timeout)
        assert info.get("op") == "serverInfo", info
        p.server_info = info
        await p.until(lambda k, x: k == "time", timeout)
        m: dict[str, Any] = {"op": "hello", "client": "m16-rtprobe/0.1", "contracts": CONTRACTS_VERSION}
        if resume is not None:
            m["resume"] = resume
        await p.send(m)
        return p

    async def send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj, separators=(",", ":")))

    async def recv(self, timeout: float = 5.0) -> tuple[str, Any]:
        m = await asyncio.wait_for(self.ws.recv(), timeout)
        if isinstance(m, (bytes, bytearray)):
            if m[0] == F.OP_TIME:
                t = F.decode_time(m)
                self.times.append(t)
                self.order.append(("time", t.epoch))
                return "time", t
            h = F.decode_batch_header(m)
            self.batches.append(h)
            recs = list(F.iter_records(m))
            snap = any(r.rflags & (F.RF_RESET | F.RF_KEYFRAME) for r in recs)   # SNAPSHOT：RESET 或关键帧记录
            self.reset_records += sum(1 for r in recs if r.rflags & F.RF_RESET)
            self.order.append(("snapshot" if snap else "batch", h.epoch))
            await self.send({"op": "ack", "frame": h.frame_seq})
            return "batch", h
        j = json.loads(m)
        self.texts.append(j)
        if j.get("op") == "advertise":
            for c in j.get("channels") or []:
                self.channels[int(c["id"])] = c
        return "json", j

    async def until(self, pred: Any, timeout: float = 10.0) -> tuple[str, Any]:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("rtprobe: condition not met")
            k, x = await self.recv(left)
            if pred(k, x):
                return k, x

    async def drain(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            try:
                await self.recv(left)
            except TimeoutError:
                return

    async def subscribe(self, subs: list[dict], timeout: float = 5.0) -> None:
        await self.send({"op": "subscribe", "subs": subs})
        last = subs[-1]["id"]
        await self.until(lambda k, x: k == "json" and x.get("op") == "subscribed" and x.get("id") == last, timeout)

    async def call(self, cid: str, service: str, args: dict | None = None) -> None:
        await self.send({"op": "call", "id": cid, "service": service, "args": args or {}})

    async def result(self, cid: str, *, final: bool = True, timeout: float = 30.0) -> dict:
        for m in self.texts:
            if m.get("op") == "result" and m.get("id") == cid and (m.get("final") or not final):
                return m
        _, r = await self.until(lambda k, x: k == "json" and x.get("op") == "result" and x.get("id") == cid
                                and (bool(x.get("final")) or not final), timeout)
        return r

    def events(self) -> list[dict]:
        out: list[dict] = []
        for m in self.texts:
            if m.get("op") == "event":
                out.append(m)
            elif m.get("op") == "events":
                out.extend(m.get("items") or [])
        return out

    def roster_ids(self) -> list[str]:
        return sorted({c["topic"].split("/")[1] for c in self.channels.values() if c["topic"].startswith("uav/")})

    async def close(self) -> None:
        await self.ws.close()
