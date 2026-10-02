"""stage `tap`（order 140，every 2，125 Hz【仿真】）：NED/FRD → ENU/FLU 换算后写 StateRing 槽（M08-FR-006、FR-050）。

行序按 slot 升序（同一 `roster_version` 内固定，Gateway 据此缓存 agent_no → 行号，17 §9.2）；Full64 字段：
`flight_state`、`flags`、`ctrl` 由 `core/fuser.py` 从 safety 块与租约合成，`mission_item` 取 mission 块，`battery_pct` 取
battery 块，位置、速度、姿态、角速度取 ENU 只读视图（与 M02 `ned_frd_to_enu_flu_batch` 同源）；Lite32 由契约生成物
`lite_from_full` 量化。写入用零拷贝 `begin_publish()/commit_publish()`。快进时按墙钟间隔 ≥ 4 ms 节流（≤ 250 Hz），
同一批内只在最后一次 tap 调用发布，并置 SlotHeader `flags.FASTFWD`；单步（C04）结束于非 tap tick 时由主循环补发一次。
tick 末清零 `mode_evt`。
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
_FLAG_FIELDS = ("flag_loc_ok", "flag_failsafe", "flag_gcs", "flag_fcu", "flag_loc_deg", "flag_alert", "locked")
_EMPTY_U16 = np.zeros(0, np.uint16)
_EMPTY_U8 = np.zeros(0, np.uint8)


def _have_kernel() -> bool:
    from ..kernels_l1 import HAVE_NUMBA

    return bool(HAVE_NUMBA)


class TapStage:
    def __init__(self, ring: Any, *, lease_owner: Callable[[], np.ndarray], roster_version: Callable[[], int],
                 wall_ns: Callable[[], int] | None = None, every: int = 2) -> None:
        self.ring = ring
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

    def __call__(self, S: FleetState, ctx: StageCtx) -> None:
        try:
            wall = self.wall_ns() if self.wall_ns is not None else 0
            fastfwd = ctx.clock is not None and getattr(ctx.clock, "rate", 1.0) > 1.0
            # 快进：墙钟间隔 ≥ 4 ms 才发布；同一批内稍后还有 tap 调用时推迟到批内最后一次（发布最新状态）
            if fastfwd and (wall - self.last_pub_wall < MIN_PUB_NS or ctx.batch_remaining >= self.every):
                self.skipped += 1
                return
            idx = np.flatnonzero(S.active).astype(np.int32)
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
        finally:
            S.mode_evt[:] = 0
