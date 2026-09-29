"""M13 传感器仿真：传感器契约装配、云台与位姿、相机几何、GNSS/IMU 噪声、Mock 检测器与热成像、MID-360 花样（桩）。

所有者：M13（AWR-03 §4.3）。导入本包无副作用（M13-FR-004、M13-NFR-010）：注册只在组合根入口
`awr.sim.sensors.plugin` 中执行。过渡：`configs/runtime.yaml` 的 `plugins:` 目前写的是本包名（已提请 M11 改为
`awr.sim.sensors.plugin`）；在此之前，只有当环境变量 `AWR_PLUGINS`（sim-core 组合根的插件清单）列出本包时，导入本包才转而
导入 `.plugin`。普通 `import awr.sim.sensors`（测试、agent-runtime、工具）不触发任何登记。

纯函数模块 `detector`（expected_pd、bayes_miss）与 `thermal_mock`（render_thermal_frame）只依赖 numpy。
"""

from __future__ import annotations

import os as _os

if "awr.sim.sensors" in [p.strip() for p in (_os.environ.get("AWR_PLUGINS") or "").split(",")]:
    from . import plugin as _plugin  # noqa: F401
