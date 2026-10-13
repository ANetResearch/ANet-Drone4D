"""M20 任务编排、机巢与接力监控的 sim-core 插件（AWR-04 §8、§9；M20 PRD §6.1、§9.1；所有者 M20，WP-07）。

导入本包无副作用：登记只在组合根入口 `awr.sim.tasking.plugin` 中执行（sim-core `plugins:` 写该模块，须在 `awr.sim.targets`、
`awr.sim.perception` 之后）；当环境变量 `AWR_PLUGINS` 列出本包名时，导入本包转而导入 `.plugin`（与 M13、M18、M19 同一约定）。
算法在纯算法包 `awr.tasking`（共享规划服务 worker 与批处理 inline 执行）；本包是运行时：

- `runtime`：TaskingRuntime（stage 入口、作业提交与生效、快照、任务生命周期、状态发布）；`jobs`：规划作业的三种执行模式；
- `nests`：机巢（起降位、电池队列、充电与换电）；`book`：任务、工作项与架次记录；`executor`：架次执行（内部调用）；
- `relay`：接力控制器（预检、站位、派遣、交接、预报、跟随、统计）；`guard`：5 Hz 禁入守卫与撤离；`visits`：巡查访问栅格；
- `models`：机型摘要与成像传感器；`queries`：命令与查询处理、dry-run、可持续性。
运行时 `awr.sim.tasking.plugin.runtime()`。
"""

from __future__ import annotations

import os as _os

if "awr.sim.tasking" in [p.strip() for p in (_os.environ.get("AWR_PLUGINS") or "").split(",")]:
    from . import plugin as _plugin  # noqa: F401
