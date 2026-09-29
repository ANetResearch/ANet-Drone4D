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

    def fill(self, S: FleetState, full: np.ndarray, lite: np.ndarray, idx: np.ndarray) -> int:
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
                    n = self.fill(S, fv, lv, idx)
                except BaseException:
                    self.ring._abort(ticket)
                    raise
                self.ring.commit_publish(ticket, n, S.t_ns, self.roster_version(), flags)
            self.last_pub_wall = wall
            self.published += 1
        finally:
            S.mode_evt[:] = 0
