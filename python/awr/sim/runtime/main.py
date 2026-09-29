"""sim-core 主循环与组合根（M08-FR-003 至 FR-009、FR-014、FR-040、FR-053；M08 §6.8、§6.16 (1)；AWR-03 §3.3、§3.7 (1)、§8.5）。

启动序列（M08-FR-005；M11-R-to-M08 第 2 条）：`init_child("sim-core")` → `compose_plugins(AWR_PLUGINS)`（组合根导入 M07、
M09、M10、M13 的插件模块，执行注册；未交付的插件记 WARNING 并跳过）→ 加载机型 profile（VH-1..VH-7，失败以
`353 VEHICLE_PROFILE_INVALID` 拒绝启动）与世界几何（M04 `open_world_query`）→ StateRing `open_or_create`：复用原文件时
生产者 `epoch = 头部 epoch + 1`、`segment = 头部 segment + 1`，新建文件时沿用 epoch = 1、segment = 0 → pipeline 构建与注册
校验 → numba 预热（N = 2 哑数组调用全部核；numba 不可用或 `AWR_KERNEL=numpy` 时退回 oracle 并发 `sim.kernel.fallback`，
机群上限 300）→ 布设机群 → `gc.collect(); gc.freeze()`、gen2 阈值 1,000,000 → 发 `sim.started`、`sim.profile.loaded` →
`bus.ready()`（liveliness `proc/sim-core/ready`）。meta.json 的 `sim` 段写入实际内核、FleetConfig、world_seed 与机型版本。

每次迭代的固定顺序（FR-003）：写心跳（StateRing 头部时钟组，禁止后台线程代写）→ 步边界 drain inbox（≤ 512 条：
`ctl/sim-core/{cmd,clock,lease,roster,estimate,query}`、`svc/geo/{height,ray_hit}`、`ctl/sim-core/{gcs,interest}`；zenoh 回调
线程只入队，`ctl/sim-core/setpoint` 回调只写 mailbox）→ 执行到期 tick 的 pipeline（250 Hz 主时钟；StateRing 发布 125 Hz）→
`events.flush()` → 慢任务轮转（`slow.py`：state_ext 2 Hz 分片打包、perf 1 Hz、估价、细校验、GeoProbeServer 分片、查询、
插件登记的慢任务、幂等表清理、gc gen2 每 ≥ 30 s【墙钟】、checkpoint 拷贝（ext）；预算 `min(1000, 2800 − 已用) µs`，下限
100 µs）→ 1 Hz 写步长统计 → `sleep_until(下一 tick 的墙钟时刻)`；暂停时在 inbox 上以 50 ms 超时阻塞（照写心跳）。
"""

from __future__ import annotations

import contextlib
import gc
import importlib
import json
import logging
import os
import resource
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import msgpack
import numpy as np

from awr.contracts import LAYOUT_ID, bus_keys
from awr.contracts.enums import LIFECYCLE_NAMES, TIMESTATE_NAMES, Lifecycle, Owner, TimeState
from awr.contracts.layouts import BUS_SETPOINT32
from awr.contracts.reasons import Reason
from awr.runtime.events import EventPublisher
from awr.runtime.principal import Principal, derive_key, verify_principal
from awr.runtime.statering import StateRing

from ..backends.base import EntitySpec, Kind, load_caps
from ..core import state_model as SM
from ..core.authority import LeaseManager
from ..core.command import CommandEngine
from ..core.estimate import EstimateService
from ..core.roster import Roster
from ..core.supervisor_queue import SupervisorQueue
from ..fleet import kernels_l1 as KL
from ..fleet.fleet import FleetSim
from ..fleet.pipeline import StageCtx
from ..fleet.profiles import ProfileError, ProfileTable
from ..fleet.stages import budgets as B
from ..fleet.stages import registry as R
from .clock import SimClock
from .config import SimConfig
from .slow import SlowTask, SlowTasks

__all__ = ["SimCore", "compose_plugins", "main", "run"]

log = logging.getLogger("awr.sim.runtime")

DRAIN_MAX = 512
IDLE_WAIT_S = 0.05
STATE_EXT_PERIOD_NS = 500_000_000
PERF_PERIOD_NS = 1_000_000_000
AUDIT_FSYNC_NS = 1_000_000_000
GC_GEN2_PERIOD_NS = 30_000_000_000
EXT_SLICE = 16
SPAWN_R_SAFE_M = 1.5


def _state_ext_extra_allowed() -> bool:
    """state_ext 的 M08 附加字段（profile、ctrl、thrust_frac 等，M08-FR-051）只在契约登记后输出（schema 为 additionalProperties false）。"""
    try:
        from awr.contracts._paths import schema_path

        d = json.loads(schema_path("rt/payloads/uav_state_ext.schema.json").read_text(encoding="utf-8"))
        return "thrust_frac" in d.get("properties", {})
    except Exception:
        return False


# ---------------------------------------------------------------- 组合根
def compose_plugins(names: tuple[str, ...] | list[str]) -> tuple[list[str], list[str]]:
    """导入插件模块（导入即登记，AWR-10 §3.3 规则 1）；返回 (已导入, 缺失)。缺失只告警，其他导入错误直接抛出。"""
    loaded, missing = [], []
    for name in names:
        try:
            importlib.import_module(name)
            loaded.append(name)
        except ModuleNotFoundError as e:
            if e.name and (name == e.name or name.startswith(e.name + ".")):
                missing.append(name)
                log.warning("plugin not delivered, skipped", extra={"kv": {"plugin": name}})
            else:
                raise
    return loaded, missing


class Inbox:
    """步边界 inbox：zenoh 回调线程只调用 `put`（deque.append + Event.set，线程安全）；主循环 drain 与带超时等待。"""

    def __init__(self) -> None:
        self._q: deque[tuple] = deque()
        self._ev = threading.Event()

    def put(self, item: tuple) -> None:
        self._q.append(item)
        self._ev.set()

    def get_nowait(self) -> tuple:
        return self._q.popleft()

    def wait(self, timeout: float) -> None:
        if not self._q:
            self._ev.wait(timeout)
        self._ev.clear()

    def __len__(self) -> int:
        return len(self._q)


class AuditLog:
    """`runs/<run>/audit.jsonl`：O_APPEND 单次 write() 追加整行，1 s fsync（17 §3.6；ADR-027）。"""

    def __init__(self, path: Path | None) -> None:
        self.fd: int | None = None
        if path is not None:
            with contextlib.suppress(OSError):
                path.parent.mkdir(parents=True, exist_ok=True)
                self.fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self._dirty = False
        self._last_sync = 0

    def write(self, rec: dict[str, Any]) -> None:
        if self.fd is None:
            return
        line = {"t_wall_ns": str(time.time_ns()), "entry": "sim-core", **rec}
        with contextlib.suppress(OSError):
            os.write(self.fd, (json.dumps(line, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode())
            self._dirty = True

    def maybe_sync(self, now_ns: int) -> None:
        if self.fd is not None and self._dirty and now_ns - self._last_sync >= AUDIT_FSYNC_NS:
            with contextlib.suppress(OSError):
                os.fsync(self.fd)
            self._dirty = False
            self._last_sync = now_ns

    def close(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.fsync(self.fd)
                os.close(self.fd)
            self.fd = None


# ---------------------------------------------------------------- sim-core
class SimCore:
    """sim-core 进程的全部状态；`iterate()` 为一次主循环迭代（`--inproc` 与测试可以单步驱动）。"""

    def __init__(self, cfg: SimConfig, bus: Any, ring: StateRing, *, reused: bool = False, secret: bytes | None = None,
                 wall_ns: Callable[[], int] = time.monotonic_ns, audit_path: Path | None = None,
                 reg: R.Registry | None = None, world: Any = None, persist_dir: Path | None = None,
                 profiles: ProfileTable | None = None, perf_ns: Callable[[], int] = time.perf_counter_ns) -> None:
        self.cfg = cfg
        self.bus = bus
        self.ring = ring
        self.reused = reused
        self.reg = reg if reg is not None else R.registry()
        self.wall_ns = wall_ns
        self.perf_ns = perf_ns
        self.secret = secret
        self.k_entry = derive_key(secret, cfg.run_id, "entry") if secret else None
        self.world = world
        self.world_error: str | None = None
        self.persist_dir = persist_dir
        self.audit = AuditLog(audit_path)
        self.inbox = Inbox()
        self.handles: list[Any] = []
        self.plugins_loaded: list[str] = []
        self.plugins_missing: list[str] = []
        self.clock = SimClock(wall_ns=wall_ns, max_batch_base=cfg.fleet.max_batch_base)
        h = ring.header()
        if reused:
            self.epoch, self.segment = h.epoch + 1, h.segment + 1
            ring.set_epoch(self.epoch)
            ring.set_segment(self.segment)
            self.start_reason = "crash_restart"
        else:
            self.epoch, self.segment = h.epoch, h.segment
            self.start_reason = "cold_start"
        self.events = EventPublisher(bus, "sim-core", self.epoch)
        self.T = profiles or ProfileTable()  # ProfileError（353）向上抛出：sim-core 拒绝启动
        self.caps = load_caps("mock")
        self.replay_caps = load_caps("replay")
        self.roster = Roster(cfg.fleet.capacity, id_base=h.id_base, id_count=h.id_count or 1024, producer="sim-core",
                             boot_s=cfg.fleet.boot_s, ready_s=cfg.fleet.ready_s)
        self.lease = LeaseManager(cfg.fleet.capacity)
        self.supq = SupervisorQueue()
        self.fleet: FleetSim | None = None
        self.engine: CommandEngine | None = None
        self.estimator: EstimateService | None = None
        self.ctx = StageCtx()
        self.geo = None
        self.interest = np.zeros(0, np.uint16)
        self.interest_seq = -1
        self.gcs_last: dict | None = None
        self.agent_slot: dict[int, int] = {}
        self.stats = {"iters": 0, "ticks": 0, "drained": 0}
        self._step_us: deque[float] = deque(maxlen=1000)
        self._last_stats_ns = 0
        self._rtf_t0 = (0, 0)
        self._rtf_milli = 1000
        self._pub_ext = None
        self._pub_perf = None
        self._proc_cpu = (time.process_time(), 0)
        self._perf_t_sim0 = 0
        self._est_q: deque = deque()
        self._query_q: deque = deque()
        self._ext_items: list[list] = []
        self._ext_order: list[int] = []
        self._ext_pos = -1
        self._ext_last = 0
        self._last_gc = 0
        self._gc_ms: deque[float] = deque(maxlen=64)
        self._overbudget: set[str] = set()
        self._rtf_limited_reported = False
        self._removing: dict[int, str] = {}
        self._stopped: list[int] = []
        self._ext_extra = _state_ext_extra_allowed()
        self._reset_hooks: list[Callable[[dict], None]] = []
        self.slow = SlowTasks(perf_ns)
        self.checkpointer: Any = None
        self._sp_raw: deque[bytes] = deque(maxlen=8192)
        self.inputlog: Any = None
        self.started = False

    # ------------------------------------------------------------ 启动
    def start(self) -> None:
        cfg = self.cfg
        self.plugins_loaded, self.plugins_missing = compose_plugins(cfg.plugins)
        if self.world is None and cfg.load_world:
            self._load_world()
        self.fleet = FleetSim(cfg.fleet, None, self.world, self.events, self.T, cfg.world_seed, reg=self.reg)
        self.T = self.fleet.T
        hooks = self.reg.hooks
        self.lease.hooks = hooks
        self.engine = CommandEngine(self.fleet.S, self.T, self.roster, self.lease, events=self.events, clock=self.clock,
                                    caps=self.caps, entry_key=self.k_entry, audit=self.audit.write, world=self.world,
                                    reg=self.reg, supervisor=self.supq, epoch_fn=lambda: self.epoch,
                                    segment_fn=lambda: self.segment, energy=self.reg.energy, hooks=hooks,
                                    PB=self.fleet.PB, fleet_ops=self, stop_motion=cfg.fleet.stop_motion,
                                    replay_caps=self.replay_caps, inputlog=self.inputlog)
        self.fleet.build_pipeline(engine=self.engine, supervisor=self.supq, ring=self.ring,
                                  lease_owner=self.lease.owner_codes, roster_version=lambda: self.roster.roster_version,
                                  lifecycle=self._lifecycle, wall_ns=self.wall_ns, timer=self.perf_ns)
        self.engine.fallback_fsm = self.fleet.uses_fallback_fsm
        self.engine.ctx = self.ctx
        self.estimator = EstimateService(self.fleet.S, self.T, self.roster, world=self.world, energy=lambda: self.reg.energy)
        c = self.ctx
        c.events, c.calls, c.clock, c.world, c.profiles = self.events, self.engine, self.clock, self.world, self.T
        c.supervisor, c.cfg, c.lease, c.paths = self.supq, cfg.fleet, self.lease, self.fleet.PB
        c.rng = self._rng_streams(cfg.world_seed)
        if self.fleet.kernel == "numba":
            t0 = self.perf_ns()
            KL.warmup()
            self.warmup_s = (self.perf_ns() - t0) / 1e9
        else:
            self.warmup_s = 0.0
            self.events.emit("sim.kernel.fallback", t_sim_ns=0, severity=2, reason=self.fleet.kernel_fallback or "numpy",
                             max_vehicles=self.fleet.max_vehicles)
        self._setup_slow()
        self._register_metrics()
        self._serve()
        self.spawn_default()
        if cfg.autoplay:
            self.clock.apply("play")
        self.ring.heartbeat(self.clock.t_ns, self.clock.state, self.clock.rate_milli, step_seq=self.clock.tick)
        for pid in self.T.ids:
            p = self.T.get(pid)
            self.events.emit("sim.profile.loaded", t_sim_ns=self.clock.t_ns, severity=0, profile_id=pid,
                             profile_version=p.version, status=p.status)
        self.events.emit("sim.started", t_sim_ns=self.clock.t_ns, severity=1, epoch=self.epoch, segment=self.segment,
                         reason=self.start_reason, kernel=self.fleet.kernel, n=len(self.roster.by_slot),
                         plugins_missing=self.plugins_missing)
        if self.world is not None:
            with contextlib.suppress(Exception):
                self.events.emit("geo.ready", t_sim_ns=self.clock.t_ns, severity=0, **self.world.ready_event())
        self.events.flush()
        self._write_meta()
        gc.collect()
        gc.freeze()
        gc.set_threshold(700, 10, 1_000_000)
        self._last_gc = self.wall_ns()
        self.handles.append(self.bus.ready())
        self.started = True
        log.info("sim-core ready", extra={"kv": {"epoch": self.epoch, "segment": self.segment, "reused": self.reused,
                                                 "world": cfg.world_id, "world_loaded": self.world is not None,
                                                 "kernel": self.fleet.kernel, "plugins_missing": self.plugins_missing}})

    def _register_metrics(self) -> None:
        """M08 所有的剧本度量（16 §12.3：`elapsed_s`、`landed_all`、`landed_home_err_m`；INT-1 补登，M10-to-M08 第 1 条、
        M16-to-M08-M09 第 6 条）。进程级登记表：新的 SimCore 覆盖旧实例登记的同名 M08 度量，他人登记的同名度量不覆盖。"""
        from awr.contracts.enums import FlightState

        from ..core import metrics as MET

        def elapsed_s(**_kw: Any) -> float:
            return self.clock.t_ns * 1e-9

        def _slots(vehicle_ids: list[str] | None) -> np.ndarray:
            act = self.fleet.S.active_idx()
            if not vehicle_ids:
                return act
            want = set(vehicle_ids)
            return np.asarray([e.slot for e in self.roster.by_slot.values() if e.id in want], np.int64)

        def landed_all(vehicle_ids: list[str] | None = None, **_kw: Any) -> float:
            s = _slots(vehicle_ids)
            if s.size == 0:
                return 1.0
            S = self.fleet.S
            fs = S.blocks["safety"]["fs"][s]
            return float(bool(((fs == int(FlightState.DISARMED)) & ~S.in_air[s].astype(bool)).all()))

        def landed_home_err_m(vehicle_ids: list[str] | None = None, **_kw: Any) -> float:
            s = _slots(vehicle_ids)
            if s.size == 0:
                return 0.0
            S = self.fleet.S
            d = S.enu.pos[s, :2] - S.enu.home[s, :2]
            return float(np.max(np.hypot(d[:, 0], d[:, 1])))

        have = {m.name: m for m in MET.list_metrics()}
        for name, fn in (("elapsed_s", elapsed_s), ("landed_all", landed_all), ("landed_home_err_m", landed_home_err_m)):
            spec = have.get(name)
            if spec is not None and spec.owner != "M08":
                continue
            MET.unregister_metric(name)
            MET.register_metric(name, fn, owner="M08")

    def _rng_streams(self, seed: int) -> dict[str, np.random.Generator]:
        """RNG 流 `PCG64(SeedSequence([world_seed, stream_id]))`（rng_streams.json，ADR-049）；键为流名小写。"""
        from awr.contracts.rng_streams import Stream, rng

        out = {st.name.lower(): rng(seed, int(st)) for st in Stream}
        out["wind"] = out["dryden"]
        return out

    def _load_world(self) -> None:
        try:
            from awr.world.geometry import GeoProbeServer, open_world_query

            self.world = open_world_query(self.cfg.worlds_dir / self.cfg.world_id)
            self.geo = GeoProbeServer(self.world)
        except Exception as e:  # 世界缺失或几何缓存异常：退化为水平地面，GeoProbeServer 不可用（探针回复 123）
            self.world = None
            self.world_error = f"{type(e).__name__}: {e}"
            log.warning("world geometry unavailable", extra={"kv": {"world": self.cfg.world_id, "error": self.world_error}})

    def _serve(self) -> None:
        b, q = self.bus, self.inbox
        self.handles += [
            b.serve(bus_keys.ctl_cmd("sim-core"), lambda r: q.put(("cmd", r))),
            b.serve(bus_keys.CTL_CLOCK, lambda r: q.put(("clock", r))),
            b.serve(bus_keys.CTL_LEASE, lambda r: q.put(("lease", r))),
            b.serve(bus_keys.ctl_roster("sim-core"), lambda r: q.put(("roster", r))),
            b.serve(bus_keys.CTL_ESTIMATE, lambda r: q.put(("estimate", r))),
            b.serve(bus_keys.CTL_QUERY, lambda r: q.put(("query", r))),
            b.serve(bus_keys.svc_geo("height"), lambda r: q.put(("geo", "height", r))),
            b.serve(bus_keys.svc_geo("ray_hit"), lambda r: q.put(("geo", "ray_hit", r))),
            b.subscribe(bus_keys.CTL_GCS, lambda k, raw: q.put(("gcs", raw))),
            b.subscribe(bus_keys.CTL_INTEREST, lambda k, raw: q.put(("interest", raw))),
            b.subscribe(bus_keys.CTL_SETPOINT, lambda k, raw: self._sp_raw.append(raw)),  # PY-CB-01：回调只入队
        ]
        with contextlib.suppress(Exception):
            self.handles.append(b.watch(bus_keys.proc_alive("agent-runtime"),
                                        lambda k, alive: q.put(("agent_alive", bool(alive)))))

    def on_setpoint(self, raw: bytes) -> None:
        """`ctl/sim-core/setpoint`（32 B raw）：订阅回调只把原始字节入队（PY-CB-01），本函数在步顶 drain 时写 mailbox，
        ingest 在同一步边界读取（M08-FR-029）。"""
        if len(raw) < BUS_SETPOINT32.itemsize or self.fleet is None:
            return
        r = np.frombuffer(raw[:BUS_SETPOINT32.itemsize], BUS_SETPOINT32)[0]
        slot = self.agent_slot.get(int(r["agent_no"]))
        if slot is None:
            return
        self.fleet.mailbox.put(slot, np.asarray(r["vel"], np.float64), float(r["yaw_rate"]), int(r["frame"]), int(r["seq"]),
                               self.clock.wall_mono_ns(), self.clock.paused_total_ns())

    # ------------------------------------------------------------ 机群
    def spawn_xy(self, k: int) -> tuple[float, float, float]:
        base = self.cfg.spawn_xy or self._flat_spot()
        x, y = base[0] + k * self.cfg.spawn_spacing_m, base[1]
        z = 0.0
        if self.world is not None:
            z = float(self.world.height_dsm(np.array([[x, y]]))[0])
        return (x, y, z)

    def _flat_spot(self) -> tuple[float, float]:
        """世界原点附近的平坦开阔格：以 4 m 步长向外搜索 DSM 与 DTM 高差 < 0.5 m、3×3 邻域一致的点。"""
        w = self.world
        if w is None:
            return (0.0, 0.0)
        pts = []
        for r in range(0, 400, 4):
            angs = np.linspace(0, 2 * np.pi, max(1, r // 2), endpoint=False) if r else [0.0]
            pts.extend((r * np.cos(ang), r * np.sin(ang)) for ang in angs)
        P_ = np.asarray(pts, np.float64)
        off = np.array([[dx, dy] for dx in (-4, 0, 4) for dy in (-4, 0, 4)], np.float64)
        Q = (P_[:, None, :] + off[None, :, :]).reshape(-1, 2)
        diff = (w.height_dsm(Q) - w.ground_dtm(Q)).reshape(len(P_), len(off))
        ok = np.flatnonzero(np.all(np.abs(diff) < 0.5, axis=1))
        return tuple(P_[ok[0]]) if ok.size else (0.0, 0.0)

    def spawn_default(self) -> None:
        for k in range(self.cfg.n_vehicles):
            x, y, z = self.spawn_xy(k)
            self.add_vehicle(self.cfg.profile_id, (x, y, z), 0.0, limits_profile=self.cfg.limits_profile)

    def add_vehicle(self, profile_id: str, home_enu_m: tuple[float, float, float], yaw_rad: float, *,
                    vehicle_id: str | None = None, limits_profile: str | None = None, initial_soc: float = 1.0,
                    backend: str = "mock", track: Any = None) -> str:
        prof = self.T.get(profile_id)
        e = self.roster.add(vehicle_id=vehicle_id, model=prof.model, profile_id=profile_id, limits_profile=limits_profile,
                            home_enu_m=home_enu_m, yaw_rad=yaw_rad, initial_soc=initial_soc, t_ns=self.clock.t_ns,
                            emit=self.events.emit, backend=backend)
        spec = EntitySpec(e.id, Kind.UAV, profile_id, limits_profile, home_enu_m, yaw_rad, initial_soc)
        fid = R.Fidelity.L0 if backend == "replay" else R.Fidelity.L1
        self.fleet.add(spec, slot=e.slot, agent_no=e.agent_no, entity_id=e.id, t_s=self.clock.t_ns * 1e-9, fidelity=fid)
        if track is not None:
            self.fleet.kinematic.attach(e.slot, track, self.clock.t_ns * 1e-9)
        self.fleet.S.lifecycle[e.slot] = e.lifecycle
        self.agent_slot = {x.agent_no: x.slot for x in self.roster.by_slot.values()}
        if self.inputlog is not None:
            self.inputlog.append("roster", self.clock.tick + 1, {"op": "add", "id": e.id, "profile_id": profile_id,
                                                                  "home_enu_m": list(home_enu_m), "backend": backend})
        hooks = self.reg.hooks
        if hooks is not None and backend != "replay":
            hooks.on_spawn(np.array([e.slot], np.int32), np.array([profile_id]), np.array([home_enu_m], np.float64),
                           np.array([initial_soc], np.float64))
        return e.id

    # ---- fleet/add 与 fleet/remove（CommandEngine 委托，AWR-12 §5.14）
    def admit_add(self, args: dict) -> tuple[int, Any]:
        S = self.fleet.S
        pid = str(args.get("profile_id") or "p600_mid360")
        if pid not in self.T.profiles:
            return int(Reason.PARAM_OUT_OF_RANGE), {"why": "UNKNOWN_PROFILE", "profile_id": pid}
        lp = args.get("speed_profile")
        if lp is not None and lp not in self.T.get(pid).limits:
            return int(Reason.PARAM_OUT_OF_RANGE), {"why": "UNKNOWN_PROFILE", "speed_profile": lp}
        n = len(self.roster.by_slot)
        if n >= self.fleet.max_vehicles:
            why = "KERNEL_LIMIT" if self.fleet.kernel == "numpy" and n < self.cfg.fleet.capacity else "CAPACITY"
            return int(Reason.PARAM_OUT_OF_RANGE), {"why": why, "max": self.fleet.max_vehicles}
        if self.roster.free_slot() is None:
            return int(Reason.PARAM_OUT_OF_RANGE), {"why": "CAPACITY", "max": self.cfg.fleet.capacity}
        vid = args.get("vehicle_id")
        if vid is not None and (not isinstance(vid, str) or not vid or self.roster.resolve(vid) is not None):
            return int(Reason.STATE), {"why": "ID_EXISTS", "vehicle_id": vid}
        h = args.get("home_enu_m")
        if not (isinstance(h, (list, tuple)) and len(h) == 3 and all(isinstance(x, (int, float)) for x in h[:2])
                and (h[2] is None or isinstance(h[2], (int, float)))):
            return int(Reason.BAD_REQUEST), {"field": "home_enu_m"}
        x, y, z = float(h[0]), float(h[1]), h[2]
        if self.world is not None:
            zones = self.world.zones
            xyz = np.array([[x, y, float(z) if z is not None else 0.0]])
            if not bool(zones.border.contains_xy(xyz[:, :2])[0]):
                return int(Reason.GEOFENCE_REJECT), {"why": "OUT_OF_BORDER"}
            if bool(zones.contains(xyz, {"nofly"})[0]) or any(p.contains_xy(xyz[:, :2])[0] for p in zones.nofly):
                return int(Reason.GEOFENCE_REJECT), {"why": "IN_NOFLY"}
            dsm = float(self.world.height_dsm(np.array([[x, y]]))[0])
            if z is None:
                z = dsm
            elif not dsm - 0.05 <= float(z) <= dsm + 0.5:
                return int(Reason.GEOFENCE_REJECT), {"why": "SPAWN_Z", "dsm_m": round(dsm, 3)}
        z = 0.0 if z is None else float(z)
        act = S.active_idx()
        if act.size:
            d = np.hypot(S.enu.home[act, 0] - x, S.enu.home[act, 1] - y)
            d2 = np.hypot(S.enu.pos[act, 0] - x, S.enu.pos[act, 1] - y)
            dmin = float(min(d.min(), d2.min()))
            lim = 2.0 * np.sqrt(2.0) * SPAWN_R_SAFE_M
            if dmin < lim:
                return int(Reason.PARAM_OUT_OF_RANGE), {"why": "SPAWN_TOO_CLOSE", "min_m": round(lim, 3),
                                                        "dist_m": round(dmin, 3)}
        vid = self.add_vehicle(pid, (x, y, z), float(args.get("yaw_rad") or 0.0), vehicle_id=vid, limits_profile=lp,
                               initial_soc=float(args.get("initial_soc", 1.0)))
        e = self.roster.resolve(vid)
        return 0, {"id": vid, "agent_no": e.agent_no, "lifecycle": "STARTING"}

    def remove(self, uav: str, force: bool) -> tuple[int, Any]:
        """fleet/remove（L05–L09）：地面或强制 → STOPPED（下一 tick REMOVED）；空中 → DRAINING（以 safety 名义降落）。"""
        e = self.roster.resolve(uav)
        if e is None:
            return int(Reason.NO_VEHICLE), {"uav": uav}
        S = self.fleet.S
        s = e.slot
        if e.lifecycle in (int(Lifecycle.STOPPED), int(Lifecycle.REMOVED)):
            return 0, {"id": uav, "lifecycle": LIFECYCLE_NAMES[e.lifecycle]}
        airborne = bool(S.in_air[s]) and (S.fidelity[s] & 8) == 0
        if force or not airborne:
            self.engine.cancel_slot(s, int(Reason.CANCELLED), "vehicle removed")
            self.lease.free(s)
            self.roster.set_lifecycle(s, Lifecycle.STOPPED, self.clock.t_ns, self.events.emit, "force" if force else None)
            self._stopped.append(s)
            return 0, {"id": uav, "lifecycle": "STOPPED"}
        self.roster.set_lifecycle(s, Lifecycle.DRAINING, self.clock.t_ns, self.events.emit)
        self._removing[s] = uav
        self.engine.submit_internal({"cid": f"drain-{uav}-{self.clock.tick}", "op": "land", "uav": uav, "args": {}},
                                    {"principal_id": "safety", "role": "safety", "entry": "scenario", "seat": False})
        return 0, {"id": uav, "lifecycle": "DRAINING"}

    def _lifecycle(self, S, ctx) -> None:
        emit = self.events.emit
        self.roster.advance(ctx.t_ns, emit)
        # L07：DRAINING 触地并上锁 → STOPPED
        for s in list(self._removing):
            e = self.roster.by_slot.get(s)
            if e is None:
                self._removing.pop(s, None)
                continue
            fs = int(S.blocks["safety"]["fs"][s])
            if fs in (int(SM.FS.DISARMED), int(SM.FS.CRASHED)) and S.landed[s]:
                self._removing.pop(s, None)
                self.lease.free(s)
                self.roster.set_lifecycle(s, Lifecycle.STOPPED, ctx.t_ns, emit)
                self._stopped.append(s)
        # L09：STOPPED → 下一 tick REMOVED（清 active、roster_version + 1）
        if self._stopped:
            done, self._stopped = self._stopped, []
            hooks = self.reg.hooks
            for s in done:
                e = self.roster.by_slot.get(s)
                if e is not None and e.lifecycle == int(Lifecycle.STOPPED):
                    self.engine.cancel_slot(s, int(Reason.CANCELLED), "vehicle removed")
                    self.fleet.remove(s)
                    self.roster.remove(s, ctx.t_ns, emit)
                    if hooks is not None:
                        hooks.on_remove(np.array([s], np.int32))
            self.agent_slot = {x.agent_no: x.slot for x in self.roster.by_slot.values()}
            self._apply_backend_caps()
        for e in self.roster.by_slot.values():
            S.lifecycle[e.slot] = e.lifecycle

    def _apply_backend_caps(self, extra: list | None = None) -> None:
        caps = [self.caps.clock] + ([self.replay_caps.clock] if any(e.backend == "replay"
                                                                     for e in self.roster.by_slot.values()) else [])
        if self.clock.apply_caps(caps + list(extra or [])):
            self.events.emit("sim.clock", t_sim_ns=self.clock.t_ns, severity=0, state=TIMESTATE_NAMES[self.clock.state],
                             rate=self.clock.rate)

    def register_reset_hook(self, fn: Callable[[dict], None]) -> None:
        self._reset_hooks.append(fn)

    def reset(self, reason: str = "scenario_reset", args: dict | None = None) -> None:
        """剧本重置（C09；M08-FR-008）：segment + 1、epoch + 1，机群重生（RESTARTING → READY），租约 FREE，在途调用 canceled 6。"""
        self.engine.cancel_all(int(Reason.CANCELLED), "reset")
        self.events.flush()
        S = self.fleet.S
        for s in list(self._stopped) + list(self._removing):
            e = self.roster.by_slot.get(s)
            if e is not None:
                self.fleet.remove(s)
                self.roster.remove(s, self.clock.t_ns, self.events.emit, "reset")
        self._stopped.clear()
        self._removing.clear()
        self.lease.reset()
        self.clock.apply("reset")
        self.ctx.tick, self.ctx.t_ns = 0, 0
        self.epoch += 1
        self.segment += 1
        self.ring.set_epoch(self.epoch)
        self.ring.set_segment(self.segment)
        self.events.set_epoch(self.epoch)
        if not self.roster.by_slot:
            self.spawn_default()
        else:
            for s in self.roster.slots_in_order():
                e = self.roster.by_slot[s]
                track = self.fleet.kinematic.tracks.get(s)
                self.fleet.remove(s)
                spec = EntitySpec(e.id, Kind.UAV, e.profile_id, e.limits_profile, e.home_enu_m, e.yaw_rad, e.initial_soc)
                fid = R.Fidelity.L0 if e.backend == "replay" else R.Fidelity.L1
                self.fleet.add(spec, slot=s, agent_no=e.agent_no, entity_id=e.id, t_s=0.0, fidelity=fid)
                if track is not None:
                    self.fleet.kinematic.attach(s, track, 0.0)
                self.roster.set_lifecycle(s, Lifecycle.RESTARTING, 0, self.events.emit, "reset")
                S.lifecycle[s] = int(Lifecycle.RESTARTING)
        for fn in self._reset_hooks:
            with contextlib.suppress(Exception):
                fn(dict(args or {}))
        if self.cfg.autoplay:
            self.clock.apply("play")
        self.events.emit("sim.reset", t_sim_ns=0, severity=1, epoch=self.epoch, segment=self.segment, reason=reason,
                         kernel=self.fleet.kernel, n=len(self.roster.by_slot))
        if self.inputlog is not None:
            self.inputlog.append("clock", 0, {"op": "reset", "args": dict(args or {})})

    # ------------------------------------------------------------ 主循环
    def iterate(self) -> float:
        """一次主循环迭代；返回建议的休眠秒数。"""
        clk = self.clock
        t_iter0 = self.perf_ns()
        self.ring.heartbeat(clk.t_ns, clk.state, clk.rate_milli, step_seq=clk.tick)  # FR-003：先写心跳（时钟组）
        self._drain()
        n = clk.steps_due()
        if n:
            ts = self.perf_ns()
            self.ctx.tick = clk.tick
            self.fleet.step(self.ctx, n)
            clk.tick = self.ctx.tick
            clk.advanced(n)
            self._step_us.append((self.perf_ns() - ts) / 1000.0 / n)
            self.stats["ticks"] += n
            if clk.step_done:
                clk.step_done = False
                tap = self.fleet.tap
                if tap is not None and self.ctx.tick % max(1, tap.every) != 0:  # C04：单步结束于非 tap tick 时补发一次
                    self.ctx.batch_remaining = 0
                    tap(self.fleet.S, self.ctx)
                self.events.emit("sim.clock", t_sim_ns=clk.t_ns, severity=0, state=TIMESTATE_NAMES[clk.state],
                                 rate=clk.rate)
        if clk.rtf_limited and not self._rtf_limited_reported:
            self._rtf_limited_reported = True
            self.events.emit("sim.rtf_limited", t_sim_ns=clk.t_ns, severity=1, requested_rate=clk.rate,
                             actual_rtf=round(self._rtf_milli / 1000.0, 3))
        elif not clk.rtf_limited and self._rtf_limited_reported and n:
            self._rtf_limited_reported = False
        self.events.flush()
        now = self.wall_ns()
        used_us = (self.perf_ns() - t_iter0) / 1000.0
        budget = max(100.0, min(float(self.cfg.fleet.slow_budget_us), self.cfg.fleet.tick_budget_us - used_us))
        self.slow.run(budget, now_wall=now, now_sim=clk.t_ns)
        if now - self._last_stats_ns >= PERF_PERIOD_NS:
            self._write_step_stats(now)
        self.stats["iters"] += 1
        if clk.state == TimeState.STEPPING:
            return 0.0
        if not clk.advancing:
            return IDLE_WAIT_S
        return max(0.0, (clk.next_deadline_ns() - self.wall_ns() - 150_000) / 1e9)

    def run(self, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            wait = self.iterate()
            if wait <= 0:
                continue
            if not self.clock.advancing:
                self.inbox.wait(wait)  # 暂停：inbox 上带超时的阻塞等待（照写心跳，20 Hz 空转；有请求立即进入下一轮）
            else:
                time.sleep(min(wait, IDLE_WAIT_S))

    def _drain(self) -> None:
        sp = self._sp_raw
        while sp:  # 流式 setpoint（32 B raw）：步顶写 mailbox（每机只保留最新值）
            self.on_setpoint(sp.popleft())
        for _ in range(DRAIN_MAX):
            try:
                item = self.inbox.get_nowait()
            except IndexError:
                return
            self.stats["drained"] += 1
            try:
                self._dispatch(item)
            except Exception:
                log.exception("inbox dispatch failed", extra={"kv": {"kind": item[0]}})
                req = item[-1]
                if hasattr(req, "close"):
                    req.close()

    def _dispatch(self, item: tuple) -> None:
        kind = item[0]
        if kind == "cmd":
            req = item[1]
            t0 = self.perf_ns()
            rep = self.engine.handle(req)
            self.engine.admission_us.append((self.perf_ns() - t0) / 1000.0)
            if len(self.engine.admission_us) > 4096:
                del self.engine.admission_us[:2048]
            req.reply_msg(rep)
        elif kind == "clock":
            req = item[1]
            req.reply_msg(self._clock_op(req.msg()))
        elif kind == "lease":
            req = item[1]
            req.reply_msg(self._lease_op(req.msg()))
        elif kind == "roster":
            item[1].reply_msg(self.roster.snapshot())
        elif kind == "estimate":
            self._est_q.append((item[1], self.clock.wall_mono_ns()))
        elif kind == "query":
            self._query_q.append(item[1])
        elif kind == "geo":
            if self.geo is not None:
                self.geo.enqueue(item[1], item[2])
            else:
                msg = item[2].msg() if hasattr(item[2], "msg") else {}
                item[2].reply_msg({"v": 1, "id": str((msg or {}).get("id", "")), "ok": False,
                                   "code": int(Reason.WORLD_NOT_READY), "result": None, "content_version": None,
                                   "derive_sha8": None, "source": None, "t_proc_us": 0, "detail": "GEO_NOT_READY"})
        elif kind == "gcs":
            with contextlib.suppress(Exception):
                self.gcs_last = msgpack.unpackb(item[1], raw=False)
                hooks = self.reg.hooks
                if hooks is not None:
                    g = self.gcs_last
                    hooks.on_gcs_beacon(g.get("principal_id"), g.get("seat_state", "FREE"), int(g.get("ping_age_ms", 0)),
                                        self.clock.wall_mono_ns(), self.clock.paused_total_ns())
        elif kind == "agent_alive":
            hooks = self.reg.hooks
            if hooks is not None:
                with contextlib.suppress(Exception):
                    hooks.on_agent_liveliness(bool(item[1]))
        elif kind == "interest":
            with contextlib.suppress(Exception):
                m = msgpack.unpackb(item[1], raw=False)
                if int(m.get("seq", -1)) >= self.interest_seq or int(m.get("seq", -1)) == 0:
                    self.interest_seq = int(m.get("seq", -1))
                    ids = sorted(set(m.get("detail", [])) | set(m.get("marks", [])))[:80]
                    self.interest = np.asarray(ids, np.uint16)
                    self.ctx.interest = self.interest

    # ------------------------------------------------------------ 时钟与租约服务
    def _principal_ok(self, msg: dict, *, need_seat: bool) -> int:
        p = msg.get("principal")
        if not isinstance(p, dict):
            return int(Reason.ROLE_FORBIDDEN)
        if p.get("_internal"):
            return 0
        if self.k_entry is not None:
            sig = p.get("sig")
            try:
                pr = Principal(str(p["principal_id"]), p["role"], p["entry"], p.get("conn_id"), bool(p.get("seat", False)))
            except (KeyError, TypeError):
                return int(Reason.ROLE_FORBIDDEN)
            if not isinstance(sig, (bytes, bytearray)) or not verify_principal(pr, str(msg.get("cid", "")), bytes(sig),
                                                                             self.k_entry):
                return int(Reason.ROLE_FORBIDDEN)
        if p.get("role") not in ("operator", "admin"):
            return int(Reason.ROLE_FORBIDDEN)
        if need_seat and not self.lease.is_seat_holder(str(p.get("principal_id"))):
            return int(Reason.SEAT_TAKEN)
        return 0

    def _clock_json(self) -> dict:
        c = self.clock
        return {"state": TIMESTATE_NAMES[c.state], "rate": c.rate, "t_sim_ns": c.t_ns, "epoch": self.epoch,
                "segment": self.segment}

    def _clock_op(self, msg: dict) -> dict:
        cid = str(msg.get("cid", ""))
        code = self._principal_ok(msg, need_seat=True)
        op = str(msg.get("op", ""))
        args = msg.get("args") or {}
        if not code:
            if op == "reset":
                self.reset(args=args)
            elif op == "checkpoint":
                code = 0 if self.checkpoint_now() else int(Reason.SERVICE_UNAVAILABLE)
            else:
                code = self.clock.apply(op, args).code
                if not code and op == "speed":
                    self.engine.on_rate_change(self.clock.rate)
            if not code:
                self.events.emit("sim.clock", t_sim_ns=self.clock.t_ns, severity=0, state=TIMESTATE_NAMES[self.clock.state],
                                 rate=self.clock.rate)
                if self.inputlog is not None and op != "reset":
                    self.inputlog.append("clock", self.clock.tick + 1, {"op": op, "args": args})
        return {"v": 1, "cid": cid, "status": "rejected" if code else "accepted", "code": int(code),
                "clock": self._clock_json()}

    def _lease_op(self, msg: dict) -> dict:
        cid = str(msg.get("cid", ""))
        op = str(msg.get("op", ""))
        code = self._principal_ok(msg, need_seat=op in ("acquire", "release"))
        lease = None
        p = msg.get("principal") or {}
        pid = str(p.get("principal_id", ""))
        if not code:
            if op.startswith("seat_"):
                code, _seat = self.lease.seat_op(op, pid, str(p.get("role")), t_wall_ns=time.time_ns())
                kind = {"seat_claim": "seat.acquired", "seat_release": "seat.released", "seat_expire": "seat.expired"}.get(op)
                if not code and kind:
                    self.events.emit(kind, t_sim_ns=self.clock.t_ns, severity=1, principal_id=pid)
                    self.lease._notify(op, np.zeros(0, np.int32), None, pid)
                if not code:
                    self.audit.write({"kind": f"seat.{op}", "t_sim_ns": self.clock.t_ns, "principal_id": pid,
                                      "role": p.get("role"), "cid": cid, "code": 0})
            elif op in ("acquire", "release"):
                e = self.roster.resolve(str(msg.get("uav") or ""))
                if e is None:
                    code = int(Reason.NO_VEHICLE)
                elif op == "acquire":
                    code = self.lease.acquire(e.slot, int(Owner.OPERATOR), pid, uav=e.id, t_ns=self.clock.t_ns,
                                              emit=self.events.emit)
                else:
                    code = self.lease.release(e.slot, pid, return_to=str(msg.get("return_to", "previous")), uav=e.id,
                                              t_ns=self.clock.t_ns, emit=self.events.emit)
                if e is not None:
                    L = self.lease.lease_json(e.slot)
                    lease = {"uav": e.id, "owner": L["owner"], "holder": L["holder"], "priority": L["priority"],
                             "ttl_ms": None, "token": None}
            else:
                code = int(Reason.BAD_REQUEST)
        if self.inputlog is not None and not code:
            self.inputlog.append("lease", self.clock.tick + 1, msg)
        return {"v": 1, "cid": cid, "status": "rejected" if code else "accepted", "code": int(code), "lease": lease,
                "seat": self.lease.seat.to_json()}

    # ------------------------------------------------------------ 慢任务（FR-004）
    def _setup_slow(self) -> None:
        sl = self.slow
        sl.add(SlowTask("audit", lambda b: self.audit.maybe_sync(self.wall_ns())))
        sl.add(SlowTask("geo", self._slow_geo))
        sl.add(SlowTask("state_ext", self._slow_state_ext))
        sl.add(SlowTask("perf", lambda b: self._publish_perf(), period_wall_ns=PERF_PERIOD_NS))
        sl.add(SlowTask("estimate", self._slow_estimate, atomic=True))
        sl.add(SlowTask("query", self._slow_query, atomic=True))
        sl.add(SlowTask("fine_check", self._slow_fine, atomic=True))
        sl.add(SlowTask("cleanup", lambda b: self.engine._expire_idem(self.clock.wall_mono_ns()), period_wall_ns=1_000_000_000))
        sl.add(SlowTask("gc", self._slow_gc, period_wall_ns=GC_GEN2_PERIOD_NS))
        if self.checkpointer is not None:
            sl.add(SlowTask("checkpoint", lambda b: self.checkpointer.maybe_save(self), period_sim_ns=1_000_000_000))
        for t in self.reg.slow:
            sl.add(SlowTask(t.name, self._plugin_task(t), int((t.period_wall_s or 0) * 1e9), int((t.period_sim_s or 0) * 1e9),
                            owner="plugin"))

    def _plugin_task(self, t: R.SlowTaskSpec) -> Callable[[float], Any]:
        def run(budget_us: float) -> Any:
            try:
                return t.fn(self.ctx)
            except Exception:
                log.exception("slow task failed", extra={"kv": {"task": t.name}})
                return None
        return run

    def _slow_geo(self, budget_us: float) -> Any:
        if self.geo is None or not self.geo.q:
            return False
        self.geo.run(int(min(500, budget_us)))
        return None

    def _slow_estimate(self, budget_us: float) -> Any:
        if not self._est_q:
            return False
        req, _t_enq = self._est_q.popleft()
        msg = req.msg() if hasattr(req, "msg") else req
        msg = msg if isinstance(msg, dict) else {}
        if not self.estimator.admit(self.clock.wall_mono_ns()):
            rep = {"v": 1, "feasible": False, "code": int(Reason.RATE_LIMITED)}
        else:
            try:
                rep = self.estimator.estimate(msg)
            except Exception:
                log.exception("estimate failed")
                rep = {"v": 1, "feasible": False, "code": int(Reason.SERVICE_UNAVAILABLE)}
        with contextlib.suppress(Exception):
            req.reply_msg(rep)
        return None

    def _slow_query(self, budget_us: float) -> Any:
        if not self._query_q:
            return False
        req = self._query_q.popleft()
        m = req.msg() or {}
        op = str(m.get("op"))
        local = self._local_query(op, m.get("args") or {})
        if local is not None:
            req.reply_msg(local)
            return None
        spec = self.reg.queries.get(op)
        if spec is None:
            req.reply_msg({"v": 1, "code": int(Reason.SERVICE_UNAVAILABLE), "detail": {"op": m.get("op")}})
            return None
        out = spec.fn(m, self.ctx)
        if isinstance(out, (bytes, bytearray)):
            req.reply(bytes(out))
        else:
            req.reply_msg(out)
        return None

    def _local_query(self, op: str, args: dict) -> dict | None:
        """M08 自身的只读查询（REST `rest/fleet.py` 经 `ctl/sim-core/query` 转发，M08-FR-080）。"""
        if op == "fleet/profiles":
            return {"v": 1, "code": 0, "items": self.T.summary()}
        if op == "fleet/profile":
            pid = str(args.get("profile_id"))
            if pid not in self.T.profiles:
                return {"v": 1, "code": int(Reason.NOT_FOUND), "detail": {"profile_id": pid}}
            return {"v": 1, "code": 0, "item": self.T.describe(pid)}
        if op == "fleet/caps":
            return {"v": 1, "code": 0, "items": {"mock": dict(self.caps.raw), "replay": dict(self.replay_caps.raw)}}
        if op == "fleet/vehicles":
            return {"v": 1, "code": 0, "items": self.vehicle_items(args.get("id"))}
        return None

    def vehicle_items(self, vid: str | None = None) -> list[dict]:
        S = self.fleet.S
        out = []
        ext = {no: x for no, x in self.state_ext_items()}
        for s in self.roster.slots_in_order():
            e = self.roster.by_slot[s]
            if vid is not None and e.id != vid:
                continue
            sb = S.blocks["safety"]
            fs, sub = int(sb["fs"][s]), int(sb["sub"][s])
            out.append({"id": e.id, "agent_no": e.agent_no, "profile_id": e.profile_id, "model": e.model,
                        "backend": e.backend, "lifecycle": LIFECYCLE_NAMES[e.lifecycle],
                        "flight_state": SM.FS(fs).name, "sub": SM.SUB[SM.FS(fs)][sub] if sub < len(SM.SUB[SM.FS(fs)]) else None,
                        "pos_enu_m": [round(float(v), 3) for v in S.enu.pos[s]],
                        "home_enu_m": [round(float(v), 3) for v in e.home_enu_m], "state_ext": ext.get(e.agent_no)})
        return out

    def _slow_fine(self, budget_us: float) -> Any:
        if not self.engine.fine_queue:
            return False
        self.engine.run_fine_checks(1)
        return None

    def _slow_gc(self, budget_us: float) -> Any:
        """gc gen2 手动回收（D1-core 每 ≥ 30 s【墙钟】一次；启用 checkpoint 时紧随 checkpoint 拷贝，ADR-021 ③）。"""
        t0 = self.perf_ns()
        gc.collect(2)
        self._gc_ms.append((self.perf_ns() - t0) / 1e6)
        return None

    def _slow_state_ext(self, budget_us: float) -> Any:
        """`state_ext` 2 Hz【墙钟】分片打包（每片 ≤ 16 架，跨迭代完成后一次发布）。"""
        now = self.wall_ns()
        if self._ext_pos < 0:
            if now - self._ext_last < STATE_EXT_PERIOD_NS or not self.roster.by_slot:
                return False
            self._ext_last = now
            self._ext_order = self.roster.slots_in_order()
            self._ext_items = []
            self._ext_pos = 0
        chunk = self._ext_order[self._ext_pos:self._ext_pos + EXT_SLICE]
        self._ext_items += self._ext_rows(chunk)
        self._ext_pos += EXT_SLICE
        if self._ext_pos >= len(self._ext_order):
            self._ext_pos = -1
            if self._pub_ext is None:
                self._pub_ext = self.bus.publisher(bus_keys.state_ext("sim-core"))
            self._pub_ext.put(msgpack.packb(self._ext_items, use_bin_type=True))
        return None

    def state_ext_items(self) -> list[list]:
        """`state/sim-core/ext` 的载荷：`[[agent_no, state_ext], ...]`（awr.uav.state_ext.v1）。"""
        return self._ext_rows(self.roster.slots_in_order())

    def _ext_rows(self, slots: list[int]) -> list[list]:
        S = self.fleet.S
        out = []
        sb = S.blocks["safety"]
        bb = S.blocks.get("battery")
        for slot in slots:
            e = self.roster.by_slot.get(slot)
            if e is None:
                continue
            fs, sub = int(sb["fs"][slot]), int(sb["sub"][slot])
            px = SM.mock_emulate_px4(SM.FS(fs), sub, SM.Intent())
            nav = SM.nav_from_custom_mode(px.custom_mode)
            acc = S.enu.acc[slot]
            soc = float(bb["soc"][slot]) if bb is not None and "soc" in bb and bb["battery_pct"][slot] != 255 else None
            ext = {"lifecycle": LIFECYCLE_NAMES[e.lifecycle],
                   "lease": {k: v for k, v in self.lease.lease_json(slot).items()},
                   "loc": {"status": "TRACKING", "gnss_fix": None, "sats": None},
                   "battery": None if soc is None else {"voltage_v": None, "current_a": None, "soc_pct": round(soc * 100, 1),
                                                        "t_remain_s": None, "wh_used": None},
                   "mission": None,
                   "accel_mps2": [float(acc[0]), float(acc[1]), float(acc[2])],
                   "home_enu_m": [float(v) for v in e.home_enu_m],
                   "frames": {"t_world_local": None},
                   "link": {"gcs_age_ms": None, "fcu_age_ms": 0},
                   "gcs_loss_policy": "ignore",
                   "px4": {"arming_state": 2 if px.armed else 1, "nav_state": None if nav is None else int(nav),
                           "landed_state": px.landed, "system_status": px.system_status, "custom_mode": px.custom_mode}}
            if self._ext_extra:
                ext.update(self._ext_m08(slot, e))
            out.append([e.agent_no, ext])
        # INT-1（M09-to-M08 第 5 条）：M09 的电池、链路年龄与 gcs_loss_policy（SafetyHooks.state_ext_fields），缺失时保持上面的缺省
        hooks = getattr(self.reg, "hooks", None)
        fn = getattr(hooks, "state_ext_fields", None)
        if fn is not None and out:
            try:
                extra = fn(np.asarray([sl for sl in slots if sl in self.roster.by_slot], np.int64))
            except Exception:
                extra = {}
            if extra:
                slot_of = {self.roster.by_slot[sl].agent_no: sl for sl in slots if sl in self.roster.by_slot}
                for row in out:
                    add = extra.get(slot_of.get(row[0], -1))
                    if add:
                        row[1].update({k: v for k, v in add.items() if k in ("battery", "link", "gcs_loss_policy")})
        return out

    def _ext_m08(self, slot: int, e) -> dict:
        """M08-FR-051 的附加字段（契约登记后输出）。"""
        S = self.fleet.S
        p = self.T.get(e.profile_id)
        nav = int(S.ctrl_mode[slot]) in (3, 4, 5, 6, 8, 9, 15)
        pe = float(np.linalg.norm(S.enu.pos_ref[slot] - S.enu.pos[slot])) if nav else None
        wr = S.enu.vel[slot] - S.enu.wind(slot)
        qx, qy = float(S.enu.q_xyzw[slot, 0]), float(S.enu.q_xyzw[slot, 1])
        tilt = float(np.degrees(np.arccos(np.clip(1.0 - 2.0 * (qx * qx + qy * qy), -1.0, 1.0))))
        from ..fleet.state import CtrlMode

        return {"profile": {"id": p.profile_id, "version": p.version, "status": p.status},
                "ctrl": {"mode": CtrlMode(int(S.ctrl_mode[slot])).name, "phase": int(S.ctrl_phase[slot])},
                "thrust_frac": round(float(S.thrust[slot]), 4), "tilt_deg": round(tilt, 3),
                "pos_err_m": None if pe is None else round(pe, 3),
                "wind_rel_mps": [round(float(x), 3) for x in wr]}

    def _publish_perf(self) -> None:
        if self._pub_perf is None:
            self._pub_perf = self.bus.publisher(bus_keys.STATE_PERF)
        self.bus_perf_msg = msg = self.perf_msg()
        self._pub_perf.put(msgpack.packb(msg, use_bin_type=True))

    def perf_msg(self) -> dict:
        stage = self.fleet.pipeline.take_stage_ns()
        now_cpu, now_w = time.process_time(), self.wall_ns()
        c0, w0 = self._proc_cpu
        cpu_pct = 100.0 * (now_cpu - c0) / max(1e-9, (now_w - w0) / 1e9) if w0 else 0.0
        self._proc_cpu = (now_cpu, now_w)
        t_sim0 = self._perf_t_sim0
        self._perf_t_sim0 = self.clock.t_ns
        dt_sim_s = (self.clock.t_ns - t_sim0) / 1e9
        if dt_sim_s <= 0:
            dt_sim_s = 1.0
        ms_per_s = {k: round(v / 1e6 / dt_sim_s, 3) for k, v in stage.items()}
        for k, v in ms_per_s.items():
            b = B.BUDGET_CORE.get(k)
            over = b is not None and v > 1.25 * b * 1000.0
            if over and k not in self._overbudget:
                self._overbudget.add(k)
                self.events.emit("sim.stage.overbudget", t_sim_ns=self.clock.t_ns, severity=1, stage=k, ms_per_s=v,
                                 budget_ms_per_s=round(b * 1000.0, 3))
            elif not over:
                self._overbudget.discard(k)
        S = self.fleet.S
        adm = self.engine.admission_us
        tap = self.fleet.tap
        est = self.slow.get("estimate")
        msg = {"v": 1, "stage_ms_per_s": ms_per_s, "n_active": len(self.roster.by_slot),
               "n_l0": int(np.count_nonzero(S.active & ((S.fidelity & 8) != 0))), "kernel": self.fleet.kernel,
               "cpu_pct": round(cpu_pct, 1), "rtf_limited": bool(self.clock.rtf_limited),
               "rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1),
               "env_query_us_p99": round(self.fleet.pipeline.p99_us("env"), 1) if "env" in self.fleet.pipeline.stats else None,
               "publish_us_p99": round(self.fleet.pipeline.p99_us("tap"), 1) if tap is not None else None,
               "admission_us_p99": round(float(np.percentile(adm, 99)), 1) if adm else None,
               "estimate_us_p99": round(est.p99_us(), 1) if est is not None and est.runs else None,
               "gc_gen2_ms_max": round(max(self._gc_ms), 3) if self._gc_ms else None}
        if self.geo is not None:
            msg["geo"] = self.geo.metrics()
        return msg

    def _write_step_stats(self, now: int) -> None:
        a = np.asarray(self._step_us, np.float64) if self._step_us else np.zeros(1)
        t0w, t0s = self._rtf_t0
        if t0w:
            dw = now - t0w
            self._rtf_milli = round(1000 * (self.clock.t_ns - t0s) / dw) if dw > 0 else 1000
        self._rtf_t0 = (now, self.clock.t_ns)
        self.ring.set_step_stats(int(np.percentile(a, 50)), int(np.percentile(a, 99)), int(a.max()),
                                 self.cfg.fleet.tick_budget_us, self._rtf_milli, self.clock.catchup_saturated)
        self._step_us.clear()
        self._last_stats_ns = now

    # ------------------------------------------------------------ meta.json 与 checkpoint
    def _write_meta(self) -> None:
        """meta.json 的 `sim` 段与 `vehicles_profiles`（rec/meta.schema.json）；另写 sidecar `sim-core.json`。"""
        if self.persist_dir is None:
            return
        side = {"sim_kernel": self.fleet.kernel, "kernel_fallback": self.fleet.kernel_fallback,
                "fleet_config": self.cfg.fleet.to_json(), "world_seed": self.cfg.world_seed,
                "warmup_s": round(getattr(self, "warmup_s", 0.0), 3), "affinity": sorted(os.sched_getaffinity(0)),
                "nice": os.getpriority(os.PRIO_PROCESS, 0), "plugins": self.plugins_loaded,
                "plugins_missing": self.plugins_missing, "cpu_model": _cpu_model()}
        with contextlib.suppress(OSError):
            (self.persist_dir / "sim-core.json").write_text(json.dumps(side, ensure_ascii=False, indent=1) + "\n")
        mp = self.persist_dir / "meta.json"
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            meta = json.loads(mp.read_text(encoding="utf-8"))
            sim = meta.setdefault("sim", {})
            sim["kernel"] = self.fleet.kernel
            sim["fleet_config"] = {**self.cfg.fleet.to_json(), "affinity": side["affinity"], "nice": side["nice"]}
            sim["world_seed"] = int(self.cfg.world_seed)
            sim["fastmath"] = False
            sim.update(_versions())
            import hashlib

            vp = []
            for pid in self.T.ids:
                p = self.T.get(pid)
                sha = hashlib.sha256(Path(p.source).read_bytes()).hexdigest() if p.source else "0" * 64
                vp.append({"profile_id": pid, "profile_version": p.version, "sha256": sha})
            meta["vehicles_profiles"] = vp
            tmp = mp.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            os.replace(tmp, mp)

    def checkpoint_now(self) -> bool:
        if self.checkpointer is None:
            return False
        return bool(self.checkpointer.save(self, force=True))

    # ------------------------------------------------------------ 停止
    def stop(self, reason: str = "stop") -> None:
        with contextlib.suppress(Exception):
            self.events.emit("sim.stopped", t_sim_ns=self.clock.t_ns, severity=1, epoch=self.epoch, segment=self.segment,
                             reason=reason, kernel=self.fleet.kernel if self.fleet else "numpy", n=len(self.roster.by_slot))
            self.events.flush()
        if self.geo is not None:
            with contextlib.suppress(Exception):
                self.geo.close()
        if self.checkpointer is not None:
            with contextlib.suppress(Exception):
                self.checkpointer.close(final=True)
        if self.inputlog is not None:
            with contextlib.suppress(Exception):
                self.inputlog.close()
        for h in reversed(self.handles):
            with contextlib.suppress(Exception):
                h.close()
        self.handles.clear()
        with contextlib.suppress(Exception):
            self.events.close()
        self.audit.close()


def _versions() -> dict[str, Any]:
    """meta.json `sim` 段的版本字段（rec/meta.schema.json：kernel_version、numba_version、numpy_version、python_version）。"""
    import platform

    from .inputlog import _version

    try:
        import numba

        nb: str | None = str(numba.__version__)
    except Exception:
        nb = None
    return {"kernel_version": _version(), "numba_version": nb, "numpy_version": str(np.__version__),
            "python_version": platform.python_version()}


def _cpu_model() -> str:
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return ""


# ---------------------------------------------------------------- 入口
def run(cfg: SimConfig, bus: Any, ring: StateRing, *, stop: threading.Event | None = None, secret: bytes | None = None,
        reused: bool = False, audit_path: Path | None = None, ready: threading.Event | None = None) -> SimCore:
    """`--inproc` 与测试入口（M11-FR-102）：在调用线程内运行主循环，直到 stop 置位。"""
    core = SimCore(cfg, bus, ring, reused=reused, secret=secret, audit_path=audit_path)
    core.start()
    if ready is not None:
        ready.set()
    ev = stop or threading.Event()
    try:
        core.run(ev.is_set)
    finally:
        core.stop()
    return core


def _write_profile_failure(persist_dir: Path, err: ProfileError) -> None:
    with contextlib.suppress(OSError):
        (persist_dir / "sim-core.json").write_text(json.dumps(
            {"status": "failed", "code": err.code, "reason": "profile_invalid", "profile_id": err.profile_id,
             "problems": err.problems}, ensure_ascii=False, indent=1) + "\n")


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m awr.sim.runtime")
    ap.add_argument("--resim", default=None, help="runs/<run>：按输入日志批处理重仿真（D1-ext，M08-FR-084）")
    ap.add_argument("--out", default=None, help="重仿真输出的 Full64 帧摘要路径")
    a = ap.parse_args(argv)
    if a.resim:
        from .resim import resim_main

        return resim_main(Path(a.resim), Path(a.out) if a.out else None)

    from awr.runtime.bus import ZenohBus
    from awr.runtime.child import init_child

    ctx = init_child("sim-core")
    cfg = SimConfig.from_env(world_id=ctx.world_id, run_id=ctx.run_id)
    secret = None
    with contextlib.suppress(OSError, FileNotFoundError):
        secret = ctx.read_secret()
    ring, reused = StateRing.open_or_create(ctx.run_dir / "state.sim-core", capacity=cfg.fleet.capacity, slots=32,
                                            layout_id=LAYOUT_ID, id_base=ctx.id_base, id_count=ctx.id_count or 1024)
    bus = ZenohBus.open("sim-core", ctx)
    rc = 0
    core: SimCore | None = None
    try:
        try:
            core = SimCore(cfg, bus, ring, reused=reused, secret=secret,
                           audit_path=ctx.persist_dir / "audit.jsonl" if ctx.supervised else None,
                           persist_dir=ctx.persist_dir if ctx.supervised else None)
        except ProfileError as e:  # 353 VEHICLE_PROFILE_INVALID：拒绝启动（M08-FR-042）
            log.error("vehicle profile invalid", extra={"kv": {"code": e.code, "profile": e.profile_id, "problems": e.problems}})
            with contextlib.suppress(Exception):
                ev = EventPublisher(bus, "sim-core", 1)
                ev.emit("sim.stopped", t_sim_ns=0, severity=3, epoch=1, segment=0, reason="profile_invalid",
                        kernel="numpy", n=0, code=e.code, detail=e.problems[:4])
                ev.flush()
            if ctx.supervised:
                _write_profile_failure(ctx.persist_dir, e)
            return 3
        from .ckpt import attach_checkpoint
        from .inputlog import attach_inputlog

        attach_inputlog(core, ctx)
        attach_checkpoint(core, ctx)
        core.start()
        core.run(lambda: ctx.stopping)
    except Exception:
        log.exception("sim-core crashed")
        rc = 1
    finally:
        if core is not None:
            core.stop("sigterm" if ctx.stopping else "error")
        with contextlib.suppress(Exception):
            bus.close()
        with contextlib.suppress(Exception):
            ring.close()
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
