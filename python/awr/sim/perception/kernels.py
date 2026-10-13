"""M19 成像感知管线的 numba 融合核（D2-FX-R2-perception；M19-NFR-001、M19-AC-010；D2-AC-13；AWR-04 §6.2、§6.3、§6.4）。

D2 验收第 1 轮（`docs/impl/D2-验收报告-第1轮.md` 4.1）：12 × 30 的感知 stage 490.6 ms/仿真秒（预算 25）、单 tick p99 9.51 ms（预算 1）。
每次（机体，成像传感器）评估约 7.7 ms，其中数组核 `level.evaluate` 在 30 对上约 0.29 ms（约 100 次小数组 numpy 运算，本机每次
1–4 µs）、视锥与几何约 0.15 ms、逐对计数器随机数约 0.19 ms/对，另有逐对 Python 的状态机、测量 dict、航迹矩阵与视线缓存。
本模块把一次评估的数值与逐对部分合成三次进入 numba：

- `fov_gate`：距离与视锥门（传感器系 FLU，x 为视轴），写出全部识别物的 d、R 与视锥内下标；
- `eval_a` = `imaging_core` + `targets.kernels_los.query_scan`：视锥内各对的方向投影关键维度（`critical_dim`）、偏轴角与云台跟踪
  判定、垂直视线的相对速度，以及 `level.evaluate` 的 N、GSD、k_c、k_light（含 NIR 照明器不动点）、k_blur、n_max 与 LWIR 的
  ΔT_app；像素与环境门（`n ≥ min(N_D, N_req)` 且 `n_max ≥ min(N_D, N_req)`）；视线请求按 target_no 升序、起点轮转，格键在核内
  算出，直接在共享视线缓存的数组表上查命中、令牌、旧值与延期；
- `eval_b` = `kernels_los.apply_jobs`（有射线作业时）+ `post_fsm`：k_los、N_eff、limiting、等级、捕获条件、TTPF（M19-FR-003）、
  量测（`√((R·1 mrad)² + GSD² + sigma_hold²)`，噪声取计数器 RNG 流 18 的 Box–Muller，与 `awr.sim.sensors.cbrng.normal` 同键同运算）、
  对状态表（pairs.PairTable）的等级迟滞与捕获状态机（`fsm_row`，`state.step` 的数组版）与单一量测来源时的航迹更新
  （`trk_update_row`，tracks.py 的逐轴闭式 Kalman）。

另有 `fov_rows`（视锥外已有对的推进）与 `summary_rows`、`summary_cols`（识别物汇总列）。运算次序与 `level.evaluate` 及 numpy 路径
（`runtime._eval_np`）逐项相同（左结合乘加、`max`/`clip` 的 NaN 语义与 `np.maximum`、`np.clip` 一致，numba 不合并 FMA）；本机 numba
的 `math.log/exp/cos/atan2/acos/pow/hypot` 与 numpy 逐位相同。唯一的差别来自 numpy 的 `d @ R`（BLAS，可能用 FMA）：只影响视锥与
跟踪判定的边界情形。`tests/perception/test_kernels.py` 以 numpy 判据与 Python 状态机对拍：离散量、事件与检测环逐项相等，
连续量相对误差 ≤ 1e-12。

numba 缺失或 `AWR_KERNEL=numpy` 时 `HAVE_NUMBA = False`，运行时走 numpy 判据与 Python 状态机（结果同上）。
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

from awr.sim.targets import kernels_los as _KL

from .params import N_P90
from .state import ARMING, CAPTURED, EPS, HOLDING, IDLE, T_DOWN, T_HOLD, T_LOST, T_UP

__all__ = [
    "CORE_COLS",
    "HAVE_NUMBA",
    "PK_COLS",
    "POST_COLS",
    "eval_a",
    "eval_b",
    "fov_gate",
    "imaging_core",
    "normals3",
    "post_fsm",
    "prefix3",
    "prefix_key",
    "prefix_tick",
    "uniform_ch",
    "warmup",
]

# ---------------------------------------------------------------- 计数器 RNG（与 awr.sim.sensors.cbrng 同一 SplitMix64 键）
_M64 = (1 << 64) - 1
_C1_I, _C2_I, _C3_I = 0x9E3779B97F4A7C15, 0xBF58476D1CE4E5B9, 0x94D049BB133111EB
C1 = np.uint64(_C1_I)
C2 = np.uint64(_C2_I)
C3 = np.uint64(_C3_I)
S11 = np.uint64(11)
S27 = np.uint64(27)
S30 = np.uint64(30)
S31 = np.uint64(31)
INV53 = 1.0 / 9007199254740992.0
TWO_PI = 2.0 * math.pi


def _sm_int(z: int) -> int:
    """SplitMix64 终混（Python 整数，按 2^64 回绕；与 `cbrng.sm` 逐位相同）。"""
    z = (z + _C1_I) & _M64
    z = ((z ^ (z >> 30)) * _C2_I) & _M64
    z = ((z ^ (z >> 27)) * _C3_I) & _M64
    return z ^ (z >> 31)


def prefix_key(seed: int, stream: int, agent: int, tick: int) -> int:
    """`cbrng.key` 的前四级 `sm(sm(sm(sm(seed) ^ stream) ^ agent) ^ tick)`（Python 整数）；末级 `sm(k ^ ch)` 在核内按通道完成。"""
    k = _sm_int(int(seed) & _M64)
    k = _sm_int(k ^ (int(stream) & _M64))
    k = _sm_int(k ^ (int(agent) & _M64))
    return _sm_int(k ^ (int(tick) & _M64))


def prefix3(seed: int, stream: int, agent: int) -> int:
    """前三级 `sm(sm(sm(seed) ^ stream) ^ agent)`（同一会话、同一机不变）。"""
    k = _sm_int(int(seed) & _M64)
    k = _sm_int(k ^ (int(stream) & _M64))
    return _sm_int(k ^ (int(agent) & _M64))


def prefix_tick(k3: int, tick: int) -> int:
    """第四级 `sm(k3 ^ tick)`。"""
    return _sm_int(k3 ^ (int(tick) & _M64))


def uniform_ch(prefix: int, ch: int) -> float:
    """U[0, 1)（与 `cbrng.uniform` 同键、同运算：`(key >> 11)·2^-53`）。"""
    return float(_sm_int(prefix ^ (int(ch) & _M64)) >> 11) * INV53


@njit(cache=True, fastmath=False)
def _sm(z):
    z = z + C1
    z = (z ^ (z >> S30)) * C2
    z = (z ^ (z >> S27)) * C3
    return z ^ (z >> S31)


@njit(cache=True, fastmath=False)
def _normal(prefix, ch):
    k = _sm(prefix ^ ch)
    b = _sm(k ^ C3)
    u1 = (float(k >> S11) + 0.5) * INV53
    u2 = float(b >> S11) * INV53
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(TWO_PI * u2)


@njit(cache=True, fastmath=False)
def _ch(sensor_no, target_no, j):
    return np.uint64((((sensor_no & 0xFFFF) << 16 | (target_no & 0xFFFF)) << 3) + j)


@njit(cache=True, fastmath=False)
def normals3(prefix, sensor_no, target_no, out):
    """三个 N(0, 1)：通道 `_ch(sensor_no, target_no, j)`，j = 0、1、2（`runtime._noise3`）。"""
    for j in range(3):
        out[j] = _normal(prefix, _ch(sensor_no, target_no, j))


# ---------------------------------------------------------------- NaN 语义与 numpy 一致的 max / clip
@njit(cache=True, fastmath=False)
def _fmax(a, b):
    """`np.maximum(a, b)`（a 为 NaN 时返回 a）。"""
    return a if (a >= b or a != a) else b


@njit(cache=True, fastmath=False)
def _clip(x, lo, hi):
    """`np.clip(x, lo, hi)` = `minimum(maximum(x, lo), hi)`（NaN 传播）。"""
    y = x if (x >= lo or x != x) else lo
    return y if (y <= hi or y != y) else hi


# ---------------------------------------------------------------- 视锥门
@njit(cache=True, fastmath=False)
def fov_gate(pos, p, Rm, hh, hv, d_out, r_out, cand_out):
    """`d = pos − p`、`R = |d|`、`u = d·Rm`（FLU）；视锥内（u_x > 0.5 且 |atan2(u_y, u_x)| ≤ hh、|atan2(u_z, u_x)| ≤ hv）的下标按升序
    写入 cand_out，返回个数。"""
    n = pos.shape[0]
    m = 0
    p0 = p[0]
    p1 = p[1]
    p2 = p[2]
    for k in range(n):
        d0 = pos[k, 0] - p0
        d1 = pos[k, 1] - p1
        d2 = pos[k, 2] - p2
        d_out[k, 0] = d0
        d_out[k, 1] = d1
        d_out[k, 2] = d2
        r_out[k] = math.sqrt(d0 * d0 + d1 * d1 + d2 * d2)
        u0 = d0 * Rm[0, 0] + d1 * Rm[1, 0] + d2 * Rm[2, 0]
        u1 = d0 * Rm[0, 1] + d1 * Rm[1, 1] + d2 * Rm[2, 1]
        u2 = d0 * Rm[0, 2] + d1 * Rm[1, 2] + d2 * Rm[2, 2]
        if u0 > 0.5 and abs(math.atan2(u1, u0)) <= hh and abs(math.atan2(u2, u0)) <= hv:
            cand_out[m] = k
            m += 1
    return m


# ---------------------------------------------------------------- 识别物打包列（TargetSnap.pk）
PK_COLS = {"pos": 0, "vel": 3, "yaw": 6, "size": 7, "crit_ov": 10, "hot": 11, "c0": 12, "camo": 13, "rho850": 14, "th_abs": 15, "dt_rel": 16,
           "t_surf": 17, "emis": 18, "occ": 19}
PK_N = 20

# imaging_core 的输出列
CORE_COLS = {"R": 0, "n": 1, "n_max": 2, "gsd": 3, "k_c": 4, "k_light": 5, "k_blur": 6, "dt_app": 7, "off": 8}
CORE_N = 9

# imaging_core 的标量参数 prm
P_KIND, P_F, P_PP, P_SRC, P_TRK, P_TRACK, P_TEXP, P_JIT, P_KL, P_KM, P_CMIN, P_CSPAN, P_EDGE = 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12
P_TS, P_TB_EB, P_TS_ES, P_KCROSS, P_SNR_DEN, P_SNR_MIN, P_SNR_SPAN, P_ND, P_FULL, P_PX, P_PY, P_PZ, P_BX, P_BY, P_BZ = (
    13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27)
P_VX, P_VY, P_VZ = 28, 29, 30
PRM_N = 31
K0 = 273.15


@njit(cache=True, fastmath=False)
def _r_nir(r0, s):
    """`level.r_nir_fixed_point` 的逐元素版（二分 50 次）。"""
    lo = 0.0
    hi = r0
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if mid - r0 * math.exp(-s * mid) >= 0.0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


@njit(cache=True, fastmath=False)
def imaging_core(cand, m, d, rr, pk, od, n_req, prm, ill, p, Rm, pvel, src, trk_max, out, need, keys, ca, cb, rot, order, oB, oH, oK, info):
    """视锥内 m 对（cand[:m]）：写 out[j, CORE_COLS]、need[j]（像素与环境门）与 keys[j]（`LosCache` 的格键：传感器 5 m 格、
    识别物脚点 2 m 格，`math.floor(x / cell)`，与 `LosCache._key` 逐位相同）。od（光学厚度，τ = exp(−od)）按 j（视锥内），n_req 按识别物下标 i。

    视线请求次序：need 的对按 j 升序、起点 `rot mod 个数` 轮转，写 order[:nn] 与按该次序的脚点 oB、高度 oH、格键 oK。
    info = [nn, 起点 s0, 键有效]（传感器或识别物坐标非有限时键无效，调用方改由 `LosCache` 取键，与此前一样抛出）。

    prm 见 P_*（传感器与环境的标量，按（传感器状态，环境）缓存）；ill[k] = (系数 r_ref·(θ_ref/max(θ, 1e-9))·√(P/P_ref), rho_ref,
    半锥角 + 1e-9)，只含开启的照明器（NIR）。p 为传感器原点、Rm 的第 0 列为视轴、pvel 为平台速度；src 为云台指向非缺省、trk_max 为
    跟踪判定的偏轴上限。kind：0 EO、1 NIR、2 LWIR。"""
    kind = int(prm[P_KIND])
    f = prm[P_F]
    pp = prm[P_PP]
    track_res = prm[P_TRACK]
    t_exp = prm[P_TEXP]
    jit = prm[P_JIT]
    kl = prm[P_KL]
    km = prm[P_KM]
    c_min = prm[P_CMIN]
    c_span = prm[P_CSPAN]
    edge = prm[P_EDGE]
    ts = prm[P_TS]
    e_b = prm[P_TB_EB]
    e_s = prm[P_TS_ES]
    k_cross = prm[P_KCROSS]
    snr_den = prm[P_SNR_DEN]
    snr_min = prm[P_SNR_MIN]
    snr_span = prm[P_SNR_SPAN]
    n_d = prm[P_ND]
    full = prm[P_FULL] != 0.0
    px = p[0]
    py = p[1]
    pz = p[2]
    b0 = Rm[0, 0]
    b1 = Rm[1, 0]
    b2 = Rm[2, 0]
    pv0 = pvel[0]
    pv1 = pvel[1]
    pv2 = pvel[2]
    n_ill = ill.shape[0]
    finite = math.isfinite(px) and math.isfinite(py) and math.isfinite(pz)
    ka0 = math.floor(px / ca) if finite else 0
    ka1 = math.floor(py / ca) if finite else 0
    ka2 = math.floor(pz / ca) if finite else 0
    for j in range(m):
        i = cand[j]
        d0 = d[i, 0]
        d1 = d[i, 1]
        d2 = d[i, 2]
        Rc = rr[i]
        # angles()：自识别物看传感器
        e0 = px - pk[i, 0]
        e1 = py - pk[i, 1]
        e2 = pz - pk[i, 2]
        h = math.hypot(e0, e1)
        eps = math.atan2(e2, h)
        beta = math.atan2(e1, e0) - pk[i, 6]
        # critical_dim(size, eps, beta)
        ln = pk[i, 7]
        w = pk[i, 8]
        hh = pk[i, 9]
        side = abs(math.cos(beta)) * w * hh + abs(math.sin(beta)) * ln * hh
        dc = math.sqrt(abs(math.sin(eps)) * ln * w + abs(math.cos(eps)) * side)
        ov = pk[i, 10]
        if ov == ov:
            dc = ov
        # 偏轴角与跟踪
        r9 = _fmax(Rc, 1e-9)
        cos_off = _clip((d0 * b0 + d1 * b1 + d2 * b2) / r9, -1.0, 1.0)
        off = math.acos(cos_off)
        tracking = src and off <= trk_max
        # 垂直视线的相对速度
        lu0 = d0 / r9
        lu1 = d1 / r9
        lu2 = d2 / r9
        v0 = pk[i, 3] - pv0
        v1 = pk[i, 4] - pv1
        v2 = pk[i, 5] - pv2
        dot = v0 * lu0 + v1 * lu1 + v2 * lu2
        w0 = v0 - dot * lu0
        w1 = v1 - dot * lu1
        w2 = v2 - dot * lu2
        v_perp = math.sqrt(w0 * w0 + w1 * w1 + w2 * w2)
        # level.evaluate
        R = _fmax(Rc, 1e-6)
        dd = dc * math.sqrt(_fmax(pk[i, 11], 0.25)) if kind == 2 else dc
        N = dd * f / (R * pp * 1e-3)
        G = R * pp * 1e-3 / f
        ta = math.exp(-od[j])                  # 路径透过率（与 numpy 路径的 np.exp(−od) 同一 libm exp）
        dt_app = math.nan
        if kind == 2:
            emis = pk[i, 18]
            if pk[i, 15] != 0.0:
                dt_app = ta * (emis * (pk[i, 17] + K0) + (1.0 - emis) * ts - e_b - e_s)
            else:
                dt_app = ta * emis * pk[i, 16] * k_cross
            snr = abs(dt_app) / snr_den
            k_c = _clip((snr - snr_min) / snr_span, 0.0, 1.0)
            k_light = 1.0
        else:
            C = abs(pk[i, 12]) * (1.0 - pk[i, 13]) * ta
            k_c = _clip((C - c_min) / c_span, 0.0, 1.0)
            if kind == 1:
                sig850 = -math.log(_fmax(ta, 1e-300)) / R if R > 0 else 0.0
                active = 0.0
                rho = _fmax(pk[i, 14], 0.0)
                for q in range(n_ill):
                    r0 = ill[q, 0] * math.sqrt(rho / ill[q, 1])
                    rn = _r_nir(r0, sig850)
                    kq = _clip((rn - R) / (edge * rn), 0.0, 1.0) if rn > 0 else 0.0
                    cone = off <= ill[q, 2]
                    active = _fmax(active, kq if cone else 0.0)
                k_light = _fmax(active, km)
            else:
                k_light = kl
        vp = abs(v_perp)
        v_res = track_res * vp if tracking else vp
        bb = v_res * t_exp + R * jit * 1e-6
        k_blur = G / math.sqrt(G * G + bb * bb) if G > 0 else 0.0
        n_max = N * k_c * k_light * k_blur * (1.0 - pk[i, 19])
        out[j, 0] = Rc
        out[j, 1] = N
        out[j, 2] = n_max
        out[j, 3] = G
        out[j, 4] = k_c
        out[j, 5] = k_light
        out[j, 6] = k_blur
        out[j, 7] = dt_app
        out[j, 8] = off
        nr = n_req[i]
        gate = nr if nr != nr else (n_d if n_d <= nr else nr)   # np.minimum(N_D, n_req)（NaN 传播）
        need[j] = full or (gate <= N and n_max >= gate)
        b0x = pk[i, 0]
        b1x = pk[i, 1]
        b2x = pk[i, 2]
        if finite and math.isfinite(b0x) and math.isfinite(b1x) and math.isfinite(b2x):
            keys[j, 0] = ka0
            keys[j, 1] = ka1
            keys[j, 2] = ka2
            keys[j, 3] = math.floor(b0x / cb)
            keys[j, 4] = math.floor(b1x / cb)
            keys[j, 5] = math.floor(b2x / cb)
        else:
            finite = False
    nn = 0
    for j in range(m):
        if need[j]:
            nn += 1
    s0 = rot % nn if nn > 0 else 0
    q = 0
    for pass_ in range(2):
        c = 0
        for j in range(m):
            if not need[j]:
                continue
            if (pass_ == 0 and c >= s0) or (pass_ == 1 and c < s0):
                order[q] = j
                i = cand[j]
                oB[q, 0] = pk[i, 0]
                oB[q, 1] = pk[i, 1]
                oB[q, 2] = pk[i, 2]
                oH[q] = pk[i, 9]
                for k in range(6):
                    oK[q, k] = keys[j, k]
                q += 1
            c += 1
    info[0] = nn
    info[1] = s0
    info[2] = 1 if finite else 0


# ---------------------------------------------------------------- 对状态（pairs.PairTable 的行；state.step 的数组版）
# PI 列（int64）
I_LEVEL, I_UPMIN, I_DOWNMAX, I_CAP, I_REASON, I_LIM, I_KIND, I_LREQ, I_HASLAST, I_HASMEAS, I_ALIVE, I_TNO, I_AGENT, I_SEQ = range(14)
PI_N = 14    # I_TNO、I_AGENT：对的识别物与机体；I_SEQ：建立次序（与 runtime.pairs 的插入次序相同，汇总的并列判定用）
# PF 列（float64；NaN = None）
F_UP0, F_DOWN0, F_TOK, F_TBAD, F_SINCE, F_TLASTOK, F_TEVAL, F_NREQ, F_NEFF, F_GSD, F_P, F_SIG, F_Z0, F_Z1, F_Z2 = range(15)
PF_N = 15
KIND_NAMES = ("eo", "nir", "lwir")
LREQ_NAMES = ("D", "R", "I")
_ND, _NR, _NI = N_P90["D"], N_P90["R"], N_P90["I"]
_LREQ_IDX = {"D": 0, "R": 1, "I": 2}


@njit(cache=True, fastmath=False)
def _lreq_nb(nr, nd, nrr, ni):
    """`level_req_of`：恰为某级阈值时取该级；否则取不超过它的最高级；都不足时 R。"""
    if abs(nd - nr) < 1e-9:
        return 0
    if abs(nrr - nr) < 1e-9:
        return 1
    if abs(ni - nr) < 1e-9:
        return 2
    best = -1
    if nr >= nd:
        best = 0
    if nr >= nrr:
        best = 1
    if nr >= ni:
        best = 2
    return 1 if best < 0 else best


@njit(cache=True, fastmath=False)
def fsm_row(PI, PF, s, t, level_m, cond, deferred, reason, tr, ntr, row):
    """`state.step` 的数组版（逐项相同）：推进第 s 行，转移写入 tr[ntr...] = (行号, 类别 0 level/1 capture/2 capture_lost, level_from,
    level_to, reason 码, captured_s, t_last_ok)，返回新的转移数。reason 为 LIMITING 下标或 -1（None）。"""
    if deferred:
        te = PF[s, F_TEVAL]
        if te == te:
            dt = t - te
            if dt < 0.0:
                dt = 0.0
            if PF[s, F_UP0] == PF[s, F_UP0]:
                PF[s, F_UP0] += dt
            if PF[s, F_DOWN0] == PF[s, F_DOWN0]:
                PF[s, F_DOWN0] += dt
            c = PI[s, I_CAP]
            if c == ARMING:
                PF[s, F_TOK] += dt
            elif c == HOLDING:
                PF[s, F_TBAD] += dt
        PF[s, F_TEVAL] = t
        return ntr
    PF[s, F_TEVAL] = t
    lv = PI[s, I_LEVEL]
    if level_m > lv:
        PF[s, F_DOWN0] = math.nan
        if PF[s, F_UP0] != PF[s, F_UP0]:
            PF[s, F_UP0] = t
            PI[s, I_UPMIN] = level_m
        elif level_m < PI[s, I_UPMIN]:
            PI[s, I_UPMIN] = level_m
        if t - PF[s, F_UP0] >= T_UP - EPS:
            tr[ntr, 0] = row
            tr[ntr, 1] = 0
            tr[ntr, 2] = lv
            tr[ntr, 3] = PI[s, I_UPMIN]
            tr[ntr, 4] = -1
            tr[ntr, 5] = 0.0
            tr[ntr, 6] = math.nan
            ntr += 1
            PI[s, I_LEVEL] = PI[s, I_UPMIN]
            PF[s, F_UP0] = math.nan
    elif level_m < lv:
        PF[s, F_UP0] = math.nan
        if PF[s, F_DOWN0] != PF[s, F_DOWN0]:
            PF[s, F_DOWN0] = t
            PI[s, I_DOWNMAX] = level_m
        elif level_m > PI[s, I_DOWNMAX]:
            PI[s, I_DOWNMAX] = level_m
        if t - PF[s, F_DOWN0] >= T_DOWN - EPS:
            tr[ntr, 0] = row
            tr[ntr, 1] = 0
            tr[ntr, 2] = lv
            tr[ntr, 3] = PI[s, I_DOWNMAX]
            tr[ntr, 4] = -1
            tr[ntr, 5] = 0.0
            tr[ntr, 6] = math.nan
            ntr += 1
            PI[s, I_LEVEL] = PI[s, I_DOWNMAX]
            PF[s, F_DOWN0] = math.nan
    else:
        PF[s, F_UP0] = math.nan
        PF[s, F_DOWN0] = math.nan
    if cond:
        PF[s, F_TLASTOK] = t
    c = PI[s, I_CAP]
    if c == IDLE:
        if cond:
            PI[s, I_CAP] = ARMING
            PF[s, F_TOK] = t
    elif c == ARMING:
        if not cond:
            PI[s, I_CAP] = IDLE
        elif t - PF[s, F_TOK] >= T_HOLD - EPS:
            PI[s, I_CAP] = CAPTURED
            PF[s, F_SINCE] = t
            tr[ntr, 0] = row
            tr[ntr, 1] = 1
            tr[ntr, 2] = PI[s, I_LEVEL]
            tr[ntr, 3] = PI[s, I_LEVEL]
            tr[ntr, 4] = -1
            tr[ntr, 5] = 0.0
            tr[ntr, 6] = math.nan
            ntr += 1
    elif c == CAPTURED:
        if not cond:
            PI[s, I_CAP] = HOLDING
            PF[s, F_TBAD] = t
            PI[s, I_REASON] = reason
    elif c == HOLDING:
        if cond:
            PI[s, I_CAP] = CAPTURED
            PI[s, I_REASON] = -1
        elif t - PF[s, F_TBAD] >= T_LOST - EPS:
            r0 = PI[s, I_REASON]
            cs = PF[s, F_TBAD] - PF[s, F_SINCE]
            tr[ntr, 0] = row
            tr[ntr, 1] = 2
            tr[ntr, 2] = PI[s, I_LEVEL]
            tr[ntr, 3] = PI[s, I_LEVEL]
            tr[ntr, 4] = r0 if r0 >= 0 else reason
            tr[ntr, 5] = cs if cs > 0.0 else 0.0
            tr[ntr, 6] = PF[s, F_TLASTOK]
            ntr += 1
            PI[s, I_CAP] = IDLE
            PI[s, I_REASON] = -1
    return ntr


@njit(cache=True, fastmath=False)
def init_row(PI, PF, s):
    """新对（`PairState()` 的缺省值：等级 0、IDLE、N_req 14、level_req R）。"""
    for c in range(PI.shape[1]):
        PI[s, c] = 0
    for c in range(PF.shape[1]):
        PF[s, c] = 0.0
    PF[s, F_UP0] = math.nan
    PF[s, F_DOWN0] = math.nan
    PF[s, F_TLASTOK] = math.nan
    PF[s, F_TEVAL] = math.nan
    PF[s, F_NREQ] = 14.0
    PI[s, I_REASON] = -1
    PI[s, I_LIM] = -1
    PI[s, I_KIND] = -1
    PI[s, I_LREQ] = 1
    PI[s, I_ALIVE] = 1


@njit(cache=True, fastmath=False)
def fov_rows(PI, PF, slots, n, t, tr, flags):
    """视锥外的一批对（level_m 0、条件不满足、不延期、reason fov）：flags[r] 位 0 汇总需重算、位 1 删除（不再活跃）。返回转移数。"""
    ntr = 0
    for r in range(n):
        s = slots[r]
        lv0 = PI[s, I_LEVEL]
        c0 = PI[s, I_CAP]
        ntr = fsm_row(PI, PF, s, t, 0, False, False, 0, tr, ntr, r)
        f = 0
        c1 = PI[s, I_CAP]
        if PI[s, I_LEVEL] != lv0 or ((c1 == CAPTURED or c1 == HOLDING) != (c0 == CAPTURED or c0 == HOLDING)):  # noqa: SIM109  （numba 核内保持标量比较）
            f |= 1
        if PI[s, I_LEVEL] <= 0 and c1 == IDLE and PF[s, F_UP0] != PF[s, F_UP0]:
            f |= 2
        flags[r] = f
    return ntr


# ---------------------------------------------------------------- 航迹（tracks.TrackTable 的行；逐轴闭式 Kalman）
T_AB, T_C, T_T, T_TL, T_Q2 = 6, 8, 9, 10, 11          # TF 列：x(6)、a、b、c、t、t_last、q²
TF_N = 12
TI_ST, TI_NU, TI_HN, TI_HP = 0, 1, 2, 3               # TI 列：state、n_upd、hist 个数、hist 写位置
TI_N = 4
TRK_HIST = 8


@njit(cache=True, fastmath=False)
def trk_update_row(s, new, t, z0, z1, z2, r2, v0, TF, TI, HIST, confirm_s, confirm_n):
    """第 s 条航迹的一次位置量测（`tracks.TrackTable._update_py` 的数组版，逐项相同）。"""
    if new:
        TF[s, 0] = z0
        TF[s, 1] = z1
        TF[s, 2] = z2
        TF[s, 3] = 0.0
        TF[s, 4] = 0.0
        TF[s, 5] = 0.0
        TF[s, T_AB] = r2
        TF[s, T_AB + 1] = 0.0
        TF[s, T_C] = v0 * v0
        TF[s, T_T] = t
        TF[s, T_TL] = t
        TI[s, TI_ST] = 0
        TI[s, TI_NU] = 0
        TI[s, TI_HN] = 0
        TI[s, TI_HP] = 0
    else:
        dt = t - TF[s, T_T]
        a = TF[s, T_AB]
        b = TF[s, T_AB + 1]
        c = TF[s, T_C]
        if dt > 0.0:
            q2 = TF[s, T_Q2]
            a = a + 2.0 * dt * b + dt * dt * c + q2 * (dt * dt * dt) / 3.0
            b = b + dt * c + q2 * (dt * dt) / 2.0
            c = c + q2 * dt
            TF[s, 0] = TF[s, 0] + dt * TF[s, 3]
            TF[s, 1] = TF[s, 1] + dt * TF[s, 4]
            TF[s, 2] = TF[s, 2] + dt * TF[s, 5]
            TF[s, T_T] = t
        sg = a + r2
        kp = a / sg
        kv = b / sg
        y0 = z0 - TF[s, 0]
        y1 = z1 - TF[s, 1]
        y2 = z2 - TF[s, 2]
        TF[s, 0] = TF[s, 0] + kp * y0
        TF[s, 1] = TF[s, 1] + kp * y1
        TF[s, 2] = TF[s, 2] + kp * y2
        TF[s, 3] = TF[s, 3] + kv * y0
        TF[s, 4] = TF[s, 4] + kv * y1
        TF[s, 5] = TF[s, 5] + kv * y2
        TF[s, T_AB] = (1.0 - kp) * a
        TF[s, T_AB + 1] = (1.0 - kp) * b
        TF[s, T_C] = c - kv * b
    TF[s, T_TL] = t
    TI[s, TI_NU] += 1
    hp = TI[s, TI_HP]
    HIST[s, hp] = t
    TI[s, TI_HP] = (hp + 1) % HIST.shape[1]
    if TI[s, TI_HN] < HIST.shape[1]:
        TI[s, TI_HN] += 1
    st = TI[s, TI_ST]
    if st == 2:
        TI[s, TI_ST] = 1
    elif st == 0:
        recent = 0
        for q in range(TI[s, TI_HN]):
            if t - HIST[s, q] <= confirm_s:
                recent += 1
        if recent >= confirm_n:
            TI[s, TI_ST] = 1


# ---------------------------------------------------------------- 视线之后：等级、量测与对状态
POST_COLS = {"k_los": 0, "n_eff": 1, "p": 2, "sigma": 3}
POST_N = 4
Q_KLOS_MIN, Q_HOLD, Q_PE, Q_ND, Q_NR, Q_NI, Q_E0, Q_E1 = 0, 1, 2, 3, 4, 5, 6, 7
QRM_N = 8
_LIM_ARG = (1, 3, 4, 5)
INFO_N = 9   # post_fsm 的 info：[n_meas, n_def, n_stale, first_def, n_tr, n_new, 已有对数, 有标志的行数, 新航迹数]


@njit(cache=True, fastmath=False)
def post_fsm(cand, m, core, pk, tno, n_req, n50, need, order, lout, nn, qrm, k3, tick, sensor_no, kind_code, t, smap, slots, PI, PF, FREE,
             PM, res, codes, z, dfr, flags, tr, info, pst, trk_on, q2_t, TMAP, TFREE, TFM, TF, TI, HIST, confirm_s, confirm_n, v0, tnew, agent):
    """视线结果之后的一次评估（与 `runtime._eval_np` 及其后的逐对 Python 推进逐项相同）。

    1. 视线：order[:nn] 为请求次序，lout[pos] = `LosCache.query_raw` 的输出；延期的对 dfr[j]，否则 k_los = 可见位数 / 3；
    2. 判据：k_los 只在请求了视线或延期的对上取视线结果（否则 1）；limiting 为 argmin(k_los·(1 − occ), k_light, k_c, k_blur)（首个
       最小值，NaN 优先，同 `np.argmin`），N < N_req 时为 pixels，N_eff ≥ N_req 时无；等级 D 3.5、R 14、I 22.4；延期的对等级记 0
       且不满足捕获条件；TTPF `x^E/(1 + x^E)`，`E = e0 + e1·x`；量测 `√((R·1 mrad)² + GSD² + sigma_hold²)` 乘 RNG 流 18 的三个正态；
    3. 对状态（pairs.PairTable）：smap[target_no] 为本（机体，传感器）已有对的行或 -1（调用方保证长度 > 最大 target_no）；新对
       （等级 > 0 或满足条件）从空位栈 FREE[:PM[0]] 取行并写回 smap；slots[j] 为各对的行（无对为 -1）；同名传感器换了类别时
       标记汇总；写最近测量与 N_req；`pairs.fsm_row` 推进；flags[j] 位 0 汇总需重算、位 1 删除、位 2 新对、位 3 有转移；转移写 tr（见 `fsm_row`）。

    res[j] = (k_los, n_eff, p, sigma)、codes[j] = (lim, level, cond, has_meas)、z[j]；info 见 INFO_N；pst[j] = 推进后对的 (等级, 捕获态,
    hold_s)（无对或已删除为 (0, IDLE, 0)，帧用）。
    RNG 键前缀：k3 为前三级（seed、流、agent_no），本核补第四级 `sm(k3 ^ tick)`（与 `prefix_tick` 相同）。

    trk_on（本机只有这一个量测来源）时按 j 次序用各量测更新航迹（`trk_update_row`；TMAP[target_no] 为槽位，新航迹从空位栈
    TFREE[:TFM[0]] 取并写回 TMAP，q² 取 q2_t[i]；新建的 target_no 依次写入 tnew，个数在 info[8]）。"""
    prefix = _sm(k3 ^ np.uint64(tick))
    klos_min = qrm[Q_KLOS_MIN]
    hold = qrm[Q_HOLD]
    pe = qrm[Q_PE]
    nd = qrm[Q_ND]
    nrr = qrm[Q_NR]
    ni = qrm[Q_NI]
    e0 = qrm[Q_E0]
    e1 = qrm[Q_E1]
    klos = np.zeros(m)
    for j in range(m):
        dfr[j] = False
    n_def = 0
    n_stale = 0
    first_def = -1
    for pos in range(nn):
        j = order[pos]
        if lout[pos, 2] != 0:
            dfr[j] = True
            n_def += 1
            if first_def < 0:
                first_def = pos
        else:
            mk = lout[pos, 0]
            klos[j] = float((mk & 1) + ((mk >> 1) & 1) + ((mk >> 2) & 1)) / 3.0
            if lout[pos, 1] != 0:
                n_stale += 1
    nz = np.empty(3)
    cnt = 0
    ntr = 0
    n_new = 0
    n_old = 0
    n_flag = 0
    n_tnew = 0
    for j in range(m):
        i = cand[j]
        tn_i = tno[i]
        dj = dfr[j]
        kl = klos[j] if (need[j] or dj) else 1.0
        ne = core[j, 2] * kl
        nr = n_req[i]
        v = (kl * (1.0 - pk[i, 19]), core[j, 5], core[j, 4], core[j, 6])
        am = 0
        bv = v[0]
        if bv == bv:
            for q in range(1, 4):
                x = v[q]
                if x != x:
                    am = q
                    break
                if x < bv:
                    am = q
                    bv = x
        lim = _LIM_ARG[am]
        if core[j, 1] < nr:
            lim = 2
        if ne >= nr:
            lim = -1
        lvl = 0
        if ne >= nd:
            lvl += 1
        if ne >= nrr:
            lvl += 1
        if ne >= ni:
            lvl += 1
        if dj:
            lvl = 0
        cond = (not dj) and kl >= klos_min and ne >= nr
        x = ne if (ne >= 0.0 or ne != ne) else 0.0
        x = x / n50[i]
        e = e0 + e1 * x
        xe = x ** e
        p = 1.0 if math.isinf(xe) else xe / (1.0 + xe)
        res[j, 0] = kl
        res[j, 1] = ne
        res[j, 2] = p
        res[j, 3] = math.nan
        codes[j, 0] = lim
        codes[j, 1] = lvl
        codes[j, 2] = 1 if cond else 0
        codes[j, 3] = 0
        if lvl >= 1 and not dj:
            a = core[j, 0] * pe
            g = core[j, 3]
            sig = math.sqrt(a * a + g * g + hold * hold)   # 平方取 x·x（正确舍入；numpy 路径同式）
            normals3(prefix, sensor_no, tno[i], nz)
            z[j, 0] = pk[i, 0] + sig * nz[0]
            z[j, 1] = pk[i, 1] + sig * nz[1]
            z[j, 2] = pk[i, 2] + sig * nz[2]
            res[j, 3] = sig
            codes[j, 3] = 1
            cnt += 1
            if trk_on:
                ts = TMAP[tn_i] if tn_i < TMAP.shape[0] else -1
                isnew = ts < 0
                if isnew:
                    kq = TFM[0] - 1
                    ts = TFREE[kq]
                    TFM[0] = kq
                    TMAP[tn_i] = ts
                    TF[ts, T_Q2] = q2_t[i]
                    tnew[n_tnew] = tn_i
                    n_tnew += 1
                sg_ = sig if (sig >= 1e-3 or sig != sig) else 1e-3     # np.maximum(sigma, 1e-3)
                trk_update_row(ts, isnew, t, z[j, 0], z[j, 1], z[j, 2], sg_ * sg_, v0, TF, TI, HIST, confirm_s, confirm_n)
        # ---- 对状态
        f = 0
        tn = tno[i]
        sl = smap[tn]
        slots[j] = sl
        pst[j, 0] = 0.0
        pst[j, 1] = 0.0
        pst[j, 2] = 0.0
        if sl < 0:
            if lvl == 0 and not cond:
                flags[j] = 0
                continue
            k_ = PM[0] - 1
            sl = FREE[k_]
            PM[0] = k_
            init_row(PI, PF, sl)
            PI[sl, I_TNO] = tn
            PI[sl, I_AGENT] = agent
            PI[sl, I_SEQ] = PM[1]
            PM[1] += 1
            slots[j] = sl
            smap[tn] = sl
            f |= 4
            n_new += 1
        else:
            n_old += 1
            if PI[sl, I_HASLAST] != 0 and PI[sl, I_KIND] != kind_code and PI[sl, I_LEVEL] != 0:
                f |= 1                                 # 同名传感器换了类别（EO 与 NIR 之间切换）：best_sensor 可能变化
        if nr != PF[sl, F_NREQ]:
            PF[sl, F_NREQ] = nr
            PI[sl, I_LREQ] = _lreq_nb(nr, nd, nrr, ni)
        PF[sl, F_NEFF] = ne
        PF[sl, F_GSD] = core[j, 3]
        PF[sl, F_P] = p
        PI[sl, I_LIM] = lim
        PI[sl, I_KIND] = kind_code
        PI[sl, I_HASMEAS] = codes[j, 3]
        PF[sl, F_SIG] = res[j, 3]
        if codes[j, 3] != 0:
            PF[sl, F_Z0] = z[j, 0]
            PF[sl, F_Z0 + 1] = z[j, 1]
            PF[sl, F_Z0 + 2] = z[j, 2]
        PI[sl, I_HASLAST] = 1
        lv0 = PI[sl, I_LEVEL]
        c0 = PI[sl, I_CAP]
        ntr0 = ntr
        ntr = fsm_row(PI, PF, sl, t, lvl, cond, dj, -1 if (cond or lim < 0) else lim, tr, ntr, j)
        if ntr != ntr0:
            f |= 8                                     # 本行有转移（调用方按 flags 非零取要处理的行）
        c1 = PI[sl, I_CAP]
        if PI[sl, I_LEVEL] != lv0 or ((c1 == 2 or c1 == 3) != (c0 == 2 or c0 == 3)):
            f |= 1
        if (not cond) and PI[sl, I_LEVEL] <= 0 and c1 == 0 and PF[sl, F_UP0] != PF[sl, F_UP0]:
            f |= 2
        else:
            pst[j, 0] = PI[sl, I_LEVEL]
            pst[j, 1] = c1
            h = 0.0
            if c1 == 1:
                h = t - PF[sl, F_TOK]
            elif c1 == 2:
                h = t - PF[sl, F_SINCE]
            elif c1 == 3:
                h = t - PF[sl, F_TBAD]
            pst[j, 2] = h if h > 0.0 else 0.0
        flags[j] = f
        if f != 0:
            n_flag += 1
    info[0] = cnt
    info[1] = n_def
    info[2] = n_stale
    info[3] = first_def
    info[4] = ntr
    info[5] = n_new
    info[6] = n_old
    info[7] = n_flag
    info[8] = n_tnew
    return cnt


# ---------------------------------------------------------------- 融合入口（一次评估两次进入 numba）
@njit(cache=True, fastmath=False)
def eval_a(cand, m, d, rr, pk, od, n_req, prm, ill, p, Rm, pvel, src, trk_max, core, need, keys, ca, cb, rot, order, oB, oH, oK, info,
           KEY, HSH, HT, COMP, VIS, T, PREV, NEXT, M, t, ttl, stale_ns, tot, sub, has_sub, lout, ra, rb, cnt, st):
    """`imaging_core` 之后直接在共享视线缓存（`targets.kernels_los`）上按请求次序做 `query_scan`（M19 的三条射线，need = 0b111）。
    返回需投射射线的作业数（> 0 时调用方投射 ra[:cnt[5]] → rb 后调用 `eval_b`）；info[2] = 0（格键无效）时不查缓存，返回 -1，
    调用方改走 `LosCache.query_raw`。"""
    imaging_core(cand, m, d, rr, pk, od, n_req, prm, ill, p, Rm, pvel, src, trk_max, core, need, keys, ca, cb, rot, order, oB, oH, oK, info)
    nn = info[0]
    if nn == 0:
        return 0
    if info[2] == 0:
        return -1
    return _KL.query_scan(KEY, HSH, HT, COMP, VIS, T, PREV, NEXT, M, oK, nn, 7, t, ttl, stale_ns, tot, sub, has_sub, p.reshape(1, 3), oB, oH, lout,
                          ra, rb, cnt, st)


@njit(cache=True, fastmath=False)
def eval_b(n_jobs, vis_rays, KEY, HSH, HT, COMP, VIS, T, PREV, NEXT, M, t_ns, ttl, max_entries, oK, cand, m, core, pk, tno, n_req, n50, need, order,
           lout, nn, qrm, k3, tick, sensor_no, kind_code, t, smap, slots, PI, PF, FREE, PM, res, codes, z, dfr, flags, tr, info, pst, trk_on, q2_t,
           TMAP, TFREE, TFM, TF, TI, HIST, confirm_s, confirm_n, v0, tnew, agent):
    """有作业时先回写射线结果（`apply_jobs`，时刻 t_ns 为整数纳秒），再做 `post_fsm`（t 为仿真秒）。"""
    if n_jobs > 0:
        _KL.apply_jobs(KEY, HSH, HT, COMP, VIS, T, PREV, NEXT, M, oK, nn, 7, t_ns, ttl, max_entries, lout, vis_rays)
    return post_fsm(cand, m, core, pk, tno, n_req, n50, need, order, lout, nn, qrm, k3, tick, sensor_no, kind_code, t, smap, slots, PI, PF, FREE,
                    PM, res, codes, z, dfr, flags, tr, info, pst, trk_on, q2_t, TMAP, TFREE, TFM, TF, TI, HIST, confirm_s, confirm_n, v0, tnew,
                    agent)


# ---------------------------------------------------------------- 识别物汇总（runtime._aggregate 的成像对部分）
_KIND_SENSOR = (1, 2, 3)   # eo、nir、lwir 的 best_sensor 码（runtime.SENSOR_CODE）


@njit(cache=True, fastmath=False)
def summary_rows(PI, PF, tidx, out, since, seq):
    """全部在用的对行（I_ALIVE）按识别物汇总：q = tidx[target_no]（快照下标，-1 跳过）；out[q] = (best_level, best_sensor 码, captured,
    capture_uav_no)。与按插入次序逐对的 `best_of` 相同：等级取最大值，类别取最先（I_SEQ 最小）达到该等级的对（等级 0 时不更新）；
    捕获取 since 最早者，并列取最先建立者。out 由调用方初始化为 (0, 0, 0, 0xFFFF)，since 为 NaN；seq[q, 0/1] 为工作数组。"""
    nt = tidx.shape[0]
    for s in range(PI.shape[0]):
        if PI[s, I_ALIVE] == 0:
            continue
        tn = PI[s, I_TNO]
        if tn < 0 or tn >= nt:
            continue
        q = tidx[tn]
        if q < 0:
            continue
        sq = PI[s, I_SEQ]
        lv = PI[s, I_LEVEL]
        if lv > 0 and (lv > out[q, 0] or (lv == out[q, 0] and sq < seq[q, 0])):
            k = PI[s, I_KIND]
            out[q, 0] = lv
            out[q, 1] = _KIND_SENSOR[k] if k >= 0 else 1
            seq[q, 0] = sq
        c = PI[s, I_CAP]
        if c == CAPTURED or c == HOLDING:  # noqa: SIM109  （numba 核内保持标量比较）
            out[q, 2] = 1
            sn = PF[s, F_SINCE]
            if since[q] != since[q] or sn < since[q] or (sn == since[q] and sq < seq[q, 1]):
                since[q] = sn
                seq[q, 1] = sq
                out[q, 3] = PI[s, I_AGENT]


@njit(cache=True, fastmath=False)
def summary_cols(PI, PF, tidx, out, since, seq, rows, cb, cs, cc, cu, write):
    """`summary_rows` 连同初始化（out = (0, 0, 0, 0xFFFF)、since = NaN）；write 时再把 out 写入实体表四列的 rows 行（与 numpy 的
    `c[rows] = out[:, k]` 相同的取值；值域在列类型之内）。"""
    n = out.shape[0]
    for q in range(n):
        out[q, 0] = 0
        out[q, 1] = 0
        out[q, 2] = 0
        out[q, 3] = 0xFFFF
        since[q] = np.nan
    summary_rows(PI, PF, tidx, out, since, seq)
    if write:
        for q in range(n):
            r = rows[q]
            cb[r] = out[q, 0]
            cs[r] = out[q, 1]
            cc[r] = out[q, 2]
            cu[r] = out[q, 3]


# ---------------------------------------------------------------- 预热（运行期签名：C 连续 float64 / int64 / bool 数组）
def warmup() -> None:
    """按运行期签名编译或读缓存（装配时调用；M08-FR-005）：运行期入口 fov_gate、eval_a、eval_b、normals3、fov_rows、summary_rows、summary_cols。"""
    if not HAVE_NUMBA:
        return
    from awr.sim.targets.los_cache import LosCache

    from . import pairs as PR
    from . import tracks as TR

    n = 2
    pk = np.zeros((n, PK_N))
    pk[:, 7:10] = 1.0
    pk[:, 10] = np.nan
    pk[:, 0] = [10.0, 20.0]
    d = np.zeros((n, 3))
    rr = np.zeros(n)
    cand = np.zeros(n, np.int64)
    m = fov_gate(pk[:, 0:3].copy(), np.zeros(3), np.eye(3), 0.5, 0.5, d, rr, cand)
    m = max(int(m), 1)
    prm = np.zeros(PRM_N)
    prm[P_F], prm[P_PP], prm[P_CSPAN], prm[P_SNR_DEN], prm[P_SNR_SPAN], prm[P_ND] = 4.8, 3.0, 0.2, 1.0, 3.0, 3.5
    core = np.zeros((n, CORE_N))
    need = np.zeros(n, np.bool_)
    keys = np.zeros((n, 6), np.int64)
    od = np.zeros(n)
    nreq = np.full(n, 14.0)
    order = np.zeros(n, np.int64)
    oB, oH, oK = np.zeros((n, 3)), np.zeros(n), np.zeros((n, 6), np.int64)
    info = np.zeros(INFO_N, np.int64)
    los = LosCache(None, max_entries=8)
    KEY, HSH, HT, COMP, VIS, TT, PREV, NEXT, LM, ttl, stale_ns, max_e, tot, sub, has_sub, lst = los.kernel_tables("M19", n)
    lout = np.zeros((n, 5), np.int64)
    ra, rb, cnt = np.zeros((3 * n, 3)), np.zeros((3 * n, 3)), np.zeros(6, np.int64)
    qrm = np.array([2.0 / 3.0, 0.02, 1e-3, 3.5, 14.0, 22.4, 2.7, 0.7])
    tab = PR.PairTable(8)
    trk = TR.TrackTable()
    trk.ensure(4, n)
    for kind in (0.0, 1.0, 2.0):
        prm[P_KIND] = kind
        nj = eval_a(cand, m, d, rr, pk, od, nreq, prm, np.ones((1, 3)), np.zeros(3), np.eye(3), np.zeros(3), True, 0.1, core, need, keys, 5.0, 2.0,
                    0, order, oB, oH, oK, info, KEY, HSH, HT, COMP, VIS, TT, PREV, NEXT, LM, 1, ttl, stale_ns, tot, sub, has_sub, lout, ra, rb, cnt,
                    lst)
        nn = int(info[0])
        res = np.zeros((n, POST_N))
        codes = np.zeros((n, 4), np.int64)
        z = np.zeros((n, 3))
        slots = np.full(n, -1, np.int64)
        smap = np.full(4, -1, np.int64)
        flags = np.zeros(n, np.int64)
        tr = np.zeros((2 * n + 1, 7))
        eval_b(nj, np.ones(max(int(cnt[5]), 1), np.bool_), KEY, HSH, HT, COMP, VIS, TT, PREV, NEXT, LM, 1, ttl, max_e, oK, cand, m, core, pk,
               np.arange(n, dtype=np.int64), nreq, np.full(n, 8.0), need, order, lout, nn, qrm, np.uint64(prefix3(1, 18, 1)), 7, 0, 0, 0.5, smap,
               slots, tab.PI, tab.PF, tab.FREE, tab.PM, res, codes, z, np.zeros(n, np.bool_), flags, tr, info, np.zeros((n, 3)), kind == 0.0,
               np.ones(n), trk.TMAP, trk.FREE, trk.FM, trk.TF, trk.TI, trk.HIST, TR.CONFIRM_S, 3, TR.V0, np.zeros(n, np.int64), 1)
    summary_rows(tab.PI, tab.PF, np.zeros(4, np.int64), np.zeros((1, 4), np.int64), np.zeros(1), np.zeros((1, 2), np.int64))
    u1, u2 = np.zeros(2, np.uint8), np.zeros(2, np.uint16)
    summary_cols(tab.PI, tab.PF, np.zeros(4, np.int64), np.zeros((1, 4), np.int64), np.zeros(1), np.zeros((1, 2), np.int64),
                 np.zeros(1, np.int64), u1, u1.copy(), u1.copy(), u2, True)
    PR.fov_rows(tab.PI, tab.PF, np.zeros(1, np.int64), 1, 0.7, np.zeros((3, 7)), np.zeros(1, np.int64))
    normals3(np.uint64(1), 0, 0, np.zeros(3))
