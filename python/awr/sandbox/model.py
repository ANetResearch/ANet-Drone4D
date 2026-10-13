"""沙盒池的数据结构（M17 PRD §6.2；17 §17.1 标识、§17.4.1 会话对象、§17.4.3 排队票）。

时间口径：池内全部时刻为墙钟 UNIX 秒（float，ADR-045 的墙钟时长；落盘后重启仍可比较），线上为 `*_unix_ns` 十进制字符串。

结束原因：池内保留细分原因（`unclaimed`、`reclaimed_cpu` 等，进审计与日志），线上会话对象与事件只用契约枚举
`{idle, lifetime, preempted, reclaimed, user, failed}`（`session.schema.json` 的 `end_reason`），映射见 `WIRE_END`；D2.1 增加
`parked_expired`（驻留保留到期或超出驻留上限，17 §17.20.4）。

D2.1（AWR-04 §15.6、§15.7.4、§15.9.3；M17 PRD §14.2）：
- 状态增加 PARKING（落盘与停止进程中，仍占槽位）与 PARKED（不占槽位、不占进程，仿真时间与寿命冻结，保留 `retention_s`）；
- 会话记录增加档位 `tier`（public、internal）、身份令牌 jti、持有起点与累计持有时长（寿命上限只计持有时间）、最近交互、驻留字段；
- 排队票增加 `kind`（new、resume、internal）与恢复票的 `sid`。
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import asdict, dataclass, field, fields
from typing import Any

__all__ = [
    "END_STATES",
    "LIVE_STATES",
    "PARKED",
    "PARKING",
    "PARK_REASONS",
    "SLOT_STATES",
    "TIERS",
    "WIRE_END",
    "SessionRecord",
    "Ticket",
    "WorldCost",
    "new_challenge_id",
    "new_pair_code",
    "new_sid",
    "new_ticket_id",
    "new_ticket_token",
    "ns",
    "wire_end_reason",
]

B32 = "abcdefghijklmnopqrstuvwxyz234567"

SPAWNING, READY, ACTIVE, IDLE, DRAINING, ENDED, FAILED = "SPAWNING", "READY", "ACTIVE", "IDLE", "DRAINING", "ENDED", "FAILED"
PARKING, PARKED = "PARKING", "PARKED"
LIVE_STATES = (SPAWNING, READY, ACTIVE, IDLE, PARKING, DRAINING)   # 占用槽位（有进程或正在停止进程）；PARKED 不在内
SLOT_STATES = LIVE_STATES
END_STATES = (ENDED, FAILED)
TIERS = ("public", "internal")
PARK_REASONS = ("idle", "hidden", "user", "idle_disconnected", "preempted", "reclaimed_cpu", "reclaimed_mem")

WAITING, OFFERED, CLAIMED, EXPIRED, LEFT = "WAITING", "OFFERED", "CLAIMED", "EXPIRED", "LEFT"
TICKET_WIRE = {WAITING: "waiting", OFFERED: "slot_ready", CLAIMED: "claimed", EXPIRED: "expired", LEFT: "expired"}

WIRE_END = {"user": "user", "lifetime": "lifetime", "idle": "idle", "unclaimed": "idle", "preempted": "preempted",
            "reclaimed": "reclaimed", "reclaimed_cpu": "reclaimed", "reclaimed_mem": "reclaimed", "maintenance": "reclaimed",
            "world_switch_failed": "failed", "spawn_failed": "failed", "crash": "failed", "failed": "failed", "gone": "failed",
            "parked_expired": "parked_expired", "park_evicted": "parked_expired", "resume_failed": "failed"}


def wire_end_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    return WIRE_END.get(reason, "failed")


def _b32(n: int) -> str:
    return "".join(secrets.choice(B32) for _ in range(n))


def new_sid() -> str:
    """`sb-` + 10 位小写 base32（50 位随机数；17 §17.1）。"""
    return "sb-" + _b32(10)


def new_ticket_id() -> str:
    return "q-" + _b32(10)


def new_challenge_id() -> str:
    return "c-" + _b32(10)


def new_pair_code() -> str:
    """指挥器配对码：8 位大写 base32（40 位随机数；M17-FR-081，P1）。"""
    return _b32(8).upper()


def new_ticket_token() -> str:
    """排队票令牌 `q1.` + 32 字节随机数的 base64url（17 §17.3.2；池只保存其 SHA-256）。"""
    return "q1." + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def ns(t_s: float | None) -> str | None:
    """墙钟秒 → `*_unix_ns` 十进制字符串。"""
    return None if t_s is None else str(round(t_s * 1e9))


@dataclass
class SessionRecord:
    sid: str
    state: str
    world: str
    world_class: str
    template: str | None
    principal_id: str
    prefix_h: str
    agg_prefix_h: str
    client: str = "other"
    created: float = 0.0
    ready_at: float | None = None
    expires: float = 0.0
    max_expires: float = 0.0
    renewals: int = 0
    last_activity: float = 0.0
    idle_since: float | None = None
    ws: int = 0
    epoch: int = 1
    restarts: int = 0
    pid: int | None = None
    pid_starttime: int | None = None
    spawn_at: float | None = None
    crashes: list[float] = field(default_factory=list)
    granted_rate: float = 1.0
    requested_rate: float = 1.0
    rate_cap: float = 10.0
    limited_by: str | None = None
    recheck_at: float | None = None
    grant_window_until: float = 0.0
    c_i: float = 0.15
    c_i_measured: bool = False
    rtf: float = 1.0
    rtf_low_since: float | None = None
    anon_mb: float = 0.0
    manual: bool = False
    end_reason: str | None = None
    drain_at: float | None = None
    notice_reason: str | None = None
    ended_at: float | None = None
    snapshot_path: str | None = None
    calendar: dict | None = None
    acoustic_bg: str | None = None
    seed: int = 0
    usage: dict = field(default_factory=dict)
    clock_state: str = "STOPPED"
    played: bool = False
    switch: dict | None = None          # 切换底图进行中：{from_world, from_template, snapshot_path}
    # ---- D2.1（M17 PRD §14.2）
    tier: str = "public"                # public | internal
    identity_jti: str | None = None     # 内部档：签发会话的身份令牌 jti（撤销检查）
    identity_sub: str | None = None     # 内部档：身份 principal（每身份会话数上限）
    identity_exp: float | None = None   # 内部档：身份令牌到期（墙钟秒）
    held_since: float = 0.0             # 本次持有槽位的起点（抢占按此计算持有时长）
    held_total_s: float = 0.0           # 此前各次持有的累计时长（寿命上限只计持有时间）
    last_input: float = 0.0             # 最近一次交互（presence、命令、会话内 REST 调用；兜底驻留据此判定）
    park_at: float | None = None        # 已安排的驻留时刻（兜底、抢占、回收；提前告知）
    park_pending: str | None = None     # 已安排驻留的原因
    park_reason: str | None = None      # 最近一次驻留的原因（PARK_REASONS）
    parked_at: float | None = None
    resume_mode: str | None = None      # checkpoint | snapshot
    remaining_s: float | None = None    # 驻留时冻结的剩余寿命
    park_dir: str | None = None         # var/sandbox/parked/<sid>/
    park_bytes: int = 0
    t_sim_ns_parked: int | None = None
    resuming: bool = False              # 本次拉起为恢复（就绪后核对 checkpoint 是否生效）
    end_after_park: bool = False        # PARKING 期间收到结束请求：驻留完成后结束
    pair: dict | None = None            # 指挥器配对码（P1）：{sha256, exp, used}
    pair_issued: list[float] = field(default_factory=list)

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SessionRecord:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class Ticket:
    ticket_id: str
    token_sha256: str
    principal_id: str
    prefix_h: str
    agg_prefix_h: str
    world: str
    template: str | None
    create_args: dict
    state: str = WAITING
    created: float = 0.0
    last_poll: float = 0.0
    offer_deadline: float | None = None
    closed_at: float | None = None
    sid: str | None = None
    seq: int = 0
    kind: str = "new"                   # new | resume | internal（D2.1；internal 排在队首）

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Ticket:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass(frozen=True)
class WorldCost:
    """`configs/sandbox/world_costs.json` 的一行（M16 生成；`awr.sandbox.world_costs.v1`）。"""

    world: str
    world_class: str = "S"
    anon_mb: float = 140.0
    ws_mb: float = 25.0
    c0: float = 0.15
    available: bool = True
