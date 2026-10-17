"""可打包相机核心（M25 §6.3.2、§6.4）：相机模型、reversed-Z 投影、外观场景分块与 EGL 光栅化。

不依赖服务、sim-core、bench 与 torch；`raster` 子模块按需导入 moderngl（无 GPU 的主机只用 `model`、`proj`、`scene`）。
"""

from awr.sensorsvc.camera.model import CameraModel, IspConfig, NoiseConfig
from awr.sensorsvc.camera.proj import R_FLU_OPT, look_at_R, proj_reversed_z, project_points, world_to_opt

__all__ = ["R_FLU_OPT", "CameraModel", "IspConfig", "NoiseConfig", "look_at_R", "proj_reversed_z", "project_points", "world_to_opt"]
