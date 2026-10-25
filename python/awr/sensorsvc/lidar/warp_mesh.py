"""网格 LiDAR GPU 产品路径（M25 §6.8.2–§6.8.7、M25-FR-070；D3-AC-12）：NVIDIA Warp `mesh_query_ray`，与 CPU 参考 `mesh.py`
（Open3D）同一算法与随机键；一次 launch 扫描一批"机 × 帧"。

- 批：`scan_batch(reqs)` 一线程一射线，`tid = f·N + i`（f 为批内帧序号，N = 20,000），分三段内核（射线生成、静态求交、演员与回波，
  经设备缓冲区衔接，降低单个内核的寄存器占用）；`scan(...)` 为批大小 1，签名与 `MeshLidar.scan` 相同。每帧结果只取决于该帧的
  输入与帧键，与批大小、批内次序无关（M25-AC-012）。
- 花样：MID-360 帧不变表（`pattern` 模块的 float64 表）常驻设备，每帧只传 float64 约化后的 (cos θ0, sin θ0, az0)，与 `mid360_pattern`
  同式；角度噪声只扰动求交方向，输出点沿名义方向 × 测得距离（点时刻传感器系）。
- 逐点位姿：K 个节点（float64 世界帧位置、四元数同半球），槽位 i 的时刻 t_s + i·5 µs，位置线性、四元数 nlerp，float64 计算。
- 静态：每瓦片一个 Warp `Mesh`（瓦片局部 float32 顶点，与 `MeshWorldQuery` 同一瓦片与面序；BVH 缺省 cubql），射线起点以 float64
  减瓦片原点后转 float32 求交；候选瓦片以射线段与外扩 1 m 的瓦片 AABB 做 float32 slab 判定（保守，只影响计算量；float64 判定的
  除法在 A40 上使 100 帧批的内核耗时约增加一倍）；命中距离按命中三角形平面以 float64 重算（与 `query._plane_t` 同式），
  取各瓦片最近命中（严格小于，瓦片次序与 CPU 相同）。
- 动态实体（ADR-198）：帧起点相同的帧共用一个 `_ActorTrack`（CPU 参考的同一磁带时刻函数重建）。主机把每个候选演员的帧内状态展开成
  分段表：校验通过的区间为 4 节点 Lagrange 三次段，校验不过的区间二分到不多于 `SEG_DIRECT_SLOTS` 个槽位时刻后逐时刻直接取样
  （磁带帧边界、可见性与容器阶跃）。内核按射线自身时刻 t_s + i·5 µs 查段求值，候选演员以与 CPU 相同的扫掠包围球筛选（float32，
  半径外扩 5 cm，候选集合是 CPU 的超集，球外射线不可能命中），射线变换到演员局部系与代理网格（与相机同一几何）求交，取较近者
  （实例 idx + 1）。
- 回波、噪声、虚警、void 与输出字段：与 `mesh.py` 逐式相同（float64）；随机数为 `cbrng.sm` 的 Warp 实现，帧键与槽位键相同，
  同键给出同一 64 位整数。天气为帧首常数 `beta_905`（消光：量程缩短与回波减弱，三档均生效）；`beta_905_per_m` 给出 M25 §6.8.5 的雾、雨换算。
- 输出：设备端前缀和（`wp.utils.array_scan`，确定性）把有效点压缩为 `LIDAR_RECORD` 记录，只拷回有效点、全部槽位真值（inst u16、
  flags u8）与逐帧计数，经锁页缓冲区环回传（`copy=False` 时直接返回其视图）。内核无浮点原子（逐帧演员计数为整数原子加）。

与 CPU 参考的差异只来自求交实现：Warp（Woop 水密求交）与 Embree 的 float32 命中距离与法线舍入不同，同一三角形命中时平面重算后的
距离差在 1e-6 m 量级；共边或掠射处可能命中相邻三角形（D3-AC-12：逐射线距离差 p99 ≤ 1 mm、最大 ≤ 1 cm，命中不一致 ≤ 1e-4）。

依赖：numpy、warp-lang（d3-sensorsvc 镜像离线安装，`tools/deploy/dmax/images/sensorsvc/requirements-warp.lock`）。无 CUDA 设备时
`open_mesh_lidar` 回退到 CPU 参考；`WarpMeshLidar(device="cpu")` 在 Warp 的 CPU 设备上运行同一内核（测试用）。
"""

import functools
import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from awr.bench.recorder.lidar import LIDAR_RECORD
from awr.sensorsvc.lidar.mesh import (
    _VIS,
    CH_ANG,
    CH_BASE,
    CH_DET,
    CH_FA,
    CH_FA_R,
    CH_RANGE,
    INST_NONE,
    INST_SPURIOUS,
    STREAM_M25,
    SWEEP_MARGIN_M,
    TIER_SCALE,
    LidarScan,
    MeshLidar,
    _ActorTrack,
    rho_sem,
)
from awr.sensorsvc.lidar.model import LidarModel
from awr.sim.sensors import cbrng
from awr.sim.sensors.lidar import pattern as pat
from awr.world.mesh.query import PLANE_PARALLEL, PLANE_TOL_M

__all__ = ["BACKEND", "SEG_DIRECT_SLOTS", "ActorSegments", "LidarFrameReq", "WarpMeshLidar", "actor_segments", "beta_905_per_m",
           "eval_segments", "open_mesh_lidar", "rng_warp", "warp_cuda_available"]

BACKEND = "warp-mesh"                    # M25 §6.17 SensorBackend.backend
SEG_DIRECT_SLOTS = 4                     # 校验不过的区间二分到槽位时刻数不多于此值后逐时刻直接取样
TILE_SLAB_PAD_M = 1.0                    # 候选瓦片 float32 slab 判定的 AABB 外扩（远大于 float32 世界坐标舍入，判定保守，只影响计算量）
SPHERE_PAD_M = 0.05                      # 扫掠包围球 float32 筛选的半径外扩（远大于 float32 舍入；球外射线不可能命中演员）
BLOCK_DIM = 128                          # 三段内核的线程块大小（A40 实测 64–128 优于缺省 256）
HOST_RING = 2                            # 主机端锁页回传缓冲区槽位数（`scan_batch(copy=False)` 的视图有效期）
ACTOR_MAX_T = 1.0e30                     # 演员求交不设上限（CPU 参考的 Embree 求交同样不设 tmax，较近者判定在其后）
_WP_BVH = "cubql"                        # CUDA 设备的 BVH 构建器（M25 §6.8.2）；`AWR_WARP_BVH` 可改为 lbvh、sah、median


def warp_cuda_available() -> bool:
    """warp-lang 可导入且至少有一个 CUDA 设备。"""
    try:
        wp = _wp()
    except ImportError:
        return False
    return wp.get_cuda_device_count() > 0


def open_mesh_lidar(q, model: LidarModel, actors: list[dict] | None = None, backend: str | None = None, device: str | None = None):
    """按后端构造网格 LiDAR：`backend`（缺省取环境变量 `AWR_LIDAR_BACKEND`，再缺省 `auto`）为 `auto`（有 CUDA 设备用 Warp，否则 CPU
    参考）、`warp`（Warp，`device` 缺省 cuda:0，无 CUDA 时为 Warp CPU 设备）或 `cpu`（Open3D CPU 参考 `MeshLidar`）。两者接口相同。"""
    b = (backend or os.environ.get("AWR_LIDAR_BACKEND", "") or "auto").strip().lower()
    if b not in ("auto", "warp", "cpu"):
        raise ValueError(f"AWR_LIDAR_BACKEND must be auto, warp or cpu (got {b!r})")
    if b == "cpu" or (b == "auto" and not warp_cuda_available()):
        return MeshLidar(q, model, actors)
    if b == "warp" and not str(device or "cuda").startswith("cpu"):
        from awr.sensorsvc.gpuhealth import gpu_expected, require_gpu

        # 本应有 GPU 的进程（容器带 NVIDIA 设备节点或 NVIDIA_VISIBLE_DEVICES）Warp 却看不到 CUDA 设备（初始化报 CUDA error 100）：
        # 设备已丢失，抛 647 平台错误，不静默退到 Warp CPU 设备（K18，B21 遗留 3）
        if gpu_expected() and not warp_cuda_available():
            require_gpu(egl=False, warp=True, stage="lidar", mode="nodes")
    return WarpMeshLidar(q, model, actors, device=device)


def beta_905_per_m(visibility_m: float | None = None, rain_mmh: float = 0.0) -> float:
    """M25 §6.8.5 的 905 nm 消光系数（1/m）：雾按 Kim（β = 3.912/V·(905/550)^−q，q 由 V（km）分段），雨 β = 1.076·R^0.67 /（4.343·1000）
    （10 mm/h 时 1.16e-3 /m）；帧首常数，作为 `scan(..., beta_905=)` 的输入。"""
    beta = 0.0
    if visibility_m is not None and visibility_m > 0:
        v = float(visibility_m) / 1000.0
        if v > 50:
            qk = 1.6
        elif v > 6:
            qk = 1.3
        elif v > 1:
            qk = 0.16 * v + 0.34
        elif v > 0.5:
            qk = v - 0.5
        else:
            qk = 0.0
        beta += 3.912 / float(visibility_m) * (905.0 / 550.0) ** (-qk)
    if rain_mmh > 0:
        beta += 1.076 * float(rain_mmh) ** 0.67 / (4.343 * 1000.0)
    return beta


@dataclass(frozen=True)
class LidarFrameReq:
    """批内一帧：参数含义与 `MeshLidar.scan` 相同（`nodes` 为 K 个 (R_ws WORLD←FLU, p_w)，时刻 t_start + 10 ms·m）。"""

    frame_seq: int
    t_start_ns: int
    nodes: Sequence[tuple[np.ndarray, np.ndarray]]
    seed: int = 0
    uav_no: int = 0
    sensor_no: int = 0
    tier: str = "realistic"
    beta_905: float = 0.0


# ---------------------------------------------------------------- 演员分段表（主机端，与 CPU 参考同一时刻函数重建）


@dataclass
class ActorSegments:
    """一个演员在一帧内的分段表（按 lo 升序；时刻 t 取 lo ≤ t 的最后一段）。`n > 0` 为 Lagrange 三次段：
    pos = p0 + Σ W_j(τ)·dP_j、yaw = y0 + Σ W_j(τ)·dY_j，τ = (t − lo)/n；`n = 0` 为直接样本（只在 t = lo 处使用）。"""

    lo: np.ndarray          # (S,) int64
    n: np.ndarray           # (S,) int64
    tau: np.ndarray         # (S, 4)
    dP: np.ndarray          # (S, 4, 3)
    dY: np.ndarray          # (S, 4)
    p0: np.ndarray          # (S, 3)
    y0: np.ndarray          # (S,)
    state: np.ndarray       # (S,) u8


def actor_segments(tr: _ActorTrack, a: int, t_slots: np.ndarray) -> ActorSegments:
    """演员 a 在槽位时刻 `t_slots`（升序 int64，首末为 tr.t0、tr.t1）上的分段表：区间 [lo, hi] 用 `tr._fit` 的 4 节点插值与 2 点校验
    （与 CPU 参考同一样本缓存与容差），校验不过在中点二分；槽位时刻不多于 `SEG_DIRECT_SLOTS` 的区间逐时刻直接取样。中点时刻归右半区间
    （与 `_ActorTrack._eval` 相同），末区间含 t1。"""
    rows: list[tuple] = []
    z4, z43 = np.zeros(4), np.zeros((4, 3))

    def rec(lo: int, hi: int, last: bool) -> None:
        i0 = int(np.searchsorted(t_slots, lo, "left"))
        i1 = int(np.searchsorted(t_slots, hi, "right" if last else "left"))
        if i1 <= i0:
            return
        if i1 - i0 > SEG_DIRECT_SLOTS:
            tau, dP, dY, p0, y0, s0, ok, mid, _ts = tr._fit(lo, hi)
            if ok[a]:
                rows.append((lo, hi - lo, tau, dP[:, a], dY[:, a], p0[a], y0[a], s0[a]))
                return
            rec(lo, mid, False)
            rec(mid, hi, last)
            return
        for t in t_slots[i0:i1]:
            p, y, s, _c = tr.sample(int(t))
            rows.append((int(t), 0, z4, z43, z4, p[a], y[a], s[a]))
            tr.direct += 1

    rec(tr.t0, tr.t1, True)
    return ActorSegments(np.array([r[0] for r in rows], np.int64), np.array([r[1] for r in rows], np.int64),
                         np.array([r[2] for r in rows], np.float64).reshape(-1, 4), np.array([r[3] for r in rows], np.float64).reshape(-1, 4, 3),
                         np.array([r[4] for r in rows], np.float64).reshape(-1, 4), np.array([r[5] for r in rows], np.float64).reshape(-1, 3),
                         np.array([r[6] for r in rows], np.float64), np.array([r[7] for r in rows], np.uint8))


def eval_segments(sg: ActorSegments, t_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """分段表在整数时刻 t_ns 处的 (pos (n, 3), yaw (n,), state (n,))：numpy 参考，与内核 `seg_eval` 同式（供测试）。"""
    t_ns = np.asarray(t_ns, np.int64)
    k = np.searchsorted(sg.lo, t_ns, "right") - 1
    pos, yaw = sg.p0[k].copy(), sg.y0[k].copy()
    m = sg.n[k] > 0
    if m.any():
        kk = k[m]
        tau = (t_ns[m] - sg.lo[kk]).astype(np.float64) / sg.n[kk].astype(np.float64)
        W = _lagrange_rows(sg.tau[kk], tau)
        pos[m] = sg.p0[kk] + np.einsum("nj,njk->nk", W, sg.dP[kk])
        yaw[m] = sg.y0[kk] + np.einsum("nj,nj->n", W, sg.dY[kk])
    return pos, yaw, sg.state[k]


def _lagrange_rows(tn: np.ndarray, tau: np.ndarray) -> np.ndarray:
    """逐行节点的 4 点 Lagrange 基函数值：tn (n, 4)、tau (n,) → (n, 4)（`mesh._lagrange` 的逐行版本，同一运算次序）。"""
    W = np.ones((len(tau), 4))
    for j in range(4):
        for m in range(4):
            if m != j:
                W[:, j] *= (tau - tn[:, m]) / (tn[:, j] - tn[:, m])
    return W


# ---------------------------------------------------------------- Warp 内核


@functools.cache
def _wp():
    import warp as wp

    if hasattr(wp.config, "log_level") and hasattr(wp, "LOG_WARNING"):
        wp.config.log_level = wp.LOG_WARNING
    else:                                                       # pragma: no cover - 旧版 warp
        wp.config.quiet = True
    wp.init()
    return wp


@functools.cache
def _kernels():
    wp = _wp()
    f64 = wp.float64
    K_C1 = wp.constant(wp.uint64(int(cbrng.C1)))
    K_C2 = wp.constant(wp.uint64(int(cbrng.C2)))
    K_C3 = wp.constant(wp.uint64(int(cbrng.C3)))
    INV53 = wp.constant(f64(cbrng.INV53))
    TWO_PI = wp.constant(f64(2 * np.pi))
    D2R = wp.constant(f64(np.pi / 180.0))                       # np.radians 的乘数
    R2D = wp.constant(f64(180.0 / np.pi))                       # np.degrees 的乘数
    ONE = wp.constant(f64(1.0))
    ZERO = wp.constant(f64(0.0))
    HALF = wp.constant(f64(0.5))
    P_PAR = wp.constant(f64(PLANE_PARALLEL))
    P_TOL = wp.constant(f64(PLANE_TOL_M))
    A_MAX_T = wp.constant(float(ACTOR_MAX_T))
    U_CH_ANG, U_CH_DET, U_CH_RANGE = wp.constant(CH_ANG), wp.constant(CH_DET), wp.constant(CH_RANGE)
    U_CH_FA, U_CH_FA_R = wp.constant(CH_FA), wp.constant(CH_FA_R)
    I_NONE, I_SPUR = wp.constant(INST_NONE), wp.constant(INST_SPURIOUS)

    @wp.struct
    class Rec:                                                  # 与 LIDAR_RECORD 同布局（20 B）
        x: wp.float32
        y: wp.float32
        z: wp.float32
        offset_time: wp.uint32
        reflectivity: wp.uint8
        tag: wp.uint8
        line: wp.uint8
        pad: wp.uint8

    @wp.struct
    class Params:
        n_slots: int
        dt_ns: wp.int64
        K: int
        seg_ns: wp.int64
        hard_max: wp.float64
        blind: wp.float64
        r10: wp.float64
        expo: wp.float64
        sig_near: wp.float64
        sig_far: wp.float64
        ang_rad: wp.float64
        fa: wp.float64
        n_tiles: int
        n_sem: int
        rays: int

    @wp.func
    def sm(z: wp.uint64):
        z = z + K_C1
        z = (z ^ (z >> wp.uint64(30))) * K_C2
        z = (z ^ (z >> wp.uint64(27))) * K_C3
        return z ^ (z >> wp.uint64(31))

    @wp.func
    def slot_key(kf: wp.uint64, i: int, c: int):
        return sm(kf ^ (wp.uint64(i) * wp.uint64(8) + wp.uint64(c)))

    @wp.func
    def u01(h: wp.uint64):
        return f64(h >> wp.uint64(11)) * INV53

    @wp.func
    def normal2(h: wp.uint64):
        b = sm(h ^ K_C3)
        u1 = (f64(h >> wp.uint64(11)) + HALF) * INV53
        u2 = u01(b)
        r = wp.sqrt(f64(-2.0) * wp.log(u1))
        return wp.vec2d(r * wp.cos(TWO_PI * u2), r * wp.sin(TWO_PI * u2))

    @wp.func
    def dir_flu(az_deg: wp.float64, el_deg: wp.float64):
        a = az_deg * D2R
        e = el_deg * D2R
        ce = wp.cos(e)
        return wp.vec3d(ce * wp.cos(a), ce * wp.sin(a), wp.sin(e))

    @wp.func
    def seg_box(o: wp.vec3, inv: wp.vec3, tmax: wp.float32, lo: wp.vec3, hi: wp.vec3):
        """射线段 [0, tmax] 与 AABB 是否相交（float32 slab；AABB 主机端外扩 `TILE_SLAB_PAD_M`，判定保守）。"""
        ta = wp.cw_mul(lo - o, inv)
        tb = wp.cw_mul(hi - o, inv)
        t0 = wp.max(wp.max(wp.min(ta[0], tb[0]), wp.min(ta[1], tb[1])), wp.max(wp.min(ta[2], tb[2]), 0.0))
        t1 = wp.min(wp.min(wp.max(ta[0], tb[0]), wp.max(ta[1], tb[1])), wp.min(wp.max(ta[2], tb[2]), tmax))
        return t0 <= t1

    @wp.func
    def safe_rcp(x: wp.float32):
        """slab 用倒数：|x| < 1e-12 时按 ±1e-12 取（与 Warp `mesh_query_ray_safe_dir` 同法，避免 0·inf）。"""
        y = x
        if wp.abs(x) < 1.0e-12:
            y = wp.where(x < 0.0, -1.0e-12, 1.0e-12)
        return 1.0 / y

    @wp.func
    def plane_t(mesh: wp.uint64, face: int, rel: wp.vec3d, d: wp.vec3d, t32: wp.float64):
        """`query._plane_t`：命中三角形平面以 float64 重算距离；近平行、非有限或与 float32 距离相差超过 1 cm 时保留 float32 距离。"""
        a = wp.mesh_get_point(mesh, face * 3 + 0)
        b = wp.mesh_get_point(mesh, face * 3 + 1)
        c = wp.mesh_get_point(mesh, face * 3 + 2)
        p0 = wp.vec3d(f64(a[0]), f64(a[1]), f64(a[2]))
        p1 = wp.vec3d(f64(b[0]), f64(b[1]), f64(b[2]))
        p2 = wp.vec3d(f64(c[0]), f64(c[1]), f64(c[2]))
        n = wp.cross(p1 - p0, p2 - p0)
        den = wp.dot(n, d)
        num = wp.dot(n, p0 - rel)
        nn = wp.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
        out = t32
        if wp.abs(den) > P_PAR * nn:
            t = num / den
            if wp.isfinite(t) and wp.abs(t - t32) <= P_TOL:
                out = wp.max(t, ZERO)
        return out

    @wp.func
    def f32v(v: wp.vec3d):
        return wp.vec3(wp.float32(v[0]), wp.float32(v[1]), wp.float32(v[2]))

    @wp.kernel
    def k_rays(prm: Params,
               t_sel: wp.array(dtype=wp.float64), t_cd: wp.array(dtype=wp.float64), t_sd: wp.array(dtype=wp.float64),
               t_ec: wp.array(dtype=wp.float64), t_sazsec: wp.array(dtype=wp.float64), t_sd0: wp.array(dtype=wp.float64),
               t_cd0: wp.array(dtype=wp.float64), t_azl: wp.array(dtype=wp.float64),
               fr_c0: wp.array(dtype=wp.float64), fr_s0: wp.array(dtype=wp.float64), fr_az0: wp.array(dtype=wp.float64),
               fr_sc: wp.array(dtype=wp.float64), fr_key: wp.array(dtype=wp.uint64),
               node_p: wp.array(dtype=wp.vec3d), node_q: wp.array(dtype=wp.vec4d),
               r_o: wp.array(dtype=wp.vec3d), r_d: wp.array(dtype=wp.vec3d), r_dn: wp.array(dtype=wp.vec3d)):
        """第 1 段：槽位射线（起点、求交方向、名义方向，float64）。三段内核共用缓冲区以降低单个内核的寄存器占用。"""
        tid = wp.tid()
        N = prm.n_slots
        f = tid // N
        i = tid - f * N
        # ---- 槽位时刻与逐点位姿（`MeshLidar.scan` 同式，float64）
        t_rel = wp.int64(i) * prm.dt_ns
        sg = wp.min(int(t_rel // prm.seg_ns), prm.K - 2)
        u = f64(t_rel - wp.int64(sg) * prm.seg_ns) / f64(prm.seg_ns)
        b = f * prm.K + sg
        pa = node_p[b]
        pb = node_p[b + 1]
        o = wp.vec3d(pa[0] * (ONE - u) + pb[0] * u, pa[1] * (ONE - u) + pb[1] * u, pa[2] * (ONE - u) + pb[2] * u)
        qa = node_q[b]
        qb = node_q[b + 1]
        qx = qa[0] * (ONE - u) + qb[0] * u
        qy = qa[1] * (ONE - u) + qb[1] * u
        qz = qa[2] * (ONE - u) + qb[2] * u
        qw = qa[3] * (ONE - u) + qb[3] * u
        qn = wp.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
        qx = qx / qn
        qy = qy / qn
        qz = qz / qn
        qw = qw / qn
        two = f64(2.0)
        r00 = ONE - two * (qy * qy + qz * qz)
        r01 = two * (qx * qy - qz * qw)
        r02 = two * (qx * qz + qy * qw)
        r10 = two * (qx * qy + qz * qw)
        r11 = ONE - two * (qx * qx + qz * qz)
        r12 = two * (qy * qz - qx * qw)
        r20 = two * (qx * qz - qy * qw)
        r21 = two * (qy * qz + qx * qw)
        r22 = ONE - two * (qx * qx + qy * qy)
        # ---- 花样（`pattern.mid360_pattern` 同式）与角度噪声
        c0 = fr_c0[f]
        s0 = fr_s0[f]
        el = t_sel[i] * (t_cd[i] * c0 - t_sd[i] * s0) + t_ec[i]
        az = t_sazsec[i] * (t_sd0[i] * c0 + t_cd0[i] * s0) + t_azl[i] + fr_az0[f]
        az = wp.mod(az, f64(360.0))
        if az < ZERO:
            az = az + f64(360.0)
        d_nom = dir_flu(az, el)
        sc = fr_sc[f]
        kf = fr_key[f]
        d_true = d_nom
        if sc > ZERO:
            na = normal2(slot_key(kf, i, U_CH_ANG))
            s = prm.ang_rad * sc
            d_true = dir_flu(az + (na[0] * s) * R2D, el + (na[1] * s) * R2D)
        d = wp.vec3d(r00 * d_true[0] + r01 * d_true[1] + r02 * d_true[2], r10 * d_true[0] + r11 * d_true[1] + r12 * d_true[2],
                     r20 * d_true[0] + r21 * d_true[1] + r22 * d_true[2])
        r_o[tid] = o
        r_d[tid] = d
        r_dn[tid] = d_nom

    @wp.kernel
    def k_static(prm: Params, r_o: wp.array(dtype=wp.vec3d), r_d: wp.array(dtype=wp.vec3d),
                 tile_mesh: wp.array(dtype=wp.uint64), tile_org: wp.array(dtype=wp.vec3d), tile_lo: wp.array(dtype=wp.vec3),
                 tile_hi: wp.array(dtype=wp.vec3), tile_t0: wp.array(dtype=wp.int32),
                 s_t: wp.array(dtype=wp.float64), s_prim: wp.array(dtype=wp.int32), s_nrm: wp.array(dtype=wp.vec3)):
        """第 2 段：静态网格最近命中（未命中 s_t = −1）。"""
        tid = wp.tid()
        o = r_o[tid]
        d = r_d[tid]
        two = f64(2.0)
        # ---- 静态网格（瓦片局部 float32 求交 + float64 平面重算）
        tmax = prm.hard_max
        best = tmax
        hit = int(0)  # noqa: UP018, RUF046
        prim = int(-1)  # noqa: UP018, RUF046
        nrm = wp.vec3(0.0, 0.0, 0.0)
        d32 = f32v(d)
        o32 = f32v(o)
        inv = wp.vec3(safe_rcp(d32[0]), safe_rcp(d32[1]), safe_rcp(d32[2]))
        tm32 = wp.float32(tmax)
        for k in range(prm.n_tiles):
            if seg_box(o32, inv, tm32, tile_lo[k], tile_hi[k]):
                rel = o - tile_org[k]
                ql = wp.mesh_query_ray(tile_mesh[k], f32v(rel), d32, wp.float32(tmax + two * P_TOL))
                if ql.result:
                    th = plane_t(tile_mesh[k], ql.face, rel, d, f64(ql.t))
                    if th <= tmax and (hit == 0 or th < best):
                        best = th
                        hit = 1
                        prim = tile_t0[k] + ql.face
                        nrm = ql.normal
        s_t[tid] = f64(-1.0)
        if hit == 1:
            s_t[tid] = best
        s_prim[tid] = prim
        s_nrm[tid] = nrm

    @wp.kernel
    def k_scan(prm: Params, r_o: wp.array(dtype=wp.vec3d), r_d: wp.array(dtype=wp.vec3d), r_dn: wp.array(dtype=wp.vec3d),
               s_t: wp.array(dtype=wp.float64), s_prim: wp.array(dtype=wp.int32), s_nrm: wp.array(dtype=wp.vec3),
               fr_sc: wp.array(dtype=wp.float64), fr_beta: wp.array(dtype=wp.float64), fr_key: wp.array(dtype=wp.uint64),
               fr_grp: wp.array(dtype=wp.int32), fr_t0: wp.array(dtype=wp.int64), sem_tab: wp.array(dtype=wp.uint8),
               rho_tab: wp.array(dtype=wp.float64),
               grp_a0: wp.array(dtype=wp.int32), act_idx: wp.array(dtype=wp.int32), act_mesh: wp.array(dtype=wp.uint64),
               act_c: wp.array(dtype=wp.vec3d), act_r2: wp.array(dtype=wp.float32), act_sem: wp.array(dtype=wp.int32),
               act_s0: wp.array(dtype=wp.int32), act_s1: wp.array(dtype=wp.int32),
               seg_lo: wp.array(dtype=wp.int64), seg_n: wp.array(dtype=wp.int64), seg_tau: wp.array(dtype=wp.vec4d),
               seg_dp: wp.array2d(dtype=wp.vec3d), seg_dy: wp.array(dtype=wp.vec4d), seg_p0: wp.array(dtype=wp.vec3d),
               seg_y0: wp.array(dtype=wp.float64), seg_vis: wp.array(dtype=wp.int32),
               o_rec: wp.array(dtype=Rec), o_inst: wp.array(dtype=wp.uint16), o_flags: wp.array(dtype=wp.uint8),
               o_valid: wp.array(dtype=wp.int32), o_ahit: wp.array(dtype=wp.int32), o_aray: wp.array(dtype=wp.int32),
               o_t: wp.array(dtype=wp.float64), o_prim: wp.array(dtype=wp.int32), o_ainst: wp.array(dtype=wp.int32)):
        """第 3 段：动态实体（逐射线时刻）、回波模型与逐槽位输出。"""
        tid = wp.tid()
        N = prm.n_slots
        f = tid // N
        i = tid - f * N
        t_rel = wp.int64(i) * prm.dt_ns
        o = r_o[tid]
        d = r_d[tid]
        d_nom = r_dn[tid]
        sc = fr_sc[f]
        kf = fr_key[f]
        tmax = prm.hard_max
        best = s_t[tid]
        hit = int(0)  # noqa: UP018, RUF046
        if best >= ZERO:
            hit = 1
        prim = s_prim[tid]
        n32 = s_nrm[tid]
        nrm = wp.vec3d(f64(n32[0]), f64(n32[1]), f64(n32[2]))
        sem = int(0)  # noqa: UP018, RUF046
        if hit == 1 and prim < prm.n_sem:
            sem = int(sem_tab[prim])
        # ---- 动态实体（逐射线时刻；扫掠包围球筛选与 CPU 相同）
        inst = int(0)  # noqa: UP018, RUF046
        g = int(fr_grp[f])
        if g >= 0:
            L = tmax
            if hit == 1:
                L = best
            t_abs = fr_t0[f] + t_rel
            d32 = f32v(d)
            L32 = wp.float32(L)
            for a in range(int(grp_a0[g]), int(grp_a0[g + 1])):
                w = f32v(act_c[a] - o)                           # 扫掠球筛选：float32，半径外扩 SPHERE_PAD_M（保守，只影响候选数）
                sp = wp.clamp(wp.dot(w, d32), 0.0, L32)
                e = w - d32 * sp
                if wp.dot(e, e) <= act_r2[a]:
                    lo = int(act_s0[a])                          # 最后一个 seg_lo ≤ t_abs 的段（二分）
                    hi = int(act_s1[a])
                    while hi - lo > 1:
                        mid = (lo + hi) // 2
                        if seg_lo[mid] <= t_abs:
                            lo = mid
                        else:
                            hi = mid
                    pos = seg_p0[lo]
                    yaw = seg_y0[lo]
                    if seg_n[lo] > wp.int64(0):
                        tau = f64(t_abs - seg_lo[lo]) / f64(seg_n[lo])
                        tn = seg_tau[lo]
                        w0 = ONE
                        w1 = ONE
                        w2 = ONE
                        w3 = ONE
                        for m in range(4):                       # `mesh._lagrange` 同一运算次序
                            if m != 0:
                                w0 = w0 * ((tau - tn[m]) / (tn[0] - tn[m]))
                            if m != 1:
                                w1 = w1 * ((tau - tn[m]) / (tn[1] - tn[m]))
                            if m != 2:
                                w2 = w2 * ((tau - tn[m]) / (tn[2] - tn[m]))
                            if m != 3:
                                w3 = w3 * ((tau - tn[m]) / (tn[3] - tn[m]))
                        dp = seg_dp[lo, 0] * w0 + seg_dp[lo, 1] * w1 + seg_dp[lo, 2] * w2 + seg_dp[lo, 3] * w3
                        pos = pos + dp
                        dy = seg_dy[lo]
                        yaw = yaw + (w0 * dy[0] + w1 * dy[1] + w2 * dy[2] + w3 * dy[3])
                    if seg_vis[lo] != 0:
                        cy = wp.cos(yaw)
                        sy = wp.sin(yaw)
                        ro = o - pos
                        ol = wp.vec3(wp.float32(cy * ro[0] + sy * ro[1]), wp.float32(-sy * ro[0] + cy * ro[1]), wp.float32(ro[2]))
                        ld = wp.vec3(wp.float32(cy * d[0] + sy * d[1]), wp.float32(-sy * d[0] + cy * d[1]), wp.float32(d[2]))
                        qe = wp.mesh_query_ray(act_mesh[a], ol, ld, A_MAX_T)
                        if qe.result:
                            ta = f64(qe.t)
                            if hit == 0 or ta < best:
                                best = ta
                                hit = 1
                                nl = qe.normal
                                nrm = wp.vec3d(cy * f64(nl[0]) - sy * f64(nl[1]), sy * f64(nl[0]) + cy * f64(nl[1]), f64(nl[2]))
                                inst = int(act_idx[a]) + 1
                                sem = int(act_sem[a])
                                prim = int(-2)  # noqa: UP018, RUF046
        # ---- 回波（`MeshLidar.scan` 同式）
        r = ZERO
        if hit == 1:
            r = best
        nlen = wp.sqrt(nrm[0] * nrm[0] + nrm[1] * nrm[1] + nrm[2] * nrm[2])
        cosi = ONE
        if nlen > ZERO:
            cosi = wp.abs(nrm[0] * d[0] + nrm[1] * d[1] + nrm[2] * d[2]) / nlen
        rho = rho_tab[wp.clamp(sem, 0, 255)]
        rho_eff = rho * wp.max(cosi, f64(0.05)) * wp.exp(f64(-2.0) * fr_beta[f] * r)
        rmax = wp.min(prm.hard_max, prm.r10 * wp.pow(wp.max(rho_eff, f64(1e-6)) / f64(0.1), prm.expo))
        p_det = ONE / (ONE + wp.exp((r - rmax) / (f64(0.05) * rmax)))
        ret = hit == 1 and r >= prm.blind and u01(slot_key(kf, i, U_CH_DET)) < p_det
        sig = prm.sig_far
        if r < f64(20.0):
            sig = prm.sig_near
        sig = sig * sc
        r_meas = r
        if sc > ZERO:
            r_meas = r + sig * normal2(slot_key(kf, i, U_CH_RANGE))[0]
        refl = int(wp.clamp(wp.floor(f64(150.0) * rho_eff + HALF), ZERO, f64(150.0)))
        spur = False
        if sc > ZERO and prm.fa > ZERO:
            spur = u01(slot_key(kf, i, U_CH_FA)) < prm.fa and not ret
            if spur:
                r_meas = prm.blind + u01(slot_key(kf, i, U_CH_FA_R)) * (prm.r10 - prm.blind)
                refl = 8
        out = ret or spur
        void = hit == 0 and d[2] < ZERO
        gi = int(I_NONE)
        if spur:
            gi = int(I_SPUR)
        elif ret:
            gi = inst
        fl = int(0)  # noqa: UP018, RUF046
        if hit == 1:
            fl = sem & 0x3F
        if void:
            fl = fl | 0x40
        if spur:
            fl = fl | 0x80
        o_inst[tid] = wp.uint16(gi)
        o_flags[tid] = wp.uint8(fl)
        rc = Rec()
        rc.x = wp.float32(d_nom[0] * r_meas)
        rc.y = wp.float32(d_nom[1] * r_meas)
        rc.z = wp.float32(d_nom[2] * r_meas)
        rc.offset_time = wp.uint32(t_rel)
        rc.reflectivity = wp.uint8(refl)
        rc.tag = wp.uint8(0)
        if spur:
            rc.tag = wp.uint8(1)
        rc.line = wp.uint8(i & 3)
        rc.pad = wp.uint8(0)
        o_rec[tid] = rc
        o_valid[tid] = 0
        if out:
            o_valid[tid] = 1
        if inst > 0:
            wp.atomic_add(o_aray, f, 1)
            if ret:
                wp.atomic_add(o_ahit, f, 1)
        if prm.rays != 0:
            o_t[tid] = r
            if hit == 0:
                o_t[tid] = f64(-1.0)
            o_prim[tid] = prim
            o_ainst[tid] = inst

    @wp.kernel
    def k_compact(o_rec: wp.array(dtype=Rec), o_valid: wp.array(dtype=wp.int32), offs: wp.array(dtype=wp.int32),
                  out: wp.array(dtype=Rec)):
        tid = wp.tid()
        if o_valid[tid] != 0:
            out[offs[tid]] = o_rec[tid]

    @wp.kernel
    def k_frame_start(n_slots: int, n_rays: int, o_valid: wp.array(dtype=wp.int32), offs: wp.array(dtype=wp.int32),
                      fstart: wp.array(dtype=wp.int32)):
        f = wp.tid()
        if f * n_slots < n_rays:
            fstart[f] = offs[f * n_slots]
        else:
            fstart[f] = offs[n_rays - 1] + o_valid[n_rays - 1]

    @wp.kernel
    def k_rng(keys: wp.array(dtype=wp.uint64), o_h: wp.array(dtype=wp.uint64), o_u: wp.array(dtype=wp.float64),
              o_n: wp.array(dtype=wp.vec2d)):
        tid = wp.tid()
        h = sm(keys[tid])
        o_h[tid] = h
        o_u[tid] = u01(h)
        o_n[tid] = normal2(h)

    return wp, Rec, Params, (k_rays, k_static, k_scan), k_compact, k_frame_start, k_rng


def rng_warp(keys: np.ndarray, device: str | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """内核所用随机数的 Warp 实现：键 k → (h = sm(k)、U01(h)、Box–Muller 两分量 (n, 2))，供与 `cbrng` 及 `mesh._u01`、`mesh._normal2`
    的逐位对拍（M25-AC-012：CPU 与 Warp 的 h64 对同一键给出同一 64 位整数）。"""
    wp, *_k, k_rng = _kernels()
    dev = device or ("cuda:0" if wp.get_cuda_device_count() > 0 else "cpu")
    k = wp.array(np.ascontiguousarray(keys, np.uint64), dtype=wp.uint64, device=dev)
    h = wp.empty(len(keys), dtype=wp.uint64, device=dev)
    u = wp.empty(len(keys), dtype=wp.float64, device=dev)
    n = wp.empty(len(keys), dtype=wp.vec2d, device=dev)
    wp.launch(k_rng, dim=len(keys), inputs=[k, h, u, n], device=dev)
    return h.numpy(), u.numpy(), n.numpy()


# ---------------------------------------------------------------- 主类


class WarpMeshLidar:
    """`q` 为 MeshWorldQuery（瓦片、面序与语义与 CPU 参考相同）；`actors` 为磁带演员表（按 idx）。接口与 `MeshLidar` 相同，另有 `scan_batch`。"""

    backend = BACKEND

    def __init__(self, q, model: LidarModel, actors: list[dict] | None = None, *, device: str | None = None, bvh: str | None = None) -> None:
        wp, self._Rec, self._Params, self._k_stages, self._k_compact, self._k_fstart, _ = _kernels()
        self.wp = wp
        self.q = q
        self.m = model
        self.actors = actors or []
        if model.slots_per_frame != pat.NPF or model.point_dt_ns != pat.POINT_DT_NS:
            raise ValueError("WarpMeshLidar supports the MID-360 pattern (20000 slots, 5 us) only")
        self.device = device or ("cuda:0" if wp.get_cuda_device_count() > 0 else "cpu")
        dev = self.device
        is_cpu = wp.get_device(dev).is_cpu
        self.bvh = "sah" if is_cpu else (bvh or os.environ.get("AWR_WARP_BVH", "") or _WP_BVH)
        self.last_track: _ActorTrack | None = None
        self.last_tracks: dict[int, _ActorTrack] = {}
        self.last_timing: dict[str, float] = {}
        self.last_rays: dict[str, np.ndarray] | None = None
        # CUDA 计时事件（§4.7 GPU 时间份额核算，ADR-213 决策 (3)）：第 1、2 段内核、第 3 段内核与压缩、D2H 回传三个区间各一对；
        # 两段之间主机重建演员轨迹时 GPU 可能空闲，该空隙不计入
        self._tev = None if is_cpu else [wp.Event(dev, enable_timing=True) for _ in range(6)]
        t0 = time.perf_counter()
        self._tiles = []                                         # 保持 points、indices 数组的引用（Mesh 只记录其设备指针）
        ids, org, lo, hi, t0s = [], [], [], [], []
        for tile in q.tiles:
            pts = wp.array(np.ascontiguousarray(tile.V, np.float32), dtype=wp.vec3, device=dev)
            idx = wp.array(np.ascontiguousarray(tile.F, np.int64).astype(np.int32).ravel(), dtype=wp.int32, device=dev)
            mesh = wp.Mesh(points=pts, indices=idx, bvh_constructor=self.bvh)
            self._tiles.append((pts, idx, mesh))
            ids.append(mesh.id)
            org.append(np.asarray(tile.origin, np.float64))
            lo.append(np.asarray(tile.lo, np.float64) - TILE_SLAB_PAD_M)
            hi.append(np.asarray(tile.hi, np.float64) + TILE_SLAB_PAD_M)
            t0s.append(int(tile.t0))
        n = max(len(ids), 1)
        self._tile_mesh = wp.array(np.array(ids or [0], np.uint64), dtype=wp.uint64, device=dev)
        self._tile_org = wp.array(np.array(org or [[0.0] * 3], np.float64).reshape(n, 3), dtype=wp.vec3d, device=dev)
        self._tile_lo = wp.array(np.array(lo or [[0.0] * 3], np.float64).reshape(n, 3).astype(np.float32), dtype=wp.vec3, device=dev)
        self._tile_hi = wp.array(np.array(hi or [[0.0] * 3], np.float64).reshape(n, 3).astype(np.float32), dtype=wp.vec3, device=dev)
        self._tile_t0 = wp.array(np.array(t0s or [0], np.int32), dtype=wp.int32, device=dev)
        self._n_tiles = len(ids)
        sem = getattr(q, "semantic", None)
        self._n_sem = 0 if sem is None else len(sem)
        self._sem = wp.array(np.asarray(sem if sem is not None and len(sem) else [0], np.uint8), dtype=wp.uint8, device=dev)
        self._rho = wp.array(rho_sem().astype(np.float64), dtype=wp.float64, device=dev)
        self._tabs = [wp.array(np.ascontiguousarray(a, np.float64), dtype=wp.float64, device=dev)
                      for a in (pat._SEL, pat._CD, pat._SD, pat.EC, pat._SAZ_SEC, pat._SD0, pat._CD0, pat._AZL)]
        self._amesh: dict[int, tuple[object, object, object, float]] = {}
        self._cap = 0
        self._ring: list = [None] * HOST_RING
        self._ring_i = -1
        self.build_ms = (time.perf_counter() - t0) * 1e3

    # ------------------------------------------------------------------ 设备资源
    def versions(self) -> dict[str, str]:
        """运行清单版本集合中本后端的条目（M25-FR-174）：warp、设备名、CUDA 驱动接口版本、BVH 构建器。"""
        wp = self.wp
        d = wp.get_device(self.device)
        drv = ""
        if d.is_cuda:
            try:
                v = wp.get_cuda_driver_version()
                drv = ".".join(str(x) for x in v) if isinstance(v, tuple) else str(v or "")
            except Exception:                                    # pragma: no cover - 旧版 warp 无此接口
                drv = ""
        return {"backend": BACKEND, "warp": str(wp.__version__), "device": str(d.name), "cuda_driver": drv, "bvh": self.bvh}

    def _actor_mesh(self, i: int) -> tuple[int, float]:
        """演员 i 的局部代理网格（与相机同一几何）的 Warp Mesh id 与外接半径；按 idx 缓存，跨帧复用。"""
        if i not in self._amesh:
            from awr.sensorsvc.camera.actors import proxy_mesh

            wp = self.wp
            P, _N, _C = proxy_mesh(self.actors[i])
            pts = wp.array(np.ascontiguousarray(P, np.float32), dtype=wp.vec3, device=self.device)
            idx = wp.array(np.arange(len(P), dtype=np.int32), dtype=wp.int32, device=self.device)
            mesh = wp.Mesh(points=pts, indices=idx, bvh_constructor="sah" if wp.get_device(self.device).is_cpu else "lbvh")
            self._amesh[i] = (pts, idx, mesh, float(np.linalg.norm(P.astype(np.float64), axis=1).max()))
        return self._amesh[i][2].id, self._amesh[i][3]

    def _ensure(self, n_rays: int, rays: bool) -> None:
        wp, dev = self.wp, self.device
        if n_rays > self._cap:
            self._rec = wp.empty(n_rays, dtype=self._Rec, device=dev)
            self._inst = wp.empty(n_rays, dtype=wp.uint16, device=dev)
            self._flags = wp.empty(n_rays, dtype=wp.uint8, device=dev)
            self._valid = wp.empty(n_rays, dtype=wp.int32, device=dev)
            self._offs = wp.empty(n_rays, dtype=wp.int32, device=dev)
            self._out = wp.empty(n_rays, dtype=self._Rec, device=dev)
            self._ro = wp.empty(n_rays, dtype=wp.vec3d, device=dev)
            self._rd = wp.empty(n_rays, dtype=wp.vec3d, device=dev)
            self._rdn = wp.empty(n_rays, dtype=wp.vec3d, device=dev)
            self._st = wp.empty(n_rays, dtype=wp.float64, device=dev)
            self._sprim = wp.empty(n_rays, dtype=wp.int32, device=dev)
            self._snrm = wp.empty(n_rays, dtype=wp.vec3, device=dev)
            self._cap = n_rays
            self._ray_cap = 0
        if rays and getattr(self, "_ray_cap", 0) < n_rays:
            self._rt = wp.empty(n_rays, dtype=wp.float64, device=dev)
            self._rprim = wp.empty(n_rays, dtype=wp.int32, device=dev)
            self._rinst = wp.empty(n_rays, dtype=wp.int32, device=dev)
            self._ray_cap = n_rays
        if not hasattr(self, "_rt"):
            self._rt = wp.empty(1, dtype=wp.float64, device=dev)
            self._rprim = wp.empty(1, dtype=wp.int32, device=dev)
            self._rinst = wp.empty(1, dtype=wp.int32, device=dev)

    def _host_slot(self, n_rays: int):
        """主机端锁页回传缓冲区环（`HOST_RING` 个槽位，逐次调用轮换）：(有效点记录, inst, flags)，容量不足时重新分配该槽位。"""
        wp = self.wp
        k = self._ring_i = (self._ring_i + 1) % HOST_RING
        slot = self._ring[k]
        if slot is None or slot[3] < n_rays:
            pinned = wp.get_device(self.device).is_cuda
            slot = self._ring[k] = (wp.empty(n_rays, dtype=self._Rec, device="cpu", pinned=pinned),
                                    wp.empty(n_rays, dtype=wp.uint16, device="cpu", pinned=pinned),
                                    wp.empty(n_rays, dtype=wp.uint8, device="cpu", pinned=pinned), n_rays)
        return slot

    # ------------------------------------------------------------------ 扫描
    def scan(self, frame_seq: int, t_start_ns: int, nodes: list[tuple[np.ndarray, np.ndarray]], *, actor_states=None, seed: int = 0,
             uav_no: int = 0, sensor_no: int = 0, tier: str = "realistic", beta_905: float = 0.0) -> LidarScan:
        """单帧（批大小 1）：参数与返回值同 `MeshLidar.scan`。"""
        return self.scan_batch([LidarFrameReq(frame_seq, t_start_ns, nodes, seed, uav_no, sensor_no, tier, beta_905)],
                               actor_states=actor_states)[0]

    def scan_batch(self, reqs: Sequence[LidarFrameReq], *, actor_states=None, rays: bool = False, copy: bool = True) -> list[LidarScan]:
        """一批帧一次 launch：返回逐帧 `LidarScan`（与输入同序）。`actor_states(t_ns)` 为全批共用的磁带时刻函数；`rays=True` 时另把逐射线
        (t：命中距离、未命中 −1；prim：全局三角形号、演员 −2、未命中 −1；inst：最近交点的演员 idx + 1) 存入 `last_rays`（对拍用）。
        `copy=False` 时返回的 points、gt_inst、gt_flags 为锁页回传缓冲区的视图（不再复制一次），在其后第 `HOST_RING` 次调用时被覆盖，
        供逐 tick 立即编码写出的流水线使用（每批数百万点时省去新内存的缺页开销）。"""
        wp, dev, m = self.wp, self.device, self.m
        B = len(reqs)
        if B == 0:
            return []
        T0 = time.perf_counter()
        N = m.slots_per_frame
        K = len(reqs[0].nodes)
        if K < 2 or any(len(r.nodes) != K for r in reqs):
            raise ValueError("all frames in a batch need the same number (>= 2) of pose nodes")
        seg_ns = m.frame_ns // (K - 1)
        # ---- 逐帧标量（主机，与 CPU 参考同式）
        R = np.array([n[0] for r in reqs for n in r.nodes], np.float64).reshape(B * K, 3, 3)
        P = np.array([n[1] for r in reqs for n in r.nodes], np.float64).reshape(B, K, 3)
        from awr.sensorsvc.poses import R_to_quat_xyzw

        Q = np.asarray(R_to_quat_xyzw(R), np.float64).reshape(B, K, 4)
        for k in range(1, K):                                    # 同半球（逐帧顺序翻转，与 CPU 相同）
            flip = np.einsum("bj,bj->b", Q[:, k], Q[:, k - 1]) < 0
            Q[flip, k] = -Q[flip, k]
        sc = np.array([TIER_SCALE[r.tier] for r in reqs], np.float64)
        beta = np.array([float(r.beta_905) for r in reqs], np.float64)
        u64 = lambda xs: np.array([int(x) & cbrng.MASK64 for x in xs], np.uint64)     # noqa: E731
        kf = cbrng.key(u64(r.seed for r in reqs), STREAM_M25, u64(r.uav_no for r in reqs), u64(r.frame_seq for r in reqs),
                       u64(CH_BASE + r.sensor_no for r in reqs))
        fs = np.array([_frame_scalars(r.frame_seq) for r in reqs], np.float64).reshape(B, 3)
        t0s = np.array([int(r.t_start_ns) for r in reqs], np.int64)
        n_rays = B * N
        self._ensure(n_rays, rays)
        A = lambda x, dt, wdt, shp=None: wp.array(np.ascontiguousarray(np.asarray(x, dt).reshape(shp) if shp else np.asarray(x, dt)),  # noqa: E731
                                                   dtype=wdt, device=dev)
        prm = self._Params()
        prm.n_slots, prm.dt_ns, prm.K, prm.seg_ns = N, m.point_dt_ns, K, seg_ns
        prm.hard_max, prm.blind, prm.r10, prm.expo = m.hard_max_m, m.blind_m, m.r_at_rho10_m, m.exponent
        prm.sig_near, prm.sig_far, prm.ang_rad, prm.fa = m.range_sigma_near_m, m.range_sigma_far_m, math.radians(m.angle_sigma_deg), m.false_alarm
        prm.n_tiles, prm.n_sem, prm.rays = self._n_tiles, self._n_sem, int(bool(rays))
        # ---- 第 1、2 段内核（射线、静态求交）先行异步执行，主机同时重建演员轨迹
        T1 = time.perf_counter()
        ev = self._tev
        stream = wp.get_stream(dev) if ev is not None else None
        if ev is not None:
            stream.record_event(ev[0])
        fr_sc, fr_key = A(sc, np.float64, wp.float64), A(kf, np.uint64, wp.uint64)
        k_rays, k_static, k_main = self._k_stages
        wp.launch(k_rays, dim=n_rays, device=dev, block_dim=BLOCK_DIM,
                  inputs=[prm, *self._tabs, A(fs[:, 0], np.float64, wp.float64), A(fs[:, 1], np.float64, wp.float64),
                          A(fs[:, 2], np.float64, wp.float64), fr_sc, fr_key, A(P, np.float64, wp.vec3d, (B * K, 3)),
                          A(Q, np.float64, wp.vec4d, (B * K, 4)), self._ro, self._rd, self._rdn])
        wp.launch(k_static, dim=n_rays, device=dev, block_dim=BLOCK_DIM,
                  inputs=[prm, self._ro, self._rd, self._tile_mesh, self._tile_org, self._tile_lo, self._tile_hi, self._tile_t0,
                          self._st, self._sprim, self._snrm])
        if ev is not None:
            stream.record_event(ev[1])
        T2 = time.perf_counter()
        at = self._actor_tables(actor_states, t0s, B, N)
        T3 = time.perf_counter()
        if ev is not None:
            stream.record_event(ev[2])
        ahit = wp.zeros(B, dtype=wp.int32, device=dev)
        aray = wp.zeros(B, dtype=wp.int32, device=dev)
        segs = at["segs"]
        cat = (lambda f, shp, dt: np.concatenate([f(s) for s in segs]).astype(dt) if segs else np.zeros(shp, dt))   # noqa: E731
        n_act = max(len(at["idx"]), 1)
        wp.launch(k_main, dim=n_rays, device=dev, block_dim=BLOCK_DIM,
                  inputs=[prm, self._ro, self._rd, self._rdn, self._st, self._sprim, self._snrm,
                          fr_sc, A(beta, np.float64, wp.float64), fr_key, A(at["grp"], np.int32, wp.int32), A(t0s, np.int64, wp.int64),
                          self._sem, self._rho, A(at["g_a0"], np.int32, wp.int32), A(at["idx"] or [0], np.int32, wp.int32),
                          A(at["mesh"] or [0], np.uint64, wp.uint64), A(at["c"] or [[0.0] * 3], np.float64, wp.vec3d, (n_act, 3)),
                          A(at["r2"] or [0.0], np.float32, wp.float32), A(at["sem"] or [0], np.int32, wp.int32),
                          A(at["s0"] or [0], np.int32, wp.int32), A(at["s1"] or [0], np.int32, wp.int32),
                          A(cat(lambda s: s.lo, (1,), np.int64), np.int64, wp.int64), A(cat(lambda s: s.n, (1,), np.int64), np.int64, wp.int64),
                          A(cat(lambda s: s.tau, (1, 4), np.float64), np.float64, wp.vec4d, (-1, 4)),
                          wp.array(np.ascontiguousarray(cat(lambda s: s.dP, (1, 4, 3), np.float64)), dtype=wp.vec3d, device=dev),
                          A(cat(lambda s: s.dY, (1, 4), np.float64), np.float64, wp.vec4d, (-1, 4)),
                          A(cat(lambda s: s.p0, (1, 3), np.float64), np.float64, wp.vec3d, (-1, 3)),
                          A(cat(lambda s: s.y0, (1,), np.float64), np.float64, wp.float64),
                          A(np.isin(cat(lambda s: s.state, (1,), np.uint8), _VIS).astype(np.int32), np.int32, wp.int32),
                          self._rec, self._inst, self._flags, self._valid, ahit, aray, self._rt, self._rprim, self._rinst])
        # ---- 有效点压缩（前缀和）与回传
        wp.utils.array_scan(self._valid[:n_rays], self._offs[:n_rays], inclusive=False)
        fstart = wp.empty(B + 1, dtype=wp.int32, device=dev)
        wp.launch(self._k_fstart, dim=B + 1, inputs=[N, n_rays, self._valid, self._offs, fstart], device=dev)
        wp.launch(self._k_compact, dim=n_rays, inputs=[self._rec, self._valid, self._offs, self._out], device=dev)
        if ev is not None:
            stream.record_event(ev[3])
        fst = fstart.numpy()                                     # 同步点
        T4 = time.perf_counter()
        total = int(fst[-1])
        h_out, h_inst, h_flags, _cap = self._host_slot(n_rays)
        if ev is not None:
            stream.record_event(ev[4])
        wp.copy(h_out, self._out, count=total)
        wp.copy(h_inst, self._inst, count=n_rays)
        wp.copy(h_flags, self._flags, count=n_rays)
        if ev is not None:
            stream.record_event(ev[5])
        hits, rays_n = ahit.numpy(), aray.numpy()
        wp.synchronize_device(dev)
        T5 = time.perf_counter()
        gpu = {}
        if ev is not None:
            el = wp.get_event_elapsed_time
            gpu = {"gpu_ms": el(ev[0], ev[1], synchronize=False) + el(ev[2], ev[3], synchronize=False),
                   "d2h_gpu_ms": el(ev[4], ev[5], synchronize=False)}
        pts = h_out.numpy()[:total].view(LIDAR_RECORD)
        gi = h_inst.numpy()[:n_rays]
        gf = h_flags.numpy()[:n_rays]
        if copy:
            pts, gi, gf = pts.copy(), gi.copy(), gf.copy()
        if rays:
            self.last_rays = {"t": self._rt.numpy()[:n_rays].copy(), "prim": self._rprim.numpy()[:n_rays].copy(),
                              "inst": self._rinst.numpy()[:n_rays].copy()}
        out = [LidarScan(pts[fst[f]:fst[f + 1]], gi[f * N:(f + 1) * N], gf[f * N:(f + 1) * N], int(r.t_start_ns), int(r.frame_seq),
                         int(hits[f]), int(rays_n[f])) for f, r in enumerate(reqs)]
        T6 = time.perf_counter()
        self.last_timing = {"frames": float(B), "points": float(total), "actors": float(len(at["idx"])), "segments": float(at["n_seg"]),
                            "prep_ms": (T1 - T0) * 1e3, "launch_ms": (T2 - T1) * 1e3, "track_ms": (T3 - T2) * 1e3,
                            "gpu_wait_ms": (T4 - T3) * 1e3, "d2h_ms": (T5 - T4) * 1e3, "unpack_ms": (T6 - T5) * 1e3, "total_ms": (T6 - T0) * 1e3,
                            **gpu}
        return out

    def _actor_tables(self, actor_states, t0s: np.ndarray, B: int, N: int) -> dict:
        """按帧起点分组重建演员轨迹（每组一个 `_ActorTrack`），选出候选演员（帧内任一样本可见或轨迹不平稳，与 CPU 相同），给出扫掠包围球
        与分段表；与第 1、2 段内核并行执行。"""
        m = self.m
        at: dict = {"grp": np.full(B, -1, np.int32), "g_a0": [0], "idx": [], "mesh": [], "c": [], "r2": [], "sem": [], "s0": [], "s1": [],
                    "segs": [], "n_seg": 0}
        self.last_tracks = {}
        if actor_states is None or not self.actors:
            return at
        from awr.sensorsvc.camera.actors import SEM_CODE

        t_rel = np.arange(N, dtype=np.int64) * m.point_dt_ns
        for gi, ts in enumerate(sorted(set(t0s.tolist()))):
            tr = _ActorTrack(actor_states, len(self.actors), ts, ts + int(t_rel[-1]))
            self.last_tracks[ts] = tr
            P0, _Y0, S0 = tr.frame_samples()
            vis0 = np.isin(S0, _VIS).any(0) | ~tr.frame_steady()
            t_slots = ts + t_rel
            for a in np.nonzero(vis0)[0]:
                mid, rad = self._actor_mesh(int(a))
                sv = np.isin(S0[:, a], _VIS)
                Pa = P0[sv, a] if sv.any() else P0[:, a]
                c = Pa.mean(0)
                Rs = float(np.linalg.norm(Pa - c, axis=1).max()) + rad + SWEEP_MARGIN_M
                sg = actor_segments(tr, int(a), t_slots)
                at["idx"].append(int(a))
                at["mesh"].append(mid)
                at["c"].append(c)
                at["r2"].append((Rs + SPHERE_PAD_M) ** 2)
                at["sem"].append(SEM_CODE.get(self.actors[int(a)]["class"], 13))
                at["s0"].append(at["n_seg"])
                at["n_seg"] += len(sg.lo)
                at["s1"].append(at["n_seg"])
                at["segs"].append(sg)
            at["g_a0"].append(len(at["idx"]))
            at["grp"][t0s == ts] = gi
        if self.last_tracks:
            self.last_track = self.last_tracks[int(t0s[-1])]
        return at


def _frame_scalars(frame_idx: int) -> tuple[float, float, float]:
    """`pattern.mid360_pattern` 的帧首约化标量 (cos θ0, sin θ0, az0)（同式，Python float64）。"""
    n0 = int(frame_idx) * pat.CARRIER
    th0 = 2.0 * math.pi * ((n0 % pat.ROT) / pat.ROT)
    return math.cos(th0), math.sin(th0), (pat.AZ0 + (pat.AZSTEP * n0) % 360.0) % 360.0
