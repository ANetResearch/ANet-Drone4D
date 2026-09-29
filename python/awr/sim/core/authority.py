"""LeaseManager（core）：席位与逐机租约（M08-FR-062；M08 §6.10.3；AWR-12 §4.2.2、§4.8；ADR-027）。

D1-core 的最小实现：
- 席位（OperatorSeat）：每个 run 至多一个持有者（按 principal 计，同一 principal 的多个标签页共享），权威在此；
  `seat_claim` 由 api 签发 operator/admin token 时调用，他人持有时 116；`seat_release`、`seat_grace`、`seat_resume` 改状态；
- 逐机租约：owner ∈ {NONE, OPERATOR, AGENT, MISSION, SWARM}，holder 为 principal；安全类命令（land、hover、rtl、
  safety_stop）免租约但要求席位；租约类命令要求持有者一致。租约 FREE（或孤儿 OPERATOR 租约）时，席位持有者的租约类
  命令按 E01/E05 隐式 acquire(OPERATOR)（本工作包设定，见实现报告偏差表）；OPERATOR 接管 MISSION（E03 特例）；
- `ctrl.owner` 投影：FAILSAFE 或 locked 时为 SAFETY，否则为租约 owner（AWR-12 §4.8.5）。
HMAC lease token、TTL、抢占表、确认令牌为 D1-ext（M08-FR-063）。本模块不读墙钟。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from awr.contracts.enums import OWNER_BY_NAME, OWNER_NAMES, Owner
from awr.contracts.reasons import Reason

__all__ = ["LEASE_EXEMPT", "LeaseManager", "Seat"]

LEASE_EXEMPT = frozenset({"land", "hover", "rtl", "safety_stop", "cancel", "resume"})
PRIORITY = {Owner.NONE: 0, Owner.SWARM: 1, Owner.MISSION: 2, Owner.AGENT: 3, Owner.OPERATOR: 4}
WRITE_ROLES = ("operator", "admin")


@dataclass
class Seat:
    state: str = "FREE"  # FREE、HELD、GRACE
    holder: str | None = None
    since_ns: int = 0  # UNIX ns（显示用），由调用方传入

    def to_json(self) -> dict[str, Any]:
        return {"state": self.state, "holder": self.holder}


@dataclass
class _Lease:
    owner: int = int(Owner.NONE)
    holder: str | None = None
    stack: list[tuple[int, str | None]] = field(default_factory=list)


EmitFn = Callable[..., Any]


class LeaseManager:
    def __init__(self, capacity: int, *, hooks: Any = None) -> None:
        self.capacity = capacity
        self.seat = Seat()
        self._lease: dict[int, _Lease] = {}
        self._owner = np.zeros(capacity, np.uint8)
        self._suspended = np.zeros(capacity, np.bool_)
        self.hooks = hooks

    def _notify(self, kind: str, slots, owner: str | None, holder: str | None) -> None:
        """租约事件同步通知 M09（`SafetyHooks.on_lease_event`，M08-FR-090）。"""
        if self.hooks is None:
            return
        try:
            from .interfaces import LeaseEvent

            self.hooks.on_lease_event(LeaseEvent(kind, np.atleast_1d(np.asarray(slots, np.int32)), owner, holder))
        except Exception:
            pass

    def suspend(self, slots: np.ndarray) -> None:
        """SafetyStop 加锁期间暂停租约持有者的写权限（M09 §7.4）。"""
        self._suspended[np.atleast_1d(np.asarray(slots, np.int64))] = True

    def resume(self, slots: np.ndarray) -> None:
        self._suspended[np.atleast_1d(np.asarray(slots, np.int64))] = False

    def suspended(self, slot: int) -> bool:
        return bool(self._suspended[slot])

    # ------------------------------------------------------------ 席位
    def seat_op(self, op: str, principal_id: str, role: str, *, t_wall_ns: int = 0) -> tuple[int, dict[str, Any]]:
        """返回 (code, seat)；code 0 为成功。"""
        s = self.seat
        if op == "seat_claim":
            if role not in WRITE_ROLES:
                return int(Reason.ROLE_FORBIDDEN), s.to_json()
            if s.state == "FREE" or s.holder == principal_id:
                if s.holder != principal_id:
                    s.since_ns = t_wall_ns
                s.state, s.holder = "HELD", principal_id
                return 0, s.to_json()
            return int(Reason.SEAT_TAKEN), s.to_json() | {"since_unix_ns": str(s.since_ns)}
        if s.holder != principal_id:
            return int(Reason.SEAT_TAKEN), s.to_json()
        if op == "seat_release":
            self._orphan_operator_leases(principal_id)
            s.state, s.holder = "FREE", None
        elif op == "seat_grace":
            s.state = "GRACE"
        elif op == "seat_resume":
            s.state = "HELD"
        elif op == "seat_expire":
            self._orphan_operator_leases(principal_id)
            s.state, s.holder = "FREE", None
        else:
            return int(Reason.BAD_REQUEST), s.to_json()
        return 0, s.to_json()

    def is_seat_holder(self, principal_id: str) -> bool:
        return self.seat.holder == principal_id and self.seat.state in ("HELD", "GRACE")

    def _orphan_operator_leases(self, principal_id: str) -> None:
        for L in self._lease.values():
            if L.owner == Owner.OPERATOR and L.holder == principal_id:
                L.holder = None  # 孤儿租约：owner 仍为 OPERATOR（AWR-12 T05）

    # ------------------------------------------------------------ 逐机租约
    def lease(self, slot: int) -> _Lease:
        L = self._lease.get(slot)
        if L is None:
            L = self._lease[slot] = _Lease()
        return L

    def can_acquire(self, slot: int, owner: int, holder: str | None) -> int:
        """acquire 的判定部分（不改状态）：0 或 100。"""
        L = self._lease.get(slot) or _Lease()
        if L.owner == owner and L.holder == holder:
            return 0
        orphan = L.owner == Owner.OPERATOR and L.holder is None
        if L.owner != Owner.NONE and not orphan and PRIORITY.get(Owner(owner), 0) <= PRIORITY.get(Owner(L.owner), 0) \
                and L.holder != holder:
            return int(Reason.LEASE_DENIED)
        return 0

    def acquire(self, slot: int, owner: int, holder: str | None, *, uav: str | None = None, t_ns: int = 0,
                emit: EmitFn | None = None) -> int:
        L = self.lease(slot)
        if L.owner == owner and L.holder == holder:
            return 0
        orphan = L.owner == Owner.OPERATOR and L.holder is None
        code = self.can_acquire(slot, owner, holder)
        if code:
            return code
        preempted = L.owner not in (Owner.NONE,) and not orphan and L.holder != holder
        if preempted and L.owner in (Owner.MISSION, Owner.AGENT, Owner.SWARM) and len(L.stack) < 2:
            L.stack.append((L.owner, L.holder))
        prev_owner, prev_holder = L.owner, L.holder
        L.owner, L.holder = int(owner), holder
        self._owner[slot] = int(owner)
        if emit is not None:
            if preempted:
                emit("lease.preempted", t_sim_ns=t_ns, severity=1, uav=uav, owner=OWNER_NAMES[prev_owner],
                     holder=prev_holder)
            emit("lease.acquired", t_sim_ns=t_ns, severity=1, uav=uav, owner=OWNER_NAMES[int(owner)], holder=holder)
        if preempted:
            self._notify("preempted", slot, OWNER_NAMES[prev_owner], prev_holder)
        self._notify("acquired", slot, OWNER_NAMES[int(owner)], holder)
        return 0

    def release(self, slot: int, holder: str | None, *, return_to: str = "previous", uav: str | None = None,
                t_ns: int = 0, emit: EmitFn | None = None) -> int:
        L = self.lease(slot)
        if L.owner == Owner.NONE:
            return int(Reason.STATE)
        if L.holder != holder:
            return int(Reason.LEASE_DENIED)
        if return_to == "previous" and L.stack:
            L.owner, L.holder = L.stack.pop()
        else:
            L.owner, L.holder, L.stack = int(Owner.NONE), None, []
        self._owner[slot] = L.owner
        if emit is not None:
            emit("lease.released", t_sim_ns=t_ns, severity=1, uav=uav, owner=OWNER_NAMES[L.owner], holder=L.holder)
        self._notify("released", slot, OWNER_NAMES[L.owner], L.holder)
        return 0

    def free(self, slot: int) -> None:
        self._lease.pop(slot, None)
        self._owner[slot] = int(Owner.NONE)
        self._suspended[slot] = False

    def reset(self) -> None:
        self._lease.clear()
        self._owner[:] = int(Owner.NONE)
        self._suspended[:] = False

    def check(self, slot: int, op: str, principal: dict[str, Any], *, uav: str | None = None, t_ns: int = 0,
              emit: EmitFn | None = None, commit: bool = True) -> int:
        """准入第 ⑤ 步：返回 0 或原因码（115、116、100）。`commit = False` 只判定不隐式 acquire（CommandEngine 在
        ⑥–⑧ 全部通过、分发时再以 `commit = True` 执行，被拒命令不改变租约）。

        - operator/admin：须持席位（116）；安全类免租约；其余命令要求持有者一致，租约 FREE 或孤儿时隐式 acquire(OPERATOR)；
        - agent：SafetyStop 115；只能对自有租约机体发安全类命令；其余命令隐式 acquire(AGENT)；
        - 内部 principal（组合根签发，`_internal`）：role safety 免租约；mission、swarm 以 MISSION、SWARM 类别隐式 acquire；
        - SafetyStop 加锁期间（suspend）持有者的租约类命令 100（安全类、resume 除外）。
        """
        role = principal.get("role")
        pid = principal.get("principal_id")
        if principal.get("_internal"):
            if role in ("safety", "scenario", "supervisor"):
                return 0
            own = {"mission": Owner.MISSION, "swarm": Owner.SWARM, "agent": Owner.AGENT}.get(str(role))
            if own is None:
                return int(Reason.ROLE_FORBIDDEN)
            if op in LEASE_EXEMPT:
                return 0
            L = self.lease(slot)
            if L.owner == own and L.holder == pid:
                return 0 if not self._suspended[slot] else int(Reason.LEASE_DENIED)
            return self._take(slot, int(own), pid, uav, t_ns, emit, commit)
        if role == "viewer":
            return int(Reason.ROLE_FORBIDDEN)
        if role == "agent":
            if op == "safety_stop":
                return int(Reason.ROLE_FORBIDDEN)  # ADR-027、C35
            L = self.lease(slot)
            if L.owner == Owner.AGENT and L.holder == pid:
                return 0 if (op in LEASE_EXEMPT or not self._suspended[slot]) else int(Reason.LEASE_DENIED)
            if op in LEASE_EXEMPT:
                return int(Reason.LEASE_DENIED)
            return self._take(slot, int(Owner.AGENT), pid, uav, t_ns, emit, commit)
        if role in WRITE_ROLES:
            if not self.is_seat_holder(pid):
                return int(Reason.SEAT_TAKEN)
            if op in LEASE_EXEMPT:
                return 0
            if self._suspended[slot]:
                return int(Reason.LEASE_DENIED)
            L = self.lease(slot)
            if L.owner == Owner.OPERATOR and L.holder == pid:
                return 0
            return self._take(slot, int(Owner.OPERATOR), pid, uav, t_ns, emit, commit)
        return int(Reason.ROLE_FORBIDDEN)

    def _take(self, slot: int, owner: int, pid: str | None, uav: str | None, t_ns: int, emit: EmitFn | None,
              commit: bool) -> int:
        if not commit:
            return self.can_acquire(slot, owner, pid)
        return self.acquire(slot, owner, pid, uav=uav, t_ns=t_ns, emit=emit)

    # ------------------------------------------------------------ 投影
    def owner_codes(self) -> np.ndarray:
        return self._owner

    def lease_json(self, slot: int) -> dict[str, Any]:
        L = self.lease(slot)
        return {"owner": OWNER_NAMES[L.owner], "holder": L.holder, "priority": PRIORITY.get(Owner(L.owner), 0),
                "ttl_ms": None}

    @staticmethod
    def owner_value(name: str) -> int:
        return OWNER_BY_NAME[name]
