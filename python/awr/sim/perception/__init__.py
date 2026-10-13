"""M19 感知与捕获（AWR-04 §6；M18-M19 PRD §4.2、§6.6–§6.13；ADR-100、ADR-101；所有者 M19，WP-06）。

导入本包无副作用：登记只在组合根入口 `awr.sim.perception.plugin` 中执行（sim-core `plugins:` 写该模块，须在 `awr.sim.targets`
之后）；当环境变量 `AWR_PLUGINS` 列出本包名时，导入本包转而导入 `.plugin`（与 M13、M18 同一约定）。M20、M21 与工具经本包取纯函数
与只读类型（签名冻结于 WP-01，`tests/contracts/test_d2_stubs.py` 比对）：

- `perception_level`、`perception_level_batch`、`feasible_view`、`feasibility_window`、`zoom_for_level`；
- `LevelResult`、`LevelBatch`、`FeasibleView`、`SensorSpecP`、`SensorState`、`ViewGeom`、`EnvSample`、`Illuminator`；
- 光学与判据：`gsd_m`、`pixels_on_target`、`p_prime_um`、`hfov_rad`、`ttpf`、`n_req_for`、`level_of`；
- 参考传感器 `gx40`、`gx40_nir`、`lwir640`、`mmw77`、`mic8`、`mid360`；可行窗口明细 `feasibility_detail`；参考作用距离 `ref_ranges`；
- 运行时 `awr.sim.perception.plugin.runtime()`（`set_requirement`、`capture_state`、`track`、`set_cue`）。
"""

from __future__ import annotations

import os as _os

from .feasibility import (
    FeasibilityResult,
    FeasibleView,
    env_for,
    feasibility_detail,
    feasibility_window,
    feasible_view,
    zoom_for_level,
)
from .johnson import level_of, n_req_for, ttpf
from .level import LevelBatch, LevelResult, ViewGeomBatch, perception_level, perception_level_batch
from .optics import f_number, gsd_m, hfov_rad, p_eff_um, p_prime_um, pixels_on_target, sigma_per_m, tau, vfov_rad
from .refs import ref_ranges
from .specs import (
    AcousticSpec,
    EnvSample,
    Illuminator,
    LidarSpec,
    PerceptionError,
    RadarSpec,
    SensorSpecP,
    SensorState,
    ViewGeom,
    gx40,
    gx40_nir,
    lwir640,
    mic8,
    mid360,
    mmw77,
)

__all__ = [
    "AcousticSpec", "EnvSample", "FeasibilityResult", "FeasibleView", "Illuminator", "LevelBatch", "LevelResult", "LidarSpec",
    "PerceptionError", "RadarSpec", "SensorSpecP", "SensorState", "ViewGeom", "ViewGeomBatch", "env_for", "f_number", "feasibility_detail",
    "feasibility_window", "feasible_view", "gsd_m", "gx40", "gx40_nir", "hfov_rad", "level_of", "lwir640", "mic8", "mid360", "mmw77",
    "n_req_for", "p_eff_um", "p_prime_um", "perception_level", "perception_level_batch", "pixels_on_target", "ref_ranges", "sigma_per_m",
    "tau", "ttpf", "vfov_rad", "zoom_for_level",
]

if "awr.sim.perception" in [p.strip() for p in (_os.environ.get("AWR_PLUGINS") or "").split(",")]:
    from . import plugin as _plugin  # noqa: F401
