"""识别物运动（10 Hz；M18-FR-005 的高度规则、FR-006、FR-007；M18-M19 PRD §6.5.1；AWR-04 §7.6）。

- static：不动；
- path：pure pursuit（前视 `max(2 m, 1.5·v)`），到点驻留 `dwell_s[i]`；`loop ∈ {once, pingpong, loop}`；
- random_walk：航向角速度为 OU 过程 `ω ← ω − ω·dt/τ + sigma_w·√(2·dt/τ)·n`（sigma_w = 0.5·turn_rate_max），预测 2 s 位置在区域外时以
  `sign(指向区域内侧)·turn_rate_max` 转向，`ω` 截断于 ±turn_rate_max；速度 `clamp(speed + sigma_v·n2, 0, speed_max)`；以
  `pause_prob` 每秒的概率驻足 2–5 s；仍将越出区域的一步不执行（保证不越界）。
- 台阶绕行（地面类）：前视点 `p + ψ̂·max(1 m, v·dt)` 的 DSM 与当前高度差 > 0.5 m 时，按 ±30°、±60°、±90° 依次找首个可通行航向
  （转向率受限）；都不可通行时本步停下。
- 高度：地面类 `z = height_dsm`；uav 类 `z = max(dsm + 10, dsm + alt_agl_m)`（路径点给出 z 时用该绝对高度）。
- 行为覆盖（`Override`，由 behavior.py 给出）：hold（驻足）、look（驻足并以转向率限制转向某点）、move（朝向或背离某点，
  `flee` 在 random_walk 区域边界处沿边界滑行）。
- 随机数只取流 17 `targets_motion`（计数器式，键 `(seed, 17, target_no, tick, ch)`；ch 0 ω、1 速度、2 驻足判定、3 驻足时长），
  与观察者与订阅无关（ADR-049）。速度变化受 `accel_mps2` 限制。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from . import kernels_motion as KM
from .geometry import wrap_pi

__all__ = ["CH_OMEGA", "CH_PAUSE", "CH_PAUSE_LEN", "CH_SPEED", "DsmGrid", "MotionState", "Override", "default_region", "dsm_of",
           "in_polygon", "initial_z", "polygon_area", "project_into", "step_motion"]

CH_OMEGA, CH_SPEED, CH_PAUSE, CH_PAUSE_LEN = 0, 1, 2, 3
STEP_MAX_M = 0.5
DETOURS = (math.radians(30), -math.radians(30), math.radians(60), -math.radians(60), math.radians(90), -math.radians(90))
UAV_MIN_AGL = 10.0
DEFAULT_REGION_HALF_M = 50.0


def polygon_area(poly: NDArray) -> float:
    x, y = poly[:, 0], poly[:, 1]
    return float(0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def in_polygon(poly: NDArray | list, x: float, y: float) -> bool:
    """射线法（边界上的点按内部处理的近似；区域面积 ≥ 100 m²）。"""
    pts = poly.tolist() if isinstance(poly, np.ndarray) else poly
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def project_into(poly: NDArray, x: float, y: float, inset: float = 0.1) -> tuple[float, float]:
    """多边形外的点 → 边界上最近点再向质心内收 `inset` m（沿边界滑行）。

    逐边在 Python 浮点上计算（与此前逐边 numpy 2 维向量的实现逐位相同：两元点积即 `a0·b0 + a1·b1`，最近点 `a + t·ab`）；质心
    `poly.mean(axis=0)` 按多边形对象缓存，内收方向的长度用 `np.hypot`（与此前相同）。D2-FX-R2-sim 第 2 次：flee 沿边界滑行时每步
    一次，此前约 100 µs/次。"""
    pts, c = _poly_cache(poly)
    best0, best1, bd = x, y, math.inf
    n = len(pts)
    for i in range(n):
        a0, a1 = pts[i]
        b0, b1 = pts[(i + 1) % n]
        ab0, ab1 = b0 - a0, b1 - a1
        L2 = ab0 * ab0 + ab1 * ab1
        t = 0.0 if L2 <= 0 else min(1.0, max(0.0, ((x - a0) * ab0 + (y - a1) * ab1) / L2))
        q0, q1 = a0 + t * ab0, a1 + t * ab1
        d = math.hypot(x - q0, y - q1)
        if d < bd:
            best0, best1, bd = q0, q1, d
    best0, best1 = float(best0), float(best1)
    v0, v1 = c[0] - best0, c[1] - best1
    L = float(np.hypot(v0, v1))
    if L > 1e-9:
        return best0 + v0 / L * inset, best1 + v1 / L * inset
    return best0, best1


_POLY: dict[int, tuple[Any, list, tuple[float, float]]] = {}


def _poly_cache(poly: NDArray) -> tuple[list, tuple[float, float]]:
    """(顶点的 Python 列表, 质心)，按多边形数组对象缓存（区域数组在放置与修改时整体替换，不原地修改）。"""
    hit = _POLY.get(id(poly))
    if hit is not None and hit[0] is poly:
        return hit[1], hit[2]
    P = np.asarray(poly, float)
    c = P.mean(axis=0)
    out = (poly, P.tolist(), (float(c[0]), float(c[1])))
    if len(_POLY) >= 256:
        _POLY.clear()
    _POLY[id(poly)] = out
    return out[1], out[2]


def default_region(x: float, y: float, half: float = DEFAULT_REGION_HALF_M) -> list[list[float]]:
    return [[x - half, y - half], [x + half, y - half], [x + half, y + half], [x - half, y + half]]


@dataclass
class Override:
    """行为对运动的覆盖：kind ∈ hold、look、move；point 为朝向或背离的点；away 表示背离。"""

    kind: str
    point: tuple[float, float] | None = None
    speed: float = 0.0
    away: bool = False
    stop_within_m: float = 0.0     # move 朝向时到达该水平距离内停下（approach dist_m）


def _fprop(i: int, doc: str) -> property:
    def get(self: MotionState) -> float:
        return float(self._v[i])

    def put(self: MotionState, v: float) -> None:
        self._v[i] = v

    return property(get, put, doc=doc)


def _iprop(i: int, doc: str) -> property:
    def get(self: MotionState) -> int:
        return int(self._v[i])

    def put(self: MotionState, v: int) -> None:
        self._v[i] = float(v)

    return property(get, put, doc=doc)


def _bprop(i: int, doc: str) -> property:
    def get(self: MotionState) -> bool:
        return bool(self._v[i] != 0.0)

    def put(self: MotionState, v: bool) -> None:
        self._v[i] = 1.0 if v else 0.0

    return property(get, put, doc=doc)


def _pprop(ix: int, iy: int, ih: int, doc: str) -> property:
    def get(self: MotionState) -> tuple[float, float] | None:
        v = self._v
        return (float(v[ix]), float(v[iy])) if v[ih] != 0.0 else None

    def put(self: MotionState, p: tuple[float, float] | None) -> None:
        v = self._v
        if p is None:
            v[ih] = 0.0
        else:
            v[ix], v[iy], v[ih] = float(p[0]), float(p[1]), 1.0

    return property(get, put, doc=doc)


class MotionState:
    """一个识别物的运动状态。D2-FX-R2-sim 第 2 次：数值字段存放在一行 float64（`kernels_motion` 的 MS 列，`_v`）；运行时把全部
    识别物的行放在同一张表中（按实体行号），运动核直接在表上推进，不再逐字段打包与写回。独立构造时自带一行。字段与取值同此前的
    dataclass（`pos` 每次读取得到一份拷贝，赋值写入该行）。"""

    __slots__ = ("_v", "pc", "pv", "region", "region_id", "region_l")

    def __init__(self, pos: NDArray | None = None, yaw: float = 0.0, speed: float = 0.0, vz: float = 0.0, omega: float = 0.0,
                 pause_until: float = -1.0, seg: int = 0, dirn: int = 1, dwell_until: float = -1.0, path_done: bool = False,
                 odo_m: float = 0.0, region: NDArray | None = None, blocked: bool = False, detour: float = 0.0,
                 flat_c: tuple[float, float] | None = None, flat_r: float = 0.0, flat_z: float = 0.0,
                 flat_fail: tuple[float, float] | None = None, region_l: list | None = None, region_id: int = 0, pc: Any = None,
                 pv: tuple = (), *, row: NDArray | None = None) -> None:
        v = np.zeros(KM.MS_N) if row is None else row
        v[:] = 0.0
        self._v = v
        self.pos = np.zeros(3) if pos is None else pos
        self.yaw, self.speed, self.vz, self.omega, self.pause_until = yaw, speed, vz, omega, pause_until
        self.seg, self.dirn, self.dwell_until, self.path_done, self.odo_m = seg, dirn, dwell_until, path_done, odo_m
        self.blocked, self.detour, self.flat_c, self.flat_r, self.flat_z, self.flat_fail = blocked, detour, flat_c, flat_r, flat_z, flat_fail
        self.region, self.region_l, self.region_id, self.pc, self.pv = region, region_l, region_id, pc, pv

    @property
    def pos(self) -> NDArray:
        return self._v[0:3].copy()

    @pos.setter
    def pos(self, p: Any) -> None:
        self._v[0:3] = p

    def detach(self) -> None:
        """改为自带的行（识别物移出运行时的表后，表中该行可能被新识别物复用）。"""
        self._v = self._v.copy()

    def __repr__(self) -> str:
        return (f"MotionState(pos={self.pos.tolist()}, yaw={self.yaw}, speed={self.speed}, vz={self.vz}, seg={self.seg}, "
                f"odo_m={self.odo_m})")

    yaw = _fprop(KM.S_YAW, "航向（rad）")
    speed = _fprop(KM.S_SPEED, "水平速度（m/s）")
    vz = _fprop(KM.S_VZ, "竖直速度（m/s）")
    omega = _fprop(KM.S_OMEGA, "随机游走的航向角速度（rad/s）")
    pause_until = _fprop(KM.S_PAUSE, "驻足到（仿真 s）")
    seg = _iprop(KM.S_SEG, "路径当前段起点下标")
    dirn = _iprop(KM.S_DIRN, "路径方向（±1）")
    dwell_until = _fprop(KM.S_DWELL, "航点驻留到（仿真 s）")
    path_done = _bprop(KM.S_DONE, "路径已走完（once）")
    odo_m = _fprop(KM.S_ODO, "累计水平里程（flee 距离判定）")
    blocked = _bprop(KM.S_BLOCKED, "本步被台阶挡住")
    detour = _fprop(KM.S_DETOUR, "当前台阶绕行偏角（rad）")
    flat_c = _pprop(KM.S_FCX, KM.S_FCY, KM.S_HASF, "平地缓存圆心（DSM 起伏 ≤ ±0.25 m），None 为无")
    flat_r = _fprop(KM.S_FR, "平地缓存半径")
    flat_z = _fprop(KM.S_FZ, "平地缓存的地面高度")
    flat_fail = _pprop(KM.S_FFX, KM.S_FFY, KM.S_HASFF, "上次未找到平地圆的位置（移动 > 5 m 后再试），None 为无")


class DsmGrid:
    """`DsmWorldQuery` 的有效 DSM 只读栅格（`kernels_motion` 的输入；与 `height_dsm` 缺省口径逐位相同）。"""

    __slots__ = ("a", "cell", "x0", "y0")

    def __init__(self, a: NDArray, x0: float, y0: float, cell: float) -> None:
        self.a, self.x0, self.y0, self.cell = a, float(x0), float(y0), float(cell)

    def at(self, x: float, y: float) -> float:
        return float(KM.dsm_at(self.a, self.x0, self.y0, self.cell, float(x), float(y)))


_DSM: dict[int, tuple[Any, DsmGrid | None]] = {}


def dsm_of(world: Any) -> DsmGrid | None:
    """世界的 DSM 栅格（只对 `DsmWorldQuery` 且栅格为 float32 时给出；其余世界，例如测试的合成世界，返回 None 走 `height_dsm`）。"""
    if world is None or not KM.HAVE_NUMBA:
        return None
    hit = _DSM.get(id(world))
    if hit is not None and hit[0] is world:
        return hit[1]
    g: DsmGrid | None = None
    try:
        from awr.world.geometry.query import DsmWorldQuery

        if getattr(type(world), "height_dsm", None) is DsmWorldQuery.height_dsm:
            gv = world.dsm_grid()
            a = np.asarray(gv.a)
            if a.dtype == np.float32 and a.ndim == 2:
                g = DsmGrid(a, gv.x0_m, gv.y0_m, gv.cell_m)
    except Exception:
        g = None
    if len(_DSM) >= 8:
        _DSM.clear()
    _DSM[id(world)] = (world, g)
    return g


def _ground(world: Any, x: float, y: float, z_fallback: float, dsm: DsmGrid | None = None) -> float:
    if world is None:
        return z_fallback
    if dsm is not None:
        return dsm.at(x, y)
    try:
        return float(world.height_dsm(np.array([[x, y]]))[0])
    except Exception:
        return z_fallback


def _z_for(cls: str, world: Any, x: float, y: float, zcur: float, alt_agl: float, z_abs: float | None,
           dsm: DsmGrid | None = None) -> float:
    g = _ground(world, x, y, zcur if cls != "uav" else zcur - alt_agl, dsm)
    if cls == "uav":
        if z_abs is not None:
            return max(float(z_abs), g + 1.0)
        return max(g + UAV_MIN_AGL, g + alt_agl)
    return g


FLAT_TOL_M = 0.25
FLAT_RADII = (15.0, 6.0)


def _refresh_flat(ms: MotionState, world: Any, g0: float) -> None:
    """以当前位置为圆心找一个 DSM 起伏 ≤ ±0.25 m 的圆（15 m 或 6 m）；圆内的前视与贴地不再逐步查询 DSM（M18-NFR-001）。"""
    x, y = float(ms.pos[0]), float(ms.pos[1])
    for R in FLAT_RADII:
        n = int(R // 1.5)
        xs = np.arange(-n, n + 1) * (R / n)
        gx, gy = np.meshgrid(xs, xs)
        keep = gx * gx + gy * gy <= R * R
        try:
            z = np.asarray(world.height_dsm(np.c_[x + gx[keep], y + gy[keep]]), float)
        except Exception:
            break
        if np.all(np.abs(z - g0) <= FLAT_TOL_M):
            ms.flat_c, ms.flat_r, ms.flat_z = (x, y), R, g0
            return
    ms.flat_c = None


def _in_flat(ms: MotionState, x: float, y: float, extra: float = 0.0) -> bool:
    c = ms.flat_c
    return c is not None and math.hypot(x - c[0], y - c[1]) + extra <= ms.flat_r


def _turn_toward(yaw: float, want: float, rate: float, dt: float) -> float:
    d = wrap_pi(want - yaw)
    lim = rate * dt
    return float(wrap_pi(yaw + max(-lim, min(lim, d))))


def _path_points(m: dict) -> NDArray:
    pts = m.get("path") or []
    return np.array([[float(p[0]), float(p[1]), np.nan if len(p) < 3 or p[2] is None else float(p[2])] for p in pts], float)


def _next_index(ms: MotionState, n: int, loop: str) -> int | None:
    nxt = ms.seg + ms.dirn
    if 0 <= nxt < n:
        return nxt
    if loop == "pingpong":
        ms.dirn = -ms.dirn
        return ms.seg + ms.dirn
    if loop == "loop":
        return 0 if ms.dirn > 0 else n - 1
    return None


def _path_desire(ms: MotionState, m: dict, t_s: float, dt: float) -> tuple[float | None, float, float | None]:
    """路径追踪：返回 (期望航向 | None, 期望速度, 当前目标点的绝对 z | None)。"""
    P = _path_points(m)
    n = len(P)
    v = float(m.get("speed_mps", 1.0))
    if n < 2 or ms.path_done or t_s < ms.dwell_until:
        return None, 0.0, None
    loop = str(m.get("loop", "loop"))
    dw = m.get("dwell_s") or []
    p = ms.pos
    nxt = _next_index(ms, n, loop)
    if nxt is None:
        ms.path_done = True
        return None, 0.0, None
    b = P[nxt]
    a0 = P[ms.seg]
    ab0 = b[:2] - a0[:2]
    L0 = float(np.hypot(*ab0))
    passed = L0 > 1e-9 and float((p[:2] - a0[:2]) @ ab0) / L0 >= L0 - 0.5   # 越过航点（转弯半径大于到点半径时）
    if passed or math.hypot(b[0] - p[0], b[1] - p[1]) <= max(1.0, 1.5 * max(ms.speed, v) * dt):
        ms.seg = nxt
        if nxt < len(dw) and float(dw[nxt]) > 0.0:
            ms.dwell_until = t_s + float(dw[nxt])
            return None, 0.0, None
        nxt = _next_index(ms, n, loop)
        if nxt is None:
            ms.path_done = True
            return None, 0.0, None
        b = P[nxt]
    a = P[ms.seg]
    ab = b[:2] - a[:2]
    L = float(np.hypot(*ab))
    stop_at_b = (nxt < len(dw) and float(dw[nxt]) > 0.0) or (loop == "once" and nxt == n - 1) or \
        (loop == "pingpong" and nxt in (0, n - 1))
    if stop_at_b:  # 在需要停下的航点前按加速度上限提前减速
        acc = float(m.get("accel_mps2", 1.0)) or 1.0
        v = min(v, math.sqrt(2.0 * acc * max(math.hypot(b[0] - p[0], b[1] - p[1]) - 0.5, 0.0)) + 0.2)
    look = max(2.0, 1.5 * max(ms.speed, v))
    if L > 1e-9:
        u = ab / L
        s2 = min(L, max(0.0, float((p[:2] - a[:2]) @ u)) + look)
        tgt = a[:2] + u * s2
    else:
        tgt = b[:2]
    return math.atan2(tgt[1] - p[1], tgt[0] - p[0]), v, None if math.isnan(b[2]) else float(b[2])


def _params(ms: MotionState, cfg: dict) -> tuple:
    """配置派生的运动参数（按配置对象缓存；取值与转换同逐步读取）：(cls, motion, mode, turn_max, vmax, acc, alt, tau, v0, sigma_v,
    pause_prob)。"""
    if ms.pc is cfg:
        return ms.pv
    m = cfg.get("motion") or {}
    pv = (cfg["class"], m, m.get("mode", "static"), float(m.get("turn_rate_max_rad_s", 1.0)),
          float(m.get("speed_max_mps", m.get("speed_mps", 0.0))), float(m.get("accel_mps2", 1.0)) or 1.0, float(cfg.get("alt_agl_m", 50.0)),
          float(m.get("turn_tau_s", 4.0)), float(m.get("speed_mps", 1.0)), float(m.get("speed_sigma_mps", 0.0)),
          float(m.get("pause_prob", 0.0)))
    ms.pc, ms.pv = cfg, pv
    return pv


def step_motion(ms: MotionState, cfg: dict, dt: float, t_s: float, world: Any, noise: NDArray, unif: NDArray,
                ov: Override | None = None, *, base_active: bool = True) -> None:
    """一步运动（原地更新 ms）。noise = [n_ω, n_v]（N(0,1)），unif = [u_pause, u_len]（U[0,1)）。DSM 查询在 `DsmWorldQuery` 上走
    `kernels_motion`（与 `height_dsm` 逐位相同），其余世界走 `height_dsm`。"""
    cls, m, mode, turn_max, vmax, acc, alt, tau, v0, sig_v, pause_p = _params(ms, cfg)
    if ov is None and (mode == "static" or not base_active) and ms.speed == 0.0:
        ms.vz = 0.0
        return   # 静止且无行为覆盖：本步无事可做
    # 位置分量以 Python 浮点参与运算（与 np.float64 逐位相同，免去 numpy 标量开销）
    px0, py0, z0 = float(ms.pos[0]), float(ms.pos[1]), float(ms.pos[2])
    want_yaw: float | None = None
    want_v = 0.0
    z_abs: float | None = None
    region = None
    if mode == "random_walk" and ms.region is not None:
        if ms.region_id != id(ms.region):
            ms.region_l, ms.region_id = ms.region.tolist(), id(ms.region)
        region = ms.region_l
    if ov is not None:
        if ov.kind in ("look", "move") and ov.point is not None:
            dx, dy = ov.point[0] - px0, ov.point[1] - py0
            brg = math.atan2(dy, dx)
            if ov.kind == "look":
                want_yaw = brg
            else:
                want_yaw = float(wrap_pi(brg + math.pi)) if ov.away else brg
                want_v = min(ov.speed, max(vmax, ov.speed))
                if not ov.away:  # 朝向某点：按加速度上限提前减速，到 stop_within_m 内停下
                    gap = math.hypot(dx, dy) - ov.stop_within_m
                    want_v = 0.0 if gap <= 0.0 else min(want_v, math.sqrt(2.0 * acc * gap) + 0.05)
        elif ov.kind == "move" and ov.point is None:
            want_v = 0.0
    elif not base_active or mode == "static":
        pass
    elif mode == "random_walk":
        sig_w = 0.5 * turn_max
        ms.omega = ms.omega - ms.omega * dt / tau + sig_w * math.sqrt(2.0 * dt / tau) * float(noise[0])
        want_v = min(max(v0 + sig_v * float(noise[1]), 0.0), vmax)
        if t_s < ms.pause_until:
            want_v = 0.0
        elif pause_p > 0.0 and float(unif[0]) < pause_p * dt:
            ms.pause_until = t_s + 2.0 + 3.0 * float(unif[1])
            want_v = 0.0
        if region is not None:
            vp = max(want_v, ms.speed, 0.5)
            px = px0 + math.cos(ms.yaw) * vp * 2.0
            py = py0 + math.sin(ms.yaw) * vp * 2.0
            if not in_polygon(region, px, py):
                c = _poly_cache(ms.region)[1]
                inward = math.atan2(c[1] - py0, c[0] - px0)
                ms.omega = math.copysign(turn_max, float(wrap_pi(inward - ms.yaw)) or 1.0)
        ms.omega = max(-turn_max, min(turn_max, ms.omega))
        want_yaw = float(wrap_pi(ms.yaw + ms.omega * dt))
    elif mode == "path":
        want_yaw, want_v, z_abs = _path_desire(ms, m, t_s, dt)
        want_v = min(want_v, vmax) if vmax > 0 else want_v
    # 台阶绕行（地面类）：期望航向在前视 far 内不可通行时取一个持续的绕行偏角（同侧优先，沿障碍边缘走），可通行后回到 0
    ground = cls != "uav" and world is not None
    dsm = dsm_of(world) if world is not None else None
    near = max(1.0, max(ms.speed, want_v) * dt)
    far = max(3.0, 2.0 * max(ms.speed, want_v))

    if world is not None and want_v > 0.0 and not _in_flat(ms, px0, py0, 0.5 * ms.flat_r):
        fp = ms.flat_fail
        if fp is None or math.hypot(px0 - fp[0], py0 - fp[1]) > 5.0:
            g0 = z0 if ground else _ground(world, px0, py0, z0, dsm)
            _refresh_flat(ms, world, g0)
            ms.flat_fail = None if ms.flat_c is not None else (px0, py0)

    def passable(yy: float, look: float) -> bool:
        if _in_flat(ms, px0 + math.cos(yy) * look, py0 + math.sin(yy) * look):
            return True
        if dsm is not None:
            return bool(KM.passable(dsm.a, dsm.x0, dsm.y0, dsm.cell, px0, py0, float(yy), float(look), z0, STEP_MAX_M))
        n = max(1, math.ceil(look))
        d = np.linspace(look / n, look, n)
        xy = np.c_[px0 + np.cos(yy) * d, py0 + np.sin(yy) * d]
        try:
            z = np.asarray(world.height_dsm(xy), float)
        except Exception:
            return True
        return bool(np.all(np.abs(z - z0) <= STEP_MAX_M))

    free_yaw: float | None = None   # 已确认 far 内可通行的航向（与本步航向相同时免去 near 检查）
    if ground and want_yaw is not None and want_v > 0.0:
        if passable(want_yaw, far):
            ms.detour = 0.0
            free_yaw = want_yaw
        else:
            if ms.detour == 0.0 or not passable(want_yaw + ms.detour, far):
                order = DETOURS if ms.detour == 0.0 else \
                    tuple(sorted(DETOURS, key=lambda d: (math.copysign(1.0, d) != math.copysign(1.0, ms.detour), abs(d))))
                pick = next((d for d in order if passable(want_yaw + d, far)), None)
                if pick is None:
                    pick = next((d for d in order if passable(want_yaw + d, near)), None)
                ms.detour = 0.0 if pick is None else pick
                if pick is None and not passable(want_yaw, near):
                    want_v = 0.0
            want_yaw = float(wrap_pi(want_yaw + ms.detour))
    # 航向（转向率受限）与速度（加速度受限）
    if want_yaw is not None:
        ms.yaw = _turn_toward(ms.yaw, want_yaw, turn_max, dt)
    dv = want_v - ms.speed
    ms.speed = max(0.0, ms.speed + max(-acc * dt, min(acc * dt, dv)))
    if ms.speed <= 1e-6:
        ms.speed = 0.0
        ms.vz = 0.0
        ms.blocked = want_v == 0.0 and ground and want_yaw is not None and not passable(ms.yaw, near)
        return
    step = ms.speed * dt
    yaw = ms.yaw
    if ground and not (free_yaw is not None and abs(wrap_pi(yaw - free_yaw)) < 1e-9) and not passable(yaw, near):
        ms.blocked = True   # 当前朝向仍被挡：本步原地转向
        ms.vz = 0.0
        return
    ms.blocked = False
    nx, ny = px0 + math.cos(yaw) * step, py0 + math.sin(yaw) * step
    if region is not None and not in_polygon(region, nx, ny):
        if ov is not None and ov.kind == "move":
            nx, ny = project_into(ms.region, nx, ny)  # flee 沿边界滑行
        else:
            ms.omega = math.copysign(turn_max, ms.omega or 1.0)
            return
    if z_abs is None and _in_flat(ms, nx, ny):
        nz = z0 if ground else max(ms.flat_z + UAV_MIN_AGL, ms.flat_z + alt)
    else:
        nz = _z_for(cls, world, nx, ny, z0, alt, z_abs, dsm)
    ms.odo_m += math.hypot(nx - px0, ny - py0)
    ms.vz = (nz - z0) / dt
    ms.pos = np.array([nx, ny, nz])


def initial_z(cls: str, world: Any, x: float, y: float, z: float | None, alt_agl: float) -> float:
    if cls == "uav":
        return _z_for("uav", world, x, y, (z or 0.0) + alt_agl, alt_agl, None)
    if world is None:
        return float(z if z is not None else 0.0)
    return _ground(world, x, y, float(z or 0.0))

