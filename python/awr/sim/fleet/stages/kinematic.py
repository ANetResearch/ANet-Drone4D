"""stage `kinematic`（order 085，every 2，125 Hz，只处理 fidelity = L0 的幽灵机）：按轨迹在世界时钟上插值写 p、v、q（M08-FR-068）。

轨迹以 World ENU 存储（`t_s` 为相对轨迹起点的秒，`pos`、可选 `vel`、`q_xyzw` WORLD←FLU）；插值时刻为 `t_sim − t0`
（t0 为加入时的仿真时刻，因此随暂停、单步、倍速一致变化）。位置用三次 Hermite（有速度列时用速度，否则中心差分），
姿态用 slerp；超出末端保持最后一帧并置 `EVT_ARRIVED`（只置一次）。写入 NED/FRD：`p_ned = (n, e, −u)`，四元数换算与
M02 对合公式相同。全部幽灵机的轨迹拼接为 CSR 数组，按 slot 维护单调游标，逐 tick 向量化推进（≤ 100 架，M08 §5.2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from ..kernels_tap import INV_SQRT2
from ..state import EVT_ARRIVED, CtrlMode
from .registry import Fidelity

if TYPE_CHECKING:
    from ..pipeline import StageCtx
    from ..state import FleetState

__all__ = ["KinematicStage", "Track", "hermite", "slerp"]


@dataclass
class Track:
    t_s: np.ndarray  # (k,) 单调递增，首点为 0
    pos: np.ndarray  # (k, 3) ENU
    vel: np.ndarray  # (k, 3) ENU
    q_xyzw: np.ndarray  # (k, 4) WORLD<-FLU

    @classmethod
    def build(cls, t_s, pos, q_xyzw=None, vel=None) -> Track:
        t = np.asarray(t_s, np.float64)
        order = np.argsort(t, kind="stable")
        t = t[order]
        keep = np.concatenate([[True], np.diff(t) > 1e-9])
        t, order = t[keep], order[keep]
        p = np.asarray(pos, np.float64)[order]
        if vel is None:
            v = np.zeros_like(p)
            if len(t) >= 2:
                v[1:-1] = (p[2:] - p[:-2]) / (t[2:] - t[:-2])[:, None]
                v[0] = (p[1] - p[0]) / (t[1] - t[0])
                v[-1] = (p[-1] - p[-2]) / (t[-1] - t[-2])
        else:
            v = np.asarray(vel, np.float64)[order]
        if q_xyzw is None:
            q = np.zeros((len(t), 4))
            q[:, 3] = 1.0
        else:
            q = np.asarray(q_xyzw, np.float64)[order]
            q = q / np.linalg.norm(q, axis=1, keepdims=True)
            for k in range(1, len(q)):  # 连续半球（slerp 走短弧）
                if np.dot(q[k], q[k - 1]) < 0:
                    q[k] = -q[k]
        return cls(t - t[0], p, v, q)


def hermite(t0, t1, p0, p1, v0, v1, t) -> tuple[np.ndarray, np.ndarray]:
    """三次 Hermite：返回 (位置, 速度)；按行向量化。"""
    h = (t1 - t0)[:, None]
    s = ((t - t0) / (t1 - t0))[:, None]
    s2, s3 = s * s, s * s * s
    p = (2 * s3 - 3 * s2 + 1) * p0 + (s3 - 2 * s2 + s) * h * v0 + (-2 * s3 + 3 * s2) * p1 + (s3 - s2) * h * v1
    v = (6 * s2 - 6 * s) / h * p0 + (3 * s2 - 4 * s + 1) * v0 + (-6 * s2 + 6 * s) / h * p1 + (3 * s2 - 2 * s) * v1
    return p, v


def slerp(q0: np.ndarray, q1: np.ndarray, u: np.ndarray) -> np.ndarray:
    d = np.sum(q0 * q1, axis=1)
    q1 = np.where(d[:, None] < 0, -q1, q1)
    d = np.abs(d)
    th = np.arccos(np.clip(d, -1.0, 1.0))
    sn = np.sin(th)
    small = sn < 1e-6
    a = np.where(small, 1.0 - u, np.sin((1.0 - u) * th) / np.where(small, 1.0, sn))
    b = np.where(small, u, np.sin(u * th) / np.where(small, 1.0, sn))
    q = a[:, None] * q0 + b[:, None] * q1
    return q / np.linalg.norm(q, axis=1, keepdims=True)


class KinematicStage:
    def __init__(self) -> None:
        self.tracks: dict[int, Track] = {}
        self.t0: dict[int, float] = {}
        self.done: set[int] = set()
        self._dirty = True
        self._slots = np.zeros(0, np.int64)
        self._off = np.zeros(0, np.int64)
        self._len = np.zeros(0, np.int64)
        self._cur = np.zeros(0, np.int64)
        self._T = np.zeros(0)
        self._P = np.zeros((0, 3))
        self._V = np.zeros((0, 3))
        self._Q = np.zeros((0, 4))

    def attach(self, slot: int, track: Track, t0_s: float) -> None:
        self.tracks[int(slot)] = track
        self.t0[int(slot)] = float(t0_s)
        self.done.discard(int(slot))
        self._dirty = True

    def drop(self, slot: int) -> None:
        if int(slot) in self.tracks:
            self.tracks.pop(int(slot), None)
            self.t0.pop(int(slot), None)
            self.done.discard(int(slot))
            self._dirty = True

    def _rebuild(self) -> None:
        slots = sorted(self.tracks)
        self._slots = np.asarray(slots, np.int64)
        lens = [len(self.tracks[s].t_s) for s in slots]
        self._len = np.asarray(lens, np.int64)
        self._off = np.concatenate([[0], np.cumsum(lens)[:-1]]).astype(np.int64) if slots else np.zeros(0, np.int64)
        self._cur = np.zeros(len(slots), np.int64)
        if slots:
            self._T = np.concatenate([self.tracks[s].t_s for s in slots])
            self._P = np.concatenate([self.tracks[s].pos for s in slots])
            self._V = np.concatenate([self.tracks[s].vel for s in slots])
            self._Q = np.concatenate([self.tracks[s].q_xyzw for s in slots])
        self._dirty = False

    def sample(self, t_s: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """全部幽灵机在仿真时刻 t_s 的 (slots, pos, vel, q_xyzw, ended)，ENU。"""
        if self._dirty:
            self._rebuild()
        n = self._slots.size
        if n == 0:
            e = np.zeros((0, 3))
            return self._slots, e, e, np.zeros((0, 4)), np.zeros(0, bool)
        t0 = np.array([self.t0[int(s)] for s in self._slots])
        tl = t_s - t0
        last = self._len - 1
        cur = np.minimum(self._cur, np.maximum(last - 1, 0))
        # 游标单调推进（时间回退时重置，例如 checkpoint 恢复）
        back = tl < self._T[self._off + cur]
        cur = np.where(back, 0, cur)
        while True:
            nxt = np.minimum(cur + 1, last)
            adv = (cur < last - 1) & (tl >= self._T[self._off + nxt])
            if not adv.any():
                break
            cur = cur + adv
        self._cur = cur
        i0 = self._off + cur
        i1 = self._off + np.minimum(cur + 1, last)
        single = last == 0
        ended = tl >= self._T[self._off + last]
        tc = np.clip(tl, self._T[i0], np.where(single, self._T[i0], self._T[i1]))
        span = self._T[i1] - self._T[i0]
        ok = span > 1e-12
        tt0 = self._T[i0]
        tt1 = np.where(ok, self._T[i1], tt0 + 1.0)
        pos, vel = hermite(tt0, tt1, self._P[i0], self._P[i1], self._V[i0], self._V[i1], np.where(ok, tc, tt0))
        u = np.where(ok, (tc - self._T[i0]) / np.where(ok, span, 1.0), 0.0)
        q = slerp(self._Q[i0], self._Q[i1], u)
        lastp = self._off + last
        pos = np.where(ended[:, None], self._P[lastp], pos)
        vel = np.where(ended[:, None], 0.0, vel)
        q = np.where(ended[:, None], self._Q[lastp], q)
        return self._slots, pos, vel, q, ended

    def __call__(self, S: FleetState, ctx: StageCtx) -> None:
        if not self.tracks:
            return
        slots, pos, vel, q, ended = self.sample(ctx.t_ns * 1e-9)
        act = S.active[slots] & ((S.fidelity[slots] & int(Fidelity.L0)) != 0)
        s = slots[act]
        if s.size == 0:
            return
        pos, vel, q, ended = pos[act], vel[act], q[act], ended[act]
        S.p_prev[s] = S.p[s]
        S.p[s, 0], S.p[s, 1], S.p[s, 2] = pos[:, 1], pos[:, 0], -pos[:, 2]
        S.v[s, 0], S.v[s, 1], S.v[s, 2] = vel[:, 1], vel[:, 0], -vel[:, 2]
        W, X, Y, Z = q[:, 3], q[:, 0], q[:, 1], q[:, 2]
        S.q[s, 0] = (W + Z) * INV_SQRT2
        S.q[s, 1] = (X + Y) * INV_SQRT2
        S.q[s, 2] = (X - Y) * INV_SQRT2
        S.q[s, 3] = (W - Z) * INV_SQRT2
        S.q_sp[s] = S.q[s]
        S.pos_ref[s] = S.p[s]
        S.omega[s] = 0.0
        S.ctrl_mode[s] = CtrlMode.KINEMATIC
        S.landed[s] = False
        S.in_air[s] = True
        for k in np.flatnonzero(ended):
            sl = int(s[k])
            if sl not in self.done:
                self.done.add(sl)
                S.mode_evt[sl] |= EVT_ARRIVED
        S.touch()

    def checkpoint_meta(self) -> dict:
        return {"t0": {str(k): v for k, v in self.t0.items()}, "cursor": {int(s): int(c) for s, c in
                                                                          zip(self._slots, self._cur, strict=False)}}

    def restore_meta(self, meta: dict) -> None:
        for k, v in (meta.get("t0") or {}).items():
            if int(k) in self.tracks:
                self.t0[int(k)] = float(v)
        self._dirty = True
