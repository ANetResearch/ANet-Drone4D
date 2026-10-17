"""broker：会话表、排队票、配额、工作量证明、续期、抢占、空闲回收、倍速授予与降级动作（M17-FR-001 至 FR-023；
AWR-04 §4.4、§4.6；17 §17.3、§17.4；M17 PRD §6.3、§6.4、§6.6.1 至 §6.6.6）。

纯逻辑：全部方法同步、在池的单个事件循环内调用（无竞争）；时间由注入的 `clock()`（墙钟秒）给出；进程、总线、落盘等副作用
经 `Effects` 交给池（异步执行，完成后回调 `on_spawned`、`on_ready`、`on_spawn_failed`、`on_exit`、`on_ended`、`on_reset`）。

操作的回复统一为 `{status, http, code, detail, result}`：`status ∈ {ok, created, existing, queued, rejected}`，`http` 为
sandbox-api 应返回的 HTTP 状态，`code` 为原因码（0 为成功，500–514 见 17 §17.9.2），`detail` 为 problem 的 detail。

线上形状：`wire(rec)` 为池内会话记录的线上视图（`awr.sandbox.session.v1` 全部字段 + sandbox-api 需要的 `principal_id`、
`binding`、`max_expires_unix_ns`、`snapshot_available`；sandbox-api 对外回复时按 schema 过滤）；`ticket_view(t)` 为
`awr.sandbox.ticket.v1`；`summary(rec)` 为 `awr.sandbox.session_state.v1` 摘要（1 Hz `state/sandbox-pool/sessions`）。

D2.1（AWR-04 §15.6、§15.7.4、§15.9.3；M17 PRD §14）：
- 驻留与恢复见 `park.BrokerParkMixin`；无连接空闲、兜底超时、抢占与 L3、L4 回收一律先驻留（PARKING → PARKED），只有用户结束、
  寿命到期、未认领、维护、故障与驻留到期直接结束；
- 实例用户口径：槽位 = 会话（SPAWNING 至 DRAINING，含 PARKING）+ OFFERED 票；内部档会话先用 `internal.reserved_slots` 个
  预留槽位（不计入 N_max），预留已用或内存准入不满足时内部票排在队首，不抢占公开会话；内部档免工作量证明与前缀配额、寿命上限
  8 h、不被抢占、L3 与 L4 回收排在最后、每个身份同时 ≤ 2 个会话，身份撤销或过期后降为公开档；
- 活动报告的 `last_input_unix_ns`（presence 与命令的最近交互）驱动服务端兜底驻留；
- P1：指挥器配对（`pair{op: code | redeem}`，M17-FR-081）与内存额度预留（`reserve{mem_mb, ttl_s}`，M17-FR-082）。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
import time
from collections.abc import Callable
from typing import Any

from awr.contracts import CONTRACTS_VERSION
from awr.runtime.binding import RunBinding

from . import governor
from .admission import world_admit
from .config import SPAWN_EST_S, PoolSettings
from .ladder import Ladder, LadderInput, SessionLoad, reclaim_score
from .model import (
    ACTIVE,
    CLAIMED,
    DRAINING,
    END_STATES,
    ENDED,
    FAILED,
    IDLE,
    LEFT,
    LIVE_STATES,
    OFFERED,
    PARKED,
    PARKING,
    READY,
    SPAWNING,
    TICKET_WIRE,
    WAITING,
    SessionRecord,
    Ticket,
    new_pair_code,
    new_sid,
    new_ticket_id,
    new_ticket_token,
    ns,
    wire_end_reason,
)
from .park import BrokerParkMixin, wire_park_reason
from .pow import Pow
from .prefix import hash_prefix, prefixes
from .queue import TicketQueue, eta_s
from .snapshot import check_snapshot
from .templates import DEFAULT_TEMPLATE, EMPTY_TEMPLATES, TemplateCatalog, build_session_config
from .tokens import Keys, ticket_token_hash, ticket_token_ok
from .worlds import WorldTable

__all__ = ["SDK_MIN_VERSION", "Broker", "Effects", "err", "ok"]

log = logging.getLogger("awr.sandbox.broker")

SDK_MIN_VERSION = "1.0.0"
TICK_HZ = 100
HOUR_S = 3600.0
NOTICE_REASONS = ("lifetime", "preempted", "reclaimed_cpu", "reclaimed_mem")
SESSION_ROUTE = "/api/sandbox/v1/sessions/"
HELD_STATES = (READY, ACTIVE, IDLE)
PAIR_TTL_S = 120.0
PAIR_PER_10MIN = 5
PAIR_MAX_FAILS = 5


def _unix_s(v: Any) -> float | None:
    """`*_unix_ns`（十进制字符串或整数）→ 墙钟秒；非法为 None。"""
    try:
        return int(v) / 1e9 if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def ok(result: Any = None, http: int = 200, status: str = "ok") -> dict[str, Any]:
    return {"status": status, "http": http, "code": 0, "detail": None, "result": result}


def err(http: int, code: int, **detail: Any) -> dict[str, Any]:
    return {"status": "rejected", "http": http, "code": int(code), "detail": detail or None, "result": None}


class Effects:
    """副作用接口（池实现；测试用记录替身）。全部方法立即返回，异步完成后由池回调 broker。"""

    def spawn(self, rec: SessionRecord, cfg_doc: dict | None, *, restart: bool = False) -> None: ...

    def stop(self, rec: SessionRecord, *, export: bool = True) -> None: ...

    def switch(self, rec: SessionRecord, cfg_doc: dict) -> None: ...

    def kill(self, rec: SessionRecord, why: str) -> None: ...

    def clock(self, rec: SessionRecord, op: str, args: dict) -> None: ...

    def reset(self, rec: SessionRecord, cfg_doc: dict, reason: str) -> None: ...

    def event(self, kind: str, sid: str, data: dict, severity: int = 1) -> None: ...

    def audit(self, kind: str, **kv: Any) -> None: ...

    def changed(self) -> None: ...

    def ticket_changed(self, t: Ticket) -> None: ...

    def park(self, rec: SessionRecord) -> None: ...

    def resume(self, rec: SessionRecord) -> None: ...

    def discard_park(self, rec: SessionRecord) -> None: ...


class Broker(BrokerParkMixin):
    def __init__(self, settings: PoolSettings, worlds: WorldTable, templates: TemplateCatalog, keys: Keys,
                 effects: Effects | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self.s = settings
        self.sb = settings.sb
        self.worlds = worlds
        self.templates = templates
        self.keys = keys
        self.fx = effects or Effects()
        self.now = clock
        self.sessions: dict[str, SessionRecord] = {}
        self.queue = TicketQueue()
        self.creates: dict[str, list[float]] = {}        # prefix_h 或 agg_h -> 实际拉起时刻（1 h 窗口）
        self.buckets: dict[str, list[float]] = {}        # "p:"/"a:" + 散列 -> [令牌, 时刻]
        self.pow_triggers: dict[str, list[float]] = {}
        p = self.sb.pow
        self.pow = Pow(keys.k_pow, ttl_s=p.ttl_s, bits=p.bits, bits_max=p.bits_hot)
        self.accepting = True
        self.disk_full = False
        self.ladder = Ladder(self.sb.ladder)
        self.level = 0
        self.last_reclaim = -1e18
        self.reclaim_hold = False           # L3/L4 回收之后、降到 L1 及以下之前不再接纳（D2-FX-R2，见 cpu_blocked）
        self._n_max: int | None = None
        self.delegated = settings.is_delegated()
        self.is_revoked: Callable[[str], bool] = lambda _jti: False   # 身份撤销表（池注入 identity.RevocationList）
        self.mem_reserved_mb = 0.0          # 扩展进程组按需启用时临时下调的沙盒组内存额度（P1，M17-FR-082）
        self.mem_reserved_until = 0.0

    # ------------------------------------------------------------ 查询
    def n_max(self) -> int:
        if self._n_max is None:
            self._n_max = self.s.n_max()
        return self._n_max

    def refresh_n_max(self) -> int:
        self._n_max = None
        return self.n_max()

    def live(self) -> list[SessionRecord]:
        return [r for r in self.sessions.values() if r.state in LIVE_STATES]

    def n_live(self) -> int:
        """占槽数：活跃会话 + OFFERED 票的预留（M17 §1.3 槽位；含内部档）。"""
        return len(self.live()) + len(self.queue.offered())

    def live_worlds(self) -> list[str]:
        return [r.world for r in self.live()] + [t.world for t in self.queue.offered()]

    def reserved_slots(self) -> int:
        return max(0, int(self.s.internal.reserved_slots))

    def n_internal(self) -> int:
        """内部档占槽数（会话 + OFFERED 的内部票）。"""
        return sum(1 for r in self.live() if r.tier == "internal") + \
            sum(1 for t in self.queue.offered() if t.kind == "internal")

    def free_slots(self) -> int:
        """公开槽位的空位：内部档先占预留槽位，超出预留的部分才计入 N_max（ADR-120）。"""
        return max(0, self.n_max() - (self.n_live() - min(self.reserved_slots(), self.n_internal())))

    def free_reserved(self) -> int:
        return max(0, self.reserved_slots() - self.n_internal())

    def queue_waiting(self) -> bool:
        return bool(self.queue.waiting())

    def m_sb(self) -> float:
        """沙盒组内存额度（MB）：`mem_sandbox_mb` 减去有效的临时预留。"""
        res = self.mem_reserved_mb if self.mem_reserved_until > self.now() else 0.0
        return max(0.0, float(self.sb.mem_sandbox_mb) - res)

    def admit(self, world: str, *, replace: str | None = None, live_worlds: list[str] | None = None,
              tier: str = "public") -> bool:
        cap = self.n_max() + (self.reserved_slots() if tier == "internal" else min(self.reserved_slots(), self.n_internal()))
        return world_admit(self.worlds.costs, self.live_worlds() if live_worlds is None else live_worlds, world,
                           n_max=cap, m_sb=self.m_sb(), replace=replace)

    def cpu_blocked(self) -> bool:
        """停止接纳（创建、恢复与派发排队票）：L3 及以上，或 L3/L4 回收之后仍在 L2（D2-FX-R2）。D2-ACC-1 变体 A 中 L3 回收驻留一个
        会话后阶梯回到 L2，空出的槽位立即被 SDK 的自动恢复占用，恢复的会话再次饿死，约每 60 s 一轮"回收 → 恢复 → 回收"
        （会话 epoch 升到 8–10）；回收之后保持停止接纳直到 L2 也解除（`l2_exit_s` 的持续条件）。"""
        return self.level >= 3 or (self.reclaim_hold and self.level >= 2)

    def startable_worlds(self) -> list[str]:
        if self.free_slots() <= 0 or self.cpu_blocked() or not self.accepting:
            return []
        return [w.world_id for w in self.worlds.usable() if self.admit(w.world_id)]

    def binding(self, rec: SessionRecord) -> RunBinding:
        return RunBinding.make(rec.sid, rec.world, kind="sandbox", run_dir=self.s.shm_root / rec.sid,
                               secret_path=self.s.runs_dir / rec.sid / "secret", persist_dir=self.s.runs_dir / rec.sid,
                               created_unix_ns=int(rec.created * 1e9))

    def _hash(self, prefix: str) -> str:
        return hash_prefix(self.keys.k_state, prefix)

    def _prefixes(self, addr: Any) -> tuple[str, str]:
        p = self.sb.prefix
        return prefixes(str(addr) if addr else None, v4=p.v4, v6=p.v6, agg_v4=p.agg_v4, agg_v6=p.agg_v6)

    # ------------------------------------------------------------ 线上视图
    def renew_block(self, rec: SessionRecord) -> str | None:
        if rec.state not in (READY, ACTIVE, IDLE):
            return None
        if rec.tier != "internal" and (self.queue_waiting() or rec.park_pending == "preempted"):
            return "queue_nonempty"
        if rec.expires >= rec.max_expires - 1e-6:
            return "max_lifetime"
        if self.level >= 3 or (rec.drain_at is not None and rec.end_reason in ("reclaimed_cpu", "reclaimed_mem")):
            return "degraded"
        return None

    def idle_ttl(self) -> float:
        """无连接且无调用到驻留的阈值（D2.1：替代 D2 的空闲结束 TTL）。"""
        pk = self.s.park
        return pk.idle_disconnected_busy_s if self.queue_waiting() else pk.idle_disconnected_s

    def wire(self, rec: SessionRecord) -> dict[str, Any]:
        now = self.now()
        block = self.renew_block(rec)
        live = rec.state in LIVE_STATES
        parked = rec.state == PARKED
        usage = {"vehicles": int(rec.usage.get("vehicles", 0)), "targets": int(rec.usage.get("targets", 0)),
                 "nests": int(rec.usage.get("nests", 0)), "tasks_active": int(rec.usage.get("tasks_active", 0)),
                 "ws": int(rec.ws)}
        if isinstance(rec.usage.get("cloned_models"), int):
            usage["cloned_models"] = int(rec.usage["cloned_models"])
        d: dict[str, Any] = {
            "sid": rec.sid, "state": rec.state, "world_id": rec.world, "template_id": rec.template,
            "principal_id": rec.principal_id, "created_unix_ns": ns(rec.created), "expires_unix_ns": ns(rec.expires),
            "lifetime_max_unix_ns": ns(rec.max_expires), "max_expires_unix_ns": ns(rec.max_expires),
            "expires_in_s": max(0, int(rec.expires - now)) if live and rec.state != PARKING else
            (max(0, int(rec.remaining_s or 0)) if rec.state in (PARKING, PARKED) else 0),
            "renewable": rec.state in HELD_STATES and block is None,
            "renew_block_reason": block, "idle_ttl_s": int(self.idle_ttl()),
            "clock": {"state": rec.clock_state, "rate_requested": float(rec.requested_rate),
                      "rate_granted": float(rec.granted_rate), "rate_cap": round(float(rec.rate_cap), 3),
                      "limited_by": governor.wire_limited_by(rec.limited_by), "tick_hz": TICK_HZ},
            "limits": self.s.limits_obj(), "usage": usage, "epoch": int(rec.epoch), "restarts": int(rec.restarts),
            "degrade_level": f"L{self.level}", "calendar": rec.calendar, "acoustic_bg": rec.acoustic_bg,
            "seed": int(rec.seed), "end_reason": wire_end_reason(rec.end_reason) if not live or rec.state == DRAINING else None,
            "endpoints": {"rest": SESSION_ROUTE + rec.sid, "rt": SESSION_ROUTE + rec.sid + "/rt"},
            "snapshot_available": True if parked else (bool(rec.snapshot_path) if not live else rec.state != SPAWNING),
            "ready_unix_ns": ns(rec.ready_at),
            "tier": rec.tier, "last_input_unix_ns": ns(rec.last_input) if rec.last_input else None,
        }
        if parked:
            d["expires_unix_ns"] = ns(now + float(rec.remaining_s or 0.0))
        d.update(self.park_wire(rec))
        if not d["calendar"]:
            d.pop("calendar")
        if not d["acoustic_bg"]:
            d.pop("acoustic_bg")
        if (live and rec.pid is not None) or rec.state in (READY, ACTIVE, IDLE, DRAINING):
            d["binding"] = self.binding(rec).to_msg()
        return d

    def summary(self, rec: SessionRecord) -> dict[str, Any]:
        """`awr.sandbox.session_state.v1` 摘要（sandbox-api 合并本地 ws 与实体数后发布 `sandbox/session`）。"""
        now = self.now()
        d: dict[str, Any] = {"sid": rec.sid, "state": rec.state, "world_id": rec.world, "template_id": rec.template,
                             "expires_unix_ns": ns(rec.expires), "max_expires_unix_ns": ns(rec.max_expires),
                             "renewable": self.renew_block(rec) is None and rec.state in (READY, ACTIVE, IDLE),
                             "renew_block_reason": self.renew_block(rec),
                             "clock": {"rate_granted": float(rec.granted_rate), "rate_cap": round(float(rec.rate_cap), 3),
                                       "limited_by": rec.limited_by, "rate_requested": float(rec.requested_rate)},
                             "queue_len": len(self.queue.waiting()), "degrade_level": f"L{self.level}",
                             "usage": {k: int(v) for k, v in rec.usage.items() if k in ("nests", "tasks_active", "cloned_models")},
                             "epoch": int(rec.epoch)}
        if rec.park_at is not None and rec.state in HELD_STATES:
            d["expiring"] = {"reason": wire_park_reason(rec.park_pending), "in_s": max(0.0, round(rec.park_at - now, 1)),
                             "action": "park"}
        elif rec.drain_at is not None and rec.end_reason in (*NOTICE_REASONS, "idle", "user", "unclaimed"):
            d["expiring"] = {"reason": wire_end_reason(rec.end_reason), "in_s": max(0.0, round(rec.drain_at - now, 1)),
                             "action": "end"}
        d["tier"] = rec.tier
        if rec.last_input:
            d["last_input_unix_ns"] = ns(rec.last_input)
        return d

    def ticket_view(self, t: Ticket) -> dict[str, Any]:
        pos = self.queue.position(t)
        eta = 0.0 if t.state == OFFERED else (self.eta(pos) if t.state == WAITING else None)
        return {"ticket_id": t.ticket_id, "state": TICKET_WIRE[t.state], "position": pos, "eta_s": eta,
                "claim_deadline_unix_ns": ns(t.offer_deadline) if t.state == OFFERED else None,
                "world_id": t.world, "template_id": t.template}

    def eta(self, position: int) -> float | None:
        now = self.now()
        q = self.sb.queue
        ends = []
        res = self.reserved_slots()
        n_int = 0
        for r in self.live():
            if r.tier == "internal" and n_int < res:
                n_int += 1          # 预留槽位上的内部会话不释放公开槽位
                continue
            if r.state in (DRAINING, PARKING) or r.drain_at is not None:
                ends.append(r.drain_at if r.drain_at is not None else now + 5.0)
                continue
            if r.park_at is not None:
                ends.append(r.park_at + 5.0)
                continue
            held_from = r.held_since or (r.ready_at if r.ready_at is not None else r.created)
            pre = max(now, held_from + q.preempt_after_s) + self.sb.notice_s
            ends.append(min(r.expires, pre) if r.tier != "internal" else r.expires)
        ends += [now] * max(0, self.n_max() - len(ends) - len([t for t in self.queue.offered() if t.kind != "internal"]))
        return eta_s(position, ends, now, spawn_s=SPAWN_EST_S)

    # ------------------------------------------------------------ 配额与工作量证明
    def _window(self, key: str, now: float) -> list[float]:
        xs = [t for t in self.creates.get(key, []) if now - t < HOUR_S]
        if xs:
            self.creates[key] = xs
        else:
            self.creates.pop(key, None)
        return xs

    def _bucket(self, key: str, rate_per_h: float, burst: float, now: float, *, take: bool) -> float:
        """令牌桶：返回需要等待的秒数（0 为可取）；take 时取一个令牌。"""
        tok, t0 = self.buckets.get(key, [float(burst), now])
        tok = min(float(burst), tok + max(0.0, now - t0) * rate_per_h / HOUR_S)
        if tok >= 1.0:
            if take:
                tok -= 1.0
            self.buckets[key] = [tok, now]
            return 0.0
        self.buckets[key] = [tok, now]
        return (1.0 - tok) * HOUR_S / rate_per_h

    def _rate_wait(self, ph: str, ah: str, now: float) -> float:
        p = self.sb.prefix
        a = self._bucket("p:" + ph, p.create_per_h, p.create_burst, now, take=False)
        b = self._bucket("a:" + ah, p.agg_create_per_h, p.agg_create_burst, now, take=False)
        return max(a, b)

    def _count_create(self, ph: str, ah: str, now: float) -> None:
        self.creates.setdefault(ph, []).append(now)
        self.creates.setdefault("agg:" + ah, []).append(now)
        if self.s.public:
            p = self.sb.prefix
            self._bucket("p:" + ph, p.create_per_h, p.create_burst, now, take=True)
            self._bucket("a:" + ah, p.agg_create_per_h, p.agg_create_burst, now, take=True)

    def pow_required(self, ph: str, ah: str, now: float) -> bool:
        if not self.s.public:
            return False
        p = self.sb.pow
        return self.queue_waiting() or len(self._window(ph, now)) >= p.prefix_creates_1h or \
            len(self._window("agg:" + ah, now)) >= p.agg_creates_1h

    def _pow_bits(self, ph: str, now: float) -> int:
        xs = [t for t in self.pow_triggers.get(ph, []) if now - t < HOUR_S]
        self.pow_triggers[ph] = xs
        return self.pow.difficulty(max(1, len(xs)))

    def challenge(self, addr: Any) -> dict[str, Any]:
        now = self.now()
        pfx, _ = self._prefixes(addr)
        ph = self._hash(pfx)
        c = self.pow.issue(pfx, ph, now, bits=self._pow_bits(ph, now))
        self.fx.changed()
        return ok(c.wire())

    def _active_count(self, h: str, *, agg: bool) -> int:
        return sum(1 for r in self.live() if (r.agg_prefix_h if agg else r.prefix_h) == h and r.tier != "internal")

    # ------------------------------------------------------------ 创建
    def _live_by(self, principal_id: str, ph: str) -> SessionRecord | None:
        """同一 principal 与前缀的未结束会话（含驻留：幂等创建返回它，由客户端恢复）。"""
        for r in self.sessions.values():
            if r.principal_id == principal_id and r.prefix_h == ph and (r.state in LIVE_STATES or r.state == PARKED) \
                    and r.state != DRAINING and r.drain_at is None:
                return r
        return None

    def _identity_count(self, sub: str) -> int:
        """该身份未结束的会话与排队中的内部票（每个身份同时 ≤ `internal.max_per_identity`）。"""
        n = sum(1 for r in self.sessions.values() if r.identity_sub == sub and r.state not in END_STATES
                and r.drain_at is None)
        return n + sum(1 for t in self.queue.open() if t.kind == "internal" and not t.sid
                       and ((t.create_args or {}).get("identity") or {}).get("sub") == sub)

    def _resolve_template(self, world: str, tid: Any, snapshot: Any, *, explicit: bool) -> tuple[str | None, dict | None, dict | None]:
        """(template_id, 模板文档, 错误回复)。"""
        if snapshot is not None:
            return None, None, None
        if tid is None or tid in EMPTY_TEMPLATES:
            return (None if tid is None and not explicit else tid), None, None
        doc = self.templates.get(world, str(tid))
        if doc is not None:
            return str(tid), doc, None
        if explicit:
            return None, None, err(422, 300, field="template", why="unknown_template", template=str(tid), world=world)
        log.warning("default template missing; empty scene", extra={"kv": {"world": world, "template": tid}})
        return str(tid), None, None

    def create(self, args: dict) -> dict[str, Any]:
        now = self.now()
        req = args.get("req") if isinstance(args.get("req"), dict) else {}
        pid = str(args.get("principal_id") or "")
        if not pid.startswith("p-"):
            return err(422, 300, field="principal_hint")
        pfx, agg = self._prefixes(args.get("addr"))
        ph, ah = self._hash(pfx), self._hash(agg)
        force_new = bool(req.get("force_new"))
        ident = args.get("identity") if isinstance(args.get("identity"), dict) else None
        internal = ident is not None and isinstance(ident.get("sub"), str) and isinstance(ident.get("jti"), str)
        cur = self._live_by(pid, ph)
        if cur is not None and not force_new:
            return ok({"session": self.wire(cur), "token": None, "idempotent": True}, 200, "existing")
        t = self.queue.by_principal(pid, ph)
        if t is not None:
            return self._queued_reply(t, rotate=True)
        if not self.accepting:
            return err(503, 502, why="maintenance", retry_after_s=60)
        if self.disk_full:
            self.fx.audit("spawn.refused", why="disk")
            return err(503, 502, why="disk", retry_after_s=60)
        world = str(req.get("world") or "synthcity")
        w = self.worlds.get(world)
        if w is None or not w.allowed:
            return err(409, 507, why="not_allowed", world=world)
        if not w.available:
            return err(409, 507, why="cache_missing", world=world)
        snap = req.get("config_snapshot")
        if snap is not None and req.get("template"):
            return err(422, 300, field="template", why="template_and_snapshot_exclusive")
        if snap is not None:
            bad = check_snapshot(snap, world=world, limits=self.s.limits_obj(), max_kib=self.sb.limits.snapshot_kib)
            if bad is not None:
                return err(422, 512, why=bad[0], **bad[1])
        explicit = "template" in req and req.get("template") is not None
        tid, tdoc, terr = self._resolve_template(world, req.get("template") if explicit else DEFAULT_TEMPLATE, snap,
                                                 explicit=explicit)
        if terr is not None:
            return terr
        n_max = self.n_max()
        create = {"principal_id": pid, "prefix_h": ph, "agg_prefix_h": ah, "req": {k: v for k, v in req.items() if k != "pow"},
                  "client": args.get("client") or "other", "template_id": tid}
        if internal:
            assert ident is not None
            if self._identity_count(ident["sub"]) >= self.s.internal.max_per_identity:
                return err(409, 504, why="per_identity", limit=self.s.internal.max_per_identity)
            create["identity"] = {"jti": ident["jti"], "sub": ident["sub"], "exp": ident.get("exp")}
            room = (self.free_reserved() > 0 or self.free_slots() > 0) and not self.cpu_blocked() and \
                not any(t.kind == "internal" for t in self.queue.waiting())
            if room and self.admit(world, tier="internal"):
                rec, tok = self._new_session(create, tdoc, now)
                return ok({"session": self.wire(rec), "token": tok[0], "token_exp_unix_ns": ns(rec.expires)}, 201, "created")
            return self._enqueue(create, world, tid, now, kind="internal")
        if self.s.public:
            if self._active_count(ph, agg=False) >= self.s.per_prefix_cap(n_max):
                self.fx.audit("quota.denied", prefix_h=ph, why="active_per_prefix")
                return err(409, 504, why="active_per_prefix", limit=self.s.per_prefix_cap(n_max))
            if self._active_count(ah, agg=True) >= self.s.per_agg_cap(n_max):
                self.fx.audit("quota.denied", prefix_h=ph, why="active_per_aggregate")
                return err(409, 504, why="active_per_aggregate", limit=self.s.per_agg_cap(n_max))
            if self.pow_required(ph, ah, now):
                why = self.pow.verify(req.get("pow"), pfx, ph, now)
                if why is not None:
                    self.pow_triggers.setdefault(ph, []).append(now)
                    c = self.pow.issue(pfx, ph, now, bits=self._pow_bits(ph, now))
                    if why != "required":
                        self.fx.audit("pow.failed", prefix_h=ph, why=why)
                    self.fx.changed()
                    return err(409, 509, why=why, challenge=c.wire())
        full = self.free_slots() <= 0 or self.cpu_blocked()
        if not full:
            if self.admit(world):
                if self.s.public:
                    wait = self._rate_wait(ph, ah, now)
                    if wait > 0:
                        self.fx.audit("quota.denied", prefix_h=ph, why="create_rate")
                        return err(429, 504, why="create_rate", retry_after_s=math.ceil(wait))
                rec, tok = self._new_session(create, tdoc, now)
                return ok({"session": self.wire(rec), "token": tok[0], "token_exp_unix_ns": ns(rec.expires)}, 201, "created")
            if not req.get("queue_ok"):
                return err(409, 508, available_now=self.startable_worlds(), queue_ok=True, world=world)
        return self._enqueue(create, world, tid, now)

    def _enqueue(self, create: dict, world: str, tid: str | None, now: float, *, kind: str = "new") -> dict[str, Any]:
        q = self.sb.queue
        if len(self.queue.open()) >= q.max and kind != "internal":
            return err(409, 500, why="queue_full", queue_len=len(self.queue.open()))
        if self.s.public and kind == "new" and self.queue.count_prefix(create["prefix_h"]) >= q.per_prefix:
            self.fx.audit("quota.denied", prefix_h=create["prefix_h"], why="ticket_per_prefix")
            return err(409, 504, why="ticket_per_prefix")
        tid_ = new_ticket_id()
        while self.queue.get(tid_) is not None:
            tid_ = new_ticket_id()
        tok = new_ticket_token()
        t = self.queue.add(Ticket(ticket_id=tid_, token_sha256=ticket_token_hash(tok), principal_id=create["principal_id"],
                                  prefix_h=create["prefix_h"], agg_prefix_h=create["agg_prefix_h"], world=world, template=tid,
                                  create_args=create, created=now, last_poll=now, kind=kind))
        self.fx.audit("queue.ticket", ticket_id=t.ticket_id, prefix_h=t.prefix_h, world=world, ticket_kind=kind)
        self.fx.changed()
        self._offer(now)
        return self._queued_reply(t, token=tok)

    def _queued_reply(self, t: Ticket, *, token: str | None = None, rotate: bool = False) -> dict[str, Any]:
        if rotate:   # 同一 principal 与前缀的幂等重复创建：票位置不变，换发票据令牌（池只存散列，无法回放原令牌）
            token = new_ticket_token()
            t.token_sha256 = ticket_token_hash(token)
            self.fx.changed()
        view = self.ticket_view(t)
        q = {"ticket_id": t.ticket_id, "ticket_token": token, "position": view["position"], "eta_s": view["eta_s"],
             "expires_unix_ns": ns(t.last_poll + self.sb.queue.ticket_idle_s)}
        if t.kind != "new":
            q["kind"] = t.kind
            if t.sid:
                q["sid"] = t.sid
        return ok({"queue": q, "available_now": self.startable_worlds(), "ticket": view}, 202, "queued")

    def _new_session(self, create: dict, tdoc: dict | None, now: float, *, sid: str | None = None) -> tuple[SessionRecord, tuple[str, dict]]:
        req = create["req"]
        world = str(req.get("world") or "synthcity")
        w = self.worlds.get(world)
        while sid is None or sid in self.sessions:
            sid = new_sid()
        seed = req.get("seed")
        seed = int(seed) if isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed < 2**32 else secrets.randbelow(2**32)
        lt = self.sb.lifetime
        ident = create.get("identity") if isinstance(create.get("identity"), dict) else None
        tier = "internal" if ident is not None else "public"
        snap = req.get("config_snapshot")
        cfg_doc = build_session_config(world, template_id=create.get("template_id"), template=tdoc, snapshot=snap,
                                       calendar=req.get("calendar"), acoustic_bg=req.get("acoustic_bg"), seed=seed,
                                       limits=self.s.limits_obj(), tz=w.tz if w else None, now=now)
        rec = SessionRecord(sid=sid, state=SPAWNING, world=world, world_class=w.tier if w else "S",
                            template=create.get("template_id") if snap is None else None, principal_id=create["principal_id"],
                            prefix_h=create["prefix_h"], agg_prefix_h=create["agg_prefix_h"],
                            client=str((req.get("client") or {}).get("kind") or create.get("client") or "other")
                            if isinstance(req.get("client"), dict) else str(create.get("client") or "other"),
                            created=now, expires=now + lt.initial_s,
                            max_expires=now + (self.s.internal.lifetime_max_s if tier == "internal" else lt.max_s),
                            last_activity=now, c_i=w.cost.c0 if w else 0.15, calendar=cfg_doc.get("calendar"),
                            acoustic_bg=cfg_doc.get("acoustic_bg"), seed=seed, tier=tier, held_since=now, last_input=now)
        if ident is not None:
            rec.identity_jti, rec.identity_sub = str(ident["jti"]), str(ident["sub"])
            rec.identity_exp = float(ident["exp"]) if isinstance(ident.get("exp"), (int, float)) else None
        rec.rate_cap = governor.rate_cap(rec.c_i, k=self.sb.rate_cap_k, hard=max(self.sb.rate_tiers))
        self.sessions[sid] = rec
        self._count_create(rec.prefix_h, rec.agg_prefix_h, now)
        tok = self.keys.issue(rec.principal_id, sid, rec.expires, tier=tier)
        self.fx.spawn(rec, cfg_doc)
        self.fx.event("sandbox.state", sid, {"from": None, "to": SPAWNING, "reason": "create", "epoch": rec.epoch})
        self.fx.audit("session.create", sid=sid, world=world, template=rec.template, prefix_h=rec.prefix_h, client=rec.client,
                      tier=tier)
        if tier == "internal":
            self.fx.audit("sandbox.internal_session", sid=sid, action="create", jti=rec.identity_jti,
                          reserved=self.n_internal() <= self.reserved_slots())
        self.fx.changed()
        return rec, tok

    # ------------------------------------------------------------ 排队票
    def queue_op(self, args: dict) -> dict[str, Any]:
        now = self.now()
        op = str(args.get("op") or "get")
        t = self.queue.get(str(args.get("ticket_id") or ""))
        if t is None:
            return err(410, 511, why="unknown")
        if not ticket_token_ok(args.get("ticket_token"), t.token_sha256):
            return err(401, 302, why="ticket_token")
        if op == "get":
            if t.state in (WAITING, OFFERED):
                t.last_poll = now
            return ok(self.ticket_view(t))
        if op == "leave":
            if t.state in (WAITING, OFFERED):
                was_offered = t.state == OFFERED
                self.queue.close(t, LEFT, now)
                self.fx.ticket_changed(t)
                self.fx.changed()
                if was_offered:
                    self._offer(now)
            return ok(None, 204)
        if op == "claim":
            if t.state == CLAIMED and t.sid in self.sessions:
                return ok({"session": self.wire(self.sessions[t.sid]), "token": None, "idempotent": True}, 200, "existing")
            if t.state == WAITING:
                t.last_poll = now
                return err(409, 105, why="NOT_READY", position=self.queue.position(t))
            if t.state != OFFERED:
                return err(410, 511, why="expired")
            hint = args.get("principal_id")
            if hint and hint != t.principal_id:
                return err(410, 511, why="principal")
            if t.kind == "resume" or (t.kind == "internal" and t.sid):
                rec = self.sessions.get(t.sid or "")
                self.queue.close(t, CLAIMED, now)
                self.fx.ticket_changed(t)
                if rec is None or rec.state != PARKED or rec.drain_at is not None:
                    self.fx.changed()
                    return err(410, 511, why="session_gone")
                return self._resume_spawn(rec, now)
            self.queue.close(t, CLAIMED, now)
            create = dict(t.create_args)
            w = self.worlds.get(t.world)
            if w is None or not w.available or not self.accepting:
                self.fx.changed()
                return err(503, 502, why="maintenance" if not self.accepting else "world_unavailable", retry_after_s=30)
            tdoc = self.templates.get(t.world, t.template) if t.template and t.template not in EMPTY_TEMPLATES else None
            rec, tok = self._new_session(create, tdoc, now)
            t.sid = rec.sid
            self.fx.ticket_changed(t)
            return ok({"session": self.wire(rec), "token": tok[0], "token_exp_unix_ns": ns(rec.expires)}, 201, "created")
        return err(422, 300, field="op")

    def _offer(self, now: float) -> None:
        """空位按队列顺序转给第一张世界可准入的 WAITING 票（M17 §6.6.2）；内部票在前，可用预留槽位或公开空位。"""
        if self.cpu_blocked() or not self.accepting:
            return
        while True:
            t = None
            for x in self.queue.waiting():
                if x.kind == "internal":
                    if (self.free_reserved() > 0 or self.free_slots() > 0) and self.admit(x.world, tier="internal"):
                        t = x
                        break
                    continue
                if self.free_slots() > 0 and self.admit(x.world):
                    t = x
                    break
            if t is None:
                return
            t.state = OFFERED
            t.offer_deadline = now + self.sb.queue.offer_claim_s
            self.fx.ticket_changed(t)
            self.fx.audit("queue.offer", ticket_id=t.ticket_id, world=t.world, ticket_kind=t.kind)
            self.fx.changed()

    # ------------------------------------------------------------ 会话操作
    def get(self, sid: str) -> dict[str, Any]:
        rec = self.sessions.get(sid)
        if rec is None:
            return err(404, 305, sid=sid)
        return ok(self.wire(rec))

    def list(self) -> dict[str, Any]:
        return ok({"sessions": [self.wire(r) for r in self.sessions.values()], "n_max": self.n_max(),
                   "queue_len": len(self.queue.waiting())})

    def _live_rec(self, sid: str, states: tuple[str, ...] = (READY, ACTIVE, IDLE)) -> tuple[SessionRecord | None, dict | None]:
        rec = self.sessions.get(sid)
        if rec is None:
            return None, err(404, 305, sid=sid)
        if rec.state in END_STATES:
            return None, err(410, 510 if rec.end_reason == "preempted" else 506, end_reason=wire_end_reason(rec.end_reason),
                             snapshot_available=bool(rec.snapshot_path))
        if rec.state in (PARKING, PARKED):
            return None, err(409, 515, state=rec.state, resume_url=SESSION_ROUTE + sid + "/resume")
        if rec.state not in states:
            return None, err(409, 513, state=rec.state)
        return rec, None

    def keepalive(self, sid: str) -> dict[str, Any]:
        now = self.now()
        rec, e = self._live_rec(sid)
        if rec is None:
            return e  # type: ignore[return-value]
        self._touch(rec, now)
        block = self.renew_block(rec)
        if block is not None:
            return ok({"session": self.wire(rec), "renewed": False, "token": None, "reason": block})
        rec.expires = min(rec.max_expires, max(rec.expires, now + self.sb.lifetime.renew_step_s))
        rec.renewals += 1
        if rec.end_reason == "lifetime" and rec.drain_at is not None:
            rec.end_reason = rec.drain_at = rec.notice_reason = None
        tok, _ = self.keys.issue(rec.principal_id, sid, rec.expires)
        self.fx.changed()
        return ok({"session": self.wire(rec), "renewed": True, "token": tok, "token_exp_unix_ns": ns(rec.expires)})

    def stop(self, sid: str, reason: str = "user") -> dict[str, Any]:
        rec = self.sessions.get(sid)
        if rec is None:
            return err(404, 305, sid=sid)
        if rec.state in END_STATES or rec.state == DRAINING:
            return ok({"state": rec.state, "session": self.wire(rec)}, 202)
        if rec.state == PARKED:
            self._end_parked(rec, reason if reason in ("user", "maintenance") else "user")
            return ok({"state": DRAINING, "session": self.wire(rec)}, 202)
        if rec.state == PARKING:
            rec.end_after_park = True
            self.fx.changed()
            return ok({"state": DRAINING, "session": self.wire(rec)}, 202)
        self.schedule_end(rec, reason if reason in ("user", "maintenance") else "user", 0.0)
        return ok({"state": DRAINING, "session": self.wire(rec)}, 202)

    def rate(self, sid: str, rate: Any) -> dict[str, Any]:
        rec, e = self._live_rec(sid, (READY, ACTIVE, IDLE))
        if rec is None:
            return e  # type: ignore[return-value]
        try:
            r = float(rate)
        except (TypeError, ValueError):
            return err(422, 300, field="rate")
        tiers = self.sb.rate_tiers
        if r not in tiers:
            return err(422, 300, field="rate", allowed=list(tiers))
        others = sum(x.c_i * x.granted_rate for x in self.live() if x is not rec)
        g = governor.grant(r, rec.c_i, others, self.s.b_sim(self.n_live()), tiers, k=self.sb.rate_cap_k, manual=rec.manual,
                           l2=self.level >= 2)
        self._set_rate(rec, g.granted, g.limited_by, requested=r, why="grant", code=g.code, cap=g.cap)
        self.fx.audit("rate.grant", sid=sid, requested=r, granted=g.granted, cap=round(g.cap, 3), limited_by=g.limited_by)
        return ok({"granted": g.granted, "cap": round(g.cap, 3), "limited_by": g.limited_by, "code": g.code,
                   "requested": r})

    def _regrant(self, rec: SessionRecord) -> None:
        """首个实测 c_i 到达时按实测值重新授予（D2-FX-R2，D2-AC-07）：就绪后 10 s 内的申请按世界成本表的先验 c0 授予（满载会话的
        实测值，小机群会话因此被限到 ×1 或 ×2），实测更低时向上补授予，不超过申请值；只在受限原因为 cap 或 budget、未降级、
        未手动控制且时钟在播放时进行（实测回调降档 `measured` 与 L2 之后不回升）。"""
        if rec.state not in (READY, ACTIVE, IDLE) or rec.manual or self.level >= 2 or rec.clock_state != "PLAYING":
            return
        if rec.requested_rate <= rec.granted_rate + 1e-9 or rec.limited_by not in ("cap", "budget"):
            return
        tiers = self.sb.rate_tiers
        others = sum(x.c_i * x.granted_rate for x in self.live() if x is not rec)
        g = governor.grant(rec.requested_rate, rec.c_i, others, self.s.b_sim(self.n_live()), tiers, k=self.sb.rate_cap_k,
                           manual=rec.manual, l2=False)
        if g.granted > rec.granted_rate + 1e-9:
            self._set_rate(rec, g.granted, g.limited_by, why="regrant", code=g.code, cap=g.cap)
            self.fx.clock(rec, "speed", {"rate": g.granted})
            self.fx.audit("rate.grant", sid=rec.sid, requested=rec.requested_rate, granted=g.granted, cap=round(g.cap, 3),
                          limited_by=g.limited_by, why="regrant")

    def _set_rate(self, rec: SessionRecord, granted: float, limited_by: str | None, *, requested: float | None = None,
                  why: str, code: int = 0, cap: float | None = None) -> None:
        now = self.now()
        if requested is not None:
            rec.requested_rate = float(requested)
        if cap is not None:
            rec.rate_cap = float(cap)
        changed = abs(rec.granted_rate - granted) > 1e-9 or rec.limited_by != limited_by
        rec.granted_rate = float(granted)
        rec.limited_by = limited_by
        rec.recheck_at = now + self.sb.recheck_s if granted > 1.0 else None
        rec.grant_window_until = now + self.sb.recheck_s
        if changed or why == "grant":
            self.fx.event("sandbox.rate", rec.sid, {"requested": rec.requested_rate, "granted": rec.granted_rate,
                                                    "cap": round(rec.rate_cap, 3), "limited_by": governor.wire_limited_by(limited_by),
                                                    "code": code or (501 if limited_by else 0), "why": why})
        self.fx.changed()

    def world_switch(self, sid: str, world: str, template: Any = None) -> dict[str, Any]:
        now = self.now()
        rec, e = self._live_rec(sid)
        if rec is None:
            return e  # type: ignore[return-value]
        if rec.switch is not None or rec.drain_at is not None:
            return err(409, 513, state=rec.state, why="busy")
        w = self.worlds.get(world)
        if w is None or not w.allowed:
            return err(409, 507, why="not_allowed", world=world)
        if not w.available:
            return err(409, 507, why="cache_missing", world=world)
        if world == rec.world:
            return self.template(sid, template or rec.template or "empty")
        if not self.admit(world, replace=rec.world):
            return err(409, 508, available_now=self.startable_worlds(), queue_ok=False, world=world)
        explicit = template is not None
        tid, tdoc, terr = self._resolve_template(world, template if explicit else (rec.template or DEFAULT_TEMPLATE), None,
                                                 explicit=explicit)
        if terr is not None:
            return terr
        cfg_doc = build_session_config(world, template_id=tid, template=tdoc, snapshot=None, calendar=None,
                                       acoustic_bg=None, seed=rec.seed, limits=self.s.limits_obj(), tz=w.tz, now=now)
        prev = rec.state
        rec.switch = {"from_world": rec.world, "from_class": rec.world_class, "from_template": rec.template,
                      "from_state": prev, "rollback": False}
        rec.world, rec.world_class, rec.template = world, w.tier, tid
        rec.calendar = cfg_doc.get("calendar")
        rec.state, rec.ready_at, rec.epoch = SPAWNING, None, rec.epoch + 1
        rec.granted_rate, rec.requested_rate, rec.limited_by = 1.0, 1.0, None
        rec.c_i, rec.c_i_measured = w.cost.c0, False
        self.fx.switch(rec, cfg_doc)
        self.fx.event("sandbox.state", sid, {"from": prev, "to": SPAWNING, "reason": "world_switch", "epoch": rec.epoch,
                                             "world_id": world})
        self.fx.audit("world.switch", sid=sid, world=world, from_world=rec.switch["from_world"])
        self.fx.changed()
        return ok({"session": self.wire(rec), "state": SPAWNING, "world": world, "template": tid}, 202)

    def template(self, sid: str, template: Any) -> dict[str, Any]:
        now = self.now()
        rec, e = self._live_rec(sid)
        if rec is None:
            return e  # type: ignore[return-value]
        if not isinstance(template, str) or not template:
            return err(422, 300, field="template")
        tid, tdoc, terr = self._resolve_template(rec.world, template, None, explicit=True)
        if terr is not None:
            return terr
        w = self.worlds.get(rec.world)
        cfg_doc = build_session_config(rec.world, template_id=tid, template=tdoc, snapshot=None, calendar=None,
                                       acoustic_bg=None, seed=rec.seed, limits=self.s.limits_obj(), tz=w.tz if w else None,
                                       now=now)
        return self._reset(rec, cfg_doc, "template", template=tid)

    def restore(self, sid: str, snapshot: Any) -> dict[str, Any]:
        rec, e = self._live_rec(sid)
        if rec is None:
            return e  # type: ignore[return-value]
        bad = check_snapshot(snapshot, world=rec.world, limits=self.s.limits_obj(), max_kib=self.sb.limits.snapshot_kib)
        if bad is not None:
            return err(422, 512, why=bad[0], **bad[1])
        w = self.worlds.get(rec.world)
        cfg_doc = build_session_config(rec.world, template_id=None, template=None, snapshot=snapshot, calendar=None,
                                       acoustic_bg=snapshot.get("acoustic_bg"), seed=int(snapshot.get("seed") or rec.seed),
                                       limits=self.s.limits_obj(), tz=w.tz if w else None, now=self.now())
        return self._reset(rec, cfg_doc, "restore", template=None)

    def _reset(self, rec: SessionRecord, cfg_doc: dict, reason: str, *, template: str | None) -> dict[str, Any]:
        self._touch(rec, self.now())
        rec.epoch += 1
        rec.template = template
        rec.calendar = cfg_doc.get("calendar") or rec.calendar
        self.fx.reset(rec, cfg_doc, reason)
        self.fx.event("sandbox.state", rec.sid, {"from": rec.state, "to": rec.state, "reason": reason, "epoch": rec.epoch})
        self.fx.changed()
        return ok({"session": self.wire(rec), "epoch": rec.epoch, "template": template}, 202)

    def activity(self, items: Any) -> dict[str, Any]:
        """`sys/session/activity{items: {sid: {ws_n, api_calls, last_api_unix_ns}}}`（M17-FR-042；WP-03 约定）。"""
        now = self.now()
        if isinstance(items, list):  # PRD §7.5 的列表写法
            items = {str(i.get("sid")): i for i in items if isinstance(i, dict)}
        if not isinstance(items, dict):
            return err(422, 300, field="items")
        for sid, it in items.items():
            rec = self.sessions.get(sid)
            if rec is None or rec.state not in LIVE_STATES or not isinstance(it, dict):
                continue
            ws = int(it.get("ws_n", it.get("ws", 0)) or 0)
            calls = int(it.get("api_calls", 0) or 0)
            rec.ws = ws
            li = _unix_s(it.get("last_input_unix_ns"))
            if li is not None:
                rec.last_input = max(rec.last_input, min(now, li))
            if calls > 0:
                la = _unix_s(it.get("last_api_unix_ns"))
                rec.last_input = max(rec.last_input, min(now, la) if la is not None else now)
            if ws > 0 or calls > 0:
                self._touch(rec, now)
        return ok({"ok": True})

    def _touch(self, rec: SessionRecord, now: float) -> None:
        rec.last_activity = now
        if rec.park_pending == "idle_disconnected":
            self.cancel_park(rec)
        if rec.state == READY:
            self._set_state(rec, ACTIVE, "claimed")
            if not rec.played:
                rec.played = True
                if not rec.resuming:              # 恢复中：核对 checkpoint 是否生效后再播放（on_resumed）
                    rec.clock_state = "PLAYING"
                    self.fx.clock(rec, "play", {})
        elif rec.state == IDLE:
            rec.idle_since = None
            self._set_state(rec, ACTIVE, "activity")

    def _set_state(self, rec: SessionRecord, to: str, reason: str | None = None) -> None:
        if rec.state == to:
            return
        prev = rec.state
        rec.state = to
        data = {"from": prev, "to": to, "epoch": rec.epoch}
        if reason:
            data["reason"] = reason
        self.fx.event("sandbox.state", rec.sid, data)
        self.fx.changed()

    # ------------------------------------------------------------ 结束
    def schedule_end(self, rec: SessionRecord, reason: str, notice_s: float) -> None:
        now = self.now()
        if rec.state in END_STATES or rec.state == DRAINING:
            return
        at = now + max(0.0, notice_s)
        if rec.drain_at is not None and rec.drain_at <= at:
            return
        rec.end_reason = reason
        rec.notice_reason = reason
        rec.drain_at = at
        if notice_s > 0:
            data = {"reason": wire_end_reason(reason), "in_s": round(notice_s, 1), "at_unix_ns": ns(at),
                    "end_unix_ns": ns(at)}
            if reason == "preempted":
                data["code"] = 510
            self.fx.event("sandbox.expiring", rec.sid, data, severity=2)
            self.fx.changed()
        else:
            self._drain(rec)

    def _drain(self, rec: SessionRecord) -> None:
        if rec.state == DRAINING or rec.state in END_STATES:
            return
        if rec.end_reason is None:
            rec.end_reason = "user"
        if rec.notice_reason is None and rec.end_reason in ("user", "idle", "unclaimed", "maintenance"):
            self.fx.event("sandbox.expiring", rec.sid, {"reason": wire_end_reason(rec.end_reason), "in_s": 0,
                                                        "at_unix_ns": ns(self.now())}, severity=2)
        rec.drain_at = self.now()
        was = rec.state
        self._set_state(rec, DRAINING, wire_end_reason(rec.end_reason))
        self.fx.audit("session.end", sid=rec.sid, reason=rec.end_reason, held_s=round(self.now() - (rec.ready_at or rec.created), 1))
        self.fx.stop(rec, export=was != SPAWNING and rec.end_reason not in ("failed", "spawn_failed", "world_switch_failed"))

    def on_ended(self, sid: str, snapshot_path: str | None) -> None:
        """池完成 DRAINING：快照已导出（或 None）、进程已退出、目录与叶子组已删除。"""
        rec = self.sessions.get(sid)
        if rec is None or rec.state in END_STATES:
            return
        now = self.now()
        rec.snapshot_path = snapshot_path
        rec.ended_at = now
        rec.pid = rec.pid_starttime = None
        final = FAILED if rec.end_reason in ("failed", "spawn_failed", "world_switch_failed", "crash") else ENDED
        self._set_state(rec, final, wire_end_reason(rec.end_reason))
        self._offer(now)

    # ------------------------------------------------------------ 进程回调
    def on_spawned(self, sid: str, pid: int, starttime: int | None) -> None:
        rec = self.sessions.get(sid)
        if rec is None:
            return
        rec.pid, rec.pid_starttime, rec.spawn_at = pid, starttime, self.now()
        self.fx.changed()

    def on_ready(self, sid: str) -> None:
        rec = self.sessions.get(sid)
        if rec is None:
            return
        now = self.now()
        if rec.state == SPAWNING:
            rec.ready_at = now
            rec.last_activity = now
            rec.played = False
            rec.clock_state = "STOPPED"
            reason = "world_switch" if rec.switch is not None else ("resume" if rec.resuming else "ready")
            rec.switch = None
            self._set_state(rec, READY, reason)
        elif rec.state in (ACTIVE, IDLE, READY) and rec.played:
            self.fx.clock(rec, "play", {})    # 崩溃重启后恢复播放（checkpoint 恢复的时钟按 ×1）
            if rec.granted_rate != 1.0:
                self._set_rate(rec, 1.0, None, why="restart")
        self.fx.changed()

    def on_spawn_failed(self, sid: str, why: str) -> None:
        rec = self.sessions.get(sid)
        if rec is None or rec.state in END_STATES:
            return
        self.fx.audit("spawn.failed", sid=sid, why=why, world=rec.world)
        if rec.resuming and self.on_resume_failed(sid, why):
            return
        sw = rec.switch
        if sw is not None and not sw.get("rollback"):   # 切换底图失败：按原世界与切换前快照恢复（M17-FR-010）
            sw["rollback"] = True
            w = self.worlds.get(sw["from_world"])
            rec.world, rec.world_class, rec.template = sw["from_world"], sw["from_class"], sw["from_template"]
            cfg = sw.get("backup_cfg") or build_session_config(rec.world, template_id=rec.template, template=None,
                                                                snapshot=None, calendar=rec.calendar, acoustic_bg=rec.acoustic_bg,
                                                                seed=rec.seed, limits=self.s.limits_obj(),
                                                                tz=w.tz if w else None, now=self.now())
            rec.epoch += 1
            self.fx.switch(rec, cfg)
            self.fx.event("sandbox.state", sid, {"from": SPAWNING, "to": SPAWNING, "reason": "world_switch_rollback",
                                                 "epoch": rec.epoch, "world_id": rec.world})
            self.fx.changed()
            return
        rec.end_reason = "world_switch_failed" if sw is not None else "spawn_failed"
        rec.switch = None
        self._drain(rec)

    def on_exit(self, sid: str, code: int | None) -> None:
        """进程非预期退出（或被看门狗终止）。DRAINING 中的退出由池的停止流程处理，不经此处。"""
        rec = self.sessions.get(sid)
        if rec is None or rec.state in END_STATES or rec.state == DRAINING:
            return
        now = self.now()
        rec.pid = rec.pid_starttime = None
        if rec.state == SPAWNING:
            self.on_spawn_failed(sid, f"exit:{code}")
            return
        sp = self.sb.spawn
        rec.crashes = [t for t in rec.crashes if now - t < sp.crash_window_s] + [now]
        self.fx.audit("session.crash", sid=sid, code=code, n=len(rec.crashes))
        if len(rec.crashes) >= sp.max_crashes:
            rec.end_reason = "failed"
            self._drain(rec)
            return
        rec.restarts += 1
        rec.epoch += 1
        self.fx.spawn(rec, None, restart=True)
        self.fx.event("sandbox.state", sid, {"from": rec.state, "to": rec.state, "reason": "crash_restart", "epoch": rec.epoch})
        self.fx.changed()

    def on_reset(self, sid: str, code: int, problems: list | None = None) -> None:
        rec = self.sessions.get(sid)
        if rec is None:
            return
        if code:
            self.fx.audit("session.reset_failed", sid=sid, code=code)
        self.fx.changed()

    def on_perf(self, sid: str, perf: dict) -> None:
        """`state/sim-core/perf`（M08-FR-224）：c_i、实际倍速、匿名页、手动锁、机体与识别物数。"""
        rec = self.sessions.get(sid)
        if rec is None or not isinstance(perf, dict):
            return
        now = self.now()
        c = perf.get("cost_core_per_rate")
        if isinstance(c, (int, float)) and c > 0 and now - (rec.ready_at or now) >= 10.0:
            first = not rec.c_i_measured
            rec.c_i = float(c)
            rec.c_i_measured = True
            rec.rate_cap = governor.rate_cap(rec.c_i, k=self.sb.rate_cap_k, hard=max(self.sb.rate_tiers))
            if first:
                self._regrant(rec)
        r = perf.get("rtf_win")
        if isinstance(r, (int, float)):
            rec.rtf = float(r) / max(rec.granted_rate, 1e-6) if rec.clock_state == "PLAYING" else 1.0
            rec.usage["rate_achieved"] = float(r)
        a = perf.get("rss_anon_mb")
        if isinstance(a, (int, float)):
            rec.anon_mb = float(a)
        rec.manual = perf.get("clock_lock") == "manual"
        for k, key in (("n_uav", "vehicles"), ("n_target", "targets"), ("n_nest", "nests"), ("n_task_active", "tasks_active")):
            if isinstance(perf.get(k), int):
                rec.usage[key] = int(perf[k])

    def on_ring(self, sid: str, clock_state: str | None, rate: float | None) -> None:
        rec = self.sessions.get(sid)
        if rec is None:
            return
        if clock_state:
            rec.clock_state = clock_state
            if clock_state == "PLAYING":
                rec.played = True

    # ------------------------------------------------------------ 1 Hz
    def tick(self, readings: LadderInput | None = None) -> None:
        now = self.now()
        sb = self.sb
        for t in self.queue.expire(now, idle_s=sb.queue.ticket_idle_s):
            self.fx.ticket_changed(t)
            self.fx.audit("queue.expired", ticket_id=t.ticket_id)
            self.fx.changed()
        self.queue.purge(now)
        for rec in list(self.sessions.values()):
            if rec.state in END_STATES:
                if rec.ended_at is not None and now - rec.ended_at > sb.snapshot_keep_s:
                    del self.sessions[rec.sid]
                    self.fx.changed()
                continue
            if rec.state == READY and now - (rec.ready_at or now) >= sb.idle.ready_claim_s and rec.last_activity <= (rec.ready_at or now):
                self.schedule_end(rec, "unclaimed", 0.0)
                continue
            if rec.state == PARKED:
                continue
            if rec.state == ACTIVE and rec.ws == 0 and now - rec.last_activity >= sb.idle.enter_s:
                rec.idle_since = rec.last_activity
                self._set_state(rec, IDLE, "no_activity")
            if rec.state in (READY, ACTIVE, IDLE) and rec.drain_at is None and now >= rec.expires - sb.notice_s:
                self.schedule_end(rec, "lifetime", max(0.0, rec.expires - now))
            if rec.drain_at is not None and now >= rec.drain_at and rec.state not in (DRAINING, SPAWNING):
                self._drain(rec)
                continue
            if rec.recheck_at is not None and now >= rec.recheck_at and rec.granted_rate > 1.0 and rec.clock_state == "PLAYING":
                new = governor.recheck(rec.granted_rate, float(rec.usage.get("rate_achieved", rec.granted_rate)), sb.rate_tiers)
                rec.recheck_at = now + sb.recheck_s
                if new is not None:
                    self._set_rate(rec, new, "measured", why="callback", code=501)
                    self.fx.clock(rec, "speed", {"rate": new})
            if not self.delegated and rec.anon_mb > sb.cgroup.rss_watchdog_mb and rec.state in (READY, ACTIVE, IDLE):
                self.fx.audit("session.rss_watchdog", sid=rec.sid, anon_mb=rec.anon_mb)
                self.fx.kill(rec, "rss_watchdog")
        self.power_tick(now)
        self._ladder(readings, now)
        self._offer(now)
        self._maybe_preempt(now)
        self.pow.prune(now)
        for k in [k for k in list(self.creates) if not self._window(k, now)]:
            self.creates.pop(k, None)
        for k in [k for k, (tok, t) in self.buckets.items() if now - t > 2 * HOUR_S]:
            self.buckets.pop(k, None)

    def _ladder(self, readings: LadderInput | None, now: float) -> None:
        inp = readings or LadderInput(now=now)
        inp.now = now
        live = [r for r in self.live() if r.state in (READY, ACTIVE, IDLE)]
        b_sim = self.s.b_sim(self.n_live())
        inp.sessions = [SessionLoad(r.sid, r.c_i, r.granted_rate, r.rtf, r.anon_mb,
                                    self.worlds.costs[r.world].anon_mb if r.world in self.worlds.costs else 140.0, r.ws,
                                    r.state == IDLE, now < r.grant_window_until or r.clock_state != "PLAYING") for r in live]
        inp.budget_exhausted = sum(r.c_i * r.granted_rate for r in live) > b_sim + 1e-9 and any(r.granted_rate > 1 for r in live)
        inp.full = self.n_live() >= self.n_max() or bool(self.queue.waiting())
        prev = self.level
        level = self.ladder.tick(inp)
        self.level = level
        if level <= 1 and self.reclaim_hold:
            self.reclaim_hold = False
            self.fx.audit("ladder.reclaim_hold", on=False)
            self.fx.changed()
        if level != prev:
            lg = log.warning if max(level, prev) >= 2 else log.info
            lg("ladder level", extra={"kv": {"from": prev, "to": level, **{k: v for k, v in self.ladder.last.items()
                                                                            if v is not None}}})
            self.fx.audit("ladder.enter" if level > prev else "ladder.exit", level=level, prev=prev)
            self.fx.changed()
        if level >= 2:
            for r in live:
                if r.granted_rate > 1.0:
                    self._set_rate(r, 1.0, "l2", why="degrade", code=501)
                    self.fx.clock(r, "speed", {"rate": 1.0})
        if level >= 3 and now - self.last_reclaim >= self.sb.ladder.reclaim_interval_s and \
                not any(r.park_pending in ("reclaimed_cpu", "reclaimed_mem") or r.state == PARKING for r in self.live()):
            cands = [x for x in inp.sessions if self.sessions[x.sid].drain_at is None and self.sessions[x.sid].park_at is None]
            pub = [x for x in cands if self.sessions[x.sid].tier != "internal"]
            cands = pub or cands          # 内部档排在最后
            if cands:
                v = max(cands, key=lambda x: reclaim_score(x, level, b_sim, len(live)))
                reason = "reclaimed_mem" if level >= 4 else "reclaimed_cpu"
                self.schedule_park(self.sessions[v.sid], reason, self.sb.notice_s)
                if not self.reclaim_hold:
                    self.reclaim_hold = True
                    self.fx.audit("ladder.reclaim_hold", on=True)
                self.fx.audit("session.reclaim", sid=v.sid, level=level, score=round(reclaim_score(v, level, b_sim, len(live)), 3))
                self.last_reclaim = now

    def _maybe_preempt(self, now: float) -> None:
        """队列中有公开票等待且无空位：持有最长且超过阈值的公开会话提前告知后驻留（D2.1：抢占改为驻留，可排队恢复）。"""
        if not any(t.kind != "internal" for t in self.queue.waiting()) or self.free_slots() > 0:
            return
        if any(r.park_pending == "preempted" or (r.state == PARKING and r.park_reason == "preempted") for r in self.live()):
            return

        def held_s(r: SessionRecord) -> float:
            return now - (r.held_since or r.ready_at or r.created)

        held = [r for r in self.live() if r.state in (READY, ACTIVE, IDLE) and r.drain_at is None and r.park_at is None
                and r.tier != "internal" and held_s(r) > self.sb.queue.preempt_after_s]
        if not held:
            return
        v = max(held, key=held_s)
        self.schedule_park(v, "preempted", self.sb.notice_s)
        self.fx.audit("session.preempt", sid=v.sid, held_s=round(held_s(v), 1), action="park")

    # ------------------------------------------------------------ P1：指挥器配对与内存额度预留
    def pair(self, args: dict) -> dict[str, Any]:
        """指挥器配对（M17-FR-081；AWR-04 §15.8.2 第 4 条）：`op = code`（SB-76，会话 token）签发 8 位 base32 单次配对码
        （120 s，每会话 10 min 内 ≤ 5 次）；`op = redeem`（SB-77，公开）核对配对码并为同一 principal 签发沙盒 token（共享席位），
        错误 5 次后该码作废。配对码只存 SHA-256。"""
        now = self.now()
        sid = str(args.get("sid") or "")
        op = str(args.get("op") or "")
        rec = self.sessions.get(sid)
        if rec is None:
            return err(404, 305, sid=sid)
        if rec.state in END_STATES:
            return err(410, 506, end_reason=wire_end_reason(rec.end_reason))
        if op == "code":
            rec.pair_issued = [t for t in rec.pair_issued if now - t < 600.0]
            if len(rec.pair_issued) >= PAIR_PER_10MIN:
                return err(429, 504, why="pair_rate", retry_after_s=math.ceil(600.0 - (now - rec.pair_issued[0])))
            code = new_pair_code()
            rec.pair = {"sha256": hashlib.sha256(code.encode()).hexdigest(), "exp": now + PAIR_TTL_S, "fails": 0}
            rec.pair_issued.append(now)
            self.fx.audit("session.pair_code", sid=sid)
            self.fx.changed()
            return ok({"code": code, "expires_unix_ns": ns(now + PAIR_TTL_S)}, 201)
        if op == "redeem":
            p = rec.pair
            code = str(args.get("code") or "").strip().upper()
            if not p or now >= float(p.get("exp") or 0):
                return err(410, 511, why="pair_expired")
            if not hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), str(p.get("sha256"))):
                p["fails"] = int(p.get("fails") or 0) + 1
                if p["fails"] >= PAIR_MAX_FAILS:
                    rec.pair = None
                self.fx.audit("session.pair_denied", sid=sid)
                self.fx.changed()
                return err(401, 302, why="pair_code")
            rec.pair = None
            tok, _ = self.keys.issue(rec.principal_id, sid, rec.expires if rec.state != PARKED else
                                     now + float(rec.remaining_s or 0.0), tier=rec.tier)
            self.fx.audit("session.paired", sid=sid)
            self.fx.changed()
            return ok({"session": self.wire(rec), "token": tok, "token_exp_unix_ns": ns(rec.expires)}, 201)
        return err(422, 300, field="op")

    def reserve(self, mem_mb: Any, ttl_s: Any) -> dict[str, Any]:
        """`sys/session/reserve{mem_mb, ttl_s}`（M17-FR-082）：临时下调沙盒组内存额度；当前占用使下调后超额时拒绝；0 撤销。"""
        now = self.now()
        try:
            mb, ttl = max(0.0, float(mem_mb)), max(0.0, min(3600.0, float(ttl_s)))
        except (TypeError, ValueError):
            return err(422, 300, field="mem_mb")
        if mb <= 0 or ttl <= 0:
            self.mem_reserved_mb, self.mem_reserved_until = 0.0, 0.0
            self.fx.audit("pool.reserve", mem_mb=0)
            self.fx.changed()
            return ok({"ok": True, "mem_sb_mb": self.m_sb()})
        from .admission import memory_mb

        used = memory_mb(self.worlds.costs, self.live_worlds())
        if used > float(self.sb.mem_sandbox_mb) - mb:
            return ok({"ok": False, "why": "insufficient", "used_mb": round(used, 1), "mem_sb_mb": self.m_sb()})
        self.mem_reserved_mb, self.mem_reserved_until = mb, now + ttl
        self.fx.audit("pool.reserve", mem_mb=mb, ttl_s=ttl)
        self.fx.changed()
        return ok({"ok": True, "mem_sb_mb": self.m_sb(), "until_unix_ns": ns(self.mem_reserved_until)})

    # ------------------------------------------------------------ 公开查询
    def meta(self, addr: Any = None) -> dict[str, Any]:
        now = self.now()
        feats = ["queue"]
        if self.s.public:
            feats.append("pow")
        if self.sb.batch.enabled and not self.s.public:
            feats.append("batch")
        req_now = False
        bits = self.sb.pow.bits
        if self.s.public:
            pfx, agg = self._prefixes(addr)
            ph, ah = self._hash(pfx), self._hash(agg)
            req_now = self.pow_required(ph, ah, now)
            bits = self._pow_bits(ph, now) if req_now else bits
        return ok({"api_version": 1, "contracts": CONTRACTS_VERSION, "access_mode": self.s.access_mode, "features": feats,
                   "max_sessions": self.n_max(), "active_sessions": len(self.live()), "queue_len": len(self.queue.waiting()),
                   "pow": {"required_now": req_now, "bits": int(bits)}, "sdk": {"min_version": SDK_MIN_VERSION},
                   "accepting": self.accepting, "degrade_level": f"L{self.level}", "eta_new_s": self.eta(1) if self.free_slots() == 0 else 0.0})

    def worlds_view(self) -> dict[str, Any]:
        items = []
        for w in self.worlds.items.values():
            w.templates = self.templates.ids(w.world_id)
            items.append(w.item(w.world_id in self.startable_worlds()))
        return ok({"items": items})

    def templates_view(self, world: str | None) -> dict[str, Any]:
        worlds = [w.world_id for w in self.worlds.items.values()]
        if world is not None and world not in worlds:
            return ok({"items": []})
        return ok({"items": self.templates.items(world, worlds)})

    # ------------------------------------------------------------ 落盘
    def dump(self) -> dict[str, Any]:
        return {"v": 1, "saved_unix_ns": ns(self.now()), "sessions": [r.to_json() for r in self.sessions.values()],
                "tickets": [t.to_json() for t in self.queue.tickets.values()], "ticket_seq": self.queue.seq,
                "creates": self.creates, "buckets": self.buckets, "pow_triggers": self.pow_triggers, "pow": self.pow.dump(),
                "accepting": self.accepting, "max_sessions_override": self.s.max_sessions_override}

    def load(self, d: dict[str, Any] | None) -> None:
        if not isinstance(d, dict) or d.get("v") != 1:
            return
        for x in d.get("sessions") or []:
            try:
                r = SessionRecord.from_json(x)
            except TypeError:
                continue
            if not r.held_since:                 # D2 写出的状态文件没有 D2.1 字段：按已知时刻补齐，避免升级后立即兜底驻留
                r.held_since = r.ready_at or r.created
            if not r.last_input:
                r.last_input = max(r.last_activity, r.held_since)
            self.sessions[r.sid] = r
        for x in d.get("tickets") or []:
            try:
                t = Ticket.from_json(x)
            except TypeError:
                continue
            self.queue.tickets[t.ticket_id] = t
        self.queue.seq = max([int(d.get("ticket_seq") or 0)] + [t.seq for t in self.queue.tickets.values()])
        self.creates = {str(k): [float(t) for t in v] for k, v in (d.get("creates") or {}).items() if isinstance(v, list)}
        self.buckets = {str(k): [float(v[0]), float(v[1])] for k, v in (d.get("buckets") or {}).items()
                        if isinstance(v, list) and len(v) == 2}
        self.pow_triggers = {str(k): [float(t) for t in v] for k, v in (d.get("pow_triggers") or {}).items()
                             if isinstance(v, list)}
        self.pow.load(d.get("pow"))
        self.accepting = bool(d.get("accepting", True))
        mo = d.get("max_sessions_override")
        if isinstance(mo, int):
            self.s.max_sessions_override = mo
            self._n_max = None

    # ------------------------------------------------------------ 运维
    def set_accepting(self, on: bool) -> None:
        self.accepting = bool(on)
        self.fx.audit("ops.accept", on=self.accepting)
        self.fx.changed()

    def set_max_sessions(self, n: int | None) -> int:
        self.s.max_sessions_override = n
        self.refresh_n_max()
        self.fx.audit("ops.limits", max_sessions=n)
        self.fx.changed()
        return self.n_max()

    def drain_all(self, notice_s: float, reason: str = "maintenance") -> list[str]:
        out = []
        for r in self.live():
            if r.state in (READY, ACTIVE, IDLE, SPAWNING):
                if r.state == SPAWNING or notice_s <= 0:
                    r.end_reason = reason
                    self._drain(r)
                else:
                    self.schedule_end(r, reason, notice_s)
                out.append(r.sid)
        for t in self.queue.open():
            self.queue.close(t, LEFT, self.now())
            self.fx.ticket_changed(t)
        self.fx.changed()
        return out
