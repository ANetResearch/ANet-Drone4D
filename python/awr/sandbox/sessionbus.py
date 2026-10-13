"""每会话的总线会话（M17-FR-029；17 §17.10.1；AWR-04 §4.7 第 2 条）。

池为每个活跃会话在其命名空间 `awr/<world>/<sid>` 打开一个总线会话：
- 订阅 `state/sim-core/perf`（1 Hz，M08-FR-224）：回调只入队（PY-CB-01），由事件循环交给 broker（c_i、实际倍速、匿名页、手动锁）；
- 以该 run 的 `K_entry`（per-run secret 经 D1 HKDF 派生）为会话 principal 签名，先 `ctl/sim-core/lease{seat_claim}`（幂等），再调用 `ctl/sim-core/clock`：`play`（会话
  ACTIVE 时启动时钟）、`speed`（治理器回调降档、L2 降速）、`reset{sandbox_config}`（载入模板、恢复快照，M08 会话配置）；
  席位由 sandbox-api 在上下文创建时固定给会话 principal（ADR-093），因此池以同一 principal 签名即可通过 sim-core 的席位检查；
- 调用 `ctl/sim-core/snapshot{op: export}` 导出配置快照（结束、切换底图前的备份与 `GET /snapshot`）；
- 共享规划服务的 `ctl/plan/{submit,cancel}` 由 planhost 在本总线会话上声明（`PlanServer.attach_bus`）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable
from typing import Any

from awr.contracts import bus_keys
from awr.runtime.bus import BusError, unpack
from awr.runtime.principal import Principal, derive_key, sign_principal

__all__ = ["SessionBus"]

log = logging.getLogger("awr.sandbox.sessionbus")


class SessionBus:
    def __init__(self, sid: str, bus: Any, *, principal_id: str, secret: bytes, loop: asyncio.AbstractEventLoop,
                 on_perf: Callable[[str, dict], None]) -> None:
        self.sid = sid
        self.bus = bus
        self.principal_id = principal_id
        self.k_entry = derive_key(secret, sid, "entry")
        self.loop = loop
        self.on_perf = on_perf
        self.handles: list[Any] = []
        self.perf: dict | None = None
        self.closed = False

    def start(self) -> None:
        loop = self.loop
        self.handles.append(self.bus.subscribe(bus_keys.STATE_PERF,
                                               lambda _k, raw: loop.call_soon_threadsafe(self._perf, bytes(raw))))

    def _perf(self, raw: bytes) -> None:
        if self.closed:
            return
        try:
            d = unpack(raw)
        except Exception:
            return
        if isinstance(d, dict):
            self.perf = d
            self.on_perf(self.sid, d)

    def _principal(self, cid: str) -> dict[str, Any]:
        pr = Principal(self.principal_id, "operator", "api", None, True)
        d = pr.fields()
        d["sig"] = sign_principal(pr, cid, self.k_entry)
        return d

    async def clock(self, op: str, args: dict | None = None, *, timeout: float = 2.0) -> dict | None:
        """`ctl/sim-core/clock`（op 为 play、pause、speed、step、reset）；无回复返回 None。"""
        cid = "sbp-" + secrets.token_hex(8)
        msg = {"v": 1, "cid": cid, "op": op, "args": dict(args or {}), "principal": self._principal(cid)}
        try:
            rep = await self.bus.call(bus_keys.CTL_CLOCK, msg, timeout=timeout, retries=1)
        except BusError as e:
            log.warning("clock call failed", extra={"kv": {"sid": self.sid, "op": op, "err": str(e)}})
            return None
        return rep if isinstance(rep, dict) else None

    async def seat_claim(self, *, timeout: float = 2.0) -> dict | None:
        """`ctl/sim-core/lease{op: seat_claim}`：以会话 principal 登记席位（与 sandbox-api 的固定席位同一 principal，幂等）。
        池在启动时钟前调用，使 `play` 不依赖 sandbox-api 先完成上下文创建。"""
        cid = "sbp-" + secrets.token_hex(8)
        msg = {"v": 1, "cid": cid, "op": "seat_claim", "principal": self._principal(cid)}
        try:
            rep = await self.bus.call(bus_keys.CTL_LEASE, msg, timeout=timeout, retries=1)
        except BusError as e:
            log.warning("seat claim failed", extra={"kv": {"sid": self.sid, "err": str(e)}})
            return None
        return rep if isinstance(rep, dict) else None

    async def snapshot(self, *, timeout: float = 2.0) -> dict | None:
        try:
            rep = await self.bus.call(bus_keys.CTL_SNAPSHOT, {"v": 1, "op": "export"}, timeout=timeout, retries=1)
        except BusError as e:
            log.warning("snapshot export failed", extra={"kv": {"sid": self.sid, "err": str(e)}})
            return None
        if isinstance(rep, dict) and not rep.get("code") and isinstance(rep.get("snapshot"), dict):
            return rep["snapshot"]
        return None

    async def reset(self, cfg_doc: dict, *, timeout: float = 5.0) -> dict | None:
        return await self.clock("reset", {"sandbox_config": cfg_doc}, timeout=timeout)

    async def checkpoint_to_disk(self, path: Any, *, timeout: float = 5.0) -> dict | None:
        """D2.1 驻留（M08 变更请求）：`ctl/sim-core/checkpoint{to: "disk", path}` → `{ok, bytes, t_sim_ns, layout_hashes}`。
        sim-core 尚未提供该接口时（无服务方）立即返回 None，由池改走过渡路径（`park.py` 模块说明）。"""
        from .park import CTL_CHECKPOINT

        key = CTL_CHECKPOINT
        cid = "sbp-" + secrets.token_hex(8)
        msg = {"v": 1, "cid": cid, "op": "checkpoint", "to": "disk", "path": str(path), "principal": self._principal(cid)}
        try:
            rep = await self.bus.call(key, msg, timeout=timeout, retries=0)
        except BusError:
            return None
        return rep if isinstance(rep, dict) else None

    def close(self) -> None:
        self.closed = True
        for h in self.handles:
            with contextlib.suppress(Exception):
                h.close()
        self.handles.clear()
        with contextlib.suppress(Exception):
            self.bus.close()
