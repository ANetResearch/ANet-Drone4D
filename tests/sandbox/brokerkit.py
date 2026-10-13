"""broker 单测的替身：注入时钟、记录副作用（RecordingEffects）、按需完成拉起与停止（`settle`）。"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from sbharness import COSTS, WORLDS, make_sb

from awr.sandbox.broker import Broker, Effects
from awr.sandbox.config import InternalSettings, ParkSettings, PoolSettings, PowerSettings
from awr.sandbox.model import SessionRecord, Ticket
from awr.sandbox.templates import TemplateCatalog
from awr.sandbox.tokens import Keys
from awr.sandbox.worlds import WorldTable

HERE = Path(__file__).resolve().parent
HINT = "0123456789abcdef0123456789abcdef"


class Clock:
    def __init__(self, t: float = 1_791_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def adv(self, s: float) -> None:
        self.t += s


class RecordingEffects(Effects):
    def __init__(self) -> None:
        self.spawned: list[tuple[str, dict | None, bool]] = []
        self.stopped: list[tuple[str, bool]] = []
        self.switched: list[tuple[str, dict]] = []
        self.killed: list[tuple[str, str]] = []
        self.clocks: list[tuple[str, str, dict]] = []
        self.resets: list[tuple[str, dict, str]] = []
        self.events: list[tuple[str, str, dict, int]] = []
        self.audits: list[tuple[str, dict]] = []
        self.tickets: list[Ticket] = []
        self.parked: list[str] = []
        self.resumed: list[str] = []
        self.discarded: list[str] = []
        self.n_changed = 0

    def spawn(self, rec: SessionRecord, cfg_doc: dict | None, *, restart: bool = False) -> None:
        self.spawned.append((rec.sid, cfg_doc, restart))

    def stop(self, rec: SessionRecord, *, export: bool = True) -> None:
        self.stopped.append((rec.sid, export))

    def switch(self, rec: SessionRecord, cfg_doc: dict) -> None:
        self.switched.append((rec.sid, cfg_doc))

    def kill(self, rec: SessionRecord, why: str) -> None:
        self.killed.append((rec.sid, why))

    def clock(self, rec: SessionRecord, op: str, args: dict) -> None:
        self.clocks.append((rec.sid, op, args))

    def reset(self, rec: SessionRecord, cfg_doc: dict, reason: str) -> None:
        self.resets.append((rec.sid, cfg_doc, reason))

    def event(self, kind: str, sid: str, data: dict, severity: int = 1) -> None:
        self.events.append((kind, sid, data, severity))

    def audit(self, kind: str, **kv: Any) -> None:
        self.audits.append((kind, kv))

    def changed(self) -> None:
        self.n_changed += 1

    def ticket_changed(self, t: Ticket) -> None:
        self.tickets.append(t)

    def park(self, rec: SessionRecord) -> None:
        self.parked.append(rec.sid)

    def resume(self, rec: SessionRecord) -> None:
        self.resumed.append(rec.sid)

    def discard_park(self, rec: SessionRecord) -> None:
        self.discarded.append(rec.sid)

    def kinds(self, sid: str | None = None) -> list[str]:
        return [k for k, s, _d, _v in self.events if sid is None or s == sid]


def make_broker(*, n_max: int = 2, public: bool = False, missing: tuple[str, ...] = (), q_cores: float = 2.5,
                delegated: bool = True, park: dict | None = None, internal: dict | None = None, power: dict | None = None,
                **sb_over: Any) -> tuple[Broker, RecordingEffects, Clock]:
    sb = make_sb(max_sessions=n_max, quota_profile="public" if public else "local", **sb_over)
    s = PoolSettings(sb=sb, state_dir=Path("/nonexistent/state"), shm_root=Path("/dev/shm/awr"), q_cores=q_cores,
                     delegated=delegated, access_mode="public" if public else "loopback",
                     park=ParkSettings.from_obj(park), internal=InternalSettings.from_obj(internal),
                     power=PowerSettings.from_obj(power))
    worlds = WorldTable(Path("/nonexistent/worlds"), dict(COSTS), ids=list(WORLDS),
                        probe=lambda _d, w: (w not in missing, "derived_cache_missing" if w in missing else None))
    keys = Keys.derive(secrets.token_bytes(32), secrets.token_bytes(32), mode=s.access_mode)
    fx = RecordingEffects()
    clk = Clock()
    b = Broker(s, worlds, TemplateCatalog(HERE / "fixtures" / "templates"), keys, fx, clock=clk)
    return b, fx, clk


def hint(i: int) -> str:
    return f"{i:032x}"


def create(b: Broker, i: int = 1, *, addr: str = "198.51.100.7", identity: dict | None = None, **req: Any) -> dict:
    args: dict[str, Any] = {"principal_id": "p-" + hint(i), "addr": addr, "req": {"world": "synthcity", **req}}
    if identity is not None:
        args["identity"] = identity
    return b.create(args)


def ready(b: Broker, sid: str) -> None:
    b.on_spawned(sid, 4242, 1)
    b.on_ready(sid)


def finish_parks(b: Broker, fx: RecordingEffects, *, mode: str = "checkpoint", t_sim_ns: int = 12_000_000_000) -> None:
    """完成全部待驻留的会话（池：暂停、checkpoint 落盘、停止进程、清理后回调 on_parked）。"""
    while fx.parked:
        sid = fx.parked.pop(0)
        b.on_parked(sid, mode=mode, t_sim_ns=t_sim_ns, nbytes=4096, park_dir=f"/nonexistent/parked/{sid}")


def finish_discards(b: Broker, fx: RecordingEffects) -> None:
    while fx.discarded:
        sid = fx.discarded.pop(0)
        b.on_ended(sid, f"/nonexistent/snapshots/{sid}.json")


def finish_stops(b: Broker, fx: RecordingEffects) -> None:
    """完成全部待停止的会话（池的停止流程：导出快照、终止进程、清理后回调 on_ended）。"""
    while fx.stopped:
        sid, _export = fx.stopped.pop(0)
        b.on_ended(sid, None)
