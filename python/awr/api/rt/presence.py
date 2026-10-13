"""presence 控制消息、每连接最近交互时刻与 show-api 的空闲兜底回收（M11-FR-220、FR-221；AWR-04 §15.7.4、§15.7.6；
17 §17.20.4；ADR-121）。

- 每条连接（`ClientSession`）记录 `last_input_ns`（墙钟 ns）：握手时为当前；收到 `presence{input_age_s}` 时取
  `max(旧值, now − input_age_s)`；收到 `call` 时为当前；从未发过 presence 的旧客户端另以订阅变更计活动（D1 客户端兼容）；
- presence 每连接 ≥ `presence_min_s` 一条，超出的忽略并计入 `rt_presence_dropped`；presence 只延后回收，不能延长寿命或越过抢占
  （沙盒寿命与抢占在池里，与本模块无关）；
- 兜底（show-api，每 `scan_s` 一次）：`now − last_input > 阈值 + grace_s` 的连接先发 `error{518, detail{reason}}` 再以 4430 关闭，
  释放观看席位并派发观看排队。阈值：operator、admin 与带身份令牌的连接 `internal_idle_s`；页面隐藏上报后 `hidden_s`；观看席位已满
  且观看排队非空时 `viewer_idle_busy_s`；否则 `viewer_idle_s`。`power.reap = false`（本机基准与浸泡）时不回收；
- sandbox-api 不在这里回收：会话内全部连接 `last_input` 的最大值随 1 s 的 `activity` 报告给池，由池决定驻留（hub）。
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .protocol import error_msg

if TYPE_CHECKING:
    from .session import ClientSession

__all__ = ["CLOSE_IDLE_RECLAIMED", "CLOSE_VIEWER_SEATS_FULL", "IDLE_RECLAIMED", "PowerView", "idle_limit_s", "note_input",
           "on_presence", "power_info", "reap_due"]

IDLE_RECLAIMED = 518
CLOSE_IDLE_RECLAIMED = 4430
CLOSE_VIEWER_SEATS_FULL = 4431
POWER_STATES = ("active", "quiet", "warning", "saving")
SERVER_INFO_KEYS = ("viewer_idle_s", "viewer_idle_busy_s", "sandbox_idle_s", "sandbox_idle_busy_s", "hidden_s", "warn_s",
                    "quiet_s", "grace_s", "presence_min_s", "internal_idle_s")


@dataclass
class PowerView:
    """`power:` 段的只读视图（墙钟秒）；缺省值同 AWR-04 §15.7.2。"""

    viewer_idle_s: float = 300.0
    viewer_idle_busy_s: float = 120.0
    sandbox_idle_s: float = 600.0
    sandbox_idle_busy_s: float = 300.0
    hidden_s: float = 60.0
    warn_s: float = 30.0
    quiet_s: float = 60.0
    grace_s: float = 90.0
    presence_min_s: float = 15.0
    internal_idle_s: float = 3600.0
    reap: bool = True
    scan_s: float = 10.0

    @classmethod
    def from_cfg(cls, cfg: Any) -> PowerView:
        if cfg is None:
            return cls()
        d = cfg.model_dump(mode="json") if hasattr(cfg, "model_dump") else dict(cfg)
        out = cls()
        for k in SERVER_INFO_KEYS:
            if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool):
                setattr(out, k, float(d[k]))
        if isinstance(d.get("reap"), bool):
            out.reap = d["reap"]
        if isinstance(d.get("scan_s"), (int, float)) and d["scan_s"] > 0:
            out.scan_s = float(d["scan_s"])
        return out


def power_info(p: PowerView) -> dict[str, float]:
    """serverInfo.power（17 §17.20.4；前端以此为准）。"""
    return {k: (int(v) if float(v).is_integer() else float(v)) for k in SERVER_INFO_KEYS for v in (getattr(p, k),)}


def note_input(s: ClientSession, now_ns: int | None = None) -> None:
    """一次交互（call、旧客户端的订阅变更）：最近交互时刻置为当前。"""
    s.last_input_ns = time.time_ns() if now_ns is None else now_ns


def on_presence(s: ClientSession, msg: dict, p: PowerView, now_ns: int | None = None) -> bool:
    """处理一条 presence；返回是否被采纳（限频之内）。"""
    now = time.time_ns() if now_ns is None else now_ns
    s.presence_seen = True
    if s.presence_last_ns and now - s.presence_last_ns < int(p.presence_min_s * 1e9):
        s.presence_dropped += 1
        gw = getattr(s, "gw", None)
        if gw is not None:
            with contextlib.suppress(Exception):
                c = gw.metrics.counters
                c["rt_presence_dropped"] = c.get("rt_presence_dropped", 0) + 1
        return False
    s.presence_last_ns = now
    age = msg.get("input_age_s", 0.0)
    try:
        age_f = min(max(float(age), 0.0), 86400.0)
    except (TypeError, ValueError):
        age_f = 0.0
    s.last_input_ns = max(s.last_input_ns, now - int(age_f * 1e9))
    v = msg.get("visible")
    if isinstance(v, bool):
        s.visible = v
    pw = msg.get("power")
    if pw in POWER_STATES:
        s.power_state = pw
    return True


def idle_limit_s(s: ClientSession, p: PowerView, *, busy: bool) -> float:
    """该连接的无交互阈值（不含余量）。"""
    if s.role in ("operator", "admin") or getattr(s, "identity_tier", "public") == "internal":
        return p.internal_idle_s
    if not getattr(s, "visible", True):
        return p.hidden_s
    return p.viewer_idle_busy_s if busy else p.viewer_idle_s


def reap_due(sessions: list[ClientSession], p: PowerView, *, busy: bool, now_ns: int | None = None) -> list[tuple[ClientSession, str]]:
    """到期应回收的连接与原因（`idle` 或 `hidden`）。"""
    if not p.reap:
        return []
    now = time.time_ns() if now_ns is None else now_ns
    out: list[tuple[ClientSession, str]] = []
    for s in sessions:
        if s.closing or not s.hello:
            continue
        limit = idle_limit_s(s, p, busy=busy) + p.grace_s
        if now - s.last_input_ns > int(limit * 1e9):
            out.append((s, "hidden" if not getattr(s, "visible", True) else "idle"))
    return out


def reclaim_msg(reason: str) -> dict:
    """关闭前的 `error{518, detail{reason}}`。"""
    m = error_msg(IDLE_RECLAIMED, "session", None, "空闲连接已回收（节能模式）")
    m["detail"] = {"reason": reason}
    return m
