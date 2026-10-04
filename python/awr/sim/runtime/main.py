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
import math
import os
import resource
import signal
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
from awr.runtime.checkpoint import IdleGate
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
from .clock import TICK_NS, SimClock
from .config import SimConfig
from .slow import SlowTask, SlowTasks

__all__ = ["SimCore", "compose_plugins", "defer_numba_blas_probe", "main", "run"]

log = logging.getLogger("awr.sim.runtime")

DRAIN_MAX = 512
# 每轮 drain 的墙钟预算（ADR-073 第 4 条）：超出即把余下的请求留到下一轮（至少处理 1 条，FIFO 不变）。一条带验签、审计、
# 输入日志与 3 个生命周期事件的命令约 0.3–1 ms，突发 20–40 条（D1-AC-27 link_drop：500 架逐机 R16，20 条并发）此前在
# 同一轮内全部处理，单步 32–39 ms
DRAIN_BUDGET_NS = 1_500_000
IDLE_WAIT_S = 0.05
# 空闲窗口的主循环等待余量（IdleGate slack，ADR-073 第 2 条）：checkpoint 后台拷贝可越过窗口末尾、让主循环醒来后等它，
# 只要下一轮（成对推进的两个 tick）按其相位的耗时上包络加上等待仍不超过 2 × 2.6 ms（单步目标 3 ms 留 0.4 ms 余量）
BG_ITER_TARGET_NS = 5_200_000
ITER_COST_DECAY = 0.1  # 各相位迭代耗时上包络的衰减（每次该相位迭代向实测值靠拢 10%，实测更大时直接取实测）
STATE_EXT_PERIOD_NS = 500_000_000
PERF_PERIOD_NS = 1_000_000_000
AUDIT_FSYNC_NS = 1_000_000_000
# gc_young：gen0 阈值、gen1 周期、两级强制阈值。gen1 强制阈值 30 → 120（FX2-R3-sim）：N = 1000 时 gen0 约每秒 30 次，
# 30 次即在下一次 gen2（每 1 s，紧随 checkpoint）之前强制一次约 2 ms 的 gen1，落在任意 tick 上；gen2 会清零各代计数，
# 120 次（约 4 s）的强制阈值只在 gen2 迟迟不来时生效。gen1 周期 10 → 120（ADR-073 第 2 条）：每 1 s 一次的 gen2 已扫描
# 这 1 s 内全部存活对象，其间的 gen1（生产口径 1.5–2.3 ms，D1 验收第 3 轮的单步尖峰之一）只是重复扫描
GC0_MIN, GC1_EVERY, GC0_FORCE, GC1_FORCE = 700, 120, 5_000, 120
GC_GEN2_PERIOD_NS = 1_000_000_000  # 手动 gen2 周期【墙钟】；每次回收后冻结幸存者，只扫描本周期新增的存活对象；1 s 一次（ADR-070；
#                                    此前 2 s，单次约 3 ms）与 1 s 一代的 checkpoint 同轮执行，单次停顿减半；2 s 一次使
#                                    单次停顿与该周期新增对象成正比（全机 RTL 等突发期间 5 s 一次约 9 ms，ADR-065）
GEN2_INLINE_US = 3000.0  # gen2 的估计耗时上限：超过时本次只冻结、不回收（风暴期间，_gen2_or_freeze，ADR-073 第 2 条）
GEN2_SKIP_ADMITTED = 64  # 自上次 gen2 以来准入的调用数达到此值（全机 RTL、500 架 link_drop 等风暴）时本次只冻结
GEN2_MAX_SKIPS = 30  # 连续只冻结的上限（约 30 s）：持续高负载时也定期回收，引用环的滞留有界
SWITCH_INTERVAL_S = 0.001  # GIL 切换间隔（CPython 缺省 5 ms）：后台线程（checkpoint 写盘、总线回调、规划池结果）至多占 GIL
#                            约 1 ms 就交还主循环（D1-AC-07 单步最大值；ADR-065）
EXT_SLICE = 16
EXT_SLICE_MIN, EXT_SLICE_MAX = 8, 48  # state_ext 每片机数的上下限（按剩余预算自适应，_slow_state_ext）
EXT_STARVE_NS = 50_000_000  # state_ext 连续因预算不足跳过的墙钟上限，超过即强制做一片最小片
# 强制片只落在轻轮（ADR-074 第 4 条）：饿死后本轮剩余预算 ≥ EXT_FORCE_MIN_US 才强制，连续超过 EXT_STARVE_HARD_NS 时不再挑轮。
# N = 1000 时 8 架一片生产口径约 0.9–1.2 ms（每片固定开销约 0.6 ms），逐机上包络使片长停在下限、几乎每片都由饿死兜底，
# 此前兜底不看本轮轻重，约一半落在剩余预算不足 600 µs 的轮次（26% 在最重的三成 tick 对上），成为各秒第 2、3 大的迭代（单步 p99
# 的来源，D1 验收第 4 轮）；挑轮之后为 8%、1%
EXT_FORCE_MIN_US = 600.0
EXT_STARVE_HARD_NS = 150_000_000
EXT_US_FLOOR = 15.0  # state_ext 逐机耗时估计的下限（µs）：防止轻载片把估计压得过低后一片取得过大
EXT_US_DECAY = 0.1  # state_ext 逐机耗时上包络的衰减（_slow_state_ext，ADR-073 第 3 条）
SPAWN_R_SAFE_M = 1.5
SPAWN_LINE_MAX = 64  # 骨架布设：N ≤ 64 时一字排开，更大的 N 排成网格（_spawn_plan）
QUERY_Q_MAX = 8
# fleet/vehicles 全表查询的分片（ADR-074 第 2 条）：N > VEH_SYNC_MAX 时不在一次慢任务内同步构造，按剩余预算每次编码一片
# （VEH_SLICE_MIN–VEH_SLICE_MAX 架，各条目即时 msgpack 编码），全部片完成后只拼接映射头与各条目字节回复。此前 N = 1000
# 时一次构造并编码约 30–60 ms 落在一轮内（D1 验收第 4 轮 D1-AC-27 link_drop 注入前 `GET /api/fleet/vehicles` 的单步最大
# 32–35 ms）。作业按不可分片任务的公平轮转与借贷启动（ADR-057：顺延满 20 ms【墙钟】强制启动），作业存在超过
# VEH_ESCALATE_NS【墙钟】后每片取上限，N = 1000 时 sim-core 侧回复时延约 0.3–0.5 s、REST 端到端约 0.75 s（总线超时 1.5 s）
VEH_SYNC_MAX = 64
VEH_SLICE_MIN, VEH_SLICE_MAX = 8, 64
VEH_ESCALATE_NS = 200_000_000
VEH_US_FLOOR = 5.0
_SE_KEY = msgpack.packb("state_ext", use_bin_type=True)
_POS_KEY = msgpack.packb("pos_enu_m", use_bin_type=True)
AUX_PIN_PERIOD_NS = 2_000_000_000  # 后台线程亲和性的重扫周期【墙钟】（cpuaff，ADR-070）


def _state_ext_extra_allowed() -> bool:
    """state_ext 的 M08 附加字段（profile、ctrl、thrust_frac 等，M08-FR-051）只在契约登记后输出（schema 为 additionalProperties false）。"""
    try:
        from awr.contracts._paths import schema_path

        d = json.loads(schema_path("rt/payloads/uav_state_ext.schema.json").read_text(encoding="utf-8"))
        return "thrust_frac" in d.get("properties", {})
    except Exception:
        return False


def defer_numba_blas_probe() -> bool:
    """推迟 numba 的 BLAS 探测导入（D1-AC-11a，FX-SIM1）。

    numba 在进程内首次编译或读缓存时导入 `numba.np.arraymath`，其模块级 `_check_blas()` 为确认 BLAS 可用而导入
    `scipy.linalg`（连带 array_api_compat 对 numpy 全部子模块的克隆，约 0.4 s），而 sim-core 的全部 njit 核都不用 BLAS。
    本函数在该模块首次导入时让探测直接判定"可用"（scipy 已是锁定依赖，判定结果与原探测相同），不改变 `numba.np.linalg`
    自身：真正编译 BLAS 类函数时仍经其 `ensure_blas()` 导入 scipy。scipy 不可用、numba 不可用或该模块已导入时不做任何事。"""
    if "numba.np.arraymath" in sys.modules:
        return False
    try:
        import importlib.util

        if importlib.util.find_spec("scipy") is None or importlib.util.find_spec("numba") is None:
            return False
        import numba.np.linalg as _nl
    except Exception:
        return False
    orig = _nl.ensure_blas
    _nl.ensure_blas = lambda: None
    try:
        import numba.np.arraymath  # noqa: F401  模块级 _check_blas() 在此执行
    except Exception:
        return False
    finally:
        _nl.ensure_blas = orig
    return True


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


# ---------------------------------------------------------------- state_ext 辅助（按片向量化，_ext_rows）
_NAV_LUT = np.zeros(256, np.bool_)
_NAV_LUT[[3, 4, 5, 6, 8, 9, 15]] = True  # pos_err 只对导航类运动模式有意义（GOTO、PATH、ORBIT、HOLD、LAND、RTL、TRAJ）
_PX4_MEMO: dict[tuple[int, int], dict] = {}
_CTRL_NAMES: dict[int, str] = {}
# state_ext 直接编码（_ext_rows_packed）的常量片段
_EXT_KEYS = {k: msgpack.packb(k) for k in ("lifecycle", "lease", "loc", "battery", "mission", "accel_mps2", "home_enu_m",
                                            "frames", "link", "gcs_loss_policy", "px4", "profile", "ctrl", "thrust_frac",
                                            "tilt_deg", "pos_err_m", "wind_rel_mps", "sens")}
_NIL = msgpack.packb(None)
_ARR2 = msgpack.Packer(use_bin_type=True).pack_array_header(2)
_FRAMES_B = msgpack.packb({"t_world_local": None}, use_bin_type=True)
_LINK_DEFAULT = msgpack.packb({"gcs_age_ms": None, "fcu_age_ms": 0}, use_bin_type=True)
_LC_B: dict[int, bytes] = {}
_STR_B: dict[str, bytes] = {}


def _lc_bytes(lc: int, pk: Callable[[Any], bytes]) -> bytes:
    b = _LC_B.get(lc)
    if b is None:
        b = _LC_B[lc] = pk(LIFECYCLE_NAMES[lc])
    return b


def _str_bytes(v: Any, pk: Callable[[Any], bytes]) -> bytes:
    if not isinstance(v, str):
        return pk(v)
    b = _STR_B.get(v)
    if b is None:
        b = _STR_B[v] = pk(v)
    return b


def _px4_json(fs: int, sub: int) -> dict:
    """state_ext 的 px4 段：`mock_emulate_px4(fs, sub, Intent())` 只取决于 (fs, sub)，按其记忆（返回副本）。"""
    d = _PX4_MEMO.get((fs, sub))
    if d is None:
        px = SM.mock_emulate_px4(SM.FS(fs), sub, SM.Intent())
        nav = SM.nav_from_custom_mode(px.custom_mode)
        d = _PX4_MEMO[(fs, sub)] = {"arming_state": 2 if px.armed else 1, "nav_state": None if nav is None else int(nav),
                                    "landed_state": px.landed, "system_status": px.system_status,
                                    "custom_mode": px.custom_mode}
    return dict(d)


def _ctrl_name(enum: Any, mode: int) -> str:
    n = _CTRL_NAMES.get(mode)
    if n is None:
        n = _CTRL_NAMES[mode] = enum(int(mode)).name
    return n


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
        self._step_us: deque[float] = deque(maxlen=1000)  # 每 tick 的单步耗时（drain → 慢任务结束，StateRing step_stats）
        self._pipe_us: deque[float] = deque(maxlen=1000)  # 其中 pipeline 部分（诊断）
        self._last_stats_ns = 0
        self._rtf_t0 = (0, 0)
        self._rtf_milli = 1000
        self._pub_ext = None
        self._pub_perf = None
        self._proc_cpu = (time.process_time(), 0)
        self._perf_t_sim0 = 0
        self._est_q: deque = deque()
        self._query_q: deque = deque()
        self._veh_jobs: deque = deque()  # fleet/vehicles 全表查询的分片作业（_slow_vehicles，ADR-074 第 2 条）
        self._veh_us_per = 30.0  # 逐机编码耗时的上包络（µs），按预算取片长
        self._veh_static: dict[int, tuple] = {}  # slot -> (条目对象, 前 5 个键值对的编码, home_enu_m 键值对的编码)
        self._veh_lc: dict[int, bytes] = {}
        self._veh_fs: dict[tuple[int, int], bytes] = {}
        self._ext_us_per = 60.0  # state_ext 逐机耗时（含每片固定开销的均摊）的上包络（µs），用于按预算取片长
        self._ext_pub_us = 500.0  # state_ext 整体拼接与发布耗时的上包络（µs）：末片之后剩余预算不足时发布顺延一轮
        self.ext_pub_bg: Any = None  # 空闲窗口发布线程（bgpub.GatedPublisher；sim-core 进程 main() 置入，ADR-073 第 3 条）
        self._ext_packed: list[bytes] = []
        self._ext_packer = msgpack.Packer(use_bin_type=True)
        self._ext_cache: dict = {}  # _ext_rows_packed 的编码缓存（px4、ctrl、profile、lease）
        self._ext_slice_wall = 0
        self._ext_rows_last: dict[int, bytes] = {}  # 最近一次完整发布的 state_ext（agent_no → msgpack 行），fleet/vehicles 复用
        self._ext_agents: list[int] = []
        self._ext_order: list[int] = []
        self._ext_pos = -1
        self._ext_last = 0
        self._last_gc = 0
        self._gc_ms: deque[float] = deque(maxlen=64)
        self._gc_young_ms: deque[float] = deque(maxlen=256)
        self._gc_est_us = [150.0, 800.0]  # gen0、gen1 预计耗时（µs，指数平均），gc_young 据此判断本轮预算是否放得下
        self._gen2_est_us = 0.0  # 上一次 gen2 的耗时（µs；跳过时按 0.7 衰减），超过 GEN2_INLINE_US 时只冻结（ADR-073）
        self._gen2_skipped = 0
        self._gen2_adm0 = 0
        self._gen2_run_skips = 0
        self._overbudget: set[str] = set()
        self._rtf_limited_reported = False
        self._removing: dict[int, str] = {}
        self._stopped: list[int] = []
        self._lc_synced: tuple | None = None  # S.lifecycle 已按此 roster 状态同步（_lifecycle）
        self._ext_extra = _state_ext_extra_allowed()
        self._reset_hooks: list[Callable[[dict], None]] = []
        self.slow = SlowTasks(perf_ns, cap_us=float(cfg.fleet.slow_budget_us))
        self.checkpointer: Any = None
        self._sp_raw: deque[bytes] = deque(maxlen=8192)
        self.inputlog: Any = None
        self.restored_ext: list[str] = []
        self.post_step_hooks: list[Callable[[int, int, int], Any]] = []
        self._post_step_us: deque[float] = deque(maxlen=1000)
        self.started = False
        self.manual_gc = False  # True：年轻代回收改由慢任务执行（sim-core 进程 main() 置位，ADR-065）
        self.pin_aux_threads = False  # True：后台线程改到主循环以外的核（sim-core 进程 main() 置位，cpuaff，ADR-070）
        self.pair_ticks = False  # True：×1 下奇偶 tick 成对推进，主循环 125 Hz 唤醒（sim-core 进程 main() 置位，ADR-070）
        # 主循环空闲窗口（休眠前 open(预计醒来时刻)、醒来后 close()）：checkpoint 写线程只在窗口内、距窗口结束有余量时编码，
        # 主循环醒来时 GIL 空闲（CheckpointStore gate；ADR-070 第 6 条③，ADR-073 第 1 条改为带截止时刻的窗口）
        self.idle_gate = IdleGate()
        self.state_lock = threading.Lock()  # 仿真状态锁：主循环每轮迭代持有；后台 checkpoint 拷贝在空闲窗口内持有（ADR-073）
        self.iter_cost_ns = [float(BG_ITER_TARGET_NS)] * 50  # 按 tick % 50 相位的迭代耗时上包络（run()，IdleGate slack）
        self.aux_pinner: Any = None

    # ------------------------------------------------------------ 启动
    def start(self) -> None:
        cfg = self.cfg
        defer_numba_blas_probe()
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
        # 年轻代回收也改由慢任务在本轮剩余预算内执行（gc_young；超量时强制），不再在任意 stage 的分配点被触发：
        # 大机群时 gen1 一次 2–7 ms，落在重 tick 上即抬高单步尾部（ADR-065）。只在 sim-core 进程（main）中启用；
        # 进程内测试台保持自动回收。stop() 时恢复
        if self.manual_gc:
            gc.disable()
        self._last_gc = self.wall_ns()
        if self.aux_pinner is not None and self.aux_pinner.active:
            self.aux_pinner.scan()
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
        # 外部度量（ADR-058；M14-to-M08 第 3 条）：agent-runtime 经 `scenario/metric{value}` 写入，M10 剧本导演读取
        for name, keys in (("target_confidence", ("target_id",)), ("t_conf_s", ("target_id", "threshold"))):
            spec = have.get(name)
            if spec is not None and not MET.is_external(name):
                continue  # 他人以计算型度量登记了同名度量：不覆盖
            if spec is None:
                MET.register_external_metric(name, owner="M14", keys=keys)
        MET.clear_external()

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
        x, y = self._spawn_plan(k)
        z = 0.0
        if self.world is not None:
            z = float(self.world.height_dsm(np.array([[x, y]]))[0])
        return (x, y, z)

    def _spawn_plan(self, k: int) -> tuple[float, float]:
        """骨架布设的第 k 个出生点（水平）。出生基点（`AWR_SIM_SPAWN` 或平坦开阔格）每个 SimCore 只求一次（D1-验收第 1 轮
        4.4：此前每架机重复 51 ms 的平坦格搜索，N = 1000 约 51 s，超过 15 s 启动宽限）。N ≤ SPAWN_LINE_MAX 时沿 +x 一字排开
        （间距 `spawn_spacing_m`，与此前一致）；更大的 N 排成 C 列网格（C ≡ 2 mod 4，行距同列距），使四层交错起飞
        （`k % 4`，fleet_ladder `ladder_load`）后同层水平距离 ≥ 2 × 间距，且网格不越出世界边界（6 m × 1000 架一字排开为 6 km）。"""
        base = getattr(self, "_spawn_base", None)
        if base is None:
            base = self._spawn_base = tuple(self.cfg.spawn_xy) if self.cfg.spawn_xy else self._flat_spot()
        d = self.cfg.spawn_spacing_m
        n = max(1, int(self.cfg.n_vehicles))
        if n <= SPAWN_LINE_MAX:
            return (base[0] + k * d, base[1])
        cols = math.ceil(math.sqrt(n))
        cols += (2 - cols % 4) % 4
        return (base[0] + (k % cols) * d, base[1] + (k // cols) * d)

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
                    backend: str = "mock", track: Any = None, sensors: list[str] | None = None) -> str:
        prof = self.T.get(profile_id)
        e = self.roster.add(vehicle_id=vehicle_id, model=prof.model, profile_id=profile_id, limits_profile=limits_profile,
                            home_enu_m=home_enu_m, yaw_rad=yaw_rad, initial_soc=initial_soc, t_ns=self.clock.t_ns,
                            emit=self.events.emit, backend=backend, sensor_names=sensors)
        spec = EntitySpec(e.id, Kind.UAV, profile_id, limits_profile, home_enu_m, yaw_rad, initial_soc)
        fid = R.Fidelity.L0 if backend == "replay" else R.Fidelity.L1
        self.fleet.add(spec, slot=e.slot, agent_no=e.agent_no, entity_id=e.id, t_s=self.clock.t_ns * 1e-9, fidelity=fid)
        if track is not None:
            self.fleet.kinematic.attach(e.slot, track, self.clock.t_ns * 1e-9)
        self.fleet.S.lifecycle[e.slot] = e.lifecycle
        self.agent_slot[e.agent_no] = e.slot  # 增量维护（agent_no 不复用；移除时在 _lifecycle 中整体重建）
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
        sensors = args.get("sensors")  # 剧本 `vehicles[].sensors` 子集（M13-to-M08 第 4 条）：roster `sensors[]` 只列这些
        if sensors is not None and not (isinstance(sensors, list) and all(isinstance(x, str) and x for x in sensors)):
            return int(Reason.PARAM_OUT_OF_RANGE), {"field": "args.sensors"}
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
                               initial_soc=float(args.get("initial_soc", 1.0)), sensors=sensors)
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
        key = (id(self.roster), id(S), self.roster.roster_version, self.roster.lc_gen, len(self.roster.by_slot))
        if key != self._lc_synced:  # roster 与生命周期未变时 S.lifecycle 已一致（每 tick 调用，ADR-060）
            for e in self.roster.by_slot.values():
                S.lifecycle[e.slot] = e.lifecycle
            self._lc_synced = key

    def _apply_backend_caps(self, extra: list | None = None) -> None:
        caps = [self.caps.clock] + ([self.replay_caps.clock] if any(e.backend == "replay"
                                                                     for e in self.roster.by_slot.values()) else [])
        if self.clock.apply_caps(caps + list(extra or [])):
            self.events.emit("sim.clock", t_sim_ns=self.clock.t_ns, severity=0, state=TIMESTATE_NAMES[self.clock.state],
                             rate=self.clock.rate)

    def register_reset_hook(self, fn: Callable[[dict], None]) -> None:
        self._reset_hooks.append(fn)

    def request_reset(self, reason: str, args: dict | None = None) -> None:
        """延迟剧本重置（M10 循环剧本 `on_complete = reset`，ADR-084）：stage 运行在 tick 之内，不能就地重置；入 inbox，
        下一次 drain（步边界外）执行 `reset(reason, args)`，语义与 `sim/reset` 相同（epoch + 1、segment + 1、机群重生）。"""
        self.inbox.put(("reset", str(reason), dict(args or {})))

    def reset(self, reason: str = "scenario_reset", args: dict | None = None) -> None:
        """剧本重置（C09；M08-FR-008）：segment + 1、epoch + 1，机群重生（RESTARTING → READY），租约 FREE，在途调用 canceled 6。"""
        self.engine.cancel_all(int(Reason.CANCELLED), "reset")
        from ..core import metrics as MET

        MET.clear_external()  # 外部度量随剧本重置清空（ADR-058）
        self.events.flush()
        S = self.fleet.S
        for s in list(self._stopped) + list(self._removing):
            e = self.roster.by_slot.get(s)
            if e is not None:
                self.fleet.remove(s)
                self.roster.remove(s, self.clock.t_ns, self.events.emit, "reset")
        self._stopped.clear()
        self._removing.clear()
        self.agent_slot = {x.agent_no: x.slot for x in self.roster.by_slot.values()}
        self.lease.reset()
        self.clock.apply("reset")
        self.ctx.tick, self.ctx.t_ns = 0, 0
        self._rtf_t0 = (0, 0)  # RTF 窗口随仿真时间归零重新开始（_write_step_stats）
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
        """一次主循环迭代；返回建议的休眠秒数。状态锁：后台 checkpoint 拷贝（ADR-073 第 2 条）在主循环休眠期间持有，
        主循环在本轮开头取得（等待计入单步）。"""
        t_iter0 = self.perf_ns()
        with self.state_lock:
            return self._iterate(t_iter0)

    def _iterate(self, t_iter0: int) -> float:
        clk = self.clock
        self.ring.heartbeat(clk.t_ns, clk.state, clk.rate_milli, step_seq=clk.tick)  # FR-003：先写心跳（时钟组）
        self._drain()
        n = clk.steps_due()
        if n:
            ts = self.perf_ns()
            self.ctx.tick = clk.tick
            if self.post_step_hooks or clk.per_tick:
                # 锁步（M14-to-M08 第 5 条）：逐 tick 推进，每步结束后同步调用钩子。设置期（剧本开局屏障，ADR-073 第 7 条）：
                # 逐 tick 推进，某个 tick 的 stage 置下内部保持后本轮不再推进（保持在下一 tick 生效，与倍速无关）
                k = 0
                while k < n:
                    self.fleet.step(self.ctx, 1)
                    clk.tick = self.ctx.tick
                    clk.advanced(1)
                    k += 1
                    if self.post_step_hooks:
                        self._run_post_step_hooks()
                    if clk.per_tick and clk.held:
                        break
                n = k
            else:
                self.fleet.step(self.ctx, n)
                clk.tick = self.ctx.tick
                clk.advanced(n)
            self._pipe_us.append((self.perf_ns() - ts) / 1000.0 / n)
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
        # 预算按本轮执行的 tick 数折算（"2800 µs − 本 tick 已用"对追帧批次的推广，ADR-057）：×1 下 n = 1 与原式相同；
        # 追帧（×N 或负载下每轮多个 tick）时慢任务不因一轮很长而只剩 100 µs 下限
        # 不足 100 µs 时本轮只执行已饿死的慢任务（heavy_skip，ADR-070）
        budget = min(float(self.cfg.fleet.slow_budget_us), max(1, n) * self.cfg.fleet.tick_budget_us - used_us)
        ck = self.checkpointer
        if ck is not None and ck.bg is not None and ck.bg.pending:
            budget = min(budget, ck.bg.SLOW_CAP_US)  # 后台拷贝在途：本轮少做慢任务，留出足够长的空闲窗口（ADR-073）
        self.slow.run(budget, now_wall=now, now_sim=clk.t_ns, heavy_skip=True)
        if n:
            # 单步耗时（18 §7.6、PERF-AC-030 的定义：每 tick 从 drain 开始到慢任务结束的墙钟；追帧批次按 tick 数均摊）。
            # 此前只计 pipeline（不含 drain、事件合批与慢任务），与 D1-AC-07 的口径不一致（FX2-R2）；pipeline 自身另记 _pipe_us
            self._step_us.append((self.perf_ns() - t_iter0) / 1000.0 / n)
        if now - self._last_stats_ns >= PERF_PERIOD_NS:
            self._write_step_stats(now)
        self.stats["iters"] += 1
        if clk.state == TimeState.STEPPING:
            return 0.0
        if not clk.advancing:
            # 暂停：有被顺延的不可分片请求时不阻塞等待，下一轮立即续做（ADR-057 有界时延；AWR-10 §4.2）
            return 0.0 if self.slow.backlog or self._veh_jobs else IDLE_WAIT_S
        nd = clk.next_deadline_ns()
        if self.pair_ticks and clk.tick % 2 == 0 and clk.rate == 1.0 and not self.post_step_hooks:
            # 成对推进（ADR-070）：下一 tick 为奇数时等到其后的偶数 tick（L1 与 StateRing 发布所在的 tick）到期再一并推进，
            # 主循环按 125 Hz 唤醒。不提前 150 µs 醒来：提前醒来时偶数 tick 尚未到期，只会先单独推进奇数 tick
            return max(0.0, (nd + TICK_NS - self.wall_ns()) / 1e9)
        return max(0.0, (nd - self.wall_ns() - 150_000) / 1e9)

    def register_post_step_hook(self, fn: Callable[[int, int, int], Any]) -> Callable[[], None]:
        """锁步钩子（`--inproc` 与测试；M14-to-M08 第 5 条）：`fn(t_sim_ns, epoch, segment)` 在每个 tick 的 pipeline 结束、
        事件合批之前于主循环线程同步调用（步边界外，预算 ≤ 1 ms，超出只计入 `post_step_us`）。登记后主循环逐 tick 推进；
        返回注销函数。钩子异常只记日志，不中断仿真。"""
        self.post_step_hooks.append(fn)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self.post_step_hooks.remove(fn)

        return remove

    def _run_post_step_hooks(self) -> None:
        t0 = self.perf_ns()
        t, ep, sg = self.clock.t_ns, self.epoch, self.segment
        for fn in list(self.post_step_hooks):
            try:
                fn(t, ep, sg)
            except Exception:
                log.exception("post-step hook failed")
        self._post_step_us.append((self.perf_ns() - t0) / 1000.0)

    def run(self, should_stop: Callable[[], bool]) -> None:
        gate = self.idle_gate
        mono, perf = time.monotonic_ns, time.perf_counter_ns
        clk = self.clock
        cost = self.iter_cost_ns
        while not should_stop():
            k0 = clk.tick % 50
            t0 = perf()
            wait = self.iterate()
            dt = perf() - t0
            c = cost[k0]
            cost[k0] = dt if dt > c else c + (dt - c) * ITER_COST_DECAY
            if wait <= 0:
                continue
            if not clk.advancing:
                gate.open(mono() + int(wait * 1e9))
                self.inbox.wait(wait)  # 暂停：inbox 上带超时的阻塞等待（照写心跳，20 Hz 空转；有请求立即进入下一轮）
            else:
                w = min(wait, IDLE_WAIT_S)
                # 窗口截止 = 预计醒来时刻；余量 = 下一轮（相位 clk.tick % 50）可容许的醒来后等待（ADR-073 第 2 条）
                slack = max(0, BG_ITER_TARGET_NS - int(cost[clk.tick % 50])) if self.pair_ticks and clk.rate == 1.0 else 0
                gate.open(mono() + int(w * 1e9), slack)
                time.sleep(w)
            gate.close()

    def _drain(self) -> None:
        sp = self._sp_raw
        while sp:  # 流式 setpoint（32 B raw）：步顶写 mailbox（每机只保留最新值）
            self.on_setpoint(sp.popleft())
        if self.engine is not None and self.engine._batch_jobs:  # 批量命令的下一片（每次迭代一片，ADR-065）
            try:
                self.engine.batch_tick()
            except Exception:
                log.exception("batch admission failed")
        t_end = self.perf_ns() + DRAIN_BUDGET_NS
        for k in range(DRAIN_MAX):
            if k and self.perf_ns() >= t_end:
                self.stats["drain_deferred"] = self.stats.get("drain_deferred", 0) + 1
                return  # 余下的请求留到下一轮（ADR-073 第 4 条）
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
            if rep is not None:  # None：批量命令转入分片准入，由 engine.batch_tick() 在后续迭代中回复（ADR-065）
                req.reply_msg(rep)
        elif kind == "clock":
            req = item[1]
            req.reply_msg(self._clock_op(req.msg()))
        elif kind == "lease":
            req = item[1]
            req.reply_msg(self._lease_op(req.msg()))
        elif kind == "reset":  # request_reset（循环剧本，ADR-084）：步边界外执行，与 sim/reset 同一路径
            self.reset(reason=item[1], args=item[2])
        elif kind == "roster":
            # 预编码回复（ADR-074 第 1 条）：此前整份重建并编码，N = 1000 时约 6.5 ms 落在一次迭代内；约 160 KB 的 zenoh 回复
            # 在主循环内仍约 2.7 ms，交给空闲窗口发布线程（不在 sim-core 进程中时就地回复）
            req, rep = item[1], self.roster.snapshot_packed()
            if self.ext_pub_bg is not None:
                self.ext_pub_bg.submit(lambda: req.reply(rep))
            else:
                req.reply(rep)
        elif kind == "estimate":
            self._est_q.append((item[1], self.clock.wall_mono_ns()))
        elif kind == "query":
            if len(self._query_q) >= QUERY_Q_MAX:  # M08 §7.2：排队 > 8 个时 111（有界时延，ADR-057）
                with contextlib.suppress(Exception):
                    item[1].reply_msg({"v": 1, "code": int(Reason.RATE_LIMITED), "detail": {"queue": len(self._query_q)}})
                return
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
    def _principal_ok(self, msg: dict, *, need_seat: bool, roles: tuple[str, ...] = ("operator", "admin")) -> int:
        p = msg.get("principal")
        if not isinstance(p, dict):
            return int(Reason.ROLE_FORBIDDEN)
        if p.get("_internal"):
            return 0
        if self.k_entry is not None:
            sig = p.get("sig")
            if isinstance(sig, str):
                sig = sig.encode("latin-1")
            try:
                pr = Principal(str(p["principal_id"]), p["role"], p["entry"], p.get("conn_id"), bool(p.get("seat", False)))
            except (KeyError, TypeError):
                return int(Reason.ROLE_FORBIDDEN)
            if not isinstance(sig, (bytes, bytearray)) or not verify_principal(pr, str(msg.get("cid", "")), bytes(sig),
                                                                             self.k_entry):
                return int(Reason.ROLE_FORBIDDEN)
        if p.get("role") not in roles:
            return int(Reason.ROLE_FORBIDDEN)
        if need_seat and p.get("role") in ("operator", "admin") and not self.lease.is_seat_holder(str(p.get("principal_id"))):
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
        """`ctl/sim-core/lease`（17 §9.4 leaseOp；M08-FR-062；ADR-027；M14-to-M08 第 1 条）。

        - operator/admin：席位操作；acquire（OPERATOR）与 release 须持席位；
        - agent（`agent:<aid>`，entry agent-runtime，K_entry 验签，不要求席位）：只允许 acquire/release，且 owner 只能是
          AGENT；acquire 按优先级抢占 MISSION/SWARM 并入栈，OPERATOR 持有时 100；release `return_to: previous` 弹栈恢复
          （MISSION 续飞由 M10 在租约回到本任务时执行）；
        - 事件 `lease.*` 的 data 带 owner、holder、by（执行者 principal）。"""
        cid = str(msg.get("cid", ""))
        op = str(msg.get("op", ""))
        p = msg.get("principal") or {}
        agent = isinstance(p, dict) and p.get("role") == "agent"
        if agent:
            code = self._principal_ok(msg, need_seat=False, roles=("agent",))
            if not code and op not in ("acquire", "release"):
                code = int(Reason.ROLE_FORBIDDEN)
            if not code and str(msg.get("owner") or "AGENT") != "AGENT":
                code = int(Reason.ROLE_FORBIDDEN)
        else:
            code = self._principal_ok(msg, need_seat=op in ("acquire", "release"))
            if not code and op == "acquire" and str(msg.get("owner") or "OPERATOR") not in ("OPERATOR",) \
                    and not p.get("_internal"):
                code = int(Reason.ROLE_FORBIDDEN)
        lease = None
        pid = str(p.get("principal_id", "")) if isinstance(p, dict) else ""
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
                owner = int(Owner.AGENT) if agent else int(Owner.OPERATOR)
                if e is None:
                    code = int(Reason.NO_VEHICLE)
                elif op == "acquire":
                    code = self.lease.acquire(e.slot, owner, pid, uav=e.id, t_ns=self.clock.t_ns, emit=self.events.emit,
                                              by=pid)
                else:
                    code = self.lease.release(e.slot, pid, return_to=str(msg.get("return_to", "previous")), uav=e.id,
                                              t_ns=self.clock.t_ns, emit=self.events.emit, by=pid)
                if e is not None:
                    L = self.lease.lease_json(e.slot)
                    lease = {"uav": e.id, "owner": L["owner"], "holder": L["holder"], "priority": L["priority"],
                             "ttl_ms": None, "token": None}
                self.audit.write({"kind": f"lease.{op}", "t_sim_ns": self.clock.t_ns, "principal_id": pid,
                                  "role": p.get("role"), "cid": cid, "uav": msg.get("uav"), "code": int(code)})
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
        sl.add(SlowTask("vehicles", self._slow_vehicles, atomic=True, pending=lambda: bool(self._veh_jobs)))
        # 整块周期任务按估计耗时择机启动（fit；gc gen2 紧随 checkpoint 在同一轮执行，ADR-021 ③、ADR-070）
        sl.add(SlowTask("perf", lambda b: self._publish_perf(), period_wall_ns=PERF_PERIOD_NS, fit=True))
        # 不可分片任务按 ADR-057 的公平轮转与预算借贷启动（`pending` 为空队列时不计积分）
        sl.add(SlowTask("estimate", self._slow_estimate, atomic=True, pending=lambda: bool(self._est_q)))
        sl.add(SlowTask("query", self._slow_query, atomic=True, pending=lambda: bool(self._query_q)))
        sl.add(SlowTask("fine_check", self._slow_fine, atomic=True, pending=lambda: bool(self.engine.fine_queue)))
        sl.add(SlowTask("cleanup", lambda b: self.engine._expire_idem(self.clock.wall_mono_ns()), period_wall_ns=1_000_000_000,
                        fit=True))
        if self.checkpointer is None:
            sl.add(SlowTask("gc", self._slow_gc, period_wall_ns=GC_GEN2_PERIOD_NS, fit=True))
        if self.manual_gc:
            sl.add(SlowTask("gc_young", self._slow_gc_young))
        if self.checkpointer is not None:
            # gc gen2 并入 checkpoint 任务：先回收（上一代的元数据已冻结并由写线程释放，回收只扫本秒新增的存活对象），
            # 再拷贝，拷贝出的元数据随即冻结（不再被年轻代回收与下一次 gen2 遍历）；此前 gen2 紧随拷贝之后执行，
            # 要遍历刚建出的整代元数据（ADR-073 第 2 条）
            # 后台拷贝时按 250 ms 轮询（是否到期以上一代的拷贝时刻判定）：请求与拷贝之间有窗口等待，按请求时刻计周期会使
            # "到期 → 未到期跳过"交替，周期变成 2 s
            bg = getattr(self.checkpointer, "bg", None) is not None
            sl.add(SlowTask("checkpoint", self._slow_checkpoint, period_sim_ns=250_000_000 if bg else 1_000_000_000,
                            fit=True))
        if self.pin_aux_threads:
            from .cpuaff import AuxPinner

            self.aux_pinner = AuxPinner()
            bg = getattr(self.checkpointer, "bg", None)
            if bg is not None and bg.thread.native_id is not None:
                self.aux_pinner.keep.add(int(bg.thread.native_id))  # 后台拷贝留在主循环核（cpuaff.AuxPinner.keep）
            if self.aux_pinner.active:
                # 新线程继承创建者（多为主循环）的亲和性：周期重扫（cpuaff 模块文档，ADR-070）
                sl.add(SlowTask("cpuaff", lambda b: self.aux_pinner.scan(), period_wall_ns=AUX_PIN_PERIOD_NS, fit=True))
        for t in self.reg.slow:
            pw, ps = int((t.period_wall_s or 0) * 1e9), int((t.period_sim_s or 0) * 1e9)
            sl.add(SlowTask(t.name, self._plugin_task(t), pw, ps, owner="plugin", fit=bool(pw or ps)))

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
        args = m.get("args") or {}
        if op == "fleet/vehicles" and args.get("id") is None and len(self.roster.by_slot) > VEH_SYNC_MAX:
            # 全表查询转入分片作业（_slow_vehicles），完成时回复（ADR-074 第 2 条）
            self._veh_jobs.append({"req": req, "slots": self.roster.slots_in_order(), "pos": 0, "parts": [],
                                   "last": self._ext_rows_last, "t0": self.wall_ns()})
            return None
        local = self._local_query(op, args)
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
        """`fleet/vehicles` 查询（慢任务内同步执行）。state_ext 取最近一次 2 Hz 分片发布的行（≤ 0.5 s【墙钟】，与网关
        `uav/{id}/state_ext` 同一来源），缺失的机体（刚加入）现算；此前每次查询都为全部机体重算 state_ext（N = 1000
        约 50 ms，主循环停顿）。指定 id 时只现算该机。"""
        S = self.fleet.S
        out = []
        slots = self.roster.slots_in_order()
        if vid is not None:
            e = self.roster.resolve(vid)
            slots = [e.slot] if e is not None else []
        last = self._ext_rows_last if vid is None else {}
        miss = [s for s in slots if self.roster.by_slot[s].agent_no not in last]
        ext: dict[int, Any] = {}
        for s_ in slots:
            raw = last.get(self.roster.by_slot[s_].agent_no)
            if raw is not None:
                no, x = msgpack.unpackb(raw, raw=False, strict_map_key=False)
                ext[no] = x
        if miss:
            ext.update({no: x for no, x in self._ext_rows(miss)})
        for s in slots:
            e = self.roster.by_slot[s]
            sb = S.blocks["safety"]
            fs, sub = int(sb["fs"][s]), int(sb["sub"][s])
            out.append({"id": e.id, "agent_no": e.agent_no, "profile_id": e.profile_id, "model": e.model,
                        "backend": e.backend, "lifecycle": LIFECYCLE_NAMES[e.lifecycle],
                        "flight_state": SM.FS(fs).name, "sub": SM.SUB[SM.FS(fs)][sub] if sub < len(SM.SUB[SM.FS(fs)]) else None,
                        "pos_enu_m": [round(float(v), 3) for v in S.enu.pos[s]],
                        "home_enu_m": [round(float(v), 3) for v in e.home_enu_m], "state_ext": ext.get(e.agent_no)})
        return out

    def _ext_value_bytes(self, no: int, raw: bytes | None) -> bytes | None:
        """state_ext 行（`packb([agent_no, state_ext])`）中 state_ext 值的编码字节；行缺失或形态不符时返回 None。"""
        if raw is None or len(raw) < 2 or raw[0] != 0x92:
            return None
        pn = msgpack.packb(no, use_bin_type=True)
        if raw[1:1 + len(pn)] != pn:
            return None
        return raw[1 + len(pn):]

    def vehicle_items_packed(self, slots: list[int], last: dict[int, bytes]) -> list[bytes]:
        """`vehicle_items()` 各条目的 msgpack 编码（逐字节与 `packb(条目)` 相同，ADR-074 第 2 条）：按条目的键序拼接
        11 个键值对的编码，其中静态字段（id、agent_no、profile_id、model、backend 与 home_enu_m）按条目对象缓存，生命周期与
        (flight_state, sub) 按取值缓存，只有位置逐次编码；state_ext 直接拼接最近一次 2 Hz 发布的已编码行（不解码、不重编），
        缺失的机体现算。N = 1000 时缓存建立后逐机约 9 µs（此前构造字典、解码 state_ext 再整体编码约 48 µs）。"""
        S = self.fleet.S
        sb = S.blocks["safety"]
        by = self.roster.by_slot
        ents = [(s_, by.get(s_)) for s_ in slots]
        ents = [(s_, e) for s_, e in ents if e is not None]
        if not ents:
            return []
        sl = np.fromiter((x[0] for x in ents), np.int64, len(ents))
        fs_l, sub_l = sb["fs"][sl].tolist(), sb["sub"][sl].tolist()
        pos_l = S.enu.pos[sl].astype(np.float64).tolist()
        vals: dict[int, bytes] = {}
        miss = []
        for s_, e in ents:
            x = self._ext_value_bytes(e.agent_no, last.get(e.agent_no))
            if x is None:
                miss.append(s_)
            else:
                vals[e.agent_no] = x
        if miss:
            for no, x in self._ext_rows(miss):
                vals[no] = msgpack.packb(x, use_bin_type=True)
        packb = msgpack.packb
        st_c, lc_c, fs_c = self._veh_static, self._veh_lc, self._veh_fs
        out = []
        for (s_, e), fs, sub, P in zip(ents, fs_l, sub_l, pos_l, strict=True):
            c = st_c.get(s_)
            if c is None or c[0] is not e:
                head = packb({"id": e.id, "agent_no": e.agent_no, "profile_id": e.profile_id, "model": e.model,
                              "backend": e.backend}, use_bin_type=True)[1:]
                home = packb("home_enu_m", use_bin_type=True) + packb([round(float(v), 3) for v in e.home_enu_m],
                                                                      use_bin_type=True)
                c = st_c[s_] = (e, head, home)
            lb = lc_c.get(e.lifecycle)
            if lb is None:
                lb = lc_c[e.lifecycle] = packb("lifecycle", use_bin_type=True) + packb(LIFECYCLE_NAMES[e.lifecycle],
                                                                                       use_bin_type=True)
            fb = fs_c.get((fs, sub))
            if fb is None:
                F = SM.FS(fs)
                subs = SM.SUB[F]
                fb = fs_c[(fs, sub)] = (packb("flight_state", use_bin_type=True) + packb(F.name, use_bin_type=True)
                                        + packb("sub", use_bin_type=True)
                                        + packb(subs[sub] if sub < len(subs) else None, use_bin_type=True))
            pb = packb([round(P[0], 3), round(P[1], 3), round(P[2], 3)], use_bin_type=True)
            out.append(b"".join((b"\x8b", c[1], lb, fb, _POS_KEY, pb, c[2], _SE_KEY, vals.get(e.agent_no, b"\xc0"))))
        return out

    def _slow_vehicles(self, budget_us: float) -> Any:
        """fleet/vehicles 全表查询的分片作业（ADR-074 第 2 条）：按剩余预算与逐机耗时上包络编码一片（至少 VEH_SLICE_MIN
        架；作业存在超过 VEH_ESCALATE_NS 后至少 VEH_SLICE_MAX 架），全部完成后回复 `{v, code: 0, items}`（与同步构造的
        `packb` 逐字节相同，但各机来自相邻的若干 tick）。启动时机由 SlowTasks 的不可分片规则决定（公平轮转、积分与借贷、
        20 ms 饿死上界）。"""
        jobs = self._veh_jobs
        if not jobs:
            return False
        job = jobs[0]
        now = self.wall_ns()
        per = max(self._veh_us_per, VEH_US_FLOOR)
        lo = VEH_SLICE_MAX if now - job["t0"] >= VEH_ESCALATE_NS else VEH_SLICE_MIN
        k = max(lo, int(min(VEH_SLICE_MAX, budget_us / per)))
        chunk = job["slots"][job["pos"]:job["pos"] + k]
        t0 = self.perf_ns()
        job["parts"].extend(self.vehicle_items_packed(chunk, job["last"]))
        if chunk:
            meas = (self.perf_ns() - t0) / 1000.0 / len(chunk)
            per0 = self._veh_us_per
            self._veh_us_per = min(meas, 2.0 * per0) if meas > per0 else per0 + (meas - per0) * EXT_US_DECAY
        job["pos"] += len(chunk)
        if job["pos"] >= len(job["slots"]):
            jobs.popleft()
            pk = msgpack.Packer(use_bin_type=True)
            parts = job["parts"]
            head = b"".join((pk.pack_map_header(3), pk.pack("v"), pk.pack(1), pk.pack("code"), pk.pack(0), pk.pack("items"),
                             pk.pack_array_header(len(parts))))
            req = job["req"]

            def send() -> None:
                try:
                    req.reply(b"".join((head, *parts)))
                except Exception:
                    log.exception("fleet/vehicles reply failed")

            if self.ext_pub_bg is not None:
                self.ext_pub_bg.submit(send)  # 拼接与回复在空闲窗口内由发布线程完成（约 0.7 MB，主循环内约 9 ms）
            else:
                send()
        return None

    def _slow_fine(self, budget_us: float) -> Any:
        if not self.engine.fine_queue:
            return False
        self.engine.run_fine_checks(1)
        return None

    def _slow_gc_young(self, budget_us: float) -> Any:
        """年轻代回收（自动回收关闭后由此执行）：gen0 计数达到阈值、gen1 到期（每 GC1_EVERY 次 gen0）时，在本轮剩余预算
        放得下预计耗时（实测的指数平均）时执行；计数超过 GC0_FORCE 时不看预算强制执行，防止长时间高负载下无界增长。"""
        c0, c1, _c2 = gc.get_count()
        if c0 < GC0_MIN and c1 < GC1_EVERY:
            return False
        gen = 1 if c1 >= GC1_EVERY else 0
        forced = c0 >= GC0_FORCE or c1 >= GC1_FORCE
        if not forced and budget_us < self._gc_est_us[gen]:
            if gen == 1 and budget_us >= self._gc_est_us[0] and c0 >= GC0_MIN:
                gen = 0      # gen1 放不下时先做 gen0（gen1 顺延，计数到 GC1_FORCE 时强制）
            else:
                return False
        t0 = self.perf_ns()
        gc.collect(gen)
        d = (self.perf_ns() - t0) / 1000.0
        self._gc_est_us[gen] = max(0.8 * self._gc_est_us[gen] + 0.2 * d, d * 0.5)
        self._gc_young_ms.append(d / 1000.0)
        return None

    def _slow_checkpoint(self, budget_us: float) -> Any:
        """checkpoint（1 s【仿真】一代）与 gc gen2 合为一个整块周期任务（ADR-073 第 2 条）：gen2 → 拷贝 → 冻结。
        自动回收未关闭（进程内测试台）时只做拷贝。返回 False 表示本轮没有到期（不计耗时）。"""
        ck = self.checkpointer
        if self.clock.t_ns - ck.last_t < ck.period:
            return False
        if ck.bg is not None:
            # 后台拷贝（sim-core 进程，ADR-073 第 2 条）：只提出请求，由 _BgCapture 在主循环空闲窗口内持状态锁执行
            if ck.bg.pending:
                return False
            ck.bg.request()
            return None
        return self.checkpoint_capture()

    def checkpoint_capture(self) -> bool:
        """gen2（或只冻结）→ 拷贝 → 冻结（主循环慢任务内，进程内测试台）。"""
        if self.manual_gc:
            self._gen2_or_freeze()
        return self.checkpoint_capture_only()

    def gen2_or_freeze(self) -> None:
        """后台拷贝的第一步（持状态锁，ADR-073 第 2 条）。"""
        self._gen2_or_freeze()

    def checkpoint_capture_only(self) -> bool:
        """拷贝 → 冻结（主循环慢任务内，或后台拷贝的第二步持状态锁）。"""
        ok = self.checkpointer.maybe_save(self)
        if self.manual_gc:
            gc.freeze()  # 拷贝出的元数据（在途调用、幂等表等，交写线程只读）移入永久代：由引用计数在写完后释放
        return ok

    def _gen2_or_freeze(self) -> None:
        """gc gen2 + 冻结；上一次 gen2 的耗时（衰减估计）超过 GEN2_INLINE_US、或自上次以来准入的调用数达到
        GEN2_SKIP_ADMITTED 时本次只冻结（ADR-073 第 2 条）。

        gen2 只扫描上次冻结以来新增且存活的对象，耗时与这 1 s 内的对象增量成正比：稳态约 0.4–1 ms，全机 RTL、500 架
        link_drop 等风暴期间新建上千个调用与事件对象，一次 10–20 ms，直接越过单步最大 12 ms。风暴期间只冻结（存活对象
        移入永久代，此后由引用计数释放；其间形成又失效的引用环不再回收，只在风暴期间发生），估计值每次跳过衰减到 0.7 倍，
        数秒后重新尝试 gen2。"""
        adm = int(self.engine.stats.get("admitted", 0)) if self.engine is not None else 0
        burst = adm - self._gen2_adm0 >= GEN2_SKIP_ADMITTED
        self._gen2_adm0 = adm
        if (burst or self._gen2_est_us > GEN2_INLINE_US) and self._gen2_run_skips < GEN2_MAX_SKIPS:
            gc.freeze()
            gc.collect(2)  # 冻结后各代为空：只清零各代计数（数微秒），使年轻代的强制阈值不在风暴中途触发
            self._gen2_est_us *= 0.7
            self._gen2_skipped += 1
            self._gen2_run_skips += 1
            return
        self._gen2_run_skips = 0
        t0 = self.perf_ns()
        gc.collect(2)
        gc.freeze()
        d_us = (self.perf_ns() - t0) / 1000.0
        self._gc_ms.append(d_us / 1000.0)
        self._gen2_est_us = d_us

    def _slow_gc(self, budget_us: float) -> Any:
        """gc gen2 手动回收（D1-core 每 ≥ 30 s【墙钟】一次；启用 checkpoint 时紧随 checkpoint 拷贝，ADR-021 ③）。
        回收后冻结幸存者（`gc.freeze()`，与启动时同一手法）：下一次 gen2 只扫描这 30 s 内新产生且存活的对象，停顿与机群
        规模、运行时长无关（N = 1000 时全量 gen2 约 35 ms，主循环停顿；ADR-065）。冻结对象仍按引用计数释放，只是不再参与
        环检测。"""
        self._gen2_or_freeze()
        return None

    def _slow_state_ext(self, budget_us: float) -> Any:
        """`state_ext` 2 Hz【墙钟】分片打包（跨迭代完成后一次发布；ADR-051）。

        每片的机数按本轮剩余预算与实测的逐机耗时（含每片固定开销的均摊）自适应取值，预算放不下 EXT_SLICE_MIN 架时本轮
        跳过（让给预算宽裕的 tick），连续跳过超过 EXT_STARVE_NS【墙钟】时在剩余预算 ≥ EXT_FORCE_MIN_US 的轮次强制做一片
        最小片（超过 EXT_STARVE_HARD_NS 时不再挑轮，ADR-074 第 4 条）。各片的行在本片内即 msgpack
        编码（流式），周期结束时只拼接数组头与已编码的行（与对整个列表 `packb` 逐字节相同）：此前固定 16 架一片、末片
        一次打包 1000 行（约 11 ms），大机群时单步出现十余毫秒的尖峰（FX2-R2）。"""
        now = self.wall_ns()
        if self._ext_pos < 0:
            if now - self._ext_last < STATE_EXT_PERIOD_NS or not self.roster.by_slot:
                return False
            self._ext_last = now
            self._ext_order = self.roster.slots_in_order()
            self._ext_agents = []
            self._ext_packed = []
            self._ext_pos = 0
            self._ext_slice_wall = now
        if self._ext_pos >= len(self._ext_order):
            # 全部片已编码、发布顺延到本轮（上一片之后的剩余预算放不下发布）：预算不足时同样按 EXT_STARVE_NS 兜底
            if budget_us < self._ext_pub_us and now - self._ext_slice_wall < EXT_STARVE_NS:
                return False
            self._ext_publish()
            return None
        per = max(self._ext_us_per, EXT_US_FLOOR)
        k = int(min(EXT_SLICE_MAX, budget_us / per))
        if k < EXT_SLICE_MIN:
            starve = now - self._ext_slice_wall
            if starve < EXT_STARVE_NS or (budget_us < EXT_FORCE_MIN_US and starve < EXT_STARVE_HARD_NS):
                return False  # 未饿死，或已饿死但本轮偏重：让给之后的轻轮（ADR-074 第 4 条）
            k = EXT_SLICE_MIN
        chunk = self._ext_order[self._ext_pos:self._ext_pos + k]
        t0 = self.perf_ns()
        agents, packed = self._ext_rows_packed(chunk)  # 直接编码（与 `_ext_rows` 再整行 pack 解码后相同，ADR-070）
        self._ext_packed.extend(packed)
        self._ext_agents.extend(agents)
        if chunk:
            # 逐机耗时取上包络（实测更大时取实测、每次至多翻倍，更小时每片向实测靠拢 10%）：此前按 0.7/0.3 指数平均，
            # 缓存温热的轻片把估计压到实际值的一半以下，下一片按估计取满 48 架，实测 2.1–2.5 ms，落在剩余预算约 0.8 ms
            # 的轮次上使该轮单步约 3 ms（ADR-073 第 3 条）
            meas = (self.perf_ns() - t0) / 1000.0 / len(chunk)
            per0 = self._ext_us_per
            self._ext_us_per = min(meas, 2.0 * per0) if meas > per0 else per0 + (meas - per0) * EXT_US_DECAY
        self._ext_pos = min(self._ext_pos + k, len(self._ext_order))
        self._ext_slice_wall = now
        if self._ext_pos >= len(self._ext_order) and budget_us - (self.perf_ns() - t0) / 1000.0 >= self._ext_pub_us:
            self._ext_publish()  # 本轮剩余预算放得下发布；否则顺延到下一次调用（ADR-073 第 3 条）
        return None

    def _ext_publish(self) -> None:
        """发布已编码的各片。N = 1000 时载荷约 0.6 MB：api 与 recorder 订阅时 zenoh put 生产口径 4–7 ms，sim-core 进程中
        交给空闲窗口发布线程（bgpub），拼接也由该线程完成（ADR-074 第 3 条；此前主线程拼接 1–5 ms）。"""
        t0 = self.perf_ns()
        self._ext_pos = -1
        if self._pub_ext is None:
            self._pub_ext = self.bus.publisher(bus_keys.state_ext("sim-core"))
        head = self._ext_packer.pack_array_header(len(self._ext_packed))
        if self.ext_pub_bg is not None:
            # 拼接与发布都在主循环空闲窗口内由后台线程完成（bgpub.put_parts，ADR-074 第 3 条）：此前主线程拼接约 0.6 MB
            # 载荷，生产口径 1–5 ms
            self.ext_pub_bg.put_parts(self._pub_ext, head, self._ext_packed)
        else:
            self._pub_ext.put(head + b"".join(self._ext_packed))
        self._ext_rows_last = dict(zip(self._ext_agents, self._ext_packed, strict=True))
        self._ext_packed = []
        self._ext_agents = []
        meas = (self.perf_ns() - t0) / 1000.0
        pub0 = self._ext_pub_us
        self._ext_pub_us = min(meas, 2.0 * pub0) if meas > pub0 else pub0 + (meas - pub0) * EXT_US_DECAY

    def state_ext_items(self) -> list[list]:
        """`state/sim-core/ext` 的载荷：`[[agent_no, state_ext], ...]`（awr.uav.state_ext.v1）。"""
        return self._ext_rows(self.roster.slots_in_order())

    def _ext_rows(self, slots: list[int]) -> list[list]:
        """一片 state_ext 行（M08-FR-051；ADR-051 全机 2 Hz 分片编码）。逐机标量先按片向量化取出（`tolist()`），逐行只做
        字典装配；PX4 显示仿真按 (fs, sub) 记忆。此前逐行做 numpy 标量索引、范数与枚举转换，约 0.35 ms/架，1000 架 2 Hz
        合计约 0.7 核（D1 验收第 1 轮，ADR-051 估算为 20 µs/架）。字段与取值不变。"""
        S = self.fleet.S
        by = self.roster.by_slot
        ents = [(sl, by[sl]) for sl in slots if sl in by]
        if not ents:
            return []
        sl = np.fromiter((x[0] for x in ents), np.int64, len(ents))
        sb = S.blocks["safety"]
        bb = S.blocks.get("battery")
        fs = sb["fs"][sl].tolist()
        sub = sb["sub"][sl].tolist()
        acc = S.enu.acc[sl].tolist()
        if bb is not None and "soc" in bb:
            # 舍入按片向量化（np.round；Python round(x, n) 走十进制转换，每次约 1 µs，是逐行开销的主体）
            soc = np.round(bb["soc"][sl].astype(np.float64) * 100, 1).tolist()
            has_soc = (bb["battery_pct"][sl] != 255).tolist()
        else:
            soc, has_soc = None, None
        m08 = self._ext_m08_rows(sl, ents) if self._ext_extra else None
        lease_json = self.lease.lease_json
        out = []
        for k, (slot, e) in enumerate(ents):
            ext = {"lifecycle": LIFECYCLE_NAMES[e.lifecycle],
                   "lease": lease_json(slot),
                   "loc": {"status": "TRACKING", "gnss_fix": None, "sats": None},
                   "battery": None if soc is None or not has_soc[k] else {
                       "voltage_v": None, "current_a": None, "soc_pct": soc[k], "t_remain_s": None,
                       "wh_used": None},
                   "mission": None,
                   "accel_mps2": acc[k],
                   "home_enu_m": [float(v) for v in e.home_enu_m],
                   "frames": {"t_world_local": None},
                   "link": {"gcs_age_ms": None, "fcu_age_ms": 0},
                   "gcs_loss_policy": "ignore",
                   "px4": _px4_json(fs[k], sub[k])}
            if m08 is not None:
                ext.update(m08[k])
            out.append([e.agent_no, ext])
        # 登记的 state_ext 钩子（M13 GNSS、云台、IMU 等，M13-to-M08 第 2 条）：原地合并到本片各行
        ext_hooks = getattr(self.reg, "state_ext_hooks", None)
        if ext_hooks:
            objs = [r[1] for r in out]
            for _owner, hfn in ext_hooks:
                try:
                    hfn(sl, int(self.clock.t_ns), objs)
                except Exception:
                    log.exception("state_ext hook failed", extra={"kv": {"owner": _owner}})
        # INT-1（M09-to-M08 第 5 条）：M09 的电池、链路年龄与 gcs_loss_policy（SafetyHooks.state_ext_fields），缺失时保持上面的缺省
        hooks = getattr(self.reg, "hooks", None)
        fn = getattr(hooks, "state_ext_fields", None)
        if fn is not None:
            try:
                extra = fn(sl)
            except Exception:
                extra = {}
            if extra:
                for k, row in enumerate(out):
                    add = extra.get(int(sl[k]))
                    if add:
                        row[1].update({kk: v for kk, v in add.items() if kk in ("battery", "link", "gcs_loss_policy")})
        return out

    def _ext_rows_packed(self, slots: list[int]) -> tuple[list[int], list[bytes]]:
        """`_ext_rows` 的直接编码版本（state_ext 2 Hz 分片打包的热路径；FX2-R3，ADR-070）：不先拼整行字典再整体
        `msgpack.pack`，而是逐键拼接已编码的片段——机体不变的键（home、frames、mission、profile）与取值有限的键
        （lifecycle、lease、px4、ctrl、gcs_loss_policy）缓存编码结果，其余逐值编码。钩子（M13 的 loc 与 sens、M09 的
        battery、link 与 gcs_loss_policy）照常调用，合并规则与 `_ext_rows` 相同：解码后与 `[agent_no, _ext_rows 的字典]`
        逐键相等（`tests/sim/test_state_ext_packed.py`）。N = 1000 时每行约 36 µs → 约 18 µs。"""
        S = self.fleet.S
        by = self.roster.by_slot
        ents = [(sl, by[sl]) for sl in slots if sl in by]
        if not ents:
            return [], []
        sl = np.fromiter((x[0] for x in ents), np.int64, len(ents))
        sb = S.blocks["safety"]
        bb = S.blocks.get("battery")
        fs = sb["fs"][sl].tolist()
        sub = sb["sub"][sl].tolist()
        acc = S.enu.acc[sl].tolist()
        if bb is not None and "soc" in bb:
            soc = np.round(bb["soc"][sl].astype(np.float64) * 100, 1).tolist()
            has_soc = (bb["battery_pct"][sl] != 255).tolist()
        else:
            soc, has_soc = None, None
        pk = self._ext_packer.pack
        cache = self._ext_cache
        lg = getattr(self.lease, "gen", None)
        if cache.get("lease_gen") != lg or lg is None:
            cache["lease"] = {}
            cache["lease_gen"] = lg
        lease_c = cache["lease"]
        lease_json = self.lease.lease_json
        # 钩子：与 `_ext_rows` 同一初值（loc 的缺省三键），M13 原地合并 loc、加 sens；M09 给出 battery、link、gcs_loss_policy
        objs = [{"loc": {"status": "TRACKING", "gnss_fix": None, "sats": None}} for _ in ents]
        ext_hooks = getattr(self.reg, "state_ext_hooks", None)
        if ext_hooks:
            for _owner, hfn in ext_hooks:
                try:
                    hfn(sl, int(self.clock.t_ns), objs)
                except Exception:
                    log.exception("state_ext hook failed", extra={"kv": {"owner": _owner}})
        hooks = getattr(self.reg, "hooks", None)
        fn = getattr(hooks, "state_ext_fields", None)
        extra: dict = {}
        if fn is not None:
            try:
                extra = fn(sl) or {}
            except Exception:
                extra = {}
        m08 = self._ext_m08_parts(sl, ents) if self._ext_extra else None
        K = _EXT_KEYS
        out_a: list[int] = []
        out_b: list[bytes] = []
        for k, (slot, e) in enumerate(ents):
            o = objs[k]
            add = extra.get(int(sl[k])) or {}
            lc = e.lifecycle
            lb = lease_c.get(slot)
            if lb is None:
                lb = lease_c[slot] = pk(lease_json(slot))
            if "battery" in add:
                bat_b = pk(add["battery"])
            elif soc is None or not has_soc[k]:
                bat_b = _NIL
            else:
                bat_b = pk({"voltage_v": None, "current_a": None, "soc_pct": soc[k], "t_remain_s": None, "wh_used": None})
            link_b = pk(add["link"]) if "link" in add else _LINK_DEFAULT
            gp = add.get("gcs_loss_policy", "ignore") if "gcs_loss_policy" in add else "ignore"
            if len(o) > 2 or (len(o) == 2 and "sens" not in o):
                # 钩子写了 loc、sens 以外的键（可能覆盖基本键）：按 `_ext_rows` 的合并规则整行装配后编码（生产中不出现）
                out_a.append(e.agent_no)
                out_b.append(pk(self._ext_rows([slot])[0]))
                continue
            home_t = tuple(e.home_enu_m)
            hb = cache.get(("home", slot))
            if hb is None or hb[0] != home_t:
                hb = cache[("home", slot)] = (home_t, pk([float(v) for v in e.home_enu_m]))
            fsb = cache.get((fs[k], sub[k]))
            if fsb is None:
                fsb = cache[(fs[k], sub[k])] = pk(_px4_json(fs[k], sub[k]))
            parts = [K["lifecycle"], _lc_bytes(lc, pk), K["lease"], lb, K["loc"], pk(o["loc"]), K["battery"], bat_b,
                     K["mission"], _NIL, K["accel_mps2"], pk(acc[k]), K["home_enu_m"], hb[1], K["frames"], _FRAMES_B,
                     K["link"], link_b, K["gcs_loss_policy"], _str_bytes(gp, pk), K["px4"], fsb]
            nk = 11
            if m08 is not None:
                parts += m08[k]
                nk += 6
            if "sens" in o:
                parts += [K["sens"], pk(o["sens"])]
                nk += 1
            out_a.append(e.agent_no)
            out_b.append(_ARR2 + pk(e.agent_no) + self._ext_packer.pack_map_header(nk) + b"".join(parts))
        return out_a, out_b

    def _ext_m08_parts(self, sl: np.ndarray, ents: list) -> list[list[bytes]]:
        """`_ext_m08_rows` 的直接编码版本：每行六个键的 [键, 值, ...] 片段（同一取值与舍入）。"""
        from ..fleet.state import CtrlMode

        S = self.fleet.S
        pk = self._ext_packer.pack
        mode = S.ctrl_mode[sl]
        nav = _NAV_LUT[mode].tolist()
        pe = np.round(np.sqrt(((S.enu.pos_ref[sl] - S.enu.pos[sl]) ** 2).sum(1)), 3).tolist()
        wr = np.round(S.enu.vel[sl] - S.enu.wind(sl), 3).tolist()
        q = S.enu.q_xyzw[sl]
        tilt = np.round(np.degrees(np.arccos(np.clip(1.0 - 2.0 * (q[:, 0] * q[:, 0] + q[:, 1] * q[:, 1]), -1.0, 1.0))),
                        3).tolist()
        thr = np.round(S.thrust[sl].astype(np.float64), 4).tolist()
        mode_l = mode.tolist()
        phase = S.ctrl_phase[sl].tolist()
        cache = self._ext_cache
        K = _EXT_KEYS
        out = []
        for k, (_slot, e) in enumerate(ents):
            pb = cache.get(("profile", e.profile_id))
            if pb is None:
                p = self.T.get(e.profile_id)
                pb = cache[("profile", e.profile_id)] = pk({"id": p.profile_id, "version": p.version, "status": p.status})
            cb = cache.get(("ctrl", mode_l[k], phase[k]))
            if cb is None:
                cb = cache[("ctrl", mode_l[k], phase[k])] = pk({"mode": _ctrl_name(CtrlMode, mode_l[k]), "phase": phase[k]})
            out.append([K["profile"], pb, K["ctrl"], cb, K["thrust_frac"], pk(thr[k]), K["tilt_deg"], pk(tilt[k]),
                        K["pos_err_m"], pk(pe[k]) if nav[k] else _NIL, K["wind_rel_mps"], pk(wr[k])])
        return out

    def _ext_m08_rows(self, sl: np.ndarray, ents: list) -> list[dict]:
        """M08-FR-051 的附加字段（契约登记后输出），按片向量化。"""
        from ..fleet.state import CtrlMode

        S = self.fleet.S
        mode = S.ctrl_mode[sl]
        nav = _NAV_LUT[mode].tolist()
        pe = np.round(np.sqrt(((S.enu.pos_ref[sl] - S.enu.pos[sl]) ** 2).sum(1)), 3).tolist()
        wr = np.round(S.enu.vel[sl] - S.enu.wind(sl), 3).tolist()
        q = S.enu.q_xyzw[sl]
        tilt = np.round(np.degrees(np.arccos(np.clip(1.0 - 2.0 * (q[:, 0] * q[:, 0] + q[:, 1] * q[:, 1]), -1.0, 1.0))),
                        3).tolist()
        thr = np.round(S.thrust[sl].astype(np.float64), 4).tolist()
        mode_l = mode.tolist()
        phase = S.ctrl_phase[sl].tolist()
        prof: dict[str, dict] = {}
        out = []
        for k, (_slot, e) in enumerate(ents):
            pj = prof.get(e.profile_id)
            if pj is None:
                p = self.T.get(e.profile_id)
                pj = prof[e.profile_id] = {"id": p.profile_id, "version": p.version, "status": p.status}
            out.append({"profile": pj, "ctrl": {"mode": _ctrl_name(CtrlMode, mode_l[k]), "phase": phase[k]},
                        "thrust_frac": thr[k], "tilt_deg": tilt[k], "pos_err_m": pe[k] if nav[k] else None,
                        "wind_rel_mps": wr[k]})
        return out

    def _ext_m08(self, slot: int, e) -> dict:
        """M08-FR-051 的附加字段（单机；与 `_ext_m08_rows` 相同）。"""
        return self._ext_m08_rows(np.array([slot], np.int64), [(slot, e)])[0]

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
        slow_ms = {k: round(v / dt_sim_s, 3) for k, v in self.slow.busy_ms().items()}
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
               "gc_gen2_ms_max": round(max(self._gc_ms), 3) if self._gc_ms else None, "gc_gen2_skipped": self._gen2_skipped,
               "ckpt_bg": dict(self.checkpointer.bg.stats) if self.checkpointer is not None and self.checkpointer.bg is not None
               else None,
               "ext_pub_bg": dict(self.ext_pub_bg.stats) if self.ext_pub_bg is not None else None,
               # M13-to-M08 第 5 条：慢任务按名计时；ADR-057 借贷与顺延诊断
               "slow_ms_per_s": slow_ms, "slow_debt_us": round(self.slow.debt_us, 1), "slow_deferred": self.slow.deferred,
               "slow_defer_max": {t.name: t.defer_max for t in self.slow.tasks if t.atomic and t.defer_max}}
        if self.geo is not None:
            msg["geo"] = self.geo.metrics()
        return msg

    def _write_step_stats(self, now: int) -> None:
        a = np.asarray(self._step_us, np.float64) if self._step_us else np.zeros(1)
        t0w, t0s = self._rtf_t0
        if t0w:
            dw = now - t0w
            # 时钟后退（sim/reset、checkpoint 恢复）时不得为负：此前重置后的第一次 1 Hz 统计算出负值，写 u32 抛 OverflowError
            # 使主循环崩溃（循环剧本每轮重置都会触发，DEMO-PUBLIC 发现，ADR-084）
            self._rtf_milli = max(0, round(1000 * (self.clock.t_ns - t0s) / dw)) if dw > 0 else 1000
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
        if self.ext_pub_bg is not None:
            with contextlib.suppress(Exception):
                self.ext_pub_bg.close()
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
        # start() 的 gc.freeze() 把当时存活的对象（含本 SimCore 自身的环）移入永久代；停止后解冻，使其随后可被回收。
        # sim-core 进程中 stop 之后即退出，不受影响；同一进程内反复启停（--inproc、测试）每次约泄漏 3.6 MB。
        gc.unfreeze()
        if self.manual_gc:
            gc.enable()


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


STANDBY_READY_LINE = "AWR_STANDBY_READY"  # 与 supervisor `STANDBY_READY` 一致（AWR-19 §4.2，ADR-070）


def _standby_preload() -> dict[str, Any]:
    """热备用进程的预热：插件装配（导入即登记，含各插件的 numba 预热）、M08 L1 核、机型表、插件的 `standby_warm()`（M10：
    剧本校验器与生成器模块）。都与
    运行状态无关（世界、剧本、StateRing、总线在接替之后才打开），接替后由 SimCore 复用（compose_plugins 对已导入模块不重复
    执行，机型表经 `profiles=` 传入）。冷启动 sim-core 约 2 s，其中这些约 1.5 s（FX2-R3-sim 自测，D1-AC-11b）。"""
    cfg = SimConfig.from_env()
    defer_numba_blas_probe()
    compose_plugins(cfg.plugins)
    out: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        if KL.HAVE_NUMBA and cfg.fleet.kernel != "numpy":
            KL.warmup()
    with contextlib.suppress(ProfileError):  # 无效时不预载：接替后按冷启动路径重新构造并按 353 处理
        out["profiles"] = ProfileTable()
    for name in cfg.plugins:  # 插件自带的预热钩子（例如 M10 的剧本校验器；按名称调用，不越过组合根的导入边界）
        fn = getattr(sys.modules.get(name), "standby_warm", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                log.exception("plugin standby warm failed", extra={"kv": {"plugin": name}})
    return out


def _standby_wait() -> dict[str, Any] | None:
    """`AWR_STANDBY=1`（supervisor 的热备用进程，AWR-19 §4.2，ADR-070）：预热后打印 `AWR_STANDBY_READY`，阻塞读 stdin 的
    一行 JSON `{"env": {...}}`（接替时 supervisor 写入的当前子进程环境）；读到 EOF（supervisor 停止备用进程或已退出）时
    返回 None。等待期间设 PDEATHSIG，supervisor 退出时随之退出。"""
    with contextlib.suppress(Exception):
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, int(signal.SIGTERM), 0, 0, 0)
    sup = os.environ.get("AWR_SUPERVISOR_PID")
    if sup and sup.isdigit() and os.getppid() != int(sup):
        return None
    pre = _standby_preload()
    sys.stdout.write(STANDBY_READY_LINE + "\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line.strip():
        return None
    try:
        env = json.loads(line).get("env") or {}
    except (ValueError, AttributeError):
        return None
    os.environ.update({str(k): str(v) for k, v in env.items()})
    os.environ.pop("AWR_STANDBY", None)
    return pre


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m awr.sim.runtime")
    ap.add_argument("--resim", default=None, help="runs/<run>：按输入日志批处理重仿真（D1-ext，M08-FR-084）")
    ap.add_argument("--out", default=None, help="重仿真输出的 Full64 帧摘要路径")
    a = ap.parse_args(argv)
    if a.resim:
        from .resim import resim_main

        return resim_main(Path(a.resim), Path(a.out) if a.out else None)

    pre: dict[str, Any] = {}
    if os.environ.get("AWR_STANDBY") == "1":
        got = _standby_wait()
        if got is None:
            return 0
        pre = got

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
                           persist_dir=ctx.persist_dir if ctx.supervised else None, profiles=pre.get("profiles"))
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
        core.manual_gc = os.environ.get("AWR_SIM_MANUAL_GC", "1") != "0"
        core.pin_aux_threads = os.environ.get("AWR_SIM_PIN_AUX", "1") != "0"
        core.pair_ticks = os.environ.get("AWR_SIM_PAIR_TICKS", "1") != "0"
        if core.checkpointer is not None and os.environ.get("AWR_SIM_CKPT_BG", "1") != "0":
            core.checkpointer.enable_background(core, core.idle_gate)  # 后台拷贝（ADR-073 第 2 条）
        if os.environ.get("AWR_SIM_EXT_BG", "1") != "0":
            from .bgpub import GatedPublisher

            core.ext_pub_bg = GatedPublisher(core.idle_gate)  # state_ext 在空闲窗口内发布（ADR-073 第 3 条）；roster 与 fleet/vehicles
            #                                                    的大回复同此（ADR-074 第 1、2 条）
        sys.setswitchinterval(SWITCH_INTERVAL_S)
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
