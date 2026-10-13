"""节能驻留与恢复（M17-FR-070 至 FR-075、NFR-016；AWR-04 §15.7.4、§15.7.5、ADR-121；17 §17.20.4；M17 PRD §14.3.2、§14.3.3）。

会话状态机的 D2.1 增量（原状态与转移不变者不重复）：

| 状态 | 事件 | 动作 | 目标 |
|---|---|---|---|
| READY、ACTIVE、IDLE | `park{reason}`（SB-69、SB-72 信标）；兜底超时（全部连接的最近交互超过阈值 + 余量，提前 `warn_s` 告知）；无连接 `idle_disconnected_s`；抢占；L3、L4 回收 | `sandbox.parking`；冻结剩余寿命与累计持有时长 | PARKING |
| PARKING | 池完成落盘 | 暂停时钟、checkpoint 与配置快照写入 `var/sandbox/parked/<sid>/`、停止 sim-core、删除 tmpfs 与叶子组、释放槽位并派发 | PARKED |
| PARKED | `resume`（SB-70），有空位、世界准入与前缀活动数通过 | 从 checkpoint 拉起（不兼容时配置快照）；`expires = now + max(remaining_s, 300)`；epoch + 1 | SPAWNING |
| PARKED | `resume`，满员 | 恢复票（`kind = resume`，与新建票同一 FIFO，不插队） | PARKED |
| PARKED | 保留到期、`DELETE`、超出驻留上限 | 配置快照另存 `snapshots/<sid>.json`（保留 600 s），删除驻留目录 | ENDED |

本模块分三部分：
- `BrokerParkMixin`：broker 的纯逻辑（注入时钟，副作用经 `Effects.park`、`resume`、`discard_park` 交给池）；
- `PoolParkMixin`：池的异步实现（总线调用、进程停止、文件与目录）；
- 驻留目录工具：`meta.json`、`snapshot.json`、`checkpoint.bin`、`session.json`；写入用临时目录加 `os.replace`。

checkpoint 落盘的两条路径（M08 变更请求 `.cache/impl/requests/d2-WP-04b-to-M08.md`）：
1. **正式**：`ctl/sim-core/checkpoint{to: "disk", path}` → `{ok, bytes, t_sim_ns, layout_hashes}`；恢复时 `AWR_RESUME_FROM`；
2. **过渡**（M08 尚未提供正式接口时自动采用）：`ctl/sim-core/clock{op: checkpoint}` 强制写一代到 tmpfs，停止进程（sim-core
   停止时同步镜像最新一代到 `runs/<sid>/ckpt/`），池复制最新一代；恢复时把该代放回 `<run_dir>/ckpt/`、按驻留时记录的布局与尺寸
   预建 StateRing，并以 `AWR_RESTART_COUNT = 1` 拉起，sim-core 走既有的崩溃重启恢复路径（epoch + 1）。
两条路径恢复后都核对环头部的仿真时刻：落后于驻留时刻超过 0.1 s 即判定 checkpoint 未生效，按配置快照口径报告
（`sandbox.resumed{resume_mode: snapshot}` 与 `session.switched{reason: resume_snapshot}`）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awr.contracts import CONTRACTS_VERSION, LAYOUT_ID, bus_keys
from awr.runtime.procutil import atomic_write

from .model import (
    ACTIVE,
    END_STATES,
    IDLE,
    PARKED,
    PARKING,
    READY,
    SPAWNING,
    SessionRecord,
    Ticket,
    new_ticket_id,
    new_ticket_token,
    ns,
)
from .tokens import ticket_token_hash

if TYPE_CHECKING:
    from .broker import Broker

__all__ = ["CKPT_NAME", "META_NAME", "RESUME_MIN_S", "SNAP_NAME", "BrokerParkMixin", "PoolParkMixin", "dir_bytes",
           "read_json", "wire_park_reason"]

log = logging.getLogger("awr.sandbox.park")

CKPT_NAME = "checkpoint.bin"
SNAP_NAME = "snapshot.json"
META_NAME = "meta.json"
CFG_NAME = "session.json"
RESUME_MIN_S = 300.0              # 恢复后寿命至少 300 s（AWR-04 §15.7.4）
RESTORE_TOL_NS = 100_000_000      # 恢复后仿真时刻与驻留时刻的容差 0.1 s（D2-AC-45）
RESTORE_WAIT_S = 3.0
# `ctl/sim-core/checkpoint`（17 §17.20.4；契约 1.2.0 的 bus/keys.json 登记后由生成的 bus_keys.CTL_CHECKPOINT 提供；在此之前由
# 同一命名空间的既有键推出，不写键字面量，KEY-01）
CTL_CHECKPOINT = getattr(bus_keys, "CTL_CHECKPOINT", None) or bus_keys.CTL_CLOCK.rsplit("/", 1)[0] + "/checkpoint"
PARKABLE = (READY, ACTIVE, IDLE)
# 驻留原因 → `sandbox.expiring` 与会话摘要中的线上原因（17 §17.7.5 的 reason 枚举）
_EXPIRING_WIRE = {"idle": "idle", "hidden": "idle", "idle_disconnected": "idle", "user": "user", "preempted": "preempted",
                  "reclaimed_cpu": "reclaimed", "reclaimed_mem": "reclaimed"}


def wire_park_reason(reason: str | None) -> str:
    return _EXPIRING_WIRE.get(reason or "", "idle")


def read_json(p: Path) -> Any:
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def dir_bytes(p: Path) -> int:
    total = 0
    for root, _d, files in os.walk(p, onerror=lambda e: None):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_size
    return total


# ======================================================================== broker
class BrokerParkMixin:
    """`Broker` 的驻留与恢复逻辑（纯逻辑；`self` 为 Broker）。"""

    # ------------------------------------------------------------ 线上视图
    def park_wire(self: Broker, rec: SessionRecord) -> dict[str, Any]:
        if rec.state not in (PARKING, PARKED):
            return {}
        pk = self.s.park
        at = rec.parked_at
        return {"parked_unix_ns": ns(at), "park_reason": rec.park_reason, "resume_mode": rec.resume_mode,
                "retention_unix_ns": ns(at + pk.retention_s) if at is not None else None,
                "remaining_s": round(float(rec.remaining_s or 0.0), 1),
                "t_sim_ns": str(int(rec.t_sim_ns_parked)) if rec.t_sim_ns_parked is not None else None}

    # ------------------------------------------------------------ 安排与执行驻留
    def schedule_park(self: Broker, rec: SessionRecord, reason: str, notice_s: float) -> None:
        """提前 notice_s 告知后驻留（兜底、抢占、回收、身份撤销后超出公开寿命）；notice_s ≤ 0 立即驻留。"""
        if rec.state not in PARKABLE:
            return
        now = self.now()
        at = now + max(0.0, notice_s)
        if rec.park_at is not None and rec.park_at <= at:
            return
        rec.park_at, rec.park_pending = at, reason
        if notice_s > 0:
            data: dict[str, Any] = {"reason": wire_park_reason(reason), "in_s": round(notice_s, 1), "at_unix_ns": ns(at),
                                    "end_unix_ns": ns(at), "action": "park"}
            if reason == "preempted":
                data["code"] = 510
            self.fx.event("sandbox.expiring", rec.sid, data, severity=2)
            self.fx.changed()
        else:
            self._park(rec, reason)

    def cancel_park(self: Broker, rec: SessionRecord) -> None:
        if rec.park_at is not None:
            rec.park_at = rec.park_pending = None
            self.fx.changed()

    def _park(self: Broker, rec: SessionRecord, reason: str) -> bool:
        if rec.state not in PARKABLE:
            return False
        now = self.now()
        rec.remaining_s = max(0.0, rec.expires - now)
        rec.held_total_s += max(0.0, now - (rec.held_since or rec.ready_at or rec.created))
        rec.park_reason = reason
        rec.park_at = rec.park_pending = None
        rec.drain_at = None
        rec.end_reason = rec.notice_reason = None
        rec.parked_at = None
        self._set_state(rec, PARKING, reason)
        self.fx.event("sandbox.parking", rec.sid, {"reason": reason})
        self.fx.audit("session.park", sid=rec.sid, reason=reason, tier=rec.tier,
                      held_s=round(rec.held_total_s, 1), remaining_s=round(rec.remaining_s, 1))
        self.fx.park(rec)
        return True

    def park(self: Broker, sid: str, reason: str = "user") -> dict[str, Any]:
        """SB-69 与 SB-72（`sys/session/park{sid, reason}`）：202 PARKING；已 PARKED 200；DRAINING 与 SPAWNING 409 `513`。"""
        from .broker import err, ok

        rec = self.sessions.get(sid)
        if rec is None:
            return err(404, 305, sid=sid)
        if rec.state in END_STATES:
            return err(410, 506, end_reason=rec.end_reason, snapshot_available=bool(rec.snapshot_path))
        if rec.state == PARKED:
            return ok({"state": PARKED, "session": self.wire(rec)}, 200)
        if rec.state == PARKING:
            return ok({"state": PARKING, "session": self.wire(rec)}, 202)
        if rec.state not in PARKABLE:
            return err(409, 513, state=rec.state)
        r = reason if reason in ("idle", "hidden", "user") else "user"
        self._park(rec, r)
        return ok({"state": PARKING, "session": self.wire(rec)}, 202)

    def on_parked(self: Broker, sid: str, *, mode: str, t_sim_ns: int | None, nbytes: int, park_dir: str) -> None:
        """池完成 PARKING：进程已停止、tmpfs 与叶子组已删除、驻留目录已原子写入。"""
        rec = self.sessions.get(sid)
        if rec is None or rec.state != PARKING:
            return
        now = self.now()
        rec.parked_at = now
        rec.resume_mode = mode
        rec.t_sim_ns_parked = t_sim_ns
        rec.park_bytes = int(nbytes)
        rec.park_dir = park_dir
        rec.pid = rec.pid_starttime = None
        rec.ws = 0
        rec.clock_state = "STOPPED"
        rec.played = False
        rec.resuming = False
        self._set_state(rec, PARKED, rec.park_reason)
        self.fx.event("sandbox.parked", sid, {"reason": rec.park_reason, "resume_mode": mode,
                                              "retention_unix_ns": ns(now + self.s.park.retention_s)})
        self.fx.audit("session.parked", sid=sid, reason=rec.park_reason, resume_mode=mode, bytes=int(nbytes))
        if rec.end_after_park:
            self._end_parked(rec, "user")
            return
        self._offer(now)
        self._park_limits(now)

    def _end_parked(self: Broker, rec: SessionRecord, reason: str) -> None:
        """结束驻留会话：配置快照另存（保留 snapshot_keep_s），删除驻留目录（池完成后回调 on_ended）。"""
        if rec.state != PARKED or rec.drain_at is not None:
            return
        rec.end_reason = reason
        rec.drain_at = self.now()
        q = self.queue.by_sid(rec.sid)
        if q is not None:
            self.queue.close(q, "EXPIRED", self.now())
            self.fx.ticket_changed(q)
        self.fx.audit("session.end", sid=rec.sid, reason=reason, parked=True)
        self.fx.discard_park(rec)
        self.fx.changed()

    def _park_limits(self: Broker, now: float) -> None:
        """驻留上限：每前缀 `per_prefix`、全站 `total_max`、磁盘 `disk_mb`；超出时最早驻留者结束（M17-FR-074）。"""
        pk = self.s.park
        parked = sorted((r for r in self.sessions.values() if r.state == PARKED and r.drain_at is None),
                        key=lambda r: r.parked_at or 0.0)
        by_pfx: dict[str, list[SessionRecord]] = {}
        for r in parked:
            by_pfx.setdefault(r.prefix_h, []).append(r)
        evict: list[SessionRecord] = []
        for rs in by_pfx.values():
            evict += rs[:max(0, len(rs) - int(pk.per_prefix))]
        rest = [r for r in parked if r not in evict]
        evict += rest[:max(0, len(rest) - int(pk.total_max))]
        rest = [r for r in rest if r not in evict]
        total = sum(r.park_bytes for r in rest)
        for r in list(rest):
            if total <= pk.disk_mb * (1 << 20):
                break
            evict.append(r)
            total -= r.park_bytes
        for r in evict:
            self._end_parked(r, "park_evicted")

    # ------------------------------------------------------------ 恢复
    def _prefix_ok(self: Broker, rec: SessionRecord) -> bool:
        if not self.s.public or rec.tier == "internal":
            return True
        return self._active_count(rec.prefix_h, agg=False) < self.s.per_prefix_cap(self.n_max())

    def resume(self: Broker, sid: str, *, token_exp: Any = None) -> dict[str, Any]:
        """SB-70（`sys/session/resume{sid, token_exp}`）：201 拉起；已活动 200；满员 202 恢复票；驻留已到期 410 `506`。
        sandbox-api 只验签与 sid；token 过期多久仍可接受（驻留保留时长）在此判定：超过时 401 `302`。"""
        from .broker import err, ok

        now = self.now()
        rec = self.sessions.get(sid)
        if rec is None:
            return err(404, 305, sid=sid)
        if isinstance(token_exp, (int, float)) and not isinstance(token_exp, bool) and \
                float(token_exp) + self.s.park.retention_s < now:
            return err(401, 302, why="token_expired")
        if rec.state in END_STATES:
            return err(410, 510 if rec.end_reason == "preempted" else 506, end_reason=self.wire(rec).get("end_reason"),
                       snapshot_available=bool(rec.snapshot_path))
        if rec.state in (SPAWNING, READY, ACTIVE, IDLE):
            if rec.state != SPAWNING:
                self._touch(rec, now)
            return ok({"session": self.wire(rec), "token": None}, 200, "existing")
        if rec.state != PARKED:
            return err(409, 513, state=rec.state, retry_after_s=1)
        if rec.drain_at is not None:
            return err(410, 506, end_reason="parked_expired", snapshot_available=True)
        if rec.parked_at is not None and now >= rec.parked_at + self.s.park.retention_s:
            self._end_parked(rec, "parked_expired")
            return err(410, 506, end_reason="parked_expired", snapshot_available=True)
        t = self.queue.by_sid(sid)
        if t is not None:
            return self._queued_reply(t, rotate=True)
        if not self.accepting:
            return err(503, 502, why="maintenance", retry_after_s=60)
        internal = rec.tier == "internal"
        if internal:
            room = self.free_reserved() > 0 or self.free_slots() > 0
        else:
            room = self.free_slots() > 0 and not self.queue_waiting()
        if room and not self.cpu_blocked() and self.admit(rec.world, tier=rec.tier):
            if not self._prefix_ok(rec):
                return err(409, 504, why="active_per_prefix", limit=self.s.per_prefix_cap(self.n_max()))
            return self._resume_spawn(rec, now)
        q = self.sb.queue
        if len(self.queue.open()) >= q.max and not internal:
            return err(409, 500, why="queue_full", queue_len=len(self.queue.open()))
        tok = new_ticket_token()
        tid = new_ticket_id()
        while self.queue.get(tid) is not None:
            tid = new_ticket_id()
        t = self.queue.add(Ticket(ticket_id=tid, token_sha256=ticket_token_hash(tok), principal_id=rec.principal_id,
                                  prefix_h=rec.prefix_h, agg_prefix_h=rec.agg_prefix_h, world=rec.world, template=rec.template,
                                  create_args={}, created=now, last_poll=now, sid=sid,
                                  kind="internal" if internal else "resume"))
        self.fx.audit("queue.ticket", ticket_id=t.ticket_id, prefix_h=t.prefix_h, world=rec.world, ticket_kind=t.kind, sid=sid)
        self.fx.changed()
        self._offer(now)
        return self._queued_reply(t, token=tok)

    def _resume_spawn(self: Broker, rec: SessionRecord, now: float) -> dict[str, Any]:
        from .broker import ok

        prev = rec.state
        internal = rec.tier == "internal"
        rec.held_since = now
        rec.ready_at = None
        rec.last_activity = rec.last_input = now
        rec.idle_since = None
        rec.expires = now + max(float(rec.remaining_s or 0.0), RESUME_MIN_S)
        lt_max = self.s.internal.lifetime_max_s if internal else self.sb.lifetime.max_s
        rec.max_expires = max(rec.expires, now + max(0.0, lt_max - rec.held_total_s))
        rec.epoch += 1
        rec.resuming = True
        rec.crashes = []
        rec.granted_rate, rec.requested_rate, rec.limited_by, rec.recheck_at = 1.0, 1.0, None, None
        rec.drain_at = rec.end_reason = rec.notice_reason = None
        rec.park_at = rec.park_pending = None
        self._set_state(rec, SPAWNING, "resume")
        self.fx.resume(rec)
        self.fx.audit("session.resume", sid=rec.sid, from_state=prev, resume_mode=rec.resume_mode, tier=rec.tier)
        tok, _ = self.keys.issue(rec.principal_id, rec.sid, rec.expires, tier=rec.tier)
        self.fx.changed()
        return ok({"session": self.wire(rec), "token": tok, "token_exp_unix_ns": ns(rec.expires)}, 201, "created")

    def on_resumed(self: Broker, sid: str, mode: str) -> None:
        """恢复的 sim-core 就绪且已核对 checkpoint 是否生效（mode 为实际口径）。"""
        rec = self.sessions.get(sid)
        if rec is None:
            return
        planned = rec.resume_mode
        rec.resume_mode = mode
        rec.resuming = False
        rec.park_dir = None
        rec.park_bytes = 0
        if rec.played and rec.state in (ACTIVE, IDLE):    # 核对期间已有访客接入：现在启动时钟
            rec.clock_state = "PLAYING"
            self.fx.clock(rec, "play", {})
        self.fx.event("sandbox.resumed", sid, {"resume_mode": mode, "epoch": rec.epoch})
        if mode == "snapshot" and planned == "checkpoint":
            self.fx.event("session.switched", sid, {"reason": "resume_snapshot", "epoch": rec.epoch}, severity=2)
        self.fx.audit("session.resumed", sid=sid, resume_mode=mode, planned=planned)

    def on_resume_failed(self: Broker, sid: str, why: str) -> bool:
        """恢复的拉起失败：checkpoint 口径改用配置快照重试一次（返回 True）；仍失败时结束（resume_failed）。"""
        rec = self.sessions.get(sid)
        if rec is None or not rec.resuming:
            return False
        if rec.resume_mode == "checkpoint":
            rec.resume_mode = "snapshot"
            rec.epoch += 1
            self.fx.audit("session.resume_retry", sid=sid, why=why)
            self.fx.event("session.switched", sid, {"reason": "resume_snapshot", "epoch": rec.epoch}, severity=2)
            self.fx.resume(rec)
            self.fx.changed()
            return True
        rec.resuming = False
        rec.end_reason = "resume_failed"
        self._drain(rec)
        return True

    # ------------------------------------------------------------ 1 Hz：兜底、无连接驻留、保留、上限、身份撤销
    def power_tick(self: Broker, now: float) -> None:
        pw = self.s.power
        pk = self.s.park
        busy = self.queue_waiting()
        for rec in list(self.sessions.values()):
            if rec.state == PARKED:
                if rec.drain_at is None and rec.parked_at is not None and now >= rec.parked_at + pk.retention_s:
                    self._end_parked(rec, "parked_expired")
                continue
            if rec.state not in PARKABLE:
                continue
            if rec.tier == "internal" and self._identity_revoked(rec, now):
                self._demote(rec, now)
            if rec.state == IDLE and rec.park_at is None:
                thr = pk.idle_disconnected_busy_s if busy else pk.idle_disconnected_s
                if now - rec.last_activity >= thr:
                    self.schedule_park(rec, "idle_disconnected", 0.0)
                    continue
            if rec.state == ACTIVE:
                thr = pw.internal_idle_s if rec.tier == "internal" else (pw.sandbox_idle_busy_s if busy else pw.sandbox_idle_s)
                deadline = max(rec.last_input, rec.held_since) + thr + pw.grace_s
                if rec.manual:
                    if rec.park_pending == "idle":
                        self.cancel_park(rec)
                elif rec.park_pending == "idle" and rec.park_at is not None and deadline > rec.park_at + 0.5:
                    self.cancel_park(rec)       # 告知期间有新的交互
                elif rec.park_at is None and now >= deadline - pw.warn_s:
                    self.schedule_park(rec, "idle", max(0.0, deadline - now))
            if rec.park_at is not None and now >= rec.park_at and rec.state in PARKABLE:
                self._park(rec, rec.park_pending or "idle")
        for rec in [r for r in self.sessions.values() if r.state == PARKED and r.tier == "internal"]:
            if self._identity_revoked(rec, now):
                self._demote(rec, now)
        self._park_limits(now)

    def _identity_revoked(self: Broker, rec: SessionRecord, now: float) -> bool:
        if rec.identity_exp is not None and now > rec.identity_exp:
            return True
        try:
            return bool(rec.identity_jti) and bool(self.is_revoked(rec.identity_jti))
        except Exception:
            return False

    def _demote(self: Broker, rec: SessionRecord, now: float) -> None:
        """身份撤销或过期：降为公开档，寿命按公开上限截断，超出者提前 notice_s 告知后驻留（M17-FR-076）。"""
        rec.tier = "public"
        holding = rec.state not in (PARKED, *END_STATES)
        held = rec.held_total_s + (max(0.0, now - (rec.held_since or now)) if holding else 0.0)
        left = max(0.0, self.sb.lifetime.max_s - held)
        if rec.state == PARKED:
            rec.remaining_s = min(float(rec.remaining_s or 0.0), left)
        else:
            rec.max_expires = min(rec.max_expires, now + left)
            rec.expires = min(rec.expires, rec.max_expires)
            if left <= self.sb.notice_s:
                self.schedule_park(rec, "preempted", self.sb.notice_s)
        self.fx.audit("sandbox.internal_session", sid=rec.sid, action="demoted", jti=rec.identity_jti)
        self.fx.event("sandbox.state", rec.sid, {"from": rec.state, "to": rec.state, "reason": "identity_revoked",
                                                 "epoch": rec.epoch})
        rec.identity_jti = rec.identity_sub = None
        rec.identity_exp = None
        self.fx.changed()


# ======================================================================== pool
class PoolParkMixin:
    """`Pool` 的驻留与恢复副作用（`self` 为 Pool）。"""

    def _park_dir(self, sid: str) -> Path:
        return self.s.parked_dir / sid

    async def _park_session(self, sid: str) -> None:
        bk = self.broker
        assert bk is not None and self.launcher is not None
        rec = bk.sessions.get(sid)
        if rec is None or rec.state != PARKING:
            return
        await self._wait_spawn_done(sid)
        b = self.bindings.get(sid) or bk.binding(rec)
        sb = self.sbus.get(sid)
        h = self.handles.get(sid)
        final = self._park_dir(sid)
        tmp = self.s.parked_dir / f".{sid}.tmp"
        await asyncio.to_thread(_fresh_dir, tmp)
        pk = self.s.park
        mode = "snapshot"
        t_sim: int | None = None
        ring: dict | None = None
        snap: dict | None = None
        t0 = time.monotonic()
        if sb is not None and h is not None:
            rep = await sb.clock("pause", {}, timeout=1.0)
            if not isinstance(rep, dict) or rep.get("code"):
                await sb.seat_claim(timeout=1.0)        # 席位固定给会话 principal（幂等）
                await sb.clock("pause", {}, timeout=1.0)
            ring = self.launcher.ring_meta(b)
            rep = await sb.checkpoint_to_disk(tmp / CKPT_NAME, timeout=pk.checkpoint_timeout_s)
            if isinstance(rep, dict) and rep.get("ok") and (tmp / CKPT_NAME).exists():
                mode = "checkpoint"
                t_sim = int(rep["t_sim_ns"]) if isinstance(rep.get("t_sim_ns"), int) else None
            else:
                await sb.clock("checkpoint", {}, timeout=min(2.0, pk.checkpoint_timeout_s))
            snap = await sb.snapshot(timeout=2.0)
        # 停止进程（sim-core 停止时同步写完并镜像最新一代 checkpoint）
        h = self.handles.pop(sid, None)
        pid = h.pid if h is not None else rec.pid
        self.expected_exit.add(sid)
        if pid:
            await self.launcher.terminate(pid, grace_s=5.0)
        if self.exitw is not None:
            self.exitw.unwatch(sid)
        self.expected_exit.discard(sid)
        self._close_sbus(sid)
        if mode != "checkpoint":
            got = await asyncio.to_thread(self.launcher.latest_checkpoint, rec, b, tmp / CKPT_NAME)
            if got is not None:
                mode, t_sim = "checkpoint", got
        if t_sim is None and ring is not None:
            t_sim = int(ring.get("t_sim_ns") or 0)
        nbytes = await asyncio.to_thread(self._write_park_dir, rec, b, tmp, final, snap, mode, t_sim, ring)
        if nbytes > pk.session_mb * (1 << 20) and (final / CKPT_NAME).exists():
            log.warning("parked checkpoint too large; keeping the configuration snapshot only",
                        extra={"kv": {"sid": sid, "bytes": nbytes}})
            with contextlib.suppress(OSError):
                (final / CKPT_NAME).unlink()
            mode = "snapshot"
            nbytes = await asyncio.to_thread(dir_bytes, final)
        self.bindings.pop(sid, None)
        await asyncio.to_thread(self.launcher.cleanup, rec, b, keep_run_dir=True)
        await asyncio.to_thread(_prune_run_dir, self.s.runs_dir / sid)
        log.info("sandbox parked", extra={"kv": {"sid": sid, "mode": mode, "bytes": nbytes,
                                                  "ms": round((time.monotonic() - t0) * 1e3)}})
        bk.on_parked(sid, mode=mode, t_sim_ns=t_sim, nbytes=nbytes, park_dir=str(final))
        self._flush()
        self.schedule_trim()

    def _write_park_dir(self, rec: SessionRecord, b: Any, tmp: Path, final: Path, snap: dict | None, mode: str,
                        t_sim: int | None, ring: dict | None) -> int:
        from .capacity import build_info

        rd = self.s.runs_dir / rec.sid
        cfg = rd / CFG_NAME
        if cfg.exists():
            shutil.copyfile(cfg, tmp / CFG_NAME)
        if snap is None:
            snap = read_json(cfg)            # 导出失败：退回会话配置（与快照同构）
        if isinstance(snap, dict):
            atomic_write(tmp / SNAP_NAME, json.dumps(snap, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 0o600)
        meta = {"sid": rec.sid, "world": rec.world, "template": rec.template, "tier": rec.tier, "epoch": rec.epoch,
                "t_sim_ns": t_sim, "ring": ring, "layout_id": LAYOUT_ID, "contracts_version": CONTRACTS_VERSION,
                "release": build_info()["release"], "parked_unix_ns": str(time.time_ns()), "reason": rec.park_reason,
                "resume_mode": mode}
        atomic_write(tmp / META_NAME, json.dumps(meta, separators=(",", ":")).encode("utf-8"), 0o600)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
        with contextlib.suppress(OSError):
            os.chmod(final, 0o700)
        return dir_bytes(final)

    # ------------------------------------------------------------ 恢复：拉起前与就绪后
    def _prepare_resume(self, rec: SessionRecord, b: Any) -> tuple[dict | None, dict[str, str], int | None]:
        """(会话配置, 额外环境, 驻留时刻 t_sim)。checkpoint 与当前代码不兼容（发布、契约、布局）时改用配置快照口径。"""
        from .capacity import build_info
        from .templates import build_session_config

        pd = Path(rec.park_dir) if rec.park_dir else self._park_dir(rec.sid)
        meta = read_json(pd / META_NAME) or {}
        snap = read_json(pd / SNAP_NAME)
        cfg_doc = None
        if isinstance(snap, dict):
            w = self.worlds.get(rec.world) if self.worlds else None
            try:
                cfg_doc = build_session_config(rec.world, template_id=None, template=None, snapshot=snap, calendar=None,
                                               acoustic_bg=snap.get("acoustic_bg"), seed=int(snap.get("seed") or rec.seed),
                                               limits=self.s.limits_obj(), tz=w.tz if w else None)
            except Exception:
                cfg_doc = None
        if cfg_doc is None:
            cfg_doc = read_json(pd / CFG_NAME)
        env: dict[str, str] = {}
        t_sim = meta.get("t_sim_ns") if isinstance(meta.get("t_sim_ns"), int) else None
        if rec.resume_mode == "checkpoint":
            compatible = (meta.get("release") == build_info()["release"] and meta.get("contracts_version") == CONTRACTS_VERSION
                          and meta.get("layout_id") == LAYOUT_ID and (pd / CKPT_NAME).exists())
            if compatible and self.launcher.stage_checkpoint(rec, b, pd / CKPT_NAME, meta):
                env = {"AWR_RESUME_FROM": str(pd / CKPT_NAME), "AWR_RESTART_COUNT": "1"}
            else:
                rec.resume_mode = "snapshot"
                self._resume_planned_fallback.add(rec.sid)
        return cfg_doc if isinstance(cfg_doc, dict) else None, env, t_sim

    async def _finish_resume(self, sid: str, b: Any, t_sim_parked: int | None) -> None:
        """就绪后核对恢复口径（环头部仿真时刻），删除驻留目录，再交给 broker。"""
        bk = self.broker
        assert bk is not None
        rec = bk.sessions.get(sid)
        if rec is None:
            return
        mode = rec.resume_mode or "snapshot"
        if mode == "checkpoint" and t_sim_parked and t_sim_parked > RESTORE_TOL_NS:
            end = time.monotonic() + RESTORE_WAIT_S
            ok_ = False
            while time.monotonic() < end:
                t = self.launcher.ring_t_sim(b)
                if t is not None and t + RESTORE_TOL_NS >= t_sim_parked:
                    ok_ = True
                    break
                await asyncio.sleep(0.05)
            if not ok_:
                log.warning("checkpoint not restored; resumed from the configuration snapshot", extra={"kv": {"sid": sid}})
                mode = "snapshot"
        if sid in self._resume_planned_fallback:
            self._resume_planned_fallback.discard(sid)
            rec.resume_mode = "checkpoint"   # 报告"计划为 checkpoint、实际为快照"
            mode = "snapshot"
        pd = Path(rec.park_dir) if rec.park_dir else None
        bk.on_resumed(sid, mode)
        if pd is not None:
            await asyncio.to_thread(shutil.rmtree, pd, True)

    async def _discard_park(self, sid: str) -> None:
        bk = self.broker
        assert bk is not None
        rec = bk.sessions.get(sid)
        if rec is None:
            return
        snap_path = await asyncio.to_thread(self._park_snapshot_out, rec)
        await asyncio.to_thread(shutil.rmtree, self.s.runs_dir / sid, True)
        await asyncio.to_thread(shutil.rmtree, self.s.shm_root / sid, True)
        bk.on_ended(sid, snap_path)
        self._flush()

    def _park_snapshot_out(self, rec: SessionRecord) -> str | None:
        """驻留目录的配置快照另存为 `snapshots/<sid>.json`（0600），删除驻留目录；返回快照路径或 None。"""
        from awr.runtime.procutil import write_private

        pd = Path(rec.park_dir) if rec.park_dir else self._park_dir(rec.sid)
        out = None
        snap = pd / SNAP_NAME
        if snap.exists():
            p = self.s.snapshots_dir / f"{rec.sid}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                write_private(p, snap.read_bytes())
                out = str(p)
        shutil.rmtree(pd, ignore_errors=True)
        return out


def _fresh_dir(p: Path) -> None:
    shutil.rmtree(p, ignore_errors=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p.parent, 0o700)
    p.mkdir(mode=0o700)


def _prune_run_dir(rd: Path) -> None:
    """驻留期间只保留 `runs/<sid>/` 的日志、per-run secret 与会话配置（AWR-04 §15.7.4：删除除日志外的内容）。"""
    keep = {"logs", "secret", CFG_NAME}
    with contextlib.suppress(OSError):
        for p in rd.iterdir():
            if p.name in keep:
                continue
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    p.unlink()
