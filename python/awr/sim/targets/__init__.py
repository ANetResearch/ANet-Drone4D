"""M18 识别物与敏感范围（AWR-04 §7；M18-M19 PRD；ADR-098、ADR-099；所有者 M18，WP-05）。

导入本包无副作用：登记只在组合根入口 `awr.sim.targets.plugin` 中执行（sim-core `plugins:` 写该模块）；当环境变量 `AWR_PLUGINS`
列出本包名时，导入本包转而导入 `.plugin`（与 M13 同一约定）。M19、M20 与工具经本包取纯函数与只读类型：

- `critical_dim`、`keepout_volumes`（签名冻结于 WP-01，`tests/contracts/test_d2_stubs.py` 比对）、`effective_radius`、
  `suggested_radius`、`predict_entry`、`inside`、`body_volumes`；
- `TargetConfig`、`TargetState1`、`TargetView`、`Volume`、`VehicleSignature`；
- `LosCache` 与 `shared_cache()`（共享视线缓存与射线预算，M18-FR-023）；
- 运行时 `awr.sim.targets.plugin.runtime()`（`view()`、`config(id)`、`state1(id)`、`keepout(id)`、`sound_bands_now(id, t)`）。
"""

from __future__ import annotations

import os as _os

from .effective import DEFAULT_MARGIN_M, SuggestedRadius, effective_radius, keepout_volumes, suggested_radius
from .geometry import Volume, body_volumes, critical_dim, inside, inside_points, predict_entry, signed_distance
from .los_cache import LosCache, LosResult, shared_cache
from .model import TargetConfig, TargetError, TargetState1, TargetView, class_defaults
from .signature import P600, VehicleSignature, signature_from_doc, signature_of

__all__ = [
    "DEFAULT_MARGIN_M", "P600", "LosCache", "LosResult", "SuggestedRadius", "TargetConfig", "TargetError", "TargetState1", "TargetView",
    "VehicleSignature", "Volume", "body_volumes", "class_defaults", "critical_dim", "effective_radius", "inside", "inside_points",
    "keepout_volumes", "predict_entry", "shared_cache", "signature_from_doc", "signature_of", "signed_distance", "suggested_radius",
]

if "awr.sim.targets" in [p.strip() for p in (_os.environ.get("AWR_PLUGINS") or "").split(",")]:
    from . import plugin as _plugin  # noqa: F401
