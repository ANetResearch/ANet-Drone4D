"""网格 LiDAR 单帧求交（M25 §6.8.3–§6.8.7）：CPU 实现（Open3D，经 M22 `MeshWorldQuery`），与 GPU（Warp）路径同一算法与随机键。

- 时刻：槽位 i 的时刻 t_s + i·5 µs；K = 11 个位姿节点（t_s + 10 ms·m），槽位位姿为相邻节点位置线性、姿态四元数 nlerp（同半球）。
- 方向：MID-360 花样（`awr.sim.sensors.lidar.pattern`，雷达系 FLU）；角度噪声只扰动求交方向，输出点沿名义方向 × 测得距离，
  即点时刻传感器系（真实运动畸变保留在点云中）。
- 静态：MeshWorldQuery 精确求交（命中三角形的语义码与法线）；演员：每个节点段（10 ms）取段中时刻的演员状态，可见演员
  （state 1、2）以与相机相同的代理网格建场景求交，取较近者（实例 idx + 1）。
- 回波（`sem_table_v1` 的 P0a 子集）：rho = rho_sem[cls]（不做纹理亮度调制与阴影补偿，待反射率纹理就绪），rho_eff = rho·max(|n·d|, 0.05)·
  exp(−2β·r)，r_max = min(r_hard, 40·(rho_eff/0.1)^0.269)，逻辑斯蒂检测，测距噪声 sigma(r)；虚警 1e-4（realistic、harsh）。
- 随机数：帧键 K_f = key(run_seed, 22, uav_no, frame_seq, 0x2000 + sensor_no)，槽位键 sm(K_f ^ (i·8 + c))（c：0 检测、1 测距、
  2 角度、3 虚警判定、4 虚警距离）。clean 档无测距、角度噪声与虚警，检测概率仍按量程模型。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import cache

import numpy as np

from awr.bench.recorder.lidar import LIDAR_RECORD
from awr.sensorsvc.lidar.model import LidarModel
from awr.sim.sensors import cbrng
from awr.sim.sensors.lidar.pattern import directions_flu, mid360_pattern

__all__ = ["INST_NONE", "INST_SPURIOUS", "LIDAR_RECORD", "TIER_SCALE", "LidarScan", "MeshLidar", "rho_sem"]

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
    """`q` 为 MeshWorldQuery；`actors` 为磁带演员表（按 idx），与相机代理同一几何。"""

    def __init__(self, q, model: LidarModel, actors: list[dict] | None = None) -> None:
        self.q = q
        self.m = model
        self.actors = actors or []
        self._mesh: dict[int, tuple[np.ndarray, float]] = {}
        self._az = np.empty(model.slots_per_frame)
        self._el = np.empty(model.slots_per_frame)

    def _actor_mesh(self, i: int) -> tuple[np.ndarray, float]:
        if i not in self._mesh:
            from awr.sensorsvc.camera.actors import proxy_mesh

            P, _N, _C = proxy_mesh(self.actors[i])
            self._mesh[i] = (P.astype(np.float64), float(np.linalg.norm(P, axis=1).max()))
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
            n_act = self._actors(o, d, t, nrm, sem, inst, seg, t_start_ns, seg_ns, K, actor_states)
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
        void = ~hit & (el_w < 0)
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

    def _actors(self, o, d, t, nrm, sem, inst, seg, t_start_ns, seg_ns, K, actor_states) -> int:
        """逐节点段：段中时刻的可见演员建 Open3D 场景（以段首传感器位置为局部原点，float32 相对坐标）。"""
        import open3d as o3d

        from awr.sensorsvc.camera.actors import SEM_CODE

        n_hits = 0
        for s in range(K - 1):
            sel = np.nonzero(seg == s)[0]
            if not len(sel):
                continue
            st = actor_states(t_start_ns + s * seg_ns + seg_ns // 2)
            if st is None:
                continue
            o0 = o[sel[0]]
            vis = np.isin(st.state, (1, 2))
            cand = []
            for j in np.nonzero(vis)[0]:
                idx = int(st.ids[j])
                Pl, rad = self._actor_mesh(idx)
                if np.linalg.norm(st.pos_enu_m[j] - o0) <= self.m.hard_max_m + rad + 1.0:
                    cand.append((idx, Pl, st.pos_enu_m[j], float(st.yaw_rad[j])))
            if not cand:
                continue
            scene = o3d.t.geometry.RaycastingScene()
            gid = {}
            for idx, Pl, pos, yaw in cand:
                c, sn = math.cos(yaw), math.sin(yaw)
                Rz = np.array([[c, -sn, 0.0], [sn, c, 0.0], [0.0, 0.0, 1.0]])
                V = (Pl @ Rz.T + (pos - o0)).astype(np.float32)
                F = np.arange(len(V), dtype=np.uint32).reshape(-1, 3)
                gid[int(scene.add_triangles(o3d.core.Tensor(V), o3d.core.Tensor(F)))] = idx
            rays = np.hstack([o[sel] - o0, d[sel]]).astype(np.float32)
            res = scene.cast_rays(o3d.core.Tensor(rays))
            ta = res["t_hit"].numpy().astype(np.float64)
            g = res["geometry_ids"].numpy()
            na = res["primitive_normals"].numpy().astype(np.float64)
            closer = np.isfinite(ta) & (ta < np.where(np.isfinite(t[sel]), t[sel], np.inf))
            if closer.any():
                k = sel[closer]
                t[k] = ta[closer]
                nrm[k] = na[closer]
                ids = np.array([gid[int(x)] for x in g[closer]])
                inst[k] = ids + 1
                sem[k] = [SEM_CODE.get(self.actors[x]["class"], 13) for x in ids]
                n_hits += int(closer.sum())
        return n_hits
