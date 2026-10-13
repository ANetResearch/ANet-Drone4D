"""MID-360 限频扫描（M13-FR-213、NFR-202；M13 §15.5 第 4 步；AWR-04 §11.7 MID-360 行；r04 §3.1）。

慢任务 `m13.lidar_scan`：只在 `uav/{id}/sensor/lidar/scan` 被订阅时生成，≤ 2 Hz【仿真】，每会话同时只为 1 架机生成（订阅集
里最新的一架为 active，其余为 paused，`payload.scan` 记录）。每帧取 D1 花样纯函数 `mid360_pattern(frame_seq)` 的一个 100 ms
帧（20 000 条射线），按片（每片 `SLICE` 条，约 ≤ 300 µs）在 M04 DSM（2 m，柱体语义）上做 numba 网格遍历求交，并对 ≤ 30 个
识别物（M18 实体表 `tgt_pos`、`tgt_size`、`tgt_yaw`）的包围盒做射线–AABB 求交；距离上限按命中面反射率
`R_max(rho) = min(hard_max, r_at_rho10·(rho/0.1)^exponent)` 截断（地物 rho = 0.2，识别物缺省 0.3 或 M18 给出的 `reflect_905`）；
测距噪声沿用 D1 参数（近 0.03 m、远 0.02 m，20 m 为界），用计数器 RNG（流 3，键的 tick 取帧序号，通道 = base + 点序号），
同一快照与种子下扫描帧逐字节一致。

位姿取帧中时刻：帧开始时刻的机体位姿按当前速度外推 50 ms（D1 `DESKEWED` 语义的近似）；快进倍率 > 5 时暂停生成（M13 §15.9）。
编码 `awr.LidarScan8.v1`（48 B 头 + 每点 8 B，传感器系 cm；refl = rho·255；tag 的 bit0 = 命中识别物，line = 点序号 & 3），
发布到 `state/sim-core/lidar`：msgpack `{v: 1, t_sim_ns, items: {<uav id>: bin(LidarScan8)}}`（M11 网关的总线形状，帧序号在
LidarScan8 头内）。
"""

from __future__ import annotations

import math
import os
from typing import TYPE_CHECKING, Any

import numpy as np

from .. import cbrng
from ..enums import SensorKind
from .frame import pack_scan8
from .pattern import NPF, directions_flu, mid360_pattern

try:
    from numba import njit

    HAVE_NUMBA = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover
    HAVE_NUMBA = False

    def njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f

        return deco(a[0]) if a and callable(a[0]) else deco

if TYPE_CHECKING:
    from ..runtime import SensorRuntime

__all__ = ["FRAME_PERIOD_NS", "SLICE", "LidarScanner", "dda_dsm", "ray_aabb", "warmup"]

FRAME_PERIOD_NS = 500_000_000   # ≤ 2 Hz
SLICE = 2500
RHO_GROUND = 0.2
RHO_TARGET = 0.3
MAX_TARGETS = 30
FAST_RATE = 5.0
CH_BASE = 1 << 24
SCAN_OFF, SCAN_ACTIVE, SCAN_PAUSED = 0, 1, 2


@njit(cache=True, fastmath=False)
def dda_dsm(a, x0, y0, cell, zmax, ox, oy, oz, dirs, rmax, out_t):
    """柱体语义 DSM 上的网格遍历（Amanatides–Woo）：每格 [t_in, t_out] 内射线高度的较小值 ≤ 格顶高即命中（入格时已在格顶
    之下为侧面命中，否则为顶面命中 `t_in + (h − z_in)/d_z`）；出界或超过 rmax 为未命中（NaN）。射线向上且高于全图最高点时提前结束。"""
    H, W = a.shape
    n = dirs.shape[0]
    for i in range(n):
        dx, dy, dz = dirs[i, 0], dirs[i, 1], dirs[i, 2]
        out_t[i] = math.nan
        fx = (ox - x0) / cell
        fy = (oy - y0) / cell
        ix = math.floor(fx)
        iy = math.floor(fy)
        if dx > 0.0:
            sx = 1
            tdx = cell / dx
            tmx = (math.floor(fx) + 1.0 - fx) * cell / dx
        elif dx < 0.0:
            sx = -1
            tdx = -cell / dx
            tmx = (fx - math.floor(fx)) * cell / -dx
        else:
            sx = 0
            tdx = math.inf
            tmx = math.inf
        if dy > 0.0:
            sy = 1
            tdy = cell / dy
            tmy = (math.floor(fy) + 1.0 - fy) * cell / dy
        elif dy < 0.0:
            sy = -1
            tdy = -cell / dy
            tmy = (fy - math.floor(fy)) * cell / -dy
        else:
            sy = 0
            tdy = math.inf
            tmy = math.inf
        t0 = 0.0
        while t0 < rmax:
            if ix < 0 or iy < 0 or ix >= W or iy >= H:
                break
            t1 = tmx if tmx < tmy else tmy
            if t1 > rmax:
                t1 = rmax
            h = a[iy, ix]
            z0 = oz + dz * t0
            z1 = oz + dz * t1
            if z0 <= h:
                out_t[i] = t0
                break
            if z1 <= h:
                out_t[i] = t0 + (h - z0) / dz
                break
            if dz >= 0.0 and z1 > zmax:
                break
            t0 = t1
            if tmx < tmy:
                ix += sx
                tmx += tdx
            else:
                iy += sy
                tmy += tdy


@njit(cache=True, fastmath=False)
def ray_aabb(ox, oy, oz, dirs, lo, hi, out_t, out_k):
    """射线与轴对齐包围盒（slab 法）：比 out_t 更近的命中写入 out_t 与盒序号 out_k。"""
    n = dirs.shape[0]
    m = lo.shape[0]
    for i in range(n):
        for k in range(m):
            tn = 0.0
            tf = math.inf
            ok = True
            for c in range(3):
                o = ox if c == 0 else (oy if c == 1 else oz)
                d = dirs[i, c]
                if d == 0.0:
                    if o < lo[k, c] or o > hi[k, c]:
                        ok = False
                        break
                    continue
                a = (lo[k, c] - o) / d
                b = (hi[k, c] - o) / d
                if a > b:
                    a, b = b, a
                if a > tn:
                    tn = a
                if b < tf:
                    tf = b
                if tn > tf:
                    ok = False
                    break
            if ok and tn > 0.0:
                cur = out_t[i]
                if cur != cur or tn < cur:
                    out_t[i] = tn
                    out_k[i] = k


def warmup() -> None:
    if not HAVE_NUMBA:
        return
    a = np.zeros((4, 4), np.float32)
    d = np.array([[0.6, 0.0, -0.8]])
    t = np.zeros(1)
    dda_dsm(a, 0.0, 0.0, 2.0, 0.0, 1.0, 1.0, 5.0, d, 100.0, t)
    # 运行期 DSM 是 M04 的只读内存映射（只读 float32 网格）：另一个签名，同样预热（D2-ACC-1 第 4.7 节：首次扫描时在主循环内编译）
    a_ro = a.copy()
    a_ro.flags.writeable = False
    dda_dsm(a_ro, 0.0, 0.0, 2.0, 0.0, 1.0, 1.0, 5.0, d, 100.0, t)
    ray_aabb(1.0, 1.0, 5.0, d, np.zeros((1, 3)), np.ones((1, 3)), t, np.zeros(1, np.int32))


class _Frame:
    __slots__ = (
        "R",
        "agent",
        "d_s",
        "d_w",
        "done",
        "expo",
        "hi",
        "k",
        "lo",
        "o",
        "pos",
        "q",
        "r10",
        "rho",
        "rho_t",
        "rmax",
        "seq",
        "slot",
        "t",
        "t_ns",
    )


class LidarScanner:
    def __init__(self, rt: SensorRuntime) -> None:
        self.rt = rt
        self.frame: _Frame | None = None
        self.seq = 0
        self.next_due = 0
        self.override: list[int] | None = None  # `scan_interest` 给出的订阅（agent_no，最后一个为最新）
        self.sink: Any = None
        self._pub = None
        self._bus = None
        self.last: dict | None = None
        self.az = np.empty(NPF)
        self.el = np.empty(NPF)
        self._zmax_key = -1
        self._zmax_val = 0.0
        self.stats = {"frames": 0, "slices": 0, "points": 0, "skipped_fast": 0, "puts": 0}

    # ------------------------------------------------------------ 订阅
    def wanted(self, ctx: Any) -> list[int]:
        if self.override is not None:
            return list(self.override)
        from ..nav_pub import _stream

        return list(_stream(ctx, "lidar") or [])

    def forget(self, slot: int) -> None:
        if self.frame is not None and self.frame.slot == slot:
            self.frame = None

    def _update_states(self, ctx: Any, want: list[int]) -> int:
        """`payload.scan`：最新订阅者 active，其余订阅者 paused，其他 off；返回 active 的 slot（无为 −1）。"""
        rt = self.rt
        S, P = rt.S, rt.pblk
        if S is None or P is None:
            return -1
        if not want:  # 无订阅：清零（只在有残留状态时写，慢任务每轮调用，D1 大机群零开销）
            if P["scan"].any():
                P["scan"][:] = SCAN_OFF
            return -1
        act = S.active_idx()
        ag = S.agent_no[act].astype(np.int64)
        newest = int(want[-1])
        st = np.where(np.isin(ag, np.asarray(want, np.int64)), SCAN_PAUSED, SCAN_OFF).astype(np.uint8)
        hit = np.flatnonzero(ag == newest)
        st[hit] = SCAN_ACTIVE
        s_new = int(act[hit[0]]) if hit.size else -1
        P["scan"][act] = st
        return s_new

    # ------------------------------------------------------------ 慢任务
    def run_slow(self, ctx: Any) -> Any:
        rt = self.rt
        S = rt.S
        if S is None or rt.blk is None:
            return False
        want = self.wanted(ctx)
        s_new = self._update_states(ctx, want)
        if self.frame is not None:
            if self.frame.slot != s_new:
                self.frame = None
            else:
                self._slice(ctx)
                return None
        if s_new < 0:
            return False
        t = int(getattr(ctx, "t_ns", 0))
        if t < self.next_due:
            return False
        rate = float(getattr(ctx, "rate", getattr(getattr(ctx, "clock", None), "rate", 1.0)) or 1.0)
        if rate > FAST_RATE:
            self.stats["skipped_fast"] += 1
            self.next_due = t + FRAME_PERIOD_NS
            return False
        if not self._start(ctx, s_new, t):
            self.next_due = t + FRAME_PERIOD_NS
            return False
        self.next_due = t + FRAME_PERIOD_NS
        self._slice(ctx)
        return None

    def _lidar_spec(self, s: int) -> Any:
        rig = self.rt.rig_of(s)
        sp = None if rig is None else rig.by_kind(SensorKind.LIDAR)
        b, P = self.rt.blk, self.rt.pblk
        if sp is None or not (b["has"][s] & sp.bit) or not (b["act"][s] & sp.bit):
            return None
        if P is not None and (int(P["off"][s]) >> sp.sensor_no) & 1:
            return None
        return sp

    def _start(self, ctx: Any, s: int, t: int) -> bool:
        rt = self.rt
        sp = self._lidar_spec(s)
        if sp is None:
            return False
        from ..frames import R_to_quat_xyzw, quat_to_R

        S = rt.S
        PQ = S.enu.pose_enu_flu(np.array([s]))
        vel = np.asarray(S.vel_enu_view()[s], np.float64)
        Rb = quat_to_R(PQ[:, 3:7])[0]
        pos_b = PQ[0, :3] + 0.05 * np.nan_to_num(vel)
        R = Rb @ sp.mount_R
        o = pos_b + Rb @ sp.mount_t
        self.seq += 1
        mid360_pattern(self.seq, self.az, self.el)
        d_s = directions_flu(self.az, self.el)
        f = _Frame()
        f.agent, f.slot, f.seq, f.t_ns = int(S.agent_no[s]), int(s), int(self.seq), int(t)
        f.o, f.R, f.d_s = o, R, d_s
        f.d_w = np.ascontiguousarray(d_s @ R.T)
        f.t = np.full(NPF, np.nan)
        f.k = np.full(NPF, -1, np.int32)
        f.rho = np.full(NPF, RHO_GROUND)
        f.done = 0
        f.pos = o.astype(np.float32)
        f.q = R_to_quat_xyzw(R[None])[0].astype(np.float32)
        rg = sp.doc.get("range") or {}
        f.rmax = float(rg.get("hard_max_m", sp.range_m))
        f.r10 = float(rg.get("r_at_rho10_m", 40.0))
        f.expo = float(rg.get("exponent", 0.269))
        f.lo, f.hi, f.rho_t = self._target_boxes(o, f.rmax)
        self.frame = f
        self.stats["frames"] += 1
        return True

    def _target_boxes(self, o: np.ndarray, rmax: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        rt = self.rt
        tb = rt._targets_block()
        if tb is None or "tgt_pos" not in tb or "tgt_size" not in tb:
            return np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0)
        act = np.asarray(tb.get("active", np.ones(len(tb["tgt_pos"]), np.uint8))) != 0
        idx = np.flatnonzero(act)
        pos = np.asarray(tb["tgt_pos"], np.float64)[idx]
        near = np.linalg.norm(pos[:, :2] - o[None, :2], axis=1) <= rmax + 10.0
        idx = idx[near][:MAX_TARGETS]
        if idx.size == 0:
            return np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0)
        pos = np.asarray(tb["tgt_pos"], np.float64)[idx]
        size = np.asarray(tb["tgt_size"], np.float64)[idx]
        yaw = np.asarray(tb.get("tgt_yaw", np.zeros(len(tb["tgt_pos"]))), np.float64)[idx]
        c, s_ = np.abs(np.cos(yaw)), np.abs(np.sin(yaw))
        hx = 0.5 * (c * size[:, 0] + s_ * size[:, 1])
        hy = 0.5 * (s_ * size[:, 0] + c * size[:, 1])
        lo = np.stack([pos[:, 0] - hx, pos[:, 1] - hy, pos[:, 2]], axis=1)
        hi = np.stack([pos[:, 0] + hx, pos[:, 1] + hy, pos[:, 2] + size[:, 2]], axis=1)
        rho = np.full(idx.size, RHO_TARGET)
        fn = getattr(rt, "target_reflect_fn", None)
        if callable(fn):
            nos = np.asarray(tb.get("target_no", idx))[idx]
            for i, no in enumerate(nos.tolist()):
                try:
                    v = fn(int(no))
                    if v is not None:
                        rho[i] = float(v)
                except Exception:
                    pass
        return np.ascontiguousarray(lo), np.ascontiguousarray(hi), rho

    def _slice(self, ctx: Any) -> None:
        f = self.frame
        if f is None:
            return
        a, b = f.done, min(f.done + SLICE, NPF)
        d = f.d_w[a:b]
        tt = np.full(b - a, np.nan)
        w = getattr(self.rt.ctx, "world", None)
        grid = None
        if w is not None and callable(getattr(w, "dsm_grid", None)):
            try:
                grid = w.dsm_grid()
            except Exception:
                grid = None
        if grid is not None:
            arr = np.asarray(grid.a)
            zmax = float(self._zmax(arr))
            dda_dsm(arr, float(grid.x0_m), float(grid.y0_m), float(grid.cell_m), zmax, float(f.o[0]), float(f.o[1]), float(f.o[2]),
                    d, f.rmax, tt)
        else:  # 无世界：平地 z = 0
            down = d[:, 2] < 0.0
            tt[down] = -f.o[2] / d[down, 2]
            tt[~(tt <= f.rmax)] = np.nan
        kk = np.full(b - a, -1, np.int32)
        if f.lo.shape[0]:
            ray_aabb(float(f.o[0]), float(f.o[1]), float(f.o[2]), d, f.lo, f.hi, tt, kk)
        f.t[a:b] = tt
        f.k[a:b] = kk
        hit_t = kk >= 0
        if hit_t.any():
            f.rho[a:b][hit_t] = f.rho_t[kk[hit_t]]
        f.done = b
        self.stats["slices"] += 1
        if f.done >= NPF:
            self._finish(ctx)

    def _zmax(self, arr: np.ndarray) -> float:
        """全图最高点（向上的射线高于它即可提前结束）；按栅格对象缓存。"""
        k = id(arr)
        if self._zmax_key != k:
            self._zmax_key = k
            self._zmax_val = float(np.nanmax(arr)) if arr.size else 0.0
        return self._zmax_val

    def _finish(self, ctx: Any) -> None:
        f = self.frame
        self.frame = None
        rt = self.rt
        rlim = np.minimum(f.rmax, f.r10 * (np.maximum(f.rho, 1e-6) / 0.1) ** f.expo)
        ok = np.isfinite(f.t) & (f.t >= 0.1) & (f.t <= rlim)
        i = np.flatnonzero(ok)
        sp = self._lidar_spec(f.slot)
        nz = (sp.noise if sp is not None else {}) or {}
        near, far = float(nz.get("range_sigma_near_m", 0.03)), float(nz.get("range_sigma_far_m", 0.02))
        n = cbrng.normal_elem(rt.seed, 3, f.agent, f.seq, CH_BASE + i)
        tr = f.t[i] + np.where(f.t[i] < 20.0, near, far) * n
        xyz = f.d_s[i] * tr[:, None]
        refl = np.clip(np.round(f.rho[i] * 255.0), 0, 255).astype(np.uint8)
        tag = (f.k[i] >= 0).astype(np.uint8)
        line = (i & 3).astype(np.uint8)
        blob = pack_scan8(f.t_ns, f.seq, f.pos, f.q, xyz, refl, tag, line)
        self.stats["points"] += int(i.size)
        import msgpack

        uav = rt.uav_id(rt.S, f.slot) if rt.S is not None else str(f.agent)
        msg = {"v": 1, "t_sim_ns": f.t_ns, "items": {str(uav): blob}}
        self.last = {"uav": uav, "seq": f.seq, "n": int(i.size), "bytes": len(blob), "t_sim_ns": f.t_ns, "idx": i,
                     "xyz": xyz, "pos": f.o.copy(), "R": f.R.copy(), "tag": tag, "blob": blob}
        payload = msgpack.packb(msg, use_bin_type=True)
        if self.sink is not None:
            self.sink(payload)
            self.stats["puts"] += 1
            return
        from ..nav_pub import _bus_put, _key

        if _bus_put(ctx, self, _key("STATE_LIDAR"), payload):
            self.stats["puts"] += 1
