"""地图指标（M27-FR-106、§6.7.7；r1 §4.4 `eval_sim.py` 的 `map_metrics`；D3-AC-14 地图到网格距离、D3-AC-15 L0 地图）。

- 地图点：FAST-LIVO2 退出时写出的 `Log/pcd/all_raw_points.pcd`（F 系），经锚定变换 `T_W_F` 到世界系后做 0.1 m 体素降采样
  （Open3D `voxel_down_sample`，哈希体素，不受 PCL VoxelGrid int32 溢出影响，r1 §2.10-2）；对齐坐标再乘以轨迹 Umeyama 变换。
- 精度（地图到网格距离）：降采样点到 GLB 网格的无符号距离（Open3D `RaycastingScene.compute_distance`；顶点减去局部原点后以 float32
  求交，与 M22 瓦片局部原点约定相同）；世界包目录经 `MeshWorldQuery.closest_batch`。`acc_mean_m` 即 D3-AC-14 的"地图到网格距离均值"。
- 完整度：参考集（"这次飞行可观测的表面"）到地图的最近距离 < τ 的比例；参考集缺省取算例的 `gt_lidar_world.ply`，没有时由同一 bag
  按真值逐点位姿积分 LiDAR 回波（B1，0.2 m 体素）。F@τ = 2PR / (P + R)，τ ∈ {0.1, 0.2, 0.5} m；Chamfer 为两向均值之和（另给 1 m 截断）。
- 颜色 MAE 与 PSNR 需要网格纹理采样，本版不计算（见 SL1 遗留）。
- L0 离线地图：同一 bag、真值逐点位姿，经 `awr.bench.eval.lidar_map.L0Map` 积分（体素 0.25 m、深度上限 60 m、预算 8 GiB）。
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np

from .traj import R_to_quat, TrajInterp, quat_to_R

__all__ = [
    "TriMeshDistance",
    "WorldMeshDistance",
    "bag_lidar_frames",
    "gt_lidar_reference",
    "half_diag",
    "l0_from_bag",
    "map_metrics",
    "open_mesh",
    "read_cloud",
    "transform_points",
    "voxel_down",
]

TAUS = (0.1, 0.2, 0.5)
VOX_EVAL = 0.1
VOX_REF = 0.2


def _o3d():
    import open3d as o3d  # 评测容器（d3-sensorsvc）或 .venv-d3

    return o3d


def read_cloud(path: Path | str) -> tuple[np.ndarray, np.ndarray | None]:
    """点云文件（PCD/PLY）→ (P (n, 3) float64, C (n, 3) 0–1 或 None)。"""
    o3d = _o3d()
    pc = o3d.io.read_point_cloud(str(path))
    P = np.asarray(pc.points, np.float64)
    C = np.asarray(pc.colors, np.float64) if pc.has_colors() else None
    return P, C


def transform_points(P: np.ndarray, T: np.ndarray) -> np.ndarray:
    T = np.asarray(T, np.float64)
    return np.asarray(P, np.float64) @ T[:3, :3].T + T[:3, 3]


def voxel_down(P: np.ndarray, C: np.ndarray | None, voxel: float) -> tuple[np.ndarray, np.ndarray | None]:
    """体素降采样（Open3D 哈希体素，体素内均值），与 r1 `pcd.voxel_down_sample` 同口径。"""
    o3d = _o3d()
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.asarray(P, np.float64)))
    if C is not None:
        pc.colors = o3d.utility.Vector3dVector(np.asarray(C, np.float64))
    ds = pc.voxel_down_sample(float(voxel))
    return np.asarray(ds.points, np.float64), (np.asarray(ds.colors, np.float64) if C is not None else None)


# ---------------------------------------------------------------------------------------------------------------- 网格
class TriMeshDistance:
    """三角网格文件（PLY / OBJ / GLB，世界系 ENU，米）的无符号距离查询；`transform` 为可选的 4 × 4 网格→世界变换。

    顶点减去局部原点（缺省为包围盒中心取整到米）后以 float32 建 RaycastingScene，查询点同样平移后求距离（M22 瓦片局部原点约定）。"""

    def __init__(self, path: Path | str, transform: np.ndarray | None = None, origin=None) -> None:
        o3d = _o3d()
        self.path = str(path)
        m = o3d.io.read_triangle_mesh(self.path)
        V = np.asarray(m.vertices, np.float64)
        F = np.asarray(m.triangles, np.int64)
        if len(F) == 0:
            raise ValueError(f"{path}: mesh has no triangles")
        if transform is not None:
            V = transform_points(V, transform)
        self.origin = (np.round((V.min(0) + V.max(0)) / 2.0) if origin is None else np.asarray(origin, np.float64)).astype(np.float64)
        self.n_tri = len(F)
        self.scene = o3d.t.geometry.RaycastingScene()
        self.scene.add_triangles(o3d.core.Tensor((V - self.origin).astype(np.float32)), o3d.core.Tensor(F.astype(np.uint32)))

    def distance(self, P: np.ndarray, chunk: int = 1 << 21) -> np.ndarray:
        o3d = _o3d()
        P = np.asarray(P, np.float64).reshape(-1, 3)
        out = np.empty(len(P), np.float32)
        for i in range(0, len(P), chunk):
            q = o3d.core.Tensor((P[i:i + chunk] - self.origin).astype(np.float32))
            out[i:i + chunk] = self.scene.compute_distance(q).numpy()
        return out

    def closest_batch(self, P: np.ndarray, max_m: float) -> np.ndarray:
        """与 `MeshWorldQuery.closest_batch` 同签名（超过 max_m 记 max_m），供 `lidar_map.fleet_report` 使用。"""
        return np.minimum(self.distance(P), np.float32(max_m))

    def info(self) -> dict:
        return {"kind": "mesh_file", "path": self.path, "n_tri": self.n_tri, "origin": self.origin.tolist()}


class WorldMeshDistance:
    """世界包（M22，`mesh.raycast`）的无符号距离：`MeshWorldQuery.closest_batch`（瓦片局部原点 float32）。"""

    def __init__(self, world_dir: Path | str, max_m: float = 50.0) -> None:
        from awr.world.mesh.query import open_mesh_world_query

        self.world_dir = str(world_dir)
        self.max_m = float(max_m)
        self.q = open_mesh_world_query(Path(world_dir))

    def distance(self, P: np.ndarray, chunk: int = 1 << 20) -> np.ndarray:
        P = np.asarray(P, np.float64).reshape(-1, 3)
        out = np.empty(len(P), np.float32)
        for i in range(0, len(P), chunk):
            out[i:i + chunk] = self.q.closest_batch(P[i:i + chunk], self.max_m)
        return out

    def closest_batch(self, P: np.ndarray, max_m: float) -> np.ndarray:
        return self.q.closest_batch(np.asarray(P, np.float64).reshape(-1, 3), float(max_m))

    def info(self) -> dict:
        return {"kind": "world_package", "path": self.world_dir, "max_m": self.max_m}


def open_mesh(spec: str | Path, transform: np.ndarray | None = None):
    """目录 → 世界包；文件 → 三角网格。"""
    p = Path(spec)
    if p.is_dir():
        return WorldMeshDistance(p)
    return TriMeshDistance(p, transform)


# ---------------------------------------------------------------------------------------------------------------- 指标
def map_metrics(P: np.ndarray, mesh, ref: np.ndarray | None, taus=TAUS) -> dict:
    """地图点 P（世界系，已降采样）的精度、完整度、F-score 与 Chamfer（r1 `map_metrics` 同键名，颜色项除外）。"""
    o3d = _o3d()
    P = np.asarray(P, np.float64).reshape(-1, 3)
    out: dict = {"n_points": len(P)}
    if len(P) == 0:
        return out
    t0 = time.perf_counter()
    d = mesh.distance(P).astype(np.float64)
    out.update({"acc_mean_m": float(d.mean()), "acc_median_m": float(np.median(d)), "acc_rmse_m": float(np.sqrt((d ** 2).mean())),
                "acc_p95_m": float(np.percentile(d, 95)), "outlier_frac_gt0.5m": float((d > 0.5).mean())})
    for tau in taus:
        out[f"precision@{tau}"] = float((d < tau).mean())
    if ref is not None and len(ref):
        pm = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
        pr = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.asarray(ref, np.float64)))
        dc = np.asarray(pr.compute_point_cloud_distance(pm))
        out.update({"ref_points": len(ref), "comp_mean_m": float(dc.mean()), "comp_median_m": float(np.median(dc))})
        for tau in taus:
            r, p_ = float((dc < tau).mean()), out[f"precision@{tau}"]
            out[f"completeness@{tau}"] = r
            out[f"fscore@{tau}"] = float(2 * p_ * r / max(p_ + r, 1e-9))
        out["chamfer_m"] = out["acc_mean_m"] + out["comp_mean_m"]
        out["chamfer_trunc1m_m"] = float(np.minimum(d, 1.0).mean() + np.minimum(dc, 1.0).mean())
    out["eval_s"] = time.perf_counter() - t0
    return out


# ---------------------------------------------------------------------------------------------------------------- 真值位姿 LiDAR
def bag_lidar_frames(bag: Path | str):
    """逐帧 (timebase_ns, CustomPoint 数组)；bag 由 `awr.sensorsvc.slam.bag.Bag1Reader` 读取（none / bz2 chunk）。"""
    from awr.sensorsvc.slam.bag import LIDAR_TOPIC, Bag1Reader, unpack_custom_msg

    with Bag1Reader(bag) as rd:
        for m in rd.messages([LIDAR_TOPIC]):
            msg = unpack_custom_msg(m.data)
            yield int(msg["timebase"]), np.array(msg["points"])


def _lidar_world_points(tb_ns: int, pts: np.ndarray, gt: TrajInterp, T_I_L: np.ndarray, min_range: float) -> np.ndarray:
    xyz = np.stack([pts["x"], pts["y"], pts["z"]], 1).astype(np.float64)
    keep = (xyz ** 2).sum(1) > min_range ** 2                               # 无回波零点占位
    if not keep.any():
        return np.empty((0, 3))
    ti = (tb_ns + pts["offset_time"][keep].astype(np.int64)) * 1e-9
    Ri, pi = gt.at(ti)
    pI = xyz[keep] @ T_I_L[:3, :3].T + T_I_L[:3, 3]
    return np.einsum("nij,nj->ni", Ri, pI) + pi


def gt_lidar_reference(bag: Path | str, gt: TrajInterp, T_I_L: np.ndarray, voxel: float = VOX_REF, min_range: float = 0.1) -> tuple[
        np.ndarray, int]:
    """B1：同一 bag 的 LiDAR 回波按真值逐点位姿变换到世界系后降采样（参考集与"已知位姿建图"上限）；返回 (点, 原始点数)。"""
    lo, hi = gt.span
    parts, n_raw = [], 0
    for tb, pts in bag_lidar_frames(bag):
        if tb * 1e-9 < lo or tb * 1e-9 + 0.1 > hi:
            continue
        w = _lidar_world_points(tb, pts, gt, np.asarray(T_I_L, np.float64), min_range)
        n_raw += len(w)
        if len(w):
            parts.append(w)
        if sum(len(p) for p in parts) > 8_000_000:                       # 分批降采样，限制峰值内存
            P, _ = voxel_down(np.concatenate(parts), None, voxel)
            parts = [P]
    if not parts:
        return np.empty((0, 3)), 0
    P, _ = voxel_down(np.concatenate(parts), None, voxel)
    return P, n_raw


def l0_from_bag(bag: Path | str, gt: TrajInterp, T_I_L: np.ndarray, uav: str = "", params=None):
    """L0 离线已知位姿地图（M27-FR-107）：真值 LiDAR 轨迹 `T_W_L = T_W_I · T_I_L`，逐点位姿积分；超预算抛 684。"""
    from awr.bench.eval.lidar_map import L0Map, L0Params, SensorTrajectory

    T_I_L = np.asarray(T_I_L, np.float64)
    R_W_I = quat_to_R(gt.q)
    p_L = gt.p + np.einsum("nij,j->ni", R_W_I, T_I_L[:3, 3])
    q_L = R_to_quat(R_W_I @ T_I_L[:3, :3])
    for k in range(1, len(q_L)):
        if q_L[k] @ q_L[k - 1] < 0:
            q_L[k] = -q_L[k]
    t_ns = np.round(gt.t * 1e9).astype(np.int64)
    traj = SensorTrajectory(t_ns, p_L, q_L)
    lo, hi = traj.span_ns
    m = L0Map(params or L0Params(), uav=uav)
    for tb, pts in bag_lidar_frames(bag):
        if tb < lo or tb + 100_000_000 > hi:
            continue
        m.integrate_frame(tb, pts, traj)
    return m


def half_diag(voxel: float) -> float:
    return math.sqrt(3.0) / 2.0 * voxel
