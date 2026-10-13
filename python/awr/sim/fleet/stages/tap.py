"""stage `tap`（order 140，every 2，125 Hz【仿真】）：NED/FRD → ENU/FLU 换算后写 StateRing 槽（M08-FR-006、FR-050）。

行序按 slot 升序（同一 `roster_version` 内固定，Gateway 据此缓存 agent_no → 行号，17 §9.2）；Full64 字段：
`flight_state`、`flags`、`ctrl` 由 `core/fuser.py` 从 safety 块与租约合成，`mission_item` 取 mission 块，`battery_pct` 取
battery 块，位置、速度、姿态、角速度取 ENU 只读视图（与 M02 `ned_frd_to_enu_flu_batch` 同源）；Lite32 由契约生成物
`lite_from_full` 量化。写入用零拷贝 `begin_publish()/commit_publish()`。快进时按墙钟间隔 ≥ 4 ms 节流（≤ 250 Hz），
同一批内只在最后一次 tap 调用发布，并置 SlotHeader `flags.FASTFWD`；单步（C04）结束于非 tap tick 时由主循环补发一次。
tick 末清零 `mode_evt`。

D2 空载优化（M08-FR-223；只在 `idle_opt` 的时钟档启用，即 sandbox100，d1_250 行为不变）：①发布频率由按档调度表给出（50 Hz）；
②活动机体为 0 且上一帧也为 0、roster_version 未变时不发布（头部时钟组由主循环心跳照写）；③读者游标表连续 2 s【墙钟】
没有活动读者（已登记且 2 s 内读过帧）时降为 5 Hz，出现活动读者后下一次 tap 恢复全速；④换算视图预计算（`_views` 缓存）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32, lite_from_full
from awr.runtime.statering import SLOT_FASTFWD

from ...core.fuser import ctrl_bytes, flags_bytes, fs_bytes

if TYPE_CHECKING:
    from ..pipeline import StageCtx
    from ..state import FleetState

__all__ = ["TapStage"]

MIN_PUB_NS = 4_000_000
IDLE_AFTER_NS = 2_000_000_000  # 无活动读者达此墙钟时长即降频（M08 §15.4.4）
IDLE_PUB_NS = 200_000_000  # 降频后的发布间隔【墙钟】（5 Hz）
READER_FRESH_NS = 2_000_000_000  # 读者心跳新鲜度（与 LOSSLESS_STALE_NS 同口径）
_FLAG_FIELDS = ("flag_loc_ok", "flag_failsafe", "flag_gcs", "flag_fcu", "flag_loc_deg", "flag_alert", "locked")
_EMPTY_U16 = np.zeros(0, np.uint16)
_EMPTY_U8 = np.zeros(0, np.uint8)


def _have_kernel() -> bool:
    from ..kernels_l1 import HAVE_NUMBA

    return bool(HAVE_NUMBA)


class TapStage:
    def __init__(self, ring: Any, *, lease_owner: Callable[[], np.ndarray], roster_version: Callable[[], int],
                 wall_ns: Callable[[], int] | None = None, every: int = 2, idle_opt: bool = False) -> None:
        self.ring = ring
        self.idle_opt = bool(idle_opt)
        self.enabled = True  # 批处理自由运行时关闭（M08-FR-221：不写 StateRing 槽）
        self._last_n = -1
        self._last_rv = -1
        self._reader_seen_wall = 0  # 最近一次看到活动读者的墙钟时刻
        self.idle = False
        self.skipped_empty = 0
        self.skipped_idle = 0
        # 读者心跳的时基（CLOCK_MONOTONIC，跨进程可比）由运行时注入（本模块不读墙钟，ADR-049）；None 时视为有活动读者
        self.mono_ns: Callable[[], int] | None = None
        self.every = int(every)
        self.lease_owner = lease_owner
        self.roster_version = roster_version
        self.wall_ns = wall_ns
        self.last_pub_wall = 0
        self.published = 0
        self.skipped = 0
        self.last_full: np.ndarray = np.zeros(0, DRONE_STATE64)
        self.last_lite: np.ndarray = np.zeros(0, SWARM_LITE32)
        self.use_kernel = _have_kernel()
        self._ok_key: tuple | None = None
        self._ok = False
        self._view_cache: dict[tuple, tuple] = {}

    def fill(self, S: FleetState, full: np.ndarray, lite: np.ndarray, idx: np.ndarray, view_key: Any = None) -> int:
        """写 Full64 与 Lite32 行。numba 可用且状态块为契约 dtype 时走融合核 `kernels_tap.tap_fill`（一次遍历写完全部字段，
        N = 1000 约 30 µs；此前 numpy 逐字段路径约 0.73 ms，M08-AC-033 要求发布 p99 ≤ 300 µs），否则走 numpy 路径（两者
        逐位相同，`tests/sim/test_tap.py`）。"""
        if self.use_kernel and idx.size >= 2 and self._kernel_ok(S):
            # n = 1 时结构化数组的字段视图同时是 C 连续的，numba 会按 'C' 布局另编一个签名（预热覆盖的是 'A'），在主循环内
            # 编译数秒（D1-AC-15 的崩溃循环同类问题）；单行走 numpy 路径（两者逐位相同）
            return self._fill_kernel(S, full, lite, idx, view_key)
        return self.fill_numpy(S, full, lite, idx)

    def _kernel_ok(self, S: FleetState) -> bool:
        sb = S.blocks.get("safety")
        key = (id(S), id(sb))
        if self._ok_key != key:
            ok = sb is not None and all(f in sb and sb[f].dtype == np.bool_ for f in _FLAG_FIELDS) \
                and sb["fs"].dtype == np.uint8 and sb["sub"].dtype == np.uint8
            mb, bb = S.blocks.get("mission"), S.blocks.get("battery")
            ok = ok and (mb is None or mb["mission_item"].dtype == np.uint16) and \
                (bb is None or bb["battery_pct"].dtype == np.uint8)
            self._ok_key, self._ok = key, bool(ok)
        return self._ok

    def _views(self, full: np.ndarray, lite: np.ndarray, n: int, view_key: Any = None) -> tuple:
        # 环槽数组是 StateRing 持有的常驻对象：按 (对象, 行数) 缓存字段视图（条目持有数组引用并核对同一对象）。此前以
        # `__array_interface__` 取数据地址作键，结构化 dtype 每次都要生成整个字段描述，约 80 µs/次（125 Hz，FX2-R3）；
        # 无环（测试，每次新建数组）时仍按数据地址
        if view_key is not None:
            key = (id(full), id(lite), n)
            ent = self._view_cache.get(key)
            if ent is not None and ent[0] is full and ent[1] is lite:
                return ent[2]
        else:
            key = (full.__array_interface__["data"][0], lite.__array_interface__["data"][0], n)
            ent = self._view_cache.get(key)
            if ent is not None:
                return ent[2]
        f, lt = full[:n], lite[:n]
        v = tuple(f[k] for k in ("agent_no", "flight_state", "flags", "mission_item", "battery_pct", "ctrl", "pos",
                                 "vel", "q", "omega", "dt_us")) + \
            tuple(lt[k] for k in ("agent_no", "flight_state", "battery_pct", "pos", "q_snorm", "vel_cms", "flags",
                                  "ctrl"))
        if len(self._view_cache) > 256:
            self._view_cache.clear()
        self._view_cache[key] = (full, lite, v) if view_key is not None else (None, None, v)
        return v

    def _fill_kernel(self, S: FleetState, full: np.ndarray, lite: np.ndarray, idx: np.ndarray, view_key: Any = None) -> int:
        from ...core.fuser import _INIT_NATIVE, _LAND_NATIVE, _UNARMED
        from ..kernels_tap import tap_fill

        n = idx.size
        sb = S.blocks["safety"]
        mb, bb = S.blocks.get("mission"), S.blocks.get("battery")
        lo = self.lease_owner()
        tap_fill(idx, S.agent_no, sb["fs"], sb["sub"], S.in_air, S.fidelity, lo, sb["flag_loc_ok"], sb["flag_failsafe"],
                 sb["flag_gcs"], sb["flag_fcu"], sb["flag_loc_deg"], sb["flag_alert"], sb["locked"],
                 mb["mission_item"] if mb is not None else _EMPTY_U16, bb["battery_pct"] if bb is not None else _EMPTY_U8,
                 S.p, S.v, S.q, S.omega, _UNARMED, _INIT_NATIVE, _LAND_NATIVE, *self._views(full, lite, n, view_key))
        return n

    def fill_numpy(self, S: FleetState, full: np.ndarray, lite: np.ndarray, idx: np.ndarray) -> int:
        n = idx.size
        f = full[:n]
        enu = S.enu
        f["agent_no"] = S.agent_no[idx]
        f["flight_state"] = fs_bytes(S, idx)
        f["flags"] = flags_bytes(S, idx)
        mb = S.blocks.get("mission")
        f["mission_item"] = mb["mission_item"][idx] if mb is not None else 0xFFFF
        bb = S.blocks.get("battery")
        f["battery_pct"] = bb["battery_pct"][idx] if bb is not None else 255
        f["ctrl"] = ctrl_bytes(S, idx, self.lease_owner())
        f["pos"] = enu.pos[idx]
        f["vel"] = enu.vel[idx]
        f["q"] = enu.q_xyzw[idx]
        f["omega"] = enu.omega_flu[idx]
        f["dt_us"] = 0
        lite_from_full(f, out=lite[:n])
        return n

    def _readers_active(self, wall: int) -> bool:
        """读者游标表中是否有活动读者（已登记、心跳在 READER_FRESH_NS 内；心跳为 CLOCK_MONOTONIC，跨进程可比）。"""
        ring = self.ring
        if self.mono_ns is None:
            return True
        cur = getattr(ring, "_cur", None)
        now = self.mono_ns()
        if cur is not None:
            try:
                return bool(np.any((cur["mode"] != 0) & (now - cur["heartbeat_ns"].astype(np.int64) < READER_FRESH_NS)))
            except Exception:
                pass
        fn = getattr(ring, "reader_cursors", None)
        if fn is None:
            return True
        return any(c["mode"] != 0 and now - int(c["heartbeat_ns"]) < READER_FRESH_NS for c in fn())

    def _idle_skip(self, S: FleetState, wall: int, n_active: int) -> bool:
        """M08-FR-223 ②③：返回本次是否不发布。"""
        rv = self.roster_version()
        if n_active == 0 and self._last_n == 0 and rv == self._last_rv:
            self.skipped_empty += 1
            return True
        if self.ring is None:
            return False
        if self._readers_active(wall):
            self._reader_seen_wall = wall
            self.idle = False
            return False
        if self._reader_seen_wall == 0:
            self._reader_seen_wall = wall
        if wall - self._reader_seen_wall >= IDLE_AFTER_NS:
            self.idle = True
        if self.idle and wall - self.last_pub_wall < IDLE_PUB_NS and rv == self._last_rv:
            self.skipped_idle += 1
            return True
        return False

    def __call__(self, S: FleetState, ctx: StageCtx) -> None:
        try:
            if not self.enabled:
                return
            wall = self.wall_ns() if self.wall_ns is not None else 0
            fastfwd = ctx.clock is not None and getattr(ctx.clock, "rate", 1.0) > 1.0
            # 快进：墙钟间隔 ≥ 4 ms 才发布；同一批内稍后还有 tap 调用时推迟到批内最后一次（发布最新状态）
            if fastfwd and (wall - self.last_pub_wall < MIN_PUB_NS or ctx.batch_remaining >= self.every):
                self.skipped += 1
                return
            idx = np.flatnonzero(S.active).astype(np.int32)
            if self.idle_opt and self._idle_skip(S, wall, idx.size):
                return
            flags = SLOT_FASTFWD if fastfwd else 0
            if self.ring is None:
                full = np.zeros(idx.size, DRONE_STATE64)
                lite = np.zeros(idx.size, SWARM_LITE32)
                self.fill(S, full, lite, idx)
                self.last_full, self.last_lite = full, lite
            else:
                fv, lv, ticket = self.ring.begin_publish()
                try:
                    n = self.fill(S, fv, lv, idx, view_key=ticket.slot)
                except BaseException:
                    self.ring._abort(ticket)
                    raise
                self.ring.commit_publish(ticket, n, S.t_ns, self.roster_version(), flags)
            self.last_pub_wall = wall
            self.published += 1
            self._last_n = idx.size
            self._last_rv = self.roster_version()
        finally:
            S.mode_evt[:] = 0
