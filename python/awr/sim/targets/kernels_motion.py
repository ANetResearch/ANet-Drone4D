"""识别物运动的 DSM 取值核（D2-FX-R2-sim 第 2 次；M18-NFR-001；D2-AC-08）。

D2 验收第 1 轮（2026-10-11）4.1：沙盒真实组合（sandbox100、10 架、30 识别物、`--perception full`）中识别物 stage 233 ms/仿真秒，
其中约 3/4 是地面类识别物的台阶判定 `passable`（每步 1–8 次）与贴地 `_ground`：每次经 `WorldQuery.height_dsm` 做一次 numpy 最近格
查询（`np.linspace`、`np.c_`、`fidx`/`idx` 的 `floor`、`clip` 与 memmap 花式下标），约 150–300 µs/次。

本模块在 DSM 的只读 float32 栅格上做同一计算：

- `dsm_at`：与 `DsmWorldQuery.height_dsm`（缺省口径：`eff` 栅格、界外钳制）逐位相同——格下标 `floor((x − x0)/cell)` 钳制到
  `[0, w − 1]`、`[0, h − 1]`，取 float32 值；
- `passable`：与 `motion.step_motion` 内的 numpy 实现逐位相同——前视距离 `d = np.linspace(look/n, look, n)`（`n = max(1, ⌈look⌉)`；
  `d_i = i·step + look/n`，`step = (look − look/n)/(n − 1)`，末项取 `look`）、采样点 `p + cos(ψ)·d`、`|z − z0| ≤ step_max` 全部满足。

numba 不可用时这些函数按普通 Python 执行（结果相同，只损失性能）；调用方在世界不是 `DsmWorldQuery`（测试的合成世界）时走原
`height_dsm` 路径。
"""

from __future__ import annotations

import math
import os

import numpy as np

try:
    from numba import njit

    HAVE_NUMBA = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover
    HAVE_NUMBA = False

    def njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f

        return deco(a[0]) if a and callable(a[0]) else deco

__all__ = ["HAVE_NUMBA", "MS_N", "OV_N", "PR_N", "dsm_at", "geo_hits", "inside_all", "passable", "py_hypot", "step_all", "warmup"]

# ---- step_all 的打包列（runtime 与本模块共用）
# 运动状态 MS[k, :]
S_X, S_Y, S_Z, S_YAW, S_SPEED, S_VZ, S_OMEGA, S_PAUSE, S_SEG, S_DIRN, S_DWELL, S_DONE, S_ODO, S_BLOCKED, S_DETOUR = range(15)
S_FCX, S_FCY, S_HASF, S_FR, S_FZ, S_FFX, S_FFY, S_HASFF = range(15, 23)
MS_N = 23
# 参数 PR[k, :]
P_GROUND, P_MODE, P_TURN, P_VMAX, P_ACC, P_ALT, P_TAU, P_V0, P_SIGV, P_PAUSEP, P_LOOP, P_POFF, P_PN, P_ROFF, P_RN, P_RCX, P_RCY, P_UAV = \
    range(18)
PR_N = 18
MODE_STATIC, MODE_RW, MODE_PATH = 0, 1, 2
LOOP_LOOP, LOOP_PINGPONG, LOOP_ONCE = 0, 1, 2
# 行为覆盖 OV[k, :]
O_KIND, O_HASPT, O_PX, O_PY, O_SPEED, O_AWAY, O_STOP = range(7)
OV_N = 7
OV_NONE, OV_HOLD, OV_LOOK, OV_MOVE = 0, 1, 2, 3
STEP_MAX_M = 0.5
UAV_MIN_AGL = 10.0
FLAT_TOL_M = 0.25
_DBL_MIN = 2.2250738585072014e-308
_R30, _R60, _R90 = math.radians(30), math.radians(60), math.radians(90)
_DET0 = np.array([_R30, -_R30, _R60, -_R60, _R90, -_R90])          # motion.DETOURS
_DETP = np.array([_R30, _R60, _R90, -_R30, -_R60, -_R90])          # 当前偏角为正时的次序（同侧优先，按幅度）
_DETN = np.array([-_R30, -_R60, -_R90, _R30, _R60, _R90])


@njit(cache=True, fastmath=False)
def dsm_at(a, x0, y0, cell, x, y):
    """最近格 DSM（界外钳制；float32 值转 float64）。"""
    h, w = a.shape
    fx = (x - x0) / cell
    fy = (y - y0) / cell
    c = 0
    r = 0
    if fx == fx:
        fc = math.floor(fx)
        if fc >= w:
            c = w - 1
        elif fc > 0.0:
            c = int(fc)
    if fy == fy:
        fr = math.floor(fy)
        if fr >= h:
            r = h - 1
        elif fr > 0.0:
            r = int(fr)
    return float(a[r, c])


@njit(cache=True, fastmath=False)
def passable(a, x0, y0, cell, px, py, yy, look, z0, step_max):
    """沿航向 yy 前视 look m 的 n 个采样点（n = max(1, ⌈look⌉)）与 z0 的 DSM 高差均 ≤ step_max。"""
    n = max(1, math.ceil(look))
    start = look / n
    step = (look - start) / (n - 1) if n > 1 else 0.0
    cy = math.cos(yy)
    sy = math.sin(yy)
    for i in range(n):
        d = look if (n > 1 and i == n - 1) else (float(i) * step + start)
        z = dsm_at(a, x0, y0, cell, px + cy * d, py + sy * d)
        if not (abs(z - z0) <= step_max):
            return False
    return True


@njit(cache=True, fastmath=False)
def geo_hits(IN, starts, ends, ORD, QQ, CNT):
    """geometric 判据的命中次序（`TargetRuntime._evaluate_geo`）：识别物 k 的体行为 [starts[k], ends[k])；每架机取首个包含它的体，
    命中按（体次序，参与者次序）排列——即逐体、体内按参与者升序、已命中的机不再计入。写 ORD[k, :m]（参与者下标）、QQ[k, :m]
    （体在该识别物内的序号）、CNT[k] = m。"""
    nt = starts.shape[0]
    nu = IN.shape[1]
    seen = np.zeros(nu, np.bool_)
    for k in range(nt):
        m = 0
        if nu > 0:
            seen[:] = False
            for q in range(starts[k], ends[k]):
                for j in range(nu):
                    if IN[q, j] and not seen[j]:
                        seen[j] = True
                        ORD[k, m] = j
                        QQ[k, m] = q - starts[k]
                        m += 1
        CNT[k] = m


@njit(cache=True, fastmath=False)
def inside_all(C, P, shape, r2, zmin, zmax, half, yawb, IN, R3):
    """全部启用敏感体 × 参与者的内外与斜距（`TargetRuntime._inside_all_r3`，与此前的 numpy 表达式逐元素同式）：

    - d = P − C；rh2 = dx² + dy²；球与半球 `rh2 + dz² ≤ r²`（半球另要 dz ≥ 0）；柱与扇形 `rh2 ≤ r²` 且 `z_min ≤ dz ≤ z_max`；
    - 扇形另要 `|((atan2(dy, dx) − yawb + π) mod 2π) − π| ≤ half` 或 rh2 < 1e−12（yawb = 识别物航向 + 方位偏移）；
    - R3 = √((dx² + dy²) + dz²)（与 `np.linalg.norm(d, axis=-1)` 同一归约次序）。"""
    nb = C.shape[0]
    nu = P.shape[0]
    two_pi = 2.0 * math.pi
    for b in range(nb):
        cx, cy, cz = C[b, 0], C[b, 1], C[b, 2]
        sh = shape[b]
        rr = r2[b]
        for j in range(nu):
            dx = P[j, 0] - cx
            dy = P[j, 1] - cy
            dz = P[j, 2] - cz
            rh2 = dx * dx + dy * dy
            s3 = rh2 + dz * dz
            R3[b, j] = math.sqrt(s3)
            if sh <= 1:
                ok = s3 <= rr and (sh == 0 or dz >= 0.0)
            else:
                ok = rh2 <= rr and dz >= zmin[b] and dz <= zmax[b]
                if ok and sh == 3:
                    a = abs((math.atan2(dy, dx) - yawb[b] + math.pi) % two_pi - math.pi)
                    ok = a <= half[b] or rh2 < 1e-12
            IN[b, j] = ok


# ---------------------------------------------------------------- 与 CPython 逐位相同的标量工具
@njit(cache=True, fastmath=False)
def _two_prod(x, y):
    """无误差乘积（Dekker 拆分；|x|、|y| < 1 时 z + zz 恰为 x·y，与 CPython 的 fma 版 `dl_mul` 给出同一低位）。"""
    z = x * y
    c = 134217729.0
    t = c * x
    xh = t - (t - x)
    xl = x - xh
    t = c * y
    yh = t - (t - y)
    yl = y - yh
    zz = ((xh * yh - z) + xh * yl + xl * yh) + xl * yl
    return z, zz


@njit(cache=True, fastmath=False)
def py_hypot(a, b):
    """CPython 3.12 `math.hypot(a, b)`（`vector_norm`：按 frexp 缩放、补偿求和与一次微分修正）的逐位复现。numba 的 math.hypot
    为 libm 的 hypot（与 np.hypot 相同），与 CPython 的结果在末位可能不同。"""
    x0 = abs(a)
    x1 = abs(b)
    if math.isinf(x0) or math.isinf(x1):
        return math.inf
    if x0 != x0 or x1 != x1:
        return math.nan
    mx = x0 if x0 > x1 else x1
    if mx == 0.0:
        return mx
    _m, max_e = math.frexp(mx)
    if max_e < -1023:
        x0 = x0 / _DBL_MIN
        x1 = x1 / _DBL_MIN
        mx = mx / _DBL_MIN
        _m, max_e = math.frexp(mx)
        sc_out = _DBL_MIN
    else:
        sc_out = 1.0
    scale = math.ldexp(1.0, -max_e)
    csum = 1.0
    frac1 = 0.0
    frac2 = 0.0
    for i in range(2):
        x = (x0 if i == 0 else x1) * scale
        hi, lo = _two_prod(x, x)
        s = csum + hi
        sl = (csum - s) + hi
        csum = s
        frac1 += lo
        frac2 += sl
    h = math.sqrt(csum - 1.0 + (frac1 + frac2))
    hi, lo = _two_prod(-h, h)
    s = csum + hi
    sl = (csum - s) + hi
    csum = s
    frac1 += lo
    frac2 += sl
    x = csum - 1.0 + (frac1 + frac2)
    h += x / (2.0 * h)
    return sc_out * (h / scale)


@njit(cache=True, fastmath=False)
def _wrap_pi(a):
    """`geometry.wrap_pi` 的标量形式：`(a + π) mod 2π − π`（Python 浮点取模语义）。"""
    return (a + math.pi) % (2.0 * math.pi) - math.pi


@njit(cache=True, fastmath=False)
def _pmax(a, b):
    """Python `max(a, b)`：b > a 时取 b，否则 a。"""
    return b if b > a else a


@njit(cache=True, fastmath=False)
def _pmin(a, b):
    """Python `min(a, b)`：b < a 时取 b，否则 a。"""
    return b if b < a else a


@njit(cache=True, fastmath=False)
def _in_flat(MS, k, x, y, extra):
    return MS[k, S_HASF] != 0.0 and py_hypot(x - MS[k, S_FCX], y - MS[k, S_FCY]) + extra <= MS[k, S_FR]


@njit(cache=True, fastmath=False)
def _passable_k(MS, k, a, x0, y0, cell, has_dsm, px0, py0, z0, yy, look):
    if _in_flat(MS, k, px0 + math.cos(yy) * look, py0 + math.sin(yy) * look, 0.0):
        return True
    if not has_dsm:
        return True
    return passable(a, x0, y0, cell, px0, py0, yy, look, z0, STEP_MAX_M)


@njit(cache=True, fastmath=False)
def _in_polygon(REG, off, n, x, y):
    inside = False
    j = n - 1
    for i in range(n):
        xi = REG[off + i, 0]
        yi = REG[off + i, 1]
        xj = REG[off + j, 0]
        yj = REG[off + j, 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


@njit(cache=True, fastmath=False)
def _project_into(REG, off, n, cx, cy, x, y):
    """`motion.project_into`（inset 0.1）。"""
    best0 = x
    best1 = y
    bd = math.inf
    for i in range(n):
        a0 = REG[off + i, 0]
        a1 = REG[off + i, 1]
        b0 = REG[off + (i + 1) % n, 0]
        b1 = REG[off + (i + 1) % n, 1]
        ab0 = b0 - a0
        ab1 = b1 - a1
        L2 = ab0 * ab0 + ab1 * ab1
        t = 0.0 if L2 <= 0 else _pmin(1.0, _pmax(0.0, ((x - a0) * ab0 + (y - a1) * ab1) / L2))
        q0 = a0 + t * ab0
        q1 = a1 + t * ab1
        d = py_hypot(x - q0, y - q1)
        if d < bd:
            best0 = q0
            best1 = q1
            bd = d
    v0 = cx - best0
    v1 = cy - best1
    L = math.hypot(v0, v1)
    if L > 1e-9:
        return best0 + v0 / L * 0.1, best1 + v1 / L * 0.1
    return best0, best1


@njit(cache=True, fastmath=False)
def _refresh_flat(MS, k, a, x0, y0, cell, g0):
    x = MS[k, S_X]
    y = MS[k, S_Y]
    for ri in range(2):
        R = 15.0 if ri == 0 else 6.0
        n = int(R // 1.5)
        step = R / n
        ok = True
        for r in range(2 * n + 1):
            gy = float(r - n) * step
            for c in range(2 * n + 1):
                gx = float(c - n) * step
                if gx * gx + gy * gy <= R * R:
                    z = dsm_at(a, x0, y0, cell, x + gx, y + gy)
                    if not (abs(z - g0) <= FLAT_TOL_M):
                        ok = False
                        break
            if not ok:
                break
        if ok:
            MS[k, S_FCX] = x
            MS[k, S_FCY] = y
            MS[k, S_HASF] = 1.0
            MS[k, S_FR] = R
            MS[k, S_FZ] = g0
            return
    MS[k, S_HASF] = 0.0


@njit(cache=True, fastmath=False)
def _ground(a, x0, y0, cell, has_world, x, y, fallback):
    if not has_world:
        return fallback
    return dsm_at(a, x0, y0, cell, x, y)


@njit(cache=True, fastmath=False)
def _next_index(MS, k, n, loop):
    """`motion._next_index`（返回 −1 表示 None）。"""
    seg = int(MS[k, S_SEG])
    dirn = int(MS[k, S_DIRN])
    nxt = seg + dirn
    if 0 <= nxt < n:
        return nxt
    if loop == LOOP_PINGPONG:
        MS[k, S_DIRN] = float(-dirn)
        return seg - dirn
    if loop == LOOP_LOOP:
        return 0 if dirn > 0 else n - 1
    return -1


@njit(cache=True, fastmath=False)
def _path_desire(MS, m, PR, k, PTS, DW, t_s, dt, out):
    """`motion._path_desire`：out = [有期望航向, 期望航向, 期望速度, 有绝对 z, 绝对 z]。"""
    off = int(PR[k, P_POFF])
    n = int(PR[k, P_PN])
    v = PR[k, P_V0]
    loop = int(PR[k, P_LOOP])
    out[0] = 0.0
    out[2] = 0.0
    out[3] = 0.0
    if n < 2 or MS[m, S_DONE] != 0.0 or t_s < MS[m, S_DWELL]:
        return
    p0 = MS[m, S_X]
    p1 = MS[m, S_Y]
    nxt = _next_index(MS, m, n, loop)
    if nxt < 0:
        MS[m, S_DONE] = 1.0
        return
    seg = int(MS[m, S_SEG])
    bx = PTS[off + nxt, 0]
    by = PTS[off + nxt, 1]
    bz = PTS[off + nxt, 2]
    ax0 = PTS[off + seg, 0]
    ay0 = PTS[off + seg, 1]
    ab0x = bx - ax0
    ab0y = by - ay0
    L0 = math.hypot(ab0x, ab0y)
    passed = L0 > 1e-9 and ((p0 - ax0) * ab0x + (p1 - ay0) * ab0y) / L0 >= L0 - 0.5
    if passed or py_hypot(bx - p0, by - p1) <= _pmax(1.0, 1.5 * _pmax(MS[m, S_SPEED], v) * dt):
        MS[m, S_SEG] = float(nxt)
        if DW[off + nxt] > 0.0:
            MS[m, S_DWELL] = t_s + DW[off + nxt]
            return
        nxt = _next_index(MS, m, n, loop)
        if nxt < 0:
            MS[m, S_DONE] = 1.0
            return
        bx = PTS[off + nxt, 0]
        by = PTS[off + nxt, 1]
        bz = PTS[off + nxt, 2]
    seg = int(MS[m, S_SEG])
    ax = PTS[off + seg, 0]
    ay = PTS[off + seg, 1]
    abx = bx - ax
    aby = by - ay
    L = math.hypot(abx, aby)
    stop_at_b = DW[off + nxt] > 0.0 or (loop == LOOP_ONCE and nxt == n - 1) or (loop == LOOP_PINGPONG and (nxt == 0 or nxt == n - 1))
    if stop_at_b:
        acc = PR[k, P_ACC]
        v = _pmin(v, math.sqrt(2.0 * acc * _pmax(py_hypot(bx - p0, by - p1) - 0.5, 0.0)) + 0.2)
    look = _pmax(2.0, 1.5 * _pmax(MS[m, S_SPEED], v))
    if L > 1e-9:
        ux = abx / L
        uy = aby / L
        s2 = _pmin(L, _pmax(0.0, (p0 - ax) * ux + (p1 - ay) * uy) + look)
        tx = ax + ux * s2
        ty = ay + uy * s2
    else:
        tx = bx
        ty = by
    out[0] = 1.0
    out[1] = math.atan2(ty - p1, tx - p0)
    out[2] = v
    if bz == bz:
        out[3] = 1.0
        out[4] = bz


@njit(cache=True, fastmath=False)
def step_all(MS, ROWS, PR, OV, BA, NZ, UN, PTS, DW, REG, a, x0, y0, cell, has_dsm, has_world, dt, t_s):
    """核适用识别物的一步运动（`motion.step_motion` 的逐行翻译，逐位相同；路径、随机游走与静止三种模式，行为覆盖 hold、look、
    move）。第 k 个识别物的运动状态为 MS[ROWS[k]]（原地更新），参数、覆盖、驻留状态与随机数按 k 取；地面类且无 DSM 栅格
    （合成世界）的识别物不交给本核。"""
    tmp = np.empty(5)
    for k in range(ROWS.shape[0]):
        m = ROWS[k]
        mode = int(PR[k, P_MODE])
        ovk = int(OV[k, O_KIND])
        base = BA[k] != 0
        if ovk == OV_NONE and (mode == MODE_STATIC or not base) and MS[m, S_SPEED] == 0.0:
            MS[m, S_VZ] = 0.0
            continue
        ground = PR[k, P_GROUND] != 0.0 and has_world
        turn_max = PR[k, P_TURN]
        vmax = PR[k, P_VMAX]
        acc = PR[k, P_ACC]
        alt = PR[k, P_ALT]
        px0 = MS[m, S_X]
        py0 = MS[m, S_Y]
        z0 = MS[m, S_Z]
        has_wy = False
        want_yaw = 0.0
        want_v = 0.0
        has_zabs = False
        z_abs = 0.0
        roff = int(PR[k, P_ROFF])
        rn = int(PR[k, P_RN])
        has_region = mode == MODE_RW and roff >= 0
        if ovk != OV_NONE:
            if ovk in (OV_LOOK, OV_MOVE) and OV[k, O_HASPT] != 0.0:
                dx = OV[k, O_PX] - px0
                dy = OV[k, O_PY] - py0
                brg = math.atan2(dy, dx)
                if ovk == OV_LOOK:
                    has_wy = True
                    want_yaw = brg
                else:
                    has_wy = True
                    away = OV[k, O_AWAY] != 0.0
                    want_yaw = _wrap_pi(brg + math.pi) if away else brg
                    osp = OV[k, O_SPEED]
                    want_v = _pmin(osp, _pmax(vmax, osp))
                    if not away:
                        gap = py_hypot(dx, dy) - OV[k, O_STOP]
                        want_v = 0.0 if gap <= 0.0 else _pmin(want_v, math.sqrt(2.0 * acc * gap) + 0.05)
            elif ovk == OV_MOVE and OV[k, O_HASPT] == 0.0:
                want_v = 0.0
        elif not base or mode == MODE_STATIC:
            pass
        elif mode == MODE_RW:
            tau = PR[k, P_TAU]
            sig_w = 0.5 * turn_max
            om = MS[m, S_OMEGA]
            om = om - om * dt / tau + sig_w * math.sqrt(2.0 * dt / tau) * NZ[k, 0]
            want_v = _pmin(_pmax(PR[k, P_V0] + PR[k, P_SIGV] * NZ[k, 1], 0.0), vmax)
            pause_p = PR[k, P_PAUSEP]
            if t_s < MS[m, S_PAUSE]:
                want_v = 0.0
            elif pause_p > 0.0 and UN[k, 0] < pause_p * dt:
                MS[m, S_PAUSE] = t_s + 2.0 + 3.0 * UN[k, 1]
                want_v = 0.0
            if has_region:
                vp = _pmax(_pmax(want_v, MS[m, S_SPEED]), 0.5)
                px = px0 + math.cos(MS[m, S_YAW]) * vp * 2.0
                py = py0 + math.sin(MS[m, S_YAW]) * vp * 2.0
                if not _in_polygon(REG, roff, rn, px, py):
                    inward = math.atan2(PR[k, P_RCY] - py0, PR[k, P_RCX] - px0)
                    w = _wrap_pi(inward - MS[m, S_YAW])
                    om = math.copysign(turn_max, w if w != 0.0 else 1.0)
            om = _pmax(-turn_max, _pmin(turn_max, om))
            MS[m, S_OMEGA] = om
            has_wy = True
            want_yaw = _wrap_pi(MS[m, S_YAW] + om * dt)
        elif mode == MODE_PATH:
            _path_desire(MS, m, PR, k, PTS, DW, t_s, dt, tmp)
            has_wy = tmp[0] != 0.0
            want_yaw = tmp[1]
            want_v = tmp[2]
            has_zabs = tmp[3] != 0.0
            z_abs = tmp[4]
            want_v = _pmin(want_v, vmax) if vmax > 0 else want_v
        spd = MS[m, S_SPEED]
        near = _pmax(1.0, _pmax(spd, want_v) * dt)
        far = _pmax(3.0, 2.0 * _pmax(spd, want_v))
        if has_world and want_v > 0.0 and not _in_flat(MS, m, px0, py0, 0.5 * MS[m, S_FR]) and \
                (MS[m, S_HASFF] == 0.0 or py_hypot(px0 - MS[m, S_FFX], py0 - MS[m, S_FFY]) > 5.0):
            g0 = z0 if ground else _ground(a, x0, y0, cell, has_world, px0, py0, z0)
            _refresh_flat(MS, m, a, x0, y0, cell, g0)
            if MS[m, S_HASF] != 0.0:
                MS[m, S_HASFF] = 0.0
            else:
                MS[m, S_HASFF] = 1.0
                MS[m, S_FFX] = px0
                MS[m, S_FFY] = py0
        has_free = False
        free_yaw = 0.0
        if ground and has_wy and want_v > 0.0:
            if _passable_k(MS, m, a, x0, y0, cell, has_dsm, px0, py0, z0, want_yaw, far):
                MS[m, S_DETOUR] = 0.0
                has_free = True
                free_yaw = want_yaw
            else:
                det = MS[m, S_DETOUR]
                if det == 0.0 or not _passable_k(MS, m, a, x0, y0, cell, has_dsm, px0, py0, z0, want_yaw + det, far):
                    order = _DET0 if det == 0.0 else (_DETP if det > 0.0 else _DETN)
                    pick = 0.0
                    found = False
                    for q in range(6):
                        if _passable_k(MS, m, a, x0, y0, cell, has_dsm, px0, py0, z0, want_yaw + order[q], far):
                            pick = order[q]
                            found = True
                            break
                    if not found:
                        for q in range(6):
                            if _passable_k(MS, m, a, x0, y0, cell, has_dsm, px0, py0, z0, want_yaw + order[q], near):
                                pick = order[q]
                                found = True
                                break
                    MS[m, S_DETOUR] = pick if found else 0.0
                    if not found and not _passable_k(MS, m, a, x0, y0, cell, has_dsm, px0, py0, z0, want_yaw, near):
                        want_v = 0.0
                want_yaw = _wrap_pi(want_yaw + MS[m, S_DETOUR])
        if has_wy:
            yw = MS[m, S_YAW]
            d = _wrap_pi(want_yaw - yw)
            lim = turn_max * dt
            MS[m, S_YAW] = _wrap_pi(yw + _pmax(-lim, _pmin(lim, d)))
        dv = want_v - spd
        spd = _pmax(0.0, spd + _pmax(-acc * dt, _pmin(acc * dt, dv)))
        MS[m, S_SPEED] = spd
        if spd <= 1e-6:
            MS[m, S_SPEED] = 0.0
            MS[m, S_VZ] = 0.0
            MS[m, S_BLOCKED] = 1.0 if (want_v == 0.0 and ground and has_wy and
                                       not _passable_k(MS, m, a, x0, y0, cell, has_dsm, px0, py0, z0, MS[m, S_YAW], near)) else 0.0
            continue
        step = spd * dt
        yaw = MS[m, S_YAW]
        if ground and not (has_free and abs(_wrap_pi(yaw - free_yaw)) < 1e-9) and \
                not _passable_k(MS, m, a, x0, y0, cell, has_dsm, px0, py0, z0, yaw, near):
            MS[m, S_BLOCKED] = 1.0
            MS[m, S_VZ] = 0.0
            continue
        MS[m, S_BLOCKED] = 0.0
        nx = px0 + math.cos(yaw) * step
        ny = py0 + math.sin(yaw) * step
        if has_region and not _in_polygon(REG, roff, rn, nx, ny):
            if ovk == OV_MOVE:
                nx, ny = _project_into(REG, roff, rn, PR[k, P_RCX], PR[k, P_RCY], nx, ny)
            else:
                om = MS[m, S_OMEGA]
                MS[m, S_OMEGA] = math.copysign(turn_max, om if om != 0.0 else 1.0)
                continue
        uav = PR[k, P_UAV] != 0.0
        if not has_zabs and _in_flat(MS, m, nx, ny, 0.0):
            fz = MS[m, S_FZ]
            nz = z0 if ground else _pmax(fz + UAV_MIN_AGL, fz + alt)
        else:
            g = _ground(a, x0, y0, cell, has_world, nx, ny, z0 - alt if uav else z0)
            nz = (_pmax(z_abs, g + 1.0) if has_zabs else _pmax(g + UAV_MIN_AGL, g + alt)) if uav else g
        MS[m, S_ODO] = MS[m, S_ODO] + py_hypot(nx - px0, ny - py0)
        MS[m, S_VZ] = (nz - z0) / dt
        MS[m, S_X] = nx
        MS[m, S_Y] = ny
        MS[m, S_Z] = nz


def warmup() -> None:
    """按运行期签名（只读 float32 栅格、float64 标量）编译或读缓存。"""
    if not HAVE_NUMBA:
        return
    a = np.zeros((4, 4), np.float32)
    a.flags.writeable = False
    dsm_at(a, 0.0, 0.0, 2.0, 1.0, 1.0)
    passable(a, 0.0, 0.0, 2.0, 1.0, 1.0, 0.5, 3.0, 0.0, 0.5)
    b = np.zeros((4, 4), np.float32)
    dsm_at(b, 0.0, 0.0, 2.0, 1.0, 1.0)
    passable(b, 0.0, 0.0, 2.0, 1.0, 1.0, 0.5, 3.0, 0.0, 0.5)
    C = np.zeros((2, 3))
    P = np.ones((3, 3))
    IN0 = np.zeros((2, 3), np.bool_)
    R30 = np.zeros((2, 3))
    inside_all(C, P, np.array([0, 3], np.int8), np.ones(2), np.zeros(2), np.ones(2), np.ones(2), np.zeros(2), IN0, R30)
    MS = np.zeros((2, MS_N))
    MS[:, S_DIRN] = 1.0
    PR = np.zeros((2, PR_N))
    PR[:, P_ROFF] = -1.0
    PR[:, P_TAU] = 4.0
    PR[:, P_ACC] = 1.0
    PR[:, P_TURN] = 1.0
    PR[:, P_VMAX] = 2.0
    PR[:, P_V0] = 1.0
    PR[:, P_GROUND] = 1.0
    PR[0, P_MODE] = MODE_RW
    PR[1, P_MODE] = MODE_PATH
    PR[1, P_PN] = 2
    OV = np.zeros((2, OV_N))
    step_all(MS, np.arange(2, dtype=np.int64), PR, OV, np.ones(2, np.int64), np.zeros((2, 2)), np.zeros((2, 2)), np.zeros((2, 3)), np.zeros(2), np.zeros((1, 2)), a, 0.0,
             0.0, 2.0, True, True, 0.1, 1.0)
    step_all(MS, np.arange(2, dtype=np.int64), PR, OV, np.ones(2, np.int64), np.zeros((2, 2)), np.zeros((2, 2)), np.zeros((2, 3)), np.zeros(2), np.zeros((1, 2)), b, 0.0,
             0.0, 2.0, True, True, 0.1, 1.0)
    py_hypot(3.0, 4.0)
    IN = np.ones((2, 3), np.bool_)
    geo_hits(IN, np.array([0], np.int64), np.array([2], np.int64), np.zeros((1, 3), np.int64), np.zeros((1, 3), np.int64),
             np.zeros(1, np.int64))
