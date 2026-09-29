"""示意锚点求解（M03-FR-016；g03 §7）：以最高 HAG 点对应的已知地标反推原点经纬度。

牛顿迭代，残差由 M02 `lla_to_world` 计算，雅可比用 ±1e-6° 中心差分；|残差| < 1 mm 收敛，最多 8 次。
本模块不含任何椭球常量或帧换算公式（M02-FR-012；AWR-03 §5.1 第 8 条）。
"""

from __future__ import annotations

import numpy as np

from awr.world.georef.frames import Anchor, T_ecef_world, lla_to_world

from .types import Landmark

H_STEP_DEG = 1e-6


def _landmark_xy(lat_l: float, lon_l: float, lat0: float, lon0: float) -> np.ndarray:
    a = Anchor(kind="synthetic", datum="WGS84", lat_deg=lat0, lon_deg=lon0, h_ellipsoid_m=0.0, h_msl_m=0.0)
    return np.asarray(lla_to_world(lat_l, lon_l, 0.0, a), np.float64)[:2]


def solve_origin_latlon(landmark: Landmark, target_xy: tuple[float, float], max_iter: int = 8, tol_m: float = 1e-3
                        ) -> tuple[float, float, int]:
    """求 (lat0, lon0) 使地标在该锚点下的 ENU 水平坐标等于 target_xy。返回 (lat0, lon0, 迭代次数)。"""
    t = np.asarray(target_xy, np.float64)
    lat0, lon0 = float(landmark.lat_deg), float(landmark.lon_deg)
    it = 0
    for it in range(1, max_iter + 1):
        r = _landmark_xy(landmark.lat_deg, landmark.lon_deg, lat0, lon0) - t
        if float(np.abs(r).max()) < tol_m:
            return lat0, lon0, it - 1
        J = np.empty((2, 2))
        for j, (dlat, dlon) in enumerate(((H_STEP_DEG, 0.0), (0.0, H_STEP_DEG))):
            fp = _landmark_xy(landmark.lat_deg, landmark.lon_deg, lat0 + dlat, lon0 + dlon)
            fm = _landmark_xy(landmark.lat_deg, landmark.lon_deg, lat0 - dlat, lon0 - dlon)
            J[:, j] = (fp - fm) / (2 * H_STEP_DEG)
        d = np.linalg.solve(J, r)
        lat0 -= float(d[0])
        lon0 -= float(d[1])
    return lat0, lon0, it


def solve_anchor(landmark: Landmark | None, fallback: tuple[float, float, float] | None, peak_xy: tuple[float, float],
                 dtm_at_peak_m: float) -> dict:
    """返回 coordinate.json 的 anchor 对象（synthetic，已按 M03 §6.4 取整）与 T_ecef_world。"""
    if landmark is not None:
        lat0, lon0, _ = solve_origin_latlon(landmark, peak_xy)
        h_msl = landmark.base_msl_m - dtm_at_peak_m
        label = (f"illustrative: world origin placed by offset from {landmark.name} "
                 f"(tallest HAG peak at E={peak_xy[0]:.1f}, N={peak_xy[1]:.1f})")
        unc = {"horizontal": 50.0, "vertical": 40.0}
    else:
        if fallback is None:
            raise ValueError("synthetic anchor needs a landmark or a fallback (lat, lon, hMsl)")
        lat0, lon0, h_msl = fallback
        label = "illustrative: city centre, no landmark evidence"
        unc = {"horizontal": 5000.0, "vertical": 40.0}
    lat0, lon0, h_msl = round(lat0, 7), round(lon0, 7), round(float(h_msl), 1)
    anchor = {"kind": "synthetic", "georeferenced": False, "datum": "WGS84", "lonDeg": lon0, "latDeg": lat0,
              "hEllipsoidM": h_msl, "hMslM": h_msl, "geoid": None, "epoch": None, "uncertaintyM": unc, "label": label}
    return anchor


def t_ecef_world_json(anchor: dict) -> list[list[float]]:
    """由 anchor 计算 `T_ecef_world`（M02 唯一实现），保存到 1e-6。"""
    T = np.asarray(T_ecef_world(Anchor.from_coordinate({"anchor": anchor})), np.float64)
    return [[round(float(v), 6) + 0.0 for v in row] for row in T]
