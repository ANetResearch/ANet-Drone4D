"""网格 LiDAR 单帧求交（M25 §6.8.3–§6.8.7）：CPU 实现（Open3D，经 M22 `MeshWorldQuery`），与 GPU（Warp）路径同一算法与随机键。

- 时刻：槽位 i 的时刻 t_s + i·5 µs；K = 11 个位姿节点（t_s + 10 ms·m），槽位位姿为相邻节点位置线性、姿态四元数 nlerp（同半球）。
- 方向：MID-360 花样（`awr.sim.sensors.lidar.pattern`，雷达系 FLU）；角度噪声只扰动求交方向，输出点沿名义方向 × 测得距离，
  即点时刻传感器系（真实运动畸变保留在点云中）。
- 静态：MeshWorldQuery 精确求交（命中三角形的语义码与法线）；演员（ADR-198，D3-AC-10）：每条候选射线按自身槽位时刻
  t_s + i·5 µs 取演员状态（与相机同一磁带时刻函数 `actor_states(t_ns)`），射线变换到演员局部系与代理网格（与相机同一几何）
  求交，取较近者（实例 idx + 1）。逐点状态由 `_ActorTrack` 给出：磁带时刻函数在磁带帧间为三次多项式（位置 Hermite、航向线性），
  区间内 4 个整数时刻样本的 Lagrange 三次插值与之逐点相等，另取 2 个样本（1/6 点与中点）校验；校验不过（区间跨磁带帧边界、可见性或
  容器阶跃、非多项式的时刻函数）则二分，区间短于 5 ms 时对其中的候选射线逐时刻直接调用时刻函数。候选演员以帧内样本位置的
  扫掠包围球筛选，只影响计算量。
- 回波（`sem_table_v1` 的 P0a 子集）：rho = rho_sem[cls]（不做纹理亮度调制与阴影补偿，待反射率纹理就绪），rho_eff = rho·max(|n·d|, 0.05)·
  exp(−2β·r)，r_max = min(r_hard, 40·(rho_eff/0.1)^0.269)，逻辑斯蒂检测，测距噪声 sigma(r)；虚警 1e-4（realistic、harsh）。
- `void`（bit6，M25 §6.8.6；D3-AC-04）：给出 `quality`（`awr.sensorsvc.quality.SensorQuality`）时，最近交点为拉伸面、洞缘或覆盖外的
  静态三角形，或射线在任意距离上都与静态网格无交点、仰角 < 0 且起点在覆盖多边形内（穿洞、越过覆盖边缘）；观测帧不受影响（按回波
  模型输出）。未给出时（D2 世界、quality 阶段之前的构建）沿用"量程内未命中且仰角 < 0"。
- 随机数：帧键 K_f = key(run_seed, 22, uav_no, frame_seq, 0x2000 + sensor_no)，槽位键 sm(K_f ^ (i·8 + c))（c：0 检测、1 测距、
  2 角度、3 虚警判定、4 虚警距离）。clean 档无测距、角度噪声与虚警，检测概率仍按量程模型。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

import numpy as np

from awr.bench.recorder.lidar import LIDAR_RECORD
from awr.sensorsvc.lidar.model import LidarModel
from awr.sim.events.tape import STATE_VISIBLE
from awr.sim.sensors import cbrng
from awr.sim.sensors.lidar.pattern import directions_flu, mid360_pattern

if TYPE_CHECKING:
    from awr.sensorsvc.quality import SensorQuality

__all__ = ["INST_NONE", "INST_SPURIOUS", "LIDAR_RECORD", "TIER_SCALE", "LidarScan", "MeshLidar", "lidar_void", "rho_sem"]

INST_SPURIOUS, INST_NONE = 65534, 65535
CH_DET, CH_RANGE, CH_ANG, CH_FA, CH_FA_R = 0, 1, 2, 3, 4
CH_BASE = 0x2000
STREAM_M25 = 22
TIER_SCALE = {"clean": 0.0, "realistic": 1.0, "harsh": 1.5}
_C3 = np.uint64(0x94D049BB133111EB)


@cache
def rho_sem() -> np.ndarray:
    """d3-semantic@1 的 905 nm 缺省反射率（256 项，未登记的码取 0.35）。"""
    from awr.contracts._paths import schema_path

    d = json.loads(schema_path("classes/d3-semantic-v1.json").read_text(encoding="utf-8"))
    t = np.full(256, 0.35)
    for c in d["classes"]:
        if "reflect_905" in c:
            t[int(c["code"])] = float(c["reflect_905"])
    return t


@dataclass
class LidarScan:
    points: np.ndarray          # (n,) LIDAR_RECORD，有效点（槽位升序）
    gt_inst: np.ndarray         # (N,) u16 全部槽位
    gt_flags: np.ndarray        # (N,) u8：bit0–5 语义码、bit6 void、bit7 spurious
    t_start_ns: int
    frame_seq: int
    actor_hits: int             # 回波为演员的槽位数（与 gt_inst 一致）
    actor_rays: int = 0         # 最近交点为演员的射线数（含未检出回波）


def _quat(R: np.ndarray) -> np.ndarray:
    from awr.sensorsvc.poses import R_to_quat_xyzw

    return np.asarray(R_to_quat_xyzw(np.asarray(R, np.float64)), np.float64)


def _quat_R(q: np.ndarray) -> np.ndarray:
    """(n, 4) xyzw → (n, 3, 3)。"""
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    return np.stack([np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
                     np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
                     np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1)], 1)


def _u01(h: np.ndarray) -> np.ndarray:
    return (h >> np.uint64(11)).astype(np.float64) * cbrng.INV53


def _normal2(h: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    b = cbrng.sm(h ^ _C3)
    u1 = ((h >> np.uint64(11)).astype(np.float64) + 0.5) * cbrng.INV53
    u2 = _u01(b)
    r = np.sqrt(-2.0 * np.log(u1))
    return r * np.cos(2 * np.pi * u2), r * np.sin(2 * np.pi * u2)


class MeshLidar:
    """`q` 为 MeshWorldQuery；`actors` 为磁带演员表（按 idx），与相机代理同一几何；`quality` 为世界的传感器侧质量信息（`void` 判定）。"""

    def __init__(self, q, model: LidarModel, actors: list[dict] | None = None, quality: SensorQuality | None = None) -> None:
        self.q = q
        self.m = model
        self.actors = actors or []
        self.quality = quality
        self._mesh: dict[int, tuple[object, float]] = {}
        self._az = np.empty(model.slots_per_frame)
        self._el = np.empty(model.slots_per_frame)
        self.last_track: _ActorTrack | None = None                 # 最近一帧的逐点演员状态求值器（查询计数与缓存键供测试与性能记录）

    def _actor_mesh(self, i: int) -> tuple[object, float]:
        """演员 i 的局部代理网格（与相机同一几何）建成的 Open3D 场景与外接半径；按 idx 缓存，跨帧复用。"""
        if i not in self._mesh:
            import open3d as o3d

            from awr.sensorsvc.camera.actors import proxy_mesh

            P, _N, _C = proxy_mesh(self.actors[i])
            V = np.ascontiguousarray(P, np.float32)
            scene = o3d.t.geometry.RaycastingScene()
            scene.add_triangles(o3d.core.Tensor(V), o3d.core.Tensor(np.arange(len(V), dtype=np.uint32).reshape(-1, 3)))
            self._mesh[i] = (scene, float(np.linalg.norm(P.astype(np.float64), axis=1).max()))
        return self._mesh[i]

    def scan(self, frame_seq: int, t_start_ns: int, nodes: list[tuple[np.ndarray, np.ndarray]], *, actor_states=None,
             seed: int = 0, uav_no: int = 0, sensor_no: int = 0, tier: str = "realistic", beta_905: float = 0.0) -> LidarScan:
        """`nodes`：K 个 (R_ws WORLD←FLU, p_w) 位姿节点，时刻 t_start + 10 ms·m；`actor_states(t_ns)` 返回 ActorStates 或 None。"""
        m = self.m
        N, K = m.slots_per_frame, len(nodes)
        i = np.arange(N)
        t_rel = i * m.point_dt_ns
        seg_ns = m.frame_ns // (K - 1)
        seg = np.minimum(t_rel // seg_ns, K - 2)
        u = (t_rel - seg * seg_ns) / seg_ns
        P = np.array([p for _, p in nodes], np.float64)
        Q = np.array([_quat(R) for R, _ in nodes])
        for k in range(1, K):                                       # 同半球
            if np.dot(Q[k], Q[k - 1]) < 0:
                Q[k] = -Q[k]
        o = P[seg] * (1 - u)[:, None] + P[seg + 1] * u[:, None]
        q = Q[seg] * (1 - u)[:, None] + Q[seg + 1] * u[:, None]
        q /= np.linalg.norm(q, axis=1, keepdims=True)
        Rs = _quat_R(q)
        mid360_pattern(frame_seq, self._az, self._el)
        d_nom = directions_flu(self._az, self._el)
        sc = TIER_SCALE[tier]
        K_f = cbrng.key(seed, STREAM_M25, uav_no, frame_seq, CH_BASE + sensor_no)
        h = lambda c: cbrng.sm(K_f ^ (i.astype(np.uint64) * np.uint64(8) + np.uint64(c)))      # noqa: E731
        if sc > 0:
            na, ne = _normal2(h(CH_ANG))
            s = math.radians(m.angle_sigma_deg) * sc
            d_true = directions_flu(self._az + np.degrees(na * s), self._el + np.degrees(ne * s))
        else:
            d_true = d_nom
        d = np.einsum("nij,nj->ni", Rs, d_true)
        # 静态
        t, prim, nrm = self.q._cast(o, d, np.full(N, m.hard_max_m))
        t = t.astype(np.float64)
        hit = np.isfinite(t)
        sem = np.zeros(N, np.int64)
        if hit.any():
            sem[hit] = np.asarray(self.q.semantic_of(prim[hit].astype(np.uint32)), np.int64)
        nrm = np.asarray(nrm, np.float64)
        inst = np.zeros(N, np.int64)
        n_act = 0
        if actor_states is not None and self.actors:
            n_act = self._actors(o, d, t, nrm, sem, inst, t_rel, int(t_start_ns), actor_states)
            hit = np.isfinite(t)
        # 回波
        r = np.where(hit, t, 0.0)
        nlen = np.linalg.norm(nrm, axis=1)
        cosi = np.where(nlen > 0, np.abs(np.einsum("ij,ij->i", nrm, d)) / np.where(nlen > 0, nlen, 1.0), 1.0)
        rho = rho_sem()[np.clip(sem, 0, 255)]
        rho_eff = rho * np.maximum(cosi, 0.05) * np.exp(-2.0 * beta_905 * r)
        rmax = m.r_max(rho_eff)
        with np.errstate(over="ignore"):
            p_det = 1.0 / (1.0 + np.exp((r - rmax) / (0.05 * rmax)))
        ret = hit & (r >= m.blind_m) & (_u01(h(CH_DET)) < p_det)
        sig = np.where(r < 20.0, m.range_sigma_near_m, m.range_sigma_far_m) * sc
        r_meas = r + sig * _normal2(h(CH_RANGE))[0] if sc > 0 else r
        refl = np.clip(np.floor(150.0 * rho_eff + 0.5), 0, 150).astype(np.uint8)
        spur = np.zeros(N, bool)
        if sc > 0 and m.false_alarm > 0:                           # 虚警：在 [blind, r_max(0.1)] 均匀取距离
            spur = (_u01(h(CH_FA)) < m.false_alarm) & ~ret
            r_meas = np.where(spur, m.blind_m + _u01(h(CH_FA_R)) * (m.r_at_rho10_m - m.blind_m), r_meas)
            refl = np.where(spur, np.uint8(8), refl)
        out = ret | spur
        el_w = d[:, 2]
        void = lidar_void(self.quality, prim, hit & (inst == 0), ~hit & (el_w < 0), o, d, q=self.q)
        gt_inst = np.where(spur, INST_SPURIOUS, np.where(ret, inst, INST_NONE)).astype(np.uint16)
        gt_flags = ((np.where(hit, sem, 0) & 0x3F) | (void.astype(np.int64) << 6) | (spur.astype(np.int64) << 7)).astype(np.uint8)
        pts = np.zeros(int(out.sum()), LIDAR_RECORD)
        xyz = d_nom[out] * r_meas[out][:, None]
        pts["x"], pts["y"], pts["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
        pts["offset_time"] = t_rel[out]
        pts["reflectivity"] = refl[out]
        pts["tag"] = np.where(spur[out], 1, 0)
        pts["line"] = i[out] & 3
        n_ret = int((ret & (inst > 0)).sum())
        return LidarScan(pts, gt_inst, gt_flags, int(t_start_ns), int(frame_seq), n_ret, n_act)

    def _actors(self, o, d, t, nrm, sem, inst, t_rel, t_start_ns: int, actor_states) -> int:
        """逐点时刻的动态实体求交（ADR-198）：候选射线以自身槽位时刻的演员位姿变换到演员局部系，与局部代理网格求交，
        取与已有命中（静态或其他演员）的较近者；返回最近交点为演员的射线数。"""
        import open3d as o3d

        from awr.sensorsvc.camera.actors import SEM_CODE

        tr = _ActorTrack(actor_states, len(self.actors), t_start_ns, t_start_ns + int(t_rel[-1]))
        self.last_track = tr
        P0, _Y0, S0 = tr.frame_samples()                            # (6, A, 3)、(6, A)：帧区间的 6 个样本
        vis0 = np.isin(S0, _VIS).any(0) | ~tr.frame_steady()
        L = np.where(np.isfinite(t), t, self.m.hard_max_m)
        oc = o.mean(0)
        reach = float(np.linalg.norm(o - oc, axis=1).max()) + self.m.hard_max_m
        for a in np.nonzero(vis0)[0]:
            scene, rad = self._actor_mesh(int(a))
            sv = np.isin(S0[:, a], _VIS)                            # 扫掠球只取可见样本（不可见时刻的位置不参与求交）
            Pa = P0[sv, a] if sv.any() else P0[:, a]
            c = Pa.mean(0)
            R = float(np.linalg.norm(Pa - c, axis=1).max()) + rad + SWEEP_MARGIN_M
            if np.linalg.norm(c - oc) > reach + R:
                continue
            w = c - o                                                # 射线段 [0, L] 到扫掠球心的最近距离
            s = np.clip(np.einsum("ij,ij->i", w, d), 0.0, L)
            sel = np.nonzero(np.einsum("ij,ij->i", w - s[:, None] * d, w - s[:, None] * d) <= R * R)[0]
            if not len(sel):
                continue
            pos, yaw, st = tr.eval(int(a), t_start_ns + t_rel[sel].astype(np.int64))
            v = np.isin(st, _VIS)
            sel, pos, yaw = sel[v], pos[v], yaw[v]
            if not len(sel):
                continue
            cy, sy = np.cos(yaw), np.sin(yaw)
            ro, rd = o[sel] - pos, d[sel]                            # 世界 → 演员局部：Rz(−yaw)
            ol = np.stack([cy * ro[:, 0] + sy * ro[:, 1], -sy * ro[:, 0] + cy * ro[:, 1], ro[:, 2]], 1)
            ld = np.stack([cy * rd[:, 0] + sy * rd[:, 1], -sy * rd[:, 0] + cy * rd[:, 1], rd[:, 2]], 1)
            res = scene.cast_rays(o3d.core.Tensor(np.hstack([ol, ld]).astype(np.float32)))
            ta = res["t_hit"].numpy().astype(np.float64)
            closer = np.isfinite(ta) & (ta < np.where(np.isfinite(t[sel]), t[sel], np.inf))
            if closer.any():
                k = sel[closer]
                nl = res["primitive_normals"].numpy().astype(np.float64)[closer]
                c2, s2 = cy[closer], sy[closer]                      # 法线回到世界系：Rz(yaw)
                t[k] = ta[closer]
                nrm[k] = np.stack([c2 * nl[:, 0] - s2 * nl[:, 1], s2 * nl[:, 0] + c2 * nl[:, 1], nl[:, 2]], 1)
                inst[k] = int(a) + 1
                sem[k] = SEM_CODE.get(self.actors[int(a)]["class"], 13)
        return int((inst > 0).sum())


def lidar_void(quality: SensorQuality | None, prim: np.ndarray, static_hit: np.ndarray, miss_down: np.ndarray, o: np.ndarray,
               d: np.ndarray, *, q=None) -> np.ndarray:
    """槽位 `void`（M25 §6.8.6）：`prim` 为静态最近交点三角形，`static_hit` 为量程内最近交点为静态网格的槽位，`miss_down` 为量程内
    无交点（静态与演员）且仰角 < 0 的槽位，`o`、`d` 为世界系射线起点与单位方向，`q` 为求交用的 MeshWorldQuery（缺省取 `quality`
    自带的）。无质量信息时即 `miss_down`（与 Warp 后端一致）。"""
    if quality is None:
        return np.asarray(miss_down, bool)
    return quality.lidar_void(prim, static_hit, miss_down, o, d, q=q)


_VIS = np.array(sorted(STATE_VISIBLE), np.uint8)
SWEEP_MARGIN_M = 0.5                    # 扫掠包围球余量（样本间轨迹偏离样本凸包的上界留量；只影响候选射线数）
TRACK_MIN_NS = 5_000_000                # 短于此长度的区间不再二分，候选射线逐时刻直接调用时刻函数
TRACK_DIRECT_MAX = 8                    # 校验未通过的区间内候选射线不多于此数时直接逐时刻调用（一次二分约需 8 次查询）
TRACK_TOL_M, TRACK_TOL_RAD = 1e-9, 1e-9  # 区间校验容差（1 nm、1 nrad：远高于舍入误差、远低于 D3-AC-10 的 1 mm）


def _wrap(x: np.ndarray) -> np.ndarray:
    return (x + np.pi) % (2.0 * np.pi) - np.pi


def _lagrange(tau_nodes: np.ndarray, tau: np.ndarray) -> np.ndarray:
    """4 点 Lagrange 基函数值：(n,) → (n, 4)。"""
    W = np.ones((len(tau), 4))
    for j in range(4):
        for m in range(4):
            if m != j:
                W[:, j] *= (tau - tau_nodes[m]) / (tau_nodes[j] - tau_nodes[m])
    return W


class _ActorTrack:
    """一帧内演员状态的逐时刻求值（ADR-198）：与相机同一磁带时刻函数 `fn(t_ns)`（返回 ActorStates 或 None）。

    磁带时刻函数（M23 §6.8）在相邻磁带帧之间位置为带速度的三次 Hermite、航向为线性，可见性与容器只在磁带帧边界阶跃，
    因此不跨帧边界的区间上，4 个整数时刻样本（区间两端与三等分点）的 Lagrange 三次插值与时刻函数逐点相等（舍入误差量级）。
    每个区间另取 1/6 点与中点两个样本校验（两点不对称，跨边界的折点不会同时落在误差零点附近）：可见性、容器在 6 个样本上
    一致且（可见时）两校验点的位置差 ≤ 1 nm、航向差 ≤ 1 nrad 才采用插值，否则在中点二分；短于 `TRACK_MIN_NS` 的区间、或其中候选时刻不多于 `TRACK_DIRECT_MAX` 个时逐时刻直接调用时刻函数。样本缓存键为整数 `t_ns`（D3-AC-10：不跨时刻共享实体状态）。
    """

    def __init__(self, fn, n_actors: int, t0_ns: int, t1_ns: int) -> None:
        self.fn = fn
        self.A = int(n_actors)
        self.t0, self.t1 = int(t0_ns), int(t1_ns)
        self.samples: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        self._fits: dict[tuple[int, int], tuple] = {}
        self.calls = 0                   # 时刻函数调用次数
        self.direct = 0                  # 逐时刻直接求值的时刻数（校验未通过的最小区间或候选很少的区间内）

    def sample(self, t_ns: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """整数时刻 t_ns 的 (pos (A, 3), yaw (A,), state (A,) u8, container (A,) u8)；未返回的演员 state = 0。"""
        assert type(t_ns) is int
        hit = self.samples.get(t_ns)
        if hit is not None:
            return hit
        self.calls += 1
        st = self.fn(t_ns)
        pos, yaw = np.zeros((self.A, 3)), np.zeros(self.A)
        state, cont = np.zeros(self.A, np.uint8), np.full(self.A, 255, np.uint8)
        if st is not None:
            ids = np.asarray(st.ids, np.int64)
            ok = (ids >= 0) & (ids < self.A)
            j = ids[ok]
            pos[j] = np.asarray(st.pos_enu_m, np.float64)[ok]
            yaw[j] = np.asarray(st.yaw_rad, np.float64)[ok]
            state[j] = np.asarray(st.state)[ok]
            cont[j] = np.asarray(st.container)[ok]
        self.samples[t_ns] = r = (pos, yaw, state, cont)
        return r

    def _fit(self, lo: int, hi: int) -> tuple:
        """区间 [lo, hi]：(节点 τ (4,), 位置增量 (4, A, 3), 航向增量 (4, A), 首样本位置 (A, 3), 首样本航向 (A,), state (A,),
        采用标志 (A,), 中点时刻, 6 个样本时刻)。"""
        key = (lo, hi)
        if key in self._fits:
            return self._fits[key]
        n = hi - lo
        ts = (lo, lo + n // 3, lo + (2 * n) // 3, hi)
        mid = lo + n // 2
        tc = (lo + n // 6, mid)
        S = [self.sample(x) for x in ts]
        C = [self.sample(x) for x in tc]
        tau = np.array([(x - lo) / n for x in ts])
        P = np.stack([s[0] for s in S])
        Y = np.stack([s[1] for s in S])
        dP = P - P[0]
        dY = _wrap(Y - Y[0])
        st = np.stack([s[2] for s in S + C])
        ct = np.stack([s[3] for s in S + C])
        ok = (st == st[0]).all(0) & (ct == ct[0]).all(0)
        vis = np.isin(st[0], _VIS)
        W = _lagrange(tau, np.array([(x - lo) / n for x in tc]))
        for w, sc in zip(W, C, strict=True):
            e_p = np.linalg.norm(np.einsum("j,jak->ak", w, dP) - (sc[0] - P[0]), axis=1)
            e_y = np.abs(_wrap(np.einsum("j,ja->a", w, dY) - _wrap(sc[1] - Y[0])))
            ok &= ~vis | ((e_p <= TRACK_TOL_M) & (e_y <= TRACK_TOL_RAD))
        self._fits[key] = r = (tau, dP, dY, P[0], Y[0], st[0], ok, mid, (*ts, *tc))
        return r

    def frame_samples(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """帧区间 6 个样本（两端、三等分点、两校验点）的 (pos (6, A, 3), yaw (6, A), state (6, A))，供候选筛选。"""
        S = [self.samples[x] for x in self._fit(self.t0, self.t1)[8]]
        return np.stack([s[0] for s in S]), np.stack([s[1] for s in S]), np.stack([s[2] for s in S])

    def frame_steady(self) -> np.ndarray:
        return self._fit(self.t0, self.t1)[6]

    def eval(self, a: int, t_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """演员 a 在各整数时刻 t_ns (n,) ∈ [t0, t1] 的 (pos (n, 3), yaw (n,), state (n,))。"""
        t_ns = np.asarray(t_ns, np.int64)
        pos, yaw, st = np.empty((len(t_ns), 3)), np.empty(len(t_ns)), np.empty(len(t_ns), np.uint8)
        self._eval(a, t_ns, np.arange(len(t_ns)), self.t0, self.t1, pos, yaw, st)
        return pos, yaw, st

    def _eval(self, a, t_ns, sel, lo, hi, pos, yaw, st) -> None:
        if not len(sel):
            return
        if hi - lo >= TRACK_MIN_NS:
            tau, dP, dY, p0, y0, s0, ok, mid, _ts = self._fit(lo, hi)
            if ok[a]:
                W = _lagrange(tau, (t_ns[sel] - lo) / (hi - lo))
                pos[sel] = p0[a] + W @ dP[:, a]
                yaw[sel] = y0[a] + W @ dY[:, a]
                st[sel] = s0[a]
                return
            if len(sel) > TRACK_DIRECT_MAX:
                left = t_ns[sel] < mid
                self._eval(a, t_ns, sel[left], lo, mid, pos, yaw, st)
                self._eval(a, t_ns, sel[~left], mid, hi, pos, yaw, st)
                return
        for j in sel:
            p, y, s, _c = self.sample(int(t_ns[j]))
            pos[j], yaw[j], st[j] = p[a], y[a], s[a]
            self.direct += 1
