"""服务端软围栏 `keepout_guard`（M18-FR-022；AWR-04 §10.6；17 §17.7.6 伪代码；M08 `register_setpoint_filter`）。

ingest 在步边界写入新鲜 velocity setpoint 之后调用：对 `keepout_guard ∈ {clamp, warn}` 的机体，取全部启用识别物的禁入体
（geometric：启用的视觉、声学配置体；physical：保守有效体；运动识别物的扇形按整圆；不外扩），对每个体求带符号距离与法向，
朝体内的速度分量 `v_in = −v·n > 0` 且距离 `d ≤ max(5 m, 0.5·|v|)` 时：clamp 去掉该分量（切向保留），warn 只告警。
事件 `uav.keepout_guard{uav, target, vol_idx, d_m, clamped}` 每机 ≤ 1 Hz【仿真】。速度 setpoint 为 NED（world 帧）或 FRD/FLU
（body 帧，`S.vel_frame == 1`，按机体航向换算）；本钩子 10 ms 内完成（体数 ≤ 64）。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .effective import keepout_volumes
from .geometry import Volume, signed_distance

__all__ = ["GUARD_MIN_M", "KeepoutGuard"]

GUARD_MIN_M = 5.0
EVENT_PERIOD_NS = 1_000_000_000


def _yaw_of(q_xyzw: np.ndarray) -> float:
    x, y, z, w = (float(v) for v in q_xyzw)
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class KeepoutGuard:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.last_evt: dict[int, int] = {}
        self.stats = {"calls": 0, "clamped": 0}

    def volumes(self) -> list[tuple[str, Volume]]:
        rt = self.rt
        out: list[tuple[str, Volume]] = []
        for T in rt.ordered():
            if not T.cfg.raw.get("enabled", True):
                continue
            mode = T.cfg.raw.get("criterion_mode", "geometric")
            st = rt.state1(T.id)
            out.extend((T.id, v) for v in keepout_volumes(T.cfg, st, mode, 0.0, conservative=True, moving_as_circle=True, cache=rt.eff))
        return out

    def __call__(self, S: Any, slots: np.ndarray, modes: list[str], ctx: Any) -> None:
        self.stats["calls"] += 1
        want = [(int(s), m) for s, m in zip(slots.tolist(), modes, strict=False) if m in ("clamp", "warn")]
        if not want or not self.rt.bind(ctx):
            return
        vols = self.volumes()
        if not vols:
            return
        try:
            S.enu.refresh()
            pos, quat = S.enu.pos, S.enu.q_xyzw
        except Exception:
            return
        roster = getattr(ctx, "roster", None)
        t_ns = int(getattr(ctx, "t_ns", 0))
        for s, mode in want:
            p = np.asarray(pos[s], float)
            v = np.asarray(S.vel_cmd[s], float)
            body = int(S.vel_frame[s]) == 1
            if body:  # FLU 机体帧 → ENU（只转水平分量）
                yaw = _yaw_of(quat[s])
                c, sn = math.cos(yaw), math.sin(yaw)
                ve = np.array([c * v[0] - sn * v[1], sn * v[0] + c * v[1], v[2]])
            else:  # NED → ENU
                ve = np.array([v[1], v[0], -v[2]])
            speed = float(np.linalg.norm(ve))
            guard_m = max(GUARD_MIN_M, 0.5 * speed)
            hit = None
            clamped = False
            for tid, vol in vols:
                d, n = signed_distance(vol, p)
                v_in = -float(ve @ n)
                if v_in > 0.0 and d <= guard_m:
                    hit = (tid, vol.vol_idx, d)
                    if mode == "clamp":
                        ve = ve + v_in * n
                        clamped = True
            if hit is None:
                continue
            if clamped:
                self.stats["clamped"] += 1
                if body:
                    c, sn = math.cos(-yaw), math.sin(-yaw)
                    S.vel_cmd[s] = (c * ve[0] - sn * ve[1], sn * ve[0] + c * ve[1], ve[2])
                else:
                    S.vel_cmd[s] = (ve[1], ve[0], -ve[2])
            if t_ns - self.last_evt.get(s, -(1 << 62)) >= EVENT_PERIOD_NS:
                self.last_evt[s] = t_ns
                e = roster.by_slot.get(s) if roster is not None and hasattr(roster, "by_slot") else None
                vid = e.id if e is not None else str(s)
                ev = getattr(ctx, "events", None)
                if ev is not None and hasattr(ev, "emit"):
                    ev.emit("uav.keepout_guard", t_sim_ns=t_ns, severity=1, uav=vid,
                            fields={"uav": vid, "target": hit[0], "vol_idx": int(hit[1]), "d_m": round(float(hit[2]), 2),
                                    "clamped": bool(clamped)})
