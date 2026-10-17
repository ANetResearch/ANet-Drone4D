"""世界帧合成、原点与锚点（M22 §6.6.3、§6.6.5；← r2 worlds_cfg.py、e04_crop_frame.py、e16_anchor.py）。

线性部分 `A = R_level · Rz(yaw) · s · R_ENU_GLTF`（Python float 标量合成，M03 normalize）；共帧继承（`inherit-frame`）时再右乘
本场景到参照场景的相对相似变换 `S_rel = M_ref_root · M_scene_root⁻¹`（两份 GLB 共用同一 OBJ 局部帧，场景坐标经各自根节点矩阵
得到）。点在 OSM 局部 ENU 中为 `P_osm = A·v + R_level·(tE, tN, 0)`；世界原点 XY 取 `P_osm` 包围盒中心，Z 取 10 m DTM（M03
`dtm_opening`）覆盖格的中位数；`T_world_source = [[A, R_level·(tE, tN, 0) − (cx, cy, z0)], [0, 0, 0, 1]]`。锚点经纬度以 OSM 参照点
为原点的局部 ENU 经 M02 `world_to_lla` 换算（唯一实现）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from awr.world.georef.frames import world_to_lla
from awr.world.georef.types import Anchor
from awr.world.glb.reader import Mat4, Scene
from awr.world.ingest.normalize import Mat3, apply_linear, compose3, exact_rz, rodrigues, scale3
from awr.world.terrain.dtm import dtm_opening

__all__ = ["R_ENU_GLTF", "WorldFrame", "frame_of_relative", "level_matrix", "linear_part", "synthesize"]

R_ENU_GLTF: Mat3 = ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0))     # E = x, N = −z, U = y
DTM_CELL_M = 10.0


def level_matrix(g: tuple[float, float]) -> Mat3:
    """把模型地面法线 (gE, gN, 1) 旋到 +Z（M22 §6.6.4）；g = (0, 0) 时为单位阵。"""
    return rodrigues((float(g[0]), float(g[1]), 1.0), (0.0, 0.0, 1.0))


def _mat3(M: Mat4) -> Mat3:
    return tuple(tuple(float(M[r][c]) for c in range(3)) for r in range(3))  # type: ignore[return-value]


def _inv_sim4(M: Mat4) -> Mat4:
    """相似变换（正交 × 均匀尺度 + 平移）的逆，Python float。"""
    A = _mat3(M)
    s2 = sum(A[r][0] ** 2 for r in range(3))
    inv = tuple(tuple(A[c][r] / s2 for c in range(3)) for r in range(3))
    t = (M[0][3], M[1][3], M[2][3])
    ti = tuple(-sum(inv[r][k] * t[k] for k in range(3)) for r in range(3))
    return (*((*inv[r], ti[r]) for r in range(3)), (0.0, 0.0, 0.0, 1.0))  # type: ignore[return-value]


def _mul4(A: Mat4, B: Mat4) -> Mat4:
    return tuple(tuple(sum(A[i][k] * B[k][j] for k in range(4)) for j in range(4)) for i in range(4))  # type: ignore[return-value]


def frame_of_relative(scene: Scene, ref: Scene) -> Mat4:
    """本场景坐标 → 参照场景坐标：`M_ref_root · M_scene_root⁻¹`（各取首个根节点的累乘矩阵）。"""
    return _mul4(ref.nodes[0][1], _inv_sim4(scene.nodes[0][1]))


def linear_part(s: float, yaw_deg: float, g: tuple[float, float], rel: Mat4 | None = None) -> tuple[Mat3, Mat3, tuple[float, float, float]]:
    """返回 (A, R_level, A 作用前的相对平移)：`P_osm = A·v + A_rel_t + R_level·(tE, tN, 0)`。"""
    R_level = level_matrix(g)
    A0 = compose3(R_level, exact_rz(yaw_deg), scale3(s, R_ENU_GLTF))
    if rel is None:
        return A0, R_level, (0.0, 0.0, 0.0)
    A = compose3(A0, _mat3(rel))
    tr = (rel[0][3], rel[1][3], rel[2][3])
    t = tuple(sum(A0[r][k] * tr[k] for k in range(3)) for r in range(3))
    return A, R_level, t  # type: ignore[return-value]


@dataclass(frozen=True)
class WorldFrame:
    A: Mat3
    t: tuple[float, float, float]
    origin_osm_m: tuple[float, float, float]        # 世界原点在 OSM 局部 ENU 中的位置 (cx, cy, z0)
    bounds_m: tuple[tuple[float, float, float], tuple[float, float, float]]
    lat_deg: float
    lon_deg: float
    m_per_unit: float
    leveled_deg: float

    @property
    def T_world_source(self) -> list[list[float]]:
        return [[*self.A[r], self.t[r]] for r in range(3)] + [[0.0, 0.0, 0.0, 1.0]]

    def apply(self, V: np.ndarray) -> np.ndarray:
        return apply_linear(self.A, V) + np.asarray(self.t)

    def to_json(self) -> dict[str, Any]:
        return {"T_world_source": [[round(v, 12) for v in row] for row in self.T_world_source],
                "origin_osm_m": [round(v, 6) for v in self.origin_osm_m],
                "bounds_m": {"min": [round(v, 3) for v in self.bounds_m[0]], "max": [round(v, 3) for v in self.bounds_m[1]]},
                "anchor": {"lat_deg": round(self.lat_deg, 7), "lon_deg": round(self.lon_deg, 7)},
                "m_per_unit": self.m_per_unit, "leveled_deg": round(self.leveled_deg, 4)}


def synthesize(V_gltf: np.ndarray, *, s: float, yaw_deg: float, g: tuple[float, float], t_osm_m: tuple[float, float],
               osm_ref: tuple[float, float], rel: Mat4 | None = None) -> WorldFrame:
    """M22 §6.6.5：由配准量合成世界帧。V_gltf 为本场景 glTF 场景帧顶点（float64）。"""
    A, R_level, t_rel = linear_part(s, yaw_deg, g, rel)
    tl = tuple(R_level[r][0] * t_osm_m[0] + R_level[r][1] * t_osm_m[1] for r in range(3))
    off = np.array([t_rel[r] + tl[r] for r in range(3)])
    P = apply_linear(A, V_gltf) + off
    lo, hi = P[:, :2].min(0), P[:, :2].max(0)
    cx, cy = float((lo[0] + hi[0]) / 2), float((lo[1] + hi[1]) / 2)
    Q = P - np.array([cx, cy, 0.0])
    dtm, _ = dtm_opening(Q, cell=DTM_CELL_M)
    z0 = float(np.nanmedian(dtm))
    t = (float(off[0] - cx), float(off[1] - cy), float(off[2] - z0))
    W = P - np.array([cx, cy, z0])
    anchor = Anchor(kind="synthetic", datum="WGS84", lat_deg=float(osm_ref[0]), lon_deg=float(osm_ref[1]), h_ellipsoid_m=0.0)
    lat, lon, _h = world_to_lla(np.array([[cx, cy, 0.0]]), anchor)
    s_eff = abs(np.linalg.det(np.array(A))) ** (1 / 3)
    return WorldFrame(A, t, (cx, cy, z0), (tuple(W.min(0).tolist()), tuple(W.max(0).tolist())),  # type: ignore[arg-type]
                      float(np.ravel(lat)[0]), float(np.ravel(lon)[0]), float(s_eff), math.degrees(math.atan(math.hypot(*g))))
