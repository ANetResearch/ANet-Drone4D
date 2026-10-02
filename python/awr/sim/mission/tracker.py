"""跟踪器（M10-FR-011 至 FR-018；M10 §6.3.4、§6.4.2、§6.5.8、§7.4.2）。

- `STATE_FIELDS`：经 `register_state_block("mission", owner="M10")` 由 M08 统一分配并参与 checkpoint，含 M08 tap 读取的
  `mission_item`（u16，0xFFFF 为无）与 `track_state`（u8）；
- `CtrlPool`：控制点池 f64[1 048 576, 3]（24 MiB），首次适配分配、释放归还空闲表（相邻空闲段合并）；
- `TrajCache`：轨迹缓存（键 = traj_key，LRU 256 条；被在途调用钉住的条目不驱逐），FR-017；
- `Tracker.stage`：stage `mission`（order 027、every 2、phase 0，125 Hz）：对 `ctrl_mode == TRAJ` 的槽位调用跟踪核，
  经 M08 `actions.set_traj_enu`（fleet 内以 M02 的唯一换算写 `tr_x/tr_v/tr_a/yaw_sp`）输出设定点；到端以静止点移交 HOLD
  （`begin_hold` + `EVT_ARRIVED`，M08 K19）；`ctrl_mode ≠ TRAJ` 的槽位跳过（FSM 或 M08 已接管）；
- 三个运动提供者（`m10.follow_path`、`m10.orbit`、`m10.goto_route`）：参数与缓存检查同步完成；需要规划时先按
  a_brake 刹停（kind 4），经 `CommandEngine.schedule_fine_check` 让调用停在 accepted，plan-pool 结果在步边界生效后
  装入轨迹并 `fine_result(cid, ok)`（M10 §7.4.2 的"纳入前等价路径"）；
- 暂停：M08 的 `pause` 会 `provider.cancel` 并交回 HOLD（目标 = 当时的 tr_x，即轨迹上的点）；本模块保存该调用的 τ，
  `resume` 以同一 cid 重新 `start` 时从 τ 以时钟斜坡恢复（FR-007；§14 反馈：请 M08 在协议中增加 `pause`）。
"""

from __future__ import annotations

import logging
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from awr.sim.fleet import actions as ACT
from awr.sim.planning import bspline as BS
from awr.sim.planning import kernels_track as KT

if TYPE_CHECKING:
    from .runtime import M10Runtime

__all__ = [
    "STATE_FIELDS",
    "CtrlPool",
    "FollowPathProvider",
    "GotoRouteProvider",
    "OrbitProvider",
    "SlotMeta",
    "Tracker",
    "TrajCache",
    "TrajEntry",
]

log = logging.getLogger("awr.sim.mission.tracker")

STATE_FIELDS: dict[str, tuple[Any, tuple[int, ...]]] = {
    "mission_item": (np.uint16, ()), "track_state": (np.uint8, ()),
    "kind": (np.uint8, ()), "off": (np.int64, ()), "nseg": (np.int32, ()), "ts": (np.float64, ()),
    "tau": (np.float64, ()), "rate": (np.float64, ()), "rate_tgt": (np.float64, ()), "rate_dot": (np.float64, ()),
    "yaw_mode": (np.uint8, ()), "yaw_arg": (np.float64, (3,)), "psi": (np.float64, ()),
    "group": (np.int32, ()), "orb": (np.float64, (KT.ORB_COLS,)), "orb_dir": (np.int8, ()), "orb_turns": (np.float64, ()),
    "slot_off": (np.float64, (3,)), "done": (np.uint8, ()), "traj_id": (np.int64, ()),
    "out_p": (np.float64, (3,)), "out_v": (np.float64, (3,)), "out_a": (np.float64, (3,)), "out_psi": (np.float64, ()),
}
POOL_CAP = 1 << 20
N_GROUPS = 64
EMAX_XY, EMAX_Z = 2.0, 1.0
A_BRAKE = 2.0
A_TAN = 2.0
CACHE_MAX = 256
YAW_CODES = {"none": KT.Y_NONE, "lookahead": KT.Y_LOOKAHEAD, "path": KT.Y_PATH, "tangent": KT.Y_PATH,
             "center": KT.Y_POINT, "axis": KT.Y_POINT, "fixed": KT.Y_FIXED}
M_TRAJ = 15
EVT_ARRIVED = 2


# ------------------------------------------------------------------------------------------------ 控制点池
class CtrlPool:
    def __init__(self, cap: int = POOL_CAP) -> None:
        self.Q = np.zeros((cap, 3), np.float64)
        self.free: list[tuple[int, int]] = [(0, cap)]
        self.used = 0

    def alloc(self, n: int) -> int:
        for i, (o, m) in enumerate(self.free):
            if m >= n:
                if m == n:
                    self.free.pop(i)
                else:
                    self.free[i] = (o + n, m - n)
                self.used += n
                return o
        return -1

    def release(self, off: int, n: int) -> None:
        if off < 0 or n <= 0:
            return
        self.used -= n
        self.free.append((off, n))
        self.free.sort()
        merged: list[tuple[int, int]] = []
        for o, m in self.free:
            if merged and merged[-1][0] + merged[-1][1] == o:
                merged[-1] = (merged[-1][0], merged[-1][1] + m)
            else:
                merged.append((o, m))
        self.free = merged

    def fragmentation(self) -> float:
        tot = sum(m for _o, m in self.free)
        return 0.0 if tot == 0 else 1.0 - max(m for _o, m in self.free) / tot


# ------------------------------------------------------------------------------------------------ 轨迹缓存
@dataclass
class TrajEntry:
    key: str
    traj_id: int
    vehicle_id: str
    ts_s: float
    Q: np.ndarray
    yaw: dict
    kind: str = "bspline"
    group: dict | None = None
    anchor_Q: np.ndarray | None = None
    anchor_ts: float = 0.5
    source: dict = field(default_factory=dict)
    len_m: float = 0.0
    duration_s: float = 0.0
    pins: int = 0

    @property
    def start(self) -> np.ndarray:
        return BS.eval_bspline(self.Q, self.ts_s, 0.0)[0]

    @property
    def end(self) -> np.ndarray:
        return BS.eval_bspline(self.Q, self.ts_s, BS.duration(self.Q, self.ts_s))[0]


class TrajCache:
    def __init__(self, maxlen: int = CACHE_MAX) -> None:
        self.maxlen = maxlen
        self._d: OrderedDict[str, TrajEntry] = OrderedDict()
        self._next_id = 1
        self.hits = 0
        self.misses = 0
        self._unpinned = 0  # 缓存中 pins == 0 的条目数：全部被钉住时 _evict 不再逐条扫描（1000 架在途，FX2-R2）

    def new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def put_traj(self, tr: dict) -> TrajEntry:
        key = str(tr["key"])
        e = self._d.get(key)
        if e is not None:
            self._d.move_to_end(key)
            return e
        anchor_Q = None
        if tr.get("kind") == "formation":
            anchor_Q = np.asarray(tr["ctrl_pts"], np.float64)
        e = TrajEntry(key, self.new_id(), str(tr.get("vehicle_id", "")), float(tr["ts_s"]),
                      np.asarray(tr["ctrl_pts"], np.float64), dict(tr.get("yaw") or {}), str(tr.get("kind", "bspline")),
                      tr.get("group"), anchor_Q, float(tr["ts_s"]), dict(tr.get("source") or {}),
                      float(tr.get("len_m", 0.0)), float(tr.get("duration_s", 0.0)))
        self._d[key] = e
        self._unpinned += 1
        self._evict()
        return e

    def put_raw(self, key: str, Q: np.ndarray, ts_s: float, yaw: dict | None = None, vehicle_id: str = "") -> TrajEntry:
        e = self._d.get(key)
        if e is None:
            e = TrajEntry(key, self.new_id(), vehicle_id, float(ts_s), np.asarray(Q, np.float64), dict(yaw or {}),
                          duration_s=BS.duration(Q, ts_s))
            self._d[key] = e
            self._unpinned += 1
            self._evict()
        return e

    def get(self, key: str | None) -> TrajEntry | None:
        if not key:
            return None
        e = self._d.get(str(key))
        if e is None:
            self.misses += 1
            return None
        self.hits += 1
        self._d.move_to_end(str(key))
        return e

    def pin(self, e: TrajEntry) -> None:
        if e.pins == 0 and self._d.get(e.key) is e:
            self._unpinned -= 1
        e.pins += 1

    def unpin(self, e: TrajEntry | None) -> None:
        if e is not None and e.pins > 0:
            e.pins -= 1
            if e.pins == 0 and self._d.get(e.key) is e:
                self._unpinned += 1
            self._evict()

    def _evict(self) -> None:
        while len(self._d) > self.maxlen and self._unpinned > 0:
            for k, e in self._d.items():
                if e.pins == 0:
                    del self._d[k]
                    self._unpinned -= 1
                    break
            else:
                self._unpinned = 0
                return

    def clear(self) -> None:
        self._d.clear()
        self._unpinned = 0

    def __len__(self) -> int:
        return len(self._d)


# ------------------------------------------------------------------------------------------------ 槽位元数据
@dataclass
class SlotMeta:
    cid: str | None = None
    provider: str | None = None
    entry: TrajEntry | None = None
    pool_off: int = -1
    pool_n: int = 0
    phase: str = "idle"                 # idle、brake、line、bspline、orbit、formation、wait_plan
    next_phase: dict | None = None      # 当前段完成后的下一段（orbit 入圆 → 环绕）
    job_id: str | None = None
    managed_fine: bool = False
    t_load_ns: int = 0
    path_rev: int = 0
    orbit_args: dict | None = None
    gid: int = -1
    paused: bool = False
    phase_before_pause: str | None = None


class Tracker:
    def __init__(self, rt: M10Runtime) -> None:
        self.rt = rt
        self.pool = CtrlPool()
        self.cache = TrajCache()
        self.meta: dict[int, SlotMeta] = {}
        self._paused_slots: set[int] = set()  # pause_slot 置位过的 slot（_hold_paused 只在其中仍暂停的机体存在时筛选）
        self.suspended: dict[str, dict] = {}   # cid → 暂停时保存的状态（resume 用）
        self.B: dict[str, np.ndarray] | None = None
        cap = N_GROUPS
        self.G_tau = np.zeros(cap)
        self.G_fmin = np.ones(cap)
        self.G_psi = np.zeros(cap)
        self.G_w = np.zeros(cap)
        self.G_off = np.zeros(cap, np.int64)
        self.G_nseg = np.ones(cap, np.int32)
        self.G_ts = np.full(cap, 0.5)
        self.G_wmax = np.full(cap, 1.0)
        self.G_taupsi = np.zeros(cap)
        self.G_active = np.zeros(cap, np.uint8)
        self.G_members: dict[int, set[int]] = {}
        self.G_key: dict[str, int] = {}
        self.G_pool: dict[int, tuple[int, int]] = {}
        self.kernel = KT.track_step if KT.HAVE_NUMBA else KT.track_step_numpy
        self._idx_cache: tuple[int, np.ndarray] = (-1, np.zeros(0, np.int32))
        self.stats = {"calls": 0, "loads": 0, "handovers": 0}

    # ------------------------------------------------------------ 绑定
    def bind(self, S: Any) -> None:
        self.B = S.blocks["mission"]
        self.B["group"][:] = -1

    def slot_meta(self, s: int) -> SlotMeta:
        m = self.meta.get(int(s))
        if m is None:
            m = self.meta[int(s)] = SlotMeta()
        return m

    # ------------------------------------------------------------ 装入与释放
    def _reset_slot(self, s: int) -> None:
        B = self.B
        B["kind"][s] = 0
        B["done"][s] = 0
        B["rate"][s] = 1.0
        B["rate_tgt"][s] = 1.0
        B["rate_dot"][s] = 0.0
        B["group"][s] = -1
        B["tau"][s] = 0.0

    def release(self, s: int, *, keep_meta: bool = False) -> None:
        m = self.meta.get(int(s))
        if self.B is not None:
            gid = int(self.B["group"][s])
            if gid >= 0:
                mem = self.G_members.get(gid)
                if mem is not None:
                    mem.discard(int(s))
                    if not mem:
                        self._free_group(gid)
            self._reset_slot(s)
        if m is not None:
            if m.pool_off >= 0:
                self.pool.release(m.pool_off, m.pool_n)
            self.cache.unpin(m.entry)
            if m.job_id is not None and self.rt.pool is not None:
                self.rt.pool.cancel(f"slot:{s}")
            if not keep_meta:
                self.meta.pop(int(s), None)
            else:
                m.pool_off, m.pool_n, m.entry, m.phase, m.next_phase, m.job_id = -1, 0, None, "idle", None, None

    def _free_group(self, gid: int) -> None:
        self.G_active[gid] = 0
        self.G_members.pop(gid, None)
        po = self.G_pool.pop(gid, None)
        if po is not None:
            self.pool.release(*po)
        for k, v in list(self.G_key.items()):
            if v == gid:
                del self.G_key[k]

    def _yaw(self, s: int, yaw: dict | None) -> None:
        y = yaw or {"mode": "lookahead"}
        mode = str(y.get("mode", "lookahead"))
        self.B["yaw_mode"][s] = YAW_CODES.get(mode, KT.Y_LOOKAHEAD)
        if mode in ("center", "axis") and y.get("center_enu_m") is not None:
            c = list(y["center_enu_m"]) + [0.0] * 3
            self.B["yaw_arg"][s] = c[:3]
        elif mode == "fixed":
            self.B["yaw_arg"][s, 0] = float(y.get("fixed_rad", self.B["psi"][s]))

    def load_bspline(self, S: Any, s: int, e: TrajEntry, *, tau0: float = 0.0, rate0: float = 1.0, cid: str | None = None,
                     provider: str | None = None) -> bool:
        m = self.slot_meta(s)
        old_off, old_n = m.pool_off, m.pool_n
        n = len(e.Q)
        off = self.pool.alloc(n)
        if off < 0:
            log.error("control-point pool exhausted", extra={"kv": {"slot": s, "n": n, "used": self.pool.used}})
            return False
        self.pool.Q[off:off + n] = e.Q
        if old_off >= 0:
            self.pool.release(old_off, old_n)
        if m.entry is not None and m.entry is not e:
            self.cache.unpin(m.entry)
        if m.entry is not e:
            self.cache.pin(e)
        B = self.B
        self._reset_slot(s)
        B["kind"][s] = KT.K_BSPLINE
        B["off"][s] = off
        B["nseg"][s] = n - 3
        B["ts"][s] = e.ts_s
        B["tau"][s] = float(tau0)
        B["rate"][s] = float(rate0)
        B["traj_id"][s] = e.traj_id
        B["psi"][s] = float(self.rt.psi_enu(S, s))
        self._yaw(s, e.yaw)
        m.pool_off, m.pool_n, m.entry, m.phase = off, n, e, "bspline"
        m.cid = cid or m.cid
        m.provider = provider or m.provider
        m.t_load_ns = int(S.t_ns)
        m.path_rev += 1
        self.stats["loads"] += 1
        self.rt.publish_path(s, e, m.path_rev)
        return True

    def load_formation(self, S: Any, s: int, e: TrajEntry, *, cid: str | None = None, provider: str | None = None) -> bool:
        g = e.group or {}
        key = str(g.get("gid", e.key))
        gid = self.G_key.get(key)
        if gid is None:
            free = np.flatnonzero(self.G_active == 0)
            if free.size == 0:
                return False
            gid = int(free[0])
            aQ = e.anchor_Q if e.anchor_Q is not None else e.Q
            off = self.pool.alloc(len(aQ))
            if off < 0:
                return False
            self.pool.Q[off:off + len(aQ)] = aQ
            self.G_pool[gid] = (off, len(aQ))
            self.G_key[key] = gid
            self.G_off[gid] = off
            self.G_nseg[gid] = len(aQ) - 3
            self.G_ts[gid] = e.anchor_ts
            self.G_tau[gid] = 0.0
            self.G_w[gid] = 0.0
            self.G_psi[gid] = float(g.get("psi0_rad", 0.0))
            mode = str(g.get("heading_mode", "filtered"))
            self.G_taupsi[gid] = max(1.0, float(g.get("tau_psi_s", 2.0))) if mode == "filtered" else (
                -1.0 if mode == "aligned" else 0.0)
            off_all = np.asarray(g.get("slots_r_max", 0.0), np.float64)
            r_max = float(off_all) if off_all.ndim == 0 else 0.0
            self.G_wmax[gid] = 3.0 / max(e.source.get("v_mps", 5.0) + r_max * 0.2, 1e-3) if r_max else 0.6
            self.G_active[gid] = 1
            self.G_members[gid] = set()
        m = self.slot_meta(s)
        if m.pool_off >= 0:
            self.pool.release(m.pool_off, m.pool_n)
            m.pool_off, m.pool_n = -1, 0
        if m.entry is not e:
            self.cache.unpin(m.entry)
            self.cache.pin(e)
        B = self.B
        self._reset_slot(s)
        B["kind"][s] = KT.K_FORM
        B["group"][s] = gid
        B["slot_off"][s] = np.asarray(g.get("slot_off_flu", [0, 0, 0]), np.float64)[:3]
        B["traj_id"][s] = e.traj_id
        B["psi"][s] = float(self.rt.psi_enu(S, s))
        self._yaw(s, {"mode": "path"})
        self.G_members[gid].add(int(s))
        m.entry, m.phase, m.gid = e, "formation", gid
        m.cid = cid or m.cid
        m.provider = provider or m.provider
        m.path_rev += 1
        self.rt.publish_path(s, e, m.path_rev)
        return True

    def start_brake(self, S: Any, s: int) -> np.ndarray:
        """按 a_brake 刹停到静止点（等待规划结果）；返回静止点（ENU）。"""
        p = np.asarray(S.enu.pos[s], np.float64).copy()
        v = np.asarray(S.enu.vel[s], np.float64).copy()
        B = self.B
        m = self.slot_meta(s)
        if m.pool_off >= 0:
            self.pool.release(m.pool_off, m.pool_n)
            m.pool_off, m.pool_n = -1, 0
        self.cache.unpin(m.entry)
        m.entry = None
        self._reset_slot(s)
        B["kind"][s] = KT.K_BRAKE
        B["orb"][s] = 0.0
        B["orb"][s, 0:3] = p
        B["orb"][s, 3:6] = v
        B["psi"][s] = float(self.rt.psi_enu(S, s))
        B["yaw_mode"][s] = KT.Y_NONE
        m.phase = "brake"
        sp = float(np.linalg.norm(v))
        return p + (v * sp / (2.0 * A_BRAKE) if sp > 1e-9 else 0.0)

    def start_line(self, S: Any, s: int, a: np.ndarray, b: np.ndarray, v_mps: float, yaw: dict | None = None,
                   vz_up: float = 3.0, vz_dn: float = 1.5, a_line: float = A_TAN) -> None:
        """直线段：梯形速度剖面（静止起止），峰值速度受水平、竖直限速约束；时间受 time_stretch 缩放。"""
        a = np.asarray(a, np.float64)
        b = np.asarray(b, np.float64)
        d = b - a
        L = float(np.linalg.norm(d))
        u = d / L if L > 1e-9 else np.zeros(3)
        uxy = float(np.hypot(u[0], u[1]))
        vz = vz_up if u[2] > 0 else vz_dn
        v = min(v_mps / uxy if uxy > 1e-9 else math.inf, vz / abs(float(u[2])) if abs(float(u[2])) > 1e-9 else math.inf)
        v = max(min(v, math.sqrt(max(L, 1e-9) * a_line)), 0.1)
        T = L / v + v / a_line if L > 1e-9 else 0.0
        B = self.B
        m = self.slot_meta(s)
        if m.pool_off >= 0:
            self.pool.release(m.pool_off, m.pool_n)
            m.pool_off, m.pool_n = -1, 0
        self._reset_slot(s)
        B["kind"][s] = KT.K_LINE
        B["orb"][s] = 0.0
        B["orb"][s, 0:3] = a
        B["orb"][s, 3:6] = b
        B["orb"][s, 7] = T
        B["orb"][s, 8] = v
        B["psi"][s] = float(self.rt.psi_enu(S, s))
        self._yaw(s, yaw or {"mode": "path"})
        m.phase = "line"
        m.path_rev += 1
        pts = np.c_[np.stack([a, b]), [0.0, T]]
        self.rt.publish_path_pts(s, pts, m.path_rev, 0)

    def start_orbit(self, S: Any, s: int, center: np.ndarray, R: float, v_mps: float, turns: float, cw: bool,
                    yaw_behavior: str = "center", theta0: float | None = None) -> None:
        B = self.B
        m = self.slot_meta(s)
        if m.pool_off >= 0:
            self.pool.release(m.pool_off, m.pool_n)
            m.pool_off, m.pool_n = -1, 0
        c = np.asarray(center, np.float64)
        p = np.asarray(S.enu.pos[s], np.float64)
        th = math.atan2(p[1] - c[1], p[0] - c[0]) if theta0 is None else float(theta0)
        self._reset_slot(s)
        B["kind"][s] = KT.K_ORBIT
        B["orb"][s] = [c[0], c[1], c[2], R, 0.0, v_mps / R, th, 0.0, A_TAN / R]
        B["orb_dir"][s] = -1 if cw else 1
        B["orb_turns"][s] = float(turns)
        B["psi"][s] = float(self.rt.psi_enu(S, s))
        if yaw_behavior == "tangent":
            B["yaw_mode"][s] = KT.Y_PATH
        elif yaw_behavior == "fixed":
            B["yaw_mode"][s] = KT.Y_FIXED
            B["yaw_arg"][s, 0] = B["psi"][s]
        else:
            B["yaw_mode"][s] = KT.Y_POINT
            B["yaw_arg"][s] = c
        m.phase = "orbit"
        m.path_rev += 1
        n = 73
        ang = th + (-1 if cw else 1) * np.linspace(0.0, 2 * math.pi, n)
        pts = np.c_[c[0] + R * np.cos(ang), c[1] + R * np.sin(ang), np.full(n, c[2]),
                    np.linspace(0.0, 2 * math.pi * R / max(v_mps, 0.1), n)]
        self.rt.publish_path_pts(s, pts, m.path_rev, 0)

    # ------------------------------------------------------------ stage（order 027、every 2、phase 0）
    def stage(self, S: Any, ctx: Any) -> None:
        B = self.B
        if B is None:
            return
        self.stats["calls"] += 1
        kind = B["kind"]
        idx = np.flatnonzero(kind != 0)
        if idx.size == 0:
            return
        traj = S.ctrl_mode[idx] == M_TRAJ
        idx = idx[traj].astype(np.int32)
        if idx.size == 0:
            return
        dt = ctx.dt_tick * 2.0
        pe = S.enu.pos
        self.kernel(idx, kind, B["off"], B["nseg"], B["ts"], B["tau"], B["rate"], B["rate_tgt"], B["rate_dot"],
                    B["yaw_mode"], B["yaw_arg"], B["psi"], B["group"], B["orb"], B["orb_dir"], B["orb_turns"],
                    B["slot_off"], B["done"], self.pool.Q, self.G_tau, self.G_fmin, self.G_psi, self.G_w, self.G_off,
                    self.G_nseg, self.G_ts, self.G_wmax, self.G_taupsi, self.G_active, pe, dt, EMAX_XY, EMAX_Z,
                    A_BRAKE, 1.0, A_TAN, B["out_p"], B["out_v"], B["out_a"], B["out_psi"])
        # 输出按 slot 行直接写入（不先花式下标拷贝四个 N 行数组，与 set_traj_enu 逐位相同；FX2-R3）
        ACT.set_traj_enu_rows(S, idx, B["out_p"], B["out_v"], B["out_a"], B["out_psi"])
        fin = idx[B["done"][idx] != 0]
        if fin.size:
            self._on_done(S, ctx, fin)
        self._hold_paused(S, ctx, idx)

    def _on_done(self, S: Any, ctx: Any, fin: np.ndarray) -> None:
        B = self.B
        hand: list[int] = []
        for s in fin.tolist():
            m = self.slot_meta(s)
            B["done"][s] = 0
            k = int(B["kind"][s])
            if k == KT.K_BRAKE and m.next_phase is None:
                B["kind"][s] = KT.K_BRAKE     # 继续保持在静止点，等待规划结果
                B["orb"][s, 6] = 1e9
                B["done"][s] = 0
                continue
            # 刹停后接续的下一段（例如 goto route=auto 且粗校验已证明直线安全：先刹停再走直线段；FX-SIM2：此前刹停段
            # 结束后一直停在静止点，调用以 202 截止失败）
            nxt = m.next_phase
            if nxt is not None:
                m.next_phase = None
                if nxt["phase"] == "orbit":
                    o = nxt["args"]
                    self.start_orbit(S, s, o["center"], o["R"], o["v"], o["turns"], o["cw"], o["yaw"], o.get("theta0"))
                    continue
                if nxt["phase"] == "line":
                    o = nxt["args"]
                    self.start_line(S, s, B["out_p"][s].copy(), o["b"], o["v"], o.get("yaw"))
                    m.next_phase = o.get("then")
                    continue
                if nxt["phase"] == "bspline" and nxt.get("entry") is not None:
                    self.load_bspline(S, s, nxt["entry"], cid=m.cid)
                    m.next_phase = nxt.get("then")
                    continue
            hand.append(s)
        if hand:
            h = np.asarray(hand, np.int64)
            z = np.zeros((h.size, 3))
            ACT.set_traj_enu(S, h, B["out_p"][h], z, z)            # 静止点移交：前馈清零
            ACT.begin_hold(S, h, ctx.t_ns * 1e-9)
            S.mode_evt[h] |= EVT_ARRIVED
            for s in hand:
                m = self.slot_meta(s)
                cid = m.cid
                grp = int(B["group"][s]) >= 0
                fin_p = B["out_p"][s].copy()
                self.release(s, keep_meta=True)
                self.stats["handovers"] += 1
                self.rt.on_slot_finished(s, cid, fin_p if grp else None)

    # ------------------------------------------------------------ 任务级暂停（FR-007：时钟斜坡停在轨迹上）
    def pause_slot(self, s: int) -> bool:
        """rate_tgt = 0：以 a_brake 的时钟斜坡停在轨迹上；停稳后交回 HOLD（目标 = 轨迹上的停止点），调用保持在途。"""
        m = self.meta.get(int(s))
        if self.B is None or m is None or int(self.B["kind"][s]) not in (KT.K_BSPLINE, KT.K_FORM):
            return False
        self.B["rate_tgt"][s] = 0.0
        m.phase_before_pause = m.phase
        m.paused = True
        self._paused_slots.add(int(s))
        return True

    def resume_slot(self, S: Any, s: int, t_s: float) -> bool:
        m = self.meta.get(int(s))
        if self.B is None or m is None or not m.paused:
            return False
        m.paused = False
        self._paused_slots.discard(int(s))
        self.B["rate_tgt"][s] = 1.0
        if int(S.ctrl_mode[s]) != M_TRAJ:
            ACT.begin_traj(S, np.array([s], np.int64), t_s)
            self.B["rate"][s] = 0.0
            self.B["rate_dot"][s] = 0.0
        return True

    def _hold_paused(self, S: Any, ctx: Any, idx: np.ndarray) -> None:
        # 没有仍处于任务级暂停的机体时不筛选（只有 pause_slot 置 paused；其登记的 slot 中已恢复或已释放的不算）：
        # 此前大机群每 125 Hz 对全部在跟踪机体做三次花式下标（FX-SIM1 只省掉了小机群的情形；FX2-R3）
        ps = self._paused_slots
        if not ps:
            return
        if not any(getattr(self.meta.get(s_), "paused", False) for s_ in ps):
            ps.clear()
            return
        B = self.B
        stop = idx[(B["rate"][idx] <= 0.0) & (B["rate_tgt"][idx] <= 0.0)]
        if stop.size == 0:
            return
        h = [int(s) for s in stop if getattr(self.meta.get(int(s)), "paused", False)]
        if h:
            hh = np.asarray(h, np.int64)
            z = np.zeros((hh.size, 3))
            ACT.set_traj_enu(S, hh, B["out_p"][hh], z, z)
            ACT.begin_hold(S, hh, ctx.t_ns * 1e-9)

    # ------------------------------------------------------------ 暂停保存与恢复
    def save_for_resume(self, s: int, cid: str) -> None:
        m = self.meta.get(int(s))
        if m is None or self.B is None:
            return
        k = int(self.B["kind"][s])
        st: dict = {"phase": m.phase, "entry": m.entry, "tau": float(self.B["tau"][s]), "provider": m.provider}
        if k in (KT.K_ORBIT, KT.K_LINE):
            st["orb"] = self.B["orb"][s].copy()
            st["orb_dir"] = int(self.B["orb_dir"][s])
            st["orb_turns"] = float(self.B["orb_turns"][s])
            st["yaw_mode"] = int(self.B["yaw_mode"][s])
            st["yaw_arg"] = self.B["yaw_arg"][s].copy()
        st["next_phase"] = m.next_phase
        if m.entry is not None:
            self.cache.pin(m.entry)
        self.suspended[cid] = st

    def restore(self, S: Any, s: int, cid: str) -> bool:
        st = self.suspended.pop(cid, None)
        if st is None:
            return False
        e = st.get("entry")
        ok = True
        if st["phase"] == "bspline" and e is not None:
            ok = self.load_bspline(S, s, e, tau0=st["tau"], rate0=0.0, cid=cid, provider=st.get("provider"))
            self.cache.unpin(e)
        elif st["phase"] in ("orbit", "line") and "orb" in st:
            B = self.B
            self._reset_slot(s)
            B["kind"][s] = KT.K_ORBIT if st["phase"] == "orbit" else KT.K_LINE
            B["orb"][s] = st["orb"]
            if st["phase"] == "orbit":
                B["orb"][s, 4] = 0.0          # 从 ω = 0 以角加速度斜坡恢复
            B["orb_dir"][s] = st["orb_dir"]
            B["orb_turns"][s] = st["orb_turns"]
            B["yaw_mode"][s] = st["yaw_mode"]
            B["yaw_arg"][s] = st["yaw_arg"]
            m = self.slot_meta(s)
            m.phase, m.cid, m.provider = st["phase"], cid, st.get("provider")
            if e is not None:
                self.cache.unpin(e)
        else:
            ok = False
        m = self.slot_meta(s)
        m.next_phase = st.get("next_phase")
        return ok

    def drop_suspended(self, cid: str) -> None:
        st = self.suspended.pop(cid, None)
        if st is not None and st.get("entry") is not None:
            self.cache.unpin(st["entry"])

    def reset(self) -> None:
        if self.B is not None:
            self.B["kind"][:] = 0
            self.B["group"][:] = -1
            self.B["mission_item"][:] = 0xFFFF
            self.B["track_state"][:] = 0
        self.meta.clear()
        self.suspended.clear()
        self.pool = CtrlPool(len(self.pool.Q))
        self.G_active[:] = 0
        self.G_members.clear()
        self.G_key.clear()
        self.G_pool.clear()


# ------------------------------------------------------------------------------------------------ 运动提供者
class _ProviderBase:
    name = "m10"
    ops: tuple[str, ...] = ()
    sub = "PATH"

    def __init__(self, rt: M10Runtime) -> None:
        self.rt = rt

    def cancel(self, cid: str, slots: np.ndarray) -> None:
        tr = self.rt.tracker
        for s in np.atleast_1d(slots).tolist():
            m = tr.meta.get(int(s))
            if m is None or (m.cid is not None and m.cid != cid):
                continue
            tr.save_for_resume(int(s), cid)
            tr.release(int(s))
        self.rt.cancel_jobs_for(cid)


class FollowPathProvider(_ProviderBase):
    name = "m10.follow_path"
    ops = ("follow_path",)

    def start(self, call: Any, slots: np.ndarray, args: dict, apply_tick: int) -> str:
        rt = self.rt
        if not rt.bound:
            rt.pending_starts.append((self, call, slots, args, apply_tick))
            return self.sub
        S = rt.S
        for s in np.atleast_1d(slots).tolist():
            if rt.tracker.restore(S, s, call.cid):
                continue
            bs = args.get("bspline") if isinstance(args.get("bspline"), dict) else None
            e = rt.tracker.cache.get(bs.get("traj_id")) if bs else None
            if e is None and bs is not None and bs.get("ctrl_pts") and call.source in ("MISSION", "SCENARIO", "SWARM"):
                try:
                    Q = np.asarray(bs["ctrl_pts"], np.float64)
                    if Q.ndim == 2 and Q.shape[1] == 3 and 4 <= len(Q) <= 4096 and 0.05 <= float(bs["ts_s"]) <= 5.0:
                        e = rt.tracker.cache.put_raw(str(bs.get("traj_id") or f"ext:{call.cid}"), Q, float(bs["ts_s"]),
                                                     args.get("yaw"))
                except (TypeError, ValueError, KeyError):
                    e = None
            if e is not None:
                if e.kind == "formation":
                    rt.tracker.load_formation(S, s, e, cid=call.cid, provider=self.name)
                else:
                    rt.tracker.load_bspline(S, s, e, cid=call.cid, provider=self.name)
                rt.extend_deadline(call.cid, e.duration_s)
                continue
            # 缓存未命中：刹停，提交 follow_path 作业（圆角、TOPP-lite、B-spline、细校验），调用停在 accepted
            p_stop = rt.tracker.start_brake(S, s)
            W = np.vstack([p_stop[None], np.asarray(args["waypoints"], np.float64)])
            rt.plan_for_call(call, s, "follow_path", {"waypoints": W, "speed_mps": args.get("speed_mps"),
                                                      "check_input": True}, W)
        return "PATH"


class GotoRouteProvider(_ProviderBase):
    name = "m10.goto_route"
    ops = ("goto:route!=direct",)

    def start(self, call: Any, slots: np.ndarray, args: dict, apply_tick: int) -> str:
        rt = self.rt
        if not rt.bound:
            rt.pending_starts.append((self, call, slots, args, apply_tick))
            return self.sub
        S = rt.S
        goal = np.asarray(args["pos"], np.float64)
        route = str(args.get("route", "auto"))
        for s in np.atleast_1d(slots).tolist():
            if rt.tracker.restore(S, s, call.cid):
                continue
            p = np.asarray(S.enu.pos[s], np.float64)
            v = float(np.linalg.norm(S.enu.vel[s]))
            speed = rt.speed_for(s, args.get("speed_mps"))
            if route == "auto" and rt.coarse_proven(p, goal, s):
                # 粗校验证明无障碍：直线段（M08 原生 GOTO 的等价；§14 第 1 条）
                if v > 0.5:
                    p_stop = rt.tracker.start_brake(S, s)
                    rt.tracker.slot_meta(s).next_phase = {"phase": "line", "args": {"b": goal, "v": speed}}
                    _ = p_stop
                else:
                    rt.tracker.start_line(S, s, p, goal, speed)
                m = rt.tracker.slot_meta(s)
                m.cid, m.provider = call.cid, self.name
                continue
            p_stop = rt.tracker.start_brake(S, s)
            rt.plan_for_call(call, s, "safe_transit", {"start": p_stop, "goal": goal, "speed_mps": speed,
                                                       "check_goal": True}, np.stack([p_stop, goal]))
        return "PATH"


class OrbitProvider(_ProviderBase):
    name = "m10.orbit"
    ops = ("orbit",)
    sub = "ORBIT"

    def start(self, call: Any, slots: np.ndarray, args: dict, apply_tick: int) -> str:
        rt = self.rt
        if not rt.bound:
            rt.pending_starts.append((self, call, slots, args, apply_tick))
            return self.sub
        S = rt.S
        c = np.asarray(args["center"], np.float64)
        R = float(args["radius_m"])
        turns = float(args.get("turns", 0) or 0)
        cw = bool(args.get("cw", True))
        yb = str(args.get("yaw_behavior", "center"))
        for s in np.atleast_1d(slots).tolist():
            if rt.tracker.restore(S, s, call.cid):
                continue
            v = min(rt.speed_for(s, args.get("speed_mps")), math.sqrt(3.0 * R))
            p = np.asarray(S.enu.pos[s], np.float64)
            d = p[:2] - c[:2]
            dist = float(np.hypot(*d))
            th0 = math.atan2(d[1], d[0]) if dist > 0.5 else 0.0
            entry = np.array([c[0] + R * math.cos(th0), c[1] + R * math.sin(th0), c[2]])
            orb = {"phase": "orbit", "args": {"center": c, "R": R, "v": v, "turns": turns, "cw": cw, "yaw": yb,
                                              "theta0": th0}}
            m = rt.tracker.slot_meta(s)
            m.cid, m.provider = call.cid, self.name
            if float(np.linalg.norm(p - entry)) < 1.0 and float(np.linalg.norm(S.enu.vel[s])) < 0.5:
                rt.tracker.start_orbit(S, s, c, R, v, turns, cw, yb, th0)
            elif rt.coarse_proven(p, entry, s):
                # 入圆段按巡航速度飞（受限速配置约束）：环绕速度 v 是圆周切向速度（受 v²/R ≤ 3 与偏航角速度约束，ladder
                # 为 1.2 m/s），不是入场速度；此前取 min(v, 巡航)，ladder 从 10 m AGL 爬到 60–105 m 需要 40–80 s（ADR-070）
                rt.tracker.start_line(S, s, p, entry, rt.speed_for(s, None), {"mode": "path"})
                m.next_phase = orb
            else:
                p_stop = rt.tracker.start_brake(S, s)
                rt.plan_for_call(call, s, "safe_transit", {"start": p_stop, "goal": entry,
                                                           "speed_mps": rt.speed_for(s, None)},
                                 np.stack([p_stop, entry]), then=orb)
        return "ORBIT"
