"""相机模型（M25 §6.3.2；AWR-05 §5.4.2）：内参以像素中心 0.5 约定（COLMAP），`dist` 描述交付图像，帧率为有理数十进制字符串。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

__all__ = ["CameraModel", "IspConfig", "NoiseConfig"]


@dataclass(frozen=True)
class IspConfig:
    ae: Literal["auto", "fixed"] = "fixed"
    ev: float = 0.0                       # 固定增益 2^ev
    ae_target: float = 0.18               # 线性中灰
    ae_alpha: float = 0.3
    ae_gain_range: tuple[float, float] = (0.5, 2.0)
    ae_error_amp: float = 0.0
    ae_error_period_s: float = 17.0
    vignette_k: float = 0.3
    tone: Literal["none", "filmic_v1"] = "none"
    jpeg_quality: int = 90


@dataclass(frozen=True)
class NoiseConfig:
    read_sigma_dn: float = 0.0            # 线性域，满量程 255
    shot_k_dn: float = 0.0                # σ² = shot_k·x + read_sigma²


@dataclass(frozen=True)
class CameraModel:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    dist: tuple[float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0)
    near_m: float = 0.3
    far_m: float = 3000.0
    fps: str = "10"
    exposure_s: float = 0.008
    readout_s: float = 0.0
    blur_subframes: int = 1
    rs_bands: int = 1
    shadow: Literal["contact", "map"] = "contact"
    isp: IspConfig = field(default_factory=IspConfig)
    noise: NoiseConfig = field(default_factory=NoiseConfig)

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or self.fx <= 0 or self.fy <= 0:
            raise ValueError("camera size and focal lengths must be positive")
        if not 0 < self.near_m < self.far_m:
            raise ValueError("need 0 < near_m < far_m")

    @staticmethod
    def from_hfov(w: int, h: int, hfov_deg: float, **kw) -> CameraModel:
        """方形像素，主点居中：fx = fy = (W / 2) / tan(hfov / 2)，cx = W / 2，cy = H / 2（像素中心 0.5 约定下的几何中心）。"""
        f = (w / 2) / math.tan(math.radians(hfov_deg) / 2)
        return CameraModel(w, h, f, f, w / 2, h / 2, **kw)

    @property
    def hfov_deg(self) -> float:
        return math.degrees(2 * math.atan(self.width / (2 * self.fx)))

    def K(self) -> tuple[tuple[float, float, float], ...]:
        return ((self.fx, 0.0, self.cx), (0.0, self.fy, self.cy), (0.0, 0.0, 1.0))

    def has_distortion(self) -> bool:
        return any(v != 0.0 for v in self.dist)


# 相机档位（AWR-20 §15.6.6、§15.16.3）：交付分辨率、卷帘读出与缺省曝光（日间 1 ms）；采集规格中相机的 `profile` 取其一，
# 显式给出的 size、readout_s、exposure_s 覆盖档位值。
CAMERA_PROFILES: dict[str, dict] = {
    "rgb720-gs": {"size": (1280, 720), "readout_s": 0.0, "exposure_s": 0.008},
    "rgb1080-rs": {"size": (1920, 1080), "readout_s": 0.020, "exposure_s": 0.001},
}
