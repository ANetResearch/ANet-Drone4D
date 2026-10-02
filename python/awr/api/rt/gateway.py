"""Gateway：60 Hz 墙钟 tick、StateRing 读者、roster 与逐机 channel、TIME 与纪元、事件、RPC、兴趣集、GCS 心跳、低频详情、
进程状态、席位与会话管理（M11 §6.1、§6.4.1；M11-FR-023、FR-024、FR-035、FR-036、FR-046 至 FR-054、FR-071 至 FR-075、
FR-085、FR-087、FR-104；AWR-17 §6.8、§9.7）。

线程模型（M11 §6.2）：全部可变状态只在 asyncio 主线程读写；zenoh 回调线程只做 `loop.call_soon_threadsafe(inbox.append, …)`
或入队（EventSubscriber）。tick 以绝对截止时间 `t0 + k·T` 驱动，落后超过一个周期时跳格（不补发，计 `tick_overruns`）。

on_tick(k)：drain inbox（roster 回复、`state/sim-core/{ext,safety,sensor,detail,env,mission,perf}`、`sys/procs`、liveliness、
席位回复）→ 事件 pump → RingSource.poll（读环、健康、纪元）→ TIME 合成（纪元 + 1 时对全部连接先排 TIME 再置 SNAPSHOT；
无 checkpoint 重启时在途调用以 212 结束）→ TIME 10 Hz 或变化即发 → 各连接 flush 事件、置到期位（sender task 发送时装帧）→
批量进度 → 兴趣集（250 ms 去抖 + 1 Hz 自愈）与 GCS 心跳（5 Hz）→ 每 1 s：窗口与令牌桶重算、`perf/server`、`sys/procs`、
在途表清理。

席位（FR-023；AWR-12 §4.2.2）：权威在 sim-core，api 缓存 `{state, holder}` 并持久化到 `/dev/shm/awr/<run>/gw.seat`
（api 重启后沿用，D1-AC-11a）；持有者最后一个连接关闭 → `seat_grace` + 30 s 墙钟宽限，宽限内同一 principal 重连 →
`seat_resume`，到期 → `seat_expire`；签发 operator token 后 30 s 内既无 WS 连接也无 REST 请求的持有者同样进入宽限；
sim-core 就绪后按缓存幂等重登记；被接管（ext）时对旧持有者的连接发 `error 116` 并以 4403 关闭。
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import json
import logging
import os
import secrets
import sys
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgpack

from awr.contracts import CONTRACTS_VERSION, bus_keys
from awr.contracts import layouts as L
from awr.contracts import topics as T
from awr.contracts.enums import TimeState
from awr.runtime.statering import StateRing

from ..audit import AuditWriter
from ..ratelimit import RateLimiter
from .channels import ENCODES, Channel, ChannelRegistry
from .clock import GatewayClock, Health, _atomic_write
from .detail import DetailDemux, EnvCache
from .events import EventIngest
from .interest import GcsBeacon, InterestAggregator
from .metrics import Metrics
from .playback import PlaybackController, unpack_or_none
from .protocol import PROTOCOL, error_msg, jdump, status_msg
from .rpc import ClientPublish, RpcRouter
from .session import CTRL_MAX, MAX_HIGH_RATE_FULL, MAX_SUBS, ClientSession
from .sources.live import LiveSource
from .sources.replay import ReplaySource

if TYPE_CHECKING:
    from ..security import TokenService
    from ..settings import ApiSettings
    from .scheduler import SubChan

__all__ = ["Gateway"]

log = logging.getLogger("awr.api.gateway")

TICK_HZ = T.TICK_HZ
PERIOD_NS = 1_000_000_000 // TICK_HZ
SEAT_GRACE_S = 30.0  # 席位宽限（墙钟，AWR-12 §4.2.2 T03；BIZ-FR-007）
SEAT_UNCONNECTED_S = 30.0  # 签发后既无 WS 连接也无 REST 请求时进入宽限的等待（本文设定，SK-E2E-to-M11 第 1 条）
LAG_PERIOD_S = 0.1
FAULT_TIMEOUT_S = 2.5
FAULT_REARM_S = 0.5
INFO_SCHEMAS = ("awr.DroneState64.v1", "awr.SwarmLite32.v1", "awr.EnvSample32.v1", "awr.VelSetpoint16.v1",
                "awr.SensorPose48.v1", "awr.rt.BatchHeader.v1", "awr.rt.RecordHeader.v1", "awr.rt.Time.v1",
                "awr.rt.ClientDataHeader.v1")
FIXED = ((1, "fleet/roster"), (2, "swarm/uav/state"), (3, "env/state"), (4, "perf/server"), (5, "sys/procs"), (6, "event"),
         (7, "perf/clients"), (8, "agent/tasks"))
PATH_BLOB_MAX = 256 * 1024
UAV_SUFFIXES = ("state", "state_ext", "safety", "env")
CAPS_DIR = Path(__file__).resolve().parents[4] / "packages" / "contracts" / "rt" / "caps"
DEFAULT_CLOCK = {"mode": "lockstep", "pausable": True, "max_speed": 10, "steppable": True}
PROC_STATUS_SKIP_REASONS = ("module_missing",)
# 启动或就绪时主动补拉其事件的辅助生产者（FX-WEB2-to-M11 第 2 条）：它们可能在 api 订阅之前发出事件、此后长时间再无后续
# 事件（demo 下 recorder 的 `rec.started`），首见补拉不会触发。事件纪元约定为"重启次数 + 1"（AWR_RESTART_COUNT + 1）；
# 受监管时等到首个 `sys/procs` 回复取得重启次数再探测，PROBE_FALLBACK_S 内仍无回复则按纪元 1 探测
PROBE_PRODUCERS = ("recorder", "agent-runtime", "job-worker")
PROBE_FALLBACK_S = 3.0


def _with_producer(key: str, producer: str) -> str:
    """把生成的 `state/sim-core/<x>` 常量换成另一生产者（bus_keys 只为 ext、safety、sensor 生成了按生产者的构造函数；
    mission、env 的构造函数已请求 M00 补充）。"""
    parts = key.split("/")
    parts[1] = producer
    return "/".join(parts)


def _load_caps() -> dict[str, dict]:
    out: dict[str, dict] = {}
    with contextlib.suppress(OSError):
        for p in sorted(CAPS_DIR.glob("*.json")):
            with contextlib.suppress(OSError, ValueError):
                d = json.loads(p.read_text(encoding="utf-8"))
                out[str(d.get("backend") or p.stem)] = d
                out.setdefault(p.stem, d)
    return out


class Gateway:
    def __init__(self, settings: ApiSettings, bus: Any, tokens: TokenService, *, ring_cls: type = StateRing,
                 world_info: dict | None = None, subscribe_events: bool = True, audit: AuditWriter | None = None,
                 limiter: RateLimiter | None = None, confirm_key: bytes | None = None) -> None:
        self.settings = settings
        self.bus = bus
        self.tokens = tokens
        self.audit = audit if audit is not None else AuditWriter(None)
        self.limiter = limiter if limiter is not None else RateLimiter()
        self.session_id = "gw-" + secrets.token_hex(6)
        self.mode = "live"
        self.clock = GatewayClock(settings.run_dir)
        self.registry = ChannelRegistry()
        for cid, topic in FIXED:
            self.registry.add_fixed(cid, topic)
        self.roster_ch = self.registry.by_topic["fleet/roster"]
        self.event_ch = self.registry.by_topic["event"]
        self.perf_ch = self.registry.by_topic["perf/server"]
        self.procs_ch = self.registry.by_topic["sys/procs"]
        self.perf_clients_ch = self.registry.by_topic["perf/clients"]
        self.registry.on_advertise = self._advertise_delta
        self.registry.on_unadvertise = self._unadvertise_delta
        self.proc_states: dict[str, str] = {}
        self.proc_items: list[dict] = []
        self.source = LiveSource(settings.ring_path, self.registry, self.clock, name=settings.producer,
                                 ring_cls=ring_cls, on_roster_change=self._request_roster,
                                 on_layout_error=self._layout_error, sup_state=self.sup_state)
        self.live_source = self.source
        self.replay_source: ReplaySource | None = None
        self.ring_cls = ring_cls
        self.sessions: list[ClientSession] = []
        self.rpc = RpcRouter(self, confirm_key=confirm_key)
        self.playback = PlaybackController(self)
        self.cpub = ClientPublish(self)
        self.env = EnvCache(self, self.registry.by_topic["env/state"])
        self.demux = DetailDemux(self)
        self.interest = InterestAggregator(self)
        self.gcs = GcsBeacon(self)
        self.metrics = Metrics()
        self.events = EventIngest(self, subscribe=subscribe_events)
        self.inbox: deque[tuple] = deque()
        self.roster: dict[str, dict] = {}
        self.roster_loaded = False
        self.by_agent_no: dict[int, str] = {}
        self.sensor_map: dict[tuple[int, int], Channel] = {}
        self.gw_roster_version = 0
        self.caps = _load_caps()
        self.clock_caps = dict(DEFAULT_CLOCK)
        self.seat: dict[str, Any] = {"state": "FREE", "holder": None}
        self.seat_since_unix_ns: int | None = None
        self.seat_claimed_mono = 0
        self.seat_grace_s = SEAT_GRACE_S
        self.seat_unconnected_s = SEAT_UNCONNECTED_S
        self._seat_grace: asyncio.TimerHandle | None = None
        self._seat_unconnected: dict[str, asyncio.TimerHandle] = {}
        self.principal_last_ping: dict[str, int] = {}
        self.principal_last_rest: dict[str, int] = {}
        self.frame_t_sim_ns = 0
        self.world_info = {k: v for k, v in (world_info or {"id": settings.world_id, "frame": "world"}).items()
                           if v is not None}
        self.statuses: dict[str, dict] = {}
        self.sim_perf: dict[str, Any] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.k = 0
        self.stopping = False
        self.switching = False
        self._tasks: list[asyncio.Task] = []
        self._handles: list[Any] = []
        self.tick_overruns = 0
        self.stats = {"ticks": 0, "roster_fetches": 0, "bumps": 0, "resets": 0, "restarts": 0}
        self.last_health = Health.UNATTACHED
        self._roster_inflight = False
        self._roster_pending = False
        self._fault_armed = False
        self._stop_task: asyncio.Task | None = None
        self._closed_bytes = 0
        self._sec = 0
        self._bg: set[asyncio.Future] = set()
        self._probe_wanted: dict[str, int] = {}  # 生产者 -> 就绪时刻（monotonic ns），等待取得重启次数后探测
        self._procs_known = False  # 已收到 sys/procs 回复
        self._load_seat()

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        self.loop = loop = asyncio.get_running_loop()
        b = self.bus
        p = self.settings.producer

        # subscribe/watch 回调只入队（PY-CB-01）；stop() 先关闭这些句柄，事件循环关闭之后不会再回调
        def sub(key: str, kind: str, prod: str = p):
            return b.subscribe(key, lambda _k, raw: loop.call_soon_threadsafe(self.inbox.append, (kind, raw, prod)))

        self._handles += [
            sub(bus_keys.state_ext(p), "ext"),
            sub(bus_keys.state_safety(p), "safety"),
            sub(bus_keys.state_sensor(p), "sensor"),
            sub(bus_keys.STATE_DETAIL, "detail"),
            sub(bus_keys.STATE_ENV, "env"),
            sub(bus_keys.STATE_MISSION, "mission"),
            sub(bus_keys.STATE_PERF, "perf"),
            # 回放生产者（D1-ext）：路径与实时完全相同（17 §9.7 第 7 条），只在回放模式下被采用
            sub(bus_keys.state_ext("replay"), "ext", "replay"),
            sub(bus_keys.state_safety("replay"), "safety", "replay"),
            sub(bus_keys.state_sensor("replay"), "sensor", "replay"),
            sub(_with_producer(bus_keys.STATE_MISSION, "replay"), "mission", "replay"),
            sub(_with_producer(bus_keys.STATE_ENV, "replay"), "env", "replay"),
            sub(bus_keys.state_agent("agents"), "agents", "agent-runtime"),
            sub(bus_keys.state_agent("tasks"), "tasks", "agent-runtime"),
            b.watch(bus_keys.proc_ready(p), lambda k, alive: loop.call_soon_threadsafe(self.inbox.append, ("ready", alive))),
            b.watch(bus_keys.proc_alive(p), lambda k, alive: loop.call_soon_threadsafe(self.inbox.append, ("alive", alive))),
            # 辅助生产者就绪（带历史：api 启动时已在运行的立即回调）→ 主动补拉其事件
            *(b.watch(bus_keys.proc_ready(n),
                      lambda k, alive, n=n: loop.call_soon_threadsafe(self.inbox.append, ("side_ready", n, alive)))
              for n in PROBE_PRODUCERS),
        ]
        self._tasks.append(asyncio.ensure_future(self._ticker()))
        self._tasks.append(asyncio.ensure_future(self._lag_sampler()))
        if self.settings.supervised:
            self._tasks.append(asyncio.ensure_future(self._fault_watchdog()))
        if self.seat.get("holder") and self.seat.get("state") in ("HELD", "GRACE"):
            # api 重启：沿用 gw.seat 中的持有者；持有者尚未重连时进入宽限（重连即恢复 HELD）
            holder = self.seat["holder"]
            self.seat["state"] = "HELD"
            self.seat_claimed_mono = time.monotonic_ns()
            self._seat_session_closed(holder)

    def _post(self, item: tuple) -> None:
        """`call_cb` 回复回调 → 主线程收件箱（只入队）。在途查询无法随 stop() 撤销：总线在事件循环关闭之后才关闭时，
        以 BusClosed 回调在途查询，此前在回调线程抛 RuntimeError（Event loop is closed）；循环已关闭时丢弃。"""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self.inbox.append, item)

    async def begin_stop(self, reason: str = "sigterm") -> None:
        """FR-104：置 stopping（新 call 与写类 REST 返回 213）→ 广播 `sys.shutting_down` 事件与 `status proc.api` →
        排空控制面（≤ 0.5 s）→ 以 1001 关闭全部 WS。"""
        if self.stopping:
            return
        self.stopping = True
        self.emit_api_event("sys.shutting_down", 1, {"proc": "api", "reason": reason})
        self.set_status(status_msg("proc.api", "warning", "api 正在停止", source="api", code=213))
        for s in list(self.sessions):
            s.flush_events()
        end = time.monotonic() + 0.5
        while time.monotonic() < end and any(s.ctrl and not s.closing for s in self.sessions):
            await asyncio.sleep(0.01)
        for s in list(self.sessions):
            s.closing = True
            s.wake.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(s.ws.close(code=1001), 1.0)
        self.audit.write("proc.api.stopping", detail={"reason": reason})

    async def stop(self) -> None:
        await self.begin_stop("shutdown")
        if self._fault_armed:
            with contextlib.suppress(Exception):
                faulthandler.cancel_dump_traceback_later()
        if self._seat_grace is not None:
            self._seat_grace.cancel()
            self._seat_grace = None
        for h in self._seat_unconnected.values():
            h.cancel()
        self._seat_unconnected.clear()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        for t in list(self.rpc.tasks):
            t.cancel()
        for h in self._handles:
            with contextlib.suppress(Exception):
                h.close()
        for closer in (self.playback.close, self.interest.close, self.gcs.close, self.cpub.close, self.events.close,
                       self.source.close):
            with contextlib.suppress(Exception):
                closer()

    async def _ticker(self) -> None:
        t0 = time.monotonic_ns()
        k = 0
        while not self.stopping:
            k += 1
            deadline = t0 + k * PERIOD_NS
            lag = time.monotonic_ns() - deadline
            if lag > PERIOD_NS:
                skip = lag // PERIOD_NS
                k += int(skip)
                self.tick_overruns += int(skip)
                continue
            if lag < 0:
                await asyncio.sleep(-lag / 1e9)
            else:
                await asyncio.sleep(0)  # 落后不足一个周期时也让出一次，避免连续 tick 饿死 sender 与接收协程
            try:
                self.on_tick(k)
            except Exception:
                log.exception("gateway tick failed")

    async def _lag_sampler(self) -> None:
        """事件循环延迟（10 Hz 调度偏差，FR-087）。"""
        while True:
            t = time.monotonic()
            await asyncio.sleep(LAG_PERIOD_S)
            self.metrics.lag.add((time.monotonic() - t - LAG_PERIOD_S) * 1e3)

    async def _fault_watchdog(self) -> None:
        """faulthandler.dump_traceback_later(2.5) 每 0.5 s 重新布置：事件循环卡住 2.5 s 即转储全部线程栈（FR-087）。"""
        self._fault_armed = True
        while True:
            with contextlib.suppress(Exception):
                faulthandler.dump_traceback_later(FAULT_TIMEOUT_S, repeat=False, file=sys.stderr)
            await asyncio.sleep(FAULT_REARM_S)

    # ------------------------------------------------------------ tick
    def on_tick(self, k: int) -> None:
        self.k = k
        self.stats["ticks"] += 1
        t = time.monotonic_ns()
        self._drain_inbox()
        self.events.pump()
        poll = self.source.poll(k, t)
        self.clock.update(poll.header, poll.health, replay=self.mode == "replay")
        bumped = False
        if poll.global_bump:
            self.bump_epoch(poll.reason)
            if poll.restarted:
                self.stats["restarts"] += 1
                self.rpc.on_producer_restart(self.source.name)
            bumped = True
        if poll.reset:
            self.stats["resets"] += 1
        if poll.health != self.last_health:
            self._on_health(poll.health)
        tb = None if bumped else self.clock.time_bytes_if_due(k)
        if poll.new_frame or self.frame_t_sim_ns == 0:
            self.frame_t_sim_ns = poll.frame_t_sim_ns
        if k // TICK_HZ != self._sec:
            # 每 1 s（墙钟；按 tick 序号换算，tick 跳格时不漏）：窗口与令牌桶、perf/server、sys/procs、在途表清理；
            # 放在置到期位之前，使 1 Hz 订阅在同一个对齐 tick 取到本秒的新值
            self._sec = k // TICK_HZ
            for s in self.sessions:
                s.tick_1hz()
            self._publish_perf()
            self._query_procs()
            if self._probe_wanted:
                self._issue_probes()
            self.rpc.gc()
        for s in self.sessions:
            if not s.hello or s.closing:
                continue
            if tb is not None:
                s.send_ctrl(tb)
            s.flush_events()
            s.mark_due(k)
        self.rpc.tick(t)
        self.interest.maybe_push(t)
        self.gcs.maybe_publish(t)
        age = (t - self.source.p.t_pub_ns) / 1e6 if poll.new_frame else None
        self.metrics.on_tick(age, (time.monotonic_ns() - t) / 1e6)

    def bump_epoch(self, reason: str) -> None:
        """全局 epoch + 1：写 gw.epoch → 对全部已 hello 的连接先排 TIME、再置 SNAPSHOT 与到期位（FR-048）。"""
        self.clock.bump(reason)
        self.stats["bumps"] += 1
        tb = self.clock.time_bytes()
        for s in self.sessions:
            if s.hello and not s.closing:
                s.on_epoch_bump(tb)
        log.info("global epoch bumped", extra={"kv": {"epoch": self.clock.global_epoch, "reason": reason}})

    # ------------------------------------------------------------ 回放模式（D1-ext，playback.py 驱动）
    def enter_replay(self, gen: int, backfill: Any) -> None:
        """open 回复：切到 ReplaySource、装入 backfill、mode = replay、全局 epoch + 1（TIME bit7）、重发 serverInfo。"""
        if self.replay_source is None:
            self.replay_source = ReplaySource(Path(self.settings.run_dir) / "state.replay", self.registry, self.clock,
                                              name="replay", ring_cls=self.ring_cls,
                                              on_roster_change=self._request_roster,
                                              on_layout_error=self._layout_error, sup_state=self.sup_state)
            self.replay_source.uav_state = self.live_source.uav_state  # 逐机 channel 表共用（roster 更新对两者同时生效）
        self.replay_source.known_gens.add(int(gen))
        self.source = self.replay_source
        self.mode = "replay"
        self.env.reset()
        self._load_backfill(backfill)
        self._request_roster("replay")
        self._poll_now()
        self.bump_epoch("playback.open")
        self._resend_server_info()

    def replay_seek(self, gen: int, backfill: Any) -> None:
        """seek 回复：先原子装入 backfill（各 channel seq + 1），再切 epoch（TIME 入队）；调用方随后排入 playbackState。"""
        if self.replay_source is not None:
            self.replay_source.known_gens.add(int(gen))
        self.env.reset()
        self._load_backfill(backfill)
        self._poll_now()
        self.bump_epoch("playback.seek")

    def exit_replay(self) -> None:
        """close：切回 LiveSource、mode = live、全局 epoch + 1、重发 serverInfo；实时 roster 与低频状态重新拉取。"""
        rs = self.replay_source
        self.source = self.live_source
        self.mode = "live"
        if rs is not None:
            rs.close()
            self.replay_source = None
        self.env.reset()
        self._request_roster(self.settings.producer)
        self.live_source.p.roster_version = -1  # 切回后按实时帧重建行号表
        self._poll_now()
        self.bump_epoch("playback.close")
        self._resend_server_info()

    def _poll_now(self) -> None:
        """模式切换时立即读一次当前源（新纪元的首个 TIME 带新源的时钟与状态）。"""
        t = time.monotonic_ns()
        self.source.p.last_attach_try_ns = 0
        poll = self.source.poll(self.k, t)
        self.clock.update(poll.header, poll.health, replay=self.mode == "replay")
        if poll.new_frame:
            self.frame_t_sim_ns = poll.frame_t_sim_ns

    def _load_backfill(self, bf: Any) -> None:
        bf = unpack_or_none(bf)
        if not isinstance(bf, dict):
            return
        roster = unpack_or_none(bf.get("roster"))
        if isinstance(roster, dict):
            self._apply_roster(roster)  # 不经 _on_roster：不改在途查询状态（FX-GW，M12-to-M11 第 2 条）
        if isinstance(bf.get("env"), (bytes, bytearray)):
            self.env.on_heartbeat(bytes(bf["env"]))
        for suffix, key in (("state_ext", "state_ext"), ("safety", "safety")):
            if isinstance(bf.get(key), (bytes, bytearray)):
                self.demux.feed_pairs(suffix, bytes(bf[key]), all_channels=True)
        for mb in bf.get("missions") or []:
            if isinstance(mb, (bytes, bytearray)):
                one = unpack_or_none(bytes(mb))
                if isinstance(one, dict):
                    self.demux.feed_mission(msgpack.packb([one], use_bin_type=True))
        if isinstance(bf.get("sensor"), (bytes, bytearray)):
            self.demux.feed_sensor(bytes(bf["sensor"]), all_channels=True)

    def _resend_server_info(self) -> None:
        for s in self.sessions:
            if s.hello and not s.closing:
                s.send_ctrl(self.server_info(s))

    def _drain_inbox(self) -> None:
        active = self.source.name
        while self.inbox:
            item = self.inbox.popleft()
            kind = item[0]
            if kind in ("ext", "safety", "sensor", "detail", "env", "mission") and item[2] != active:
                continue  # 非当前模式的生产者（回放中忽略实时低频状态，反之亦然）
            try:
                if kind == "roster":
                    self._on_roster(item[1], item[2], item[3] if len(item) > 3 else None)
                elif kind == "ext":
                    self.demux.feed_pairs("state_ext", item[1])
                elif kind == "safety":
                    self.demux.feed_pairs("safety", item[1])
                elif kind == "sensor":
                    self.demux.feed_sensor(item[1])
                elif kind == "detail":
                    self.demux.feed_detail(item[1])
                elif kind == "env":
                    self.env.on_heartbeat(item[1])
                elif kind == "mission":
                    self.demux.feed_mission(item[1])
                elif kind == "perf":
                    self.sim_perf = msgpack.unpackb(item[1], raw=False)
                elif kind == "agents":
                    self.demux.feed_agents(item[1])
                elif kind == "tasks":
                    self.demux.feed_tasks(item[1])
                elif kind == "procs":
                    self._on_procs(item[1])
                elif kind == "ready":
                    if item[1]:
                        self._on_producer_ready()
                elif kind == "alive":
                    self.live_source.note_liveliness(bool(item[1]))
                elif kind == "side_ready":
                    if item[2]:
                        self._probe_wanted.setdefault(item[1], time.monotonic_ns())
                        self._issue_probes()
                elif kind == "seat":
                    rep = item[1]
                    if isinstance(rep, dict) and isinstance(rep.get("seat"), dict):
                        self.update_seat(rep["seat"])
            except Exception:
                log.exception("gateway inbox item failed", extra={"kv": {"kind": kind}})

    # ------------------------------------------------------------ 健康、状态横幅与进程表
    def _on_health(self, h: str) -> None:
        self.last_health = h
        if h == Health.OK:
            self.clear_status("proc.sim-core")
            return
        msg = {Health.STALLED: "仿真心跳停滞", Health.DOWN: "仿真进程不可达", Health.UNATTACHED: "仿真尚未就绪",
               Health.RESTARTING: "仿真重启中", Health.FAILED: "仿真进程熔断"}.get(h, h)
        self.set_status(status_msg("proc.sim-core", "warning" if h != Health.FAILED else "error", msg,
                                   source="sim-core", code=211))

    def set_status(self, st: dict) -> None:
        if self.statuses.get(st["id"]) == st:
            return
        self.statuses[st["id"]] = st
        self._broadcast(st)

    def clear_status(self, sid: str) -> None:
        if self.statuses.pop(sid, None) is not None:
            self._broadcast({"op": "removeStatus", "ids": [sid]})

    def _broadcast(self, m: dict | str | bytes) -> None:
        mm = m if isinstance(m, (str, bytes)) else jdump(m)
        for s in self.sessions:
            if not s.closing:
                s.send_ctrl(mm)

    def sup_state(self, name: str) -> str | None:
        return self.proc_states.get(name)

    def _query_procs(self) -> None:
        loop = self.loop
        if loop is None or not self.settings.has_supervisor:
            return
        self.bus.call_cb(bus_keys.SYS_PROCS, {"v": 1},
                         lambda rep, err: self._post(("procs", rep if err is None else None)),
                         timeout=0.5, retries=0)

    def _on_procs(self, rep: Any) -> None:
        if not isinstance(rep, dict):
            return
        items = [dict(it) for it in rep.get("items") or [] if isinstance(it, dict) and "name" in it]
        for it in items:
            it.pop("log_tail", None)
        self.proc_items = items
        self._procs_known = True
        if self._probe_wanted:
            self._issue_probes()
        for it in items:
            self._proc_status(str(it["name"]), str(it.get("state")), it)
        self.procs_ch.publish(msgpack.packb({"items": items}, use_bin_type=True), self.frame_t_sim_ns)

    def _issue_probes(self) -> None:
        """对就绪的辅助生产者发起事件探测（`EventIngest.probe`）。纪元 = 该进程的重启次数 + 1：受监管时等首个 `sys/procs`
        回复（≤ 1 s 一次），PROBE_FALLBACK_S 后仍未取得则按 1；不受监管（测试、inproc）时立即按 1。"""
        now = time.monotonic_ns()
        for name, t0 in list(self._probe_wanted.items()):
            restarts = 0
            if self.settings.has_supervisor:
                it = next((it for it in self.proc_items if it.get("name") == name), None)
                if it is not None and isinstance(it.get("restarts"), int):
                    restarts = max(0, int(it["restarts"]))
                elif not self._procs_known and now - t0 < int(PROBE_FALLBACK_S * 1e9):
                    continue  # 等 sys/procs
            del self._probe_wanted[name]
            if self.events.probe(name, restarts + 1):
                self.stats["event_probes"] = self.stats.get("event_probes", 0) + 1

    def on_proc_state(self, data: dict) -> None:
        """supervisor `evt/supervisor/proc`（kind `proc.state`，data `{name, from, to, restarts, rc, reason}`）。"""
        name, to = data.get("name"), data.get("to")
        if isinstance(name, str) and isinstance(to, str):
            known = next((it for it in self.proc_items if it.get("name") == name), {})
            self._proc_status(name, to, {"last_reason": data.get("reason"), "on_demand": bool(known.get("on_demand"))})
            self.audit.write("proc.state", detail={"name": name, "from": data.get("from"), "to": to,
                                                   "reason": data.get("reason")})

    def _proc_status(self, name: str, state: str, it: dict) -> None:
        self.proc_states[name] = state
        sid = f"proc.{name}"
        if name == self.settings.producer or name == "api":
            return  # sim-core 由生产者健康驱动；api 自身停止时单独发 proc.api
        skip = (state == "STOPPED" and it.get("on_demand")) or (state == "FAILED" and it.get("last_reason")
                                                                 in PROC_STATUS_SKIP_REASONS)
        if state == "RUNNING" or skip:
            self.clear_status(sid)
            return
        level = "error" if state == "FAILED" else "warning"
        self.set_status(status_msg(sid, level, f"进程 {name} 状态 {state}", source="supervisor",
                                   code=213 if state == "FAILED" else None))

    def _layout_error(self, err: str) -> None:
        self.set_status(status_msg("ring.layout_mismatch", "error", "二进制布局哈希不一致", source="sim-core", code=312))

    # ------------------------------------------------------------ roster 与逐机 channel（FR-035）
    def roster_producer(self) -> str:
        """当前模式的名册生产者：回放模式为 replay-worker（`ctl/replay/roster`），实时为 sim-core。"""
        return "replay" if self.mode == "replay" else self.settings.producer

    def _request_roster(self, producer: str) -> None:
        """查询 `ctl/<producer>/roster`。只接受当前模式的生产者（实时事件在回放中触发的查询忽略，模式切换时各自重取）；
        已有查询在途时只置 pending，回复到达后按**当前模式**的生产者重查（M12-to-M11 第 2 条）。"""
        if producer != self.roster_producer():
            self.stats["roster_ignored"] = self.stats.get("roster_ignored", 0) + 1
            return
        if self._roster_inflight:
            self._roster_pending = True
            return
        loop = self.loop
        if loop is None:
            return
        self._roster_inflight = True
        self.stats["roster_fetches"] += 1
        self.bus.call_cb(bus_keys.ctl_roster(producer), {"v": 1},
                         lambda rep, err: self._post(("roster", rep, err, producer)),
                         timeout=1.0, retries=2)

    def _on_producer_ready(self) -> None:
        self._request_roster(self.settings.producer)
        holder = self.seat.get("holder")
        if holder:  # sim-core 重启就绪后按缓存幂等重登记席位（M11-FR-023）
            self._seat_op("seat_claim", holder)

    def on_path_changed(self, data: dict) -> None:
        """M10 `path.changed{vehicle_id, traj_id, revision, file, bytes}`：读 `/dev/shm/awr/<run>/paths/<file>`（≤ 64 KiB 的
        `awr.blob.polyline4.v1`）推送 `uav/{id}/path`；超过 256 KB 的 blob 不进 WS（FR-074，17 §6.5）。"""
        vid, name = data.get("vehicle_id"), data.get("file")
        if not isinstance(vid, str) or vid not in self.roster or not isinstance(name, str):
            return
        base = (Path(self.settings.run_dir) / "paths").resolve()
        p = (base / name).resolve()
        if base not in p.parents:
            return
        try:
            if p.stat().st_size > PATH_BLOB_MAX:
                log.warning("path blob too large for WS", extra={"kv": {"uav": vid, "bytes": p.stat().st_size}})
                return
            blob = p.read_bytes()
        except OSError:
            return
        try:
            ch = self.registry.get_or_create(f"uav/{vid}/path", entity={"kind": "uav", "id": vid},
                                             producer=self.settings.producer)
        except ValueError:
            return
        ch.publish(blob, self.frame_t_sim_ns)

    def on_producer_started(self, ev: dict) -> None:
        self._request_roster(self.settings.producer)

    def _on_roster(self, rep: Any, err: Any, producer: str | None = None) -> None:
        self._roster_inflight = False
        want = self.roster_producer()
        stale = producer is not None and producer != want
        if self._roster_pending or stale:
            # 在途期间又有变化，或回复来自上一模式的生产者（open 时在途的实时查询）：按当前模式的生产者重查
            self._roster_pending = False
            self._request_roster(want)
        if stale:
            self.stats["roster_stale"] = self.stats.get("roster_stale", 0) + 1
            return  # 丢弃：回放模式不装入实时名册，反之亦然（M12-to-M11 第 2 条）
        if err is not None or not isinstance(rep, dict):
            return
        self._apply_roster(rep)

    def _apply_roster(self, rep: dict) -> None:
        self.roster_loaded = True
        entries = [e for e in rep.get("entries") or [] if isinstance(e, dict) and isinstance(e.get("id"), str)]
        new_ids = {e["id"]: e for e in entries}
        gone: list[int] = []
        for vid in [v for v in self.roster if v not in new_ids]:
            gone += self._drop_vehicle(vid)
        added: list[Channel] = []
        for vid, e in new_ids.items():
            if vid not in self.roster or self.roster[vid].get("agent_no") != e.get("agent_no"):
                if vid in self.roster:
                    gone += self._drop_vehicle(vid)
                added += self._add_vehicle(vid, e)
            else:
                added += self._sync_sensors(vid, e)
            self.roster[vid] = e
            self.by_agent_no[int(e["agent_no"])] = vid
        self.gw_roster_version += 1
        self.roster_ch.publish(msgpack.packb({"roster_version": self.gw_roster_version, "entries": entries},
                                             use_bin_type=True), self.frame_t_sim_ns)
        if gone:
            self._unadvertise_delta(gone)
        if added:
            self._advertise_delta(added)
        self._update_clock_caps()

    def _add_vehicle(self, vid: str, e: dict) -> list[Channel]:
        ent = {"kind": e.get("kind", "uav") or "uav", "id": vid}
        prod = e.get("producer") or self.settings.producer
        chans: list[Channel] = []
        for suffix in UAV_SUFFIXES:
            try:
                chans.append(self.registry.get_or_create(f"uav/{vid}/{suffix}", entity=ent, announce=False, producer=prod))
            except ValueError:
                return []
        no = int(e["agent_no"])
        self.source.attach_uav_channel(no, chans[0])
        return chans + self._sync_sensors(vid, e)

    def _sync_sensors(self, vid: str, e: dict) -> list[Channel]:
        no = int(e["agent_no"])
        out: list[Channel] = []
        ent = {"kind": e.get("kind", "uav") or "uav", "id": vid}
        for s in e.get("sensors") or []:
            if not isinstance(s, dict) or not isinstance(s.get("name"), str) or not isinstance(s.get("sensor_no"), int):
                continue
            topic = f"uav/{vid}/sensor/{s['name']}/pose"
            if topic in self.registry.by_topic:
                self.sensor_map[(no, s["sensor_no"])] = self.registry.by_topic[topic]
                continue
            try:
                ch = self.registry.get_or_create(topic, entity=ent, announce=False, producer=e.get("producer"))
            except ValueError:
                continue
            self.sensor_map[(no, s["sensor_no"])] = ch
            out.append(ch)
        return out

    def _drop_vehicle(self, vid: str) -> list[int]:
        e = self.roster.pop(vid, None)
        if e is None:
            return []
        no = int(e["agent_no"])
        self.by_agent_no.pop(no, None)
        self.source.detach_uav_channel(no)
        for k in [k for k in self.sensor_map if k[0] == no]:
            del self.sensor_map[k]
        ids: list[int] = []
        for topic in [t for t in self.registry.by_topic if t.startswith(f"uav/{vid}/")]:
            c = self.registry.remove(topic, announce=False)
            if c is not None:
                ids.append(c.id)
        return ids

    def _update_clock_caps(self) -> None:
        """`serverInfo.clock` 由 roster 中在场后端的 `caps.clock` 合成；变化时对全部连接重发 serverInfo（FR-054）。"""
        refs = {str(e.get("caps_ref") or e.get("backend") or "mock") for e in self.roster.values()} or {"mock"}
        clocks = [self.caps.get(r, {}).get("clock") or DEFAULT_CLOCK for r in sorted(refs)]
        non = [c for c in clocks if c.get("mode") != "lockstep"]
        if non:
            cc = {"mode": non[0].get("mode", "live"), "pausable": False, "max_speed": 1, "steppable": False}
        else:
            cc = {"mode": "lockstep", "pausable": all(c.get("pausable", True) for c in clocks),
                  "max_speed": min(float(c.get("max_speed", 10)) for c in clocks),
                  "steppable": all(c.get("steppable", True) for c in clocks)}
            if float(cc["max_speed"]).is_integer():
                cc["max_speed"] = int(cc["max_speed"])
        if cc != self.clock_caps:
            self.clock_caps = cc
            for s in self.sessions:
                if s.hello and not s.closing:
                    s.send_ctrl(self.server_info(s))

    def _advertise_delta(self, chans: list[Channel]) -> None:
        m = jdump({"op": "advertise", "channels": [c.advert() for c in chans]})
        for s in self.sessions:
            s.send_ctrl(m)
            s.on_new_channels(chans)

    def _unadvertise_delta(self, ids: list[int]) -> None:
        m = jdump({"op": "unadvertise", "ids": list(ids)})
        for s in self.sessions:
            s.send_ctrl(m)
            s.on_removed_channels(ids)
        self.interest.on_subs_changed()

    def roster_entry(self, vid: Any) -> dict | None:
        return self.roster.get(vid) if isinstance(vid, str) else None

    def roster_ids(self) -> list[str]:
        return [self.by_agent_no[n] for n in sorted(self.by_agent_no)]

    def agent_no_of(self, vid: str) -> int | None:
        e = self.roster.get(vid)
        return int(e["agent_no"]) if e is not None else None

    def sensor_channel(self, agent_no: int, sensor_no: int) -> Channel | None:
        return self.sensor_map.get((agent_no, sensor_no))

    # ------------------------------------------------------------ 订阅钩子
    def on_subchan_added(self, s: ClientSession, sc: SubChan) -> None:
        """新 SubChan：懒生产的逐机 channel 立即取当前值（下一帧 SNAPSHOT 即带当前状态）；`perf/server` 以最近一秒的值
        加上当前全部连接的行重发一次，使新连接的 SNAPSHOT 含自己的 conn_id（M11-net-to-M11 第 4 条）。"""
        ch = sc.channel
        if ch is self.perf_ch and self.metrics.last:
            msg = dict(self.metrics.last, clients=[x.perf_row() for x in self.sessions])
            ch.publish(msgpack.packb(msg, use_bin_type=True), self.frame_t_sim_ns)
            return
        if ch.seq != 0 or not ch.entity:
            return
        if ch.is_full64:
            no = self.agent_no_of(ch.entity["id"])
            if no is not None:
                self.source.fill_uav_channel(no, ch)
        elif ch.encoding == "msgpack":
            self.demux.fill(ch)

    def on_subs_changed(self, s: ClientSession) -> None:
        self.interest.on_subs_changed()

    # ------------------------------------------------------------ 席位与 GCS（FR-023、FR-024）
    def _seat_path(self) -> Path | None:
        return Path(self.settings.run_dir) / "gw.seat" if self.settings.run_dir is not None else None

    def _load_seat(self) -> None:
        p = self._seat_path()
        if p is None:
            return
        with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
            d = msgpack.unpackb(p.read_bytes(), raw=False)
            if d.get("holder") and d.get("state") in ("HELD", "GRACE"):
                self.seat = {"state": d["state"], "holder": d["holder"]}
                self.seat_since_unix_ns = d.get("since_unix_ns")

    def _save_seat(self) -> None:
        p = self._seat_path()
        if p is None:
            return
        with contextlib.suppress(OSError):
            p.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(p, msgpack.packb({"v": 1, "state": self.seat.get("state"), "holder": self.seat.get("holder"),
                                            "since_unix_ns": self.seat_since_unix_ns}))

    def update_seat(self, seat: dict) -> None:
        holder = seat.get("holder")
        state = seat.get("state", "FREE") if holder else "FREE"
        if holder != self.seat.get("holder"):
            self.seat_since_unix_ns = time.time_ns() if holder else None  # leaseReply.seat 不带起始时间，按 api 首次观察记
            self.seat_claimed_mono = time.monotonic_ns()
        if self.seat.get("state") == "GRACE" and state == "HELD" and holder == self.seat.get("holder") and \
                self._seat_grace is not None and not any(s.principal.id == holder for s in self.sessions):
            state = "GRACE"  # api 宽限计时进行中（sim-core 可能尚未处理 seat_grace）
        new = {"state": state, "holder": holder}
        changed = new != self.seat
        holder_changed = holder != self.seat.get("holder")
        self.seat = new
        if holder is None and self._seat_grace is not None:
            self._seat_grace.cancel()
            self._seat_grace = None
        if changed:
            self._save_seat()
        if holder_changed:
            self._resend_server_info()  # serverInfo.seat（held、none、other）随持有者变化（M15-to-M11 第 3 条）

    def on_seat_event(self, kind: str, data: dict) -> None:
        pid = data.get("principal_id")
        if kind in ("seat.acquired", "seat.takeover") and isinstance(pid, str):
            self.update_seat({"state": "HELD", "holder": pid})
        elif kind in ("seat.released", "seat.expired") and (pid is None or pid == self.seat.get("holder")):
            self.update_seat({"state": "FREE", "holder": None})

    def note_seat_claimed(self, pid: str) -> None:
        """REST 签发 operator/admin token 成功：30 s 内既无 WS 连接也无 REST 请求时进入宽限（SK-E2E-to-M11 第 1 条）。"""
        self.seat_claimed_mono = time.monotonic_ns()
        self.principal_last_rest[pid] = time.monotonic_ns()
        if self.seat.get("holder") == pid and self.seat.get("state") == "GRACE":
            # 宽限期内同一 principal 重新签发（seat_claim 已使 sim-core 回到 HELD）：取消宽限计时
            if self._seat_grace is not None:
                self._seat_grace.cancel()
                self._seat_grace = None
            self.seat = {"state": "HELD", "holder": pid}
            self._save_seat()
        if self.loop is None or any(s.principal.id == pid for s in self.sessions):
            return
        old = self._seat_unconnected.pop(pid, None)
        if old is not None:
            old.cancel()
        self._seat_unconnected[pid] = self.loop.call_later(self.seat_unconnected_s, self._seat_unconnected_check, pid)

    def note_rest_activity(self, pid: str) -> None:
        self.principal_last_rest[pid] = time.monotonic_ns()

    def _seat_unconnected_check(self, pid: str) -> None:
        self._seat_unconnected.pop(pid, None)
        if self.seat.get("holder") != pid or any(s.principal.id == pid for s in self.sessions):
            return
        idle = (time.monotonic_ns() - self.principal_last_rest.get(pid, 0)) / 1e9
        if idle < self.seat_unconnected_s and self.loop is not None:
            self._seat_unconnected[pid] = self.loop.call_later(self.seat_unconnected_s - idle,
                                                               self._seat_unconnected_check, pid)
            return
        self._seat_session_closed(pid)

    def _seat_op(self, op: str, holder: str) -> None:
        """向 sim-core 发 seat_claim、seat_grace、seat_resume 或 seat_expire（席位权威在 sim-core），回复更新缓存。"""
        loop = self.loop
        if loop is None:
            return
        cid = "seat-" + secrets.token_hex(6)
        principal = self.tokens.sign_principal(holder, "operator", cid, conn_id=None, seat=True)
        self.bus.call_cb(bus_keys.CTL_LEASE, {"v": 1, "cid": cid, "op": op, "principal": principal},
                         lambda rep, err: self._post(("seat", rep, err)),
                         timeout=1.0, retries=2)
        self.audit.write(f"seat.{op}", principal_id=holder, cid=cid)

    def _seat_session_closed(self, pid: str) -> None:
        """T03：席位持有者的最后一个 ClientSession 关闭 → GRACE，启动 30 s 墙钟宽限。"""
        if self.stopping or self.seat.get("holder") != pid or self.seat.get("state") not in ("HELD", "GRACE"):
            return
        if any(s.principal.id == pid for s in self.sessions):
            return
        self.seat = {"state": "GRACE", "holder": pid}
        self._save_seat()
        self._seat_op("seat_grace", pid)
        if self._seat_grace is not None:
            self._seat_grace.cancel()
        if self.loop is not None:
            self._seat_grace = self.loop.call_later(self.seat_grace_s, self._seat_grace_expired, pid)
        log.info("seat grace", extra={"kv": {"principal_id": pid, "grace_s": self.seat_grace_s}})

    def _seat_grace_expired(self, pid: str) -> None:
        """T05：宽限到期 → seat_expire（sim-core 释放席位，OPERATOR 租约成为孤儿租约），缓存置 FREE。"""
        self._seat_grace = None
        if self.seat.get("holder") != pid or self.seat.get("state") != "GRACE":
            return
        if any(s.principal.id == pid for s in self.sessions):
            return
        self._seat_op("seat_expire", pid)
        self.update_seat({"state": "FREE", "holder": None})
        log.info("seat expired", extra={"kv": {"principal_id": pid}})

    def _seat_session_opened(self, pid: str) -> None:
        """T04：同一 principal 在宽限期内重连 → HELD。"""
        t = self._seat_unconnected.pop(pid, None)
        if t is not None:
            t.cancel()
        if self.seat.get("holder") != pid or self.seat.get("state") != "GRACE":
            return
        if self._seat_grace is not None:
            self._seat_grace.cancel()
            self._seat_grace = None
        self.seat = {"state": "HELD", "holder": pid}
        self._save_seat()
        self._seat_op("seat_resume", pid)

    def on_seat_takeover(self, new_holder: str) -> None:
        """席位被接管（ext）：对旧持有者的连接发 `error 116` 并以 4403 关闭。"""
        old = [s for s in self.sessions if s.principal.id != new_holder and s.role != "viewer"]
        self.update_seat({"state": "HELD", "holder": new_holder})
        self.audit.write("seat.takeover", principal_id=new_holder)
        for s in old:
            s.send_ctrl(error_msg(116, "seat", None, "席位已被接管"))
            self._spawn_close(s, 4403)

    def _spawn_close(self, s: ClientSession, code: int) -> None:
        async def go() -> None:
            await asyncio.sleep(0.05)
            s.closing = True
            s.wake.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(s.ws.close(code=code), 1.0)

        t = asyncio.ensure_future(go())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def is_seat_holder(self, pid: str) -> bool:
        return self.seat.get("holder") == pid and self.seat.get("state") in ("HELD", "GRACE")

    def seat_json(self) -> dict:
        return dict(self.seat)

    def seat_for(self, pid: str) -> str:
        if self.seat.get("holder") is None:
            return "none"
        return "held" if self.seat.get("holder") == pid else "other"

    def note_ping(self, pid: str, t_mono: int) -> None:
        self.principal_last_ping[pid] = t_mono

    def read_only_reason(self) -> str | None:
        if self.mode == "replay":
            return "replay"
        if self.switching:
            return "switching"
        return None

    # ------------------------------------------------------------ 事件
    def emit_api_event(self, kind: str, level: int, data: dict, *, uav: str | None = None, cid: str | None = None) -> None:
        self.events.emit_api(kind, level, data, uav=uav, cid=cid)

    # ------------------------------------------------------------ 会话
    def add_session(self, s: ClientSession) -> None:
        self.sessions.append(s)
        self._seat_session_opened(s.principal.id)

    def remove_session(self, s: ClientSession) -> None:
        if s in self.sessions:
            self.sessions.remove(s)
        self.rpc.detach_session(s)
        self._closed_bytes += s.stats["bytes"] + s.stats["ctrl_bytes"]
        s.release()
        self.interest.on_subs_changed()
        self._seat_session_closed(s.principal.id)

    def conn_count(self, pid: str) -> tuple[int, int]:
        return len(self.sessions), sum(1 for s in self.sessions if s.principal.id == pid)

    def server_info(self, s: ClientSession) -> dict:
        h = self.source.p
        caps = ["time", "credit", "rpc", "events", "clientPublish", "procHealth"]
        if self.playback_available():
            caps.append("playbackControl")
        return {"op": "serverInfo", "name": "awr-gateway", "protocol": PROTOCOL, "sessionId": self.session_id,
                "connId": s.conn_id, "capabilities": caps, "window": s.window, "tickHz": TICK_HZ,
                "rateClasses": list(T.RATE_CLASSES), "world": dict(self.world_info),
                "run": {"id": self.settings.run_id, "segment": int(h.segment or 0)}, "mode": self.mode,
                "dataStart_ns": self.playback.data_start_ns if self.mode == "replay" else None,
                "dataEnd_ns": self.playback.data_end_ns if self.mode == "replay" else None, "clock": dict(self.clock_caps), "role": s.role,
                "principal": s.principal.id, "seat": self.seat_for(s.principal.id), "contracts": CONTRACTS_VERSION,
                "layouts": {k: L.SCHEMA_HASH[k] for k in INFO_SCHEMAS},
                "limits": {"maxSubs": MAX_SUBS, "maxHighRateFull": MAX_HIGH_RATE_FULL, "ctrlQueue": CTRL_MAX,
                           "maxTextBytes": 262144, "maxBinaryBytes": 4096},
                "serverUnix_ns": str(time.time_ns())}

    def playback_available(self) -> bool:
        return True

    def handshake(self, s: ClientSession) -> None:
        """连接建立：依次 serverInfo、全量 advertise、TIME、全部活动 status（17 §6.2 第 6 步）。"""
        s.send_ctrl(self.server_info(s))
        s.send_ctrl({"op": "advertise", "channels": self.registry.adverts()})
        s.send_ctrl(self.clock.time_bytes())
        for st in self.statuses.values():
            s.send_ctrl(st)

    # ------------------------------------------------------------ 可观测性（1 Hz）
    def _publish_perf(self) -> None:
        sp = self.sim_perf or {}
        h = self.source.p.last_header
        sim: dict[str, Any] = {"n_active": int(sp.get("n_active", len(self.roster))),
                               "kernel": sp.get("kernel") if sp.get("kernel") in ("numba", "numpy") else "numpy",
                               "stage_ms_per_s": {str(k): float(v) for k, v in (sp.get("stage_ms_per_s") or {}).items()
                                                  if isinstance(v, (int, float))}}
        if isinstance(sp.get("cpu_pct"), (int, float)):
            sim["cpu_pct"] = float(sp["cpu_pct"])
        if h is not None:
            sim.update({"rtf": h.rtf_milli / 1000.0, "step_p50_us": h.step_p50_us, "step_p99_us": h.step_p99_us,
                        "step_max_us": h.step_max_us, "catchup_saturated": int(h.catchup_saturated)})
        geo = sp.get("geo") if isinstance(sp.get("geo"), dict) else None
        total = sum(s.stats["bytes"] + s.stats["ctrl_bytes"] for s in self.sessions) + self._closed_bytes
        msg = self.metrics.build(encodes_total=ENCODES[0], bytes_total=total, n_clients=len(self.sessions),
                                 tick_overruns=self.tick_overruns, clients=[s.perf_row() for s in self.sessions],
                                 sim=sim, geo=geo)
        self.perf_ch.publish(msgpack.packb(msg, use_bin_type=True), self.frame_t_sim_ns)
        if self.perf_clients_ch.subscribers:
            rows = [{"conn_id": s.conn_id, **s.client_stats} for s in self.sessions]
            self.perf_clients_ch.publish(msgpack.packb({"clients": rows}, use_bin_type=True), self.frame_t_sim_ns)

    def health(self) -> dict:
        p = self.source.p
        return {"attached": p.ring is not None, "health": p.health, "segment": p.segment, "epoch": p.epoch,
                "global_epoch": self.clock.global_epoch, "pid": os.getpid()}

    def inspect(self) -> dict[str, Any]:
        ages = sorted(self.source.stats["tick_age_ms"][-60:]) or [0.0]
        return {"tick": {"hz": TICK_HZ, "k": self.k, "age_p50_ms": round(ages[len(ages) // 2], 3),
                         "age_p99_ms": round(ages[min(len(ages) - 1, int(len(ages) * 0.99))], 3),
                         "overruns": self.tick_overruns},
                "global_epoch": self.clock.global_epoch, "session_id": self.session_id, "mode": self.mode,
                "producer": self.source.info(), "seat": dict(self.seat),
                "clients": [s.inspect_row() for s in self.sessions],
                "channels": [{"id": c.id, "topic": c.topic, "seq": c.seq, "encodes": c.encodes, "hits": c.hits,
                              "subscribers": c.subscribers, "last_t_sim_ns": c.t_sim_ns}
                             for c in sorted(self.registry.by_id.values(), key=lambda c: c.id)],
                "event_ring": {"oldest_seq": self.events.oldest, "newest_seq": self.events.newest,
                               "count": len(self.events.ring), "bytes": self.events.ring.nbytes},
                "interest": {"detail": self.interest.detail, "marks": self.interest.marks, "topics": self.interest.topics,
                             "seq": self.interest.seq},
                "rpc": dict(self.rpc.stats), "cpub": dict(self.cpub.stats), "stats": dict(self.stats)}

    @property
    def sim_state(self) -> str:
        return TimeState(self.clock.state & 0x0F).name
