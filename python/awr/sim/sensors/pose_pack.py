"""SensorPosePacker：兴趣集 并 标记集的 SensorPose48 打包与发布（M13-FR-013；M13 §6.5.3；17 §6.5、§9.3）。

慢任务 `m13.sensor_pose`（M08 `register_slow_task`，自计时）：仿真时间每跨过一个 100 ms 网格点开始一轮，取当时的
`ctx.interest`（detail 并 marks ≤ 80 架）中已装配、带视场传感器的 slot，按 slot 升序每次执行一片（≤ 16 架），打成
`awr.SensorPose48.v1` 行后立即发布到 `state/sim-core/sensor`（msgpack `{v: 1, t_sim_ns, rows: bin(n·48)}`，行内含 agent_no、
sensor_no；每条消息的 t_sim_ns 为该片的打包时刻）。D1 只打包 camera、thermal 两列（LiDAR 列从 V0.2 起）。
flags：bit0 ACTIVE = `act` 位；bit1 FOV_VALID = ACTIVE 且状态 ∈ {ACTIVE, DEGRADED} 且内参有效（提请的 bit2 GIMBAL_LIMIT、
bit3 DEGRADED 在 17 登记前不置位）。

位姿来源可替换（`pose_source`，M13-FR-004 的 SensorBackend 测试替身沿用同一打包路径，M13-AC-004）。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from awr.contracts.layouts import SENSOR_POSE48

from .enums import SensorKind, SensorState
from .frames import R_to_quat_xyzw, gimbal_R, quat_to_R
from .gimbal import COL_OF_KIND

if TYPE_CHECKING:
    from .runtime import SensorRuntime

__all__ = ["FLAG_ACTIVE", "FLAG_FOV_VALID", "PERIOD_NS", "SLICE", "SensorPosePacker", "decode_rows"]

PERIOD_NS = 100_000_000
SLICE = 16
FLAG_ACTIVE = 1
FLAG_FOV_VALID = 2
COLS = (SensorKind.CAMERA, SensorKind.THERMAL)

PoseSource = Callable[[np.ndarray, Any, np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]


def default_pose_source(PQ: np.ndarray, spec: Any, az: np.ndarray, el: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """机体位姿 (n,7) × T_base_mount × R_gimbal -> (pos (n,3), R_ws (n,3,3))。"""
    Rb = quat_to_R(PQ[:, 3:7])
    Rs = Rb @ spec.mount_R
    if spec.gimbal is not None:
        Rs = Rs @ gimbal_R(az, el)
    pos = PQ[:, :3] + Rb @ spec.mount_t
    return pos, Rs


def decode_rows(rows: bytes | np.ndarray) -> np.ndarray:
    return np.frombuffer(bytes(rows), SENSOR_POSE48)


class SensorPosePacker:
    def __init__(self, rt: SensorRuntime) -> None:
        self.rt = rt
        self.out = np.zeros(SLICE * len(COLS), SENSOR_POSE48)
        self.queue = np.zeros(0, np.int64)
        self.pos = 0
        self.next_due = 0
        self.round_t_ns = 0
        self.pose_source: PoseSource = default_pose_source
        self._pub = None
        self._bus = None
        self.last_payload: bytes | None = None
        self.sink: Callable[[bytes], None] | None = None  # 测试钩子：替代总线发布
        self.stats = {"rounds": 0, "slices": 0, "rows": 0, "puts": 0}

    # ------------------------------------------------------------ 打包
    def pack(self, slots: np.ndarray, out: np.ndarray) -> int:
        rt = self.rt
        S, b = rt.S, rt.blk
        slots = np.asarray(slots, np.int64)
        if slots.size == 0:
            return 0
        PQ = S.enu.pose_enu_flu(slots)
        good = np.all(np.isfinite(PQ), axis=1)  # 姿态含 NaN 的 slot 本轮跳过（M13 §7.5）
        row = 0
        for kind in COLS:
            k = COL_OF_KIND[kind]
            for rig_i, sel in rt.group_by_rig(slots):
                spec = rt.rig_list[rig_i].by_kind(kind)
                if spec is None:
                    continue
                ss = slots[sel]
                m = ((b["has"][ss] & spec.bit) != 0) & good[sel]
                if not m.any():
                    continue
                ss = ss[m]
                n = ss.size
                pos, Rs = self.pose_source(PQ[sel][m], spec, b["g_az"][ss, k], b["g_el"][ss, k])
                sl = slice(row, row + n)
                out["agent_no"][sl] = S.agent_no[ss]
                out["sensor_no"][sl] = spec.sensor_no
                out["kind"][sl] = int(spec.kind)
                act = (b["act"][ss] & spec.bit) != 0
                stt = b["state"][ss, int(kind)]
                fov_ok = spec.intr is not None and math.isfinite(spec.hfov_rad)
                fv = act & ((stt == SensorState.ACTIVE) | (stt == SensorState.DEGRADED)) & fov_ok
                out["flags"][sl] = act.astype(np.uint8) * FLAG_ACTIVE | fv.astype(np.uint8) * FLAG_FOV_VALID
                out["_pad"][sl] = 0
                out["pos"][sl] = pos
                out["q"][sl] = R_to_quat_xyzw(Rs)
                out["hfov_rad"][sl] = spec.hfov_rad
                out["vfov_rad"][sl] = spec.vfov_rad
                out["range_m"][sl] = spec.range_m
                row += n
        return row

    # ------------------------------------------------------------ 慢任务
    def run_slow(self, ctx: Any) -> Any:
        rt = self.rt
        if rt.S is None or rt.blk is None:
            return False
        t = int(getattr(ctx, "t_ns", 0))
        if self.pos >= self.queue.size:
            if t < self.next_due:
                return False
            self.next_due = (t // PERIOD_NS + 1) * PERIOD_NS
            self.queue = rt.interest_slots(ctx)
            self.pos = 0
            self.round_t_ns = t
            self.stats["rounds"] += 1
            if self.queue.size == 0:
                return False
        sl = self.queue[self.pos:self.pos + SLICE]
        self.pos += SLICE
        self.stats["slices"] += 1
        n = self.pack(sl, self.out)
        if n == 0:
            return None
        self.stats["rows"] += n
        self.publish(ctx, t, self.out[:n])
        return None

    def publish(self, ctx: Any, t_ns: int, rows: np.ndarray) -> None:
        import msgpack

        payload = msgpack.packb({"v": 1, "t_sim_ns": int(t_ns), "rows": rows.tobytes()}, use_bin_type=True)
        self.last_payload = payload
        if self.sink is not None:
            self.sink(payload)
            self.stats["puts"] += 1
            return
        ev = getattr(ctx, "events", None)
        bus = getattr(ev, "bus", None)
        if bus is None:
            return
        try:
            if self._pub is None or self._bus is not bus:
                from awr.contracts import bus_keys

                self._bus = bus
                self._pub = bus.publisher(bus_keys.state_sensor("sim-core"))
            self._pub.put(payload)
            self.stats["puts"] += 1
        except Exception:
            self._pub = None
