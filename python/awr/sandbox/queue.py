"""排队票（M17-FR-014；17 §17.4.3；M17 PRD §6.4、§6.6.2）。

票状态机：WAITING →（空位且世界可准入，按队列顺序）OFFERED（预留一个槽位，`offer_claim_s` 内认领）→ CLAIMED；WAITING 连续
`ticket_idle_s` 无长轮询、OFFERED 到期未认领 → EXPIRED；离队 → LEFT。线上状态 `waiting`、`slot_ready`、`claimed`、`expired`。
终态的票保留 `KEEP_CLOSED_S` 供长轮询读到终态后删除。

ETA（17 §17.4.3）：队列非空期间会话停止续期，持有超过 `preempt_after_s` 者按持有时长最长者先被抢占（提前 `notice_s` 告知）；
第 k 个等待者的估计 = 第 k 早结束的会话的剩余时间 + 拉起时长；超出会话数时无法估计（null）。

D2.1（AWR-04 §15.6.4；M17-FR-073、FR-076）：票分三种 `kind`：`new`（新建）、`resume`（恢复驻留会话，与新建票同一 FIFO，
不插队）、`internal`（内部身份，排在全部公开票之前，彼此按入队顺序）。
"""

from __future__ import annotations

from collections.abc import Iterable

from .model import CLAIMED, EXPIRED, LEFT, OFFERED, WAITING, Ticket

__all__ = ["KEEP_CLOSED_S", "TicketQueue", "eta_s"]

KEEP_CLOSED_S = 600.0


def eta_s(position: int, ends: Iterable[float], now: float, *, spawn_s: float = 6.0) -> float | None:
    """`ends`：每个占槽会话的预计结束时刻（寿命截止与抢占时刻的较小者）；空槽按 now。"""
    e = sorted(ends)
    if position <= 0:
        return 0.0
    if position > len(e):
        return None
    return round(max(0.0, e[position - 1] - now) + spawn_s, 1)


class TicketQueue:
    def __init__(self) -> None:
        self.tickets: dict[str, Ticket] = {}
        self.seq = 0

    def add(self, t: Ticket) -> Ticket:
        self.seq += 1
        t.seq = self.seq
        self.tickets[t.ticket_id] = t
        return t

    def get(self, ticket_id: str) -> Ticket | None:
        return self.tickets.get(ticket_id)

    def open(self) -> list[Ticket]:
        """WAITING 与 OFFERED：内部票在前，其余按入队顺序。"""
        return sorted((t for t in self.tickets.values() if t.state in (WAITING, OFFERED)),
                      key=lambda t: (t.kind != "internal", t.seq))

    def waiting(self) -> list[Ticket]:
        return [t for t in self.open() if t.state == WAITING]

    def offered(self) -> list[Ticket]:
        return [t for t in self.open() if t.state == OFFERED]

    def by_principal(self, principal_id: str, prefix_h: str) -> Ticket | None:
        """同一 principal 与前缀的新建或内部票（恢复票按 sid 查找，见 by_sid）。"""
        for t in self.open():
            if t.principal_id == principal_id and t.prefix_h == prefix_h and t.kind != "resume":
                return t
        return None

    def by_sid(self, sid: str) -> Ticket | None:
        for t in self.open():
            if t.kind == "resume" and t.sid == sid:
                return t
        return None

    def count_prefix(self, prefix_h: str) -> int:
        """该前缀的新建票数（恢复票不占新建票名额，M17-FR-073）。"""
        return sum(1 for t in self.open() if t.prefix_h == prefix_h and t.kind == "new")

    def position(self, t: Ticket) -> int:
        """WAITING 票在等待者中的位置（1 起）；OFFERED 为 0。"""
        if t.state != WAITING:
            return 0
        w = self.waiting()
        return 1 + w.index(t) if t in w else 0

    def close(self, t: Ticket, state: str, now: float) -> None:
        assert state in (CLAIMED, EXPIRED, LEFT)
        t.state = state
        t.closed_at = now
        t.offer_deadline = None

    def expire(self, now: float, *, idle_s: float) -> list[Ticket]:
        """WAITING 超过 idle_s 未轮询、OFFERED 认领期限已过 → EXPIRED；返回被作废的票。"""
        out = []
        for t in self.open():
            if (t.state == WAITING and now - t.last_poll >= idle_s) or \
                    (t.state == OFFERED and t.offer_deadline is not None and now >= t.offer_deadline):
                self.close(t, EXPIRED, now)
                out.append(t)
        return out

    def purge(self, now: float) -> None:
        for k in [k for k, t in self.tickets.items() if t.closed_at is not None and now - t.closed_at > KEEP_CLOSED_S]:
            del self.tickets[k]
