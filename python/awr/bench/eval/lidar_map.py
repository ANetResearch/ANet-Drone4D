"""L0 离线已知位姿地图与多机覆盖（M27-FR-107、§6.7.7 `l0map`；AWR-05 §5.5.6；D3-AC-15，D3-AC-27 的 Jaccard 口径）。

`slam_eval` 中的 `eval_sim` 以真值逐点位姿离线积分 LiDAR 点，产出每机 L0 地图、覆盖率、两两 Jaccard 与合并覆盖率；不做 TSDF
面重建（r1：TSDF 0.15 m、80 m 深度在约 1 km 航线上内存达约 400 GB）。

- 逐点位姿：点 i 的时刻 `timebase_ns + offset_time_i`，在真值传感器轨迹（`SensorTrajectory`：机体 PoseHistory 与名义挂载合成，
  位置线性、姿态球面插值，与 `awr.sensorsvc.poses` 同式）上取 WORLD←FLU 位姿；射线起点为该时刻传感器原点，终点为
  `R(t_i)·xyz_i + p(t_i)`。不按帧位姿积分（r4：8 m/s 时中位误差 0.47 m）。
- 过滤：传感器系距离 `|xyz|` 为 0（全槽位零点占位）、非有限、超过深度上限（缺省 60 m）的点不入图；`tag` 低 2 位非 0
  （01 = 虚警等噪声回波，契约 `lidar_frame.v2`）的点不入图。
- 占据：稀疏 8³ 块哈希 log-odds，体素 0.25 m，网格与世界原点对齐（各机地图可直接求交）。log-odds 以 0.01 为单位的 int16 定点存储
  （命中 +0.85、穿越 −0.40、钳位 [−2.0, 3.5]，与 r4 原型同参数），整数运算逐位确定；占据判据 log-odds ≥ 0.85。
- 每帧更新（OctoMap `computeUpdate` 语义）：一帧内每个体素至多更新一次，命中优先于穿越；穿越体素为起点体素到终点体素之前的
  3D-DDA（Amanatides–Woo）6 连通序列。DDA 的下一个边界参数逐轴按 `(边界 − a) / d` 直接计算（不做增量累加），平局按 x、y、z
  顺序；与独立参考实现（逐轴交点按 (t, 轴) 归并）逐体素相同。
- 覆盖与 Jaccard：0.5 m 表面体素 = 占据体素（0.25 m）所在的 0.5 m 格；参考集 S_ref 缺省为全机队入图命中点所在的 0.5 m 格
  （§6.7.7：全机队真值位姿射线命中），可传入网格采样参考集。两两 Jaccard = |A 交 B| / |A 并 B|；合并覆盖 = |各机并集 交 S_ref| / |S_ref|。
- 到网格距离：占据体素中心到 GLB 网格的无符号距离，经 `MeshWorldQuery.closest_batch`（Open3D `compute_distance`，瓦片局部原点
  float32）；对比视图量化为 u8（0–0.5 m，步长 2 mm，超出按 0.5 m；AWR-14 §21.8）。
- 内存：块池与哈希表的容量计入 `nbytes`；超过 `budget_bytes`（缺省 8 GiB，扩容瞬时峰值不超过 1.5 倍，作业 `--memory 16g`）时
  抛 684 SLAM_JOB_FAILED，不静默截断。积分核为 numba（项目核心依赖）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from itertools import combinations
from pathlib import Path

import numpy as np
from numba import njit

from awr.contracts.reasons import REASONS
from awr.sim.sensors.frames import mount_R, quat_to_R

__all__ = [
    "JACCARD_MAX",
    "MAP_VERSION",
    "STORAGE_BYTES_PER_BLOCK",
    "L0Map",
    "L0MapError",
    "L0Params",
    "SensorTrajectory",
    "coverage_and_jaccard",
    "distance_stats",
    "fleet_report",
    "mesh_distance",
    "mesh_error_u8",
    "pack_keys",
    "unpack_keys",
    "world_rays",
]

MAP_VERSION = "l0map-1"
JACCARD_MAX = 0.3                       # D3-AC-15 / D3-AC-27：两两覆盖 Jaccard ≤ 0.3（0.5 m 表面体素）
LO_UNIT = 0.01                          # log-odds 定点单位
BLOCK = 8
BLOCK_VOX = BLOCK ** 3
ERR_CAP_M, ERR_STEP_M = 0.5, 0.002      # 对比视图误差着色：0–0.5 m，步长 2 mm（u8 0–250）
_OFF = 1 << 20                          # 打包键每轴 21 位、偏置 2^20
_MASK21 = (1 << 21) - 1
_FREE_BITS = np.uint8(0x03)             # flag 低 2 位：本帧已更新（1 命中、2 穿越）；bit7：曾被观测
_OBS = np.uint8(0x80)
_M1, _M2 = np.uint64(0xBF58476D1CE4E5B9), np.uint64(0x94D049BB133111EB)
_S30, _S27, _S31 = np.uint64(30), np.uint64(27), np.uint64(31)
_BYTES_PER_BLOCK = BLOCK_VOX * 3        # 内存：int16 log-odds + uint8 flag
STORAGE_BYTES_PER_BLOCK = BLOCK_VOX * 2 + BLOCK_VOX // 8 + 8     # 存储口径：int16 log-odds、观测位图、块键（1096 B/块）


class L0MapError(RuntimeError):
    """L0 地图作业失败（684 SLAM_JOB_FAILED：内存预算超限、真值位姿不覆盖点时刻、坐标越界）。"""

    def __init__(self, detail: str, code: int = 684, **extra) -> None:
        self.code = int(code)
        self.name = REASONS[self.code].name
        self.detail = detail
        self.extra = extra
        super().__init__(f"{self.code} {self.name}: {detail}")


@dataclass(frozen=True)
class L0Params:
    """L0 地图参数（M27-FR-107）；log-odds 为实数值，内部按 0.01 定点。"""

    voxel_m: float = 0.25
    max_depth_m: float = 60.0
    l_hit: float = 0.85
    l_miss: float = -0.40
    l_min: float = -2.0
    l_max: float = 3.5
    l_occ: float = 0.85
    surface_voxel_m: float = 0.5
    tag_drop_mask: int = 0x03
    budget_bytes: int = 8 << 30

    def fixed(self) -> tuple[int, int, int, int, int]:
        q = lambda v: round(v / LO_UNIT)                                        # noqa: E731
        return q(self.l_hit), q(self.l_miss), q(self.l_min), q(self.l_max), q(self.l_occ)

    @property
    def coarse(self) -> int:
        k = self.surface_voxel_m / self.voxel_m
        if abs(k - round(k)) > 1e-9 or round(k) < 1:
            raise ValueError("surface_voxel_m must be an integer multiple of voxel_m")
        return round(k)


# ---------------------------------------------------------------------------------------------------------------- 键
def pack_keys(v: np.ndarray) -> np.ndarray:
    """(n, 3) int64 格坐标 → int64 打包键（每轴 21 位，偏置 2^20；按键排序即按 (x, y, z) 字典序）。"""
    v = np.asarray(v, np.int64).reshape(-1, 3)
    if len(v) and (v.min() < -_OFF or v.max() >= _OFF):
        raise L0MapError("grid coordinate outside the 21-bit key range")
    return ((v[:, 0] + _OFF) << 42) | ((v[:, 1] + _OFF) << 21) | (v[:, 2] + _OFF)


def unpack_keys(k: np.ndarray) -> np.ndarray:
    k = np.asarray(k, np.int64)
    return np.stack([((k >> 42) & _MASK21) - _OFF, ((k >> 21) & _MASK21) - _OFF, (k & _MASK21) - _OFF], 1)


# ---------------------------------------------------------------------------------------------------------------- 轨迹
def _slerp_batch(q0: np.ndarray, q1: np.ndarray, u: np.ndarray) -> np.ndarray:
    """逐行球面插值（[x, y, z, w]；q1 先翻到 q0 同半球，点积 > 0.9995 时线性插值后归一化，同 `awr.sensorsvc.poses.slerp`）。"""
    d = np.einsum("ij,ij->i", q0, q1)
    q1 = np.where((d < 0)[:, None], -q1, q1)
    d = np.abs(d)
    out = np.empty_like(q0)
    lin = d > 0.9995
    if lin.any():
        q = q0[lin] + u[lin, None] * (q1[lin] - q0[lin])
        out[lin] = q / np.linalg.norm(q, axis=1, keepdims=True)
    s = ~lin
    if s.any():
        th = np.arccos(np.minimum(d[s], 1.0))
        sn = np.sin(th)
        out[s] = (np.sin((1 - u[s]) * th) / sn)[:, None] * q0[s] + (np.sin(u[s] * th) / sn)[:, None] * q1[s]
    return out


@dataclass
class SensorTrajectory:
    """真值传感器轨迹：时刻严格递增的 WORLD←FLU 位姿样本（ENU，m；四元数 [x, y, z, w]）。"""

    t_ns: np.ndarray
    p: np.ndarray
    q: np.ndarray

    def __post_init__(self) -> None:
        self.t_ns = np.ascontiguousarray(self.t_ns, np.int64).reshape(-1)
        self.p = np.ascontiguousarray(self.p, np.float64).reshape(-1, 3)
        q = np.asarray(self.q, np.float64).reshape(-1, 4)
        self.q = q / np.linalg.norm(q, axis=1, keepdims=True)
        if not (len(self.t_ns) == len(self.p) == len(self.q)) or len(self.t_ns) < 2:
            raise L0MapError("trajectory needs >= 2 samples with matching t, p and q")
        if (np.diff(self.t_ns) <= 0).any():
            raise L0MapError("trajectory t_ns must increase strictly")

    @classmethod
    def from_base(cls, t_ns, pos_enu_m, q_ws_xyzw, mount_xyz_m=(0.0, 0.0, 0.0), mount_rpy_deg=(0.0, 0.0, 0.0)) -> SensorTrajectory:
        """机体真值（PoseHistory）与名义挂载合成传感器轨迹：`T_world_sensor = T_world_base · T_base_mount`（无云台的 LiDAR）。"""
        from awr.sim.sensors.frames import R_to_quat_xyzw

        p_b = np.asarray(pos_enu_m, np.float64).reshape(-1, 3)
        q_b = np.asarray(q_ws_xyzw, np.float64).reshape(-1, 4)
        q_b = q_b / np.linalg.norm(q_b, axis=1, keepdims=True)
        R_wb = quat_to_R(q_b) if len(q_b) > 1 else quat_to_R(q_b).reshape(1, 3, 3)
        Rm = mount_R(mount_rpy_deg)
        p_s = p_b + np.einsum("nij,j->ni", R_wb, np.asarray(mount_xyz_m, np.float64))
        R_ws = R_wb @ Rm
        q_s = np.array([R_to_quat_xyzw(R) for R in R_ws], np.float64)
        for k in range(1, len(q_s)):                                               # 连续同半球（插值前 slerp 仍逐对翻转）
            if q_s[k] @ q_s[k - 1] < 0:
                q_s[k] = -q_s[k]
        return cls(np.asarray(t_ns, np.int64), p_s, q_s)

    @classmethod
    def from_pose_history(cls, messages: Iterable[bytes], mount_xyz_m=(0.0, 0.0, 0.0),
                          mount_rpy_deg=(0.0, 0.0, 0.0)) -> SensorTrajectory:
        """`drone4d.pose_history.v1` 消息序列（同一架机，按存档顺序）→ 传感器轨迹；重复时刻只保留首次出现的样本。"""
        from awr.phys.pose_history import unpack_pose_history

        ts, ps, qs = [], [], []
        for m in messages:
            _h, blk = unpack_pose_history(m)
            ts.append(blk["t_ns"])
            ps.append(blk["pos_enu_m"])
            qs.append(blk["q_ws_xyzw"])
        if not ts:
            raise L0MapError("no pose_history messages")
        t = np.concatenate(ts)
        t_u, first = np.unique(t, return_index=True)
        return cls.from_base(t_u, np.concatenate(ps)[first], np.concatenate(qs)[first], mount_xyz_m, mount_rpy_deg)

    @property
    def span_ns(self) -> tuple[int, int]:
        return int(self.t_ns[0]), int(self.t_ns[-1])

    def at(self, t_ns) -> tuple[np.ndarray, np.ndarray]:
        """逐时刻位姿：(R (n, 3, 3) WORLD←FLU, p (n, 3))；时刻须落在样本区间内（不外推，否则 684）。"""
        t = np.asarray(t_ns, np.int64).reshape(-1)
        if len(t) == 0:
            return np.empty((0, 3, 3)), np.empty((0, 3))
        if t.min() < self.t_ns[0] or t.max() > self.t_ns[-1]:
            raise L0MapError(f"point time outside the truth trajectory [{self.t_ns[0]}, {self.t_ns[-1]}]",
                             t_min=int(t.min()), t_max=int(t.max()))
        n = np.clip(np.searchsorted(self.t_ns, t, side="right") - 1, 0, len(self.t_ns) - 2)
        u = (t - self.t_ns[n]) / (self.t_ns[n + 1] - self.t_ns[n])
        p = self.p[n] * (1 - u)[:, None] + self.p[n + 1] * u[:, None]
        q = _slerp_batch(self.q[n], self.q[n + 1], u.astype(np.float64))
        return quat_to_R(q).reshape(-1, 3, 3), p


def world_rays(timebase_ns: int, points: np.ndarray, traj: SensorTrajectory, params: L0Params | None = None) -> tuple[
        np.ndarray, np.ndarray, dict]:
    """一帧观测点（`LIDAR_RECORD`，点时刻传感器系）→ 世界系射线 (起点 (m, 3), 终点 (m, 3), 过滤计数)，逐点位姿。"""
    params = params or L0Params()
    pts = np.asarray(points)
    xyz = np.stack([pts["x"], pts["y"], pts["z"]], 1).astype(np.float64)
    r = np.linalg.norm(xyz, axis=1)
    invalid = ~np.isfinite(xyz).all(axis=1) | (r == 0.0)
    tag_drop = ~invalid & ((pts["tag"].astype(np.int64) & int(params.tag_drop_mask)) != 0)
    deep = ~invalid & ~tag_drop & (r > params.max_depth_m)
    keep = ~invalid & ~tag_drop & ~deep
    cnt = {"points": len(pts), "used": int(keep.sum()), "dropped_depth": int(deep.sum()), "dropped_tag": int(tag_drop.sum()),
           "dropped_invalid": int(invalid.sum())}
    if not keep.any():
        return np.empty((0, 3)), np.empty((0, 3)), cnt
    R, p = traj.at(int(timebase_ns) + pts["offset_time"][keep].astype(np.int64))
    return p, np.einsum("nij,nj->ni", R, xyz[keep]) + p, cnt


def _coherent_order(d: np.ndarray) -> np.ndarray:
    """射线按方向 1° 格蛇形排序（相邻射线穿越相邻块，缓存命中率高；本机实测单帧约快 1.8 倍）。帧内每体素至多更新一次，
    更新结果与射线顺序无关，排序只影响速度。"""
    el = np.floor(np.degrees(np.arctan2(d[:, 2], np.hypot(d[:, 0], d[:, 1])))).astype(np.int64) + 90
    az = np.floor(np.degrees(np.arctan2(d[:, 1], d[:, 0]))).astype(np.int64) + 180
    az = np.where(el % 2 == 0, az, 359 - az)
    return np.argsort(el * 360 + az, kind="stable")


# ---------------------------------------------------------------------------------------------------------------- 积分核
@njit(cache=True, fastmath=False)
def _probe(keys, key):
    mask = keys.shape[0] - 1
    z = np.uint64(key)                                                              # splitmix64 终混合
    z = (z ^ (z >> _S30)) * _M1
    z = (z ^ (z >> _S27)) * _M2
    h = np.int64((z ^ (z >> _S31)) & np.uint64(mask))
    while True:
        k = keys[h]
        if k == key or k == -1:
            return h
        h = (h + 1) & mask


@njit(cache=True, fastmath=False)
def _rehash(keys, vals, cap):
    nk = np.full(cap, -1, np.int64)
    nv = np.zeros(cap, np.int32)
    for i in range(keys.shape[0]):
        if keys[i] != -1:
            h = _probe(nk, keys[i])
            nk[h] = keys[i]
            nv[h] = vals[i]
    return nk, nv


@njit(cache=True, fastmath=False)
def _bkey(x, y, z):
    return (((x >> 3) + 1048576) << 42) | (((y >> 3) + 1048576) << 21) | ((z >> 3) + 1048576)


# 核内不重绑定数组（numba 对循环内重绑定的数组变量做引用计数与 phi 合并，实测整段循环慢约 6 倍）：容量不足时返回状态码与
# 断点，由 Python 侧扩容后从断点射线重入。帧内每体素至多更新一次（flag 低 2 位），重入重复处理的射线不会二次更新。
ST_DONE, ST_POOL, ST_TABLE, ST_TOUCHED, ST_BUDGET = 0, 1, 2, 3, -1


@njit(cache=True, fastmath=False)
def _scan(a, b, phase, r0, keys, vals, lo, flag, nblk, touched, nt, l_hit, l_miss, l_min, l_max, max_blocks):
    """一帧射线（体素单位坐标 a 起点、b 终点）：phase 0 命中、phase 1 穿越（Amanatides–Woo，起点体素至终点体素之前）。

    返回 (状态, 阶段, 断点射线, nblk, nt, 命中更新数, 穿越更新数)；完成时清除本帧标记。"""
    n_hit = 0
    n_free = 0
    n = a.shape[0]
    inf = np.inf
    if phase == 0:
        for r in range(r0, n):
            ex = np.int64(math.floor(b[r, 0]))
            ey = np.int64(math.floor(b[r, 1]))
            ez = np.int64(math.floor(b[r, 2]))
            bk = _bkey(ex, ey, ez)
            h = _probe(keys, bk)
            if keys[h] == bk:
                s = np.int64(vals[h])
            else:
                if nblk >= max_blocks:
                    return ST_BUDGET, 0, r, nblk, nt, n_hit, n_free
                if nblk >= lo.shape[0]:
                    return ST_POOL, 0, r, nblk, nt, n_hit, n_free
                if 2 * (nblk + 1) > keys.shape[0]:
                    return ST_TABLE, 0, r, nblk, nt, n_hit, n_free
                keys[h] = bk
                vals[h] = nblk
                s = nblk
                nblk += 1
            loc = (ex & 7) + 8 * (ey & 7) + 64 * (ez & 7)
            f = flag[s, loc]
            if f & 3 == 0:
                if nt >= touched.shape[0]:
                    return ST_TOUCHED, 0, r, nblk, nt, n_hit, n_free
                flag[s, loc] = f | 0x81
                touched[nt] = s * 512 + loc
                nt += 1
                lo[s, loc] = min(max(np.int64(lo[s, loc]) + l_hit, l_min), l_max)
                n_hit += 1
        r0 = 0
    for r in range(r0, n):
        ax, ay, az = a[r, 0], a[r, 1], a[r, 2]
        dx, dy, dz = b[r, 0] - ax, b[r, 1] - ay, b[r, 2] - az
        ix = np.int64(math.floor(ax))
        iy = np.int64(math.floor(ay))
        iz = np.int64(math.floor(az))
        nx = abs(np.int64(math.floor(b[r, 0])) - ix)
        ny = abs(np.int64(math.floor(b[r, 1])) - iy)
        nz = abs(np.int64(math.floor(b[r, 2])) - iz)
        sx = np.int64(1) if dx > 0 else np.int64(-1)
        sy = np.int64(1) if dy > 0 else np.int64(-1)
        sz = np.int64(1) if dz > 0 else np.int64(-1)
        kx = np.int64(0)
        ky = np.int64(0)
        kz = np.int64(0)
        cx, cy, cz = ix, iy, iz
        last = np.int64(-1)
        s = np.int64(-1)
        for _ in range(nx + ny + nz):
            bk = _bkey(cx, cy, cz)
            if bk != last:
                h = _probe(keys, bk)
                if keys[h] == bk:
                    s = np.int64(vals[h])
                else:
                    if nblk >= max_blocks:
                        return ST_BUDGET, 1, r, nblk, nt, n_hit, n_free
                    if nblk >= lo.shape[0]:
                        return ST_POOL, 1, r, nblk, nt, n_hit, n_free
                    if 2 * (nblk + 1) > keys.shape[0]:
                        return ST_TABLE, 1, r, nblk, nt, n_hit, n_free
                    keys[h] = bk
                    vals[h] = nblk
                    s = nblk
                    nblk += 1
                last = bk
            loc = (cx & 7) + 8 * (cy & 7) + 64 * (cz & 7)
            f = flag[s, loc]
            if f & 3 == 0:
                if nt >= touched.shape[0]:
                    return ST_TOUCHED, 1, r, nblk, nt, n_hit, n_free
                flag[s, loc] = f | 0x82
                touched[nt] = s * 512 + loc
                nt += 1
                lo[s, loc] = min(max(np.int64(lo[s, loc]) + l_miss, l_min), l_max)
                n_free += 1
            tx = inf
            ty = inf
            tz = inf
            if kx < nx:
                tx = ((ix + 1 + kx if sx > 0 else ix - kx) - ax) / dx
            if ky < ny:
                ty = ((iy + 1 + ky if sy > 0 else iy - ky) - ay) / dy
            if kz < nz:
                tz = ((iz + 1 + kz if sz > 0 else iz - kz) - az) / dz
            if tx <= ty and tx <= tz:
                cx += sx
                kx += 1
            elif ty <= tz:
                cy += sy
                ky += 1
            else:
                cz += sz
                kz += 1
    for i in range(nt):
        flag[touched[i] // 512, touched[i] % 512] &= 0x80
    return ST_DONE, 1, n, nblk, 0, n_hit, n_free


@njit(cache=True, fastmath=False)
def _add_blocks(i0, bkeys, blo, bobs, keys, vals, lo, flag, nblk, l_min, l_max, max_blocks):
    """块级 log-odds 相加（合并与装载）：bkeys (m,) 块键、blo (m, 512) int16、bobs (m, 512) bool 观测位；返回 (状态, 断点, nblk)。"""
    for i in range(i0, bkeys.shape[0]):
        h = _probe(keys, bkeys[i])
        if keys[h] == bkeys[i]:
            s = np.int64(vals[h])
        else:
            if nblk >= max_blocks:
                return ST_BUDGET, i, nblk
            if nblk >= lo.shape[0]:
                return ST_POOL, i, nblk
            if 2 * (nblk + 1) > keys.shape[0]:
                return ST_TABLE, i, nblk
            keys[h] = bkeys[i]
            vals[h] = nblk
            s = nblk
            nblk += 1
        for j in range(512):
            lo[s, j] = min(max(np.int64(lo[s, j]) + np.int64(blo[i, j]), l_min), l_max)
            if bobs[i, j]:
                flag[s, j] |= 0x80
    return ST_DONE, bkeys.shape[0], nblk


@njit(cache=True, fastmath=False)
def _lookup(v, keys, vals, lo, flag, out_lo, out_obs):
    for i in range(v.shape[0]):
        h = _probe(keys, _bkey(v[i, 0], v[i, 1], v[i, 2]))
        if keys[h] == -1:
            out_lo[i] = 0
            out_obs[i] = False
        else:
            s = vals[h]
            loc = (v[i, 0] & 7) + 8 * (v[i, 1] & 7) + 64 * (v[i, 2] & 7)
            out_lo[i] = lo[s, loc]
            out_obs[i] = (flag[s, loc] & 0x80) != 0


# ---------------------------------------------------------------------------------------------------------------- 地图
@dataclass(eq=False)
class L0Map:
    """单机 L0 地图（稀疏 8³ 块哈希 log-odds）。`integrate_frame` 逐帧积分；超出内存预算时抛 684 后地图作废。"""

    params: L0Params = field(default_factory=L0Params)
    uav: str = ""

    def __post_init__(self) -> None:
        self._lo = np.zeros((4096, BLOCK_VOX), np.int16)
        self._flag = np.zeros((4096, BLOCK_VOX), np.uint8)
        self._keys = np.full(16384, -1, np.int64)
        self._vals = np.zeros(16384, np.int32)
        self._touched = np.empty(1 << 18, np.int64)
        self._nblk = 0
        self._nt = 0
        self._hit_parts: list[np.ndarray] = []
        self._hit_surface = np.empty(0, np.int64)
        self._failed = False
        self.peak_nbytes = self.nbytes
        self.counts = {"frames": 0, "points": 0, "used": 0, "dropped_depth": 0, "dropped_tag": 0, "dropped_invalid": 0,
                       "hit_updates": 0, "free_updates": 0}
        self.t_range_ns: list[int] | None = None

    # -------------------------------------------------------------- 预算
    @property
    def max_blocks(self) -> int:
        return max(int(self.params.budget_bytes // (_BYTES_PER_BLOCK + 48)), 1)

    @property
    def nbytes(self) -> int:
        return int(self._lo.nbytes + self._flag.nbytes + self._keys.nbytes + self._vals.nbytes + self._touched.nbytes)

    @property
    def n_blocks(self) -> int:
        return self._nblk

    def _grow(self, st: int) -> None:
        """核返回的容量状态 → 扩容（块池与断点记录翻倍、哈希表翻倍重排）；预算超限时地图作废并抛 684。"""
        if st == ST_POOL:
            cap = min(2 * len(self._lo), self.max_blocks)
            lo = np.zeros((cap, BLOCK_VOX), np.int16)
            fl = np.zeros((cap, BLOCK_VOX), np.uint8)
            lo[:self._nblk] = self._lo[:self._nblk]
            fl[:self._nblk] = self._flag[:self._nblk]
            self.peak_nbytes = max(self.peak_nbytes, self.nbytes + lo.nbytes + fl.nbytes)
            self._lo, self._flag = lo, fl
        elif st == ST_TABLE:
            self._keys, self._vals = _rehash(self._keys, self._vals, 2 * len(self._keys))
        elif st == ST_TOUCHED:
            t = np.empty(2 * len(self._touched), np.int64)
            t[:self._nt] = self._touched[:self._nt]
            self._touched = t
        else:
            self._failed = True
            raise L0MapError(f"L0 map exceeds the memory budget of {self.params.budget_bytes} bytes",
                             blocks=int(self._nblk), budget_bytes=int(self.params.budget_bytes))
        self.peak_nbytes = max(self.peak_nbytes, self.nbytes)

    def _check(self) -> None:
        if self._failed:
            raise L0MapError("map is invalid after a failed integration")

    # -------------------------------------------------------------- 积分
    def integrate_scan(self, origins: np.ndarray, ends: np.ndarray) -> None:
        """一帧世界系射线（m）：每体素每帧至多更新一次，命中优先。"""
        self._check()
        o = np.asarray(origins, np.float64).reshape(-1, 3)
        e = np.asarray(ends, np.float64).reshape(-1, 3)
        if len(o) != len(e):
            raise ValueError("origins and ends differ in length")
        if len(o) == 0:
            return
        if not (np.isfinite(o).all() and np.isfinite(e).all()):
            raise L0MapError("non-finite ray coordinates")
        vox = self.params.voxel_m
        a = np.ascontiguousarray(o / vox)
        b = np.ascontiguousarray(e / vox)
        lim = float(_OFF * BLOCK - BLOCK)
        if max(np.abs(a).max(), np.abs(b).max()) >= lim:
            raise L0MapError("ray coordinates outside the block key range")
        order = _coherent_order(b - a)
        a, b = np.ascontiguousarray(a[order]), np.ascontiguousarray(b[order])
        h, m, lmin, lmax, _ = self.params.fixed()
        phase, r0, nh, nf = 0, 0, 0, 0
        while True:
            st, phase, r0, self._nblk, self._nt, dh, df = _scan(a, b, phase, r0, self._keys, self._vals, self._lo, self._flag, self._nblk,
                                                                 self._touched, self._nt, h, m, lmin, lmax, self.max_blocks)
            nh, nf = nh + dh, nf + df
            if st == ST_DONE:
                break
            self._grow(st)
        self.counts["hit_updates"] += int(nh)
        self.counts["free_updates"] += int(nf)
        cs = np.floor(e / self.params.surface_voxel_m).astype(np.int64)
        self._hit_parts.append(np.unique(pack_keys(cs)))
        if sum(len(x) for x in self._hit_parts) > (1 << 21):
            self._fold_hits()

    def integrate_frame(self, timebase_ns: int, points: np.ndarray, traj: SensorTrajectory) -> dict:
        """一帧 `LIDAR_RECORD` 观测（`lidar_frame.v2`）按逐点真值位姿积分；返回该帧过滤计数。"""
        o, e, cnt = world_rays(timebase_ns, points, traj, self.params)
        self.integrate_scan(o, e)
        self.counts["frames"] += 1
        for k, v in cnt.items():
            self.counts[k] += v
        t0 = int(timebase_ns)
        t1 = t0 + (int(points["offset_time"].max()) if len(points) else 0)
        self.t_range_ns = [t0, t1] if self.t_range_ns is None else [min(self.t_range_ns[0], t0), max(self.t_range_ns[1], t1)]
        return cnt

    def integrate(self, frames: Iterable[tuple[int, np.ndarray]], traj: SensorTrajectory) -> L0Map:
        for t0, pts in frames:
            self.integrate_frame(t0, pts, traj)
        return self

    def _fold_hits(self) -> None:
        if self._hit_parts:
            self._hit_surface = np.unique(np.concatenate([self._hit_surface, *self._hit_parts]))
            self._hit_parts = []

    # -------------------------------------------------------------- 查询
    def _blocks(self) -> tuple[np.ndarray, np.ndarray]:
        """(块键 (m,), 槽位 (m,))，按块键升序。"""
        used = self._keys != -1
        k, s = self._keys[used], self._vals[used].astype(np.int64)
        o = np.argsort(k)
        return k[o], s[o]

    def occupied_voxels(self) -> np.ndarray:
        """占据体素（log-odds ≥ l_occ）的格坐标 (m, 3) int64，按 (x, y, z) 字典序。"""
        self._check()
        occ = self.params.fixed()[4]
        bk, sl = self._blocks()
        m = self._lo[sl] >= occ
        bi, loc = np.nonzero(m)
        base = unpack_keys(bk[bi]) * BLOCK
        v = base + np.stack([loc & 7, (loc >> 3) & 7, loc >> 6], 1)
        return v[np.argsort(pack_keys(v), kind="stable")]

    def occupied_centers(self) -> np.ndarray:
        return (self.occupied_voxels().astype(np.float64) + 0.5) * self.params.voxel_m

    def observed_count(self) -> int:
        _bk, sl = self._blocks()
        return int(((self._flag[sl] & _OBS) != 0).sum())

    def logodds(self, voxels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """格坐标 (n, 3) → (log-odds 实数值 (n,), 是否曾被观测 (n,))；未分配的块为 (0, False)。"""
        v = np.ascontiguousarray(np.asarray(voxels, np.int64).reshape(-1, 3))
        out_lo = np.zeros(len(v), np.int16)
        out_obs = np.zeros(len(v), np.bool_)
        _lookup(v, self._keys, self._vals, self._lo, self._flag, out_lo, out_obs)
        return out_lo.astype(np.float64) * LO_UNIT, out_obs

    def surface_keys(self) -> np.ndarray:
        """0.5 m 表面体素：占据体素所在的粗格（打包键，升序）。"""
        return np.unique(pack_keys(np.floor_divide(self.occupied_voxels(), self.params.coarse)))

    def hit_surface_keys(self) -> np.ndarray:
        """入图命中点所在的 0.5 m 格（S_ref 的本机份额；打包键，升序）。"""
        self._fold_hits()
        return self._hit_surface.copy()

    # -------------------------------------------------------------- 合并与持久化
    def _block_arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        bk, sl = self._blocks()
        return bk, self._lo[sl], (self._flag[sl] & _OBS) != 0

    def _add(self, bk: np.ndarray, blo: np.ndarray, bobs: np.ndarray) -> None:
        _h, _m, lmin, lmax, _ = self.params.fixed()
        bk, blo, bobs = np.ascontiguousarray(bk, np.int64), np.ascontiguousarray(blo, np.int16), np.ascontiguousarray(bobs, np.bool_)
        i = 0
        while True:
            st, i, self._nblk = _add_blocks(i, bk, blo, bobs, self._keys, self._vals, self._lo, self._flag, self._nblk, lmin, lmax,
                                            self.max_blocks)
            if st == ST_DONE:
                break
            self._grow(st)

    def merge(self, other: L0Map, uav: str = "merged") -> L0Map:
        """log-odds 相加后钳位（独立观测融合）；参数须相同。命中参考格取并集。"""
        if other.params != self.params:
            raise ValueError("cannot merge maps with different parameters")
        self._check()
        other._check()
        out = L0Map(self.params, uav)
        out._add(*self._block_arrays())
        out._add(*other._block_arrays())
        out._hit_surface = np.union1d(self.hit_surface_keys(), other.hit_surface_keys())
        for k in out.counts:
            out.counts[k] = self.counts[k] + other.counts[k]
        return out

    def save(self, path: Path) -> Path:
        """`.npz`：块键、log-odds、观测位、命中参考格、参数与计数（逐位可复原）。"""
        self._check()
        bk, blo, bobs = self._block_arrays()
        meta = {"version": MAP_VERSION, "uav": self.uav, "params": asdict(self.params), "counts": self.counts,
                "t_range_ns": self.t_range_ns, "peak_nbytes": int(self.peak_nbytes)}
        path = Path(path)
        with path.open("wb") as fh:
            np.savez_compressed(fh, block_keys=bk, logodds=blo, observed=np.packbits(bobs, axis=1), hit_surface=self.hit_surface_keys(),
                                meta=np.frombuffer(json.dumps(meta, sort_keys=True).encode(), np.uint8))
        return path

    @classmethod
    def load(cls, path: Path, budget_bytes: int | None = None) -> L0Map:
        with np.load(Path(path)) as z:
            meta = json.loads(bytes(z["meta"]).decode())
            if meta.get("version") != MAP_VERSION:
                raise L0MapError(f"unsupported L0 map version {meta.get('version')!r}")
            params = L0Params(**meta["params"])
            if budget_bytes is not None:
                params = replace(params, budget_bytes=int(budget_bytes))
            m = cls(params, meta["uav"])
            m._add(z["block_keys"], z["logodds"], np.unpackbits(z["observed"], axis=1, count=BLOCK_VOX).astype(bool))
            m._hit_surface = z["hit_surface"].astype(np.int64)
        m.counts.update(meta["counts"])
        m.t_range_ns = meta["t_range_ns"]
        return m

    def stats(self) -> dict:
        """单机统计（JSON）：计数、块数、稀疏块存储（MB 与 MB/机·分钟，按 `STORAGE_BYTES_PER_BLOCK`）、内存容量与峰值字节、
        占据与观测体素数。"""
        n_occ = len(self.occupied_voxels())
        dur_s = (self.t_range_ns[1] - self.t_range_ns[0]) / 1e9 if self.t_range_ns else 0.0
        used_mb = self._nblk * STORAGE_BYTES_PER_BLOCK / 1e6
        return {"uav": self.uav, **self.counts, "blocks": int(self._nblk), "occupied_voxels": int(n_occ),
                "observed_voxels": self.observed_count(), "storage_mb": used_mb, "nbytes": int(self.nbytes),
                "peak_nbytes": int(self.peak_nbytes), "duration_s": dur_s,
                "storage_mb_per_min": used_mb / (dur_s / 60.0) if dur_s > 0 else None}


# ---------------------------------------------------------------------------------------------------------------- 覆盖与 Jaccard
def coverage_and_jaccard(surf: Mapping[str, np.ndarray], s_ref: np.ndarray | None = None) -> dict:
    """每机 0.5 m 表面体素（打包键）→ 覆盖率、两两 Jaccard（上三角与完整矩阵）、合并覆盖率。S_ref 缺省为各机表面体素的并集。"""
    names = sorted(surf)
    sets = {u: np.unique(np.asarray(surf[u], np.int64)) for u in names}
    union = np.unique(np.concatenate([sets[u] for u in names])) if names else np.empty(0, np.int64)
    ref = union if s_ref is None else np.unique(np.asarray(s_ref, np.int64))
    nref = max(len(ref), 1)
    cov = {u: float(len(np.intersect1d(sets[u], ref, assume_unique=True)) / nref) for u in names}
    jac: dict[str, float] = {}
    mat = np.eye(len(names))
    for (i, u), (j, w) in combinations(enumerate(names), 2):
        inter = len(np.intersect1d(sets[u], sets[w], assume_unique=True))
        uni = len(sets[u]) + len(sets[w]) - inter
        mat[i, j] = mat[j, i] = inter / uni if uni else 0.0
        jac[f"{u}|{w}"] = float(mat[i, j])
    merged = float(len(np.intersect1d(union, ref, assume_unique=True)) / nref)
    best = max(cov.values(), default=0.0)
    jmax = max(jac.values(), default=0.0)
    return {"uavs": names, "surface_voxels": {u: len(sets[u]) for u in names}, "s_ref_voxels": len(ref),
            "coverage": cov, "jaccard": jac, "jaccard_matrix": mat.tolist(), "jaccard_max": float(jmax),
            "merged_coverage": merged, "merged_over_best": merged / best if best > 0 else None,
            "jaccard_ok": bool(jmax <= JACCARD_MAX)}


# ---------------------------------------------------------------------------------------------------------------- 到网格距离
def mesh_distance(centers: np.ndarray, q, max_m: float = 5.0, chunk: int = 1 << 20) -> np.ndarray:
    """点到 GLB 网格的无符号距离（float32，超过 max_m 记 max_m）；`q` 为 `MeshWorldQuery`（`closest_batch`）。"""
    P = np.asarray(centers, np.float64).reshape(-1, 3)
    out = np.empty(len(P), np.float32)
    for i in range(0, len(P), chunk):
        out[i:i + chunk] = q.closest_batch(P[i:i + chunk], float(max_m))
    return out


def mesh_error_u8(d: np.ndarray) -> np.ndarray:
    """对比视图误差着色的量化：0–0.5 m、步长 2 mm、四舍五入，超出按 0.5 m（u8 0–250；反量化误差 ≤ 1 mm）。"""
    d = np.asarray(d, np.float64)
    return np.floor(np.clip(d, 0.0, ERR_CAP_M) / ERR_STEP_M + 0.5).astype(np.uint8)


def distance_stats(d: np.ndarray, voxel_m: float = 0.25) -> dict:
    d = np.asarray(d, np.float64)
    if len(d) == 0:
        return {"n": 0}
    hd = math.sqrt(3.0) / 2.0 * voxel_m
    return {"n": len(d), "mean_m": float(d.mean()), "median_m": float(np.median(d)), "p90_m": float(np.percentile(d, 90)),
            "p95_m": float(np.percentile(d, 95)), "max_m": float(d.max()), "within_half_diag": float((d <= hd + 1e-6).mean()),
            "half_diag_m": hd}


def fleet_report(maps: Mapping[str, L0Map], q=None, s_ref: np.ndarray | None = None, max_m: float = 5.0) -> dict:
    """机队 L0 结果（`eval_sim` 的 L0 段，JSON）：每机统计与到网格距离、覆盖、两两 Jaccard、合并覆盖。

    S_ref 缺省为全机队入图命中点所在的 0.5 m 格（§6.7.7）。`q` 给出时计算占据体素中心到网格的距离分布。"""
    names = sorted(maps)
    if not names:
        raise ValueError("no maps")
    p0 = maps[names[0]].params
    if any(maps[u].params != p0 for u in names):
        raise ValueError("maps use different parameters")
    ref = s_ref
    if ref is None:
        ref = np.unique(np.concatenate([maps[u].hit_surface_keys() for u in names]))
    per = {}
    for u in names:
        st = maps[u].stats()
        if q is not None:
            st["mesh_distance"] = distance_stats(mesh_distance(maps[u].occupied_centers(), q, max_m), p0.voxel_m)
        per[u] = st
    cj = coverage_and_jaccard({u: maps[u].surface_keys() for u in names}, ref)
    return {"version": MAP_VERSION, "params": asdict(p0), "s_ref": "fleet_hits" if s_ref is None else "given",
            "jaccard_threshold": JACCARD_MAX, "per_uav": per, **cj}
