"""共享展示的实时观看席位与观看排队（show-api 内存；M11-FR-223、FR-224、FR-225；AWR-04 §15.6.3；17 §17.20.3；ADR-120）。

- 席位：每条 viewer 角色、未带身份令牌的 `/api/rt` 连接占 1 个席位，上限 `V_max = viewer_seats.max`（auto 取 `net.ws_max`）；
  operator、admin 与内部身份的连接不计。握手时 `admit(principal)`：该 principal 有 `seat_ready` 预留时消耗预留；否则只有
  `live + 预留数 < V_max` 才放行（队列非空时空位总是先预留给队首，后来者不能插队）；
- 排队票：`issue(principal_hint, prefix)`（R81）同一 principal 幂等（返回同一 `ticket_id`，换发票据令牌）；每前缀 ≤ `per_prefix`
  张（409 `504`）；队列 ≤ `queue_max`（409 `500`）。票据令牌 `vq1.` + 32 字节随机数（base64url），只保存 SHA-256；
- 派发：席位释放（断开、节能、空闲兜底）与新票时按 FIFO 把队首置为 `seat_ready`，为其 principal 预留 `reserve_s`（30 s），预留期内
  该 principal 的握手占用预留席位；超时则作废并派发下一张；`waiting` 票 `ticket_idle_s`（60 s）无长轮询即作废（`519`）；
- 预计等待：`eta_s = ⌈position / max(λ, 1/120)⌉`，λ 为最近 10 min 的席位释放速率（次/s）；
- 状态文件（FR-225）：`snapshot()` 给出 `{t_unix_ns, live, max, queue, eta_s, releases_per_s}`，由 show-api 每 1 s 原子写入
  `AWR_SHOW_STATUS_FILE`（缺省 `<AWR_SHM_ROOT>/_status/show.json`，0644），sandbox-api 读取后组装 SB-71。

队列只在内存中：show-api 重启时清空，客户端以 4431 重新排队（AWR-04 §15.6.3 第 3 条）。全部方法只在事件循环线程调用。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import math
import os
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from awr.runtime.procutil import atomic_write

__all__ = ["SeatsConfig", "Ticket", "TicketError", "ViewerSeats", "status_path"]

ALPHA = "abcdefghijklmnopqrstuvwxyz234567"
RATE_WINDOW_S = 600.0
ETA_FLOOR_RATE = 1.0 / 120.0


def status_path(env: dict[str, str] | None = None) -> Path:
    """show-api 状态文件路径（与池 `awr.sandbox.capacity.show_status_path` 同一规则）；`AWR_SHOW_STATUS_OUT` 只改写入路径
    （测试以合成文件验证池的读取时，使真实写入者不覆盖它）。"""
    e = os.environ if env is None else env
    if e.get("AWR_SHOW_STATUS_OUT"):
        return Path(e["AWR_SHOW_STATUS_OUT"])
    if e.get("AWR_SHOW_STATUS_FILE"):
        return Path(e["AWR_SHOW_STATUS_FILE"])
    return Path(e.get("AWR_SHM_ROOT") or "/dev/shm/awr") / "_status" / "show.json"


@dataclass
class SeatsConfig:
    max: int = 24
    queue_max: int = 100
    reserve_s: float = 30.0
    per_prefix: int = 2
    ticket_idle_s: float = 60.0
    long_poll_s: float = 25.0

    @classmethod
    def from_cfg(cls, cfg: Any, ws_max: int) -> SeatsConfig:
        d = cfg.model_dump(mode="json") if hasattr(cfg, "model_dump") else dict(cfg or {})
        mx = d.get("max", "auto")
        out = cls(max=int(ws_max) if mx in (None, "auto") else int(mx))
        for k in ("queue_max", "per_prefix"):
            if isinstance(d.get(k), int):
                setattr(out, k, int(d[k]))
        for k in ("reserve_s", "ticket_idle_s", "long_poll_s"):
            if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool):
                setattr(out, k, float(d[k]))
        return out


class TicketError(Exception):
    def __init__(self, code: int, status: int, why: str | None = None) -> None:
        super().__init__(why or str(code))
        self.code = code
        self.status = status
        self.why = why


@dataclass
class Ticket:
    ticket_id: str
    principal_id: str
    prefix: str
    token_sha: bytes
    created_mono: float
    last_poll_mono: float
    state: str = "waiting"                      # waiting | seat_ready | expired
    reserve_deadline_mono: float | None = None
    reserve_deadline_unix_ns: int | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)


def _tid() -> str:
    return "vq-" + "".join(secrets.choice(ALPHA) for _ in range(10))


def _token() -> str:
    return "vq1." + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _sha(tok: str) -> bytes:
    return hashlib.sha256(tok.encode("utf-8")).digest()


class ViewerSeats:
    def __init__(self, cfg: SeatsConfig, *, clock=time.monotonic) -> None:
        self.cfg = cfg
        self.clock = clock
        self.live = 0                                           # 占席位的 viewer 连接数
        self.reserved: dict[str, tuple[float, str]] = {}        # principal -> (预留截止单调秒, ticket_id)
        self.tickets: OrderedDict[str, Ticket] = OrderedDict()
        self.by_principal: dict[str, str] = {}
        self.releases: deque[float] = deque()
        self.started = clock()
        self.stats = {"admitted": 0, "refused": 0, "issued": 0, "dispatched": 0, "expired": 0, "reserve_timeouts": 0,
                      "claimed": 0, "left": 0}

    # ------------------------------------------------------------ 席位
    @property
    def max(self) -> int:
        return max(0, int(self.cfg.max))

    def free(self) -> int:
        return self.max - self.live - len(self.reserved)

    def waiting(self) -> list[Ticket]:
        return [t for t in self.tickets.values() if t.state == "waiting"]

    def full(self) -> bool:
        return self.live + len(self.reserved) >= self.max

    def busy(self) -> bool:
        """席位已满且观看排队非空（兜底改用 viewer_idle_busy_s，AWR-04 §15.7.2）。"""
        return self.full() and bool(self.waiting())

    def admit(self, principal_id: str) -> bool:
        """viewer 握手：有预留则消耗；否则 live + 预留 < V_max 才放行。放行即占席位（调用方在断开时 `release()`）。"""
        self.gc()
        r = self.reserved.pop(principal_id, None)
        if r is not None:
            t = self.tickets.pop(r[1], None)
            if t is not None:
                self.by_principal.pop(t.principal_id, None)
                t.state = "expired"
                t.event.set()
            self.live += 1
            self.stats["claimed"] += 1
            self.stats["admitted"] += 1
            return True
        if self.free() > 0 and not self.waiting():
            self.live += 1
            self.stats["admitted"] += 1
            return True
        self.stats["refused"] += 1
        return False

    def release(self) -> None:
        """占席位的连接关闭：释放并派发队首。"""
        if self.live > 0:
            self.live -= 1
        self.releases.append(self.clock())
        self.dispatch()

    # ------------------------------------------------------------ 排队
    def issue(self, principal_id: str, prefix: str) -> tuple[Ticket, str, bool]:
        """R81：返回 (票, 票据令牌, 是否新建)。同一 principal 幂等（换发令牌）；超限抛 TicketError。"""
        self.gc()
        now = self.clock()
        tid = self.by_principal.get(principal_id)
        if tid is not None and tid in self.tickets:
            t = self.tickets[tid]
            tok = _token()
            t.token_sha = _sha(tok)
            t.last_poll_mono = now
            return t, tok, False
        if sum(1 for t in self.tickets.values() if t.prefix == prefix) >= self.cfg.per_prefix:
            raise TicketError(504, 409, "ticket_per_prefix")
        if len(self.tickets) >= self.cfg.queue_max:
            raise TicketError(500, 409, "viewer_queue_full")
        tok = _token()
        t = Ticket(_tid(), principal_id, prefix, _sha(tok), now, now)
        self.tickets[t.ticket_id] = t
        self.by_principal[principal_id] = t.ticket_id
        self.stats["issued"] += 1
        self.dispatch()
        return t, tok, True

    def get(self, ticket_id: str, token: str | None) -> Ticket:
        t = self.tickets.get(ticket_id)
        if t is None or not token or not hmac.compare_digest(t.token_sha, _sha(token)) or t.state == "expired":
            raise TicketError(519, 410)
        return t

    async def poll(self, ticket_id: str, token: str | None, wait_s: float) -> Ticket:
        """R82：长轮询到状态变化或 wait_s（≤ long_poll_s）。"""
        self.gc()
        t = self.get(ticket_id, token)
        t.last_poll_mono = self.clock()
        w = max(0.0, min(float(wait_s), self.cfg.long_poll_s))
        if t.state == "waiting" and w > 0:
            t.event.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(t.event.wait(), w)
            t.last_poll_mono = self.clock()
            if t.state == "expired" or self.tickets.get(ticket_id) is not t:
                raise TicketError(519, 410)
        return t

    def leave(self, ticket_id: str, token: str | None) -> None:
        """R83：离队（seat_ready 的预留一并释放并派发下一张）。"""
        t = self.get(ticket_id, token)
        self._drop(t)
        self.stats["left"] += 1
        self.dispatch()

    def _drop(self, t: Ticket) -> None:
        self.tickets.pop(t.ticket_id, None)
        if self.by_principal.get(t.principal_id) == t.ticket_id:
            self.by_principal.pop(t.principal_id, None)
        r = self.reserved.get(t.principal_id)
        if r is not None and r[1] == t.ticket_id:
            self.reserved.pop(t.principal_id, None)
        t.state = "expired"
        t.event.set()

    def dispatch(self) -> None:
        now = self.clock()
        while self.free() > 0:
            head = next((t for t in self.tickets.values() if t.state == "waiting"), None)
            if head is None:
                return
            head.state = "seat_ready"
            head.reserve_deadline_mono = now + self.cfg.reserve_s
            head.reserve_deadline_unix_ns = time.time_ns() + int(self.cfg.reserve_s * 1e9)
            self.reserved[head.principal_id] = (head.reserve_deadline_mono, head.ticket_id)
            self.stats["dispatched"] += 1
            head.event.set()

    def gc(self) -> None:
        """作废超时票与过期预留（每次调用与 1 Hz 定时器）。"""
        now = self.clock()
        changed = False
        for t in list(self.tickets.values()):
            if t.state == "waiting" and now - t.last_poll_mono > self.cfg.ticket_idle_s:
                self._drop(t)
                self.stats["expired"] += 1
                changed = True
            elif t.state == "seat_ready" and t.reserve_deadline_mono is not None and now > t.reserve_deadline_mono:
                self._drop(t)
                self.stats["reserve_timeouts"] += 1
                changed = True
        for pid, (deadline, tid) in list(self.reserved.items()):
            if now > deadline or tid not in self.tickets:
                self.reserved.pop(pid, None)
                changed = True
        while self.releases and now - self.releases[0] > RATE_WINDOW_S:
            self.releases.popleft()
        if changed:
            self.dispatch()

    # ------------------------------------------------------------ 估计与状态
    def position(self, t: Ticket) -> int:
        if t.state != "waiting":
            return 0
        n = 0
        for x in self.tickets.values():
            if x.state == "waiting":
                n += 1
                if x is t:
                    return n
        return n

    def release_rate(self) -> float:
        span = min(RATE_WINDOW_S, max(1.0, self.clock() - self.started))
        return len(self.releases) / span

    def eta_s(self, position: int) -> int | None:
        if position <= 0:
            return 0
        lam = max(self.release_rate(), ETA_FLOOR_RATE)
        return math.ceil(position / lam)

    def ticket_view(self, t: Ticket) -> dict[str, Any]:
        pos = self.position(t)
        return {"ticket_id": t.ticket_id, "state": t.state, "position": pos, "eta_s": self.eta_s(pos) if t.state == "waiting" else 0,
                "reserve_deadline_unix_ns": str(t.reserve_deadline_unix_ns) if t.reserve_deadline_unix_ns else None}

    def snapshot(self) -> dict[str, Any]:
        q = len(self.waiting())
        return {"t_unix_ns": str(time.time_ns()), "live": int(self.live), "max": self.max, "queue": q,
                "reserved": len(self.reserved), "eta_s": self.eta_s(q + 1) if self.full() else None,
                "releases_per_s": round(self.release_rate(), 5)}

    def write_status(self, path: Path) -> None:
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json.dumps(self.snapshot(), separators=(",", ":")).encode("utf-8"), 0o644)
