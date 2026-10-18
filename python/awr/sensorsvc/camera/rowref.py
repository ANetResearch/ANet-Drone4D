"""卷帘验收参照（docs/05 §5.4.3 卷帘快门、ADR-179；M25 §15.4；D3-AC-60）：只用于验收与回归对拍，不进入在线路径。

行时刻：第 i 行曝光中点相对 t_mid 为 `(i − (H − 1)/2) · T_ro / H`（与 `timing.row_mid_ns` 同式，这里不取整到纳秒）；连续像素
纵坐标 v 处的行时刻为 `(v − H/2) · T_ro / H`（像素中心 v = i + 0.5，与 `raster.VS_RS` 的行号换算一致）。位姿函数 `pose_at(t)`
给出相对 t_mid 的时刻 t（秒）处的 WORLD←FLU 旋转与位置，是构造轨迹本身，不经读出窗口 9 节点插值。

- `RowRayReference`：Warp BVH 逐行射线求交。第 i 行的全部像素以该行精确时刻的相机位姿从像素中心 (j + 0.5, i + 0.5) 发射射线，
  输出平面深度、实例、语义与命中三角形（未命中：深度 0、实例 0、语义 255，与光栅化的天空约定相同）。三角形以局部原点（缺省为
  t_mid 相机位置）float32 存放；有 CUDA 设备时在 GPU 上求交，否则用 Warp 的 CPU 设备（同一内核）。
- `render_bands`：行带方法（`rs_method = bands`）参照实现：每条行带以行带中心行时刻的位姿做一次全局快门渲染，取该带的行拼接，
  与逐带裁剪绘制等价。量化误差上界为 T_ro / (2 · 带数) 的位姿时差（16 带 0.63 ms）。
- `project_rs`：已知世界点在卷帘图像中的连续像素位置，逐点求不动点 v = proj_v(X, pose(t(v)))（收缩因子为竖直像素速度 × 行间隔，
  构造轨迹上 < 0.05，迭代到 1e-9 px）。
- 标记度量：`label_stats`（每个标记的面积、质心与内边界像素数）、`centroid_errors`（两幅标记图逐标记质心距离）、
  `edge_offsets`（两幅标记图逐标记的平均边缘偏移 = 对称差像素数 / 内边界像素数；内边界按 4 邻域判定，像素数不超过边界的欧氏长度，
  因此估计值不小于真实的平均偏移）。

依赖：numpy；`RowRayReference` 需要 warp-lang（d3-sensorsvc 镜像离线安装，`tools/deploy/dmax/images/sensorsvc/requirements-warp.lock`）。
"""

from __future__ import annotations

import functools
import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from awr.sensorsvc.camera.model import CameraModel
from awr.sensorsvc.camera.proj import R_FLU_OPT, world_to_opt

__all__ = ["RS_BANDS_REF", "PoseFn", "RefOut", "RowRayReference", "actor_triangles", "centroid_errors", "dilate_labels", "edge_offsets",
           "label_stats", "plane_depth", "project_rs", "render_bands", "row_times_s", "rs_nodes", "scene_triangles"]

RS_BANDS_REF = 16                       # D3-AC-60 行带方法的带数
SEM_SKY = 255
PoseFn = Callable[[float], tuple[np.ndarray, np.ndarray]]


def row_times_s(cm: CameraModel, readout_s: float) -> np.ndarray:
    """(H,) float64：各行曝光中点相对 t_mid 的时刻（秒）。"""
    H = cm.height
    return (np.arange(H, dtype=np.float64) - (H - 1) / 2) * readout_s / H


def rs_nodes(pose_at: PoseFn, readout_s: float, n: int = 9) -> list[tuple[np.ndarray, np.ndarray]]:
    """读出窗口 [−T_ro/2, T_ro/2] 内 n 个等间隔位姿节点（`RasterCamera.render(..., rs_nodes=)` 的输入，与 capture 同式）。"""
    return [pose_at(-readout_s / 2 + k * readout_s / (n - 1)) for k in range(n)]


def _rot_z(yaw: float) -> np.ndarray:
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def scene_triangles(scene) -> np.ndarray:
    """`AppearanceScene` 的全部三角形 → (n, 3, 3) float64 world ENU（静态场景：实例 0、语义 0，与光栅化相同）。"""
    if not scene.batches:
        return np.zeros((0, 3, 3))
    return np.concatenate([b.pos.astype(np.float64)[b.idx.astype(np.int64)] + b.origin for b in scene.batches])


def actor_triangles(actors: list[dict], idx: np.ndarray, pos: np.ndarray, yaw: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """演员代理网格（`actors.proxy_mesh`，与光栅化同一几何）按一帧位姿变换到世界：返回 (tris (n, 3, 3), 实例 idx + 1, 语义码)。"""
    from awr.sensorsvc.camera.actors import SEM_CODE, proxy_mesh

    tris, inst, sem = [], [], []
    for i, p, a in zip(idx, pos, yaw, strict=True):
        P, _N, _C = proxy_mesh(actors[int(i)])
        T = P.astype(np.float64).reshape(-1, 3, 3) @ _rot_z(float(a)).T + np.asarray(p, np.float64)
        tris.append(T)
        inst.append(np.full(len(T), int(i) + 1, np.uint16))
        sem.append(np.full(len(T), SEM_CODE.get(actors[int(i)]["class"], 13), np.uint8))
    if not tris:
        return np.zeros((0, 3, 3)), np.zeros(0, np.uint16), np.zeros(0, np.uint8)
    return np.concatenate(tris), np.concatenate(inst), np.concatenate(sem)


def _pixel_dirs(cm: CameraModel) -> tuple[np.ndarray, np.ndarray]:
    """像素中心光学帧方向（z = 1）的 x、y 分量：(W,)、(H,)。"""
    return (np.arange(cm.width) + 0.5 - cm.cx) / cm.fx, (np.arange(cm.height) + 0.5 - cm.cy) / cm.fy


def plane_depth(cm: CameraModel, pose_at: PoseFn, readout_s: float, z0: float = 0.0) -> np.ndarray:
    """逐行精确位姿下水平面 z = z0 的平面深度解析值 (H, W)（射线朝上或平面在后方时为 0），用于校验参照的行时刻与深度定义。"""
    xs, ys = _pixel_dirs(cm)
    out = np.zeros((cm.height, cm.width))
    for i, t in enumerate(row_times_s(cm, readout_s)):
        R, p = pose_at(float(t))
        Rwo = np.asarray(R, np.float64) @ R_FLU_OPT
        dz = Rwo[2, 0] * xs + Rwo[2, 1] * ys[i] + Rwo[2, 2]               # d_opt = (x, y, 1)，平面深度即射线参数
        with np.errstate(divide="ignore", invalid="ignore"):
            d = (z0 - float(p[2])) / dz
        out[i] = np.where(np.isfinite(d) & (d > 0), d, 0.0)
    return out


def project_rs(cm: CameraModel, pose_at: PoseFn, readout_s: float, X: np.ndarray, *, tol_px: float = 1e-9,
               max_iter: int = 60) -> tuple[np.ndarray, np.ndarray]:
    """卷帘图像中世界点的连续像素位置：返回 (uv (n, 2)，z (n,) 该时刻平面深度)。不收敛（收缩因子 ≥ 1）时抛 ValueError。"""
    X = np.asarray(X, np.float64).reshape(-1, 3)
    H = cm.height
    uv = np.zeros((len(X), 2))
    z = np.zeros(len(X))
    for k, x in enumerate(X):
        v = H / 2
        for _ in range(max_iter):
            M = world_to_opt(*pose_at((v - H / 2) * readout_s / H))
            q = M[:3, :3] @ x + M[:3, 3]
            v_new = cm.fy * q[1] / q[2] + cm.cy
            done = abs(v_new - v) <= tol_px
            v = v_new
            if done:
                break
        else:
            raise ValueError(f"rolling-shutter projection did not converge for point {k}")
        M = world_to_opt(*pose_at((v - H / 2) * readout_s / H))
        q = M[:3, :3] @ x + M[:3, 3]
        uv[k] = (cm.fx * q[0] / q[2] + cm.cx, cm.fy * q[1] / q[2] + cm.cy)
        z[k] = q[2]
    return uv, z


@dataclass
class RefOut:
    depth_m: np.ndarray         # (H, W) float32 平面深度，未命中 0
    instance: np.ndarray        # (H, W) uint16
    semantic: np.ndarray        # (H, W) uint8，未命中 255
    face: np.ndarray            # (H, W) int32 命中三角形序号，未命中 −1
    device: str
    trace_ms: float


@functools.cache
def _kernel():
    import warp as wp

    @wp.kernel
    def rowray(mesh: wp.uint64, rwo: wp.array(dtype=wp.mat33), org: wp.array(dtype=wp.vec3), K: wp.vec4, W: int, max_t: float,
               depth: wp.array(dtype=float), face: wp.array(dtype=int)):
        tid = wp.tid()
        i = tid // W
        j = tid - i * W
        d = rwo[i] * wp.vec3((float(j) + 0.5 - K[2]) / K[0], (float(i) + 0.5 - K[3]) / K[1], 1.0)
        n = wp.length(d)
        q = wp.mesh_query_ray(mesh, org[i], d / n, max_t)
        if q.result:
            depth[tid] = q.t / n                                         # d_opt.z = 1：平面深度 = 射线长度 / |d|
            face[tid] = q.face
        else:
            depth[tid] = 0.0
            face[tid] = -1

    return rowray


class RowRayReference:
    """Warp 逐行射线参照：三角形、逐三角形实例与语义常驻设备，`render` 按构造轨迹逐行求交。"""

    def __init__(self, tris_w: np.ndarray, inst: np.ndarray, sem: np.ndarray, *, origin: np.ndarray, device: str | None = None) -> None:
        import warp as wp

        wp.config.quiet = True
        wp.init()
        self.wp = wp
        self.device = device or ("cuda:0" if wp.get_cuda_device_count() > 0 else "cpu")
        T = np.asarray(tris_w, np.float64).reshape(-1, 3, 3)
        if len(T) == 0 or len(inst) != len(T) or len(sem) != len(T):
            raise ValueError("need ≥ 1 triangle and one instance / semantic code per triangle")
        self.origin = np.asarray(origin, np.float64)
        pts = (T.reshape(-1, 3) - self.origin).astype(np.float32)
        self._pts = wp.array(pts, dtype=wp.vec3, device=self.device)
        self._idx = wp.array(np.arange(len(pts), dtype=np.int32), dtype=int, device=self.device)
        self.mesh = wp.Mesh(points=self._pts, indices=self._idx)
        self.inst_tab = np.r_[np.asarray(inst, np.uint16), np.uint16(0)]          # 末项：未命中
        self.sem_tab = np.r_[np.asarray(sem, np.uint8), np.uint8(SEM_SKY)]
        self.triangles = len(T)

    @staticmethod
    def from_scene(scene, actors: list[dict] | None = None, frame=None, *, origin: np.ndarray, device: str | None = None) -> RowRayReference:
        """静态外观场景（实例 0、语义 0）加一帧演员（`raster.ActorFrame`，代理网格与光栅化相同）。"""
        T = [scene_triangles(scene)]
        inst = [np.zeros(len(T[0]), np.uint16)]
        sem = [np.zeros(len(T[0]), np.uint8)]
        if actors and frame is not None and len(frame.idx):
            Ta, ia, sa = actor_triangles(actors, frame.idx, frame.pos, frame.yaw)
            T.append(Ta)
            inst.append(ia)
            sem.append(sa)
        return RowRayReference(np.concatenate(T), np.concatenate(inst), np.concatenate(sem), origin=origin, device=device)

    def render(self, cm: CameraModel, pose_at: PoseFn, readout_s: float) -> RefOut:
        """第 i 行取 `pose_at(row_times_s[i])`；`readout_s = 0` 时为全局快门参照。深度超出 [near, far] 的命中按未命中处理。"""
        import time

        wp = self.wp
        W, H = cm.width, cm.height
        rwo = np.zeros((H, 3, 3), np.float32)
        org = np.zeros((H, 3), np.float32)
        for i, t in enumerate(row_times_s(cm, readout_s)):
            R, p = pose_at(float(t))
            rwo[i] = np.asarray(R, np.float64) @ R_FLU_OPT
            org[i] = np.asarray(p, np.float64) - self.origin
        depth = wp.zeros(W * H, dtype=float, device=self.device)
        face = wp.zeros(W * H, dtype=int, device=self.device)
        t0 = time.perf_counter()
        wp.launch(_kernel(), dim=W * H, device=self.device,
                  inputs=[self.mesh.id, wp.array(rwo, dtype=wp.mat33, device=self.device), wp.array(org, dtype=wp.vec3, device=self.device),
                          wp.vec4(cm.fx, cm.fy, cm.cx, cm.cy), W, float(cm.far_m) * 4.0, depth, face])
        wp.synchronize_device(self.device)
        ms = (time.perf_counter() - t0) * 1e3
        z = depth.numpy().reshape(H, W)
        f = face.numpy().reshape(H, W).astype(np.int32)
        f[(z < cm.near_m) | (z > cm.far_m)] = -1
        z = np.where(f >= 0, z, 0.0).astype(np.float32)
        return RefOut(z, self.inst_tab[f], self.sem_tab[f], f, self.device, ms)


def render_bands(cam, cm: CameraModel, pose_at: PoseFn, readout_s: float, *, bands: int = RS_BANDS_REF, actors=None):
    """行带方法：第 b 带为行 [⌊b·H/n⌋, ⌊(b+1)·H/n⌋)，位姿取带中心行时刻；返回拼接后的 `RenderOut`（各带耗时累加）。"""
    from awr.sensorsvc.camera.raster import RenderOut

    H = cm.height
    t_row = row_times_s(cm, readout_s)
    acc: RenderOut | None = None
    for b in range(bands):
        r0, r1 = b * H // bands, (b + 1) * H // bands
        tc = 0.5 * (t_row[r0] + t_row[r1 - 1])
        r = cam.render(cm, *pose_at(float(tc)), actors=actors)
        if acc is None:
            acc = r
            continue
        for k in ("rgb8", "depth_m", "instance", "semantic", "instance_unocc"):
            a = getattr(acc, k)
            if a is not None:
                a[r0:r1] = getattr(r, k)[r0:r1]
        acc.draw_ms += r.draw_ms
        acc.readback_ms += r.readback_ms
        acc.batches_drawn += r.batches_drawn
    assert acc is not None
    return acc


# ---------------------------------------------------------------- 标记度量


def label_stats(lab: np.ndarray, n: int) -> dict[str, np.ndarray]:
    """标记图（0 为背景，1..n−1 为标记号）→ 每个标记的 area、质心 cx、cy（像素中心 0.5 约定）、内边界像素数 boundary、
    是否触及图像边缘 border。"""
    H, W = lab.shape
    L = lab.astype(np.int64)
    flat = L.ravel()
    area = np.bincount(flat, minlength=n)[:n].astype(np.float64)
    yy, xx = np.divmod(np.arange(H * W), W)
    with np.errstate(divide="ignore", invalid="ignore"):
        cx = np.bincount(flat, weights=xx + 0.5, minlength=n)[:n] / area
        cy = np.bincount(flat, weights=yy + 0.5, minlength=n)[:n] / area
    P = np.pad(L, 1, constant_values=-1)
    inner = ((P[1:-1, 1:-1] != P[:-2, 1:-1]) | (P[1:-1, 1:-1] != P[2:, 1:-1]) | (P[1:-1, 1:-1] != P[1:-1, :-2])
             | (P[1:-1, 1:-1] != P[1:-1, 2:]))
    boundary = np.bincount(flat[inner.ravel()], minlength=n)[:n].astype(np.float64)
    edge = np.r_[L[0], L[-1], L[:, 0], L[:, -1]]
    border = np.bincount(edge, minlength=n)[:n] > 0
    return {"area": area, "cx": cx, "cy": cy, "boundary": boundary, "border": border}


def centroid_errors(lab_a: np.ndarray, lab_b: np.ndarray, ids: np.ndarray) -> np.ndarray:
    """两幅标记图中各标记质心的欧氏距离（px）。"""
    n = int(max(lab_a.max(), lab_b.max(), ids.max() if len(ids) else 0)) + 1
    a, b = label_stats(lab_a, n), label_stats(lab_b, n)
    return np.hypot(a["cx"][ids] - b["cx"][ids], a["cy"][ids] - b["cy"][ids])


def edge_offsets(lab_ref: np.ndarray, lab: np.ndarray, ids: np.ndarray) -> np.ndarray:
    """逐标记平均边缘偏移（px）：|A_ref Δ A| / A_ref 内边界像素数。"""
    n = int(max(lab_ref.max(), lab.max(), ids.max() if len(ids) else 0)) + 1
    m = (lab_ref != lab).ravel()
    sym = (np.bincount(lab_ref.ravel()[m].astype(np.int64), minlength=n)[:n]
           + np.bincount(lab.ravel()[m].astype(np.int64), minlength=n)[:n]).astype(np.float64)
    bd = label_stats(lab_ref, n)["boundary"]
    with np.errstate(divide="ignore", invalid="ignore"):
        return sym[ids] / bd[ids]


def dilate_labels(lab: np.ndarray, r: int) -> np.ndarray:
    """标记图按 8 邻域膨胀 r 像素（只填背景；相邻标记的膨胀区重叠时取较大标记号）。用于把各通道的二值掩膜归属到参照标记。"""
    out = lab.copy()
    for _ in range(r):
        P = np.pad(out, 1)
        nb = np.max(np.stack([P[1 + dy:P.shape[0] - 1 + dy, 1 + dx:P.shape[1] - 1 + dx]
                              for dy in (-1, 0, 1) for dx in (-1, 0, 1)]), axis=0)
        out = np.where(out > 0, out, nb)
    return out
